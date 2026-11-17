"""Trusted audio access injected into provider adapters."""

from collections.abc import Awaitable, Callable

from voice_eval.domain.contracts import AudioInput

AudioLoader = Callable[[AudioInput], Awaitable[bytes]]
