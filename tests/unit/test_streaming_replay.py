import asyncio
from collections.abc import AsyncIterator

import pytest

from voice_eval.application.streaming import (
    StreamingCompletionTimeout,
    StreamingSessionEndedWithoutTerminal,
    replay_stream,
)
from voice_eval.domain.contracts import (
    AudioFrame,
    StreamingTranscriptEvent,
    StreamingTranscriptionOptions,
)

_END = object()


class FakeClock:
    def __init__(self) -> None:
        self.now_ns = 0

    def monotonic_ns(self) -> int:
        return self.now_ns

    def advance_ms(self, milliseconds: float) -> None:
        self.now_ns += round(milliseconds * 1_000_000)

    async def sleep(self, seconds: float) -> None:
        self.now_ns += round(seconds * 1_000_000_000)
        await asyncio.sleep(0)


class FakeSession:
    def __init__(
        self,
        clock: FakeClock,
        *,
        terminal_on_second_frame: bool = False,
        terminal_delay_after_finish_ms: float | None = 5,
        close_stream_without_terminal: bool = False,
        send_delay_ms: float = 0,
    ) -> None:
        self.clock = clock
        self.terminal_on_second_frame = terminal_on_second_frame
        self.terminal_delay_after_finish_ms = terminal_delay_after_finish_ms
        self.close_stream_without_terminal = close_stream_without_terminal
        self.send_delay_ms = send_delay_ms
        self.queue: asyncio.Queue[StreamingTranscriptEvent | object] = asyncio.Queue()
        self.sent_frames: list[tuple[int, int]] = []
        self.finish_called = False
        self.closed = False

    async def send_audio(self, frame: AudioFrame) -> None:
        self.sent_frames.append((frame.sequence, self.clock.monotonic_ns()))
        if frame.sequence == 0:
            self.clock.advance_ms(4)
            self.queue.put_nowait(StreamingTranscriptEvent(text="hello", is_final=False))
        elif frame.sequence == 1:
            self.clock.advance_ms(2)
            if self.terminal_on_second_frame:
                self.queue.put_nowait(
                    StreamingTranscriptEvent(text="hello world", is_final=True, is_terminal=True)
                )
            else:
                self.queue.put_nowait(StreamingTranscriptEvent(text="hello world", is_final=True))
        self.clock.advance_ms(self.send_delay_ms)
        await asyncio.sleep(0)

    async def finish_audio(self) -> None:
        self.finish_called = True
        self.clock.advance_ms(3)
        if self.terminal_delay_after_finish_ms is not None:
            asyncio.create_task(self._send_terminal_after_delay())
        elif self.close_stream_without_terminal:
            self.queue.put_nowait(_END)

    async def _send_terminal_after_delay(self) -> None:
        delay_ms = self.terminal_delay_after_finish_ms
        if delay_ms is None:
            return
        await self.clock.sleep(delay_ms / 1000)
        self.queue.put_nowait(
            StreamingTranscriptEvent(
                text="", is_final=True, is_terminal=True, request_id="fake-request"
            )
        )

    async def events(self) -> AsyncIterator[StreamingTranscriptEvent]:
        while True:
            item = await self.queue.get()
            if item is _END:
                return
            assert isinstance(item, StreamingTranscriptEvent)
            yield item

    async def aclose(self) -> None:
        self.closed = True
        self.queue.put_nowait(_END)


class FakeProvider:
    provider_id = "fake"

    def __init__(self, clock: FakeClock, session: FakeSession) -> None:
        self.clock = clock
        self.session = session

    async def open_session(self, options: StreamingTranscriptionOptions) -> FakeSession:
        del options
        self.clock.advance_ms(2)
        return self.session


def _options() -> StreamingTranscriptionOptions:
    return StreamingTranscriptionOptions(
        model="fake-v1", sample_rate_hz=16_000, encoding="pcm_s16le"
    )


def _frames() -> list[AudioFrame]:
    return [
        AudioFrame(sequence=0, data=b"\x00\x01", duration_ms=10),
        AudioFrame(sequence=1, data=b"\x02\x03", duration_ms=10),
    ]


