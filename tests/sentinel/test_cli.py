"""python -m leasedesk.sentinel.cli review FILE: the summary and the verdict block, no Switch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from leasedesk.common.fences import extract_blocks
from leasedesk.common.schemas import LeaseVerdict
from leasedesk.sentinel.cli import main

from ..keymaster.conftest import policy_dict
from .conftest import BUCKET, CATALOG, raw, stmt


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    pfile, cfile = tmp_path / "policy.yaml", tmp_path / "catalog.md"
    pfile.write_text(yaml.safe_dump(policy_dict()))
    cfile.write_text("```yaml\n" + yaml.safe_dump(CATALOG) + "```\n")
    return pfile, cfile


def run(tmp: Path, proposal: object, files: tuple[Path, Path]) -> int:
    path = tmp / "proposal.json"
    path.write_text(json.dumps(proposal))
    return main(["review", str(path), "--policy", str(files[0]), "--catalog", str(files[1])])


def test_review_prints_summary_and_verdict(
    tmp_path: Path, files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, raw(stmt("All", ["s3:*"], [BUCKET])), files) == 0
    out = capsys.readouterr().out
    assert "verdict LS-0000 · risk HIGH" in out
    (block,) = [b for b in extract_blocks(out) if b.kind == "verdict"]
    v = LeaseVerdict.model_validate(block.data)
    assert v.lease_id == "LS-0000" and v.risk == "HIGH"
    assert "```lease" in out  # the corrected lease follows


def test_unreadable_policy_exits_2(
    tmp_path: Path, files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    files[0].write_text("version: [")
    assert run(tmp_path, raw(), files) == 2
    assert "policy" in capsys.readouterr().err.lower()


def test_not_json_exits_2(
    tmp_path: Path, files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "p.json"
    path.write_text("{nope")
    assert main(["review", str(path), "--policy", str(files[0]), "--catalog", str(files[1])]) == 2
