# EC2 Agentless YARA Scanner

Fully agentless YARA scanner for all EBS volumes in an AWS account. No code runs on target EC2 instances. The system snapshots each volume, attaches copies to a dedicated scanner EC2, and runs a YARA container against each filesystem. Results land in S3.

## Architecture

```
User (Lambda invoke)
    │
    ▼
Step Functions State Machine
    ├─ ListVolumes
    ├─ CreateSnapshots (parallel, one per volume)
    ├─ WaitSnapshots (polls until all snapshots complete)
    └─ ScanVolumes (MaxConcurrency=1, per snapshot):
           CreateVolume → scanner AZ
           AttachVolume → scanner EC2
           WaitVolumeAttached
           MountAndScan (SSM Run Command):
               • NVMe device discovery via /sys/block/*/device/serial
               • mount -o ro
               • docker run YARA scanner image
               • aws s3 cp results
               • umount
           CleanupVolume (always runs via Catch: States.ALL)
    └─ GenerateReport → S3 summary.json
```

**Infrastructure:**
- Dedicated VPC (private subnet + NAT GW + SSM/S3/SecretsManager VPC endpoints)
- Scanner EC2 t3.medium (AL2023, Docker+SSM, ASG min/max/desired=1)
- S3 results bucket (90-day lifecycle)
- Secrets Manager secret for GHCR credentials
- All resources tagged `ManagedBy=yara-scanner`

## Project Structure

```
ec2_disk_scanner/
├── cloudformation/
│   ├── root.yaml           # Parent stack — parameters, nested stack wiring
│   ├── vpc.yaml            # VPC, subnets, NAT GW, VPC endpoints
│   ├── iam.yaml            # IAM roles (Lambda, SFN, scanner EC2)
│   ├── storage.yaml        # S3 bucket + Secrets Manager secret stub
│   ├── scanner.yaml        # Scanner EC2 launch template + ASG
│   └── orchestration.yaml  # 10 Lambda functions + Step Functions state machine
├── lambda/
│   ├── trigger_scan/       # List volumes → start SFN execution
│   ├── create_snapshot/    # CreateSnapshot + tag
│   ├── check_snapshot/     # Poll snapshots → send task token
│   ├── create_volume/      # CreateVolume in scanner AZ
│   ├── attach_volume/      # AttachVolume to scanner EC2
│   ├── check_volume_attached/  # Poll until attached → send task token
│   ├── run_scan/           # Send SSM Run Command
│   ├── check_scan_status/  # Poll SSM command → send task token
│   ├── cleanup/            # Detach + delete scan volume + snapshot
│   └── generate_report/    # Aggregate results → summary.json
├── step_functions/
│   └── state_machine.asl.json  # Reference ASL (embedded in orchestration.yaml)
└── scripts/
    └── deploy.sh           # End-to-end deploy: package + deploy + credentials + wait
```

## Prerequisites

