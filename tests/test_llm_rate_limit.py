from __future__ import annotations

import threading

import pytest

from tutorbot.config import load_settings
from tutorbot.llm.rate_limit import RequestIntervalLimiter


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.parametrize("value", ["0", "-1", "1.5", '"10"', "true"])
def test_requests_per_minute_rejects_non_positive_or_non_integer_toml(tmp_path, value):
    config = tmp_path / "config.toml"
    config.write_text(f"[llm]\nrequests_per_minute = {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"llm.requests_per_minute must be a positive integer"):
        load_settings(config)


def test_requests_per_minute_defaults_and_accepts_positive_int(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[llm]\nmodel_fast = 'example'\n", encoding="utf-8")
    assert load_settings(config).llm.requests_per_minute == 10
    config.write_text("[llm]\nrequests_per_minute = 4\n", encoding="utf-8")
    assert load_settings(config).llm.requests_per_minute == 4
    assert load_settings().llm.requests_per_minute == 10


def test_first_request_is_immediate_then_interval_has_no_burst():
    time = FakeTime()
    limiter = RequestIntervalLimiter(monotonic=time.monotonic, sleep=time.sleep)
    starts = []
    for _ in range(4):
        with limiter.request():
            starts.append(time.monotonic())
    assert starts == [0, 6, 12, 18]
    assert time.sleeps == [6, 6, 6]


def test_concurrent_requests_are_serialized_by_the_same_interval():
    time = FakeTime()
    limiter = RequestIntervalLimiter(monotonic=time.monotonic, sleep=time.sleep)
    starts = []
    barrier = threading.Barrier(8)

    def invoke():
        barrier.wait()
        with limiter.request():
            starts.append(time.monotonic())

    threads = [threading.Thread(target=invoke) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(starts) == [float(i * 6) for i in range(8)]


def test_limiter_fails_instead_of_spinning_on_broken_injected_clock():
    limiter = RequestIntervalLimiter(monotonic=lambda: 0, sleep=lambda _: None)
    with limiter.request():
        pass
    with pytest.raises(RuntimeError, match="clock did not advance"), limiter.request():
        pass
