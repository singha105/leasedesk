"""Read `aws iam create-access-key` JSON on stdin and store it as a named profile.

Writes ~/.aws/credentials and ~/.aws/config (both mode 600). Prints the profile name only,
never the key id or secret. Also supports --remove to delete a profile from both files.
"""

from __future__ import annotations

import configparser
import json
import sys
from pathlib import Path

AWS_DIR = Path.home() / ".aws"
CREDS = AWS_DIR / "credentials"
CONFIG = AWS_DIR / "config"


def _load(path: Path) -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser()
    if path.exists():
        cp.read(path)
    return cp


def _save(cp: configparser.RawConfigParser, path: Path) -> None:
    AWS_DIR.mkdir(mode=0o700, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    with path.open("w") as fh:
        cp.write(fh)
    path.chmod(0o600)


def write(profile: str, region: str) -> None:
    key = json.load(sys.stdin)["AccessKey"]
    creds = _load(CREDS)
    if not creds.has_section(profile):
        creds.add_section(profile)
    creds.set(profile, "aws_access_key_id", key["AccessKeyId"])
    creds.set(profile, "aws_secret_access_key", key["SecretAccessKey"])
    _save(creds, CREDS)

    config = _load(CONFIG)
    section = f"profile {profile}"
    if not config.has_section(section):
        config.add_section(section)
    config.set(section, "region", region)
    config.set(section, "output", "json")
    _save(config, CONFIG)
    print(f"    wrote profile {profile} to ~/.aws/credentials (mode 600)")


def remove(profile: str) -> None:
    for path, section in ((CREDS, profile), (CONFIG, f"profile {profile}")):
        cp = _load(path)
        if cp.remove_section(section):
            _save(cp, path)
    print(f"    removed profile {profile} from ~/.aws")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--remove":
        remove(sys.argv[2])
    elif len(sys.argv) == 3:
        write(sys.argv[1], sys.argv[2])
    else:
        sys.exit("usage: write_profile.py <profile> <region> < key.json | --remove <profile>")
