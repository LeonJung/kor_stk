from ks_ws.config import Settings, get_settings


def test_loads_env_vars():
    s = Settings()
    assert s.env == "mock"
    assert s.app_key == "test_app_key"
    assert s.app_secret == "test_app_secret"
    assert s.account_cano == "12345678"
    assert s.account_prdt == "01"


def test_get_settings_is_cached():
    a = get_settings()
    b = get_settings()
    assert a is b


def test_env_override(monkeypatch):
    monkeypatch.setenv("KIS_ENV", "live")
    get_settings.cache_clear()
    assert get_settings().env == "live"
