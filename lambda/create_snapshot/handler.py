"""
create_snapshot/handler.py

Creates an EBS snapshot of the given volume and tags it so the scanner
can find and clean it up later.
"""
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")


def lambda_handler(event, context):
    volume_id = event["volume_id"]
    execution_id = event["execution_id"]

    logger.info("Creating snapshot for volume %s (execution %s)", volume_id, execution_id)

    response = ec2.create_snapshot(
        VolumeId=volume_id,
        Description=f"YARA scanner snapshot — execution {execution_id}",
        TagSpecifications=[
            {
                "ResourceType": "snapshot",
                "Tags": [
                    {"Key": "ManagedBy", "Value": "yara-scanner"},
                    {"Key": "SourceVolumeId", "Value": volume_id},
                    {"Key": "ExecutionId", "Value": execution_id},
                    {"Key": "Name", "Value": f"yara-scan-{volume_id}"},
                ],
            }
        ],
    )

    snapshot_id = response["SnapshotId"]
    logger.info("Created snapshot %s for volume %s", snapshot_id, volume_id)

    return {
        "snapshot_id": snapshot_id,
        "volume_id": volume_id,
        "execution_id": execution_id,
    }