def test_replay_uses_attempt_monotonic_offsets_and_explicit_terminal_event() -> None:
    async def run() -> None:
        clock = FakeClock()
        session = FakeSession(clock)
        result = await replay_stream(
            FakeProvider(clock, session),
            _options(),
            _frames(),
            monotonic_ns=clock.monotonic_ns,
            sleep=clock.sleep,
        )

        assert [sent_ns / 1_000_000 for _, sent_ns in session.sent_frames] == [2, 12]
        assert result.session_setup_ms == 2
        assert result.first_audio_frame_offset_ms == 2
        assert result.last_audio_frame_offset_ms == 12
        assert result.ttft_ms == 4
        assert result.first_final_transcript_ms == 12
        assert result.input_sealed_offset_ms == 25
        assert result.terminal_transcript_offset_ms == 30
        assert result.terminal_request_id == "fake-request"
        assert result.post_audio_finalization_ms == 5
        assert result.terminal_before_input_sealed is False
        assert result.stream_wall_time_ms == 28
        assert result.request_total_ms == 30
        assert result.replay_overrun_ms == 0
        assert session.finish_called is True
        assert session.closed is True
        assert result.events[0].event.is_terminal is False

    asyncio.run(run())


def test_stable_final_segment_does_not_substitute_for_terminal_completion() -> None:
    async def run() -> None:
        clock = FakeClock()
        session = FakeSession(
            clock,
            terminal_delay_after_finish_ms=None,
            close_stream_without_terminal=True,
        )
        with pytest.raises(StreamingSessionEndedWithoutTerminal):
            await replay_stream(
                FakeProvider(clock, session),
                _options(),
                _frames(),
                monotonic_ns=clock.monotonic_ns,
                sleep=clock.sleep,
            )
        assert session.closed is True

    asyncio.run(run())


def test_terminal_received_before_input_seal_is_recorded_without_negative_finalization() -> None:
    async def run() -> None:
        clock = FakeClock()
        session = FakeSession(clock, terminal_on_second_frame=True)
        result = await replay_stream(
            FakeProvider(clock, session),
            _options(),
            _frames(),
            monotonic_ns=clock.monotonic_ns,
            sleep=clock.sleep,
        )

        assert result.terminal_transcript_offset_ms < result.input_sealed_offset_ms
        assert result.terminal_before_input_sealed is True
        assert result.post_audio_finalization_ms == 0
        assert result.events[-1].event.is_terminal is True
        assert session.closed is True

    asyncio.run(run())


def test_terminal_timeout_closes_the_session() -> None:
    async def run() -> None:
        clock = FakeClock()
        session = FakeSession(clock, terminal_delay_after_finish_ms=None)
        with pytest.raises(StreamingCompletionTimeout):
            await replay_stream(
                FakeProvider(clock, session),
                _options(),
                _frames(),
                completion_timeout_ms=1,
                monotonic_ns=clock.monotonic_ns,
                sleep=clock.sleep,
            )
        assert session.closed is True

    asyncio.run(run())


def test_replay_reports_when_provider_send_latency_breaks_real_time_pacing() -> None:
    async def run() -> None:
        clock = FakeClock()
        session = FakeSession(clock, send_delay_ms=15)
        result = await replay_stream(
            FakeProvider(clock, session),
            _options(),
            _frames(),
            monotonic_ns=clock.monotonic_ns,
            sleep=clock.sleep,
        )

        assert [sent_ns / 1_000_000 for _, sent_ns in session.sent_frames] == [2, 21]
        assert result.replay_overrun_ms == 16

    asyncio.run(run())


def test_frame_sequence_must_be_contiguous_and_session_is_closed_on_error() -> None:
    async def run() -> None:
        clock = FakeClock()
        session = FakeSession(clock)
        frames = [AudioFrame(sequence=1, data=b"\x00\x01", duration_ms=10)]
        with pytest.raises(ValueError, match="contiguous"):
            await replay_stream(
                FakeProvider(clock, session),
                _options(),
                frames,
                monotonic_ns=clock.monotonic_ns,
                sleep=clock.sleep,
            )
        assert session.closed is True

    asyncio.run(run())
