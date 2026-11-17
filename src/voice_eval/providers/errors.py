"""Provider failures normalized for retry and reporting policy."""

from enum import StrEnum


class ProviderErrorCode(StrEnum):
    AUTHENTICATION = "authentication"
    INVALID_INPUT = "invalid_input"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    TRANSIENT_SERVER = "transient_server"
    UNSUPPORTED_FEATURE = "unsupported_feature"
    UNKNOWN = "unknown"


class ProviderError(Exception):
    """A provider failure with explicit retryability and request correlation."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool,
        provider_id: str,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.provider_id = provider_id
        self.request_id = request_id
