#!/usr/bin/env bash
#
# Update every hosted nable box at once via AWS SSM Run Command. No SSH, no keys,
# no open port 22. Targets EC2 instances by tag and tells each one to pull the new
# release image and restart. Nothing is built on the box, so one box or fifty is
# the same single command. See docs/FLEET.md for the full picture.
#
# One-time prerequisites, per box:
#   1. The SSM agent (preinstalled on Amazon Linux 2023) plus an instance-profile
#      IAM role that includes the managed policy AmazonSSMManagedInstanceCore.
#   2. A tag so the fleet can be targeted, e.g. app=nable:
#        aws ec2 create-tags --resources i-0123abc --tags Key=app,Value=nable
#   3. The GHCR image must be pullable by the box: make the package public, or run
#      `docker login ghcr.io` on the box with a read-only token. See docs/FLEET.md.
# The machine running THIS script needs the AWS CLI + jq and ssm:SendCommand.
#
# Usage:
#   deploy/fleet-update.sh                 # all app=nable boxes -> latest release
#   deploy/fleet-update.sh 0.8.100         # pin every box to a specific version
#   TAG_KEY=env TAG_VALUE=prod AWS_REGION=us-east-1 deploy/fleet-update.sh 0.8.100
set -euo pipefail

VERSION="${1:-latest}"
TAG_KEY="${TAG_KEY:-app}"
TAG_VALUE="${TAG_VALUE:-nable}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-2}}"

for bin in aws jq; do
  command -v "$bin" >/dev/null 2>&1 || { echo "error: '$bin' is required on this machine" >&2; exit 1; }
done

# Preflight: confirm at least one box is reachable by SSM under this tag before
# dispatching. send-command will happily return a CommandId that targets zero
# instances and look like success, the silent no-op that makes a "fleet update"
# land nowhere. describe-instance-information only lists instances whose SSM agent
# is online AND whose IAM role can reach SSM, so a zero count means the one-time
# per-box setup (SSM role + tag) is missing. See docs/FLEET.md.
ONLINE=$(aws ssm describe-instance-information \
  --region "${REGION}" \
  --filters "Key=tag:${TAG_KEY},Values=${TAG_VALUE}" \
  --query 'length(InstanceInformationList)' --output text 2>/dev/null || echo 0)
if [ "${ONLINE:-0}" = "0" ]; then
  {
    echo "error: no ${TAG_KEY}=${TAG_VALUE} instances are online in SSM in ${REGION}."
    echo "       Each box needs an instance-profile role with AmazonSSMManagedInstanceCore"
    echo "       plus the ${TAG_KEY}=${TAG_VALUE} tag. Nothing was dispatched. See docs/FLEET.md."
  } >&2
  exit 1
fi
echo "Found ${ONLINE} reachable ${TAG_KEY}=${TAG_VALUE} box(es) in ${REGION}."

# This runs ON each box: pin the version compose reads, pull the image, restart,
# and reclaim the disk the old image held. Idempotent, re-running is safe.
REMOTE_SCRIPT=$(cat <<EOF
set -e
cd /opt/nable
if grep -q '^NABLE_VERSION=' .env 2>/dev/null; then
  sed -i 's/^NABLE_VERSION=.*/NABLE_VERSION=${VERSION}/' .env
else
  echo 'NABLE_VERSION=${VERSION}' >> .env
fi
docker compose --profile tls pull
docker compose --profile tls up -d
docker image prune -f
EOF
)

echo "Updating all ${TAG_KEY}=${TAG_VALUE} boxes to nable '${VERSION}' in ${REGION} ..."
CMD_ID=$(aws ssm send-command \
  --region "${REGION}" \
  --targets "Key=tag:${TAG_KEY},Values=${TAG_VALUE}" \
  --document-name "AWS-RunShellScript" \
  --comment "nable fleet update -> ${VERSION}" \
  --parameters "$(jq -n --arg s "$REMOTE_SCRIPT" '{commands: [$s]}')" \
  --query "Command.CommandId" --output text)

echo
echo "Dispatched. SSM CommandId: ${CMD_ID}"
echo "Watch it land across the fleet:"
echo "  aws ssm list-command-invocations --region ${REGION} --command-id ${CMD_ID} \\"
echo "    --details --query 'CommandInvocations[].{instance:InstanceId,status:Status}' --output table"
