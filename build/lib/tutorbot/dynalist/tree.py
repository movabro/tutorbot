"""In-memory index over a flat Dynalist doc/read node list."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

ROOT_ID = "root"


@dataclass
class Node:
    id: str
    content: str = ""
    note: str = ""
    created: int = 0
    modified: int = 0
    children: list[str] = field(default_factory=list)
    checked: bool = False
    checkbox: bool = False
    collapsed: bool = False
    heading: int = 0
    color: int = 0

    @classmethod
    def from_api(cls, d: dict) -> "Node":
        return cls(
            id=d["id"],
            content=d.get("content") or "",
            note=d.get("note") or "",
            created=int(d.get("created") or 0),
            modified=int(d.get("modified") or 0),
            children=list(d.get("children") or []),
            checked=bool(d.get("checked", False)),
            checkbox=bool(d.get("checkbox", False)),
            collapsed=bool(d.get("collapsed", False)),
            heading=int(d.get("heading") or 0),
            color=int(d.get("color") or 0),
        )


class NodeIndex:
    def __init__(self, file_id: str, nodes: list[Node], version: int | None = None):
        self.file_id = file_id
        self.version = version
        self.by_id: dict[str, Node] = {n.id: n for n in nodes}
        self.parent_of: dict[str, str] = {}
        for n in nodes:
            for c in n.children:
                self.parent_of[c] = n.id

    @classmethod
    def from_doc_read(cls, file_id: str, resp: dict) -> "NodeIndex":
        return cls(file_id, [Node.from_api(d) for d in resp.get("nodes", [])], resp.get("version"))

    def get(self, node_id: str) -> Node | None:
        return self.by_id.get(node_id)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.by_id

    def children_of(self, node_id: str) -> list[Node]:
        n = self.by_id.get(node_id)
        if not n:
            return []
        return [self.by_id[c] for c in n.children if c in self.by_id]

    def ancestors(self, node_id: str) -> Iterator[Node]:
        """Nearest ancestor first, excluding the synthetic root."""
        cur = self.parent_of.get(node_id)
        while cur is not None and cur != ROOT_ID:
            n = self.by_id.get(cur)
            if n is None:
                break
            yield n
            cur = self.parent_of.get(cur)

    def iter_subtree(self, node_id: str, depth: int = 0) -> Iterator[tuple[Node, int]]:
        """Pre-order (node, depth) in child order."""
        n = self.by_id.get(node_id)
        if n is None:
            return
        yield n, depth
        for c in n.children:
            yield from self.iter_subtree(c, depth + 1)

    def walk(self) -> Iterator[Node]:
        """All nodes under the root, document order (root excluded)."""
        for c in (self.by_id[ROOT_ID].children if ROOT_ID in self.by_id else []):
            for n, _ in self.iter_subtree(c):
                yield n

    def text_of(self, node_id: str, skip: Callable[[Node], bool] | None = None) -> str:
        """Indented plain text of a subtree (content + note), skipping subtrees where skip() is true."""
        lines: list[str] = []

        def rec(nid: str, depth: int) -> None:
            n = self.by_id.get(nid)
            if n is None or (skip and skip(n)):
                return
            if n.content:
                lines.append("  " * depth + n.content)
            if n.note:
                lines.extend("  " * depth + "  > " + ln for ln in n.note.splitlines())
            for c in n.children:
                rec(c, depth + 1)

        rec(node_id, 0)
        return "\n".join(lines)

    def max_modified(self, node_id: str, skip: Callable[[Node], bool] | None = None) -> int:
        best = 0

        def rec(nid: str) -> None:
            nonlocal best
            n = self.by_id.get(nid)
            if n is None or (skip and skip(n)):
                return
            best = max(best, n.modified)
            for c in n.children:
                rec(c)

        rec(node_id)
        return best
