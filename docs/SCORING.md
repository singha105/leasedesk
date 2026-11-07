# How sentinel scores a lease

sentinel has no model inside it. Every verdict comes from the rules below, the proposal, the
access-policy and the resource-catalog, so the same lease always gets the same verdict and
anyone can redo the sum. `@sentinel explain LS-0007` prints the findings and the arithmetic
for a lease; `@sentinel rules` lists every rule with its source.

## The table

| Step | Rule |
| - | - |
| 1 | Any BLOCK finding → **BLOCK**. Nobody can approve it; the verdict carries score 100 and the reply says "score n/a". |
| 2 | Otherwise **score = 40 x HIGH + 12 x MEDIUM + 3 x LOW + 1 per full 30 minutes** of duration. INFO findings add nothing. |
| 3 | **HIGH** if there is any HIGH finding or the score is 40 or more. HIGH leases go to security-review. |
| 4 | **MEDIUM** if the score is 12 or more. |
| 5 | **LOW** otherwise. |

The verdict's `score` is capped at 100 (the schema's maximum); `explain` shows the uncapped sum.

Failing closed: an analyzer that crashes adds a BLOCK (`analyzer-error`). A missing
access-policy or resource-catalog is a BLOCK. In real mode, an Access Analyzer timeout or
error sets `aws_validation: "skipped"` and adds nothing, so it can never lower a risk or count
as a pass.

Text is data: words in `summary` or `justification` only pick the wording of a fix (which
narrower actions to suggest). They never change a rule, a severity, the score or the risk.

## Worked examples

**LOW**, read one folder of the billing bucket for 60 minutes (`s3:GetObject` on
`…/exports/2026-10/*`, `s3:ListBucket` with an `s3:prefix` condition):
`access-read-list` (LOW) on each statement.
3x2 LOW + 2 (60 min / 30) = **8 → LOW** (under 12).

**MEDIUM**, the same read for 239 minutes: adds `duration-long` (MEDIUM).
12x1 MEDIUM + 3x2 LOW + 7 (239 min / 30) = **25 → MEDIUM** (12 or more).

**HIGH**, `s3:*` on the billing bucket for 60 minutes: `wildcard-service`,
`access-permissions-management` (the wildcard includes `s3:PutBucketPolicy`) and
`wildcard-covers-tamper`, all HIGH.
40x3 HIGH + 2 (60 min / 30) = **122 → HIGH** (a HIGH finding; the verdict shows 100).
The corrected lease replaces `s3:*` with `s3:GetObject` on `…/*` and `s3:ListBucket` on the
bucket, and re-reviews as LOW.

**BLOCK**, `s3:PutBucketPolicy` on the billing bucket: `tamper-exposure` (BLOCK), plus
`access-permissions-management` (HIGH) and one `lint-other` remark (LOW). Any BLOCK →
**BLOCK**; the score isn't added up. There is no corrected lease, because dropping the action
leaves nothing.

## Rules

