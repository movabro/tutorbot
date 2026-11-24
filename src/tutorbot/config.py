"""Settings loaded from config.toml and .env. Supports pydantic/tomli if installed, with stdlib fallback."""
from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .feedback.loader import parse_feedback_rules
from .feedback.models import ConfigIssue, FeedbackRulesCfg

log = logging.getLogger(__name__)


def load_dotenv(path: str | Path = ".env") -> None:
    """Load key=value pairs from .env into os.environ (without overwriting existing vars)."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v


def _parse_toml_bytes(data: bytes) -> dict[str, Any]:
    try:
        import tomllib as _toml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        try:
            import tomli as _toml  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            return _simple_toml_parse(data.decode("utf-8"))
    return _toml.loads(data.decode("utf-8"))


def _strip_inline_comment(s: str) -> str:
    in_q: str | None = None
    esc = False
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch in ('"', "'"):
            if in_q is None:
                in_q = ch
            elif in_q == ch:
                in_q = None
        elif ch == "#" and in_q is None:
            return s[:i].strip()
    return s.strip()


def _parse_val(v: str) -> Any:
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    try:
        return ast.literal_eval(v)
    except Exception:
        return v.strip('"').strip("'")


def _parse_array(value: str) -> list:
    class BoolNames(ast.NodeTransformer):
        def visit_Name(self, node):
            if node.id in ("true", "false"):
                return ast.copy_location(ast.Constant(node.id == "true"), node)
            return node

    tree = BoolNames().visit(ast.parse(value, mode="eval"))
    result = ast.literal_eval(tree)
    if not isinstance(result, list) or any(isinstance(x, (list, dict)) for x in result):
        raise ValueError("expected a one-dimensional array")
    return result


def _unsupported_rules_value(value: str) -> str | None:
    """Detect unsupported constructs outside quoted strings, including in arrays."""
    unquoted = re.sub(r'"(?:\\.|[^"\\])*"|\'[^\']*\'', '""', value)
    if "{" in unquoted or "}" in unquoted:
        return "inline table"
    if re.search(r"(?<![\w-])(?:\d{4}-\d\d-\d\d(?:[Tt ][0-9:.+-]+[Zz]?)?|\d\d:\d\d:\d\d(?:\.\d+)?)(?![\w-])", unquoted):
        return "datetime literal"
    return None


def _simple_toml_parse(text: str) -> dict[str, Any]:
    from .feedback.models import RulesParseFailure

    root: dict[str, Any] = {}
    cur = root
    lines = text.splitlines()
    section = ""
    failure = None
    i = 0
    while i < len(lines):
        i += 1
        line = _strip_inline_comment(lines[i - 1])
        if not line:
            continue
        if line.startswith("[") and line.endswith("]") and "=" not in line:
            array_table = line.startswith("[[") and line.endswith("]]")
            sec = line[2:-2].strip() if array_table else line[1:-1].strip()
            section = sec
            if failure and (sec == "feedback_rules" or sec.startswith("feedback_rules.")):
                continue
            cur = root
            names = sec.split(".")
            for name in names[:-1]:
                child = cur.setdefault(name, {})
                if isinstance(child, list) and child:
                    child = child[-1]
                if not isinstance(child, dict):
                    raise ValueError(f"TOML table conflicts with a value: {sec}")
                cur = child
            name = names[-1]
            if array_table:
                child = cur.setdefault(name, [])
                if not isinstance(child, list):
                    raise ValueError(f"TOML table conflicts with a value: {sec}")
                child.append({})
                cur = child[-1]
            else:
                child = cur.setdefault(name, {})
                if isinstance(child, list) and child:
                    child = child[-1]
                if not isinstance(child, dict):
                    raise ValueError(f"TOML table conflicts with a value: {sec}")
                cur = child
            continue
        if failure and (section == "feedback_rules" or section.startswith("feedback_rules.")):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if section == "feedback_rules" or section.startswith("feedback_rules."):
                construct = None
                if v.startswith(('"""', "'''")):
                    construct = "multiline string"
                elif "." in k:
                    construct = "dotted key"
                else:
                    construct = _unsupported_rules_value(v)
                if construct:
                    failure = RulesParseFailure(i, construct)
                    continue
            if v.startswith("["):
                start = i
                while True:
                    # The Python parser detects unterminated arrays and strings.
                    try:
                        parsed = _parse_array(v)
                    except (ValueError, SyntaxError):
                        if i >= len(lines):
                            raise ValueError(f"invalid TOML array at line {start}") from None
                        i += 1
                        v += "\n" + _strip_inline_comment(lines[i - 1])
                        if section == "feedback_rules" or section.startswith("feedback_rules."):
                            construct = _unsupported_rules_value(v)
                            if construct:
                                failure = RulesParseFailure(i, construct)
                                break
                    else:
                        cur[k] = parsed
                        break
            else:
                if v.startswith(('"', "'")):
                    try:
                        parsed = ast.literal_eval(v)
                    except (SyntaxError, ValueError):
                        raise ValueError(f"invalid TOML string at line {i}") from None
                    cur[k] = parsed
                else:
                    cur[k] = _parse_val(v)
        else:
            raise ValueError(f"invalid TOML at line {i}")
    if failure:
        root["feedback_rules"] = failure
    return root


