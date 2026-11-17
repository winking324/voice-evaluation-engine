from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from functools import wraps
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from voice_eval.domain.contracts import (
    AudioFrame,
    AudioInput,
    StreamingTranscriptionOptions,
    TranscriptionOptions,
)
from voice_eval.providers._http import map_connection_error
from voice_eval.providers.deepgram import (
    DeepgramProvider,
    DeepgramStreamingProvider,
    DeepgramStreamingSession,
)
from voice_eval.providers.errors import ProviderError, ProviderErrorCode
from voice_eval.providers.openai import (
    OpenAIProvider,
    OpenAIRealtimeProvider,
    OpenAIRealtimeSession,
)

KEY = SecretStr("test-secret-key")
AUDIO = AudioInput(
    storage_key="samples/hello.wav",
    sha256="a" * 64,
    duration_ms=1200,
    media_type="audio/wav",
    sample_rate_hz=24000,
    channels=1,
)


def async_test(function: Callable[..., Coroutine[object, object, None]]) -> Callable[..., None]:
    @wraps(function)
    def run(*args: object, **kwargs: object) -> None:
        asyncio.run(function(*args, **kwargs))

    return run


async def load_audio(_: AudioInput) -> bytes:
    return b"fake-audio-bytes"


def response(payload: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(200, json=payload, headers=headers)


@async_test
async def test_openai_batch_maps_request_and_preserves_raw_text_and_request_id() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response({"text": "May 5th, 2026."}, {"x-request-id": "req-openai"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(KEY, load_audio, client=client)
    result = await provider.transcribe(
        AUDIO,
        TranscriptionOptions(
            model="gpt-transcribe",
            language="en",
            provider_options={"prompt": "dictation", "temperature": 0},
        ),
    )

    request = requests[0]
    body = request.content.decode()
    assert request.url.path == "/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer test-secret-key"
    assert 'name="model"' in body and "gpt-transcribe" in body
    assert 'name="language"' in body and "en" in body
    assert 'name="prompt"' in body and "dictation" in body
    assert 'filename="audio.wav"' in body
    assert "fake-audio-bytes" in body
    assert result.text == "May 5th, 2026."
    assert result.request_id == "req-openai"
    await client.aclose()


@async_test
async def test_openai_http_failure_maps_code_request_id_and_hides_secret() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"x-request-id": "req-rate"}, text="secret-bearing body")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(KEY, load_audio, client=client)

    with pytest.raises(ProviderError) as raised:
        await provider.transcribe(AUDIO, TranscriptionOptions(model="gpt-transcribe"))

    assert raised.value.code == ProviderErrorCode.RATE_LIMIT
    assert raised.value.request_id == "req-rate"
    assert "test-secret-key" not in str(raised.value)
    assert "secret-bearing body" not in str(raised.value)
    await client.aclose()


@async_test
async def test_openai_explicit_whisper_maps_segment_timestamps() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(
            {
                "text": "hello",
                "segments": [{"text": "hello", "start": 0.2, "end": 0.7}],
            }
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(KEY, load_audio, client=client)
    result = await provider.transcribe(
        AUDIO,
        TranscriptionOptions(model="whisper-1", timestamps=True),
    )

    body = requests[0].content.decode()
    assert 'name="response_format"' in body and "verbose_json" in body
    assert 'name="timestamp_granularities[]"' in body and "segment" in body
    assert [(segment.text, segment.start_ms, segment.end_ms) for segment in result.segments] == [
        ("hello", 200, 700)
    ]
    await client.aclose()


@async_test
async def test_openai_realtime_maps_interim_and_completed_events_and_closes() -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.messages = [
                json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.delta",
                        "item_id": "item-1",
                        "delta": "hello",
                    }
                ),
                json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "item-1",
                        "transcript": "Hello there.",
                    }
                ),
            ]
            self.closed = False
            self.response = type(
                "HandshakeResponse", (), {"headers": {"x-request-id": "openai-ws"}}
            )()

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def recv(self) -> str:
            return json.dumps({"type": "session.updated"})

        def __aiter__(self) -> AsyncIterator[str]:
            async def iterate() -> AsyncIterator[str]:
                for item in self.messages:
                    yield item

            return iterate()

        async def close(self) -> None:
            self.closed = True

    socket = Socket()
    connection: dict[str, Any] = {}

    async def connect(url: str, **kwargs: Any) -> Socket:
        connection.update(url=url, **kwargs)
        return socket

    provider = OpenAIRealtimeProvider(KEY, connect=connect)
    session = await provider.open_session(
        StreamingTranscriptionOptions(
            model="gpt-live-transcribe",
            sample_rate_hz=24000,
            encoding="pcm16",
        )
    )
    await session.send_audio(AudioFrame(sequence=0, data=b"pcm", duration_ms=20))
    await session.finish_audio()
    events = [event async for event in session.events()]
    await session.aclose()

    assert connection["url"].endswith("intent=transcription")
    assert connection["additional_headers"]["Authorization"] == "Bearer test-secret-key"
    update = json.loads(socket.sent[0])
    assert update["session"]["audio"]["input"]["transcription"]["model"] == "gpt-live-transcribe"
    assert json.loads(socket.sent[1])["type"] == "input_audio_buffer.append"
    assert json.loads(socket.sent[2])["type"] == "input_audio_buffer.commit"
    assert [(event.text, event.is_final, event.segment_id) for event in events] == [
        ("hello", False, "item-1"),
        ("Hello there.", True, "item-1"),
    ]
    assert events[0].is_terminal is False
    assert events[1].is_terminal is True
    assert events[1].request_id == "openai-ws"
    assert socket.closed


