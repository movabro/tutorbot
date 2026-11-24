"""Safe loading and deterministic normalization of persona files."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .models import GateResult, PersonaResult, TaskSpec


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


def approval_gate(conn, task: TaskSpec, persona: PersonaResult, *, interactive: bool,
                  dry_run: bool) -> GateResult:
    """Check hash-bound authorization without consuming a one-use approval."""
    if persona.error:
        return GateResult(False, "persona_error")
    if persona.path is None:
        return GateResult(True, "no_persona")
    if task.persona_approval == "always_allowed":
        return GateResult(True, "always_allowed")
    if task.persona_approval not in ("user_approved_before_use", "approve_each_run"):
        return GateResult(False, "awaiting_approval")

    from .. import clock
    from . import store

    kind = "standing" if task.persona_approval == "user_approved_before_use" else "once"
    approval = store.find_approval(conn, task.id, persona.hash, kind)
    if approval:
        return GateResult(True, kind, approval[0] if kind == "once" and not dry_run else None)
    if dry_run:
        return GateResult(True, "preview_unapproved", preview=True)
    if interactive:
        print(f"Persona: {persona.path}\nHash: {persona.hash}\n"
              f"Lines: {persona.line_count}\n" + "\n".join((persona.text or "").splitlines()[:40]))
        try:
            answer = input("Approve persona? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() == "y":
            approval_id = store.grant_approval(conn, task.id, persona.path, persona.hash,
                                               kind, clock.now_iso())
            return GateResult(True, kind, approval_id if kind == "once" else None)
    return GateResult(False, "awaiting_approval")
