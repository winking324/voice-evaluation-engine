"""Controlled, provider-neutral real-time replay for streaming STT attempts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from voice_eval.domain.contracts import (
    AudioFrame,
    StreamingTranscriptEvent,
    StreamingTranscriptionOptions,
)
from voice_eval.providers.protocols import (
    StreamingSpeechToTextProvider,
)

MonotonicClock = Callable[[], int]
AsyncSleeper = Callable[[float], Awaitable[None]]


class StreamingReplayError(RuntimeError):
    """The provider session did not complete the controlled replay correctly."""


class StreamingSessionEndedWithoutTerminal(StreamingReplayError):
    """The event stream closed without an explicit whole-sample terminal event."""


class StreamingCompletionTimeout(StreamingReplayError):
    """No explicit whole-sample terminal event arrived before the configured timeout."""


@dataclass(frozen=True, slots=True)
class TimedTranscriptEvent:
    event: StreamingTranscriptEvent
    received_offset_ns: int

    @property
    def received_offset_ms(self) -> float:
        return self.received_offset_ns / 1_000_000


@dataclass(frozen=True, slots=True)
class StreamingReplayResult:
    """Attempt-relative evidence and derived metrics for one finite audio sample."""

    events: tuple[TimedTranscriptEvent, ...]
    session_setup_ms: float
    request_total_ms: float
    first_audio_frame_offset_ms: float
    last_audio_frame_offset_ms: float
    input_sealed_offset_ms: float
    first_nonempty_transcript_offset_ms: float | None
    first_final_transcript_offset_ms: float | None
    terminal_transcript_offset_ms: float
    terminal_request_id: str | None
    ttft_ms: float | None
    first_final_transcript_ms: float | None
    post_audio_finalization_ms: float
    terminal_before_input_sealed: bool
    stream_wall_time_ms: float
    replay_overrun_ms: float


async def replay_stream(
    provider: StreamingSpeechToTextProvider,
    options: StreamingTranscriptionOptions,
    frames: Iterable[AudioFrame],
    *,
    completion_timeout_ms: int = 30_000,
    monotonic_ns: MonotonicClock = time.monotonic_ns,
    sleep: AsyncSleeper = asyncio.sleep,
) -> StreamingReplayResult:
    """Send finite audio frames on a real-time schedule and collect stream events.

    Each frame's ``duration_ms`` defines when the next frame is scheduled. The last
    frame's duration is also paced before input is sealed. If provider writes fall
    behind that absolute schedule, replay continues as soon as possible and reports
    the largest observed overrun instead of silently claiming the input was paced.

    TTFT is measured from the first successful ``send_audio`` invocation (its start
    offset is retained after the call succeeds) to the first nonempty transcript
    event. An explicit ``is_terminal`` event is required; stable segments and event
    iterator closure alone do not establish whole-sample completion.
    """

    if completion_timeout_ms <= 0:
        raise ValueError("completion_timeout_ms must be greater than zero")

    attempt_started_ns = monotonic_ns()
    session = await provider.open_session(options)
    session_opened_ns = monotonic_ns()

    timed_events: list[TimedTranscriptEvent] = []
    audio_origin_ns: int | None = None
    first_frame_send_ns: int | None = None
    last_frame_send_ns: int | None = None
    input_sealed_ns: int | None = None
    terminal_event: TimedTranscriptEvent | None = None
    replay_overrun_ns = 0
    event_task: asyncio.Task[TimedTranscriptEvent] | None = None
    primary_error: BaseException | None = None

    async def receive_until_terminal() -> TimedTranscriptEvent:
        async for event in session.events():
            received_ns = monotonic_ns()
            observed = TimedTranscriptEvent(
                event=event,
                received_offset_ns=received_ns - attempt_started_ns,
            )
            timed_events.append(observed)
            if event.is_terminal:
                return observed
        raise StreamingSessionEndedWithoutTerminal(
            "provider event stream ended without an is_terminal transcript event"
        )

    try:
        event_task = asyncio.create_task(receive_until_terminal())
        elapsed_audio_ns = 0

        for expected_sequence, frame in enumerate(frames):
            if frame.sequence != expected_sequence:
                raise ValueError(
                    "audio frame sequence must be contiguous and start at zero; "
                    f"expected {expected_sequence}, got {frame.sequence}"
                )

            if event_task.done():
                # A terminal event can be observed while the final audio frames are
                # still being dispatched. A stream ending without terminal is an error.
                terminal_event = event_task.result()

            send_started_ns = monotonic_ns()
            if audio_origin_ns is None:
                audio_origin_ns = send_started_ns

            await session.send_audio(frame)
            if first_frame_send_ns is None:
                first_frame_send_ns = send_started_ns
            last_frame_send_ns = send_started_ns

            frame_start_target_ns = audio_origin_ns + elapsed_audio_ns
            replay_overrun_ns = max(
                replay_overrun_ns,
                max(0, send_started_ns - frame_start_target_ns),
            )
            elapsed_audio_ns += frame.duration_ms * 1_000_000
            frame_end_target_ns = audio_origin_ns + elapsed_audio_ns
            replay_overrun_ns = max(
                replay_overrun_ns,
                max(0, monotonic_ns() - frame_end_target_ns),
            )
            await _sleep_until(frame_end_target_ns, monotonic_ns, sleep)

        if first_frame_send_ns is None or audio_origin_ns is None or last_frame_send_ns is None:
            raise ValueError("at least one audio frame is required")

        if event_task.done():
            terminal_event = event_task.result()

        await session.finish_audio()
        input_sealed_ns = monotonic_ns()

        try:
            terminal_event = await asyncio.wait_for(
                asyncio.shield(event_task), timeout=completion_timeout_ms / 1000
            )
        except TimeoutError as exc:
            raise StreamingCompletionTimeout(
                "no terminal transcript event within "
                f"{completion_timeout_ms} ms after input sealing"
            ) from exc

        if terminal_event is None:
            # Defensive: a successful task always returns the terminal event.
            raise StreamingSessionEndedWithoutTerminal("provider returned no terminal event")

        first_nonempty = next(
            (
                item
                for item in timed_events
                if item.received_offset_ns >= first_frame_send_ns - attempt_started_ns
                and item.event.text.strip()
            ),
            None,
        )
        first_final = next(
            (
                item
                for item in timed_events
                if item.received_offset_ns >= first_frame_send_ns - attempt_started_ns
                and item.event.is_final
                and item.event.text.strip()
            ),
            None,
        )

        first_frame_offset_ms = _elapsed_ms(attempt_started_ns, first_frame_send_ns)
        last_frame_offset_ms = _elapsed_ms(attempt_started_ns, last_frame_send_ns)
        input_sealed_offset_ms = _elapsed_ms(attempt_started_ns, input_sealed_ns)
        terminal_offset_ms = terminal_event.received_offset_ms
        first_nonempty_offset_ms = first_nonempty.received_offset_ms if first_nonempty else None
        first_final_offset_ms = first_final.received_offset_ms if first_final else None

        return StreamingReplayResult(
            events=tuple(timed_events),
            session_setup_ms=_elapsed_ms(attempt_started_ns, session_opened_ns),
            request_total_ms=terminal_offset_ms,
            first_audio_frame_offset_ms=first_frame_offset_ms,
            last_audio_frame_offset_ms=last_frame_offset_ms,
            input_sealed_offset_ms=input_sealed_offset_ms,
            first_nonempty_transcript_offset_ms=first_nonempty_offset_ms,
            first_final_transcript_offset_ms=first_final_offset_ms,
            terminal_transcript_offset_ms=terminal_offset_ms,
            terminal_request_id=terminal_event.event.request_id,
            ttft_ms=(first_nonempty_offset_ms - first_frame_offset_ms)
            if first_nonempty_offset_ms is not None
            else None,
            first_final_transcript_ms=(first_final_offset_ms - first_frame_offset_ms)
            if first_final_offset_ms is not None
            else None,
            post_audio_finalization_ms=max(0.0, terminal_offset_ms - input_sealed_offset_ms),
            terminal_before_input_sealed=terminal_event.received_offset_ms < input_sealed_offset_ms,
            stream_wall_time_ms=terminal_offset_ms - first_frame_offset_ms,
            replay_overrun_ms=replay_overrun_ns / 1_000_000,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        close_error: BaseException | None = None
        try:
            await asyncio.shield(session.aclose())
        except BaseException as exc:  # Preserve the attempt failure if close also fails.
            close_error = exc
        if event_task is not None and not event_task.done():
            event_task.cancel()
        if event_task is not None:
            try:
                await event_task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                if primary_error is None and close_error is None:
                    close_error = exc
        if primary_error is None and close_error is not None:
            raise close_error


async def _sleep_until(deadline_ns: int, monotonic_ns: MonotonicClock, sleep: AsyncSleeper) -> None:
    remaining_ns = deadline_ns - monotonic_ns()
    if remaining_ns > 0:
        await sleep(remaining_ns / 1_000_000_000)


def _elapsed_ms(start_ns: int, end_ns: int) -> float:
    return (end_ns - start_ns) / 1_000_000
