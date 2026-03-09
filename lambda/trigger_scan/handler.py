"""
trigger_scan/handler.py

Two modes:
  1. Direct invocation (payload = {}): lists all in-use EBS volumes and
     starts a Step Functions execution. Returns execution ARN + volume count.
  2. Called from state machine with action='list_volumes': just returns the
     list of volumes so the Map state can fan out.
"""
import json
import os
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")
sfn = boto3.client("stepfunctions")


def lambda_handler(event, context):
    action = event.get("action", "trigger")

    if action == "list_volumes":
        return list_volumes(event)
    else:
        return trigger_scan(event, context)


def list_volumes(event):
    """Return all in-use EBS volumes for the Map state."""
    execution_id = event.get("execution_id", "")
    volumes = get_all_volumes()
    logger.info("Found %d in-use volumes", len(volumes))
    return {
        "execution_id": execution_id,
        "volumes": volumes,
        "volume_count": len(volumes),
    }


def trigger_scan(event, context):
    """Start a Step Functions execution."""
    state_machine_arn = os.environ["STATE_MACHINE_ARN"]

    volumes = get_all_volumes()
    if not volumes:
        return {
            "message": "No in-use EBS volumes found — nothing to scan",
            "volume_count": 0,
        }

    response = sfn.start_execution(
        stateMachineArn=state_machine_arn,
        input=json.dumps({"triggered_by": "lambda", "volume_count": len(volumes)}),
    )

    execution_arn = response["executionArn"]
    # Extract a short ID from the ARN for readability
    execution_id = execution_arn.split(":")[-1]

    logger.info(
        "Started execution %s for %d volumes", execution_id, len(volumes)
    )

    return {
        "execution_arn": execution_arn,
        "execution_id": execution_id,
        "volume_count": len(volumes),
    }


def get_all_volumes():
    """Paginate through all in-use EBS volumes."""
    volumes = []
    paginator = ec2.get_paginator("describe_volumes")
    for page in paginator.paginate(
        Filters=[{"Name": "status", "Values": ["in-use"]}]
    ):
        for vol in page["Volumes"]:
            name = ""
            for tag in vol.get("Tags", []):
                if tag["Key"] == "Name":
                    name = tag["Value"]
                    break
            volumes.append(
                {
                    "volume_id": vol["VolumeId"],
                    "size_gb": vol["Size"],
                    "volume_type": vol["VolumeType"],
                    "availability_zone": vol["AvailabilityZone"],
                    "encrypted": vol["Encrypted"],
                    "name": name,
                }
            )
    return volumes
