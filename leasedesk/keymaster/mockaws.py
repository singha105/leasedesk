"""Mock AWS for LeaseDesk: a long-running moto server seeded like the real sandbox.

    uv run python -m leasedesk.keymaster.mockaws        # or: make moto

Why a server and not in-process ``mock_aws``: ``scripts/demo/check_access.sh`` runs the AWS CLI
in another process, and a keymaster restart must not wipe IAM state. The server seeds the same
users, boundary and buckets as scripts/aws/setup_sandbox.sh with auth off, writes the mock keys
to .state/mock-aws.env (mode 600), then turns moto's IAM auth on so lease-demo-dev is really
denied until keymaster writes a policy. moto ignores aws:CurrentTime, so in mock mode access
ends when keymaster deletes the policy at expiry; ``simulate`` checks the time window itself.
"""

from __future__ import annotations

import json
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import boto3
import moto.settings
from moto.server import ThreadedMotoServer

from leasedesk.common.envfile import set_env_value
from leasedesk.config import Settings

ACCOUNT = "123456789012"
MOTO_ADMIN = "testing"  # moto's conventional fake credential, used only before auth is on
POLICIES = Path("scripts/aws/policies")
DEMO_USER = "lease-demo-dev"


@dataclass
class MockSandbox:
    account_id: str
    bucket: str
    decoy: str
    boundary_doc: dict[str, Any]
    keys: dict[str, tuple[str, str]]


def _policy(name: str, account_id: str) -> str:
    return (POLICIES / name).read_text().replace("<ACCOUNT_ID>", account_id)


def seed(
    session: boto3.Session, *, endpoint_url: str | None = None, suffix: str = "mock"
) -> MockSandbox:
    iam = session.client("iam", endpoint_url=endpoint_url)
    s3 = session.client("s3", endpoint_url=endpoint_url)
    boundary_text = _policy("demo-boundary.json", ACCOUNT)
    boundary = iam.create_policy(
        PolicyName="LeaseDeskDemoBoundary", Path="/leasedesk-system/", PolicyDocument=boundary_text
    )["Policy"]["Arn"]
    # moto authorizes IAM calls against Resource "*", so the mock keymaster gets its IAM
    # actions on "*". Real AWS keeps the /leasedesk/* scoping (docs/DECISIONS.md).
    km = _policy("keymaster-policy.json", ACCOUNT).replace(
        f"arn:aws:iam::{ACCOUNT}:user/leasedesk/*", "*"
    )
    users = {
        "keymaster": ("leasedesk-keymaster", "/leasedesk-system/", km),
        "sentinel": (
            "leasedesk-sentinel",
            "/leasedesk-system/",
            _policy("sentinel-policy.json", ACCOUNT),
        ),
    }
    keys: dict[str, tuple[str, str]] = {}
    for role, (user, path, doc) in users.items():
        iam.create_user(UserName=user, Path=path)
        iam.put_user_policy(
            UserName=user, PolicyName=f"LeaseDesk{role.title()}", PolicyDocument=doc
        )
        k = iam.create_access_key(UserName=user)["AccessKey"]
        keys[role] = (k["AccessKeyId"], k["SecretAccessKey"])
    iam.create_user(UserName=DEMO_USER, Path="/leasedesk/", PermissionsBoundary=boundary)
    k = iam.create_access_key(UserName=DEMO_USER)["AccessKey"]
    keys["demo"] = (k["AccessKeyId"], k["SecretAccessKey"])
    bucket, decoy = f"leasedesk-demo-billing-{suffix}", f"leasedesk-demo-prod-secrets-{suffix}"
    for b in (bucket, decoy):
        s3.create_bucket(Bucket=b)
    s3.put_object(Bucket=bucket, Key="exports/2026-10/invoices.csv", Body=b"invoice,total\n1,42\n")
    s3.put_object(Bucket=decoy, Key="db-password.txt", Body=b"decoy - not a real secret\n")
    return MockSandbox(ACCOUNT, bucket, decoy, json.loads(boundary_text), keys)


class MockAwsServer:
    """Start moto, seed it, turn IAM auth on. Restores moto's setting on exit."""

    def __init__(self, port: int = 5055, host: str = "127.0.0.1") -> None:
        self._server = ThreadedMotoServer(ip_address=host, port=port, verbose=False)
        self.url = ""
        self.sandbox: MockSandbox | None = None
        self._saved = moto.settings.INITIAL_NO_AUTH_ACTION_COUNT

    def __enter__(self) -> MockAwsServer:
        self._server.start()
        host, port = self._server.get_host_and_port()
        self.url = f"http://{host}:{port}"
        admin = boto3.Session(
            aws_access_key_id=MOTO_ADMIN, aws_secret_access_key=MOTO_ADMIN, region_name="us-east-1"
        )
        self.sandbox = seed(admin, endpoint_url=self.url)
        moto.settings.INITIAL_NO_AUTH_ACTION_COUNT = 0
        return self

    def __exit__(
        self,
        _t: type[BaseException] | None,
        _e: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        moto.settings.INITIAL_NO_AUTH_ACTION_COUNT = self._saved
        self._server.stop()

    def session(self, role: str) -> boto3.Session:
        assert self.sandbox is not None  # noqa: S101
        key_id, secret = self.sandbox.keys[role]
        return boto3.Session(
            aws_access_key_id=key_id, aws_secret_access_key=secret, region_name="us-east-1"
        )

    def write_env(self, path: Path) -> None:
        assert self.sandbox is not None  # noqa: S101
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        values = {
            "LEASEDESK_MOCK_ENDPOINT": self.url,
            "LEASEDESK_MOCK_ACCOUNT": self.sandbox.account_id,
            "LEASEDESK_MOCK_BUCKET": self.sandbox.bucket,
            "KEYMASTER_AWS_ACCESS_KEY_ID": self.sandbox.keys["keymaster"][0],
            "KEYMASTER_AWS_SECRET_ACCESS_KEY": self.sandbox.keys["keymaster"][1],
            "SENTINEL_AWS_ACCESS_KEY_ID": self.sandbox.keys["sentinel"][0],
            "SENTINEL_AWS_SECRET_ACCESS_KEY": self.sandbox.keys["sentinel"][1],
            "DEMO_AWS_ACCESS_KEY_ID": self.sandbox.keys["demo"][0],
            "DEMO_AWS_SECRET_ACCESS_KEY": self.sandbox.keys["demo"][1],
        }
        path.write_text("")
        for k, v in values.items():
            set_env_value(path, k, v)


def main() -> None:
    settings = Settings()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with MockAwsServer(port=settings.moto_port) as server:
        server.write_env(settings.state_dir / "mock-aws.env")
        assert server.sandbox is not None  # noqa: S101
        print(
            f"mock AWS ready at {server.url} (bucket {server.sandbox.bucket}); "
            "mock keys in .state/mock-aws.env. IAM auth is ON. Ctrl-C to stop.",
            flush=True,
        )
        stop.wait()
    print("mock AWS stopped (all mock state is gone).")


if __name__ == "__main__":
    main()
