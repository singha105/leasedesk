# Part 3: sentinel reviewer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** sentinel judges every lease with deterministic rules, so that a keymaster review request in a lease thread gets a verdict (LOW / MEDIUM / HIGH / BLOCK, findings, fixes, score arithmetic) that keymaster acts on, and nobody else can forge or launder one.

**Architecture:** `leasedesk/sentinel/` is a `SwitchAgent` (shared client, no protocol code of its own). A review is a pipeline: parse the proposal leniently into a `ProposalView`, run nine analyzers (pure functions plus two AWS calls that only run in real mode), score the findings with a fixed table, attach fixes, and, when every fix is mechanical, build and re-review a corrected lease. Verdicts are bound to the exact proposal by a `proposal_hash`, and only keymaster's pinned id gets a gating verdict; everyone else gets an advisory answer that carries no verdict block.

**Tech Stack:** Python 3.12, uv, pydantic v2, parliament 1.6.4 (action data + `analyze_policy_string`), boto3 `accessanalyzer` (botocore Stubber in tests; moto has no Access Analyzer), pyyaml, sqlite3, structlog, typer. Tests: pytest, pytest-asyncio.

**Spec:** the Part 3 brief (conversation of 2026-11-06) + `docs/SPEC.md`. `docs/PROTOCOL_NOTES.md` and `docs/DECISIONS.md` override both where they record server behaviour.

## Global Constraints

