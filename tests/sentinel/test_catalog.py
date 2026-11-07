# ruff: noqa: E501 - the sample table is one row per line, as people write it
"""resource-catalog as a Markdown table (Part 4), the old YAML block still accepted."""

from __future__ import annotations

from pathlib import Path

import pytest

from leasedesk.common.policy import parse_policy_text
from leasedesk.sentinel.catalog import CatalogError, parse_catalog_text

DOCS = Path(__file__).resolve().parents[2] / "switch" / "docs"

TABLE = """# resource-catalog

Some words for people.

| ARN | Owner | Sensitivity | Typical access | Sensitive actions |
| --- | --- | --- | --- | --- |
| `arn:aws:s3:::leasedesk-demo-billing-mock` | billing team | internal | read exports to debug reports | |
| `arn:aws:s3:::leasedesk-demo-prod-secrets-mock` | security | crown-jewel | none; decoy | s3:GetObject, s3:PutObject, s3:DeleteObject |
| `arn:aws:logs:us-east-1:123456789012:log-group:/leasedesk/app` | platform team | internal | read app logs | |

More words.
"""

YAML = """intro
```yaml
version: 1
resources:
  - arn: "arn:aws:s3:::leasedesk-demo-billing-*"
    owner: "billing team"
    sensitivity: internal
```
"""


def test_table_catalog_parses() -> None:
    cat = parse_catalog_text(TABLE)
    billing, secrets, logs = cat.resources
    assert (billing.arn, billing.owner, billing.sensitivity) == (
        "arn:aws:s3:::leasedesk-demo-billing-mock",
        "billing team",
        "internal",
    )
    assert billing.note == "read exports to debug reports"
    assert billing.sensitive_actions == []
    assert secrets.sensitivity == "crown-jewel"
    assert secrets.sensitive_actions == ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    assert logs.arn.endswith(":log-group:/leasedesk/app")
    assert [e.arn for e in cat.crown_jewels()] == [secrets.arn]


def test_yaml_catalog_still_parses() -> None:
    (entry,) = parse_catalog_text(YAML).resources
    assert entry.arn == "arn:aws:s3:::leasedesk-demo-billing-*"


def test_table_with_unknown_sensitivity_rejected() -> None:
    bad = TABLE.replace("| crown-jewel |", "| top-secret |")
    with pytest.raises(CatalogError, match="sensitivity"):
        parse_catalog_text(bad)


def test_table_without_needed_columns_rejected() -> None:
    with pytest.raises(CatalogError):
        parse_catalog_text("| Name | Owner |\n| - | - |\n| x | y |\n")


def test_repo_catalog_template_parses() -> None:
    text = (DOCS / "resource-catalog.md").read_text().replace("<ACCOUNT_ID>", "123456789012")
    cat = parse_catalog_text(text)
    assert [e.sensitivity for e in cat.resources] == ["internal", "crown-jewel", "internal"]
    assert cat.resources[2].arn.endswith(":log-group:/leasedesk/app")


def test_repo_policy_template_parses_with_one_high_risk_approval() -> None:
    text = (DOCS / "access-policy.yaml").read_text().replace("<ACCOUNT_ID>", "123456789012")
    policy = parse_policy_text(text)
    assert policy.high_risk.approvals == 1
    assert "production uses 2" in text


@pytest.mark.parametrize(
    "table",
    [
        "| ARN | Sensitivity |\n| arn:aws:s3:::secret | crown-jewel |",
        "| ARN | Sensitivity |\n| --- |\n| arn:aws:s3:::secret | crown-jewel |",
        "| ARN | Sensitivity |\n| --- | --- |\n| arn:aws:s3:::secret | crown-jewel | extra |",
    ],
)
def test_malformed_table_rejected_instead_of_losing_protections(table: str) -> None:
    with pytest.raises(CatalogError):
        parse_catalog_text(table)
