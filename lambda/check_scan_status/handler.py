"""
check_scan_status/handler.py

Polls SSM GetCommandInvocation until the command succeeds or fails,
then calls send_task_success or send_task_failure via the task_token
that run_scan stored in SSM Parameter Store.

This Lambda is invoked periodically by Step Functions (waitForTaskToken
on the MountAndScan state). The task_token is retrieved from Parameter Store
using the execution_id + volume_id composite key.
"""
import time
import json
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
sfn = boto3.client("stepfunctions")

POLL_INTERVAL_SECONDS = 15
TIMEOUT_BUFFER_MS = 30_000

# Terminal SSM status values
TERMINAL_SUCCESS = {"Success"}
TERMINAL_FAILURE = {"Failed", "Cancelled", "TimedOut", "Cancelling", "Undeliverable", "Terminated"}


def lambda_handler(event, context):
    command_id = event["command_id"]
    instance_id = event["instance_id"]
    volume_id = event["volume_id"]
    execution_id = event["execution_id"]
    task_token_param = event["task_token_param"]

    # Retrieve task_token from SSM Parameter Store
    task_token = get_task_token(task_token_param)
    if not task_token:
        logger.error("Could not retrieve task token from %s", task_token_param)
        return

    logger.info(
        "Polling SSM command %s on instance %s (volume %s)",
        command_id, instance_id, volume_id
    )

    while True:
        if context.get_remaining_time_in_millis() < TIMEOUT_BUFFER_MS:
            msg = f"Lambda timeout imminent — SSM command {command_id} still running"
            logger.error(msg)
            sfn.send_task_failure(
                taskToken=task_token,
                error="ScanTimeout",
                cause=msg,
            )
            cleanup_token_param(task_token_param)
            return

        status, detail = get_command_status(command_id, instance_id)
        logger.info("SSM command %s status: %s", command_id, status)

        if status in TERMINAL_SUCCESS:
            logger.info("SSM command %s succeeded", command_id)
            sfn.send_task_success(
                taskToken=task_token,
                output=json.dumps({
                    "scan_complete": True,
                    "command_id": command_id,
                    "volume_id": volume_id,
                    "execution_id": execution_id,
                }),
            )
            cleanup_token_param(task_token_param)
            return

        if status in TERMINAL_FAILURE:
            msg = f"SSM command {command_id} failed with status {status}: {detail}"
            logger.error(msg)
            sfn.send_task_failure(
                taskToken=task_token,
                error="ScanFailed",
                cause=msg,
            )
            cleanup_token_param(task_token_param)
            return

        # Still running — continue polling
        time.sleep(POLL_INTERVAL_SECONDS)


def get_command_status(command_id, instance_id):
    try:
        response = ssm.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id,
        )
        status = response["Status"]
        detail = response.get("StatusDetails", "")
        return status, detail
    except ssm.exceptions.InvocationDoesNotExist:
        # Command not yet delivered to instance — treat as pending
        return "Pending", "InvocationDoesNotExist"


def get_task_token(param_name):
    try:
        response = ssm.get_parameter(Name=param_name, WithDecryption=True)
        return response["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None


def cleanup_token_param(param_name):
    try:
        ssm.delete_parameter(Name=param_name)
    except Exception as e:
        logger.warning("Could not delete token parameter %s: %s", param_name, e)
