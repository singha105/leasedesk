"""make doctor: is the LeaseDesk setup healthy?

Pure checkers turn facts into ``Check`` lines; ``gather`` reads the facts: file modes, the
server's /health, keymaster's and sentinel's own keys (get_my_context), and the Gateway as the
admin person (rooms, roles, the governance package and its documents, the stored template).
Nothing here prints or logs a secret: checks carry names, modes, ids and HTTP statuses only.
"""

from __future__ import annotations

import stat
from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from leasedesk.common.policy import parse_policy_text
from leasedesk.config import Settings
from leasedesk.sentinel.catalog import parse_catalog_text
from leasedesk.switch.credentials import load_credentials
from leasedesk.switch.errors import SwitchError
from leasedesk.switch.gateway_admin import GatewayAdmin, admin_login, server_health
from leasedesk.switch.models import MyContext
from leasedesk.switch.ops import OpsClient

ACCESS, REVIEW = "access-desk", "security-review"
PACKAGE = "leasedesk-governance"
TEMPLATE_NAME = "LeaseDesk"
TEMPLATE_FILE = "switch/leasedesk.template.yaml"
WANT_AGENTS: dict[str, set[str]] = {
    ACCESS: {"scribe", "skeptic", "keymaster", "sentinel"},
    REVIEW: {"keymaster", "sentinel"},
}
# Role -> the agent that must hold it (None: anyone). One live role lease per agent across all
# rooms (docs/PROTOCOL_NOTES.md), so only access-desk defines roles.
ROOM_ROLES: dict[str, dict[str, str | None]] = {
    ACCESS: {"drafter": None, "executor": "keymaster", "reviewer": "sentinel"},
    REVIEW: {},
}
CONSOLE_AGENTS = ("scribe", "skeptic")
OPEN_AGENTS = ("scribe", "skeptic", "keymaster", "sentinel")
REAL_PROFILES = ("leasedesk-keymaster", "leasedesk-sentinel", "lease-demo-dev")
RUN_TEMPLATE = f"uv run python scripts/gateway.py template-run {TEMPLATE_FILE}"
OPENCODE_FIX = (
    "npm install -g --prefix ~/.local opencode-ai@1 (Switch 0.40 can't start OpenCode 2.x)"
)


class Check(BaseModel):
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""

    def line(self) -> str:
        return f"PASS {self.name}" if self.ok else f"FAIL {self.name}: {self.detail} → {self.fix}"


class RoomFacts(BaseModel):
    name: str
    id: str
    agents: set[str] = Field(default_factory=set)  # agent names
    roles: dict[str, list[str]] = Field(default_factory=dict)  # role -> holder names
    packages: set[str] = Field(default_factory=set)  # package names
    archived: bool = False


def _pass(name: str) -> Check:
    return Check(name=name, ok=True)


def _fail(name: str, detail: str, fix: str) -> Check:
    return Check(name=name, ok=False, detail=detail, fix=fix)


def report(checks: list[Check]) -> tuple[str, int]:
    bad = sum(not c.ok for c in checks)
    tail = (
        f"doctor: all {len(checks)} checks pass"
        if not bad
        else f"doctor: {bad} of {len(checks)} checks failed"
    )
    return "\n".join([*(c.line() for c in checks), tail]), 1 if bad else 0


# --- files --------------------------------------------------------------------------------------


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _bad_modes(root: Path, paths: Iterable[Path]) -> list[str]:
    out = []
    for p in paths:
        want = 0o700 if p.is_dir() else 0o600
        if _mode(p) != want:
            rel = "." if p == root else str(p.relative_to(root))
            out.append(f"{rel} {oct(_mode(p))}")
    return out


