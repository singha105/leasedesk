#!/usr/bin/env bash
# Remove everything setup_sandbox.sh created. Idempotent: missing pieces are skipped.
#
#   scripts/aws/teardown_sandbox.sh --profile <your-admin-profile> [--dry-run] [--yes]
#
# Deletes: both demo buckets (and their objects), all access keys and inline policies of the
# three LeaseDesk users, the users, the LeaseDeskDemoBoundary policy, and the three local
# profiles in ~/.aws. Without --yes it asks before deleting anything.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REGION="us-east-1"
ADMIN_PROFILE=""
DRY=false
YES=false
export AWS_PAGER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) ADMIN_PROFILE="$2"; shift 2 ;;
    --dry-run) DRY=true; shift ;;
    --yes) YES=true; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$ADMIN_PROFILE" ] || { echo "error: pass --profile <admin-profile>" >&2; exit 2; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run()  { if $DRY; then printf '  [dry-run] %s\n' "$*" >&2; else "$@"; fi; }
adm()  { aws --profile "$ADMIN_PROFILE" --region "$REGION" "$@"; }
exists() { adm "$@" >/dev/null 2>&1; }

ACCOUNT_ID="$(adm sts get-caller-identity --query Account --output text)"
BOUNDARY_ARN="arn:aws:iam::$ACCOUNT_ID:policy/leasedesk-system/LeaseDeskDemoBoundary"
SUFFIX="$(cat "$ROOT/.state/aws-suffix" 2>/dev/null || true)"
USERS="leasedesk-keymaster leasedesk-sentinel lease-demo-dev"

say "Account ****${ACCOUNT_ID: -4}, suffix '${SUFFIX:-<none>}'."
say "This deletes the LeaseDesk users, their keys and policies, the boundary and both demo buckets."
if ! $DRY && ! $YES; then
  printf 'Type "teardown" to continue: '; read -r answer
  [ "$answer" = "teardown" ] || { say "aborted"; exit 1; }
fi

step "S3 buckets"
if [ -n "$SUFFIX" ]; then
  for b in "leasedesk-demo-billing-$SUFFIX" "leasedesk-demo-prod-secrets-$SUFFIX"; do
    if exists s3api head-bucket --bucket "$b"; then
      run adm s3 rm "s3://$b" --recursive --only-show-errors
      run adm s3api delete-bucket --bucket "$b"
      say "  deleted $b"
    else
      say "  $b not found"
    fi
  done
else
  say "  no .state/aws-suffix, so bucket names are unknown; list with: aws s3 ls | grep leasedesk-demo-"
fi

step "IAM users"
for u in $USERS; do
  if ! exists iam get-user --user-name "$u"; then say "  $u not found"; continue; fi
  for k in $(adm iam list-access-keys --user-name "$u" --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
    run adm iam delete-access-key --user-name "$u" --access-key-id "$k"
  done
  for p in $(adm iam list-user-policies --user-name "$u" --query PolicyNames --output text); do
    run adm iam delete-user-policy --user-name "$u" --policy-name "$p"
  done
  for a in $(adm iam list-attached-user-policies --user-name "$u" --query 'AttachedPolicies[].PolicyArn' --output text); do
    run adm iam detach-user-policy --user-name "$u" --policy-arn "$a"
  done
  if [ "$(adm iam get-user --user-name "$u" --query 'User.PermissionsBoundary.PermissionsBoundaryArn' --output text)" != "None" ]; then
    run adm iam delete-user-permissions-boundary --user-name "$u"
  fi
  run adm iam delete-user --user-name "$u"
  say "  deleted $u"
done

step "Boundary policy"
if exists iam get-policy --policy-arn "$BOUNDARY_ARN"; then
  for v in $(adm iam list-policy-versions --policy-arn "$BOUNDARY_ARN" \
      --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text); do
    run adm iam delete-policy-version --policy-arn "$BOUNDARY_ARN" --version-id "$v"
  done
  run adm iam delete-policy --policy-arn "$BOUNDARY_ARN"
  say "  deleted LeaseDeskDemoBoundary"
else
  say "  LeaseDeskDemoBoundary not found"
fi

step "Local profiles"
for p in leasedesk-keymaster leasedesk-sentinel lease-demo-dev; do
  run python3 "$HERE/write_profile.py" --remove "$p"
done
if ! $DRY; then rm -f "$ROOT/.state/aws-sandbox.env"; fi
say ""
say "Teardown complete. .state/aws-suffix is kept so a re-run of setup reuses the same names."
