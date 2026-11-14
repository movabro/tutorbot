"""Entry date resolution (spec D3): self -> nearest dated ancestor -> created."""
from __future__ import annotations

import re
from datetime import date

from ..clock import ms_to_local_date
from ..dynalist.tree import Node, NodeIndex

# Dynalist date syntax: !(YYYY-MM-DD) optionally followed by time
BANG_RE = re.compile(r"!\(\s*(\d{4})-(\d{2})-(\d{2})")
LEAD_RE = re.compile(r"^\s*(\d{4})[-./](\d{2})[-./](\d{2})(?!\d)")


def _mk(y: str, m: str, d: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def bang_date(text: str | None) -> date | None:
    m = BANG_RE.search(text or "")
    return _mk(*m.groups()) if m else None


def own_date(node: Node) -> date | None:
    return bang_date(node.content) or bang_date(node.note)


def ancestor_node_date(node: Node) -> date | None:
    d = bang_date(node.content) or bang_date(node.note)
    if d:
        return d
    m = LEAD_RE.match(node.content or "")
    return _mk(*m.groups()) if m else None


def resolve_entry_date(index: NodeIndex, node: Node, tz_name: str) -> tuple[date, str]:
    """Returns (date, source) with source in self|ancestor|created."""
    d = own_date(node)
    if d:
        return d, "self"
    for anc in index.ancestors(node.id):
        d = ancestor_node_date(anc)
        if d:
            return d, "ancestor"
    return ms_to_local_date(node.created, tz_name), "created"
