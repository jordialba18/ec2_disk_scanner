"""
check_volume_attached/handler.py

Polls the scan volume until its attachment state becomes 'attached',
then sends task success. Sends task failure if Lambda is about to time out.
"""
import time
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")
sfn = boto3.client("stepfunctions")

POLL_INTERVAL_SECONDS = 5
TIMEOUT_BUFFER_MS = 30_000


def lambda_handler(event, context):
    task_token = event["task_token"]
    scan_volume_id = event["scan_volume_id"]
    volume_id = event["volume_id"]
    snapshot_id = event["snapshot_id"]
    execution_id = event["execution_id"]
    instance_id = event["instance_id"]

    logger.info("Waiting for scan volume %s to attach to %s", scan_volume_id, instance_id)

    while True:
        if context.get_remaining_time_in_millis() < TIMEOUT_BUFFER_MS:
            msg = f"Lambda timeout imminent — volume {scan_volume_id} not yet attached"
            logger.error(msg)
            sfn.send_task_failure(
                taskToken=task_token,
                error="VolumeAttachTimeout",
                cause=msg,
            )
            return

        state = get_attachment_state(scan_volume_id)
        logger.info("Volume %s attachment state: %s", scan_volume_id, state)

        if state == "attached":
            logger.info("Volume %s is attached", scan_volume_id)
            import json
            sfn.send_task_success(
                taskToken=task_token,
                output=json.dumps({"attached": True, "scan_volume_id": scan_volume_id}),
            )
            return

        if state in ("detached", "error"):
            msg = f"Volume {scan_volume_id} entered unexpected state: {state}"
            logger.error(msg)
            sfn.send_task_failure(
                taskToken=task_token,
                error="VolumeAttachFailed",
                cause=msg,
            )
            return

        time.sleep(POLL_INTERVAL_SECONDS)


def get_attachment_state(volume_id):
    response = ec2.describe_volumes(VolumeIds=[volume_id])
    volume = response["Volumes"][0]
    attachments = volume.get("Attachments", [])
    if not attachments:
        return volume["State"]  # 'available', 'creating', etc.
    return attachments[0]["State"]
