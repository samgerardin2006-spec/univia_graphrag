"""Load local configuration without executing shell commands."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from .schema import ValidationError


def load_env(path=".env"):
    """Load simple KEY=value lines; existing environment variables take priority."""
    path = Path(path)
    if not path.is_file():
        return
    values = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValidationError(f"Invalid .env assignment on line {line_number}")
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise ValidationError(f"Invalid .env quoting on line {line_number}") from exc
        if len(parts) > 1:
            raise ValidationError(f"Quote values containing spaces in .env on line {line_number}")
        values[name] = parts[0] if parts else ""
    for name, value in values.items():
        os.environ.setdefault(name, value)
