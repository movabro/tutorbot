"""Fail-closed pre-edit reconciliation for feedback insert batches.

The wrapper does not replace the writer's final-attempt recovery protocol.
Its use still requires a read-consistent Dynalist service and the global lock.
"""
from __future__ import annotations

import logging

from ..dynalist.client import F_PARENT, NodeNotFound, UnknownOutcome
from ..dynalist.tree import NodeIndex


class _UnsafeRead(BaseException):
    """Escape the writer's stale-index fallback without changing the outbox."""

    def __init__(self, file_id):
        self.file_id = file_id


class _FilteredCursor:
    def __init__(self, cursor, blocked):
        self._cursor, self._blocked = cursor, blocked

    def fetchall(self):
        return [row for row in self._cursor.fetchall()
                if row["file_id"] not in self._blocked
                or row["bot_kind"] not in ("feedback_rule", "feedback_rule_item")]


class _FilteredConnection:
    """Hide unsafe feedback ops while preserving ordinary outbox processing."""
    def __init__(self, conn, blocked):
        self._conn, self._blocked = conn, blocked

    def execute(self, sql, *args):
        cursor = self._conn.execute(sql, *args)
        if sql.startswith("SELECT * FROM write_op WHERE status IN"):
            return _FilteredCursor(cursor, self._blocked)
        return cursor

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _validated_index(file_id, response):
    nodes = response["nodes"]
    if not isinstance(nodes, list):
        raise TypeError("invalid nodes")
    index = NodeIndex.from_doc_read(file_id, response)
    if len(index.by_id) != len(nodes) or "root" not in index:
        raise ValueError("incomplete document")
    seen = set()
    todo = ["root"]
    while todo:
        node_id = todo.pop()
        if node_id in seen or node_id not in index:
            raise ValueError("invalid document tree")
        seen.add(node_id)
        todo.extend(index.get(node_id).children)
    if len(seen) != len(nodes):
        raise ValueError("disconnected document")
    return index


def flush_feedback_safe(conn, client, *, redact_all=False, **kwargs):
    """Run the existing writer without its identifier-bearing log messages."""
    from ..dynalist.writer import WriteReport, flush_outbox

    writer_log = logging.getLogger("tutorbot.dynalist.writer")

    class _Redact(logging.Filter):
        def filter(self, record):
            if redact_all or any(isinstance(arg, str) and arg.startswith("fr:")
                                 for arg in (record.args or ())):
                record.msg = "Dynalist write event"
                record.args = ()
            return True

    redactor = _Redact()
    writer_log.addFilter(redactor)
    try:
        blocked = set()
        adapter = FeedbackFlushAdapter(client, conn, blocked=blocked)
        files = {row[0] for row in conn.execute(
            "SELECT DISTINCT file_id FROM write_op WHERE action='insert' "
            "AND bot_kind IN ('feedback_rule','feedback_rule_item') "
            "AND status IN ('pending','failed') AND (attempts<5 OR last_error=?)",
            ("unknown_outcome_reconcile",))}
        # Never let the writer reconcile against an index supplied by a caller.
        kwargs["indices"] = {fid: idx for fid, idx in (kwargs.get("indices") or {}).items()
                             if fid not in files}
        report = WriteReport()
        for fid in files:
            try:
                adapter.doc_read(fid)
            except _UnsafeRead:
                blocked.add(fid)
                report.errors.append("feedback reconciliation unavailable or ambiguous")
        # A later read can fail too, including after a successful batch. Restart
        # with that file's feedback ops excluded; committed operations stay done.
        while True:
            try:
                part = flush_outbox(_FilteredConnection(conn, blocked), adapter, **kwargs)
                for name in ("done", "reconciled", "skipped", "failed", "changes_sent"):
                    setattr(report, name, getattr(report, name) + getattr(part, name))
                report.errors.extend(part.errors)
                return report
            except _UnsafeRead as exc:
                blocked.add(exc.file_id)
                report.errors.append("feedback reconciliation unavailable or ambiguous")
    finally:
        writer_log.removeFilter(redactor)


class FeedbackFlushAdapter:
    def __init__(self, client, conn=None, *, blocked=None):
        self._client = client
        self._conn = conn
        self._blocked = blocked if blocked is not None else set()

    def __getattr__(self, name):
        return getattr(self._client, name)

    def doc_read(self, file_id):
        if self._conn is None or file_id in self._blocked:
            return self._client.doc_read(file_id)
        from ..dynalist.writer import _resolve_ref

        ops = self._conn.execute(
            "SELECT parent_ref, marker FROM write_op WHERE file_id=? AND action='insert' "
            "AND bot_kind IN ('feedback_rule','feedback_rule_item') AND status IN ('pending','failed') "
            "AND (attempts<5 OR last_error=?)",
            (file_id, "unknown_outcome_reconcile")).fetchall()
        if not ops:
            return self._client.doc_read(file_id)
        try:
            response = self._client.doc_read(file_id)
            index = _validated_index(file_id, response)
            for op in ops:
                parent_id, state = _resolve_ref(self._conn, op["parent_ref"], retry_failed=True)
                if state != "ready":
                    continue
                parent = index.get(parent_id)
                if parent is None:
                    # A complete fresh document proves the parent is gone; the
                    # writer must mark this op skipped rather than reconcile it.
                    continue
                if not op["marker"] or any(index.get(c) is None for c in parent.children):
                    raise ValueError("incomplete parent or marker")
                matches = sum(((child.note or "").lstrip().splitlines() or [""])[0].strip() == op["marker"]
                              for child in index.children_of(parent_id))
                if matches > 1:
                    raise ValueError("ambiguous marker")
            return response
        except Exception as exc:
            raise _UnsafeRead(file_id) from exc

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
            index = _validated_index(file_id, response)
            matches = {}
            missing_parent = False
            for position, parent_id, marker in feedback:
                parent = index.get(parent_id)
                if parent is None:
                    missing_parent = True
                    continue
                if any(index.get(child) is None for child in parent.children):
                    raise ValueError("incomplete parent")
                found = [child.id for child in index.children_of(parent_id)
                         if ((child.note or "").lstrip().splitlines() or [""])[0].strip() == marker]
                if len(found) > 1:
                    raise ValueError("ambiguous marker")
                if found:
                    matches[position] = found[0]
        except Exception as exc:
            raise UnknownOutcome("feedback_reconcile", "fresh read unavailable or ambiguous") from exc

        if missing_parent:
            raise NodeNotFound("feedback_parent_missing")
        remaining = [change for pos, change in enumerate(changes) if pos not in matches]
        new_ids = self._client.doc_edit(file_id, remaining) if remaining else []
        insert_count = sum(change.get("action") == "insert" for change in remaining)
        if len(new_ids) != insert_count or any(not isinstance(node_id, str) or not node_id for node_id in new_ids):
            raise UnknownOutcome("feedback_reconcile", "invalid edit result")
        new_iter = iter(new_ids)
        return [matches[pos] if pos in matches else next(new_iter)
                for pos, change in enumerate(changes) if change.get("action") == "insert"]
