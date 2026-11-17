import pytest
from pydantic import SecretStr

from voice_eval.config import Settings


def test_provider_keys_are_loaded_as_redacted_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # Supported by pydantic-settings at runtime.

    assert isinstance(settings.openai_api_key, SecretStr)
    assert settings.openai_api_key.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings)
