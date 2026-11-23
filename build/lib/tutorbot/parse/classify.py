"""Node classification: bot-owned vs user nodes, learning units, quiz answer areas (spec §3.3, §3.5)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Iterable

from ..dynalist.tree import Node, NodeIndex
from .hashing import content_hash, norm
from .tags import extract_tags, has_tag

if TYPE_CHECKING:
    from ..config import TagsCfg

log = logging.getLogger(__name__)

# Used when no config is given; keeps this module importable without pydantic.
DEFAULT_TAGS = SimpleNamespace(goal=["목표", "goal"], dump=["지식덤프", "dump"],
                               study=["공부", "study"], bot="tutorbot")

MARKER_PREFIX = "tb:"
GOAL, DUMP, STUDY = "goal", "dump", "study"


@dataclass
class Unit:
    kind: str                 # goal | dump | study
    node: Node
    content_hash: str
    tags: list[str]           # all tags in content, original case
    topic_tags: list[str]     # non-reserved tags, lowercased
    nested_ignored: list[str] = field(default_factory=list)  # node ids of ignored inner tagged nodes


class Classifier:
    """`registry` maps node_id -> bot kind for this file (from bot_node, deleted_at IS NULL)."""

    def __init__(self, index: NodeIndex, tags: "TagsCfg | None" = None,
                 registry: dict[str, str] | None = None):
        self.index = index
        self.tags = tags or DEFAULT_TAGS
        self.registry = registry or {}
        self._memo: dict[str, bool] = {}

    # --- bot ownership -----------------------------------------------------
    def is_direct_bot(self, node: Node) -> bool:
        return (
            node.id in self.registry
            or has_tag(node.content, self.tags.bot)
            or (node.note or "").lstrip().startswith(MARKER_PREFIX)
        )

    def is_bot(self, node: Node) -> bool:
        """Direct bot node, or descendant of a bot node (except the answer area of a quiz_item)."""
        hit = self._memo.get(node.id)
        if hit is not None:
            return hit
        if self.is_direct_bot(node):
            res = True
        else:
            pid = self.index.parent_of.get(node.id)
            parent = self.index.get(pid) if pid else None
            if parent is None or pid == "root":
                res = False
            elif self.registry.get(parent.id) == "quiz_item":
                res = False  # user answer area
            else:
                res = self.is_bot(parent)
        self._memo[node.id] = res
        return res

    # --- units -------------------------------------------------------------
    def unit_kind(self, node: Node) -> str | None:
        if has_tag(node.content, self.tags.goal):
            return GOAL
        if has_tag(node.content, self.tags.dump):
            return DUMP
        if has_tag(node.content, self.tags.study):
            return STUDY
        return None

    def reserved(self) -> set[str]:
        r = {t.casefold() for t in (*self.tags.goal, *self.tags.dump, *self.tags.study)}
        r.add(self.tags.bot.casefold())
        return r

    def find_units(self) -> list[Unit]:
        units: list[Unit] = []
        reserved = self.reserved()

        def visit(nid: str, current: Unit | None) -> None:
            node = self.index.get(nid)
            if node is None or self.is_bot(node):
                return
            if current is None:
                kind = self.unit_kind(node)
                if kind:
                    tags = extract_tags(node.content)
                    current = Unit(
                        kind=kind, node=node,
                        content_hash=content_hash(self.index, nid, self.is_bot),
                        tags=tags,
                        topic_tags=[t.casefold() for t in tags if t.casefold() not in reserved],
                    )
                    units.append(current)
            elif self.unit_kind(node):
                current.nested_ignored.append(nid)
                log.warning("nested unit tag ignored in node %s (inside %s)", nid, current.node.id)
            for c in node.children:
                visit(c, current)

        for c in (self.index.get("root").children if self.index.get("root") else []):
            visit(c, None)
        return units

    # --- quiz answers ------------------------------------------------------
    def answer_nodes(self, quiz_node: Node) -> list[Node]:
        return [c for c in self.index.children_of(quiz_node.id) if not self.is_direct_bot(c)]

    def answer_text(self, quiz_node: Node) -> str:
        parts = [self.index.text_of(c.id, skip=self.is_bot) for c in self.answer_nodes(quiz_node)]
        return "\n".join(p for p in parts if p).strip()

    def answer_hash(self, quiz_node: Node) -> str | None:
        text = self.answer_text(quiz_node)
        if not text:
            return None
        import hashlib
        h = hashlib.sha256()
        for c in self.answer_nodes(quiz_node):
            h.update(content_hash(self.index, c.id, self.is_bot).encode())
        return "v1:" + h.hexdigest()


def is_skip_answer(text: str, skip_answers: Iterable[str]) -> bool:
    t = norm(text).casefold().replace(" ", "")
    return t in {s.casefold().replace(" ", "") for s in skip_answers}
