# resource-catalog (golden tests)

```yaml
version: 1
resources:
- arn: arn:aws:s3:::leasedesk-demo-billing-*
  owner: billing team
  sensitivity: internal
- arn: arn:aws:s3:::leasedesk-demo-prod-secrets-*
  owner: security
  sensitivity: crown-jewel
  sensitive_actions:
  - s3:GetObject
  - s3:PutObject
  - s3:DeleteObject
- arn: arn:aws:logs:us-east-1:123456789012:log-group:/leasedesk/*
  owner: platform team
  sensitivity: internal
```
