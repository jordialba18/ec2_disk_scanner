"""
create_volume/handler.py

Creates a new EBS volume in the scanner AZ from a completed snapshot.
The volume is tagged so cleanup can find and delete it.
"""
import os
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")


def lambda_handler(event, context):
    snapshot_id = event["snapshot_id"]
    volume_id = event["volume_id"]
    execution_id = event["execution_id"]
    scanner_az = os.environ["SCANNER_AZ"]

    logger.info(
        "Creating scan volume from snapshot %s in AZ %s", snapshot_id, scanner_az
    )

    response = ec2.create_volume(
        SnapshotId=snapshot_id,
        AvailabilityZone=scanner_az,
        VolumeType="gp3",
        TagSpecifications=[
            {
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "ManagedBy", "Value": "yara-scanner"},
                    {"Key": "SourceVolumeId", "Value": volume_id},
                    {"Key": "SnapshotId", "Value": snapshot_id},
                    {"Key": "ExecutionId", "Value": execution_id},
                    {"Key": "Name", "Value": f"yara-scan-vol-{volume_id}"},
                    {"Key": "Role", "Value": "yara-scan-volume"},
                ],
            }
        ],
    )

    scan_volume_id = response["VolumeId"]
    logger.info(
        "Created scan volume %s from snapshot %s", scan_volume_id, snapshot_id
    )

    return {
        "scan_volume_id": scan_volume_id,
        "snapshot_id": snapshot_id,
        "volume_id": volume_id,
        "execution_id": execution_id,
    }
