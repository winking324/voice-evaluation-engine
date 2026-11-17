"""Environment-backed application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Voice Evaluation Engine"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    openai_api_key: SecretStr | None = None
    deepgram_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process; tests can clear this cache when needed."""

    return Settings()
