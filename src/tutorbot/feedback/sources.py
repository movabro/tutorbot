"""Iterative feedback source selection and context snapshots."""
from __future__ import annotations

from dataclasses import dataclass

from ..dynalist.tree import NodeIndex
from ..parse.classify import Classifier
from ..parse.tags import node_tag_set, tags_match
from .models import ContextLine, NodeRef, ReferenceBlock, TargetBlock
from .timefilter import matches_time, resolve_time_bounds
from .tokens import estimate_tokens


@dataclass(frozen=True)
class SelectedNode:
    ref: NodeRef
    selection_index: int
    context_scope: str


def compute_bot_flags(index: NodeIndex, classifier: Classifier) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    root = index.get("root")
    if root is None:
        return flags
    stack = list(reversed(root.children))
    while stack:
        node_id = stack.pop()
        node = index.get(node_id)
        if node is None or node_id in flags:
            continue
        parent = index.parent_of.get(node_id)
        inherited = parent != "root" and classifier.registry.get(parent) != "quiz_item" and flags.get(parent, False)
        flags[node_id] = classifier.is_direct_bot(node) or bool(inherited)
        stack.extend(reversed(node.children))
    return flags


def _walk(index, parent_id, flags, *, include_parent, max_depth=None, nested_tags=frozenset()):
        parent = index.get(parent_id)
        if parent is None:
            return
        stack = [(parent_id, 0)] if include_parent and parent_id != "root" else [
            (node_id, 0 if parent_id == "root" else 1) for node_id in reversed(parent.children)
        ]
        while stack:
            node_id, depth = stack.pop()
            node = index.get(node_id)
            if node is None or flags.get(node_id, False):
                continue
            if depth and nested_tags and tags_match(node_tag_set(node.content, node.note), nested_tags):
                continue
            yield node, depth
            if max_depth is None or depth < max_depth:
                stack.extend((child, depth + 1) for child in reversed(node.children))


def _source_nodes(task, source, index, flags, marks, T, prev_started_at, first_lower):
        bounds = resolve_time_bounds(source.time, T, task.timezone, prev_started_at or first_lower, task)
        wanted = frozenset(source.tags_any)
        for node, depth in _walk(index, source.parent_node_id, flags,
                                 include_parent=source.scope != "descendants",
                                 max_depth=0 if source.scope == "self" else None):
            if tags_match(node_tag_set(node.content, node.note), wanted) and matches_time(
                    node, bounds, marks.get((source.file_id, node.id))):
                yield node, depth


def resolve_selection(task, indices, bot_flags, bot_marks, T, prev_started_at, first_lower):
        selected = []
        seen = set()
        for position, source in enumerate(task.selection):
            index = indices.get(source.file_id)
            if index is None:
                continue
            for node, _ in _source_nodes(task, source, index, bot_flags.get(source.file_id, {}),
                                         bot_marks, T, prev_started_at, first_lower):
                ref = NodeRef(source.file_id, node.id)
                if ref not in seen:
                    seen.add(ref)
                    selected.append(SelectedNode(ref, position, source.context_scope))
        return selected


def _line(file_id, node, depth):
        text = node.content or ""
        note = node.note or None
        return ContextLine(NodeRef(file_id, node.id), depth, text, note,
                           estimate_tokens(text + (note or "")))


def render_target(index, bot_flags, selected, nested_tags):
        depth = 0 if selected.context_scope == "self" else 1 if selected.context_scope == "self_and_children" else None
        tags = frozenset(nested_tags) if selected.context_scope == "self_excluding_nested_goal_subtrees" else frozenset()
        lines = tuple(_line(index.file_id, node, level) for node, level in _walk(
            index, selected.ref.node_id, bot_flags, include_parent=True, max_depth=depth, nested_tags=tags))
        return TargetBlock(selected, lines, sum(line.tokens for line in lines))


def render_reference(index, bot_flags, source_index, source, nodes):
        lines = tuple(_line(index.file_id, node, depth) for node, depth in nodes)
        return ReferenceBlock(source_index, source, lines, sum(line.tokens for line in lines))


def resolve_references(task, indices, bot_flags, bot_marks, T, prev_started_at, first_lower):
        blocks = []
        for position, source in enumerate(task.references):
            index = indices.get(source.file_id)
            if index is None:
                continue
            nodes = list(_source_nodes(task, source, index, bot_flags.get(source.file_id, {}),
                                       bot_marks, T, prev_started_at, first_lower))
            blocks.append(render_reference(index, bot_flags.get(source.file_id, {}), position, source, nodes))
        return blocks
