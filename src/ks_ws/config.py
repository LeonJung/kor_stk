from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="KIS_",
        case_sensitive=False,
        extra="ignore",
    )

    env: Literal["mock", "live"] = "mock"
    app_key: str
    app_secret: str
    account_cano: str
    account_prdt: str = "01"
    hts_id: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