- No LLM in sentinel. Same proposal + same access-policy + same resource-catalog + same mode → byte-identical verdict.
- Never fail open. An analyzer that raises adds a BLOCK finding `analyzer-error`. AWS errors or timeouts set `aws_validation: "skipped"` and add nothing; they never lower risk. No access-policy loaded → BLOCK `policy-unavailable`; no resource-catalog → BLOCK `catalog-unavailable`.
- Text in `summary` / `justification` never changes risk, score or rule ids. It may only pick wording of a fix from a fixed keyword map (read/list/debug → read actions; write/upload → write actions) and a path token matching `^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*/?$` (≤ 64 chars). Nothing else from those fields is echoed.
- Severities: `BLOCK | HIGH | MEDIUM | LOW | INFO`. Every HIGH and MEDIUM finding has a non-empty `fix`; every finding has a `reference` URL.
- Scoring: any BLOCK → risk BLOCK (verdict `score: 100`, text "score n/a"). Otherwise `score = 40·HIGH + 12·MEDIUM + 3·LOW + floor(duration/30)`, capped at 100 in the verdict (the uncapped sum is shown by `explain`). HIGH if any HIGH or score ≥ 40; MEDIUM if score ≥ 12; else LOW.
- Gating verdicts only for review requests whose `sender` equals `LEASEDESK_KEYMASTER_ID`. keymaster accepts a verdict only from `LEASEDESK_SENTINEL_ID` and only when its `proposal_hash` matches the stored proposal.
- Advisory answers use a ```` ```advisory ```` block with `kind: "lease_advisory"`, never a `verdict` block.
- Reply ≤ 6 summary lines + the fenced block; the verdict message stays ≤ 2,000 characters (findings in the block are cut in severity order, with an INFO `more-findings` pointer to `@sentinel explain`).
- Mock mode makes no AWS call. Real mode uses only profile `leasedesk-sentinel`; never `default`.
- Paid checks only when `LEASEDESK_ENABLE_PAID_CHECKS=true` and real mode; each call logged as `paid_call` with its price ($0.002).
- A review takes < 2 s in mock mode; `review_done` logs `elapsed_ms`.
- Logs pass the redaction processor; no key, token or secret anywhere. `.env`, `.secrets/`, `.state/` never committed.
- Execution: native, in order, `make lint test` after each task; **one commit at the end**: `feat: sentinel reviewer`. Push only on the user's OK.

## Rulings in this plan (the user can overrule any of them at approval)

1. **`s3:*` is HIGH, not BLOCK.** The brief's golden list says HIGH, while its tamper list says `s3:PutBucketPolicy` is always BLOCK. Ruling: tamper/exposure fires on actions *named* in the proposal; a wildcard that expands to a tamper action gets HIGH `wildcard-covers-tamper`, naming those actions (so a security lead must approve it). Escalation still matches the *expanded* set, as the brief says (`lambda:*` + `iam:PassRole` → BLOCK).
2. **Resource `*` is HIGH** (`resource-star`), exempt from the prefix BLOCK, as the golden list says. The schema is relaxed to accept `"*"` as a resource so sentinel can judge it; keymaster still refuses it at submit (it is outside `allowed_resource_prefixes`), so `*` never reaches approval.
3. **`iam:PassRole` alone → BLOCK**: sentinel also enforces the access-policy's `forbidden_actions` (`iam:*`), independently of keymaster. It also gets the near-miss MEDIUM note.
4. **Near misses are one finding**: all escalation paths that are one action short are listed in a single MEDIUM `escalation-near-miss`, so `iam:PassRole` alone doesn't stack eight MEDIUMs.
5. **Access level is one finding per statement**, at the statement's highest level (Read and List are one class).
6. **`proposal_hash` is added to the verdict schema** (`"sha256:<hex>"` of the canonical proposal JSON). Without it, someone could post a forged review request carrying another lease's id and get sentinel to answer for it; keymaster now refuses a verdict whose hash doesn't match.
7. **keymaster is exempt from the 10-per-minute rate limit** (its requests are already gated by confirm); everyone else is limited.
8. **Cross-account check applies to ARNs that carry an account id.** S3 bucket ARNs have none; buckets are pinned by allowed prefixes and the catalog. The "bucket in another account" golden case uses an S3 access-point ARN in account 999999999999.
9. **`s3:DeletePublicAccessBlock` is not an IAM action** (the API is authorised by `s3:PutBucketPublicAccessBlock`; parliament doesn't know the name). The tamper list keeps it and adds `s3:PutBucketPublicAccessBlock` and `s3:PutAccountPublicAccessBlock`.
10. **resource-catalog** is a person-written library document (template `switch/docs/resource-catalog.md`, a Markdown page with one ```` ```yaml ```` block of `{arn, owner, sensitivity, sensitive_actions}` entries; ARNs may end in `*` because the mock bucket suffix changes per run). Read like access-policy: only documents with `created_by_agent_id` null.
11. **Shared code moves**, no behaviour change: `render_policy`/`serialize` → `leasedesk/common/render.py`; `glob_intersects` → `leasedesk/common/globs.py`; `PolicyLoader`/`parse_policy_text` → `leasedesk/common/policy.py`; the person-document fetcher → `leasedesk/switch/documents.py`; role claiming (retry every 60 s, on `room_join` and on `subscription_changed`) → `SwitchAgent`.
12. **Agent-to-agent delivery**: keymaster's review request tries `target_roles=["reviewer"]` → `target_names=["sentinel"]` → plain post; sentinel answers with `target_names=["keymaster"]` → plain `@keymaster` post; keymaster accepts a pinned-sentinel verdict on unaddressed messages too. The live run records which path the server takes.

## Review Focus

1. A forged review request that reuses a real lease id, posted in that lease's thread: sentinel answers advisory only, and even a hand-copied verdict for that lease fails the `proposal_hash` check. Tests in Task 1 and Task 5.
2. Proposal shapes the strict schema rejects (`NotAction`, `Principal`, `Effect: Deny`, extra top-level keys, non-list `statements`): sentinel still returns a BLOCK verdict with a reason instead of crashing. Tests in Task 2 and Task 6.
3. Wildcards hiding dangerous actions (`s3:Put*`, `lambda:*`, `*`, `arn:aws:s3:::leasedesk-demo-*` covering the prod-secrets decoy): caught by expansion or glob intersection. Tests in Task 2 and Task 6.
4. A verdict with many findings: the posted message stays ≤ 2,000 characters and still parses on keymaster's side. Test in Task 5.
5. An analyzer crash or AWS timeout in real mode: the risk never drops (crash → BLOCK, timeout → `skipped`). Tests in Task 2 and Task 3.

---

### Task 1: Trust pinning in both agents

