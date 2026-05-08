"""Sanity-check that .env is populated correctly and that the configured
APP_KEY / APP_SECRET can actually obtain a KIS access token.

Run after copying .env.example to .env and filling in the values:

    cp .env.example .env  # then edit .env
    uv run examples/verify_kis_token.py

Output redacts the secret token to its first/last few chars.
"""

from datetime import UTC, datetime

from ks_ws.auth.token import _read_cache, get_token
from ks_ws.config import get_settings


def _redact(s: str, head: int = 6, tail: int = 4) -> str:
    if len(s) <= head + tail:
        return "*" * len(s)
    return f"{s[:head]}...{s[-tail:]}"


def main() -> None:
    settings = get_settings()

    print("=== KIS settings ===")
    print(f"  env:        {settings.env}")
    print(f"  app_key:    {_redact(settings.app_key)}")
    print(f"  app_secret: {_redact(settings.app_secret)}")
    print(f"  account:    {settings.account_cano}-{settings.account_prdt}")
    if settings.hts_id:
        print(f"  hts_id:     {settings.hts_id}")

    print("\n=== Issuing token ===")
    token = get_token(settings)
    print(f"  token: {_redact(token, head=8, tail=4)} (len={len(token)})")

    cached = _read_cache(settings.env)
    if cached is not None:
        remaining = (cached.expires_at - datetime.now(UTC)).total_seconds()
        print(f"  expires:   {cached.expires_at.isoformat()}")
        print(f"  remaining: {remaining / 3600:.1f} hours")

    print("\n=== Re-issuing (should hit disk cache) ===")
    token2 = get_token(settings)
    print(f"  cache hit: {token == token2}")


if __name__ == "__main__":
    main()
