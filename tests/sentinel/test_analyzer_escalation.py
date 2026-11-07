"""escalation: known privilege-escalation combinations, from the published sources."""

from __future__ import annotations

from leasedesk.sentinel.analyzers import escalation
from leasedesk.sentinel.context import ReviewContext

from .conftest import BUCKET, rules, stmt, view

FN = "arn:aws:lambda:us-east-1:123456789012:function:leasedesk-demo"
ROLE = "arn:aws:iam::123456789012:role/leasedesk-demo"


def test_data_has_every_path_the_brief_names() -> None:
    have = {frozenset(a.lower() for a in p.requires) for p in escalation.PATHS}
    for need in [
        {"iam:createpolicyversion"},
        {"iam:passrole", "lambda:createfunction", "lambda:invokefunction"},
        {"iam:passrole", "ecs:registertaskdefinition", "ecs:runtask"},
        {"iam:passrole", "codebuild:createproject", "codebuild:startbuild"},
        {"iam:passrole", "sagemaker:createnotebookinstance"},
        {"ssm:sendcommand"},
        {"ssm:startsession"},
        {"iam:attachrolepolicy", "sts:assumerole"},
    ]:
        assert frozenset(need) in have, need
    assert len(escalation.PATHS) >= 27
    assert all(p.reference.startswith("https://") for p in escalation.PATHS)


def test_full_combination_blocks(ctx: ReviewContext) -> None:
    p = view(
        stmt("Pass", ["iam:PassRole"], [ROLE]),
        stmt("Fn", ["lambda:CreateFunction", "lambda:InvokeFunction"], [FN]),
    )
    found = escalation.analyze(p, ctx)
    block = [f for f in found if f.severity == "BLOCK"]
    assert [f.rule for f in block] == ["escalation-rhino-15-passrole-lambda-invoke"]
    assert block[0].fix is not None and "iam:PassRole" in block[0].fix
    assert "Rhino Security Labs, method 15" in block[0].detail
    assert (block[0].reference or "").startswith("https://hackingthe.cloud/")


def test_single_action_path_blocks(ctx: ReviewContext) -> None:
    found = escalation.analyze(view(stmt("V", ["iam:CreatePolicyVersion"], [ROLE])), ctx)
    assert "escalation-rhino-1-create-policy-version" in rules(found)


def test_wildcards_are_expanded(ctx: ReviewContext) -> None:
    p = view(stmt("Pass", ["iam:PassRole"], [ROLE]), stmt("Fn", ["lambda:*"], [FN]))
    assert any(f.severity == "BLOCK" for f in escalation.analyze(p, ctx))


def test_one_action_short_is_a_single_medium(ctx: ReviewContext) -> None:
    found = escalation.analyze(view(stmt("Pass", ["iam:PassRole"], [ROLE])), ctx)
    assert [f.rule for f in found] == ["escalation-near-miss"]
    f = found[0]
    assert f.severity == "MEDIUM" and f.fix is not None
    assert "ec2:RunInstances" in f.detail and "cloudformation:CreateStack" in f.detail


def test_plain_reads_are_clean(ctx: ReviewContext) -> None:
    assert escalation.analyze(view(stmt("R", ["s3:GetObject"], [f"{BUCKET}/a/*"])), ctx) == []
