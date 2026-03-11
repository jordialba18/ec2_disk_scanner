#!/usr/bin/env bash
# deploy.sh — Deploy YARA EC2 agentless scanner end-to-end
#
# Usage:
#   ./scripts/deploy.sh <SCANNER_IMAGE_URI> [OPTIONS]
#
# The scanner image must exist in the local Docker daemon before running this script.
# It will be saved as a gzip tarball and uploaded to the results S3 bucket.
#
# Options:
#   --region  <region>     AWS region (required if not in AWS CLI config)
#   --profile <profile>    AWS CLI profile name
#   --stack   <name>       CloudFormation stack name (default: yara-ec2-scanner)
#   --bucket  <bucket>     Staging S3 bucket (default: auto-generated)
#   --auto-scan            Trigger scan immediately after deploy and wait for results
#   --output  <dir>        Directory to download scan results to (default: ./results)

set -euo pipefail

# -----------------------------------------------------------------------
# Timing helpers
# -----------------------------------------------------------------------
DEPLOY_START=$(date +%s)

elapsed() {
    local now
    now=$(date +%s)
    local secs=$(( now - DEPLOY_START ))
    printf "%d:%02d" $(( secs / 60 )) $(( secs % 60 ))
}

step() {
    echo ""
    echo ">>> [$(elapsed)] $*"
}

# -----------------------------------------------------------------------
# Parse positional args (required)
# -----------------------------------------------------------------------
SCANNER_IMAGE_URI="${1:-}"

if [[ -z "$SCANNER_IMAGE_URI" ]]; then
    echo "Usage: $0 <SCANNER_IMAGE_URI> [OPTIONS]" >&2
    echo "       The image must exist in the local Docker daemon (docker images)." >&2
    exit 1
fi
shift 1

# Pre-flight: verify image exists locally
if ! docker image inspect "$SCANNER_IMAGE_URI" &>/dev/null; then
    echo "ERROR: Docker image '$SCANNER_IMAGE_URI' not found locally." >&2
    echo "       Build or pull the image first, then re-run deploy.sh." >&2
    exit 1
fi

# -----------------------------------------------------------------------
# Parse optional flags
# -----------------------------------------------------------------------
AWS_REGION=""
AWS_PROFILE=""
STACK_NAME="yara-ec2-scanner"
STAGING_BUCKET=""
AUTO_SCAN=false
OUTPUT_DIR="./results"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --region)    AWS_REGION="$2";     shift 2 ;;
        --profile)   AWS_PROFILE="$2";    shift 2 ;;
        --stack)     STACK_NAME="$2";     shift 2 ;;
        --bucket)    STAGING_BUCKET="$2"; shift 2 ;;
        --auto-scan) AUTO_SCAN=true;      shift   ;;
        --output)    OUTPUT_DIR="$2";     shift 2 ;;
        *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Export profile so all subsequent AWS CLI calls use it
if [[ -n "$AWS_PROFILE" ]]; then
    export AWS_PROFILE
fi

# -----------------------------------------------------------------------
# Region detection
# -----------------------------------------------------------------------
if [[ -z "$AWS_REGION" ]]; then
    AWS_REGION=$(aws configure get region 2>/dev/null || true)
fi

if [[ -z "$AWS_REGION" ]]; then
    # Endpoint-based: if credentials are valid the SDK infers region from metadata
    AWS_REGION=$(aws ec2 describe-availability-zones \
        --query "AvailabilityZones[0].RegionName" --output text 2>/dev/null || true)
fi

if [[ -z "$AWS_REGION" || "$AWS_REGION" == "None" ]]; then
    echo "ERROR: AWS region could not be determined." >&2
    echo "       Pass --region <region> or run: aws configure set region <region>" >&2
    exit 1
fi

export AWS_DEFAULT_REGION="$AWS_REGION"

# -----------------------------------------------------------------------
# Derived values
# -----------------------------------------------------------------------
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

if [[ -z "$STAGING_BUCKET" ]]; then
    STAGING_BUCKET="yara-scanner-staging-${AWS_ACCOUNT_ID}-${AWS_REGION}"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$PROJECT_ROOT/dist"
CFN_DIR="$PROJECT_ROOT/cloudformation"

echo "================================================="
echo "YARA EC2 Scanner Deployment"
echo "================================================="
echo "Scanner Image : $SCANNER_IMAGE_URI"
echo "AWS Account   : $AWS_ACCOUNT_ID"
echo "AWS Region    : $AWS_REGION"
echo "Staging S3    : s3://$STAGING_BUCKET"
echo "Stack Name    : $STACK_NAME"
echo "Auto-scan     : $AUTO_SCAN"
[[ -n "$AWS_PROFILE" ]] && echo "Profile       : $AWS_PROFILE"
echo "================================================="

# -----------------------------------------------------------------------
# Step 1: Create staging bucket (idempotent)
# -----------------------------------------------------------------------
step "Step 1: Ensuring staging S3 bucket exists..."

if aws s3api head-bucket --bucket "$STAGING_BUCKET" 2>/dev/null; then
    echo "    Bucket s3://$STAGING_BUCKET already exists"
