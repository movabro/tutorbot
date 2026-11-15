"""Parsing helpers for explicitly tagged personal-task trees."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from ..dynalist.tree import Node, NodeIndex
from .hashing import HASH_PREFIX, norm
from .tags import has_tag

STATUS_NAMES = ("Backlog", "InProgress", "FastTrack", "Completed", "Abandon")
DATE_MARKER = re.compile(r"!\(([^)]*)\)")
VALID_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class ProgressRecord:
    node_id: str
    content: str
    date_text: str | None
    entry_date: date | None
    detail_paths: list[str] = field(default_factory=list)
    content_hash: str = ""
    warning: str | None = None


@dataclass
class TrackedTask:
    node: Node
    status: str | None
    status_conflict: bool
    category_path: list[str]
    progress: list[ProgressRecord]


def _plain_content(node: Node) -> str:
    return norm(DATE_MARKER.sub("", node.content).strip())


def _progress_record(
    index: NodeIndex,
    node: Node,
    tracking_tag: str | tuple[str, ...],
    skip: Callable[[Node], bool] | None = None,
) -> ProgressRecord:
    raw_matches = DATE_MARKER.findall(node.content)
    date_text = raw_matches[0].strip() if raw_matches else None
    parsed: date | None = None
    warning: str | None = None
    if not raw_matches:
        warning = "progress record is missing !(yyyy-mm-dd)"
    elif len(raw_matches) > 1 or not VALID_DATE.fullmatch(date_text or ""):
        warning = f"invalid progress date marker: {', '.join(raw_matches)}"
    else:
        try:
            parsed = date.fromisoformat(date_text or "")
        except ValueError:
            warning = f"invalid progress date marker: {date_text}"

    details: list[str] = []
    if node.note:
        details.append(f"[note] {norm(node.note)}")

    def visit(nid: str, parts: tuple[str, ...]) -> None:
        child = index.get(nid)
        if child is None or (skip and skip(child)) or has_tag(child.content, tracking_tag):
            return
        path = (*parts, _plain_content(child))
        if child.note:
            details.append(" > ".join((*path, f"[note] {norm(child.note)}")))
        elif child.content:
            details.append(" > ".join(path))
        for child_id in child.children:
            visit(child_id, path)

    for child_id in node.children:
        visit(child_id, ())

    def serialize(nid: str, depth: int = 0) -> str:
        current = index.get(nid)
        if (
            current is None
            or (skip and skip(current))
            or (nid != node.id and has_tag(current.content, tracking_tag))
        ):
            return ""
        fields = [norm(current.content), norm(current.note)]
        body = f"{depth}\x1f" + "\x1f".join(fields) + "\x1e"
        return body + "".join(serialize(child_id, depth + 1) for child_id in current.children)

    import hashlib

    digest = hashlib.sha256(serialize(node.id).encode("utf-8")).hexdigest()
    return ProgressRecord(
        node_id=node.id,
        content=node.content,
        date_text=date_text,
        entry_date=parsed,
        detail_paths=details,
        content_hash=f"{HASH_PREFIX}{digest}",
        warning=warning,
    )


def parse_tracked_tasks(
    index: NodeIndex,
    *,
    tracking_tag: str | tuple[str, ...] = "Track",
    status_names: tuple[str, ...] = STATUS_NAMES,
    skip: Callable[[Node], bool] | None = None,
) -> tuple[list[TrackedTask], list[tuple[str, str]]]:
    """Return explicitly tagged tasks and diagnostics; untagged category nodes are ignored."""
    tracked = [
        node for node in index.walk()
        if not (skip and skip(node)) and has_tag(node.content, tracking_tag)
    ]
    tracked_ids = {node.id for node in tracked}
    tasks: list[TrackedTask] = []
    warnings: list[tuple[str, str]] = []
    for node in tracked:
        ancestor_statuses = [
            ancestor.content.strip()
            for ancestor in index.ancestors(node.id)
            if ancestor.content.strip() in status_names
        ]
        distinct_statuses = set(ancestor_statuses)
        conflict = len(distinct_statuses) > 1
        status = ancestor_statuses[0] if len(distinct_statuses) == 1 else None
        if not ancestor_statuses:
            warnings.append((node.id, "tracking item has no recognized status ancestor"))
        elif conflict:
            warnings.append((node.id, "tracking item has conflicting status ancestors"))

        category_path: list[str] = []
        if status == "Completed":
            ancestors = list(index.ancestors(node.id))
            status_index = next(
                (i for i, ancestor in enumerate(ancestors) if ancestor.content.strip() == "Completed"),
                None,
            )
            if status_index is not None:
                category_path = [
                    ancestor.content.strip()
                    for ancestor in reversed(ancestors[:status_index])
                    if ancestor.content.strip()
                ]

        progress: list[ProgressRecord] = []
        for child_id in node.children:
            child = index.get(child_id)
            if (
                child_id in tracked_ids
                or child is None
                or (skip and skip(child))
            ):
                continue
            progress.append(_progress_record(index, child, tracking_tag, skip))
        for record in progress:
            if record.warning:
                warnings.append((record.node_id, record.warning))
        tasks.append(TrackedTask(node, status, conflict, category_path, progress))
    return tasks, warnings
