"""Settings loaded from config.toml and .env. Supports pydantic/tomli if installed, with stdlib fallback."""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


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


def _simple_toml_parse(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    cur = root
    for raw in text.splitlines():
        line = _strip_inline_comment(raw)
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            sec = line[1:-1].strip()
            cur = root
            for name in sec.split("."):
                child = cur.setdefault(name, {})
                if not isinstance(child, dict):
                    raise ValueError(f"TOML table conflicts with a value: {sec}")
                cur = child
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = _parse_val(v.strip())
    return root


@dataclass
class DynalistCfg:
    token_env: str = "DYNALIST_TOKEN"
    file_ids: list[str] = field(default_factory=list)


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

    @classmethod
    def model_validate(cls, d: dict[str, Any]) -> "Settings":
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


def load_settings(path: str | Path | None = None, dotenv_path: str | Path = ".env") -> Settings:
    load_dotenv(dotenv_path)
    if path is None:
        s = Settings()
    else:
        with open(path, "rb") as f:
            s = Settings.model_validate(_parse_toml_bytes(f.read()))
    if not s.dynalist.file_ids and os.environ.get("DYNALIST_FILE_ID"):
        s.dynalist.file_ids = [os.environ["DYNALIST_FILE_ID"]]
    return s
