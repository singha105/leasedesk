# resource-catalog

Every AWS resource a lease can touch, with its owner and how sensitive it is. A **person**
pastes this into a Gateway library document named `resource-catalog`. It reaches the rooms in
the `leasedesk-governance` package. No agent may write it: sentinel and scribe read it only from
a document no agent created.

| ARN | Owner | Sensitivity | Typical access | Sensitive actions |
| --- | --- | --- | --- | --- |
| `arn:aws:s3:::leasedesk-demo-billing-mock` | billing team | internal | read invoice exports under exports/YYYY-MM/ to debug the billing report | |
| `arn:aws:s3:::leasedesk-demo-prod-secrets-mock` | security | crown-jewel | none; a decoy that stands in for production secrets | s3:GetObject, s3:PutObject, s3:DeleteObject |
| `arn:aws:logs:us-east-1:<ACCOUNT_ID>:log-group:/leasedesk/app` | platform team | internal | read application logs (logs:FilterLogEvents, logs:GetLogEvents) | |

How to fill it in:

- The bucket names above are the mock-mode ones. In real mode, use the names
  `scripts/aws/setup_sandbox.sh` printed (`leasedesk-demo-billing-<suffix>`), and put the account
  id in the log group ARN (mock mode uses `123456789012`).
- scribe copies ARNs from this table exactly, so list each resource by its full ARN. An ARN may
  end in `*` to cover several resources; sentinel matches it with IAM glob rules.
- `crown-jewel` makes sentinel rate any read HIGH (security approves in security-review) and any
  change BLOCK. "Sensitive actions" is what the optional paid AWS check
  (CheckAccessNotGranted) asks about.
