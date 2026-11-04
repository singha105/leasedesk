#!/usr/bin/env bash
# Create the LeaseDesk AWS sandbox (real mode). Idempotent: safe to re-run.
#
#   scripts/aws/setup_sandbox.sh --profile <your-admin-profile> [--dry-run] [--rotate-keys]
#   scripts/aws/setup_sandbox.sh --dry-run          # offline plan, no AWS calls at all
#
# Creates (region us-east-1):
#   /leasedesk-system/leasedesk-keymaster  inline LeaseDeskKeymaster  -> profile leasedesk-keymaster
#   /leasedesk-system/leasedesk-sentinel   inline LeaseDeskSentinel   -> profile leasedesk-sentinel
#   /leasedesk/lease-demo-dev              no policies, boundary      -> profile lease-demo-dev
#   managed policy /leasedesk-system/LeaseDeskDemoBoundary
#   buckets leasedesk-demo-billing-<suffix> and leasedesk-demo-prod-secrets-<suffix>
# Secrets go straight from AWS into ~/.aws/credentials (mode 600) and are never printed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REGION="us-east-1"
ADMIN_PROFILE=""
DRY=false
ROTATE=false
export AWS_PAGER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) ADMIN_PROFILE="$2"; shift 2 ;;
    --dry-run) DRY=true; shift ;;
    --rotate-keys) ROTATE=true; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

OFFLINE=false
if [ -z "$ADMIN_PROFILE" ]; then
  if $DRY; then OFFLINE=true; else
    echo "error: pass --profile <admin-profile> (this script never falls back to 'default')" >&2; exit 2
  fi
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run()  { if $DRY; then printf '  [dry-run] %s\n' "$*" >&2; else "$@"; fi; }
adm()  { aws --profile "$ADMIN_PROFILE" --region "$REGION" "$@"; }
# read-only probe: returns 1 when offline so every resource counts as missing
probe() { if $OFFLINE; then return 1; fi; adm "$@" >/dev/null 2>&1; }

# ---- account and suffix ---------------------------------------------------
step "Account"
if $OFFLINE; then
  ACCOUNT_ID="<ACCOUNT_ID>"
  say "  offline plan (no --profile): account shown as <ACCOUNT_ID>"
else
  ACCOUNT_ID="$(adm sts get-caller-identity --query Account --output text)"
  say "  admin profile '$ADMIN_PROFILE' is in account ****${ACCOUNT_ID: -4}"
fi

mkdir -p "$ROOT/.state" && chmod 700 "$ROOT/.state"
SUFFIX_FILE="$ROOT/.state/aws-suffix"
if [ -s "$SUFFIX_FILE" ]; then
  SUFFIX="$(cat "$SUFFIX_FILE")"
else
  SUFFIX="$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
  if ! $DRY; then printf '%s\n' "$SUFFIX" > "$SUFFIX_FILE"; chmod 600 "$SUFFIX_FILE"; fi
fi
BILLING="leasedesk-demo-billing-$SUFFIX"
DECOY="leasedesk-demo-prod-secrets-$SUFFIX"
BOUNDARY_ARN="arn:aws:iam::$ACCOUNT_ID:policy/leasedesk-system/LeaseDeskDemoBoundary"
say "  bucket suffix: $SUFFIX (stored in .state/aws-suffix)"

policy() { sed "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" "$HERE/policies/$1"; }

# ---- permissions boundary -------------------------------------------------
step "Permissions boundary LeaseDeskDemoBoundary"
if probe iam get-policy --policy-arn "$BOUNDARY_ARN"; then
  CURRENT_VER="$(adm iam get-policy --policy-arn "$BOUNDARY_ARN" --query Policy.DefaultVersionId --output text)"
  CURRENT_DOC="$(adm iam get-policy-version --policy-arn "$BOUNDARY_ARN" --version-id "$CURRENT_VER" \
    --query PolicyVersion.Document --output json | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), sort_keys=True))')"
  WANT_DOC="$(policy demo-boundary.json | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), sort_keys=True))')"
  if [ "$CURRENT_DOC" = "$WANT_DOC" ]; then
    say "  exists, up to date"
  else
    say "  exists, document differs: adding a new default version"
    for v in $(adm iam list-policy-versions --policy-arn "$BOUNDARY_ARN" \
        --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text); do
      run adm iam delete-policy-version --policy-arn "$BOUNDARY_ARN" --version-id "$v"
    done
    run adm iam create-policy-version --policy-arn "$BOUNDARY_ARN" --set-as-default \
      --policy-document "$(policy demo-boundary.json)" >/dev/null
  fi