@async_test
async def test_openai_realtime_rejects_unsupported_audio_format() -> None:
    provider = OpenAIRealtimeProvider(KEY, connect=lambda *_args, **_kwargs: None)
    with pytest.raises(ProviderError) as raised:
        await provider.open_session(
            StreamingTranscriptionOptions(
                model="gpt-live-transcribe", sample_rate_hz=16000, encoding="pcm16"
            )
        )
    assert raised.value.code == ProviderErrorCode.UNSUPPORTED_FEATURE


@async_test
async def test_openai_realtime_setup_errors_close_socket_and_are_typed() -> None:
    class Socket:
        def __init__(self, failure: str) -> None:
            self.failure = failure
            self.closed = False

        async def send(self, _: str) -> None:
            if self.failure == "send":
                raise OSError("private transport detail")

        async def recv(self) -> str:
            if self.failure == "receive":
                raise OSError("private transport detail")
            if self.failure == "json":
                return "{invalid json"
            if self.failure == "timeout":
                await asyncio.sleep(0.02)
            return json.dumps({"type": "session.updated"})

        async def close(self) -> None:
            self.closed = True

    for failure in ("send", "receive", "json", "timeout"):
        socket = Socket(failure)

        async def connect(*_args: Any, _socket: Socket = socket, **_kwargs: Any) -> Socket:
            return _socket

        provider = OpenAIRealtimeProvider(KEY, connect=connect, timeout_seconds=0.001)
        with pytest.raises(ProviderError) as raised:
            await provider.open_session(
                StreamingTranscriptionOptions(
                    model="gpt-live-transcribe", sample_rate_hz=24000, encoding="pcm16"
                )
            )
        assert socket.closed
        assert "private transport detail" not in str(raised.value)


@async_test
async def test_openai_events_convert_malformed_and_unexpected_disconnects() -> None:
    class Socket:
        def __init__(self, messages: list[str], disconnect: bool = False) -> None:
            self.messages = messages
            self.disconnect = disconnect

        async def send(self, _: str) -> None:
            pass

        def __aiter__(self) -> AsyncIterator[str]:
            async def iterate() -> AsyncIterator[str]:
                for message in self.messages:
                    yield message
                if self.disconnect:
                    raise OSError("private socket detail")

            return iterate()

        async def close(self) -> None:
            pass

    for socket in (
        Socket(["{invalid json"]),
        Socket([json.dumps({"type": "event", "request_id": "openai-known"})], disconnect=True),
    ):
        session = OpenAIRealtimeSession(socket, interim_results=True)
        await session.finish_audio()
        with pytest.raises(ProviderError) as raised:
            _ = [event async for event in session.events()]
        assert "private socket detail" not in str(raised.value)
        if socket.disconnect:
            assert raised.value.request_id == "openai-known"


