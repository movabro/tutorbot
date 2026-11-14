"""Subtree content hash (spec §4)."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Callable

from ..dynalist.tree import Node, NodeIndex

HASH_PREFIX = "v1:"


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFC", s or "")
    lines = []
    for line in s.splitlines():
        line = re.sub(r"[ \t]+", " ", line.strip())
        if line:
            lines.append(line)
    return "\n".join(lines)


def serialize(index: NodeIndex, node_id: str, is_bot: Callable[[Node], bool], depth: int = 0) -> str:
    node = index.get(node_id)
    if node is None or is_bot(node):
        return ""
    out = [f"{depth}\x1f{norm(node.content)}\x1f{norm(node.note)}\x1e"]
    for c in node.children:
        out.append(serialize(index, c, is_bot, depth + 1))
    return "".join(out)


def content_hash(index: NodeIndex, node_id: str, is_bot: Callable[[Node], bool]) -> str:
    return HASH_PREFIX + hashlib.sha256(serialize(index, node_id, is_bot).encode("utf-8")).hexdigest()


def hash_version(h: str | None) -> str | None:
    return h.split(":", 1)[0] if h and ":" in h else None