else
  run adm iam create-policy --policy-name LeaseDeskDemoBoundary --path /leasedesk-system/ \
    --description "Ceiling for any LeaseDesk lease" --policy-document "$(policy demo-boundary.json)" >/dev/null
  say "  created"
fi

# ---- users ----------------------------------------------------------------
ensure_user() { # name path [boundary]
  if probe iam get-user --user-name "$1"; then
    say "  user $1 exists"
  else
    if [ -n "${3:-}" ]; then
      run adm iam create-user --user-name "$1" --path "$2" --permissions-boundary "$3" >/dev/null
    else
      run adm iam create-user --user-name "$1" --path "$2" >/dev/null
    fi
    say "  user $1 created under $2"
  fi
  if [ -n "${3:-}" ]; then
    run adm iam put-user-permissions-boundary --user-name "$1" --permissions-boundary "$3"
  fi
}

step "IAM users"
ensure_user leasedesk-keymaster /leasedesk-system/
ensure_user leasedesk-sentinel  /leasedesk-system/
ensure_user lease-demo-dev      /leasedesk/ "$BOUNDARY_ARN"

step "Inline policies"
run adm iam put-user-policy --user-name leasedesk-keymaster --policy-name LeaseDeskKeymaster \
  --policy-document "$(policy keymaster-policy.json)"
say "  leasedesk-keymaster: LeaseDeskKeymaster"
run adm iam put-user-policy --user-name leasedesk-sentinel --policy-name LeaseDeskSentinel \
  --policy-document "$(policy sentinel-policy.json)"
say "  leasedesk-sentinel: LeaseDeskSentinel"
if ! $OFFLINE; then
  LEFTOVER="$(adm iam list-user-policies --user-name lease-demo-dev --query PolicyNames --output text 2>/dev/null || true)"
  if [ -n "$LEFTOVER" ] && [ "$LEFTOVER" != "None" ]; then
    say "  WARNING: lease-demo-dev has inline policies ($LEFTOVER); a fresh sandbox has none."
    say "           keymaster's startup reconcile (Part 2) removes leasedesk-* leftovers."
  else
    say "  lease-demo-dev: no policies (as intended)"
  fi
fi

# ---- access keys into named profiles --------------------------------------
profile_works() { # profile user
  local arn
  arn="$(aws sts get-caller-identity --profile "$1" --query Arn --output text 2>/dev/null || true)"
  case "$arn" in *":user/"*"/$2") return 0 ;; *) return 1 ;; esac
}

ensure_key() { # user profile
  if $OFFLINE || $DRY; then
    if ! $OFFLINE && profile_works "$2" "$1"; then say "  profile $2 already works"; return; fi
    say "  [dry-run] aws iam create-access-key --user-name $1 | write_profile.py $2"; return
  fi
  if profile_works "$2" "$1"; then say "  profile $2 already works"; return; fi
  local existing
  existing="$(adm iam list-access-keys --user-name "$1" --query 'AccessKeyMetadata[].AccessKeyId' --output text)"
  if [ -n "$existing" ] && [ "$existing" != "None" ]; then
    if $ROTATE; then
      for k in $existing; do adm iam delete-access-key --user-name "$1" --access-key-id "$k"; done
      say "  deleted $(echo "$existing" | wc -w | tr -d ' ') orphaned key(s) for $1"
    else
      say "  ERROR: $1 already has access key(s) but profile $2 does not work."
      say "         Re-run with --rotate-keys to replace them."; exit 1
    fi
  fi
  adm iam create-access-key --user-name "$1" --output json | python3 "$HERE/write_profile.py" "$2" "$REGION"
}