**Files:** Modify `leasedesk/common/schemas.py`, `leasedesk/keymaster/commands.py`, `leasedesk/keymaster/service.py`, `leasedesk/common/format.py`. Create `leasedesk/sentinel/trust.py`. Tests: `tests/keymaster/test_commands.py`, `tests/common/test_schemas.py`, `tests/sentinel/test_trust.py`.

**Produces:** `proposal_hash(p: LeaseProposal) -> str` (`"sha256:" + hexdigest` of `json.dumps(p.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))`); `LeaseVerdict.proposal_hash: str`; `Finding.severity: Severity` (Literal); `sentinel.trust.request_mode(sender: str, settings: Settings) -> Literal["gating", "advisory"]`.

- [ ] Tests first: `test_verdict_from_scribe_ignored_when_pinned` (an agent that isn't sentinel), `test_verdict_from_human_ignored_and_logged` (pinned; asserts the `verdict_from_wrong_sender` log and no state change), `test_verdict_with_wrong_proposal_hash_refused`, `test_unaddressed_verdict_from_pinned_sentinel_applied`, `test_unaddressed_verdict_from_others_ignored_silently`; `test_request_from_keymaster_id_is_gating`, `test_request_from_anyone_else_is_advisory`, `test_unpinned_keymaster_means_advisory_outside_dev_mode`. Run, watch them fail.
- [ ] Implement: hash on both sides; keymaster `on_unaddressed` routes a ```` ```verdict ```` block from the pinned sentinel to `commands.verdict`, ignores it from anyone else without replying; dev fake verdict fills the hash. Run `make lint test`.

### Task 2: Analyzers

**Files:** Create `leasedesk/sentinel/{model.py,context.py,catalog.py,analyzers/__init__.py,analyzers/structure.py,lint.py,access_level.py,escalation.py,scope.py,tamper.py,duration.py,aws.py}`, `leasedesk/sentinel/data/escalation_paths.yaml`, `switch/docs/resource-catalog.md`; move shared code per Ruling 11. Tests: `tests/sentinel/test_analyzer_<name>.py`.

**Produces:** `ProposalView.parse(raw: Any) -> tuple[ProposalView | None, list[Finding]]` (lenient: keeps unknown keys so `structure` can name them); `ReviewContext(policy: AccessPolicy | None, catalog: Catalog | None, account_id: str, aws_mode: str, paid_checks: bool)`; each analyzer `analyze(p: ProposalView, ctx: ReviewContext) -> list[Finding]`; `aws.validate(doc: str, client) -> AwsResult(status, findings)` and `aws.crown_jewels(doc, access, client) -> list[Finding]`; `expanded_actions(p) -> dict[str, set[str]]` (sid → lower-cased `svc:Action`, via parliament's `iam_definition`); `access_level(action) -> str | None`.

- [ ] Explore first, record in DECISIONS: parliament 1.6.4 uses `analyze_policy_string(str) -> Policy` with `.findings` (`issue`, `detail`, `location`; severity/title come from `enhance_finding`), `expand_action("s3:Get*")`, and the bundled `iam_definition` (455 services; levels List / Read / Write / Permissions management / Tagging). Importing it warns (pkg_resources, SyntaxWarning); `analyzers/lint.py` imports it under `warnings.catch_warnings()`. Lint runs once per statement (one-statement policy) so every finding maps to a sid. Probe on 2026-11-06: 1 ms per analysis.
- [ ] Fetch the two source pages and record each path's exact reference: Rhino Security Labs methods 1–21 and hackingthe.cloud anchors for ECS, CodeBuild, SageMaker and SSM. Write `escalation_paths.yaml` (≥ 27 entries: `id`, `name`, `requires`, `severity: BLOCK`, `reference`).
- [ ] Per analyzer, tests first, then code. Rule ids: `struct-not-allow`, `struct-not-action`, `struct-not-resource`, `struct-principal`, `struct-unknown-key`, `struct-current-time`, `struct-bad-arn`, `struct-unreadable` (BLOCK) · `lint-malformed`, `lint-unknown-action`, `lint-unknown-prefix`, `lint-invalid-arn` (BLOCK), `lint-resource-mismatch`, `lint-condition-mismatch` (MEDIUM, parliament's text as the fix), other parliament issues LOW · `access-permissions-management` (HIGH), `access-write`, `access-tagging` (MEDIUM), `access-read-list` (LOW), `wildcard-service`, `wildcard-all`, `wildcard-covers-tamper` (HIGH) · `escalation-<id>` (BLOCK), `escalation-near-miss` (MEDIUM) · `resource-star` (HIGH), `resource-outside-prefixes`, `resource-other-account`, `forbidden-action`, `crown-jewel-write` (BLOCK), `crown-jewel-read` (HIGH), `list-bucket-no-prefix` (LOW) · `tamper-exposure` (BLOCK) · `duration-over-max` (BLOCK), `duration-long`, `write-long` (MEDIUM) · `aws-<issueCode>` (ERROR → BLOCK, SECURITY_WARNING → HIGH, WARNING → MEDIUM, SUGGESTION → LOW, `learnMoreLink` as the reference) · `crown-jewel-access-granted` (BLOCK).
- [ ] AWS tests use botocore `Stubber`: findings map by `locations[].path` index to the sid; `ClientError`, `EndpointConnectionError` and a 5 s `asyncio.wait_for` timeout give `skipped`; paid check logs `paid_call` once per call and runs only with the flag in real mode. Add `access-analyzer:CheckAccessNotGranted` to `scripts/aws/policies/sentinel-policy.json`. `make lint test`.

### Task 3: Scoring

**Files:** Create `leasedesk/sentinel/scoring.py`, `docs/SCORING.md`. Test: `tests/sentinel/test_scoring.py`.

**Produces:** `score(findings: list[Finding], duration_minutes: int) -> Scored(risk: Risk, score: int, raw: int, arithmetic: str)`.

- [ ] Tests first: the table's boundaries (one HIGH → HIGH; 12 → MEDIUM; 11 → LOW; any BLOCK → BLOCK with 100; INFO adds 0; cap at 100 with `raw` kept), and that `arithmetic` reads like `40×1 HIGH + 12×2 MEDIUM + 3×1 LOW + 4 (120 min ÷ 30) = 71 → HIGH`. Implement; write `docs/SCORING.md` (the table, the rule list with severities, three worked examples). `make lint test`.

### Task 4: Least-privilege fixes and the corrected lease

**Files:** Create `leasedesk/sentinel/fixes.py`, `leasedesk/sentinel/review.py`. Tests: `tests/sentinel/test_fixes.py`, `tests/sentinel/test_review.py`.

**Produces:** `review(raw: Any, ctx: ReviewContext, *, aws: AwsChecks | None = None) -> Review(findings, scored, aws_validation, corrected: dict | None, proposal_hash: str | None, elapsed_ms: float)`; `corrected_lease(view, findings, ctx) -> dict | None`.

- [ ] Tests first: every HIGH/MEDIUM finding over the golden cases has a fix; mechanical fixes = replace `s3:*` (object ARNs → `s3:GetObject`, or `s3:GetObject`+`s3:PutObject` for a write purpose; bucket ARNs → `s3:ListBucket`), add a `StringLike s3:prefix` condition derived from the same bucket's object ARN (else the summary's path token), shorten to 60 minutes, drop named tamper/escalation actions. Non-mechanical (`resource-star`, crown-jewel write, outside prefixes, other account) → no corrected lease. The corrected lease is re-reviewed and offered only if it isn't BLOCK and is lower risk than the original. An analyzer that raises → BLOCK `analyzer-error`. `make lint test`.

### Task 5: sentinel the agent, CLI and `make up`

**Files:** Create `leasedesk/sentinel/{agent.py,store.py,text.py,ratelimit.py,__main__.py,cli.py}`, `leasedesk/switch/documents.py`, `scripts/demo/supervise.py`. Modify `leasedesk/switch/agent.py` (role claiming), `leasedesk/keymaster/{service.py,__main__.py,commands.py}` (shared pieces, review-request fallback chain), `leasedesk/config.py` (`LEASEDESK_CATALOG_DOC`, `LEASEDESK_CATALOG_FILE`, `LEASEDESK_SENTINEL_DB`, `LEASEDESK_AWS_ACCOUNT_ID`), `Makefile` (`run-sentinel`, `up`), `.env.example`, `CLAUDE.md`. Tests: `tests/sentinel/test_agent.py`, `tests/sentinel/test_cli.py`, `tests/test_supervise.py`, existing keymaster tests stay green.

- [ ] Tests first (fake Switch from `tests/switch/fakeswitch.py`): a review request from keymaster's id (addressed or not) → one targeted reply to `keymaster` in the same thread with a parseable verdict block and ≤ 6 summary lines; the same request from a person → advisory reply, no `verdict` block; `@sentinel check` + lease block → advisory with an `advisory` block; `explain LS-0007` → every finding and the arithmetic (also after a restart, from `.state/sentinel.db`); `rules` → every rule id with its reference; the 11th request in a minute from one person → one "slow down" reply, keymaster never limited; a repeated proposal hits the cache; policy-hash mismatch → reload once, then INFO `policy-hash-mismatch`; 40 findings → message ≤ 2,000 chars and keymaster's parser accepts it; role `reviewer` claimed with retry; SIGINT exits 0.
- [ ] `python -m leasedesk.sentinel.cli review path/to/proposal.json [--policy FILE] [--catalog FILE]` prints the summary and the verdict block (`lease_id` `LS-0000`).
- [ ] `make up`: moto in the background, then `scripts/demo/supervise.py` runs keymaster and sentinel, restarts a child that dies (backoff 1 s → 30 s), does not restart exit codes 2/3/4 (config, taken over, auth) and says why, and stops both on Ctrl-C. `make lint test`.

### Task 6: Golden tests

**Files:** Create `tests/sentinel/fixtures/*.json` (≥ 24), `tests/sentinel/fixtures/context/{access-policy.yaml,resource-catalog.md}`, `tests/sentinel/test_golden.py`.

- [ ] One fixture per case, `{"name", "proposal", "expect": {"risk", "rules"}}`; one parametrized test asserts the exact risk, that the expected rule ids are present, that every HIGH/MEDIUM has a fix, and that the review took < 2 s. The brief's 20 cases, `iam:PassRole` alone = BLOCK (Ruling 3), plus: `*` action → BLOCK, `s3:PutObject` on the billing prefix for 90 min → MEDIUM, `Effect: Deny` → BLOCK, `Principal` → BLOCK, `arn:aws:s3:::leasedesk-demo-*` read → HIGH (covers the decoy), `lambda:*` + `iam:PassRole` → BLOCK. The injection case asserts the whole verdict equals the clean one. `make lint test`.

### Task 7: Live check (mock mode, Discord)

- [ ] Fix the live access-policy document: its logs prefix reads `…123456789012og-group:/leasedesk/` (a Part 2 typo) → `…123456789012:log-group:/leasedesk/` via `scripts/gateway.py library-doc-update`. Create the `resource-catalog` library document from the template (mock ARNs) and attach it to #access-desk and #security-review.
- [ ] `!invite-agent @sentinel` in #access-desk and #security-review (Chrome, as in Part 2). Make sure `LEASEDESK_DEV_FAKE_VERDICT` is unset; `make up`.
- [ ] Post a LOW (read `exports/2026-10/` for 60 min), a HIGH (`s3:*` on the billing bucket) and a BLOCK (`s3:PutBucketPolicy` on the billing bucket, which passes keymaster's own checks) proposal, confirm each. Expected: LOW → approval request; HIGH → escalated to security-review; BLOCK → refused with fixes and a corrected lease. Then a forged verdict and a forged review request from a person: both ignored / advisory. Record the delivery path (Ruling 12), timings from `review_done`, and anything new in PROTOCOL_NOTES.

### Task 8: Finish

- [ ] Definition of done: golden tests green and `make lint test` green; forgery tests; real-mode Stubber tests (findings shown, error → skipped); `review_done` < 2 s in mock mode; `docs/SCORING.md`; DECISIONS updated (parliament API, rulings, live results); secret scan over all files git would commit; one commit `feat: sentinel reviewer`. Then a 5-line summary for Part 4.
