import pytest


@pytest.fixture(autouse=True)
def _kis_env(monkeypatch):
    """Inject test KIS credentials so Settings() can instantiate without a real .env."""
    monkeypatch.setenv("KIS_ENV", "mock")
    monkeypatch.setenv("KIS_APP_KEY", "test_app_key")
    monkeypatch.setenv("KIS_APP_SECRET", "test_app_secret")
    monkeypatch.setenv("KIS_ACCOUNT_CANO", "12345678")
    monkeypatch.setenv("KIS_ACCOUNT_PRDT", "01")
    monkeypatch.setenv("KIS_HTS_ID", "")
    from ks_ws.config import get_settings

    get_settings.cache_clear()