else
    if [ "$AWS_REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$STAGING_BUCKET" --region "$AWS_REGION"
    else
        aws s3api create-bucket \
            --bucket "$STAGING_BUCKET" \
            --region "$AWS_REGION" \
            --create-bucket-configuration LocationConstraint="$AWS_REGION"
    fi
    aws s3api put-bucket-versioning \
        --bucket "$STAGING_BUCKET" \
        --versioning-configuration Status=Enabled
    aws s3api put-public-access-block \
        --bucket "$STAGING_BUCKET" \
        --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
    echo "    Created s3://$STAGING_BUCKET"
fi

# -----------------------------------------------------------------------
# Step 2: Package Lambda functions
# -----------------------------------------------------------------------
step "Step 2: Packaging Lambda functions..."
mkdir -p "$DIST_DIR"

LAMBDA_FUNCTIONS=(
    "trigger_scan"
    "create_snapshot"
    "check_snapshot"
    "create_volume"
    "attach_volume"
    "check_volume_attached"
    "run_scan"
    "check_scan_status"
    "cleanup"
    "generate_report"
)

for fn in "${LAMBDA_FUNCTIONS[@]}"; do
    SRC_DIR="$PROJECT_ROOT/lambda/$fn"
    ZIP_FILE="$DIST_DIR/${fn}.zip"

    if [ ! -d "$SRC_DIR" ]; then
        echo "    ERROR: Lambda source directory not found: $SRC_DIR" >&2
        exit 1
    fi

    echo "    Zipping $fn..."
    (cd "$SRC_DIR" && zip -q "$ZIP_FILE" handler.py)
    echo "    Uploading lambda/${fn}.zip to s3://$STAGING_BUCKET..."
    aws s3 cp "$ZIP_FILE" "s3://$STAGING_BUCKET/lambda/${fn}.zip" --region "$AWS_REGION"
done

echo "    All Lambda packages uploaded."

# -----------------------------------------------------------------------
# Step 3: Package CloudFormation (upload nested templates)
# -----------------------------------------------------------------------
step "Step 3: Packaging CloudFormation templates..."

PACKAGED_TEMPLATE="$CFN_DIR/root-packaged.yaml"

aws cloudformation package \
    --template-file "$CFN_DIR/root.yaml" \
    --s3-bucket "$STAGING_BUCKET" \
    --s3-prefix cfn \
    --output-template-file "$PACKAGED_TEMPLATE" \
    --region "$AWS_REGION"

echo "    Packaged template written to $PACKAGED_TEMPLATE"

# -----------------------------------------------------------------------
# Step 4: Deploy CloudFormation stack
# -----------------------------------------------------------------------
step "Step 4: Deploying CloudFormation stack '$STACK_NAME'..."

aws cloudformation deploy \
    --template-file "$PACKAGED_TEMPLATE" \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --parameter-overrides \
        ScannerImageURI="$SCANNER_IMAGE_URI" \
        LambdaCodeBucket="$STAGING_BUCKET" \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --no-fail-on-empty-changeset

echo "    Stack deployment complete."

# Fetch stack outputs needed by subsequent steps
RESULTS_BUCKET=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='ResultsBucketName'].OutputValue" \
    --output text)

TRIGGER_FUNCTION=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='TriggerScanFunctionName'].OutputValue" \
    --output text)

SCANNER_ASG_NAME=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='ScannerASGName'].OutputValue" \
    --output text)

# -----------------------------------------------------------------------
# Step 4.5: Upload scanner image to S3 (air-gapped distribution)
# -----------------------------------------------------------------------
step "Step 4.5: Uploading scanner image to S3..."

S3_IMAGE_PATH="s3://${RESULTS_BUCKET}/scanner-image/scanner.tar.gz"
if aws s3 ls "$S3_IMAGE_PATH" --region "$AWS_REGION" &>/dev/null; then
    echo "    Image already exists at ${S3_IMAGE_PATH} — skipping upload."
else
    SCANNER_TMPFILE=$(mktemp /tmp/scanner-image-XXXXXX.tar.gz)
    trap 'rm -f "$SCANNER_TMPFILE"' EXIT

    echo "    Saving and compressing Docker image (this may take a few minutes)..."
    docker save "$SCANNER_IMAGE_URI" | gzip > "$SCANNER_TMPFILE"
    IMAGE_SIZE=$(du -sh "$SCANNER_TMPFILE" | cut -f1)
    echo "    Compressed image size: ${IMAGE_SIZE} — uploading to S3..."
    aws s3 cp "$SCANNER_TMPFILE" "$S3_IMAGE_PATH" \
        --region "$AWS_REGION"

    echo "    Scanner image uploaded to ${S3_IMAGE_PATH}"
fi

# -----------------------------------------------------------------------
# Step 5: Wait for scanner EC2 SSM agent to come Online
# -----------------------------------------------------------------------
step "Step 5: Waiting for scanner EC2 SSM agent to come Online (timeout 10 min)..."

SSM_TIMEOUT=600
SSM_START=$(date +%s)

