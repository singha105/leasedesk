# LeaseDesk

LeaseDesk is a just-in-time cloud access desk that lives in a Switch room. An engineer asks in plain English, the scribe agent (Claude) drafts a lease proposal, sentinel (deterministic rules plus AWS Access Analyzer) and skeptic (a free model) attack it, a named human approves in chat, and keymaster writes a time-boxed inline IAM policy that AWS itself stops honouring at expiry. AI only drafts and critiques; the decision and the credentials sit behind deterministic gates. Built solo for the WeMakeDevs "Flip the Switch" hackathon (Best Business Workflow track, Nov 3-7 2026). Full spec: [docs/SPEC.md](docs/SPEC.md).

## Ground truth rule

This spec was written from the Switch docs, not from a running server. The live server wins. Before writing protocol code, confirm every route, field and operation name against GET /agents/{agent_id}/ops on the running server and against the Switch docs MCP. When this spec disagrees with the server, follow the server and log the difference in docs/DECISIONS.md.

Recorded server behaviour lives in [docs/PROTOCOL_NOTES.md](docs/PROTOCOL_NOTES.md) and the live operation list in [docs/switch-ops.json](docs/switch-ops.json). Read both before touching `leasedesk/switch/`.

## Commands

| Command | What it does |
| - | - |
| `make setup` | `uv sync` with dev dependencies (Python 3.12) |
| `make lint` | `ruff check`, `ruff format --check`, `mypy leasedesk/` |
| `make test` | `pytest` |
| `make up` | start the local stack for a demo (filled in by Part 2) |
| `make doctor` | check tools, env, server health and credential file modes |

Other targets (`register`, `run-keymaster`, `run-sentinel`, `e2e-mock`) are stubs until the part that owns them.

## Conventions

- Agents are async (`asyncio`, `httpx.AsyncClient`, `websockets`).
- Every wire payload (Switch frames, op arguments and results, lease and verdict blocks) has a pydantic v2 model. No bare `dict` crosses a module boundary.
- Every Switch call goes through `leasedesk/switch/`. Nothing else imports `httpx` or `websockets` for Switch.
- Logs are structured (`structlog`, JSON) and pass through the redaction processor; API keys, bearer tokens and AWS keys never reach a log line.
- Every rule in the spec's security model has at least one test that would fail if the rule broke.
- AWS mode defaults to `mock` (moto). `real` only through the named profiles `leasedesk-keymaster`, `leasedesk-sentinel`, `lease-demo-dev`.
- Participant ids are opaque strings: match them, never parse them.

## Secrets

- Never commit `.env`, `.secrets/` or `.state/` (all gitignored). Credential files are mode 600, their directories 700.
- Never print a key, even partially, in a terminal, a room, a log, a commit or a screenshot.

## Definition of done for every part

Tests green (`make lint test`), `docs/DECISIONS.md` updated, and a commit.
