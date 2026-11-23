"""Concept index formatting and candidate retrieval (spec §9.3)."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .. import render


@dataclass
class ConceptCandidate:
    id: int
    path: str
    state: str
    name: str
    description: str


class ConceptRetriever:
    """Provides candidate leaf concepts and compressed index string (`id|상위>개념|상태기호`)."""

    def __init__(self, conn: sqlite3.Connection, goal_id: int, full_threshold: int = 150) -> None:
        self.conn = conn
        self.goal_id = goal_id
        self.full_threshold = full_threshold
        self._all_rows = conn.execute(
            "SELECT * FROM concept WHERE goal_id=? AND user_deleted=0 ORDER BY depth ASC, sort_order ASC, id ASC",
            (goal_id,),
        ).fetchall()
        self._by_id = {r["id"]: r for r in self._all_rows}

    def _path_of(self, cid: int) -> str:
        parts: list[str] = []
        cur = self._by_id.get(cid)
        while cur is not None:
            parts.append(cur["name"])
            pid = cur["parent_id"]
            cur = self._by_id.get(pid) if pid is not None else None
        parts.reverse()
        return ">".join(parts)

    def all_leaf_candidates(self) -> list[ConceptCandidate]:
        out: list[ConceptCandidate] = []
        for r in self._all_rows:
            if not r["is_leaf"]:
                continue
            out.append(
                ConceptCandidate(
                    id=int(r["id"]),
                    path=self._path_of(r["id"]),
                    state=r["state"],
                    name=r["name"],
                    description=r["description"] or "",
                )
            )
        return out

    def candidates(self, text: str, k: int = 25) -> list[ConceptCandidate]:
        leaves = self.all_leaf_candidates()
        if len(leaves) <= self.full_threshold:
            return leaves
        # Keyword / token overlap fallback for > threshold
        tokens = set(re.findall(r"\w{2,}", text.casefold()))
        scored: list[tuple[int, int, ConceptCandidate]] = []
        for idx, c in enumerate(leaves):
            doc_tokens = set(re.findall(r"\w{2,}", f"{c.path} {c.description}".casefold()))
            overlap = len(tokens & doc_tokens)
            scored.append((overlap, -idx, c))
        scored.sort(reverse=True)
        top = [c for _, _, c in scored[:k]]
        top.sort(key=lambda c: c.id)
        return top

    @staticmethod
    def format_index(candidates: list[ConceptCandidate]) -> str:
        """One line per concept: `id|상위>개념|상태기호` (spec §9.3.1)."""
        lines = []
        for c in candidates:
            icon = render.STATE_ICON.get(c.state, "⬜")
            lines.append(f"{c.id}|{c.path}|{icon}")
        return "\n".join(lines)
