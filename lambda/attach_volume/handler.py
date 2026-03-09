"""
attach_volume/handler.py

Discovers the scanner EC2 instance by its ASG name tag, then attaches
the scan volume to it.
"""
import os
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")

# Device name passed to the EC2 API (Nitro maps this to /dev/nvme*n1)
DEVICE_NAME = "/dev/sdf"


def lambda_handler(event, context):
    scan_volume_id = event["scan_volume_id"]
    volume_id = event["volume_id"]
    snapshot_id = event["snapshot_id"]
    execution_id = event["execution_id"]

    instance_id = find_scanner_instance()
    logger.info(
        "Attaching scan volume %s to scanner instance %s", scan_volume_id, instance_id
    )

    ec2.attach_volume(
        VolumeId=scan_volume_id,
        InstanceId=instance_id,
        Device=DEVICE_NAME,
    )

    logger.info("AttachVolume called for %s → %s", scan_volume_id, instance_id)

    return {
        "scan_volume_id": scan_volume_id,
        "volume_id": volume_id,
        "snapshot_id": snapshot_id,
        "execution_id": execution_id,
        "instance_id": instance_id,
        "device_name": DEVICE_NAME,
    }


def find_scanner_instance():
    """Find the running scanner EC2 by its ASG name tag (stack-unique)."""
    scanner_asg_name = os.environ["SCANNER_ASG_NAME"]
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:aws:autoscaling:groupName", "Values": [scanner_asg_name]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )

    instances = [
        i
        for r in response["Reservations"]
        for i in r["Instances"]
    ]

    if not instances:
        raise RuntimeError(
            f"No running scanner instance found in ASG '{scanner_asg_name}'. "
            "Ensure the ASG has launched and the instance is healthy."
        )

    if len(instances) > 1:
        logger.warning(
            "Multiple instances found in ASG '%s' (%d), using first: %s",
            scanner_asg_name,
            len(instances),
            instances[0]["InstanceId"],
        )

    return instances[0]["InstanceId"]
