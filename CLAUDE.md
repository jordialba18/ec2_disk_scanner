# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Deploy

```bash
# Minimum required (image must exist locally: docker images)
./scripts/deploy.sh yara-scanner:latest

# Full options
./scripts/deploy.sh yara-scanner:latest \
    --region us-east-1 \
    --profile my-profile \
    --stack yara-ec2-scanner \
    --bucket my-staging-bucket \
    --auto-scan \
    --output ./results
```

The script: creates staging bucket → zips each `lambda/*/handler.py` → uploads to S3 → `cloudformation package` → `cloudformation deploy` → `docker save | gzip | s3 cp` scanner image → polls SSM until Online → (optionally) invokes scan and waits.

## Trigger a scan manually

```bash
STACK=yara-ec2-scanner
REGION=us-east-1
FN=$(aws cloudformation describe-stacks --stack-name $STACK --region $REGION \
    --query "Stacks[0].Outputs[?OutputKey=='TriggerScanFunctionName'].OutputValue" \
    --output text)
aws lambda invoke --function-name "$FN" --payload '{}' \
    --cli-binary-format raw-in-base64-out --region $REGION response.json
cat response.json  # returns execution_arn, volume_count
```

## Cleanup (remove all resources)

```bash
STACK=yara-ec2-scanner && REGION=us-east-1
BUCKET=$(aws cloudformation describe-stacks --stack-name $STACK --region $REGION \
    --query "Stacks[0].Outputs[?OutputKey=='ResultsBucketName'].OutputValue" --output text)
aws s3 rm "s3://$BUCKET" --recursive
aws cloudformation delete-stack --stack-name $STACK --region $REGION
# Check for orphaned resources tagged ManagedBy=yara-scanner
aws ec2 describe-snapshots --filters "Name=tag:ManagedBy,Values=yara-scanner" \
    --query "Snapshots[*].SnapshotId" --output text
```

---

## Architecture

**Fully agentless:** no code is deployed to target EC2s. Instead, each EBS volume is snapshotted, the snapshot is materialized as a new volume in the scanner's AZ, attached to the scanner EC2, and scanned by a YARA Docker container via SSM Run Command. The scanner EC2 never has IAM access to target workloads.

### CloudFormation — nested stacks with StackPrefix

`root.yaml` orchestrates five nested stacks. All nested stacks receive a `StackPrefix` parameter (set to `!Ref AWS::StackName` from root) to name their resources predictably and avoid collisions between multiple deployments:

```
root.yaml
├── vpc.yaml         — VPC, private subnet, SSM/S3 VPC endpoints (air-gapped, no NAT GW)
├── storage.yaml     — S3 bucket ({StackPrefix}-results-{account}-{region}) + Secrets Manager
├── iam.yaml         — Lambda role, SFN role, scanner EC2 role + instance profile
├── scanner.yaml     — LaunchTemplate + ASG (min/max/desired=1)
└── orchestration.yaml — 10 Lambda functions + Step Functions state machine
```

Whenever adding a new named resource in a nested template, use `!Sub "${StackPrefix}-name"` not a hardcoded string. S3 bucket names (max 63 chars) and Lambda names (max 64 chars) are the binding constraints.

### Step Functions execution flow

```
ListVolumes (trigger_scan: action=list_volumes)
    ↓
CreateSnapshots  [Map, MaxConcurrency=0 — all snapshots in parallel]
    ↓
WaitSnapshots    [waitForTaskToken → check_snapshot polls until all complete]
    ↓
ScanVolumes      [Map, MaxConcurrency=1 — MUST be 1, single scanner EC2 / single /dev/sdf]
  ├── CreateVolume  (in scanner AZ)
  ├── AttachVolume  (discovers scanner EC2 by ASG name tag)
  ├── WaitVolumeAttached  [waitForTaskToken → check_volume_attached polls until attached]
  ├── MountAndScan  [waitForTaskToken → run_scan sends SSM command; check_scan_status polls]
  └── CleanupVolume (always via Catch: States.ALL — detach + delete volume + snapshot)
    ↓
GenerateReport → s3://{bucket}/scans/{execution_id}/summary.json
```

### Two distinct polling patterns

**Pattern 1 — Lambda holds the task token directly** (`check_snapshot`, `check_volume_attached`):
The `waitForTaskToken` state passes the token to the Lambda in the payload. The Lambda runs a `while True: time.sleep(15)` loop up to its timeout (840s), calling `sfn.send_task_success/failure` when done. Always guard with `context.get_remaining_time_in_millis() < 30_000` before the sleep to avoid a silent timeout.

**Pattern 2 — task token stored in SSM Parameter Store** (`run_scan` + `check_scan_status`):
`run_scan` stores the token at `/yara-scanner/task-tokens/{execution_id}/{volume_id}` as a SecureString, fires the SSM Run Command, and returns immediately. `check_scan_status` is a separate Lambda (also polling with `time.sleep`) that retrieves the token from SSM, polls `ssm.get_command_invocation`, and calls `send_task_success/failure`. This decouples the long-running SSM command from the Lambda that initiates it.

### NVMe device discovery

On Nitro-based EC2s, `/dev/sdf` (the device name sent to the EC2 API) is remapped to `/dev/nvme*n1`. Device identity is established by matching the EBS volume ID (minus the `vol-` prefix) against `/sys/block/nvmeXn1/device/serial`. The serial may be padded with spaces — always `tr -d ' '` before comparing. See `run_scan/handler.py:build_scan_script`.

### Scanner EC2 discovery

`attach_volume/handler.py` finds the scanner by filtering `ec2.describe_instances` on `tag:aws:autoscaling:groupName = $SCANNER_ASG_NAME`. The ASG name comes from `SCANNER_ASG_NAME` env var, set in CloudFormation from `ScannerStack.Outputs.ScannerASGName`. This is stack-unique — avoids picking up another deployment's scanner.

### YARA Docker image contract

The scanner image must:
- Accept `/scan` as the directory to scan (bind-mounted read-only)
- Accept `--output <file>` to write a JSON result file
- Write `{"status": "complete", "findings": [...]}` to the output file
- Exit 0 on success (findings or clean), non-zero on error

### Key environment variables (Lambda)

| Lambda | Key env vars |
|--------|-------------|
| `trigger_scan` | `STATE_MACHINE_ARN` |
| `attach_volume` | `SCANNER_ASG_NAME`, `SCANNER_AZ` |
| `run_scan` | `RESULTS_BUCKET`, `SCANNER_IMAGE_URI`, `SCANNER_AZ` |
| all others | `SCANNER_AZ` |

### Troubleshooting quick reference

| Symptom | Cause |
|---------|-------|
| `device_not_found` in scan result | NVMe serial mismatch; volume still attaching; check `check_volume_attached` logs |
| Docker load fails on scanner EC2 | Image tarball not uploaded; verify `s3://<bucket>/scanner-image/scanner.tar.gz` exists |
| SFN stuck at `WaitSnapshots` | Large volumes can take 30+ min per TB |
| SSM PingStatus not Online | EC2 userdata failed; check `/var/log/yara-scanner-init.log` via `get-console-output` |
| Orphaned volumes/snapshots after failure | Cleanup Lambda logs; manually delete resources tagged `ManagedBy=yara-scanner` |
