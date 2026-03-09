"""
check_snapshot/handler.py

Polls all snapshot IDs until every one reaches state 'completed',
then calls send_task_success to unblock the WaitSnapshots state.

If Lambda is about to time out, calls send_task_failure instead.
"""
import time
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")
sfn = boto3.client("stepfunctions")

POLL_INTERVAL_SECONDS = 15
TIMEOUT_BUFFER_MS = 30_000  # 30 s safety margin before Lambda times out


def lambda_handler(event, context):
    task_token = event["task_token"]
    snapshots = event["snapshots"]
    execution_id = event.get("execution_id", "")

    # snapshots is a list of {snapshot_id, volume_id, execution_id}
    snapshot_ids = [s["snapshot_id"] for s in snapshots]
    logger.info("Waiting for %d snapshots: %s", len(snapshot_ids), snapshot_ids)

    while True:
        if context.get_remaining_time_in_millis() < TIMEOUT_BUFFER_MS:
            msg = f"Lambda timeout imminent — {len(snapshot_ids)} snapshots not yet complete"
            logger.error(msg)
            sfn.send_task_failure(
                taskToken=task_token,
                error="SnapshotTimeout",
                cause=msg,
            )
            return

        completed, pending = check_snapshots(snapshot_ids)
        logger.info(
            "Snapshot status — completed: %d, pending: %d",
            len(completed),
            len(pending),
        )

        if not pending:
            logger.info("All snapshots complete")
            sfn.send_task_success(
                taskToken=task_token,
                output='{"snapshots_complete": true}',
            )
            return

        time.sleep(POLL_INTERVAL_SECONDS)


def check_snapshots(snapshot_ids):
    response = ec2.describe_snapshots(SnapshotIds=snapshot_ids)
    completed = []
    pending = []
    for snap in response["Snapshots"]:
        if snap["State"] == "completed":
            completed.append(snap["SnapshotId"])
        else:
            pending.append(
                {"id": snap["SnapshotId"], "state": snap["State"], "progress": snap.get("Progress", "?")}
            )
    return completed, pending
