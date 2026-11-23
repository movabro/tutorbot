"""Tag extraction. Tags are looked up in content only (never note)."""
from __future__ import annotations

import re
import unicodedata

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


def node_tag_set(content: str | None, note: str | None) -> frozenset[str]:
    """Extract normalized tags from both content and note."""
    return frozenset(
        unicodedata.normalize("NFC", tag).casefold()
        for text in (content, note) for tag in TAG_RE.findall(text or "")
    )


def tags_match(tag_set: frozenset[str], wanted: frozenset[str]) -> bool:
    normalized = {unicodedata.normalize("NFC", tag).casefold() for tag in wanted}
    return not normalized or not tag_set.isdisjoint(normalized)
