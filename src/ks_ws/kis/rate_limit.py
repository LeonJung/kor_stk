"""Token-bucket rate limiter for KIS REST.

KIS allows roughly 2 requests/second on the mock environment and 20
requests/second on live. Going over earns a 500 from the server (the
mock environment is particularly strict — observed 500s on
back-to-back calls). This limiter is installed as an httpx event hook
in ``kis.http.make_client`` so every request transparently waits its
turn.

Implementation: a single-process, thread-safe token bucket. Multi-
process coordination (Redis-style) is a future concern; for one
trading agent on one machine this suffices.
"""

import threading
import time

# Default per-second rates per KIS environment.
_DEFAULT_RATES: dict[str, float] = {
    "mock": 2.0,
    "live": 20.0,
}


class RateLimiter:
    """Token-bucket: capacity tokens, refilled at `rate` per second."""

    def __init__(self, rate_per_sec: float, capacity: int | None = None) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate = rate_per_sec
        self.capacity = capacity if capacity is not None else max(1, int(rate_per_sec))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until at least one token is available, then consume it."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            wait = (1 - self._tokens) / self.rate
        # Release lock while sleeping so other threads (if any) can refill.
        time.sleep(wait)
        with self._lock:
            self._tokens = max(0.0, self._tokens - 1 + wait * self.rate)
            self._last = time.monotonic()


_LIMITERS: dict[str, RateLimiter] = {}
_REGISTRY_LOCK = threading.Lock()


def get_limiter(env: str) -> RateLimiter:
    """Return a process-wide singleton limiter for the named environment.
    Currently keyed by the env name; an account-specific limiter can be
    added later if multiple accounts share a process.
    """
    with _REGISTRY_LOCK:
        if env not in _LIMITERS:
            _LIMITERS[env] = RateLimiter(_DEFAULT_RATES.get(env, 2.0))
        return _LIMITERS[env]


def reset_for_tests() -> None:
    """Test helper — drop the registry so each test starts fresh."""
    with _REGISTRY_LOCK:
        _LIMITERS.clear()
