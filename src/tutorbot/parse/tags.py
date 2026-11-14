"""Tag extraction. Tags are looked up in content only (never note)."""
from __future__ import annotations

import re

TAG_RE = re.compile(r"(?<![\w#])#([^\s#!@()\[\],.;:?]+)")


def extract_tags(content: str | None) -> list[str]:
    """Tag names without '#', original case, in order of appearance."""
    return TAG_RE.findall(content or "")


def has_tag(content: str | None, names: list[str] | tuple[str, ...] | str) -> bool:
    if isinstance(names, str):
        names = [names]
    wanted = {n.lstrip("#").casefold() for n in names}
    return any(t.casefold() in wanted for t in extract_tags(content))


def strip_tags(content: str | None) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", content or "")).strip()