def check_env(env_path: Path, secrets_dir: Path, state_dir: Path | None = None) -> list[Check]:
    """.env 600; everything under .secrets/ 600 (dirs 700); .state/ 700 and its *.env 600."""
    checks: list[Check] = []
    if not env_path.exists():
        checks.append(_fail(".env", "missing", "cp .env.example .env && chmod 600 .env"))
    elif _mode(env_path) != 0o600:
        detail = f"mode {oct(_mode(env_path))} (want 0o600)"
        checks.append(_fail(".env", detail, f"chmod 600 {env_path.name}"))
    else:
        checks.append(_pass(".env"))

    if not secrets_dir.exists():
        checks.append(_fail(".secrets", "missing", "make register"))
    else:
        bad = _bad_modes(secrets_dir, [secrets_dir, *sorted(secrets_dir.rglob("*"))])
        fix = "chmod 700 the directories and 600 the files under .secrets/"
        checks.append(_fail(".secrets", ", ".join(bad), fix) if bad else _pass(".secrets"))

    if state_dir is not None and state_dir.exists():
        bad = _bad_modes(state_dir, [state_dir, *sorted(state_dir.glob("*.env"))])
        fix = "chmod 700 .state and 600 its .env files"
        checks.append(_fail(".state", ", ".join(bad), fix) if bad else _pass(".state"))
    return checks


# --- rooms --------------------------------------------------------------------------------------


def _role_fix(role: str, holder: str | None) -> str:
    if holder is None:
        return f"@scribe take the {role} role in this room"
    return (
        f"{holder} claims {role} when it starts (and retries every 60 s): restart it with "
        f"make up, and check {holder} holds no role in another room"
    )


def _room_roles(room: RoomFacts, want: dict[str, str | None]) -> list[Check]:
    if not want:
        if room.roles:
            detail = (
                f"has {', '.join(sorted(room.roles))}; an agent holds one role lease across "
                f"all rooms, so a role here can take one away from {ACCESS}"
            )
            fix = f"DELETE /gateway/rooms/{room.id}/roles/<name> for each"
            return [_fail(f"{room.name} roles", detail, fix)]
        return [_pass(f"{room.name} roles")]
    checks = []
    for role, holder in want.items():
        name = f"{room.name} role {role}"
        if role not in room.roles:
            checks.append(_fail(name, "not defined", f"re-run the template: {RUN_TEMPLATE}"))
            continue
        held = room.roles[role]
        ok = bool(held) if holder is None else holder in held
        detail = f"held by {', '.join(held) or 'nobody'}"
        checks.append(_pass(name) if ok else _fail(name, detail, _role_fix(role, holder)))
    return checks


def check_rooms(
    rooms: list[RoomFacts], want_agents: dict[str, set[str]] | None = None
) -> list[Check]:
    """One live room per name, with its agents, the governance package and the right roles."""
    want_agents = WANT_AGENTS if want_agents is None else want_agents
    checks: list[Check] = []
    for name, agents in want_agents.items():
        live = [r for r in rooms if r.name == name and not r.archived]
        if not live:
            checks.append(_fail(f"room {name}", "not found", RUN_TEMPLATE))
            continue
        if len(live) > 1:
            ids = ", ".join(r.id for r in live)
            detail = f"{len(live)} live rooms named {name}: {ids}"
            fix = (
                "archive the old room so escalations and approvals land in one place "
                "(POST /gateway/rooms/<id>/archive)"
            )
            checks.append(_fail(f"room {name}", detail, fix))
            continue
        (room,) = live
        checks.append(_pass(f"room {name}"))
        missing = agents - room.agents
        if missing:
            fix = f"invite them to #{name} (Console: the room → Agents → Invite)"
            checks.append(_fail(f"{name} agents", f"missing {', '.join(sorted(missing))}", fix))
        else:
            checks.append(_pass(f"{name} agents"))
        if PACKAGE in room.packages:
            checks.append(_pass(f"{name} package"))
        else:
            fix = f"uv run python scripts/gateway.py package-attach --room {room.id}"
            checks.append(_fail(f"{name} package", f"{PACKAGE} not attached", fix))
        checks.extend(_room_roles(room, ROOM_ROLES.get(name, {})))
    return checks


# --- documents, AWS, providers, template --------------------------------------------------------


def check_docs(policy_text: str | None, catalog_text: str | None) -> list[Check]:
    """The person-written access-policy and resource-catalog parse as keymaster and sentinel
    parse them."""
    checks = []
    for name, text, parse in (
        ("access-policy", policy_text, parse_policy_text),
        ("resource-catalog", catalog_text, parse_catalog_text),
    ):
        if text is None:
            detail = f"no person-written {name} in {PACKAGE}"
            fix = (
                f"a person creates it (scripts/gateway.py library-doc) and adds it to {PACKAGE} "
                "(scripts/gateway.py package)"
            )
            checks.append(_fail(name, detail, fix))
            continue
        try:
            parse(text)
        except (ValueError, yaml.YAMLError) as exc:
            detail = (str(exc).splitlines() or [type(exc).__name__])[0][:200]
            fix = "a person fixes it: uv run python scripts/gateway.py library-doc-update"
            checks.append(_fail(name, detail, f"{fix} --doc-id <id> --content-file <file>"))
        else:
            checks.append(_pass(name))
    return checks


