#!/usr/bin/env bash
# Build the Lambda deployment zip.
#
# Dependencies are installed for Lambda's platform explicitly, not for whatever
# machine happens to run this: psycopg ships compiled wheels, and a macOS wheel
# in a Linux Lambda fails at import time with an error that looks nothing like
# the actual cause.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/build/lambda"
ZIP="${ROOT}/build/rateradar.zip"

rm -rf "${BUILD}" "${ZIP}"
mkdir -p "${BUILD}"

python -m pip install \
  --target "${BUILD}" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade \
  --quiet \
  "httpx>=0.27" "psycopg[binary]>=3.2" "pydantic>=2.7" "pydantic-settings>=2.3"

# The package itself, plus the two directories it reads at runtime.
cp -r "${ROOT}/src/rateradar" "${BUILD}/rateradar"
cp -r "${ROOT}/migrations" "${BUILD}/migrations"
mkdir -p "${BUILD}/config"
cp "${ROOT}/config/brands.allowlist" "${BUILD}/config/"

# typer and click are CLI-only; boto3 is supplied by the runtime. Dropping them
# keeps the zip well under Lambda's 50MB limit.
find "${BUILD}" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "${BUILD}" -type d -name "tests" -prune -exec rm -rf {} +

( cd "${BUILD}" && zip -qr "${ZIP}" . )

printf 'built %s (%s)\n' "${ZIP}" "$(du -h "${ZIP}" | cut -f1)"
