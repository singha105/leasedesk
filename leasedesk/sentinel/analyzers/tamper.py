"""tamper_and_exposure: actions that blind security or expose data are always BLOCK when a
lease names them. A wildcard that covers one of them is HIGH and names what it covers, so a
security lead decides (``s3:*`` included; see DECISIONS 2026-11-06)."""

from __future__ import annotations

from leasedesk.common.globs import glob_intersects
from leasedesk.common.schemas import Finding
from leasedesk.sentinel.actions import is_wildcard
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.findings import make, register
from leasedesk.sentinel.model import ProposalView

NAME = "tamper_and_exposure"
LOGGING = "https://attack.mitre.org/techniques/T1562/008/"
PUBLIC = (
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html"
)
KMS = "https://docs.aws.amazon.com/kms/latest/developerguide/deleting-keys.html"
TAMPER: dict[str, tuple[str, str]] = {
    "cloudtrail:StopLogging": ("stops CloudTrail logging", LOGGING),
    "cloudtrail:DeleteTrail": ("deletes a CloudTrail trail", LOGGING),
    "config:StopConfigurationRecorder": ("stops AWS Config recording", LOGGING),
    "guardduty:DeleteDetector": ("turns GuardDuty off", LOGGING),
    "s3:PutBucketPolicy": ("can make a bucket public", PUBLIC),
    "s3:PutBucketAcl": ("can make a bucket public", PUBLIC),
    "s3:PutObjectAcl": ("can make objects public", PUBLIC),
    "s3:DeletePublicAccessBlock": ("removes S3 Block Public Access", PUBLIC),
    "s3:PutBucketPublicAccessBlock": ("can switch off S3 Block Public Access", PUBLIC),
    "s3:PutAccountPublicAccessBlock": ("can switch off S3 Block Public Access", PUBLIC),
    "kms:ScheduleKeyDeletion": ("schedules a KMS key for deletion", KMS),
}
_LOWER = {k.lower(): k for k in TAMPER}
register("tamper-exposure", "BLOCK", "Blinds security tooling or exposes data", LOGGING, NAME)
register(
    "wildcard-covers-tamper",
    "HIGH",
    "A wildcard that includes tamper or exposure actions",
    LOGGING,
    NAME,
)


def analyze(p: ProposalView, ctx: ReviewContext) -> list[Finding]:
    out: list[Finding] = []
    for s in p.statements:
        for action in s.actions:
            name = _LOWER.get(action.lower())
            if name is not None:
                what, ref = TAMPER[name]
                out.append(
                    make(
                        "tamper-exposure",
                        s.sid,
                        f"{name} {what}.",
                        f"Drop {name}; that change needs a change request, not a lease.",
                        reference=ref,
                    )
                )
            elif is_wildcard(action):
                covered = [t for t in TAMPER if glob_intersects(action, t)]
                if covered:
                    out.append(
                        make(
                            "wildcard-covers-tamper",
                            s.sid,
                            f"{action} includes {', '.join(covered[:4])}.",
                            f"Name the actions you need instead of {action}.",
                        )
                    )
    return out
