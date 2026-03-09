"""
cleanup/handler.py

Cleans up all resources created for a single volume scan:
  - Detaches and deletes the scan volume (copy created by create_volume)
  - Deletes the snapshot (created by create_snapshot)

Always runs, even when prior steps failed (via Catch: States.ALL).
Handles already-detached/deleted resources gracefully.
"""
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")


def lambda_handler(event, context):
    scan_volume_id = event.get("scan_volume_id")
    snapshot_id = event.get("snapshot_id")
    volume_id = event.get("volume_id", "unknown")
    execution_id = event.get("execution_id", "unknown")

    logger.info(
        "Cleanup for volume %s: scan_vol=%s snapshot=%s",
        volume_id, scan_volume_id, snapshot_id,
    )

    errors = []

    # --- Detach and delete scan volume ---
    if scan_volume_id:
        try:
            detach_volume(scan_volume_id)
        except Exception as e:
            logger.warning("Error detaching %s: %s", scan_volume_id, e)
            errors.append(f"detach: {e}")

        try:
            delete_volume(scan_volume_id)
        except Exception as e:
            logger.warning("Error deleting volume %s: %s", scan_volume_id, e)
            errors.append(f"delete_volume: {e}")
    else:
        logger.info("No scan_volume_id provided — skipping volume cleanup")

    # --- Delete snapshot ---
    if snapshot_id:
        try:
            delete_snapshot(snapshot_id)
        except Exception as e:
            logger.warning("Error deleting snapshot %s: %s", snapshot_id, e)
            errors.append(f"delete_snapshot: {e}")
    else:
        logger.info("No snapshot_id provided — skipping snapshot cleanup")

    result = {
        "cleaned_up": True,
        "volume_id": volume_id,
        "scan_volume_id": scan_volume_id,
        "snapshot_id": snapshot_id,
        "errors": errors,
    }

    if errors:
        logger.warning("Cleanup completed with %d errors: %s", len(errors), errors)
    else:
        logger.info("Cleanup successful for volume %s", volume_id)

    return result


def detach_volume(volume_id):
    """Detach a volume, ignoring if it's already detached."""
    try:
        vol = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    except ec2.exceptions.ClientError as e:
        if "InvalidVolume.NotFound" in str(e):
            logger.info("Volume %s not found — already deleted", volume_id)
            return
        raise

    attachments = vol.get("Attachments", [])
    if not attachments:
        logger.info("Volume %s has no attachments", volume_id)
        return

    attached_states = [a["State"] for a in attachments if a["State"] not in ("detached", "detaching")]
    if not attached_states:
        logger.info("Volume %s attachments already detached/detaching", volume_id)
        return

    logger.info("Detaching volume %s (force=True)", volume_id)
    try:
        ec2.detach_volume(VolumeId=volume_id, Force=True)
    except ec2.exceptions.ClientError as e:
        if "IncorrectState" in str(e) or "InvalidAttachment" in str(e):
            logger.info("Volume %s detach error ignored: %s", volume_id, e)
        else:
            raise

    # Wait for detachment
    waiter = ec2.get_waiter("volume_available")
    waiter.wait(
        VolumeIds=[volume_id],
        WaiterConfig={"Delay": 5, "MaxAttempts": 24},  # up to 2 minutes
    )
    logger.info("Volume %s is now available (detached)", volume_id)


def delete_volume(volume_id):
    """Delete a volume, ignoring if already deleted."""
    try:
        ec2.delete_volume(VolumeId=volume_id)
        logger.info("Deleted volume %s", volume_id)
    except ec2.exceptions.ClientError as e:
        if "InvalidVolume.NotFound" in str(e):
            logger.info("Volume %s already deleted", volume_id)
        else:
            raise


def delete_snapshot(snapshot_id):
    """Delete a snapshot, ignoring if already deleted."""
    try:
        ec2.delete_snapshot(SnapshotId=snapshot_id)
        logger.info("Deleted snapshot %s", snapshot_id)
    except ec2.exceptions.ClientError as e:
        if "InvalidSnapshot.NotFound" in str(e):
            logger.info("Snapshot %s already deleted", snapshot_id)
        else:
            raise
