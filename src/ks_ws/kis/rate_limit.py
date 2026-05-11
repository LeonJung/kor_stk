"""Token-bucket rate limiter for KIS REST with sliding-window safety.

KIS allows roughly 2 requests/second on the mock environment and 20
requests/second on live. Going over earns a 500 from the server (the
mock environment is particularly strict — observed 500s on
back-to-back calls).

KIS uses a **sliding 1-second window** — burst at the second boundary
violates even when the average is within limit. We mitigate by keeping
the **token-bucket capacity smaller than the per-second rate** so a
burst can't immediately consume more than `safety_factor × rate` tokens
in one moment.

Defaults (env var ``KIS_RATE_PER_SEC`` overrides):
- mock: 2 req/s (KIS doesn't publish; observed empirically)
- live: 15 req/s (KIS official 20, with 25% headroom for sliding-window)

Multi-key support: ``get_limiter(env, key="account_a")`` keeps a separate
limiter per (env, key). Useful when a single process holds credentials
for multiple KIS accounts (todo: 5/11-12 multi-account support).
"""

import os
import threading
import time

# Default per-second rates per KIS environment. Override with env var
# ``KIS_RATE_PER_SEC`` (applies to all envs). Live default = 15 (with
# 25% headroom under KIS official 20 to survive sliding-window bursts).
_DEFAULT_RATES: dict[str, float] = {
    "mock": float(os.environ.get("KIS_RATE_PER_SEC", "2.0")),
    "live": float(os.environ.get("KIS_RATE_PER_SEC", "15.0")),
}

# Capacity = how many tokens the bucket can hold; keep <= rate to prevent
# burst at second boundary from violating sliding window.
_CAPACITY_RATIO = 0.7  # 70% of rate


class RateLimiter:
    """Token-bucket: capacity tokens, refilled at `rate` per second.

    Default capacity = max(1, int(rate × 0.7)) — sliding-window safety so
    burst at second boundary doesn't violate KIS's policy.
    """

    def __init__(self, rate_per_sec: float, capacity: int | None = None) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate = rate_per_sec
        self.capacity = capacity if capacity is not None else max(1, int(rate_per_sec * _CAPACITY_RATIO))
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


_LIMITERS: dict[tuple[str, str], RateLimiter] = {}
_REGISTRY_LOCK = threading.Lock()


def get_limiter(env: str, key: str = "default") -> RateLimiter:
    """Return a process-wide singleton limiter for (env, key).

    ``key`` defaults to "default" (single-account use). Pass account/app-key
    identifier to maintain a separate limiter per account when running
    multiple keys from the same process (5/11-12 multi-account capture).
    """
    with _REGISTRY_LOCK:
        ck = (env, key)
        if ck not in _LIMITERS:
            _LIMITERS[ck] = RateLimiter(_DEFAULT_RATES.get(env, 2.0))
        return _LIMITERS[ck]


def reset_for_tests() -> None:
    """Test helper — drop the registry so each test starts fresh."""
    with _REGISTRY_LOCK:
        _LIMITERS.clear()
