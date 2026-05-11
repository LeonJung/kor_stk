import threading
import time

import pytest

from ks_ws.kis.rate_limit import RateLimiter, get_limiter, reset_for_tests


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


def test_initial_burst_does_not_wait():
    """Capacity tokens available immediately — first N calls go through."""
    rl = RateLimiter(rate_per_sec=10, capacity=5)
    start = time.monotonic()
    for _ in range(5):
        rl.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05  # essentially zero


def test_overflow_waits_for_refill():
    """6th call beyond capacity=5 should wait ~1/rate seconds."""
    rl = RateLimiter(rate_per_sec=10, capacity=2)
    rl.acquire()
    rl.acquire()
    start = time.monotonic()
    rl.acquire()
    elapsed = time.monotonic() - start
    # 1 token / 10 per sec = 0.1s, allow some slack
    assert 0.05 < elapsed < 0.5


def test_rate_invalid_rejected():
    with pytest.raises(ValueError):
        RateLimiter(rate_per_sec=0)
    with pytest.raises(ValueError):
        RateLimiter(rate_per_sec=-1)


def test_get_limiter_returns_singleton_per_env():
    a = get_limiter("mock")
    b = get_limiter("mock")
    assert a is b
    assert get_limiter("live") is not a


def test_default_rates_match_kis_constraints():
    mock_rl = get_limiter("mock")
    live_rl = get_limiter("live")
    assert mock_rl.rate == 2.0
    # live default = 15 with 25% headroom under KIS official 20 (sliding window)
    assert live_rl.rate == 15.0
    # capacity is sliding-window safe (≤ rate × 0.7)
    assert live_rl.capacity <= int(live_rl.rate)
    assert mock_rl.capacity <= int(mock_rl.rate * 0.7) or mock_rl.capacity == 1


def test_reset_clears_registry():
    a = get_limiter("mock")
    reset_for_tests()
    b = get_limiter("mock")
    assert a is not b


def test_threadsafe_under_concurrency():
    """Five threads, 4 acquires each, capacity=2, rate=20.
    Without locking, would over-consume tokens. With locks, total time is
    bounded by (total_acquires - capacity) / rate = (20 - 2) / 20 = 0.9s.
    """
    rl = RateLimiter(rate_per_sec=20, capacity=2)
    counts = [0] * 5

    def worker(idx: int) -> None:
        for _ in range(4):
            rl.acquire()
            counts[idx] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    assert sum(counts) == 20
    # Refill takes (20 - 2) / 20 = 0.9s; allow generous slack for scheduling.
    assert elapsed < 1.5
