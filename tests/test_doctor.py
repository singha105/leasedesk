"""make doctor: pure checkers over the setup's facts, and the report they print."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from leasedesk import doctor
from leasedesk.doctor import Check, RoomFacts
from leasedesk.switch.gateway_admin import GatewayAdmin
from leasedesk.switch.models import MyContext

ROOT = Path(__file__).resolve().parent.parent
GOOD_ACCESS_ROLES = {"drafter": ["scribe"], "executor": ["keymaster"], "reviewer": ["sentinel"]}


def access(**kw: object) -> RoomFacts:
    base: dict[str, object] = {
        "name": "access-desk",
        "id": "r-1",
        "agents": {"scribe", "skeptic", "keymaster", "sentinel"},
        "roles": dict(GOOD_ACCESS_ROLES),
        "packages": {"leasedesk-governance"},
    }
    return RoomFacts.model_validate({**base, **kw})


def review(**kw: object) -> RoomFacts:
    base: dict[str, object] = {
        "name": "security-review",
        "id": "r-2",
        "agents": {"keymaster", "sentinel"},
        "roles": {},
        "packages": {"leasedesk-governance"},
    }
    return RoomFacts.model_validate({**base, **kw})


def failed(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok]


def only_failure(checks: list[Check]) -> Check:
    (f,) = failed(checks)
    return f


# --- rooms --------------------------------------------------------------------------------------


def test_missing_agent_fails_with_invite_fix() -> None:
    f = only_failure(doctor.check_rooms([access(agents={"scribe", "keymaster"}), review()]))
    assert f.name == "access-desk agents"
    assert "skeptic" in f.detail and "sentinel" in f.detail
    assert "invite" in f.fix.lower()


def test_duplicate_room_name_fails() -> None:
    old = review(id="r-old")
    f = only_failure(doctor.check_rooms([access(), review(), old]))
    assert f.name == "room security-review"
    assert "2 live rooms" in f.detail and "r-old" in f.detail
    assert "archive the old room" in f.fix


def test_archived_duplicate_is_ignored() -> None:
    old = review(id="r-old", archived=True, packages=set(), agents=set())
    assert failed(doctor.check_rooms([access(), review(), old])) == []


def test_missing_room_fails_with_template_fix() -> None:
    f = only_failure(doctor.check_rooms([access()]))
    assert f.name == "room security-review"
    assert "template-run" in f.fix


def test_role_without_holder_fails() -> None:
    for role, holders, who in (
        ("drafter", [], "scribe"),
        ("executor", [], "keymaster"),
        ("executor", ["scribe"], "keymaster"),
        ("reviewer", ["skeptic"], "sentinel"),
    ):
        roles = {**GOOD_ACCESS_ROLES, role: holders}
        f = only_failure(doctor.check_rooms([access(roles=roles), review()]))
        assert f.name == f"access-desk role {role}", role
        assert who in f.fix, (role, f.fix)


def test_reviewer_shared_with_skeptic_passes() -> None:
    roles = {**GOOD_ACCESS_ROLES, "reviewer": ["sentinel", "skeptic"]}
    assert failed(doctor.check_rooms([access(roles=roles), review()])) == []


def test_undefined_role_fails() -> None:
    roles = {"drafter": ["scribe"], "executor": ["keymaster"]}
    f = only_failure(doctor.check_rooms([access(roles=roles), review()]))
    assert f.name == "access-desk role reviewer"
    assert "not defined" in f.detail


def test_roles_in_security_review_fail() -> None:
    f = only_failure(doctor.check_rooms([access(), review(roles={"reviewer": []})]))
    assert f.name == "security-review roles"
    assert "reviewer" in f.detail and "one role lease" in f.detail


def test_package_missing_fails_with_attach_fix() -> None:
    f = only_failure(doctor.check_rooms([access(), review(packages=set())]))
    assert f.name == "security-review package"
    assert "package-attach --room r-2" in f.fix


# --- documents ----------------------------------------------------------------------------------


def _policy_text() -> str:
    text = (ROOT / "switch/docs/access-policy.yaml").read_text()
    return text.replace("<ACCOUNT_ID>", "123456789012")


def _catalog_text() -> str:
    return (ROOT / "switch/docs/resource-catalog.md").read_text()


def test_policy_parse_error_fails() -> None:
    checks = doctor.check_docs("approvals: [not, a, policy", _catalog_text())
    f = only_failure(checks)
    assert f.name == "access-policy"
    assert f.detail and "library-doc-update" in f.fix


def test_missing_document_fails() -> None:
    f = only_failure(doctor.check_docs(_policy_text(), None))
    assert f.name == "resource-catalog"
    assert "leasedesk-governance" in f.detail


def test_catalog_parse_error_fails() -> None:
    bad = (
        "| ARN | Owner | Sensitivity | Typical access | Sensitive actions |\n"
        "|-|-|-|-|-|\n"
        "| a | b | x | c | d |\n"
    )
    f = only_failure(doctor.check_docs(_policy_text(), bad))
    assert f.name == "resource-catalog"


# --- AWS, env, providers, template -----------------------------------------------------------


def test_real_mode_missing_profile_fails() -> None:
    checks = doctor.check_aws_profiles("real", {"leasedesk-keymaster", "default"})
    names = [c.name for c in failed(checks)]
    assert names == ["AWS profile leasedesk-sentinel", "AWS profile lease-demo-dev"]
    assert all("setup_sandbox.sh" in c.fix for c in failed(checks))


def test_mock_mode_needs_no_profile() -> None:
    assert failed(doctor.check_aws_profiles("mock", set())) == []


def test_env_mode_644_fails(tmp_path: Path) -> None:
    env, secrets, state = _good_files(tmp_path)
    env.chmod(0o644)
    f = only_failure(doctor.check_env(env, secrets, state))
    assert (f.name, f.fix) == (".env", "chmod 600 .env")
    assert "0o644" in f.detail


def test_secret_file_mode_fails(tmp_path: Path) -> None:
    env, secrets, state = _good_files(tmp_path)
    (secrets / "agents" / "keymaster.json").chmod(0o644)
    (state / "mock-aws.env").chmod(0o640)
    bad = failed(doctor.check_env(env, secrets, state))
    assert [c.name for c in bad] == [".secrets", ".state"]
    assert "agents/keymaster.json" in bad[0].detail
    assert "mock-aws.env" in bad[1].detail


def _good_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    env.chmod(0o600)
    secrets = tmp_path / ".secrets"
    (secrets / "agents").mkdir(parents=True)
    for d in (secrets, secrets / "agents"):
        d.chmod(0o700)
    key = secrets / "agents" / "keymaster.json"
    key.write_text("{}")
    key.chmod(0o600)
    state = tmp_path / ".state"
    state.mkdir()
    state.chmod(0o700)
    mock = state / "mock-aws.env"
    mock.write_text("A=1\n")
    mock.chmod(0o600)
    (state / "moto.pid").write_text("1")  # not a credential: any mode
    return env, secrets, state


def _context(opencode: str = "1.19.5", process: str = "running") -> MyContext:
    managed = {"machine": {"id": "m-1", "name": "Mac", "state": "online"}, "process": process}
    return MyContext.model_validate(
        {
            "you": {"name": "keymaster"},
            "machines": [
                {
                    "id": "m-1",
                    "name": "Mac",
                    "state": "online",
                    "providers": [
                        {"provider": "claude", "installed": True, "version": "2", "auth": "ok"},
                        {
                            "provider": "opencode",
                            "installed": True,
                            "version": opencode,
                            "auth": "ok",
                        },
                    ],
                }
            ],
            "agents": [
                {"id": "a-1", "name": "scribe", "who_can_talk": "anyone", "managed": managed},
                {"id": "a-2", "name": "skeptic", "who_can_talk": "anyone", "managed": managed},
                {"id": "a-3", "name": "keymaster", "who_can_talk": "anyone", "managed": None},
                {"id": "a-4", "name": "sentinel", "who_can_talk": "anyone", "managed": None},
            ],
        }
    )


def test_opencode_2_fails_with_npm_fix() -> None:
    f = only_failure(doctor.check_context(_context(opencode="2.2.6")))
    assert f.name == "OpenCode"
    assert "2.2.6" in f.detail
    assert "opencode-ai@1" in f.fix


def test_console_agent_not_running_fails() -> None:
    bad = failed(doctor.check_context(_context(process="stopped")))
    assert [c.name for c in bad] == ["scribe", "skeptic"]
    assert "stopped" in bad[0].detail


def test_agent_not_open_to_anyone_fails() -> None:
    ctx = _context()
    ctx.agents[2].who_can_talk = "owner_only"
    f = only_failure(doctor.check_context(ctx))
    assert f.name == "keymaster addressing"
    assert "gateway.py addressing --agent keymaster --mode anyone" in f.fix


def test_template_drift_fails() -> None:
    local = "version: 2\n"
    assert failed(doctor.check_template(local, local)) == []
    f = only_failure(doctor.check_template("version: 1\n", local))
    assert f.name == "Console template LeaseDesk"
    assert only_failure(doctor.check_template(None, local)).detail == "not imported"


# --- the whole report ---------------------------------------------------------------------------


def test_all_good_passes(tmp_path: Path) -> None:
    env, secrets, state = _good_files(tmp_path)
    checks = [
        *doctor.check_env(env, secrets, state),
        *doctor.check_rooms([access(), review()]),
        *doctor.check_docs(_policy_text(), _catalog_text()),
        *doctor.check_aws_profiles("mock", set()),
        *doctor.check_context(_context()),
        *doctor.check_template("t", "t"),
    ]
    assert failed(checks) == []
    text, code = doctor.report(checks)
    assert code == 0
    assert all(line.startswith("PASS ") for line in text.splitlines()[:-1])
    assert text.splitlines()[-1] == f"doctor: all {len(checks)} checks pass"


def test_report_shows_detail_and_fix_and_exits_1() -> None:
    text, code = doctor.report(
        [Check(name="a", ok=True), Check(name="b", ok=False, detail="d", fix="f")]
    )
    assert code == 1
    assert text.splitlines() == ["PASS a", "FAIL b: d → f", "doctor: 1 of 2 checks failed"]


# --- reading the facts through the gateway ------------------------------------------------------


def test_room_facts_from_gateway() -> None:
    rooms = [
        {"id": "r-1", "name": "access-desk", "archived": False},
        {"id": "r-0", "name": "access-desk", "archived": True},
    ]
    detail = {
        "id": "r-1",
        "name": "access-desk",
        "archived": False,
        "agent_ids": ["a-1", "a-3"],
        "roles": [{"name": "executor", "exclusive": True, "held_by": ["keymaster"]}],
    }
    agents = [{"id": "a-1", "name": "scribe"}, {"id": "a-3", "name": "keymaster"}]
    packages = [{"id": "p-1", "name": "leasedesk-governance"}]
    seen: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}?{req.url.query.decode()}")
        routes = {
            "/gateway/auth/login": {},
            "/gateway/auth/logout": {},
            "/gateway/rooms": rooms,
            "/gateway/agents": agents,
            "/gateway/rooms/r-1": detail,
            "/gateway/rooms/r-1/packages": packages,
        }
        body = routes.get(req.url.path)
        if body is None:
            return httpx.Response(404, json={"detail": "nope"})
        return httpx.Response(200, content=json.dumps(body))

    async def run() -> list[RoomFacts]:
        gw = GatewayAdmin(
            "http://switch", email="e", password="p", transport=httpx.MockTransport(handle)
        )
        async with gw:
            return await doctor.room_facts(gw)

    facts = asyncio.run(run())
    assert facts == [
        RoomFacts(
            name="access-desk",
            id="r-1",
            agents={"scribe", "keymaster"},
            roles={"executor": ["keymaster"]},
            packages={"leasedesk-governance"},
        ),
        RoomFacts(name="access-desk", id="r-0", archived=True),
    ]
    assert "GET /gateway/rooms?include_archived=true" in seen
    assert not any("/rooms/r-0/" in s for s in seen), "archived rooms are not read further"


def test_governance_rejects_distinct_ids_but_deduplicates_same_id() -> None:
    from unittest.mock import AsyncMock

    import pytest

    from leasedesk.switch.gateway_admin import GwDocument, GwNamed

    async def run() -> None:
        gw = AsyncMock(spec=GatewayAdmin)
        gw.room_packages.return_value = [GwNamed(id="p", name="leasedesk-governance")]
        first = GwDocument(id="one", name="access-policy", content=_policy_text())
        second = GwDocument(id="two", name="access-policy", content=_policy_text())
        gw.package_documents.return_value = [first, second]
        with pytest.raises(LookupError, match=r"more than one.*access-policy"):
            await doctor.governance_texts(gw, "room")
        gw.document.assert_not_awaited()
        gw.package_documents.return_value = [first, first]
        gw.document.return_value = first
        assert await doctor.governance_texts(gw, "room") == {"access-policy": _policy_text()}
        gw.document.assert_awaited_once_with("one")

    asyncio.run(run())