@async_test
async def test_deepgram_batch_maps_query_and_word_timings() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(
            {
                "metadata": {"request_id": "dg-req", "duration": 1.2},
                "results": {
                    "channels": [
                        {
                            "alternatives": [
                                {
                                    "transcript": "hello world",
                                    "confidence": 0.98,
                                    "words": [
                                        {
                                            "word": "hello",
                                            "start": 0.1,
                                            "end": 0.4,
                                            "confidence": 0.99,
                                        },
                                        {
                                            "word": "world",
                                            "start": 0.5,
                                            "end": 0.9,
                                            "confidence": 0.97,
                                        },
                                    ],
                                }
                            ]
                        }
                    ]
                },
            }
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepgramProvider(KEY, load_audio, client=client)
    result = await provider.transcribe(
        AUDIO,
        TranscriptionOptions(
            model="nova-3",
            language="en-US",
            timestamps=True,
            provider_options={"keyterm": ["OpenAI", "Deepgram"], "punctuate": True},
        ),
    )

    request = requests[0]
    params = parse_qs(urlparse(str(request.url)).query)
    assert params["model"] == ["nova-3"]
    assert params["language"] == ["en-US"]
    assert params["utterances"] == ["true"]
    assert params["keyterm"] == ["OpenAI", "Deepgram"]
    assert request.headers["authorization"] == "Token test-secret-key"
    assert request.content == b"fake-audio-bytes"
    assert result.text == "hello world"
    assert result.request_id == "dg-req"
    assert result.provider_duration_ms is None
    assert result.usage.billed_audio_duration_ms is None
    assert [(segment.text, segment.start_ms, segment.end_ms) for segment in result.segments] == [
        ("hello", 100, 400),
        ("world", 500, 900),
    ]
    await client.aclose()


@async_test
async def test_deepgram_stream_maps_interim_final_and_speech_final_and_closes() -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[bytes | str] = []
            self.closed = False
            self.messages = [
                json.dumps(
                    {
                        "type": "Results",
                        "start": 0.1,
                        "duration": 0.3,
                        "is_final": False,
                        "speech_final": False,
                        "channel": {"alternatives": [{"transcript": "hello", "confidence": 0.8}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "Results",
                        "start": 0.1,
                        "duration": 0.4,
                        "is_final": True,
                        "speech_final": True,
                        "channel": {
                            "alternatives": [{"transcript": "hello there", "confidence": 0.9}]
                        },
                    }
                ),
                json.dumps({"type": "Metadata", "request_id": "dg-stream-request"}),
            ]

        async def send(self, message: bytes | str) -> None:
            self.sent.append(message)

        def __aiter__(self) -> AsyncIterator[str]:
            async def iterate() -> AsyncIterator[str]:
                for item in self.messages:
                    yield item

            return iterate()

        async def close(self) -> None:
            self.closed = True

    socket = Socket()
    connection: dict[str, Any] = {}

    async def connect(url: str, **kwargs: Any) -> Socket:
        connection.update(url=url, **kwargs)
        return socket

    provider = DeepgramStreamingProvider(KEY, connect=connect)
    session = await provider.open_session(
        StreamingTranscriptionOptions(
            model="nova-3",
            sample_rate_hz=16000,
            encoding="linear16",
            interim_results=True,
            endpointing_ms=350,
            provider_options={"keyterm": ["OpenAI", "Deepgram"]},
        )
    )
    await session.send_audio(AudioFrame(sequence=0, data=b"pcm", duration_ms=20))
    await session.finish_audio()
    events = [event async for event in session.events()]
    await session.aclose()

    params = parse_qs(urlparse(connection["url"]).query)
    assert params["model"] == ["nova-3"]
    assert params["interim_results"] == ["true"]
    assert params["endpointing"] == ["350"]
    assert params["keyterm"] == ["OpenAI", "Deepgram"]
    assert connection["additional_headers"]["Authorization"] == "Token test-secret-key"
    assert socket.sent[0] == b"pcm"
    assert json.loads(socket.sent[1]) == {"type": "CloseStream"}
    assert [event.is_final for event in events] == [False, True, False]
    assert [event.speech_final for event in events] == [False, True, None]
    assert [event.is_terminal for event in events] == [False, False, True]
    assert events[-1].request_id == "dg-stream-request"
    assert events[1].start_ms == 100 and events[1].end_ms == 500
    assert events[1].speech_final is True
    assert len(socket.sent) == 2
    assert socket.closed


@async_test
async def test_deepgram_events_convert_malformed_and_unexpected_disconnects() -> None:
    class Socket:
        def __init__(self, messages: list[str], disconnect: bool = False) -> None:
            self.messages = messages
            self.disconnect = disconnect

        async def send(self, _: str) -> None:
            pass

        def __aiter__(self) -> AsyncIterator[str]:
            async def iterate() -> AsyncIterator[str]:
                for message in self.messages:
                    yield message
                if self.disconnect:
                    raise OSError("private socket detail")

            return iterate()

        async def close(self) -> None:
            pass

    for socket in (
        Socket(["{invalid json"]),
        Socket([json.dumps({"type": "Metadata", "request_id": "dg-known"})], disconnect=True),
    ):
        session = DeepgramStreamingSession(socket)
        await session.finish_audio()
        with pytest.raises(ProviderError) as raised:
            _ = [event async for event in session.events()]
        assert "private socket detail" not in str(raised.value)
        if socket.disconnect:
            assert raised.value.request_id == "dg-known"


@async_test
async def test_deepgram_http_failure_maps_status_request_id_and_hides_key() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"dg-request-id": "dg-auth"}, text="test-secret-key")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepgramProvider(KEY, load_audio, client=client)
    with pytest.raises(ProviderError) as raised:
        await provider.transcribe(AUDIO, TranscriptionOptions(model="nova-3"))
    assert raised.value.code == ProviderErrorCode.AUTHENTICATION
    assert raised.value.request_id == "dg-auth"
    assert "test-secret-key" not in str(raised.value)
    await client.aclose()


def test_streaming_handshake_failure_preserves_request_id() -> None:
    class Failure(Exception):
        response = type(
            "HandshakeResponse",
            (),
            {"status_code": 403, "headers": {"dg-request-id": "dg-handshake"}},
        )()

    error = map_connection_error("deepgram", Failure("sensitive details"))
    assert error.code == ProviderErrorCode.AUTHENTICATION
    assert error.request_id == "dg-handshake"
    assert "sensitive details" not in str(error)
