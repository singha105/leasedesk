# Part 4: rooms as code, template and scenarios — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Switch template creates the whole LeaseDesk setup: scribe and skeptic as Console-run agents, #access-desk and #security-review linked, with roles, room instructions and the how-to-ask doc. `make doctor` proves the setup is healthy, and the workflow is proven by eight recorded live scenarios and a one-process `make e2e-mock`.

**Architecture:** The template is generated from the brief files by `scripts/build_template.py`, so the briefs exist once and a test fails if the committed template drifts. Person-side setup goes through `scripts/gateway.py`: library docs, the GitHub reference, the package, attaching the package and validating the template. On this server that is the Gateway, because a native local server has no Gateway web page. Agents find person-written documents directly or inside an attached package. `make doctor` reads the setup back through the gateway as the person and through keymaster's API key. The e2e test runs the real keymaster and sentinel against an in-process fake Switch: ops go through `httpx.MockTransport` and events through a fake room session. AWS is moto's `mock_aws`, and a scripted stand-in plays scribe.

**Tech Stack:** Python 3.12, uv, pydantic v2, pyyaml, httpx (MockTransport), moto, typer, pytest + pytest-asyncio. Switch Console 0.40.1 / switch-core 0.31.0 (native, API http://localhost:57637).

**Spec:** the Part 4 brief (conversation of 2026-11-07) + `docs/SPEC.md`. The installed server overrides both: its source is on disk under `~/Library/Application Support/switchdash/local-switch-server/native/switch-core/0.31.0/src/core/switch_core/` (`rooms_yaml.py`, `template_v2.py`, `agent_templates.py`), along with `get_template_guide`, `GET /gateway/openapi.json` and `docs/PROTOCOL_NOTES.md`.

## Global Constraints

- Only scribe uses Claude: provider `claude`, model `sonnet`, `advanced_config.effort: low`. skeptic runs OpenCode with a free Zen model. No other agent uses any model.
- access-policy and resource-catalog are written by a person: library documents with `created_by_agent_id` null, created or changed only through `scripts/gateway.py` (the admin user), never by an agent.
- keymaster never waits for skeptic, and never moves a lease without sentinel's verdict.
- The template must import in the installed Console with no warnings (Console's own `parseAgents` warnings and the server's `POST /gateway/templates/validate`).
- Never print, log or commit a secret. `.env`, `.secrets/`, `.state/` stay gitignored, mode 600 (dirs 700). No secret in a room, a log line, a commit or a screenshot.
- AWS real mode only through profiles `leasedesk-keymaster`, `leasedesk-sentinel`, `lease-demo-dev`. Sandbox setup uses the admin profile the user names. Never `default`.
- Ask before deleting or archiving anything (rooms, docs, templates, agents). Push only on the user's OK.
- Execution: native, in the brief's order 1–12, `make lint test` after each task. **One commit at the end:** `feat: rooms as code, template and scenarios`. That commit is the feature freeze: after it, fixes only.

## Rulings in this plan (the user can overrule any of them at approval)

1. **Template version 2.** The live guide says "Write new templates in version 2". v2 compiles to v1 on the server (`template_v2.compile_v2`) and passes room `roles`, `docs`, `references` and `instructions` through, so nothing in the brief is lost. In return we get a native `machine` param (`default: [$first]`), `{scribe.name}` in kickoffs and `if:`. v1 still runs; the Console reads both.
2. **No `packages:` in the template.** switch-core 0.31.0's `RoomSpec` has no `packages` field and doesn't forbid extra keys, so the line would be dropped without a word. `scripts/gateway.py package-attach` attaches `leasedesk-governance` to both rooms right after the run, and `make doctor` FAILs until it is attached.
3. **Bridge default is `["LeaseDesk Demo", Discord, Mattermost, $first]`.** Bridges match by exact display name, and here the Discord bridge is named "LeaseDesk Demo". The brief's `[Discord, Mattermost, $first]` would pick Mattermost.
4. **scribe's tool limits live in the template.** Claude's advanced settings include `disallowedTools` (a list) and `effort` (`low`), so the template sets `advanced_config: {effort: low, disallowedTools: [Bash, Write, Edit, NotebookEdit, WebFetch, WebSearch]}`. Step 7 then only verifies them in Console. skeptic gets `advanced_config: {smallModel: <the same free model>, webSearch: "false"}`, so OpenCode's background model is free too.
5. **Package documents count.** `References` gets `packages: list[PackageMeta]`, and `person_document_fetcher` looks at top-level documents plus each package's documents, still only `created_by_agent_id` null. Without this, the package would hide the policy from keymaster and sentinel.
6. **resource-catalog becomes a table.** sentinel's `parse_catalog_text` reads a Markdown table with columns `ARN | Owner | Sensitivity | Typical access | Sensitive actions`, and still accepts the old ```` ```yaml ```` block, which is the live document until it is updated. The table is the only source; there is no duplicate YAML.
7. **The confirmer is shown against the draft.** If the person who confirms is not the `requester_hint` scribe wrote down, the approval request says so: `Who: Priya -> IAM user lease-demo-dev (drafted for Arnab)`. An approver then sees that someone else took over a draft.
8. **Icons: none.** The repo is private, so raw GitHub URLs would not load for anyone else, and Switch draws an icon from the name. Display names: Scribe, Skeptic (template), Keymaster, Sentinel (set at registration).
9. **Message prefix.** Every keymaster lease message starts `LS-n · <STATE> ·`, for example `LS-0004 · UNDER_REVIEW · confirmed by Arnab`. sentinel's request header regex accepts the new form, plus the old one so in-flight requests still pass.
10. **The friday summary (optional) is skipped:** feature freeze, and it isn't in the definition of done.
11. **Step 1's live half is scenario 1.** The automated check comes first; the real scribe → keymaster → confirm path is exercised live in scenario 1 once scribe exists.

## Review Focus

1. Two rooms with the same name (the Part 2 #access-desk/#security-review next to the template's): keymaster escalates into both, and an approval given in the old #security-review counts. Expected: doctor FAILs on duplicate names, with "archive the old room" as the fix. Test in Task 7.
2. skeptic offline or rate-limited (free model): the reviewer role is held by sentinel alone, the review request reaches it by role, and approval is asked as soon as sentinel's verdict lands. Test in Task 9 (scenario with no skeptic present) and live scenario 8.
3. Someone other than the asker confirms a scribe draft: the approval request names both (Ruling 7). Test in Task 1.
4. The policy reachable only through the package (no direct attachment): keymaster and sentinel still load it; a package document written by an agent is ignored. Test in Task 4.
5. A template edit that drifts from the briefs or declares a param nothing uses: `test_template_is_up_to_date` and `test_every_param_used` fail. Test in Task 5.

---

### Task 1: Requester-confirm flow (brief step 1)

**Files:** Modify `leasedesk/common/format.py` (`approval_request` gains `drafted_for: str | None`), `leasedesk/keymaster/service.py` (passes it when `requester_hint` ≠ requester's policy name, case-insensitive). Test: `tests/keymaster/test_confirm_flow.py`.

- [ ] Tests first, using `tests/keymaster/harness.py`. Where `test_commands.py` already covers a case, reuse it and say so in the ledger:
  - `test_agent_submit_waits_for_named_confirm`: scribe (agent) submits with hint "Arnab". State `AWAITING_CONFIRM`; the reply contains `Arnab, if this is your request, reply: @keymaster confirm LS-0001`.
  - `test_principal_confirm_sends_review_by_role_in_thread`: `UNDER_REVIEW`, review request targeted `roles=["reviewer"]` in the submit thread.
  - `test_agent_confirm_refused` (scribe and skeptic ids).
  - `test_unlisted_person_confirm_refused`.
  - `test_unconfirmed_lease_cancelled_after_15_min`.
  - `test_confirmer_other_than_hint_named_in_approval`.
- [ ] Run them and watch the new ones fail. Implement Ruling 7. Run `make lint test`.

### Task 2: Briefs (step 2)

**Files:** Create `switch/briefs/scribe.md`, `switch/briefs/skeptic.md`. Test: `tests/test_briefs.py`.

- [ ] Write both briefs from the brief's text. scribe.md ends with a ```` ```json ```` block holding `LeaseProposal.model_json_schema()` (indent 2) and one ```` ```lease ```` example: read `exports/2026-10/*` of `leasedesk-demo-billing-*` plus `s3:ListBucket` with an `s3:prefix` condition, 60 min. skeptic.md is the brief's text as given.
- [ ] Tests:
  - `test_scribe_schema_matches_model`: the json block == `LeaseProposal.model_json_schema()`.
  - `test_scribe_example_is_low`: it validates; `validate_proposal` against `switch/docs/access-policy.yaml` gives no problems; a sentinel review with the template catalog gives LOW.
  - `test_briefs_forbid_what_the_brief_forbids`: scribe.md names `aws:CurrentTime`, "never approve", the 8-line limit and "one round"; skeptic.md has the 5-line limit and "never post a verdict block".
- [ ] Run `make lint test`.

### Task 3: Policy and catalog documents (step 3)

**Files:** Modify `switch/docs/access-policy.yaml` (`approvals: 1  # production uses 2`), `switch/docs/resource-catalog.md` (table, Ruling 6), `leasedesk/sentinel/catalog.py`. Test: `tests/sentinel/test_catalog.py`.

- [ ] Tests first:
  - `test_table_catalog_parses`: three rows → billing internal, prod-secrets crown-jewel with `s3:GetObject, s3:PutObject, s3:DeleteObject`, `/leasedesk/*` log group internal. "Typical access" becomes `note`.
  - `test_yaml_catalog_still_parses`.
  - `test_table_with_unknown_sensitivity_rejected` (parse error → `catalog-unavailable` BLOCK, as today).
  - `test_template_docs_parse`: the repo's access-policy.yaml through `parse_policy_text` (placeholders filled by the test), and the catalog through `parse_catalog_text`.
- [ ] Implement `parse_catalog_text(text) -> Catalog`: a fenced yaml block wins; otherwise the first pipe table whose header has `ARN` and `Sensitivity`. Backticks around cells are stripped; "Sensitive actions" is comma-separated. Run `make lint test`.

### Task 4: Package documents and gateway commands (step 4)

**Files:** Modify `leasedesk/switch/models.py` (`PackageMeta{id, name, documents: list[DocumentMeta]}`, `References.packages`), `leasedesk/switch/documents.py`, `scripts/gateway.py`. Tests: `tests/switch/test_documents.py`, `tests/test_gateway_script.py`.

**Produces:**
- `gateway.py reference --name leasedesk-repo --url https://github.com/singha105/leasedesk --instructions "How LeaseDesk works. Read docs/SPEC.md before explaining the process."`
- `package --name leasedesk-governance --doc <id>… --reference <id>…` (creates, or reuses one with that name, then adds the members).
- `package-attach --name leasedesk-governance --room <room id>…`
- `template-validate FILE`: prints `ok/blocked`, then each error and warning, and exits 1 on any.
- Every command prints ids and HTTP codes only.

- [ ] Tests first:
  - `test_package_document_found`.
  - `test_package_document_by_agent_ignored`.
  - `test_same_doc_direct_and_in_package_is_one`.
  - `test_two_different_docs_same_name_refused`.
  - `test_references_without_packages_key_parses`.
  - For gateway.py: pure helpers `validation_report(body: dict) -> tuple[str, int]` and `members_to_add(existing: set[str], wanted: list[str]) -> list[str]`.
- [ ] Implement, then `make lint test`.
- [ ] Live: update the two library documents in place (`library-doc-update`, ids `003cba5c…` and `f4ffdbe7…`). The live policy keeps its real participant ids and gets `approvals: 1`; the friend is added after their `whoami` (Task 6). Then create the reference and the package. Record what `list_references` returns for package documents (top-level, nested, or both) in PROTOCOL_NOTES.

### Task 5: The template (step 5)

**Files:** Create `scripts/build_template.py`, `switch/leasedesk.template.yaml` (generated, header comment "generated by scripts/build_template.py, do not edit"). Add Makefile `template:`. Test: `tests/switch/test_template.py`.

**Produces:** the v2 template. Its content:
- **Params:**
  - `bridge` (type bridge, default per Ruling 3)
  - `machine` (type machine, `[$first]`, advanced)
  - `scribe_name` (`scribe`), `skeptic_name` (`skeptic`): string, advanced, pattern `^[a-z0-9][a-z0-9._-]*$`
  - `skeptic_model` (string, advanced, default = the free Zen model chosen in Task 6)
- **Agents** `scribe` / `skeptic`:
  - `name: "{scribe_name}"` / `"{skeptic_name}"`, display names Scribe / Skeptic
  - providers `claude` / `opencode`, models `sonnet` / `"{skeptic_model}"`, `advanced_config` per Ruling 4
  - `machine: "{machine}"`, `directory: "{$agents_dir}/{scribe.name}"` (and the same for skeptic)
  - `addressing: anyone`, `allow_existing: true`, instructions = the brief file
- **Group** `desk`: "LeaseDesk", "Just-in-time AWS access, leased from chat", link `access → review` labelled `escalation`.
- **Room `access`:**
  - `access-desk`, `channel_public`, `bridge: "{bridge}"`, the brief's instructions (agents named via `{scribe.name}` / `{skeptic.name}`)
  - agents `[scribe, skeptic, keymaster, sentinel]`, users `["{$creator}"]`
  - roles drafter (exclusive) / reviewer (shared) / executor (exclusive) with the brief's texts
  - doc how-to-ask (five lines plus an example)
  - kickoff: the brief's text with `@{scribe.name}` / `@{skeptic.name}`
- **Room `review`:** `security-review`, `channel_private`, the brief's instructions, agents `[keymaster, sentinel]`, users `["{$creator}"]`, roles reviewer and executor.

- [ ] Tests first:
  - `test_template_is_up_to_date`: the committed file == `build_template.render()`.
  - `test_agent_instructions_are_the_briefs`.
  - `test_roles_match_the_brief` (names, exclusive flags, texts).
  - `test_every_param_used_and_every_placeholder_declared`: mirrors server lint, plus `{$…}` builtins and `{key.name}`.
  - `test_only_scribe_uses_claude`.
  - `test_no_packages_key` (Ruling 2).
  - `test_rooms_linked_access_to_review`.
- [ ] Implement `render() -> str` with PyYAML, multi-line strings as literal blocks. Run `make template lint test`.
- [ ] Live: run `gateway.py template-validate switch/leasedesk.template.yaml` and expect `ok`, 0 warnings. **User:** Console → Templates → Import template → pick the file. Report any warning text; fix and repeat until none. Log every server/Console vs brief difference in DECISIONS.

### Task 6: Run it live (steps 6–7)

- [ ] **User:** install OpenCode and connect OpenCode Zen (`opencode auth login`, free key). Then run `opencode models opencode`; I pick a free model from that list for `skeptic_model` and rebuild the template. If the user prefers, I run `brew install opencode` after their OK; the sign-in is theirs alone.
- [ ] `make register` (idempotent), `make up` running.
- [ ] **User:** Console → Templates → LeaseDesk → Use, defaults. Then I run `package-attach` to both new rooms and send `@keymaster reload-policy`.
- [ ] **User:** in access-desk, `!follow-ups off`; add the friend to both channels; the friend sends `@keymaster whoami`. I add their id to the live policy (security group) with `library-doc-update`. Then Capture as template on access-desk; I diff it against our `access` room block and log the differences in DECISIONS.
- [ ] **User:** archive the Part 2 #access-desk and #security-review (ask first; Review Focus 1).
- [ ] Verify in Console: scribe provider Claude Code, model sonnet, effort low, the six disallowed tools, anyone can talk to it, and directory `{$agents_dir}/scribe` (an empty folder); skeptic on the chosen free model.

### Task 7: `make doctor` (step 8)

**Files:** Rewrite `scripts/doctor.py` as glue over `leasedesk/doctor.py`. Test: `tests/test_doctor.py`.

**Produces:**
- `Check(name: str, ok: bool, detail: str, fix: str)`.
- Pure checkers:
  - `check_env(env_path, secrets_dir) -> list[Check]`
  - `check_rooms(rooms: list[RoomFacts], want_agents: set[str]) -> list[Check]`, where `RoomFacts{name, id, agents: set[str], roles: dict[str, list[str]], packages: set[str], archived: bool}`
  - `check_docs(policy_text: str | None, catalog_text: str | None) -> list[Check]`
  - `check_aws_profiles(mode: str, profiles: set[str]) -> list[Check]`
- IO: `/health`, keymaster and sentinel `get_my_context` with their own keys, and gateway `GET /rooms`, `/rooms/{id}`, `/rooms/{id}/roles`, `/rooms/{id}/packages`, `/packages/{id}/documents`, `/documents/{id}`.
- Output: `PASS name` or `FAIL name: detail → fix`. Exit 1 on any FAIL.

- [ ] Tests first:
  - `test_missing_agent_fails_with_invite_fix`.
  - `test_duplicate_room_name_fails`.
  - `test_role_without_holder_fails`: drafter and executor need a holder, reviewer needs sentinel.
  - `test_package_missing_fails_with_attach_fix`.
  - `test_policy_parse_error_fails`.
  - `test_real_mode_missing_profile_fails`.
  - `test_env_mode_644_fails`.
  - `test_all_good_passes`.
- [ ] Implement, `make lint test`, then run `make doctor` live until it shows all PASS.

### Task 8: Live scenarios (step 9)

**Files:** Create `docs/SCENARIOS.md`.

- [ ] Write scenarios 1–8 from the brief. Each has: setup, what to type (and who types it), expected messages and states, and a record block: `## Scenario N: <name>` / `Run: <date time>, <mode>` / `Result: PASS|FAIL. …` / `Notes: …`.
- [ ] Run them with the user in Discord, recording the time and result of each:
  - Scenario 3 needs the friend.
  - Scenario 7 needs real mode. **User** names the admin profile, then `scripts/aws/setup_sandbox.sh --profile <it>` and `LEASEDESK_AWS_MODE=real`. Kill keymaster while a lease is ACTIVE, wait for expiry (`make check-access` shows DENIED), restart, and the reconciler cleans up. Then back to mock.
  - Scenario 8 stops skeptic in Console.
- [ ] Fix what fails, with a test first for each fix.

### Task 9: `make e2e-mock` in one process (step 10)

**Files:** Create `tests/e2e/{__init__.py,conftest.py,fakeswitch.py,scribe.py,test_scenarios.py}`. Modify `Makefile` (`e2e-mock: pytest -m e2e tests/e2e tests/keymaster/test_e2e_moto.py`), `pyproject.toml` (marker) if needed.

**Produces:**
- `FakeSwitch`:
  - `.room(name, agents, people, roles) -> room_id`
  - `.transport(agent) -> httpx.MockTransport` implementing every op keymaster and sentinel call (from `grep -o 'call("[a-z_]*"' leasedesk/switch/ops.py` and the typed wrappers)
  - `.connection_factory` (the `SwitchAgent` signature; a session with `ready`, `connection_id`, `run()`, `stop()`)
  - `.say(room_id, person, body, thread_id=None) -> message_id`
  - `.posts(room_id) -> list[Posted]`
  - `await .wait_for(pred, timeout=5)`
  - Targeted delivery: by role → holders, by name → that agent. Body prefixed `@name `, as recorded live.
  - Documents: one person-written access-policy and resource-catalog via a package.
- `ScribeStandIn.draft(room_id, thread_id, asker, proposal) -> None`: posts `@keymaster submit` plus the lease block in the asker's thread, like the brief's scribe.

- [ ] Tests, `pytestmark = pytest.mark.e2e`, using real `Keymaster` and `Sentinel` (mock mode, `mock_aws` seeded by `mockaws.seed`, injected clock):
  - `test_scenario1_happy_path`: simulate ALLOWED while ACTIVE; after the clock moves past expiry, DENIED; ledger row written.
  - `test_scenario2_self_approve_refused`.
  - `test_scenario4_block_with_fix`: `iam:PassRole` + `lambda:CreateFunction` → BLOCKED; the finding names the escalation path and its reference; sentinel offers a corrected lease or a narrower fix.
  - `test_scenario5_injection`: a CTO/AdministratorAccess draft is refused or BLOCKED; a hand-pasted LOW verdict is ignored; `@keymaster approve` from an agent is refused.
  - `test_scenario6_freeze`: active lease revoked at once; submit refused; unfreeze.
  - `test_no_skeptic_does_not_wait` (Review Focus 2).
  - A session-scoped check fails if the e2e run takes ≥ 60 s.
- [ ] Run `make e2e-mock` and expect PASS in < 60 s. Then `make lint test`.

### Task 10: Polish (step 11)

**Files:** Modify `leasedesk/common/format.py` (Ruling 9), `leasedesk/sentinel/agent.py` (`_HEADER`), `leasedesk/switch/register.py` (display names, if the server takes them at registration; otherwise a `gateway.py` patch). Tests: `tests/common/test_format.py`, `tests/sentinel/test_agent.py`.

- [ ] Tests first:
  - `test_every_lease_message_starts_with_id_and_state`: parametrized over every lease formatter, regex `^LS-\d{4} · [A-Z_]+ · `.
  - `test_review_header_old_and_new_forms_accepted`.
  - `test_register_sends_display_name` (if supported).
- [ ] Implement, then `make lint test e2e-mock`.

### Task 11: Done (step 12)

- [ ] Definition of done:
  - template imports with 0 warnings and creates both rooms, the link, the roles, the how-to-ask doc and both agents;
  - `make doctor` all PASS;
  - SCENARIOS.md has 1–8 recorded, 7 in real mode;
  - `make e2e-mock` < 60 s;
  - `make lint test` green.
- [ ] DECISIONS: Rulings 1–11, the live differences, the capture diff.
- [ ] PROTOCOL_NOTES: template run, package documents in `list_references`, roles live.
- [ ] Secret scan over everything git would commit; one commit `feat: rooms as code, template and scenarios`.
- [ ] Memory update, then the 5-line summary for Part 5.
