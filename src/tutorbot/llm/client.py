"""LLM provider abstraction + retry / JSON validation + llm_call logging (spec D6, §6.4, §9)."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .. import clock
from .rate_limit import RateLimiterError, RequestIntervalLimiter
from .schemas import SchemaValidationError

PROMPTS_DIR = Path(__file__).parent / "prompts"
RETRY_BACKOFF = (2, 8)
logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM call or validation error after retries."""


class GeminiRequestError(LLMError):
    """Safe, structured Gemini HTTP/transport failure."""

    def __init__(
        self,
        category: str,
        *,
        status: int | None = None,
        retry_after: str | None = None,
        retry_delay: float | None = None,
        transient: bool = False,
    ) -> None:
        self.category = category
        self.status = status
        self.retry_after = retry_after
        self.retry_delay = retry_delay
        self.transient = transient
        label = f" HTTP {status}" if status is not None else ""
        super().__init__(f"{category}{label}")


class GeminiInvalidResponse(LLMError):
    """Malformed successful Gemini response; permanent and safe to report."""

    def __init__(self) -> None:
        super().__init__("gemini_invalid_response")


_USAGE_TOKEN_FIELDS = (
    "promptTokenCount",
    "candidatesTokenCount",
    "totalTokenCount",
    "cachedContentTokenCount",
    "thoughtsTokenCount",
    "toolUsePromptTokenCount",
)


def _parse_gemini_response(response_body: bytes, status_code: int) -> tuple[str, int, int]:
    """Validate a successful Gemini envelope without retaining or exposing its body."""
    try:
        if not 200 <= status_code < 300:
            raise GeminiInvalidResponse()
        data = json.loads(response_body.decode("utf-8"))
        if not isinstance(data, dict):
            raise GeminiInvalidResponse()

        # Usage metadata is optional. When present it must be an object with at
        # least one recognized count; every supplied count must be a nonnegative int.
        usage = data.get("usageMetadata")
        counts = {field: 0 for field in _USAGE_TOKEN_FIELDS}
        if "usageMetadata" in data:
            if not isinstance(usage, dict):
                raise GeminiInvalidResponse()
            recognized = set(usage).intersection(_USAGE_TOKEN_FIELDS)
            if not recognized:
                raise GeminiInvalidResponse()
            for field in recognized:
                value = usage[field]
                if type(value) is not int or value < 0:
                    raise GeminiInvalidResponse()
                counts[field] = value

        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise GeminiInvalidResponse()
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise GeminiInvalidResponse()
        content = candidate.get("content")
        if not isinstance(content, dict):
            raise GeminiInvalidResponse()
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            raise GeminiInvalidResponse()
        text_parts = []
        for part in parts:
            if not isinstance(part, dict):
                raise GeminiInvalidResponse()
            if "text" in part:
                if not isinstance(part["text"], str):
                    raise GeminiInvalidResponse()
                text_parts.append(part["text"])
        if not text_parts:
            raise GeminiInvalidResponse()
        return (
            "".join(text_parts),
            counts["promptTokenCount"],
            counts["candidatesTokenCount"],
        )
    except GeminiInvalidResponse:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
        raise GeminiInvalidResponse() from None


