"""Thread-safe no-burst request interval limiter for Gemini HTTP calls."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class RateLimiterError(RuntimeError):
    """The limiter could not safely determine a request start time."""


class RequestIntervalLimiter:
    """Serialize request starts at a fixed interval without accumulating permits."""

    def __init__(
        self,
        requests_per_minute: int = 10,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(requests_per_minute) is not int or requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be a positive integer")
        self.requests_per_minute = requests_per_minute
        self.interval = 60.0 / requests_per_minute
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_start: float | None = None

    @contextmanager
    def request(self) -> Iterator[None]:
        """Hold the lock through transport start/execution to prevent concurrent starts."""
        with self._lock:
            now = self._monotonic()
            if self._last_start is not None:
                delay = self.interval - (now - self._last_start)
                if delay > 0:
                    self._sleep(delay)
                now = self._monotonic()
                # Do not spin forever if an injected clock/sleep pair is broken.
                if now - self._last_start < self.interval:
                    raise RateLimiterError("request limiter clock did not advance after sleep")
            self._last_start = now
            yield