def check_aws_profiles(mode: str, profiles: set[str]) -> list[Check]:
    if mode != "real":
        return [_pass(f"AWS mode {mode}")]
    fix = "scripts/aws/setup_sandbox.sh --profile <your-admin-profile>"
    return [_pass("AWS mode real")] + [
        _pass(f"AWS profile {p}")
        if p in profiles
        else _fail(f"AWS profile {p}", "not in ~/.aws/config", fix)
        for p in REAL_PROFILES
    ]


def _provider_check(ctx: MyContext, provider: str, label: str, fix: str) -> Check:
    seen = []
    for m in ctx.machines:
        for p in m.providers:
            if p.provider != provider:
                continue
            if not p.installed:
                seen.append(f"not installed on {m.name}")
            elif provider == "opencode" and not (p.version or "").startswith("1."):
                seen.append(f"{p.version} on {m.name} (want 1.x)")
            elif p.auth != "ok":
                seen.append(f"sign-in {p.auth} on {m.name}")
            elif m.state != "online":
                seen.append(f"{m.name} is {m.state}")
            else:
                return _pass(label)
    return _fail(label, "; ".join(seen) or "no machine reports it", fix)


def check_context(ctx: MyContext) -> list[Check]:
    """What keymaster's get_my_context shows: the providers scribe and skeptic run on, that
    Console runs them, and that anyone may talk to every LeaseDesk agent."""
    checks = [
        _provider_check(ctx, "opencode", "OpenCode", OPENCODE_FIX),
        _provider_check(ctx, "claude", "Claude Code", "sign in to Claude Code on that machine"),
    ]
    agents = {a.name: a for a in ctx.agents}
    for name in CONSOLE_AGENTS:
        a = agents.get(name)
        start = f"start {name} in Console (Agents → {name})"
        if a is None:
            checks.append(_fail(name, "no such agent", RUN_TEMPLATE))
        elif a.managed is None:
            checks.append(_fail(name, "not run by Console", RUN_TEMPLATE))
        elif a.managed.process != "running":
            checks.append(_fail(name, f"process {a.managed.process}", start))
        else:
            checks.append(_pass(name))
    for name in OPEN_AGENTS:
        a = agents.get(name)
        if a is not None and a.who_can_talk != "anyone":
            fix = f"uv run python scripts/gateway.py addressing --agent {name} --mode anyone"
            checks.append(_fail(f"{name} addressing", f"who can talk: {a.who_can_talk}", fix))
        elif a is not None:
            checks.append(_pass(f"{name} addressing"))
    return checks


def check_template(stored: str | None, local: str) -> list[Check]:
    name = f"Console template {TEMPLATE_NAME}"
    if stored is None:
        fix = f"Console → Templates → Import {TEMPLATE_FILE}"
        return [_fail(name, "not imported", fix)]
    if stored != local:
        fix = f"PATCH /gateway/templates/<id> with {TEMPLATE_FILE}, or re-import it in Console"
        return [_fail(name, f"differs from {TEMPLATE_FILE} (make template?)", fix)]
    return [_pass(name)]


# --- reading the facts --------------------------------------------------------------------------


async def room_facts(
    gw: GatewayAdmin, names: Iterable[str] = tuple(WANT_AGENTS)
) -> list[RoomFacts]:
    """Every room with a LeaseDesk name, archived ones included (but not read further)."""
    wanted = set(names)
    agent_names = {a.id: a.name for a in await gw.list_agents()}
    facts = []
    for r in await gw.list_rooms(include_archived=True):
        if r.name not in wanted:
            continue
        if r.archived:
            facts.append(RoomFacts(name=r.name, id=r.id, archived=True))
            continue
        detail = await gw.room(r.id)
        packages = await gw.room_packages(r.id)
        facts.append(
            RoomFacts(
                name=r.name,
                id=r.id,
                agents={agent_names.get(i, i) for i in detail.agent_ids},
                roles={role.name: role.held_by for role in detail.roles},
                packages={p.name for p in packages},
            )
        )
    return facts


