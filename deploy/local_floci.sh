#!/usr/bin/env bash
# Stand up the whole stack locally: Floci for AWS, Docker Compose for Postgres,
# and this function deployed into the emulator exactly as it is in Sydney.
#
# Why not run the real Terraform against Floci: it would share the state file
# with production, and Terraform would then believe the real function had been
# replaced by an emulated one. Separating state is possible, but the local stack
# needs four resources, and a 60-line script is easier to trust than a second
# state file is to keep honest. If local fidelity ever needs to include IAM
# policy evaluation, revisit with a dedicated workspace.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FUNCTION="${RATERADAR_LOCAL_FUNCTION:-rateradar}"
BUCKET="${RATERADAR_LOCAL_BUCKET:-rateradar-backups-local}"
PARAM="/rateradar/database_url"
ENDPOINT="${AWS_ENDPOINT_URL:-http://localhost:4566}"

# Inside a Lambda container, "localhost" is that container. The database and the
# emulator both live on the host, so the function is given host-relative names.
HOST_ALIAS="${RATERADAR_LOCAL_HOST_ALIAS:-host.docker.internal}"
DB_URL_FOR_LAMBDA="postgresql://rateradar:rateradar@${HOST_ALIAS}:5432/rateradar"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

for tool in floci docker aws; do
  command -v "$tool" >/dev/null || {
    echo "missing: $tool" >&2
    [ "$tool" = floci ] && echo "  install: curl -fsSL https://floci.io/install.sh | sh" >&2
    exit 1
  }
done

export AWS_ENDPOINT_URL="$ENDPOINT"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"

say "Starting Floci and Postgres"
floci start >/dev/null 2>&1 || true
docker compose -f "${ROOT}/docker-compose.yml" up -d postgres >/dev/null

# Postgres accepts connections a moment after the container reports running.
for _ in $(seq 1 30); do
  docker compose -f "${ROOT}/docker-compose.yml" exec -T postgres pg_isready -U rateradar >/dev/null 2>&1 && break
  sleep 1
done

say "Building the deployment package"
"${ROOT}/deploy/build_package.sh"

say "Creating the emulated AWS resources"
aws s3api create-bucket --bucket "$BUCKET" \
  --create-bucket-configuration LocationConstraint="$AWS_DEFAULT_REGION" >/dev/null 2>&1 || true

# The function reads its connection string from Parameter Store in production,
# so it does so here too -- testing a different code path locally is how you
# ship a broken one.
aws ssm put-parameter --name "$PARAM" --type SecureString \
  --value "$DB_URL_FOR_LAMBDA" --overwrite >/dev/null

ENV_VARS="Variables={\
RATERADAR_MIGRATIONS_DIR=/var/task/migrations,\
RATERADAR_BRAND_ALLOWLIST_FILE=/var/task/config/brands.allowlist,\
RATERADAR_DATABASE_URL_PARAM=${PARAM},\
RATERADAR_BACKUP_BUCKET=${BUCKET},\
RATERADAR_USER_AGENT=RateRadar/0.1-local (+https://github.com/vinodhkumarp/rateradar),\
AWS_ENDPOINT_URL=http://${HOST_ALIAS}:4566,\
PYTHONUNBUFFERED=1}"

if aws lambda get-function --function-name "$FUNCTION" >/dev/null 2>&1; then
  say "Updating the function"
  aws lambda update-function-code --function-name "$FUNCTION" \
    --zip-file "fileb://${ROOT}/build/rateradar.zip" >/dev/null
  aws lambda wait function-updated --function-name "$FUNCTION" 2>/dev/null || sleep 2
  aws lambda update-function-configuration --function-name "$FUNCTION" \
    --environment "$ENV_VARS" >/dev/null
else
  say "Creating the function"
  aws lambda create-function --function-name "$FUNCTION" \
    --runtime python3.12 --handler rateradar.handler.handler \
    --role arn:aws:iam::000000000000:role/lambda-local \
    --timeout 900 --memory-size 512 \
    --zip-file "fileb://${ROOT}/build/rateradar.zip" \
    --environment "$ENV_VARS" >/dev/null
fi
aws lambda wait function-active --function-name "$FUNCTION" 2>/dev/null || sleep 2

say "Applying migrations through the function"
"${ROOT}/deploy/local_invoke.sh" migrate

cat <<SUMMARY

Local stack ready.

  function   $FUNCTION          (Floci, $ENDPOINT)
  database   postgresql://rateradar:rateradar@localhost:5432/rateradar
  bucket     s3://$BUCKET

  make local-invoke CMD=collect      one collection pass, for real, against the banks
  make local-invoke CMD=backup       dump to the emulated bucket
  rateradar runs                     read the ledger from your shell

SUMMARY
