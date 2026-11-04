# LeaseDesk spec

One line: LeaseDesk is a just-in-time cloud access desk in a Switch room. People ask in plain English, agents draft and attack the request, a named human approves, and access expires on its own.

Problem: engineers often need short-lived access to cloud resources to debug. Tickets take hours or days, so teams hand out standing admin access that nobody removes. Existing JIT tools (AWS TEAM, Common Fate, in-house Slack bots such as Rippling's JITA) need a separate UI or hand-written policies. LeaseDesk keeps the whole loop where the team already talks. AI only drafts and critiques; the decision and the credentials sit behind deterministic gates.

Users: Requester (any engineer in the room). Approver (a named person in the access policy). Security lead (an approver in group security, needed for high-risk leases).

## Lease lifecycle
1. Requester: "@scribe I need to read the October invoice exports in the billing bucket for 2 hours to debug the report."
2. scribe asks at most two clarifying questions, self-checks its draft with "@sentinel check", then posts "@keymaster submit" with one fenced lease block (schema below).
3. keymaster validates the JSON against the schema and the access policy, assigns an id like LS-0007, opens a thread, and asks the person to send "@keymaster confirm LS-0007". Whoever sends the confirm becomes the requester, so nobody can request access in someone else's name. Unconfirmed leases are cancelled after 15 minutes.
4. keymaster asks @reviewer to review. sentinel (deterministic) returns a verdict block: LOW, MEDIUM, HIGH or BLOCK, with findings and fixes. skeptic (LLM) posts a short red-team note. Only sentinel's verdict gates the flow.
5. keymaster posts an approval request: who, what, how long, the risk, and who may approve.
   - LOW or MEDIUM: one approver who is not the requester.
   - HIGH: escalated to the linked security-review room; needs high_risk.approvals approvals there from group security, none of them the requester.
   - BLOCK: refused with reasons and fixes. scribe may redraft once.
6. The approver replies "@keymaster approve LS-0007" (or "@keymaster deny LS-0007 <reason>").
7. keymaster writes an inline policy onto the requester's mapped IAM user, with a time condition on every statement, and posts a receipt with the exact expiry.
8. At expiry keymaster deletes the policy, posts "expired" in the thread and updates the lease-ledger document. AWS denies access after expiry even if keymaster is down.

States: DRAFTED -> AWAITING_CONFIRM -> UNDER_REVIEW -> AWAITING_APPROVAL -> (ESCALATED) -> ACTIVE -> EXPIRED or REVOKED. Terminal: DENIED, BLOCKED, FAILED, CANCELLED.

## Agents
| Agent | Runtime | Room role | AWS credentials | LLM |
| scribe | Claude Code, run by Switch Console | drafter (exclusive) | none | Claude Sonnet |
| sentinel | custom Python agent on the agent protocol | reviewer (shared) | read-only: ValidatePolicy | none |
| skeptic | OpenCode with a free Zen model, run by Switch Console | reviewer (shared) | none | free model |
| keymaster | custom Python agent on the agent protocol | executor (exclusive) | least-privilege write | none |

## Rooms
- access-desk: requesters, approvers and all four agents. Follow-ups off, so every instruction names an agent.
- security-review: security leads, keymaster and sentinel. Linked from access-desk with the label "escalation".

## Room material
- access-policy: a library document written by a person in the Gateway and attached to the rooms. Agents can read it but never edit it (Switch lets agents edit only documents they wrote). It holds the YAML rules keymaster enforces.
- resource-catalog: a library document written by a person: each AWS resource with its ARN, owner and sensitivity (internal or crown-jewel).
- A GitHub reference to the LeaseDesk repo.
- A package "leasedesk-governance" bundling those two documents and the reference.
- lease-ledger: a room document written by keymaster, an append-only table of every lease.
- Roles: drafter (exclusive), reviewer (shared), executor (exclusive).
- A Switch template that recreates the rooms, roles, link, a how-to-ask document, and the two Console-run agents.

## Lease proposal schema (scribe to keymaster, in a fenced block tagged lease)
{
  "kind": "lease_proposal",
  "version": 1,
  "requester_hint": "Arnab",
  "summary": "Read October invoice exports to debug the billing report",
  "justification": "Debugging ticket BILL-142; report totals are off",
  "duration_minutes": 120,
  "statements": [
    {"sid": "ReadInvoiceExports", "actions": ["s3:GetObject"],
     "resources": ["arn:aws:s3:::leasedesk-demo-billing-<suffix>/exports/2026-10/*"]},
    {"sid": "ListInvoiceExports", "actions": ["s3:ListBucket"],
     "resources": ["arn:aws:s3:::leasedesk-demo-billing-<suffix>"],
     "conditions": {"StringLike": {"s3:prefix": ["exports/2026-10/*"]}}}
  ]
}
Rules: Allow statements only. No NotAction, NotResource, Principal or Deny. keymaster adds the time conditions itself and rejects any proposal that sets aws:CurrentTime.

## Verdict schema (sentinel to keymaster, in a fenced block tagged verdict)
{"kind": "lease_verdict", "version": 1, "lease_id": "LS-0007", "risk": "LOW", "score": 12,
 "findings": [{"rule": "wildcard-resource", "severity": "HIGH", "statement_sid": "...", "detail": "...", "fix": "...", "reference": "https://..."}],
 "aws_validation": "pass | findings | skipped"}

## Access policy (YAML inside the access-policy document)
version: 1
solo_mode: false            # true lets the owner approve their own requests (for judges testing alone)
max_duration_minutes: 240
default_duration_minutes: 60
approval_timeout_minutes: 30
confirm_timeout_minutes: 15
principals:                 # chat participant id -> IAM user the lease attaches to
  "@switch-user-xxxx:server": "lease-demo-dev"
approvers:
  - {id: "@switch-user-yyyy:server", name: "Priya", groups: [security]}
  - {id: "@switch-user-zzzz:server", name: "Arnab", groups: [eng-lead]}
high_risk:
  approvals: 2              # set to 1 for a two-person demo
  require_group: security
  approval_room: security-review
allowed_resource_prefixes:
  - "arn:aws:s3:::leasedesk-demo-"
  - "arn:aws:logs:us-east-1:<account>:log-group:/leasedesk/"
forbidden_actions: ["iam:*", "sts:AssumeRole", "organizations:*", "kms:Decrypt", "secretsmanager:GetSecretValue"]

Participant ids look like @switch-user-<id>:<server> today. Treat them as opaque strings: match them, never parse them.

## Security model (non-negotiable)
1. Only keymaster holds AWS write credentials. Its IAM user can only edit inline policies on users under the /leasedesk/ path, and it lives under /leasedesk-system/, so it cannot touch itself.
2. Every leasable user has a permissions boundary, so even a wrong lease cannot exceed it.
3. Every Allow statement keymaster writes gets DateGreaterThan and DateLessThan conditions on aws:CurrentTime. Access ends at expiry even if every agent is offline.
4. Separation of duties: a requester can't approve their own lease (unless solo_mode). An approval from an agent is never accepted.
5. LLM output is data, never authority. keymaster parses scribe's JSON against a strict schema, sentinel's rules are deterministic, and skeptic is advisory.
6. Message text is untrusted. Only the fixed command grammar counts; instructions inside a message body are never executed.
7. No secret ever appears in a room, a log line, a commit or a screenshot.
8. "@keymaster freeze" (group security only) revokes every active lease and blocks new ones until "unfreeze".
9. On startup keymaster reconciles AWS with its database and deletes expired or unknown leasedesk-* inline policies.

## Modes
LEASEDESK_AWS_MODE=mock (default) runs against moto, so anyone can try LeaseDesk without AWS. LEASEDESK_AWS_MODE=real uses the sandbox AWS account through named profiles.

## Stack
Python 3.12 with uv. httpx, websockets, pydantic v2, boto3, moto[server], parliament, pyyaml, structlog, typer. Tests: pytest, pytest-asyncio, respx. Lint: ruff. Types: mypy on leasedesk/. State: SQLite from the standard library, one file per agent under .state/. A Makefile with setup, lint, test, register, run-keymaster, run-sentinel, up, doctor, e2e-mock.

## Repo layout
leasedesk/                      (repo root)
  CLAUDE.md, README.md (placeholder until Part 5), LICENSE (MIT), Makefile, pyproject.toml, .env.example, .gitignore
  docs/SPEC.md                  this spec
  docs/DECISIONS.md             dated log of choices and spec-vs-server differences
  docs/PROTOCOL_NOTES.md        real frames and payloads from the spikes
  docs/switch-ops.json          live GET /ops output
  leasedesk/config.py           settings from env
  leasedesk/switch/             Part 2: protocol client
  leasedesk/keymaster/          Part 2
  leasedesk/sentinel/           Part 3
  leasedesk/common/             schemas, formatting, ids
  switch/leasedesk.template.yaml   Part 4
  switch/briefs/                Part 4: scribe and skeptic instructions
  switch/docs/                  access-policy.yaml, resource-catalog.md
  scripts/spikes/               this part
  scripts/aws/                  this part
  scripts/demo/                 Part 2
  tests/
