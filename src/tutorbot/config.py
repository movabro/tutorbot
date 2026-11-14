"""Settings loaded from config.toml and .env. Supports pydantic/tomli if installed, with stdlib fallback."""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
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
        return _toml.loads(data.decode("utf-8"))
    except ModuleNotFoundError:
        try:
            import tomli as _toml  # type: ignore[import-not-found]
            return _toml.loads(data.decode("utf-8"))
        except ModuleNotFoundError:
            return _simple_toml_parse(data.decode("utf-8"))


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
            cur = root.setdefault(sec, {})
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


@dataclass
class Settings:
    timezone: str = "Asia/Seoul"
    db_path: str = "data/tutorbot.sqlite3"
    dynalist: DynalistCfg = field(default_factory=DynalistCfg)
    tags: TagsCfg = field(default_factory=TagsCfg)
    quiz: QuizCfg = field(default_factory=QuizCfg)
    processing: ProcessingCfg = field(default_factory=ProcessingCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)

    @classmethod
    def model_validate(cls, d: dict[str, Any]) -> "Settings":
        return cls(
            timezone=d.get("timezone", "Asia/Seoul"),
            db_path=d.get("db_path", "data/tutorbot.sqlite3"),
            dynalist=DynalistCfg(**d.get("dynalist", {})),
            tags=TagsCfg(**d.get("tags", {})),
            quiz=QuizCfg(**d.get("quiz", {})),
            processing=ProcessingCfg(**d.get("processing", {})),
            llm=LLMCfg(**d.get("llm", {})),
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