step "Access keys"
ensure_key leasedesk-keymaster leasedesk-keymaster
ensure_key leasedesk-sentinel  leasedesk-sentinel
ensure_key lease-demo-dev      lease-demo-dev

# ---- buckets --------------------------------------------------------------
ensure_bucket() {
  if probe s3api head-bucket --bucket "$1"; then
    say "  bucket $1 exists"
  else
    run adm s3api create-bucket --bucket "$1" >/dev/null
    say "  bucket $1 created"
  fi
  run adm s3api put-public-access-block --bucket "$1" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  run adm s3api put-bucket-tagging --bucket "$1" --tagging 'TagSet=[{Key=project,Value=leasedesk}]'
}

step "S3 buckets"
ensure_bucket "$BILLING"
ensure_bucket "$DECOY"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/invoices.csv" <<'CSV'
invoice_id,customer,issued_on,amount_usd,status
INV-2026-10-001,Acme Robotics,2026-10-02,1240.00,paid
INV-2026-10-002,Globex Labs,2026-10-05,860.50,paid
INV-2026-10-003,Initech,2026-10-11,4310.75,overdue
INV-2026-10-004,Umbrella Analytics,2026-10-19,295.00,paid
INV-2026-10-005,Stark Freight,2026-10-27,1999.99,open
CSV
printf 'decoy\n' > "$TMP/db-password.txt"
run adm s3api put-object --bucket "$BILLING" --key exports/2026-10/invoices.csv \
  --body "$TMP/invoices.csv" --content-type text/csv >/dev/null
run adm s3api put-object --bucket "$DECOY" --key db-password.txt \
  --body "$TMP/db-password.txt" --content-type text/plain >/dev/null
say "  sample objects uploaded"

if ! $DRY; then
  cat > "$ROOT/.state/aws-sandbox.env" <<ENV
LEASEDESK_DEMO_SUFFIX=$SUFFIX
LEASEDESK_DEMO_BILLING_BUCKET=$BILLING
LEASEDESK_DEMO_DECOY_BUCKET=$DECOY
LEASEDESK_DEMO_BOUNDARY_ARN=$BOUNDARY_ARN
ENV
  chmod 600 "$ROOT/.state/aws-sandbox.env"
fi

# ---- verify ---------------------------------------------------------------
step "Verify"
if $DRY; then
  say "  [dry-run] aws sts get-caller-identity --profile leasedesk-keymaster"
  say "  [dry-run] expect AccessDenied: aws s3api list-objects-v2 --bucket $BILLING --profile lease-demo-dev"
else
  KM_ARN="$(aws sts get-caller-identity --profile leasedesk-keymaster --query Arn --output text)"
  say "  keymaster identity: ${KM_ARN/$ACCOUNT_ID/****}"
  # New keys can take a few seconds to propagate; AccessDenied is the answer we want.
  result=""
  for _ in 1 2 3 4 5 6 7 8; do
    out="$(aws s3api list-objects-v2 --bucket "$BILLING" --profile lease-demo-dev --region "$REGION" 2>&1 || true)"
    case "$out" in
      *AccessDenied*) result="AccessDenied"; break ;;
      *InvalidAccessKeyId*|*SignatureDoesNotMatch*) sleep 4 ;;
      *) result="UNEXPECTED"; break ;;
    esac
  done
  if [ "$result" = "AccessDenied" ]; then
    say "  lease-demo-dev on $BILLING: AccessDenied (correct, no lease yet)"
  else
    say "  ERROR: lease-demo-dev did not get AccessDenied on $BILLING:"; say "$out" | head -3; exit 1
  fi
fi

step "Done"
say "  Reminder: set a \$1 budget alert (Billing and Cost Management > Budgets > Create budget >"
say "  'Zero spend' or a \$1 monthly cost budget with an email alert) before you leave this running."
say "  Tear down with: scripts/aws/teardown_sandbox.sh --profile $ADMIN_PROFILE"
