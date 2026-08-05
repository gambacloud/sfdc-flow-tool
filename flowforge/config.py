"""
Read credentials from a .env file.

An environment variable set in one shell does not survive into the next one, so
a key exported before running the tool is often simply not there by the time it
runs. A file in the repo makes the setting stick. It is gitignored.

Deliberately does not override variables already set in the environment - an
explicit export should always win over a file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

ENV_FILENAME = ".env"

# Keys the app looks for, in the order providers are auto-detected.
KNOWN_KEYS = [
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "SF_INSTANCE_URL",
    "SF_SESSION_ID",
]


# Invisible characters that Windows editors and PowerShell put at the start of a
# file. Left in place they become part of the variable's name, which then looks
# identical to the right name when printed and matches nothing.
_INVISIBLE = "﻿​‎‏"


def parse_env(text: str) -> Dict[str, str]:
    """
    Minimal KEY=value parsing. Blank lines and # comments are skipped, a leading
    `export` is tolerated, surrounding quotes are stripped so a value pasted with
    quotes still works, and a byte-order mark never ends up in a name.
    """
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().strip(_INVISIBLE).strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip().strip(_INVISIBLE).strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(directory: Path | str = ".") -> List[str]:
    """
    Load `.env` from `directory` into os.environ. Returns the names that were
    applied, so the caller can say where the credentials came from.
    """
    path = Path(directory) / ENV_FILENAME
    if not path.is_file():
        return []

    # utf-8-sig drops a byte-order mark if one is there and behaves like utf-8
    # if it is not. PowerShell 5.1 writes one by default.
    applied = []
    for key, value in parse_env(path.read_text(encoding="utf-8-sig")).items():
        if not value:
            continue  # an empty value would only shadow a real one
        if key not in os.environ:  # a real export wins
            os.environ[key] = value
            applied.append(key)
    return applied


def env_help(directory: Path | str = ".") -> str:
    """The message shown when no key is found anywhere."""
    path = (Path(directory) / ENV_FILENAME).resolve()
    return (
        "No LLM key found.\n\n"
        f"Create {path} with one line:\n\n"
        "    GEMINI_API_KEY=your-key\n"
        "    # or: ANTHROPIC_API_KEY=sk-ant-...\n\n"
        "A key exported with $env: only lasts for that one shell, which is why\n"
        "setting it in one command and running the tool in the next does nothing.\n"
        "The file is gitignored."
    )
