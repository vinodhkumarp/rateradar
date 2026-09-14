#!/usr/bin/env bash
# Invoke the emulated function and print its response.
set -euo pipefail

COMMAND="${1:-collect}"
FUNCTION="${RATERADAR_LOCAL_FUNCTION:-rateradar}"

export AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://localhost:4566}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"

RESPONSE="$(mktemp)"
trap 'rm -f "$RESPONSE"' EXIT

aws lambda invoke --function-name "$FUNCTION" \
  --payload "{\"command\":\"${COMMAND}\"}" \
  --cli-binary-format raw-in-base64-out \
  --log-type Tail --query LogResult --output text "$RESPONSE" \
  | base64 --decode

echo
python3 -m json.tool < "$RESPONSE" 2>/dev/null || cat "$RESPONSE"

# A Lambda that raises still returns HTTP 200 with an error payload: check the
# body, or a failing run looks like a successful one.
if grep -q '"errorMessage"' "$RESPONSE"; then
  echo "invocation failed" >&2
  exit 1
fi
