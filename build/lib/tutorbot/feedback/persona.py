"""Safe loading and deterministic normalization of persona files."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .models import PersonaResult, TaskSpec


def resolve_persona(task: TaskSpec) -> PersonaResult:
    path = task.persona_path
    if path is None:
        return PersonaResult(None, None, None, 0, 0, None)

    def failure(reason: str) -> PersonaResult:
        return PersonaResult(path, None, None, 0, 0, reason)

    if (not isinstance(path, str) or not path or path.startswith(("~", ".."))
            or "\\" in path or "\0" in path or Path(path).is_absolute()
            or Path(path).suffix not in (".md", ".txt")):
        return failure("outside_base_dir")
    try:
        base = task.base_dir.resolve()
        candidate = (base / path).resolve()
        if not candidate.is_relative_to(base):
            return failure("outside_base_dir")
        if not candidate.exists():
            return failure("not_found")
        if not candidate.is_file():
            return failure("not_found")
        # A bounded read avoids loading arbitrarily large files, including those
        # that grow after a stat call.
        with candidate.open("rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            return failure("too_large")
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return failure("not_utf8")
    except (OSError, RuntimeError):
        return failure("not_found")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    text = "\n".join(lines) + "\n"
    digest = "v1:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return PersonaResult(path, text, digest, len(data), len(lines), None)
