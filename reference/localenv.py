"""Loads the project's .env so the scripts behave the same in PowerShell, cmd and bash.

PowerShell has no `source`, and cmd has neither, so telling everyone to export variables by
hand is a per-shell instruction that goes wrong. The scripts read the file directly instead.
Real environment variables still win, so `$env:X=...` or `export X=...` overrides the file.

No dependency: the format here is one `KEY=value` per line, `export` and surrounding quotes
optional, `#` comments and blank lines skipped.
"""
from __future__ import annotations

import os
import pathlib

DEFAULT_PATH = pathlib.Path(__file__).resolve().parents[1] / ".env"


def load(path: pathlib.Path | str | None = None) -> dict[str, str]:
    """Copy values from a .env file into os.environ. Returns what it set."""
    path = pathlib.Path(path) if path else DEFAULT_PATH
    if not path.is_file():
        return {}
    applied: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in os.environ:  # a real environment variable beats the file
            os.environ[key] = value
            applied[key] = value
    return applied
