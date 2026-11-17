"""Small shared HTTP error mapping for provider adapters."""

import httpx

from voice_eval.providers.errors import ProviderError, ProviderErrorCode


def map_http_error(provider_id: str, error: httpx.HTTPError) -> ProviderError:
    request_id = None
    response = getattr(error, "response", None)
    if response is not None:
        request_id = response.headers.get("x-request-id") or response.headers.get("dg-request-id")
        status = response.status_code
        if status == 401 or status == 403:
            code, retryable = ProviderErrorCode.AUTHENTICATION, False
        elif status == 429:
            code, retryable = ProviderErrorCode.RATE_LIMIT, True
        elif status in (400, 404, 413, 422):
            code, retryable = ProviderErrorCode.INVALID_INPUT, False
        elif status >= 500:
            code, retryable = ProviderErrorCode.TRANSIENT_SERVER, True
        else:
            code, retryable = ProviderErrorCode.UNKNOWN, False
        message = f"{provider_id} returned HTTP {status}"
    elif isinstance(error, httpx.TimeoutException):
        code, retryable = ProviderErrorCode.TIMEOUT, True
        message = f"{provider_id} request timed out"
    else:
        code, retryable = ProviderErrorCode.UNKNOWN, True
        message = f"{provider_id} transport request failed"
    return ProviderError(
        code, message, retryable=retryable, provider_id=provider_id, request_id=request_id
    )


def map_connection_error(provider_id: str, error: Exception) -> ProviderError:
    """Normalize WebSocket handshake and transport errors without exposing details."""

    if isinstance(error, TimeoutError):
        return ProviderError(
            ProviderErrorCode.TIMEOUT,
            f"{provider_id} streaming connection timed out",
            retryable=True,
            provider_id=provider_id,
        )
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", {})
    request_id = headers.get("x-request-id") or headers.get("dg-request-id")
    if isinstance(status, int):
        if status in {401, 403}:
            code, retryable = ProviderErrorCode.AUTHENTICATION, False
        elif status == 429:
            code, retryable = ProviderErrorCode.RATE_LIMIT, True
        elif status in {400, 404, 413, 422}:
            code, retryable = ProviderErrorCode.INVALID_INPUT, False
        elif status >= 500:
            code, retryable = ProviderErrorCode.TRANSIENT_SERVER, True
        else:
            code, retryable = ProviderErrorCode.UNKNOWN, False
        message = f"{provider_id} streaming handshake returned HTTP {status}"
    else:
        code, retryable = ProviderErrorCode.UNKNOWN, True
        message = f"{provider_id} streaming transport failed"
    return ProviderError(
        code, message, retryable=retryable, provider_id=provider_id, request_id=request_id
    )