while true; do
    STATUS=$(aws ssm describe-instance-information \
        --filters "Key=tag:aws:autoscaling:groupName,Values=${SCANNER_ASG_NAME}" \
        --query "InstanceInformationList[0].PingStatus" \
        --output text \
        --region "$AWS_REGION" 2>/dev/null || true)

    if [[ "$STATUS" == "Online" ]]; then
        echo "    SSM agent is Online."
        break
    fi

    NOW=$(date +%s)
    WAITED=$(( NOW - SSM_START ))
    if [[ $WAITED -ge $SSM_TIMEOUT ]]; then
        echo "ERROR: SSM agent did not come Online within ${SSM_TIMEOUT}s." >&2
        echo "       Check EC2 userdata logs and IAM permissions." >&2
        exit 1
    fi

    echo "    SSM status: ${STATUS:-not registered} — waiting... (${WAITED}s elapsed)"
    sleep 15
done

# -----------------------------------------------------------------------
# Step 6: (optional) Trigger scan and wait for results
# -----------------------------------------------------------------------
if [[ "$AUTO_SCAN" == "true" ]]; then
    step "Step 6: Triggering scan via Lambda '${TRIGGER_FUNCTION}'..."

    INVOKE_TMPFILE=$(mktemp)
    aws lambda invoke \
        --function-name "$TRIGGER_FUNCTION" \
        --payload '{}' \
        --cli-binary-format raw-in-base64-out \
        --region "$AWS_REGION" \
        "$INVOKE_TMPFILE" >/dev/null

    EXEC_ARN=$(jq -r '.execution_arn // empty' "$INVOKE_TMPFILE")
    rm -f "$INVOKE_TMPFILE"

    if [[ -z "$EXEC_ARN" ]]; then
        echo "ERROR: Lambda invoke did not return an execution_arn." >&2
        exit 1
    fi

    echo "    Execution ARN: $EXEC_ARN"
    echo "    Polling for completion..."

    SCAN_TIMEOUT=7200  # 2 hours
    SCAN_START=$(date +%s)

    while true; do
        SF_STATUS=$(aws stepfunctions describe-execution \
            --execution-arn "$EXEC_ARN" \
            --query "status" \
            --output text \
            --region "$AWS_REGION")

        case "$SF_STATUS" in
            SUCCEEDED)
                echo "    Scan SUCCEEDED."
                break
                ;;
            FAILED|TIMED_OUT|ABORTED)
                echo "ERROR: Scan execution ended with status: $SF_STATUS" >&2
                aws stepfunctions describe-execution \
                    --execution-arn "$EXEC_ARN" \
                    --region "$AWS_REGION" \
                    --query "{status:status,cause:cause,error:error}" \
                    --output json >&2
                exit 1
                ;;
        esac

        NOW=$(date +%s)
        WAITED=$(( NOW - SCAN_START ))
        if [[ $WAITED -ge $SCAN_TIMEOUT ]]; then
            echo "ERROR: Scan did not complete within ${SCAN_TIMEOUT}s." >&2
            exit 1
        fi

        echo "    Status: $SF_STATUS — waiting... ($(elapsed) total elapsed)"
        sleep 30
    done

    # Download summary.json — try execution output first, fall back to guessed key
    mkdir -p "$OUTPUT_DIR"
    SF_OUTPUT=$(aws stepfunctions describe-execution \
        --execution-arn "$EXEC_ARN" \
        --query "output" \
        --output text \
        --region "$AWS_REGION" 2>/dev/null || echo "{}")

    REPORT_S3_KEY=$(echo "$SF_OUTPUT" | jq -r '.report_s3_key // empty' 2>/dev/null || true)

    if [[ -z "$REPORT_S3_KEY" ]]; then
        EXEC_NAME=$(basename "$EXEC_ARN")
        REPORT_S3_KEY="scans/${EXEC_NAME}/summary.json"
    fi

    if aws s3 cp "s3://${RESULTS_BUCKET}/${REPORT_S3_KEY}" "${OUTPUT_DIR}/summary.json" \
            --region "$AWS_REGION" 2>/dev/null; then
        echo "    Results downloaded to ${OUTPUT_DIR}/summary.json"
    else
        echo "    WARNING: Could not download summary.json"
        echo "             Check: aws s3 ls s3://${RESULTS_BUCKET}/scans/"
    fi
fi

# -----------------------------------------------------------------------
# Final summary
# -----------------------------------------------------------------------
echo ""
echo "================================================="
echo "DEPLOYMENT COMPLETE [$(elapsed)]"
echo "================================================="
echo ""
echo "Results bucket : s3://${RESULTS_BUCKET}/"
echo "Scanner image  : s3://${RESULTS_BUCKET}/scanner-image/scanner.tar.gz"
echo "Scanner ASG    : ${SCANNER_ASG_NAME}"
echo ""
echo "To trigger a new scan:"
echo "  aws lambda invoke \\"
echo "      --function-name '${TRIGGER_FUNCTION}' \\"
echo "      --payload '{}' --cli-binary-format raw-in-base64-out \\"
echo "      --region ${AWS_REGION} response.json && cat response.json"
echo ""
echo "To view results:"
echo "  aws s3 ls s3://${RESULTS_BUCKET}/scans/"
echo ""
