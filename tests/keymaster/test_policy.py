from __future__ import annotations

import yaml

from leasedesk.keymaster.policy import PolicyLoader, parse_policy_text

from .conftest import policy_dict

GOOD = yaml.safe_dump(policy_dict())


def test_plain_yaml() -> None:
    assert parse_policy_text(GOOD).max_duration_minutes == 240


def test_yaml_inside_fence() -> None:
    text = f"# access-policy\nRules below.\n```yaml\n{GOOD}```\nThanks."
    assert parse_policy_text(text).high_risk.approvals == 2


async def test_policy_parse_error_keeps_last_good() -> None:
    texts = [GOOD, "version: 1\napprovers: [oops", yaml.safe_dump(policy_dict(solo_mode=True))]

    async def fetch() -> str:
        return texts.pop(0)

    loader = PolicyLoader(fetch)
    first = await loader.load()
    assert first.policy is not None and first.changed and first.error is None
    second = await loader.load()
    assert second.error is not None and not second.changed
    assert second.policy is first.policy
    assert loader.current is first.policy
    third = await loader.load()
    assert third.changed and third.policy is not None and third.policy.solo_mode


async def test_unchanged_reload_is_not_a_change() -> None:
    async def fetch() -> str:
        return GOOD

    loader = PolicyLoader(fetch)
    await loader.load()
    again = await loader.load()
    assert again.changed is False and again.error is None


async def test_first_load_failure_has_no_policy() -> None:
    async def fetch() -> str:
        raise FileNotFoundError("access-policy document not found")

    result = await PolicyLoader(fetch).load()
    assert result.policy is None and "not found" in (result.error or "")
