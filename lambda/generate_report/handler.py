"""
generate_report/handler.py

Reads all per-volume JSON results from S3, aggregates them into a
summary.json, and writes it to s3://<bucket>/scans/<execution_id>/summary.json.
"""
import os
import json
import boto3
import logging
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


def lambda_handler(event, context):
    execution_id = event["execution_id"]
    scan_results = event.get("scan_results", [])
    results_bucket = os.environ["RESULTS_BUCKET"]

    logger.info(
        "Generating report for execution %s (%d volumes)",
        execution_id, len(scan_results)
    )

    # Collect all per-volume results from S3
    volume_reports = []
    total_findings = 0
    errors = []

    prefix = f"scans/{execution_id}/"
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=results_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("summary.json"):
                continue  # Skip any existing summary

            try:
                body = s3.get_object(Bucket=results_bucket, Key=key)["Body"].read()
                data = json.loads(body)
                volume_reports.append(data)

                # Count findings — support different output schemas
                findings = (
                    data.get("findings", [])
                    or data.get("matches", [])
                    or data.get("results", [])
                )
                total_findings += len(findings)

                if data.get("status") in ("error", "failed"):
                    errors.append(data.get("volume_id", key))

            except Exception as e:
                logger.warning("Could not read result %s: %s", key, e)
                errors.append(key)

    summary = {
        "execution_id": execution_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_volumes": len(volume_reports),
        "total_findings": total_findings,
        "volumes_with_errors": errors,
        "scan_status": "complete_with_errors" if errors else "complete",
        "volumes": volume_reports,
    }

    report_key = f"scans/{execution_id}/summary.json"
    s3.put_object(
        Bucket=results_bucket,
        Key=report_key,
        Body=json.dumps(summary, indent=2, default=str),
        ContentType="application/json",
    )

    logger.info(
        "Report written to s3://%s/%s (%d volumes, %d findings)",
        results_bucket, report_key, len(volume_reports), total_findings,
    )

    return {
        "report_s3_key": report_key,
        "report_s3_uri": f"s3://{results_bucket}/{report_key}",
        "total_volumes": len(volume_reports),
        "total_findings": total_findings,
        "execution_id": execution_id,
    }
