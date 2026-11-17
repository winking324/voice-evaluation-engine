"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Voice Evaluation Engine"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    openai_api_key: SecretStr | None = None
    deepgram_api_key: SecretStr | None = None
    data_root: Path = Path("data")
    database_path: Path = Path("data/runs/voice-evaluation.sqlite3")
    worker_queue_capacity: int = Field(default=32, gt=0, le=1000)
    max_pending_runs: int = Field(default=32, gt=0, le=1000)
    max_samples_per_run: int = Field(default=100, gt=0, le=10000)
    max_worker_concurrency: int = Field(default=16, gt=0, le=64)


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process; tests can clear this cache when needed."""

    return Settings()
