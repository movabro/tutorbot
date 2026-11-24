"""Fail-closed pre-edit reconciliation for feedback insert batches.

The wrapper does not replace the writer's final-attempt recovery protocol.
Its use still requires a read-consistent Dynalist service and the global lock.
"""
from __future__ import annotations

from ..dynalist.client import F_PARENT, UnknownOutcome
from ..dynalist.tree import NodeIndex


class FeedbackFlushAdapter:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    def doc_edit(self, file_id, changes):
        feedback = []
        seen = set()
        for index, change in enumerate(changes):
            note = change.get("note", "")
            first = (note.lstrip().splitlines() or [""])[0].strip() if isinstance(note, str) else ""
            if change.get("action") == "insert" and first.startswith(
                    ("tb:feedback_rule:", "tb:feedback_rule_item:")):
                marker = first
                if marker in seen:
                    raise UnknownOutcome("feedback_reconcile", "duplicate batch marker")
                seen.add(marker)
                feedback.append((index, change.get(F_PARENT), marker))
        if not feedback:
            return self._client.doc_edit(file_id, changes)

        try:
            response = self._client.doc_read(file_id)
            raw_nodes = response["nodes"]
            if not isinstance(raw_nodes, list):
                raise ValueError("invalid nodes")
            index = NodeIndex.from_doc_read(file_id, response)
            if len(index.by_id) != len(raw_nodes):
                raise ValueError("duplicate node")
            matches = {}
            for position, parent_id, marker in feedback:
                parent = index.get(parent_id)
                if parent is None or any(index.get(child) is None for child in parent.children):
                    raise ValueError("incomplete parent")
                found = [child.id for child in index.children_of(parent_id)
                         if ((child.note or "").lstrip().splitlines() or [""])[0].strip() == marker]
                if len(found) > 1:
                    raise ValueError("ambiguous marker")
                if found:
                    matches[position] = found[0]
        except Exception as exc:
            raise UnknownOutcome("feedback_reconcile", "fresh read unavailable or ambiguous") from exc

        remaining = [change for pos, change in enumerate(changes) if pos not in matches]
        new_ids = self._client.doc_edit(file_id, remaining) if remaining else []
        insert_count = sum(change.get("action") == "insert" for change in remaining)
        if len(new_ids) != insert_count or any(not isinstance(node_id, str) or not node_id for node_id in new_ids):
            raise UnknownOutcome("feedback_reconcile", "invalid edit result")
        new_iter = iter(new_ids)
        return [matches[pos] if pos in matches else next(new_iter)
                for pos, change in enumerate(changes) if change.get("action") == "insert"]
