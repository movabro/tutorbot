"""LLM provider abstraction + retry / JSON validation + llm_call logging (spec D6, §6.4, §9)."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .. import clock
from .schemas import SchemaValidationError

PROMPTS_DIR = Path(__file__).parent / "prompts"
RETRY_BACKOFF = (2, 8)


class LLMError(Exception):
    """LLM call or validation error after retries."""


def load_prompt(name: str) -> str:
    p = PROMPTS_DIR / f"{name}.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def _extract_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        raise SchemaValidationError(f"Expected JSON string or dict, got {type(raw).__name__}")
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except ValueError as e:
        raise SchemaValidationError(f"Invalid JSON: {e}") from e


class LLMClient:
    """Wraps a real provider (Gemini HTTP) or an injected backend (e.g. FakeLLM)."""

    def __init__(
        self,
        *,
        provider: str = "gemini",
        api_key: str = "",
        model_smart: str = "gemini-3.5-flash-lite",
        model_fast: str = "gemini-3.5-flash-lite",
        backend: Any | None = None,
        conn: sqlite3.Connection | None = None,
        run_id: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model_smart = model_smart
        self.model_fast = model_fast
        self.backend = backend
        self.conn = conn
        self.run_id = run_id
        self._sleep = sleep
        self.stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def model_for_task(self, task: str) -> str:
        t = task.split(":")[0]
        if t in ("t4_grade", "t5_weekly", "t6_profile"):
            return self.model_fast or "gemini-3.5-flash-lite"
        return self.model_smart or "gemini-3.5-flash-lite"

    def _log_call(
        self,
        task: str,
        model: str,
        ref: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        ok: bool,
        error: str | None = None,
    ) -> None:
        self.stats["calls"] += 1
        self.stats["prompt_tokens"] += prompt_tokens
        self.stats["completion_tokens"] += completion_tokens
        if self.conn is None:
            return
        self.conn.execute(
            "INSERT INTO llm_call(run_id,task,model,ref,prompt_tokens,completion_tokens,latency_ms,ok,error,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id,
                task,
                model,
                ref,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                1 if ok else 0,
                error,
                clock.now_iso(),
            ),
        )

    def _invoke_once(self, task: str, model: str, messages: list[dict[str, str]]) -> tuple[Any, int, int]:
        if self.backend is not None:
            out = self.backend.complete_json(task.split(":")[0], messages, None)
            return out, 100, 50
        if self.provider == "gemini":
            return self._call_gemini(model, messages)
        raise LLMError(f"Unsupported LLM provider: {self.provider}")

    def _call_gemini(self, model: str, messages: list[dict[str, str]]) -> tuple[str, int, int]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
        sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
        user_parts = [m["content"] for m in messages if m.get("role") != "system"]
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": "\n\n".join(user_parts)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        }
        if sys_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(sys_parts)}]}
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {err_body[:300]}") from e
        except Exception as e:
            raise RuntimeError(f"Network error: {e}") from e

        usage = data.get("usageMetadata") or {}
        pt = int(usage.get("promptTokenCount") or 0)
        ct = int(usage.get("candidatesTokenCount") or 0)
        try:
            txt = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Malformed Gemini response: {data}") from e
        return txt, pt, ct

    def complete_json(
        self,
        task: str,
        messages: list[dict[str, str]],
        validator: Callable[[Any], Any] | None = None,
        ref: str | None = None,
    ) -> Any:
        """Call LLM with up to 2 network retries (2s, 8s) and 1 JSON repair retry on validation failure (spec §6.4)."""
        model = self.model_for_task(task)
        msgs = list(messages)
        repaired = False

        while True:
            attempt = 0
            while True:
                t0 = time.monotonic()
                try:
                    raw, pt, ct = self._invoke_once(task, model, msgs)
                    latency = int((time.monotonic() - t0) * 1000)
                    break
                except SchemaValidationError as e:
                    latency = int((time.monotonic() - t0) * 1000)
                    self._log_call(task, model, ref, 0, 0, latency, False, str(e))
                    if not repaired:
                        repaired = True
                        msgs = msgs + [
                            {
                                "role": "user",
                                "content": f"이전 응답이 JSON 스키마 검증에 실패했습니다 ({e}). 유효한 JSON으로 다시 출력하세요.",
                            }
                        ]
                        continue
                    raise LLMError(str(e)) from e
                except Exception as e:
                    latency = int((time.monotonic() - t0) * 1000)
                    self._log_call(task, model, ref, 0, 0, latency, False, str(e))
                    if attempt >= len(RETRY_BACKOFF):
                        raise LLMError(str(e)) from e
                    self._sleep(RETRY_BACKOFF[attempt])
                    attempt += 1

            try:
                parsed = _extract_json(raw)
                result = validator(parsed) if validator is not None else parsed
                self._log_call(task, model, ref, pt, ct, latency, True, None)
                return result
            except (SchemaValidationError, ValueError) as e:
                self._log_call(task, model, ref, pt, ct, latency, False, f"validation: {e}")
                if not repaired:
                    repaired = True
                    msgs = msgs + [
                        {
                            "role": "user",
                            "content": f"응답 JSON 검증 오류: {e}. 규칙에 맞게 수정된 JSON만 다시 출력하세요.",
                        }
                    ]
                    continue
                raise LLMError(f"JSON validation failed after repair: {e}") from e
