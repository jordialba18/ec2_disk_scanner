"""
run_scan/handler.py

Sends an SSM Run Command to the scanner EC2. The embedded bash script:
  1. Discovers the NVMe device via /sys/block/*/device/serial
  2. Detects filesystem type with blkid (ext4, xfs, ntfs, or partition p1)
  3. Mounts read-only
  4. Loads the YARA Docker image from S3 (if not already cached) and runs it
  5. Uploads results to S3
  6. Unmounts and cleans up

The Run Command returns a command_id. The actual completion is polled by
check_scan_status, which calls send_task_success/failure via the task_token
stored in an SSM parameter.
"""
import os
import json
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")

# SSM document for running shell commands
SSM_DOCUMENT = "AWS-RunShellScript"


def lambda_handler(event, context):
    task_token = event["task_token"]
    scan_volume_id = event["scan_volume_id"]
    volume_id = event["volume_id"]
    execution_id = event["execution_id"]
    instance_id = event["instance_id"]

    results_bucket = os.environ["RESULTS_BUCKET"]
    scanner_image_uri = os.environ["SCANNER_IMAGE_URI"]

    # Build the scan script with all parameters interpolated server-side
    scan_script = build_scan_script(
        scan_volume_id=scan_volume_id,
        volume_id=volume_id,
        execution_id=execution_id,
        results_bucket=results_bucket,
        scanner_image_uri=scanner_image_uri,
    )

    # Store task_token in SSM Parameter Store so check_scan_status can retrieve it
    param_name = f"/yara-scanner/task-tokens/{execution_id}/{volume_id}"
    ssm.put_parameter(
        Name=param_name,
        Value=task_token,
        Type="SecureString",
        Overwrite=True,
    )

    logger.info(
        "Sending SSM Run Command to %s for volume %s", instance_id, scan_volume_id
    )

    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=SSM_DOCUMENT,
        Parameters={
            "commands": [scan_script],
            "executionTimeout": ["3300"],  # 55 minutes
        },
        TimeoutSeconds=60,
        Comment=f"YARA scan: execution={execution_id} volume={volume_id}",
        CloudWatchOutputConfig={
            "CloudWatchLogGroupName": f"/yara-scanner/ssm/{execution_id}",
            "CloudWatchOutputEnabled": True,
        },
        # Store command_id + task_token mapping so check_scan_status can poll
        OutputS3BucketName=results_bucket,
        OutputS3KeyPrefix=f"ssm-output/{execution_id}/{volume_id}/",
    )

    command_id = response["Command"]["CommandId"]
    logger.info("SSM command %s sent", command_id)

    # Return command metadata so check_scan_status can poll
    # check_scan_status is NOT a waitForTaskToken step — it's invoked separately
    # via the task_token stored in SSM Parameter Store
    return {
        "command_id": command_id,
        "instance_id": instance_id,
        "scan_volume_id": scan_volume_id,
        "volume_id": volume_id,
        "execution_id": execution_id,
        "task_token_param": param_name,
    }


