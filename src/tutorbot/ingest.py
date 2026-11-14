"""Pipeline stages 3-4: bot registry reconciliation, parsing and change detection (spec §6.2)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import clock
from .dynalist.tree import NodeIndex
from .parse.classify import Classifier, Unit
from .parse.dates import resolve_entry_date
from .parse.hashing import HASH_PREFIX, hash_version
from .parse.tags import strip_tags

if TYPE_CHECKING:
    from .config import Settings


@dataclass
class IngestReport:
    goals_new: int = 0
    goals_changed: int = 0
    dumps_changed: int = 0
    entries_new: int = 0
    entries_changed: int = 0
    entries_deleted: int = 0
    quiz_answers_found: int = 0
    quiz_orphaned: int = 0
    bot_nodes_gone: int = 0
    warnings: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, node_id, reason)

    def summary(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "warnings"}
        d["warnings"] = len(self.warnings)
        return d


def load_registry(conn: sqlite3.Connection, file_id: str) -> dict[str, str]:
    rows = conn.execute("SELECT node_id, kind FROM bot_node WHERE file_id=? AND deleted_at IS NULL", (file_id,))
    return {r["node_id"]: r["kind"] for r in rows}


def reconcile_registry(conn: sqlite3.Connection, index: NodeIndex, rep: IngestReport,
                       bot_tag: str = "tutorbot") -> None:
    now = clock.now_iso()
    reg = load_registry(conn, index.file_id)
    for nid in list(reg.keys()):
        if nid not in index:
            conn.execute("UPDATE bot_node SET deleted_at=? WHERE file_id=? AND node_id=?",
                         (now, index.file_id, nid))
            rep.bot_nodes_gone += 1
            del reg[nid]
    # Recover any unregistered nodes in the tree that have a tb:<kind>:<ref_id> marker or #tutorbot tag
    from .parse.tags import has_tag
    for node in index.walk():
        if node.id in reg:
            continue
        first_note_line = ((node.note or "").lstrip().splitlines() or [""])[0].strip()
        kind: str | None = None
        ref_id: int | None = None
        if first_note_line.startswith("tb:"):
            parts = first_note_line.split(":")
            if len(parts) >= 2 and parts[1]:
                kind = parts[1]
            if len(parts) >= 3:
                try:
                    ref_id = int(parts[2])
                except ValueError:
                    ref_id = None
        elif has_tag(node.content, bot_tag):
            kind = "recovered"
        if kind:
            conn.execute(
                "INSERT INTO bot_node(file_id,node_id,kind,ref_id,created_at,deleted_at)"
                " VALUES(?,?,?,?,?,NULL)"
                " ON CONFLICT(file_id,node_id) DO UPDATE SET kind=excluded.kind,"
                " ref_id=COALESCE(excluded.ref_id,bot_node.ref_id), deleted_at=NULL",
                (index.file_id, node.id, kind, ref_id, now),
            )
            if kind == "feedback" and ref_id is not None:
                conn.execute("UPDATE feedback SET node_id=? WHERE entry_id=? AND node_id IS NULL",
                             (node.id, ref_id))
            elif kind == "concept" and ref_id is not None:
                conn.execute("UPDATE concept SET node_id=? WHERE id=? AND node_id IS NULL",
                             (node.id, ref_id))
            elif kind == "quiz_item" and ref_id is not None:
                conn.execute("UPDATE quiz_attempt SET node_id=?, status=CASE WHEN status='pending_write' THEN 'open' ELSE status END"
                             " WHERE id=? AND node_id IS NULL",
                             (node.id, ref_id))
            elif kind == "weekly_report" and ref_id is not None:
                conn.execute("UPDATE weekly_report SET node_id=? WHERE id=? AND node_id IS NULL",
                             (node.id, ref_id))


def _active_goals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM goal WHERE status != 'archived'").fetchall()


def _link_goal(conn: sqlite3.Connection, unit: Unit) -> int | None:
    goals = _active_goals(conn)
    if len(goals) == 1:
        return goals[0]["id"]
    for t in unit.topic_tags:
        for g in goals:
            if g["topic_tag"] == t:
                return g["id"]
    return None


def _goal_fields(raw: str) -> tuple[str, str | None]:
    title = strip_tags(raw.splitlines()[0] if raw else "")
    return title or "(untitled goal)", None


def ingest_index(conn: sqlite3.Connection, index: NodeIndex, settings: Settings,
                 rep: IngestReport | None = None) -> IngestReport:
    """Parse one document into the DB. Caller wraps in a transaction."""
    rep = rep or IngestReport()
    fid = index.file_id
    now = clock.now_iso()
    reconcile_registry(conn, index, rep, getattr(settings.tags, "bot", "tutorbot"))
    cls = Classifier(index, settings.tags, load_registry(conn, fid))
    units = cls.find_units()
    seen: dict[str, set[str]] = {"goal": set(), "dump": set(), "study": set()}

    # goals first so dumps/entries can link to goals created in this same run
    for u in sorted(units, key=lambda u: u.kind != "goal"):
        n = u.node
        seen[u.kind].add(n.id)
        raw = index.text_of(n.id, skip=cls.is_bot)
        if u.kind == "goal":
            _ingest_goal(conn, u, raw, fid, now, rep)
        elif u.kind == "dump":
            _ingest_dump(conn, cls, u, raw, fid, now, rep)
        else:
            _ingest_study(conn, index, cls, u, raw, fid, now, settings, rep)

    # vanished user nodes -> deleted (data kept)
    for table, kind in (("study_entry", "study"), ("knowledge_dump", "dump")):
        for r in conn.execute(f"SELECT id, node_id FROM {table} WHERE file_id=? AND status!='deleted'", (fid,)).fetchall():
            if r["node_id"] not in seen[kind]:
                conn.execute(f"UPDATE {table} SET status='deleted' WHERE id=?", (r["id"],))
                if kind == "study":
                    rep.entries_deleted += 1
    for r in conn.execute("SELECT id, node_id FROM goal WHERE file_id=? AND status!='archived'", (fid,)).fetchall():
        if r["node_id"] not in seen["goal"]:
            conn.execute("UPDATE goal SET status='archived', updated_at=? WHERE id=?", (now, r["id"]))

    # Quiz answer collection (spec §3.5): only `open` attempts for this file_id
    for att in conn.execute(
        "SELECT id, node_id FROM quiz_attempt WHERE file_id=? AND status='open'",
        (fid,),
    ).fetchall():
        aid = att["id"]
        qnid = att["node_id"]
        if not qnid or qnid not in index:
            conn.execute("UPDATE quiz_attempt SET status='orphaned' WHERE id=?", (aid,))
            rep.quiz_orphaned += 1
            continue
        qnode = index.get(qnid)
        if qnode is None:
            continue
        ans_text = cls.answer_text(qnode)
        ans_hash = cls.answer_hash(qnode)
        conn.execute(
            "UPDATE quiz_attempt SET answer_text=?, answer_hash=? WHERE id=?",
            (ans_text if ans_text else None, ans_hash, aid),
        )
        if ans_text:
            rep.quiz_answers_found += 1

    return rep


def _ingest_goal(conn, u: Unit, raw: str, fid: str, now: str, rep: IngestReport) -> None:
    n = u.node
    if not u.topic_tags:
        rep.warnings.append(("goal_no_topic", n.id, "goal needs one topic tag, e.g. #rust"))
        return
    topic = u.topic_tags[0]
    title, _ = _goal_fields(n.content)
    row = conn.execute("SELECT * FROM goal WHERE file_id=? AND node_id=?", (fid, n.id)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO goal(file_id,node_id,title,topic_tag,status,source_hash,created_at,updated_at)"
            " VALUES(?,?,?,?, 'pending_curriculum',?,?,?)",
            (fid, n.id, title, topic, u.content_hash, now, now))
        rep.goals_new += 1
    elif row["source_hash"] != u.content_hash:
        conn.execute("UPDATE goal SET title=?, topic_tag=?, source_hash=?, updated_at=? WHERE id=?",
                     (title, topic, u.content_hash, now, row["id"]))
        rep.goals_changed += 1


def _ingest_dump(conn, cls: Classifier, u: Unit, raw: str, fid: str, now: str, rep: IngestReport) -> None:
    n = u.node
    goal_id = _link_goal(conn, u)
    row = conn.execute("SELECT * FROM knowledge_dump WHERE file_id=? AND node_id=?", (fid, n.id)).fetchone()
    status = "pending" if goal_id else "skipped_untagged"
    if goal_id is None:
        rep.warnings.append(("untagged", n.id, "topic tag required when several goals are active"))
    if row is None:
        conn.execute(
            "INSERT INTO knowledge_dump(goal_id,file_id,node_id,raw_text,content_hash,status)"
            " VALUES(?,?,?,?,?,?)", (goal_id, fid, n.id, raw, u.content_hash, status))
        rep.dumps_changed += 1
        return
    if row["content_hash"] != u.content_hash:
        conn.execute("UPDATE knowledge_dump SET goal_id=?, raw_text=?, content_hash=?, status=?, retry_count=0"
                     " WHERE id=?", (goal_id, raw, u.content_hash, status, row["id"]))
        rep.dumps_changed += 1
    elif hash_version(row["processed_hash"]) not in (None, HASH_PREFIX[:-1]):
        conn.execute("UPDATE knowledge_dump SET processed_hash=content_hash WHERE id=?", (row["id"],))
    elif row["status"] == "skipped_untagged" and goal_id:
        conn.execute("UPDATE knowledge_dump SET goal_id=?, status='pending' WHERE id=?", (goal_id, row["id"]))


def _ingest_study(conn, index: NodeIndex, cls: Classifier, u: Unit, raw: str, fid: str, now: str,
                  settings: Settings, rep: IngestReport) -> None:
    n = u.node
    goal_id = _link_goal(conn, u)
    day, src = resolve_entry_date(index, n, settings.timezone)
    max_mod = index.max_modified(n.id, skip=cls.is_bot)
    body_len = len(strip_tags(index.text_of(n.id, skip=cls.is_bot)).replace("\n", ""))
    if goal_id is None:
        status = "skipped_untagged"
        rep.warnings.append(("untagged", n.id, "topic tag required when several goals are active"))
    elif body_len < settings.processing.min_entry_chars:
        status = "too_short"
    else:
        status = "pending"
    row = conn.execute("SELECT * FROM study_entry WHERE file_id=? AND node_id=?", (fid, n.id)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO study_entry(goal_id,file_id,node_id,entry_date,date_source,raw_text,content_hash,"
            "max_modified,status,first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (goal_id, fid, n.id, day.isoformat(), src, raw, u.content_hash, max_mod, status, now))
        rep.entries_new += 1
        return
    if row["content_hash"] != u.content_hash:
        conn.execute(
            "UPDATE study_entry SET goal_id=?, entry_date=?, date_source=?, raw_text=?, content_hash=?,"
            " max_modified=?, status=?, retry_count=0, last_error=NULL WHERE id=?",
            (goal_id, day.isoformat(), src, raw, u.content_hash, max_mod, status, row["id"]))
        rep.entries_changed += 1
        return
    # unchanged text: refresh date/goal links (a move changes the date, not the hash)
    new_status = row["status"]
    if row["status"] in ("skipped_untagged", "deferred") and status == "pending":
        new_status = "pending"
    elif row["status"] == "deleted":
        new_status = status
    conn.execute("UPDATE study_entry SET goal_id=?, entry_date=?, date_source=?, max_modified=?, status=? WHERE id=?",
                 (goal_id, day.isoformat(), src, max_mod, new_status, row["id"]))
    if hash_version(row["processed_hash"]) not in (None, HASH_PREFIX[:-1]):
        conn.execute("UPDATE study_entry SET processed_hash=content_hash WHERE id=?", (row["id"],))
