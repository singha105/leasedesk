"""Edit KEY=value lines in a .env file without disturbing the rest. The file stays mode 600."""

from __future__ import annotations

from pathlib import Path


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def set_env_value(path: Path, name: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    out: list[str] = []
    replaced = False
    for ln in lines:
        if ln.startswith(f"{name}="):
            if not replaced:
                out.append(f"{name}={value}")
                replaced = True
            continue
        out.append(ln)
    if not replaced:
        out.append(f"{name}={value}")
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)