@dataclass
class DynalistCfg:
    token_env: str = "DYNALIST_TOKEN"


@dataclass
class TagsCfg:
    goal: list[str] = field(default_factory=lambda: ["목표", "goal"])
    dump: list[str] = field(default_factory=lambda: ["지식덤프", "dump"])
    study: list[str] = field(default_factory=lambda: ["공부", "study"])
    bot: str = "tutorbot"


@dataclass
class QuizCfg:
    per_day: int = 5
    max_new_per_day: int = 2
    expire_days: int = 3
    skip_answers: list[str] = field(default_factory=lambda: ["?", "모름", "pass", "패스"])


@dataclass
class ProcessingCfg:
    min_entry_chars: int = 20
    settle_minutes: int = 60
    similarity_skip: float = 0.95
    max_entries_per_run: int = 20
    entry_max_chars: int = 6000
    priority_quiz: bool = True


@dataclass
class LLMCfg:
    provider: str = "gemini"
    api_key_env: str = "LLM_API_KEY"
    model_smart: str = "gemini-3.5-flash-lite"
    model_fast: str = "gemini-3.5-flash-lite"
    daily_token_budget: int = 300000
    index_full_threshold: int = 150
    requests_per_minute: int = 10

    def __post_init__(self) -> None:
        if type(self.requests_per_minute) is not int or self.requests_per_minute <= 0:
            raise ValueError("llm.requests_per_minute must be a positive integer")


@dataclass
class LoggingCfg:
    log_file: str = "logs/tutorbot.log"


@dataclass
class DomainProfileCfg:
    enabled: bool = True
    file_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source_domains: list[str] = field(default_factory=list)
    tracking_tag: str = "Track"
    tracking_tag_aliases: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    prompt: str = ""
    output_format: str = "feedback_json"
    status_nodes: list[str] = field(default_factory=lambda: [
        "Backlog", "InProgress", "FastTrack", "Completed", "Abandon",
    ])


def _default_domains() -> dict[str, DomainProfileCfg]:
    return {
        "study": DomainProfileCfg(),
        "work": DomainProfileCfg(),
        "life": DomainProfileCfg(),
        "career": DomainProfileCfg(source_domains=["work", "life"]),
        "personal_tasks": DomainProfileCfg(),
        "devlog": DomainProfileCfg(),
    }


