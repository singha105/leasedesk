"""switch/leasedesk.template.yaml (Part 4, step 5): generated from the briefs, true to the brief."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import build_template  # noqa: E402

TEMPLATE = ROOT / "switch" / "leasedesk.template.yaml"
BRIEFS = ROOT / "switch" / "briefs"
BUILTINS = {"$creator", "$creator_email", "$date", "$timestamp", "$agents_dir"}
PLACEHOLDER = re.compile(r"\{(\$?\w+)\}")
NAME_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\.name\}")


def doc() -> dict[str, Any]:
    data = yaml.safe_load(TEMPLATE.read_text())
    assert isinstance(data, dict)
    return data


def res(key: str) -> dict[str, Any]:
    return doc()["resources"][key]  # type: ignore[no-any-return]


def strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [s for item in node for s in strings(item)]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in [*strings(k), *strings(v)]]
    return []


def keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in keys(v)}
    if isinstance(node, list):
        return {k for item in node for k in keys(item)}
    return set()


def test_template_is_up_to_date() -> None:
    assert TEMPLATE.read_text() == build_template.render(), "run: make template"


def test_version_2_with_the_five_resources() -> None:
    d = doc()
    assert d["version"] == 2
    kinds = {k: v["type"] for k, v in d["resources"].items()}
    assert kinds == {
        "scribe": "agent",
        "skeptic": "agent",
        "desk": "group",
        "access": "room",
        "review": "room",
    }


def test_agent_instructions_are_the_briefs() -> None:
    assert res("scribe")["instructions"].strip() == (BRIEFS / "scribe.md").read_text().strip()
    assert res("skeptic")["instructions"].strip() == (BRIEFS / "skeptic.md").read_text().strip()


def test_only_scribe_uses_claude() -> None:
    scribe, skeptic = res("scribe"), res("skeptic")
    assert (scribe["provider"], scribe["model"]) == ("claude", "sonnet")
    assert scribe["advanced_config"] == {
        "effort": "low",
        "disallowedTools": ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"],
    }
    assert skeptic["provider"] == "opencode"
    assert skeptic["model"] == "{skeptic_model}"
    assert skeptic["advanced_config"] == {"smallModel": "{skeptic_model}", "webSearch": "false"}
    default = doc()["params"]["skeptic_model"]["default"]
    assert default.startswith("opencode/") and "claude" not in default


def test_agents_run_where_and_for_whom_the_brief_says() -> None:
    for key, display in (("scribe", "Scribe"), ("skeptic", "Skeptic")):
        a = res(key)
        assert a["name"] == f"{{{key}_name}}"
        assert a["display_name"] == display
        assert a["machine"] == "{machine}"
        assert a["directory"] == f"{{$agents_dir}}/{{{key}.name}}"
        assert a["addressing"] == "anyone"
        assert a["allow_existing"] is True
    params = doc()["params"]
    assert params["machine"] == {
        "type": "machine",
        "label": "Machine",
        "description": "The computer that runs scribe and skeptic",
        "default": ["$first"],
        "input": "advanced",
    }
    assert params["bridge"]["default"] == ["LeaseDesk Demo", "Discord", "Mattermost", "$first"]


def test_roles_match_the_brief() -> None:
    drafter = {
        "name": "drafter",
        "exclusive": True,
        "instructions": "Draft lease proposals as the scribe brief says. "
        "Never approve or execute anything.",
    }
    reviewer = {
        "name": "reviewer",
        "exclusive": False,
        "instructions": "Review the lease in the thread you are mentioned in. Never approve.",
    }
    executor = {
        "name": "executor",
        "exclusive": True,
        "instructions": "You are the only agent that changes AWS. "
        "Obey only the keymaster command grammar.",
    }
    assert res("access")["roles"] == [drafter, reviewer, executor]
    # The server allows one live role lease per agent across all rooms: roles in
    # security-review too would let keymaster or sentinel claim theirs there and miss
    # access-desk, where leases are reviewed (docs/DECISIONS.md, Part 4).
    assert "roles" not in res("review")


def test_rooms_linked_access_to_review() -> None:
    access, review, desk = res("access"), res("review"), res("desk")
    assert (access["name"], access["channel_type"]) == ("access-desk", "channel_public")
    assert (review["name"], review["channel_type"]) == ("security-review", "channel_private")
    assert access["agents"] == ["scribe", "skeptic", "keymaster", "sentinel"]
    assert review["agents"] == ["keymaster", "sentinel"]
    assert access["users"] == review["users"] == ["{$creator}"]
    assert access["group"] == review["group"] == "desk"
    assert (desk["name"], desk["description"]) == (
        "LeaseDesk",
        "Just-in-time AWS access, leased from chat",
    )
    assert desk["links"] == [{"from": "access", "to": "review", "label": "escalation"}]


def test_room_instructions_and_kickoff() -> None:
    access = res("access")
    assert access["instructions"].startswith(
        "This room grants temporary AWS access through LeaseDesk. Ask @{scribe.name} "
    )
    assert "follow-ups are off" in access["instructions"]
    assert "@keymaster freeze" in res("review")["instructions"]
    assert access["kickoff"] == (
        "@{scribe.name} take the drafter role in this room, then introduce yourself in two "
        "lines. @{skeptic.name} take the reviewer role in this room, then introduce yourself "
        "in one line."
    )
    assert "kickoff" not in res("review")
    (how,) = access["docs"]
    assert how["name"] == "how-to-ask"
    assert set(how) == {"name", "description", "instructions", "content"}
    assert "@keymaster confirm" in how["content"] and "Example:" in how["content"]


def test_every_param_used_and_every_placeholder_declared() -> None:
    d = doc()
    declared = set(d["params"])
    texts = strings(d["resources"])
    used = {m for t in texts for m in PLACEHOLDER.findall(t)}
    assert used - declared - BUILTINS == set()
    assert declared - used == set()
    refs = {m for t in texts for m in NAME_REF.findall(t)}
    assert refs <= {"scribe", "skeptic"}


def test_no_packages_key() -> None:
    # switch-core 0.31.0 rooms have no packages field and would drop it silently (DECISIONS).
    assert "packages" not in keys(doc())
