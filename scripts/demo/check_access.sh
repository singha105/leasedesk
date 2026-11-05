#!/usr/bin/env bash
# Every 10 s, try to list the demo billing exports as lease-demo-dev and print a big
# ALLOWED or DENIED with the time. Leave it running beside the Discord window during a demo.
#
#   mock mode (default): reads .state/mock-aws.env written by `make moto`
#   real mode:           LEASEDESK_AWS_MODE=real, uses --profile lease-demo-dev and the bucket
#                        from .state/aws-sandbox.env
#
# Keys are passed to the aws CLI as environment variables and never printed.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE="${LEASEDESK_AWS_MODE:-mock}"
INTERVAL="${CHECK_INTERVAL:-10}"
TZ_NAME="${LEASEDESK_TZ:-America/New_York}"
PREFIX="exports/2026-10/"

# Read KEY=VALUE lines without executing the file.
env_value() {
  local file="$1" key="$2" line
  [[ -f "$file" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == "$key="* ]] && { printf '%s' "${line#*=}"; return 0; }
  done <"$file"
  return 1
}

if [[ "$MODE" == "mock" ]]; then
  ENV_FILE="$ROOT/.state/mock-aws.env"
  [[ -f "$ENV_FILE" ]] || { echo "No $ENV_FILE. Start mock AWS first: make moto" >&2; exit 2; }
  ENDPOINT="$(env_value "$ENV_FILE" LEASEDESK_MOCK_ENDPOINT)"
  BUCKET="$(env_value "$ENV_FILE" LEASEDESK_MOCK_BUCKET)"
  AWS_ACCESS_KEY_ID="$(env_value "$ENV_FILE" DEMO_AWS_ACCESS_KEY_ID)"
  AWS_SECRET_ACCESS_KEY="$(env_value "$ENV_FILE" DEMO_AWS_SECRET_ACCESS_KEY)"
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
  unset AWS_PROFILE AWS_SESSION_TOKEN
  AWS_ARGS=(--endpoint-url "$ENDPOINT" --region "${LEASEDESK_AWS_REGION:-us-east-1}")
  WHERE="mock AWS"
elif [[ "$MODE" == "real" ]]; then
  ENV_FILE="$ROOT/.state/aws-sandbox.env"
  BUCKET="$(env_value "$ENV_FILE" LEASEDESK_DEMO_BILLING_BUCKET)" \
    || { echo "No bucket in $ENV_FILE. Run scripts/aws/setup_sandbox.sh first." >&2; exit 2; }
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  AWS_ARGS=(--profile lease-demo-dev)
  WHERE="real AWS (profile lease-demo-dev)"
else
  echo "LEASEDESK_AWS_MODE must be mock or real, not $MODE" >&2
  exit 2
fi

ALLOWED_ART=(
  "    _    _     _     _____        _______ ____  "
  "   / \\  | |   | |   / _ \\ \\      / / ____|  _ \\ "
  "  / _ \\ | |   | |  | | | \\ \\ /\\ / /|  _| | | | |"
  " / ___ \\| |___| |__| |_| |\\ V  V / | |___| |_| |"
  "/_/   \\_\\_____|_____\\___/  \\_/\\_/  |_____|____/ "
)
DENIED_ART=(
  " ____  _____ _   _ ___ _____ ____  "
  "|  _ \\| ____| \\ | |_ _| ____|  _ \\ "
  "| | | |  _| |  \\| || ||  _| | | | |"
  "| |_| | |___| |\\  || || |___| |_| |"
  "|____/|_____|_| \\_|___|_____|____/ "
)
GREEN=$'\033[1;32m'; RED=$'\033[1;31m'; DIM=$'\033[2m'; OFF=$'\033[0m'

echo "Checking s3://$BUCKET/$PREFIX as lease-demo-dev on $WHERE every ${INTERVAL}s. Ctrl-C stops."
while true; do
  NOW="$(TZ="$TZ_NAME" date '+%-I:%M:%S %p %Z')"
  if OUT="$(aws s3 ls "s3://$BUCKET/$PREFIX" "${AWS_ARGS[@]}" 2>&1)"; then
    printf '\n%s' "$GREEN"; printf '%s\n' "${ALLOWED_ART[@]}"; printf '%s' "$OFF"
    printf '%s  lease-demo-dev can read the billing exports\n' "$NOW"
    printf '%s%s%s\n' "$DIM" "$(printf '%s\n' "$OUT" | head -3)" "$OFF"
  else
    printf '\n%s' "$RED"; printf '%s\n' "${DENIED_ART[@]}"; printf '%s' "$OFF"
    REASON="$(printf '%s\n' "$OUT" | grep -oE '\((AccessDenied|InvalidAccessKeyId|NoSuchBucket|[A-Za-z]+)\)' | head -1)"
    printf '%s  lease-demo-dev is denied %s\n' "$NOW" "${REASON:-}"
  fi
  sleep "$INTERVAL"
done