@dataclass
class Settings:
    timezone: str = "Asia/Seoul"
    db_path: str = "data/tutorbot.sqlite3"
    dynalist: DynalistCfg = field(default_factory=DynalistCfg)
    tags: TagsCfg = field(default_factory=TagsCfg)
    quiz: QuizCfg = field(default_factory=QuizCfg)
    processing: ProcessingCfg = field(default_factory=ProcessingCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    domains: dict[str, DomainProfileCfg] = field(default_factory=_default_domains)
    feedback_rules: FeedbackRulesCfg = field(default_factory=FeedbackRulesCfg)

    @classmethod
    def model_validate(cls, d: dict[str, Any], *, base_dir: Path | None = None,
                       allow_placeholders: bool = False) -> "Settings":
        domains = _default_domains()
        for name, profile in d.get("domains", {}).items():
            if name not in domains:
                raise ValueError(f"unknown domain profile: {name}")
            domains[name] = replace(domains[name], **profile)
        for source_domain in domains["career"].source_domains:
            if source_domain not in {"work", "life"}:
                raise ValueError(f"invalid career source domain: {source_domain}")
        allowed_statuses = {"Backlog", "InProgress", "FastTrack", "Completed", "Abandon"}
        if set(domains["personal_tasks"].status_nodes) != allowed_statuses:
            raise ValueError("personal_tasks.status_nodes must contain the five supported PascalCase states")
        if not domains["personal_tasks"].tracking_tag.strip():
            raise ValueError("personal_tasks.tracking_tag must not be empty")
        for name, profile in domains.items():
            if profile.output_format != "feedback_json":
                raise ValueError(f"{name}.output_format must be 'feedback_json'")
            if any(not tag.strip() for tag in profile.tags):
                raise ValueError(f"{name}.tags must not contain empty tags")
            if any(not tag.strip() for tag in profile.tracking_tag_aliases):
                raise ValueError(f"{name}.tracking_tag_aliases must not contain empty tags")
        return cls(
            timezone=d.get("timezone", "Asia/Seoul"),
            db_path=d.get("db_path", "data/tutorbot.sqlite3"),
            dynalist=DynalistCfg(**d.get("dynalist", {})),
            tags=TagsCfg(**d.get("tags", {})),
            quiz=QuizCfg(**d.get("quiz", {})),
            processing=ProcessingCfg(**d.get("processing", {})),
            llm=LLMCfg(**d.get("llm", {})),
            logging=LoggingCfg(**d.get("logging", {})),
            domains=domains,
            feedback_rules=parse_feedback_rules(
                d.get("feedback_rules"), base_dir=base_dir or Path.cwd(),
                default_timezone=d.get("timezone", "Asia/Seoul"),
                allow_placeholders=allow_placeholders,
            ),
        )

    def dynalist_token(self) -> str:
        load_dotenv()
        tok = os.environ.get(self.dynalist.token_env, "")
        if not tok:
            raise RuntimeError(f"env var {self.dynalist.token_env} is not set")
        return tok

    def llm_api_key(self) -> str:
        load_dotenv()
        key = os.environ.get(self.llm.api_key_env, "")
        if not key:
            raise RuntimeError(f"env var {self.llm.api_key_env} is not set")
        return key


def load_settings(path: str | Path | None = None, dotenv_path: str | Path = ".env",
                  *, rules_file: str | Path | None = None, base_dir: str | Path | None = None,
                  allow_placeholders: bool = False) -> Settings:
    load_dotenv(dotenv_path)
    selected_rules = rules_file or os.environ.get("TUTORBOT_FEEDBACK_RULES_FILE")
    ignored_tasks = False
    if path is None:
        s = Settings()
    else:
        with open(path, "rb") as f:
            raw_config = _parse_toml_bytes(f.read())
        raw_feedback = raw_config.get("feedback_rules")
        if selected_rules and isinstance(raw_feedback, dict) and "tasks" in raw_feedback:
            ignored_tasks = bool(raw_feedback["tasks"])
            raw_config["feedback_rules"] = {k: v for k, v in raw_feedback.items() if k != "tasks"}
        s = Settings.model_validate(raw_config,
                                    base_dir=Path(base_dir) if base_dir else Path(path).resolve().parent,
                                    allow_placeholders=allow_placeholders)
    if selected_rules:
        if ignored_tasks:
            log.warning("config.toml feedback_rules.tasks ignored: rules file in use")
        rules_dir = Path(base_dir) if base_dir else Path(selected_rules).resolve().parent
        try:
            with open(selected_rules, "rb") as f:
                raw = _parse_toml_bytes(f.read())
            loaded = parse_feedback_rules(raw.get("feedback_rules"), base_dir=rules_dir,
                                          source=str(selected_rules), default_timezone=s.timezone,
                                          is_rules_file=True, allow_placeholders=allow_placeholders)
        except (OSError, ValueError) as exc:
            loaded = FeedbackRulesCfg(source=str(selected_rules), base_dir=rules_dir)
            loaded.issues.append(ConfigIssue("FR-G007", "error", "feedback_rules",
                                             f"rules file not found, unreadable, or invalid: {selected_rules}"))
            log.debug("rules file could not be loaded: %s", type(exc).__name__)
        for key in ("enabled", "max_llm_calls_per_run", "max_units_per_task",
                    "stale_lock_minutes", "safety_input_token_limit"):
            setattr(loaded, key, getattr(s.feedback_rules, key))
        # The replacement discards config tasks, including errors about the
        # shape or number of those tasks, but preserves scalar/global errors.
        loaded.issues.extend(s.feedback_rules.issues)
        for issue in loaded.issues:
            if issue.code == "FR-G007" and issue.severity == "error":
                log.error("feedback_config_error code=%s path=%s", issue.code, issue.path)
        s.feedback_rules = loaded
    return s