async def governance_texts(gw: GatewayAdmin, room_id: str) -> dict[str, str]:
    """Person-written documents in the governance package attached to ``room_id``, by name."""
    documents: dict[str, set[str]] = {}
    for pkg in await gw.room_packages(room_id):
        if pkg.name != PACKAGE:
            continue
        for d in await gw.package_documents(pkg.id):
            if d.created_by_agent_id is None and d.name in {"access-policy", "resource-catalog"}:
                documents.setdefault(d.name, set()).add(d.id)
    for name, ids in documents.items():
        if len(ids) > 1:
            raise LookupError(f"more than one person-written document is named {name}")
    return {
        name: (await gw.document(next(iter(ids)))).content or "" for name, ids in documents.items()
    }


async def agent_context(base_url: str, name: str, secrets_dir: Path) -> tuple[Check, MyContext]:
    label = f"{name} key"
    try:
        creds = load_credentials(name, secrets_dir)
    except FileNotFoundError:
        return _fail(label, "no saved key", "make register"), MyContext()
    try:
        async with OpsClient(base_url, creds) as ops:
            ctx = MyContext.model_validate(await ops.get_my_context())
    except SwitchError as exc:
        return _fail(label, f"get_my_context: {type(exc).__name__}", "make register"), MyContext()
    if ctx.you.name != name:
        return _fail(label, "the key belongs to another agent", "make register"), ctx
    return _pass(label), ctx


async def gather(settings: Settings, root: Path, *, prompt: bool = False) -> list[Check]:
    checks = check_env(root / ".env", root / settings.secrets_dir, root / settings.state_dir)
    base = settings.switch_base_url
    if not base:
        fix = "uv run python scripts/spikes/health.py, then set it in .env"
        return [*checks, _fail("SWITCH_BASE_URL", "not set", fix), *_aws(settings)]
    try:
        status = await server_health(base)
    except Exception as exc:
        status, why = 0, type(exc).__name__
    else:
        why = f"HTTP {status}"
    if status != 200:
        checks.append(_fail("Switch server", f"GET /health: {why}", "start Switch Console"))
        return [*checks, *_aws(settings)]
    checks.append(_pass("Switch server"))

    secrets = root / settings.secrets_dir
    keymaster_ok, keymaster_ctx = await agent_context(base, "keymaster", secrets)
    sentinel_ok, _ = await agent_context(base, "sentinel", secrets)
    checks += [keymaster_ok, sentinel_ok]
    if keymaster_ok.ok:
        checks.extend(check_context(keymaster_ctx))

    try:
        email, password = admin_login(prompt)
    except SystemExit as exc:
        fix = "run scripts/doctor.py --prompt"
        return [*checks, _fail("Gateway admin", str(exc), fix), *_aws(settings)]
    try:
        async with GatewayAdmin(base, email=email, password=password) as gw:
            del password
            rooms = await room_facts(gw)
            checks.extend(check_rooms(rooms))
            access = [r for r in rooms if r.name == ACCESS and not r.archived]
            try:
                texts = await governance_texts(gw, access[0].id) if len(access) == 1 else {}
            except LookupError as exc:
                checks.append(
                    _fail("governance documents", str(exc), "keep one document ID per name")
                )
            else:
                checks.extend(check_docs(texts.get("access-policy"), texts.get("resource-catalog")))
            stored = None
            for t in await gw.list_templates():
                if t.name == TEMPLATE_NAME and not t.official:
                    stored = await gw.template_content(t.id)
            checks.extend(check_template(stored, (root / TEMPLATE_FILE).read_text()))
    except SwitchError as exc:
        checks.append(_fail("Gateway admin", f"{type(exc).__name__}", "run with --prompt"))
    return [*checks, *_aws(settings)]


def _aws(settings: Settings) -> list[Check]:
    if settings.aws_mode != "real":
        return check_aws_profiles(settings.aws_mode, set())
    import boto3  # only in real mode: reads profile names, never credentials

    return check_aws_profiles("real", set(boto3.session.Session().available_profiles))
