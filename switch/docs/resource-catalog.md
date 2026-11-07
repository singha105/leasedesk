# resource-catalog

Every AWS resource a lease can touch, with its owner and how sensitive it is. A **person**
pastes this into a Gateway library document named `resource-catalog` and attaches it to the
access-desk and security-review rooms. sentinel reads it only from a document no agent created.

- `sensitivity: crown-jewel` makes sentinel rate a read HIGH (security approves) and any change
  BLOCK.
- ARNs may end in `*`. The demo buckets get a random suffix, so the entries match on the prefix.
- `sensitive_actions` is what the optional paid check (CheckAccessNotGranted) asks AWS about.

```yaml
version: 1
resources:
  - arn: "arn:aws:s3:::leasedesk-demo-billing-*"
    owner: "billing team"
    sensitivity: internal
    note: "invoice exports; read access for debugging is normal"
  - arn: "arn:aws:s3:::leasedesk-demo-prod-secrets-*"
    owner: "security"
    sensitivity: crown-jewel
    sensitive_actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    note: "decoy that stands in for production secrets"
  - arn: "arn:aws:logs:us-east-1:<ACCOUNT_ID>:log-group:/leasedesk/*"
    owner: "platform team"
    sensitivity: internal
```
