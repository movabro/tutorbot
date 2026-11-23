"""Feedback execution planning helpers."""
from __future__ import annotations

from .models import NodeRef, UnitPlan, UnitSpec


def build_units(task, selected, max_units: int) -> UnitPlan:
    """Plan calls and destinations without reading or writing external state."""
    if max_units < 1:
        raise ValueError("max_units must be positive")
    if not selected:
        return UnitPlan((), 0)
    under_parent = task.output.placement == "under_parent"
    parent = NodeRef(task.output.file_id, task.output.parent_node_id) if under_parent else None
    if task.unit == "per_node":
        items = tuple(
            UnitSpec(f"{node.ref.file_id}.{node.ref.node_id}", (node,),
                     (parent if under_parent else node.ref,))
            for node in selected[:max_units]
        )
        return UnitPlan(items, max(0, len(selected) - max_units))
    if task.unit == "aggregate":
        destinations = (parent,) if under_parent else tuple(node.ref for node in selected[:max_units])
        skipped = 0 if under_parent else max(0, len(selected) - max_units)
        return UnitPlan((UnitSpec("agg", tuple(selected), destinations),), skipped)
    raise ValueError("unknown feedback unit")
