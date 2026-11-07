"""IAM action data from parliament 1.6.4: expand wildcards and look up AWS access levels.

parliament bundles AWS's service authorization data (``iam_definition``, 455 services). Its
import warns about pkg_resources and old regex escapes; those are silenced here only.
Everything is lower-cased for matching and mapped back to AWS's own spelling for display.
"""

from __future__ import annotations

import fnmatch
import warnings
from functools import cache

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import parliament

LEVEL_RANK = {"Permissions management": 0, "Write": 1, "Tagging": 2, "Read": 3, "List": 3}


def _index() -> dict[str, dict[str, tuple[str, str]]]:
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for service in parliament.iam_definition:
        prefix = service["prefix"].lower()
        privs = out.setdefault(prefix, {})
        for p in service["privileges"]:
            name = p["privilege"]
            privs[name.lower()] = (f"{service['prefix']}:{name}", p["access_level"])
    return out


INDEX = _index()


def _split(action: str) -> tuple[str, str]:
    if action == "*":
        return "*", "*"
    service, _, name = action.partition(":")
    return service.lower(), name.lower()


def known_service(action: str) -> bool:
    service, _ = _split(action)
    return service == "*" or service in INDEX


@cache
def expand(pattern: str) -> frozenset[str]:
    """Lower-cased ``svc:action`` names a pattern (``s3:Get*``, ``*``, exact) covers."""
    service, name = _split(pattern)
    services = INDEX if service == "*" else {service: INDEX.get(service, {})}
    out: set[str] = set()
    for svc, privs in services.items():
        if any(c in name for c in "*?"):
            out |= {f"{svc}:{a}" for a in privs if fnmatch.fnmatchcase(a, name)}
        elif name in privs:
            out.add(f"{svc}:{name}")
    return frozenset(out)


def is_wildcard(action: str) -> bool:
    return "*" in action or "?" in action


def display(action: str) -> str:
    """AWS's spelling of a known action (``s3:getobject`` -> ``s3:GetObject``)."""
    service, name = _split(action)
    hit = INDEX.get(service, {}).get(name)
    return hit[0] if hit else action


def access_level(action: str) -> str | None:
    service, name = _split(action)
    hit = INDEX.get(service, {}).get(name)
    return hit[1] if hit else None


def service_actions(service: str) -> list[str]:
    return [v[0] for v in INDEX.get(service.lower(), {}).values()]


def is_write(action: str) -> bool:
    level = access_level(action)
    return level in ("Write", "Permissions management", "Tagging")
