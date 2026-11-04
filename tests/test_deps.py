"""Guards against dependency breakage (parliament needs pkg_resources from setuptools<81)."""

from __future__ import annotations

import json
import warnings


def test_parliament_imports_and_flags_resource_star() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from parliament import analyze_policy_string

    policy = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}],
    }
    issues = [f.issue for f in analyze_policy_string(json.dumps(policy)).findings]
    assert "RESOURCE_STAR" in issues
