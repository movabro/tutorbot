"""Normalized feedback rule types, independent of application settings."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sources import SelectedNode


@dataclass(frozen=True)
class ConfigIssue:
    code: str
    severity: str
    path: str
    message: str
    disables_task: bool = True


@dataclass(frozen=True)
class RulesParseFailure:
    line: int
    construct: str


@dataclass(frozen=True)
class ScheduleSpec:
    kind: str
    hour: int = 0
    minute: int = 0
    weekday: int = 6
    day: int | None = None
    month: int = 12
    quarter_anchor: str = "end"
    quarter_anchor_invalid: str | None = None
    week_end: bool = False


@dataclass(frozen=True)
class SourceSpec:
    file_id: str
    parent_node_id: str
    scope: str
    time: str
    tags_any: tuple[str, ...] = ()
    context_scope: str | None = None


@dataclass(frozen=True)
class OutputSpec:
    placement: str
    file_id: str | None = None
    parent_node_id: str | None = None
    format: str | None = None


@dataclass(frozen=True)
class TaskSpec:
    index: int
    id: str
    purpose: str
    domain: str
    persona_path: str | None
    persona_approval: str | None
    unit: str
    schedule: ScheduleSpec
    timezone: str
    input_token_range: tuple[int, int]
    output_token_range: tuple[int, int]
    evidence: str
    selection: tuple[SourceSpec, ...]
    references: tuple[SourceSpec, ...]
    output: OutputSpec
    base_dir: Path
    valid: bool
    issues: tuple[ConfigIssue, ...]

    def config_hash(self) -> str:
        values = asdict(self)
        # Diagnostics and source location do not describe the normalized task.
        for key in ("index", "base_dir", "valid", "issues"):
            values.pop(key)
        # Preserve hashes of pre-extension tasks.
        if not values["schedule"]["week_end"]:
            del values["schedule"]["week_end"]
        if values["output"]["format"] is None:
            del values["output"]["format"]
        canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "v1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class FeedbackRulesCfg:
    enabled: bool = True
    max_llm_calls_per_run: int = 30
    max_units_per_task: int = 20
    stale_lock_minutes: int = 360
    safety_input_token_limit: int | None = None
    tasks: list[TaskSpec] = field(default_factory=list)
    issues: list[ConfigIssue] = field(default_factory=list)
    source: str = "config.toml"
    base_dir: Path = Path(".")

    def valid_tasks(self) -> list[TaskSpec]:
        if any(i.severity == "error" for i in self.issues):
            return []
        return [task for task in self.tasks if task.valid]

    def file_ids(self) -> list[str]:
        if not self.enabled:
            return []
        ids = []
        for task in self.valid_tasks():
            ids.extend(source.file_id for source in (*task.selection, *task.references))
            if task.output.file_id:
                ids.append(task.output.file_id)
        return list(dict.fromkeys(ids))

    def write_file_ids(self) -> list[str]:
        if not self.enabled:
            return []
        ids = []
        for task in self.valid_tasks():
            if task.output.placement == "under_parent":
                if task.output.file_id:
                    ids.append(task.output.file_id)
            else:
                ids.extend(source.file_id for source in task.selection)
        return list(dict.fromkeys(ids))

    def safety_limit(self, task: TaskSpec) -> int:
        maximum = task.input_token_range[1]
        limit = self.safety_input_token_limit
        return max(maximum, limit if limit is not None else min(4 * maximum, 1_000_000))


@dataclass(frozen=True)
class NodeRef:
    file_id: str
    node_id: str


@dataclass(frozen=True)
class ScheduleDecision:
    action: str
    cycle_key: str | None
    scheduled_at: datetime | None
    skip_reason: str | None


@dataclass(frozen=True)
class TimeBounds:
    kind: str
    field: str
    lower_ms: int
    upper_ms: int


@dataclass(frozen=True)
class BotWriteMark:
    own_hash: str
    effective_modified_ms: int


@dataclass(frozen=True)
class TargetSnapshot:
    own_hash: str
    effective_modified_ms: int


@dataclass(frozen=True)
class ContextLine:
    ref: NodeRef
    depth: int
    text: str
    note: str | None
    tokens: int


@dataclass(frozen=True)
class TargetBlock:
    selected: SelectedNode
    lines: tuple[ContextLine, ...]
    tokens: int


@dataclass(frozen=True)
class ReferenceBlock:
    source_index: int
    source: SourceSpec
    lines: tuple[ContextLine, ...]
    tokens: int


@dataclass(frozen=True)
class UnitSpec:
    ref: str
    inputs: tuple[SelectedNode, ...]
    write_to: tuple[NodeRef, ...]


@dataclass(frozen=True)
class UnitPlan:
    items: tuple[UnitSpec, ...]
    skipped_cap: int


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str
    approval_id: int | None = None
    preview: bool = False


@dataclass(frozen=True)
class PersonaResult:
    path: str | None
    text: str | None
    hash: str | None
    size_bytes: int
    line_count: int
    error: str | None


@dataclass(frozen=True)
class LockResult:
    acquired: bool
    token: str | None = None
    owner_pid: int | None = None
    since: str | None = None
    stolen_run_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SyncResult:
    written: int
    failed: int
    pending: int
    late_failed_run_ids: tuple[int, ...] = ()