def build_scan_script(
    scan_volume_id, volume_id, execution_id, results_bucket, scanner_image_uri
):
    """Build the bash script that runs inside the scanner EC2."""
    # Strip 'vol-' prefix for NVMe serial matching
    vol_serial = scan_volume_id.replace("vol-", "")

    return f"""#!/bin/bash
set -euo pipefail
export VOL_ID="{volume_id}"
export SCAN_VOL_ID="{scan_volume_id}"
export VOL_SERIAL="{vol_serial}"
export EXECUTION_ID="{execution_id}"
export RESULTS_BUCKET="{results_bucket}"
export SCANNER_IMAGE_URI="{scanner_image_uri}"
export MOUNT_POINT="/mnt/scan/${{SCAN_VOL_ID}}"
export RESULT_FILE="/tmp/yara-result-${{VOL_ID}}.json"

echo "=== YARA Scan starting: $VOL_ID (scan volume $SCAN_VOL_ID) ==="
mkdir -p "$MOUNT_POINT"

# -----------------------------------------------------------------------
# 1. Discover NVMe device by serial (handles Nitro device remapping)
# /dev/sdf passed by EC2 API maps to /dev/nvme*n1 on Nitro instances
# The serial number in /sys/block/nvmeXn1/device/serial matches the
# volume ID (without the 'vol-' prefix).
# -----------------------------------------------------------------------
DEVICE=""
for d in /sys/block/nvme*n1; do
    serial=$(cat "$d/device/serial" 2>/dev/null || true)
    # Serial may be padded with spaces; trim
    serial=$(echo "$serial" | tr -d ' ')
    echo "Checking $d: serial=$serial (looking for $VOL_SERIAL)"
    if [[ "$serial" == "$VOL_SERIAL"* ]] || [[ "$serial" == *"$VOL_SERIAL"* ]]; then
        DEVICE="/dev/$(basename $d)"
        echo "Found device: $DEVICE"
        break
    fi
done

if [ -z "$DEVICE" ]; then
    echo "ERROR: Could not find NVMe device for volume $SCAN_VOL_ID" >&2
    # Write failure result
    echo '{{"status":"error","error":"device_not_found","volume_id":"'"$VOL_ID"'","execution_id":"'"$EXECUTION_ID"'"}}' > "$RESULT_FILE"
    aws s3 cp "$RESULT_FILE" "s3://$RESULTS_BUCKET/scans/$EXECUTION_ID/$VOL_ID.json"
    exit 1
fi

# -----------------------------------------------------------------------
# 2. Detect filesystem and mount read-only
# -----------------------------------------------------------------------
FS_TYPE=$(blkid -o value -s TYPE "$DEVICE" 2>/dev/null || echo "unknown")
echo "Filesystem type on $DEVICE: $FS_TYPE"

MOUNTED=false

try_mount() {{
    local dev="$1"
    local opts="$2"
    if mount -o ro,$opts "$dev" "$MOUNT_POINT" 2>/dev/null; then
        MOUNTED=true
        echo "Mounted $dev at $MOUNT_POINT (opts: ro,$opts)"
        return 0
    fi
    return 1
}}

case "$FS_TYPE" in
    ext2|ext3|ext4)
        try_mount "$DEVICE" "norecovery" || true
        ;;
    xfs)
        try_mount "$DEVICE" "norecovery,nouuid" || true
        ;;
    ntfs)
        # ntfs-3g supports read-only mount
        if command -v ntfs-3g &>/dev/null; then
            ntfs-3g -o ro,allow_other "$DEVICE" "$MOUNT_POINT" 2>/dev/null && MOUNTED=true || true
        fi
        ;;
    *)
        # Unknown or no filesystem at raw device — try common types and partition p1
        try_mount "$DEVICE" "norecovery" || \
        try_mount "$DEVICE" "" || \
        try_mount "${{DEVICE}}p1" "norecovery" || \
        try_mount "${{DEVICE}}p1" "" || true
        ;;
esac

if [ "$MOUNTED" = "false" ]; then
    echo "WARNING: Could not mount $DEVICE — writing unmountable result"
    echo '{{"status":"skipped","reason":"unmountable","volume_id":"'"$VOL_ID"'","execution_id":"'"$EXECUTION_ID"'"}}' > "$RESULT_FILE"
    aws s3 cp "$RESULT_FILE" "s3://$RESULTS_BUCKET/scans/$EXECUTION_ID/$VOL_ID.json"
    exit 0
fi

# -----------------------------------------------------------------------
# 3. Load scanner image from S3 (if not already in Docker cache) and run
# -----------------------------------------------------------------------
if ! docker image inspect "$SCANNER_IMAGE_URI" &>/dev/null; then
    echo "Loading scanner image from S3..."
    aws s3 cp "s3://$RESULTS_BUCKET/scanner-image/scanner.tar.gz" /tmp/scanner.tar.gz
    docker load < /tmp/scanner.tar.gz
    rm -f /tmp/scanner.tar.gz
else
    echo "Scanner image already loaded: $SCANNER_IMAGE_URI"
fi

echo "Running YARA scan on $MOUNT_POINT..."
docker run --rm \
    -v "$MOUNT_POINT:/scan:ro" \
    -e VOLUME_ID="$VOL_ID" \
    -e EXECUTION_ID="$EXECUTION_ID" \
    -e OUTPUT_FILE="/tmp/result.json" \
    -v /tmp:/tmp \
    "$SCANNER_IMAGE_URI" \
    --output /tmp/result.json \
    /scan 2>&1 | tee /tmp/yara-scan-output.txt

# -----------------------------------------------------------------------
# 4. Collect results and upload to S3
# -----------------------------------------------------------------------
if [ -f /tmp/result.json ]; then
    cp /tmp/result.json "$RESULT_FILE"
else
    # Build a minimal result from stdout if image doesn't write JSON
    SCAN_OUTPUT=$(cat /tmp/yara-scan-output.txt | head -1000)
    python3 -c "
import json, sys
output = sys.stdin.read()
result = {{
    'status': 'complete',
    'volume_id': '$VOL_ID',
    'execution_id': '$EXECUTION_ID',
    'raw_output': output
}}
print(json.dumps(result))
" <<< "$SCAN_OUTPUT" > "$RESULT_FILE"
fi

# Annotate result with metadata
python3 -c "
import json
with open('$RESULT_FILE') as f:
    data = json.load(f)
data['volume_id'] = '$VOL_ID'
data['execution_id'] = '$EXECUTION_ID'
data['scan_volume_id'] = '$SCAN_VOL_ID'
data['status'] = data.get('status', 'complete')
with open('$RESULT_FILE', 'w') as f:
    json.dump(data, f)
"

echo "Uploading results to s3://$RESULTS_BUCKET/scans/$EXECUTION_ID/$VOL_ID.json"
aws s3 cp "$RESULT_FILE" "s3://$RESULTS_BUCKET/scans/$EXECUTION_ID/$VOL_ID.json"

# -----------------------------------------------------------------------
# 5. Cleanup
# -----------------------------------------------------------------------
umount "$MOUNT_POINT" 2>/dev/null || true
rm -rf "$MOUNT_POINT"
rm -f /tmp/result.json /tmp/yara-scan-output.txt "$RESULT_FILE"

echo "=== YARA Scan complete for $VOL_ID ==="
"""