| Rule | Severity | Analyzer | What it means |
| - | - | - | - |
| `struct-unreadable` | BLOCK | structure | The proposal can't be read as a lease |
| `struct-schema` | BLOCK | structure | Not the lease proposal shape keymaster accepts |
| `struct-not-allow` | BLOCK | structure | A statement that isn't Allow |
| `struct-not-action` | BLOCK | structure | NotAction grants everything except a list |
| `struct-not-resource` | BLOCK | structure | NotResource grants every resource except a list |
| `struct-principal` | BLOCK | structure | Principal belongs in resource policies, not a lease |
| `struct-unknown-key` | BLOCK | structure | A key a lease statement can't have |
| `struct-current-time` | BLOCK | structure | The proposal sets aws:CurrentTime; keymaster owns the time window |
| `struct-bad-arn` | BLOCK | structure | A resource that isn't a well-formed ARN |
| `lint-malformed` | BLOCK | lint | parliament couldn't read the policy |
| `lint-unknown-action` | BLOCK | lint | An action AWS doesn't have (with parliament's message and a "did you mean") |
| `lint-unknown-prefix` | BLOCK | lint | A service prefix AWS doesn't have |
| `lint-invalid-arn` | BLOCK | lint | parliament says an ARN is malformed |
| `lint-resource-mismatch` | MEDIUM | lint | A named action that no listed resource fits (parliament's format is the fix) |
| `lint-condition-mismatch` | MEDIUM | lint | A condition that doesn't apply to the action, so it restricts nothing (one finding per statement) |
| `lint-other` | LOW | lint | Another parliament remark (one finding per kind of remark per statement) |
| `access-permissions-management` | HIGH | access_level | Permissions-management actions (they change who can do what) |
| `access-write` | MEDIUM | access_level | Write actions |
| `access-tagging` | MEDIUM | access_level | Tagging actions |
| `access-read-list` | LOW | access_level | Read and List actions only |
| `wildcard-service` | HIGH | access_level | Every action of a service (`svc:*`) |
| `wildcard-all` | HIGH | access_level | Every action in AWS (`*`) |
| `escalation-<id>` | BLOCK | escalation | A full privilege-escalation path (31 paths in `leasedesk/sentinel/data/escalation_paths.yaml`, from Rhino Security Labs, hackingthe.cloud and AWS) |
| `escalation-near-miss` | MEDIUM | escalation | One action away from one or more escalation paths (one finding for all of them) |
| `resource-star` | HIGH | scope | Resource `*` |
| `resource-outside-prefixes` | BLOCK | scope | A resource outside access-policy `allowed_resource_prefixes` |
| `resource-other-account` | BLOCK | scope | An ARN in another AWS account |
| `forbidden-action` | BLOCK | scope | An action access-policy forbids |
| `crown-jewel-read` | HIGH | scope | Reads a crown-jewel resource from resource-catalog (a bucket ARN covers its objects) |
| `crown-jewel-write` | BLOCK | scope | Changes a crown-jewel resource from resource-catalog |
| `list-bucket-no-prefix` | LOW | scope | `s3:ListBucket` without an `s3:prefix` condition lists the whole bucket |
| `tamper-exposure` | BLOCK | tamper_and_exposure | Named action that blinds security tooling or exposes data (CloudTrail, Config, GuardDuty, bucket policy and ACLs, Block Public Access, KMS key deletion) |
| `wildcard-covers-tamper` | HIGH | tamper_and_exposure | A wildcard that includes one of those actions |
| `duration-over-max` | BLOCK | duration | Longer than access-policy `max_duration_minutes` |
| `duration-long` | MEDIUM | duration | Longer than 120 minutes |
| `write-long` | MEDIUM | duration | Write access for longer than 60 minutes |
| `aws-<issue-code>` | ERROR → BLOCK, SECURITY_WARNING → HIGH, WARNING → MEDIUM, SUGGESTION → LOW | aws_validate | An IAM Access Analyzer ValidatePolicy finding (real mode only) |
| `crown-jewel-access-granted` | BLOCK | crown_jewels | Access Analyzer CheckAccessNotGranted says the lease reaches a crown jewel (paid, opt-in, real mode, keymaster's review requests only) |
| `crown-jewel-check-skipped` | INFO | crown_jewels | The paid check could not run |
| `analyzer-error` | BLOCK | review | An analyzer failed, so the lease can't be judged |
| `policy-unavailable` | BLOCK | review | No access-policy is loaded |
| `catalog-unavailable` | BLOCK | review | No resource-catalog is loaded |
| `policy-hash-mismatch` | INFO | review | keymaster and sentinel read different access-policy versions |
| `more-findings` | INFO | review | More findings than fit in one message; `explain` has them all |

Every HIGH and MEDIUM finding carries one concrete fix. When every fix is mechanical (swap a
wildcard for named actions, add an `s3:prefix` condition, shorten the lease, drop a named
dangerous action), sentinel also posts a corrected lease block, re-reviewed, that can be
submitted as is. Its first line says what changed, for example "replaces s3:* with
s3:GetObject, s3:ListBucket; adds an s3:prefix condition (exports/2026-10/*); shortens 120 to
60 min". It is offered only when the re-review has no BLOCK or HIGH finding and a lower risk.
