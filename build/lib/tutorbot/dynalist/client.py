"""Dynalist HTTP client: token buckets, retry with backoff, error classification.

All API field names that the spec marks as "verify in M0" are constants here.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

API_BASE = "https://dynalist.io/api/v1/"
USER_AGENT = "tutorbot/0.1.0"

# --- field-name constants (verify against the real API in M0) -------------
F_PARENT = "parent_id"
F_NODE = "node_id"
F_INDEX = "index"
F_NEW_IDS = "new_node_ids"
INDEX_LAST = -1

RETRY_BACKOFF = (2, 4, 8, 16, 32)
MAX_CHANGES_PER_REQUEST = 100

Transport = Callable[[str, dict], "tuple[int, dict]"]


class DynalistError(Exception):
    def __init__(self, code: str, msg: str = "", status: int = 0):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code, self.msg, self.status = code, msg, status


class RetryableError(DynalistError):
    """Rate limit, 5xx or network trouble."""


class FatalError(DynalistError):
    """InvalidToken and other non-retryable errors."""


class NodeNotFound(FatalError):
    """File or node missing (NotFound / NodeNotFound)."""


class UnknownOutcome(RetryableError):
    """Request may or may not have been applied (timeout). Writer must reconcile."""


def classify(code: str, status: int = 200) -> type[DynalistError]:
    c = (code or "").lower()
    if c == "toomanyrequests" or status == 429 or status >= 500:
        return RetryableError
    if c in ("notfound", "nodenotfound"):
        return NodeNotFound
    return FatalError


def _decode_response(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        return data
    return {"_msg": raw.strip()[:300]} if raw.strip() else {}


class TokenBucket:
    """Refill `per_minute` tokens/min up to `burst`. acquire() blocks via sleep()."""

    def __init__(self, per_minute: float, burst: float,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.rate = per_minute / 60.0
        self.burst = float(burst)
        self.tokens = float(burst)
        self._mono, self._sleep = monotonic, sleep
        self._last = monotonic()

    def _refill(self) -> None:
        now = self._mono()
        self.tokens = min(self.burst, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self, n: float = 1) -> None:
        n = min(n, self.burst)
        self._refill()
        while self.tokens < n:
            self._sleep((n - self.tokens) / self.rate)
            self._refill()
        self.tokens -= n


def _httpx_transport(timeout: float = 30.0) -> Transport:
    try:
        import httpx  # lazy: only needed if installed

        client = httpx.Client(timeout=timeout)

        def post(path: str, body: dict) -> tuple[int, dict]:
            try:
                r = client.post(API_BASE + path, json=body)
            except httpx.TimeoutException as e:
                raise UnknownOutcome("Timeout", str(e)) from e
            except httpx.HTTPError as e:
                raise RetryableError("NetworkError", str(e)) from e
            return r.status_code, _decode_response(r.text)

        return post
    except ModuleNotFoundError:
        import socket
        import urllib.error
        import urllib.request

        def post_urllib(path: str, body: dict) -> tuple[int, dict]:
            req = urllib.request.Request(
                API_BASE + path,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = resp.status
                    raw = resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                status = e.code
                raw = e.read().decode("utf-8", errors="replace")
            except (socket.timeout, TimeoutError) as e:
                raise UnknownOutcome("Timeout", str(e)) from e
            except urllib.error.URLError as e:
                if isinstance(e.reason, (socket.timeout, TimeoutError)):
                    raise UnknownOutcome("Timeout", str(e)) from e
                raise RetryableError("NetworkError", str(e)) from e
            data = _decode_response(raw)
            return status, data

        return post_urllib


class DynalistClient:
    def __init__(self, token: str, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 changes_per_minute: float = 200, max_retries: int = 5):
        self.token = token
        self._transport = transport
        self._sleep = sleep
        self.max_retries = max_retries
        self.read_bucket = TokenBucket(30, 100, monotonic, sleep)
        self.edit_req_bucket = TokenBucket(60, 50, monotonic, sleep)
        self.edit_change_bucket = TokenBucket(changes_per_minute, 500, monotonic, sleep)
        self.stats = {"reads": 0, "changes": 0, "requests": 0}

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = _httpx_transport()
        return self._transport

    def _call(self, path: str, body: dict) -> dict:
        payload = {"token": self.token, **body}
        attempt = 0
        while True:
            self.stats["requests"] += 1
            try:
                status, data = self.transport(path, payload)
                code = str(data.get("_code", "Ok" if status == 200 else f"HTTP{status}"))
                if status == 200 and code.lower() == "ok":
                    return data
                err_cls = classify(code, status)
                err = err_cls(code, str(data.get("_msg", "")), status)
            except UnknownOutcome:
                raise  # caller (writer) must reconcile before retrying
            except RetryableError as e:
                err = e
            if not isinstance(err, RetryableError) or attempt >= self.max_retries:
                raise err
            self._sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
            attempt += 1

    # --- endpoints ---------------------------------------------------------
    def file_list(self) -> list[dict]:
        return self._call("file/list", {}).get("files", [])

    def doc_read(self, file_id: str) -> dict:
        self.read_bucket.acquire()
        self.stats["reads"] += 1
        return self._call("doc/read", {"file_id": file_id})

    def doc_edit(self, file_id: str, changes: list[dict]) -> list[str]:
        """Apply changes (<=100 per request). Returns new_node_ids in insert order."""
        new_ids: list[str] = []
        for i in range(0, len(changes), MAX_CHANGES_PER_REQUEST):
            chunk = changes[i:i + MAX_CHANGES_PER_REQUEST]
            self.edit_req_bucket.acquire()
            self.edit_change_bucket.acquire(len(chunk))
            self.stats["changes"] += len(chunk)
            resp = self._call("doc/edit", {"file_id": file_id, "changes": chunk})
            new_ids.extend(resp.get(F_NEW_IDS) or [])
        return new_ids


# --- change builders (single place for field names) --------------------------
def ch_insert(parent_id: str, content: str, note: str = "", index: int = INDEX_LAST,
              checked: bool = False) -> dict:
    d: dict[str, Any] = {"action": "insert", F_PARENT: parent_id, F_INDEX: index, "content": content}
    if note:
        d["note"] = note
    if checked:
        d["checked"] = True
    return d


def ch_edit(node_id: str, **fields: Any) -> dict:
    return {"action": "edit", F_NODE: node_id, **fields}


def ch_move(node_id: str, parent_id: str, index: int = INDEX_LAST) -> dict:
    return {"action": "move", F_NODE: node_id, F_PARENT: parent_id, F_INDEX: index}


def ch_delete(node_id: str) -> dict:
    return {"action": "delete", F_NODE: node_id}