def _header_value(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:  # noqa: BLE001 - malformed/hostile header implementations are ignored
        return None
    return str(value) if value is not None else None


def _retry_after_seconds(
    value: str | None,
    *,
    wall_clock: Callable[[], datetime],
) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        try:
            seconds = float(value)
            return seconds if math.isfinite(seconds) else None
        except (ValueError, OverflowError):
            return None
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        now = wall_clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        seconds = (retry_at - now).total_seconds()
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _gemini_retry_delay(body: bytes | str) -> float | None:
    try:
        decoded = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
        payload = json.loads(decoded)
        details = payload.get("error", {}).get("details", [])
        if not isinstance(details, list):
            return None
        for detail in details:
            if not isinstance(detail, dict) or not detail.get("@type", "").endswith("google.rpc.RetryInfo"):
                continue
            delay = detail.get("retryDelay")
            if isinstance(delay, str):
                match = re.fullmatch(r"\s*(\d+(?:\.\d{1,9})?)s\s*", delay)
                if match:
                    seconds = float(match.group(1))
                    return seconds if math.isfinite(seconds) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        pass
    return None


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
        requests_per_minute: int = 10,
        limiter: RequestIntervalLimiter | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        http_transport: Callable[..., Any] | None = None,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model_smart = model_smart
        self.model_fast = model_fast
        self.backend = backend
        self.conn = conn
        self.run_id = run_id
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._limiter = limiter or RequestIntervalLimiter(
            requests_per_minute, monotonic=monotonic, sleep=sleep
        )
        self.requests_per_minute = self._limiter.requests_per_minute
        self._http_transport = http_transport or urllib.request.urlopen
        self._limiter_scope_logged = False
        self.stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def model_for_task(self, task: str) -> str:
        t = task.split(":")[0]
        if t in ("t4_grade", "t5_weekly", "t6_profile", "doctor_test"):
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

    def _invoke_once(
        self, task: str, model: str, messages: list[dict[str, str]]
    ) -> tuple[Any, int, int, int | None]:
        if self.backend is not None:
            out = self.backend.complete_json(task.split(":")[0], messages, None)
            return out, 100, 50, None
        if self.provider == "gemini":
            return self._call_gemini(model, messages)
        raise LLMError(f"Unsupported LLM provider: {self.provider}")

    def _call_gemini(
        self, model: str, messages: list[dict[str, str]]
    ) -> tuple[str, int, int, int]:
        if not self._limiter_scope_logged:
            logger.info(
                "gemini_limiter requests_per_minute=%s scope=process_local "
                "coordination=none_across_processes_or_hosts",
                self.requests_per_minute,
            )
            self._limiter_scope_logged = True
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
            # Keep the limiter locked through the actual transport so concurrent callers
            # cannot pass together or reorder their request start timestamps.
            with self._limiter.request(), self._http_transport(req, timeout=60) as resp:
                response_body = resp.read()
                status_code = int(getattr(resp, "status", 200))
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read(65536)
            except Exception:  # noqa: BLE001 - body-read failure must not expose transport details
                err_body = b""
            status = int(e.code)
            retry_after = _header_value(getattr(e, "headers", None), "Retry-After")
            retry_delay = _gemini_retry_delay(err_body)
            category = "rate_limited" if status == 429 else (
                "server_error" if 500 <= status <= 599 else "http_error"
            )
            raise GeminiRequestError(
                category,
                status=status,
                retry_after=retry_after,
                retry_delay=retry_delay,
                transient=(status == 429 or 500 <= status <= 599),
            ) from None
        except RateLimiterError:
            raise LLMError("request_limiter_error") from None
        except LLMError:
            raise
        except Exception:  # noqa: BLE001 - transport exception strings may contain the API key
            # urllib exception strings can contain the API-key URL; never propagate them.
            raise GeminiRequestError("network_error", transient=True) from None

        txt, pt, ct = _parse_gemini_response(response_body, status_code)
        return txt, pt, ct, status_code

    def complete_json(
        self,
        task: str,
        messages: list[dict[str, str]],
        validator: Callable[[Any], Any] | None = None,
        ref: str | None = None,
    ) -> Any:
        """Call LLM with bounded per-payload transient retries and one JSON repair."""
        model = self.model_for_task(task)
        msgs = list(messages)
        for variant in range(2):
            repair = variant == 1
            fallback_index = 0
            for attempt in range(3):
                t0 = self._monotonic()
                try:
                    raw, pt, ct, http_status = self._invoke_once(task, model, msgs)
                except GeminiRequestError as e:
                    latency = int((self._monotonic() - t0) * 1000)
                    status = f" HTTP {e.status}" if e.status is not None else ""
                    safe_error = f"{e.category}{status} attempt={attempt + 1}"
                    self._log_call(task, model, ref, 0, 0, latency, False, safe_error)
                    logger.info(
                        "llm_call task=%s model=%s variant=%s attempt=%s result=error "
                        "category=%s status=%s latency_ms=%s",
                        task, model, "repair" if repair else "prompt", attempt + 1,
                        e.category, e.status, latency,
                    )
                    if not e.transient or attempt == 2:
                        raise LLMError(f"{e.category}{status}") from None
                    if e.status == 429:
                        delay = _retry_after_seconds(e.retry_after, wall_clock=self._wall_clock)
                        if delay is None and e.retry_delay is not None:
                            delay = e.retry_delay
                            wait_source = "server_body"
                        elif delay is not None:
                            wait_source = "server_header"
                        if delay is None:
                            delay = RETRY_BACKOFF[fallback_index]
                            fallback_index += 1
                            wait_source = "fallback"
                    else:
                        delay = RETRY_BACKOFF[attempt]
                        wait_source = "fallback"
                    logger.info(
                        "llm_retry task=%s model=%s variant=%s attempt=%s "
                        "wait_seconds=%s wait_source=%s",
                        task, model, "repair" if repair else "prompt", attempt + 1,
                        delay, wait_source,
                    )
                    self._sleep(delay)
                    continue
                except SchemaValidationError:
                    latency = int((self._monotonic() - t0) * 1000)
                    self._log_call(task, model, ref, 0, 0, latency, False, "backend_schema_error")
                    if repair:
                        raise LLMError("json_validation_failed_after_repair") from None
                    msgs = msgs + [
                        {"role": "user", "content": "이전 응답이 유효한 JSON 스키마가 아닙니다. 규칙에 맞는 JSON으로 다시 출력하세요."}
                    ]
                    break
                except LLMError as e:
                    latency = int((self._monotonic() - t0) * 1000)
                    category = str(e) if str(e) in {
                        "gemini_invalid_response", "request_limiter_error"
                    } else "llm_error"
                    self._log_call(task, model, ref, 0, 0, latency, False, category)
                    if self.backend is None:
                        logger.info(
                            "llm_call task=%s model=%s variant=%s attempt=%s result=error "
                            "category=%s latency_ms=%s",
                            task, model, "repair" if repair else "prompt",
                            attempt + 1, category, latency,
                        )
                    raise LLMError(category) from None
                except Exception:  # noqa: BLE001 - injected backends may raise arbitrary errors
                    latency = int((self._monotonic() - t0) * 1000)
                    self._log_call(task, model, ref, 0, 0, latency, False, "backend_error")
                    # Preserve the injected backend's existing bounded retry behavior,
                    # but never apply network waits or the Gemini HTTP limiter to it.
                    if self.backend is not None and attempt < 2:
                        continue
                    raise LLMError("llm_backend_error") from None

                latency = int((self._monotonic() - t0) * 1000)
                try:
                    parsed = _extract_json(raw)
                    result = validator(parsed) if validator is not None else parsed
                except (SchemaValidationError, ValueError):
                    self._log_call(task, model, ref, pt, ct, latency, False, "json_validation_failed")
                    if http_status is not None:
                        logger.info(
                            "llm_call task=%s model=%s variant=%s attempt=%s result=error "
                            "category=json_validation_failed status=%s latency_ms=%s",
                            task, model, "repair" if repair else "prompt",
                            attempt + 1, http_status, latency,
                        )
                    if repair:
                        raise LLMError("json_validation_failed_after_repair") from None
                    msgs = msgs + [
                        {"role": "user", "content": "이전 응답이 JSON/스키마 검증에 실패했습니다. 규칙에 맞는 JSON만 다시 출력하세요."}
                    ]
                    break
                self._log_call(task, model, ref, pt, ct, latency, True, None)
                if http_status is not None:
                    logger.info(
                        "llm_call task=%s model=%s variant=%s attempt=%s result=ok "
                        "category=success status=%s latency_ms=%s prompt_tokens=%s completion_tokens=%s",
                        task, model, "repair" if repair else "prompt",
                        attempt + 1, http_status, latency, pt, ct,
                    )
                return result
            else:
                # All transient attempts for this payload variant were exhausted.
                raise LLMError("gemini_retry_exhausted") from None
        raise LLMError("json_validation_failed_after_repair") from None