- AWS CLI configured with credentials that have CloudFormation, EC2, Lambda, S3, IAM, SSM, and Secrets Manager permissions
- A YARA scanner Docker image hosted on GHCR (see [YARA Docker image](#yara-docker-image))

## Deployment

```bash
# Minimum — region taken from AWS CLI config
./scripts/deploy.sh ghcr.io/your-org/yara-scanner:latest <ghcr_user> <ghcr_token>

# Full options
./scripts/deploy.sh ghcr.io/your-org/yara-scanner:latest <ghcr_user> <ghcr_token> \
    --region us-west-2 \
    --stack yara-ec2-scanner \
    --bucket my-staging-bucket \
    --auto-scan \
    --output ./results
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--region` | from `aws configure` | AWS region (error if undetectable) |
| `--profile` | default | AWS CLI profile |
| `--stack` | `yara-ec2-scanner` | CloudFormation stack name |
| `--bucket` | auto-generated | Staging S3 bucket for Lambda ZIPs and CFN templates |
| `--auto-scan` | off | Trigger scan immediately after deploy and wait for completion |
| `--output` | `./results` | Directory to download `summary.json` into (with `--auto-scan`) |

The script performs these steps automatically:
1. Creates staging S3 bucket (idempotent)
2. Zips and uploads each Lambda handler
3. Packages CloudFormation nested templates
4. Deploys the root CloudFormation stack
5. Populates GHCR credentials in Secrets Manager
6. Polls SSM until the scanner EC2 reports Online (10 min timeout)
7. *(with `--auto-scan`)* Invokes the scan, polls until SUCCEEDED, downloads `summary.json`

## Running a Scan

```bash
STACK=yara-ec2-scanner
REGION=us-east-1

# Resolve function name and bucket from stack outputs
FN=$(aws cloudformation describe-stacks --stack-name $STACK --region $REGION \
    --query "Stacks[0].Outputs[?OutputKey=='TriggerScanFunctionName'].OutputValue" \
    --output text)
BUCKET=$(aws cloudformation describe-stacks --stack-name $STACK --region $REGION \
    --query "Stacks[0].Outputs[?OutputKey=='ResultsBucketName'].OutputValue" \
    --output text)

# Start a scan of all in-use EBS volumes
aws lambda invoke \
    --function-name "$FN" \
    --payload '{}' --cli-binary-format raw-in-base64-out \
    --region $REGION response.json
cat response.json
# Returns: {"execution_arn": "...", "execution_id": "...", "volume_count": N}

# Monitor progress
aws stepfunctions describe-execution --execution-arn <arn> --region $REGION

# View per-volume results
aws s3 ls "s3://$BUCKET/scans/<execution_id>/"

# View summary report
aws s3 cp "s3://$BUCKET/scans/<execution_id>/summary.json" -
```

## YARA Docker Image

The scanner expects a Docker image that:
- Accepts `/scan` as the path to scan (bind-mounted read-only)
- Accepts `--output <file>` flag to write JSON results
- Exits 0 on success (findings or no findings)
- Exits non-zero on error

Minimal example `Dockerfile`:
```dockerfile
FROM python:3.12-slim
RUN pip install yara-python
COPY rules/ /rules/
COPY scan.py /scan.py
ENTRYPOINT ["python3", "/scan.py", "--rules", "/rules"]
```

Minimal `scan.py`:
```python
import yara, json, argparse, os, sys

parser = argparse.ArgumentParser()
parser.add_argument("path")
parser.add_argument("--rules", default="/rules")
parser.add_argument("--output", default="/tmp/result.json")
args = parser.parse_args()

rules = yara.compile(args.rules)
matches = []
for root, dirs, files in os.walk(args.path):
    for f in files:
        path = os.path.join(root, f)
        try:
            m = rules.match(path)
            if m:
                matches.append({"file": path, "rules": [str(r) for r in m]})
        except Exception:
            pass

result = {"status": "complete", "findings": matches}
with open(args.output, "w") as fh:
    json.dump(result, fh)
print(f"Scan complete: {len(matches)} matches")
```

## Cost Considerations

Per scan execution:
- **Snapshots**: ~$0.05/GB-month (deleted after scan)
- **EBS volumes**: ~$0.08/GB-month gp3 (created and deleted per scan)
- **NAT Gateway**: ~$0.045/hour + data processing (for GHCR pulls)
- **Scanner EC2** t3.medium: ~$0.0416/hour (always running in ASG)
- **Lambda**: negligible
- **Step Functions**: $0.025 per 1,000 state transitions

To reduce costs when not scanning, you can manually set ASG desired=0. Remember to set it back to 1 before the next scan.

## Cleanup

```bash
STACK=yara-ec2-scanner
REGION=us-east-1

# Empty the results bucket first (S3 must be empty to delete the stack)
BUCKET=$(aws cloudformation describe-stacks --stack-name $STACK --region $REGION \
    --query "Stacks[0].Outputs[?OutputKey=='ResultsBucketName'].OutputValue" \
    --output text)
aws s3 rm "s3://$BUCKET" --recursive

# Delete the stack (removes all nested stacks, IAM roles, Secrets Manager secret, etc.)
aws cloudformation delete-stack --stack-name $STACK --region $REGION

# Check for orphaned snapshots/volumes from interrupted scans
aws ec2 describe-snapshots \
    --filters "Name=tag:ManagedBy,Values=yara-scanner" \
    --query "Snapshots[*].SnapshotId" --output text

aws ec2 describe-volumes \
    --filters "Name=tag:ManagedBy,Values=yara-scanner" \
    --query "Volumes[*].VolumeId" --output text
```

## Troubleshooting

| Symptom | Check |
|---------|-------|
| SSM agent not Online after deploy | EC2 userdata failed; check console output (`aws ec2 get-console-output`); verify SSM VPC endpoints exist; check `/var/log/yara-scanner-init.log` |
| `device_not_found` in scan result | NVMe serial mismatch; volume may still be attaching; check `check_volume_attached` Lambda logs |
| Docker pull fails | GHCR credentials not set or invalid; verify the secret value in Secrets Manager |
| Scan volume not deleted | Check Cleanup Lambda logs; run manual cleanup against resources tagged `ManagedBy=yara-scanner` |
| Step Functions execution stuck in WaitSnapshots | Large volume; snapshot can take 30+ min for multi-TB volumes |
| NTFS volume not mounting | `ntfs-3g` install may have failed; check `/var/log/yara-scanner-init.log` via `get-console-output` |

To manually check SSM status for the scanner EC2:
```bash
ASG=$(aws cloudformation describe-stacks --stack-name $STACK --region $REGION \
    --query "Stacks[0].Outputs[?OutputKey=='ScannerASGName'].OutputValue" --output text)
aws ssm describe-instance-information \
    --filters "Key=tag:aws:autoscaling:groupName,Values=$ASG" \
    --region $REGION
# PingStatus must be "Online"
```

## Security Notes

- Scanner EC2 is in a private subnet with no inbound access
- All S3 traffic goes through the S3 VPC Gateway Endpoint (never over the internet)
- SSM communication uses VPC Interface Endpoints
- Secrets Manager credentials never leave the VPC
- All scan volumes are read-only mounts
- Results bucket enforces SSL-only access
- EC2 metadata service requires IMDSv2 (HttpTokens=required)
