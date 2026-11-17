# Provider adapters, request execution, and latency measurement

## Provider-neutral contract

Batch and streaming are separate capabilities; do not force a file-upload API into a fake streaming interface. Conceptual batch port:

```python
class SpeechToTextProvider(Protocol):
    provider_id: str

    async def transcribe(
        self, audio: AudioInput, options: TranscriptionOptions
    ) -> TranscriptionResult: ...
```

Define a separate `StreamingSpeechToTextProvider` capability for opening a session, sending audio frames, receiving normalized interim/final events, and closing the session. A provider may implement batch, streaming, or both. Compare TTFT only across at least two provider/model configurations that expose comparable streaming transcription behavior. Provider selection is capability-gated: if a requested model does not support partial streaming transcripts, mark TTFT unavailable for it; do not substitute a different timing metric.

`TranscriptionOptions` contains only meaningful shared concepts (language/locale, model, timestamps, formatting preferences) plus a validated provider-specific options object. The adapter records the exact provider model identifier and options actually sent. Do not pretend provider-specific options are interchangeable. `TranscriptionResult` includes raw text, optional segments/word timings/confidence/usage, provider request ID, provider-reported duration if provided, and raw response reference. Adapter errors are typed (authentication, invalid input, rate limit, timeout, transient server, unsupported feature, unknown) and preserve retryability.

## Adapter responsibilities

- Validate local audio metadata and map supported formats to provider requirements. Avoid lossy transcoding unless required, and record any transformation/checksum.
- Keep SDK/network calls isolated. Set connect/read/overall timeouts and close resources reliably.
- Capture provider request identifiers and usage/cost inputs without logging secrets or audio content.
- Return only normalized transport shape; never normalize transcript text or calculate WER inside an adapter.
- Be contract-tested with mocked SDK responses, including empty, partial, malformed, rate-limited and timeout cases.

## Fair benchmark setup

- Send the same audio bytes (or identically transformed bytes) to each provider.
- Pin language, region, model, feature flags and relevant options; record unsupported options explicitly.
- Randomize/interleave provider order across samples or repeat runs to reduce load/time drift bias.
- Separate cold-start/cache behavior if relevant; state whether upload, queue, network and retries are included.
- Keep a fixed concurrency profile for a comparison. Do not compare one provider at concurrency 1 with another at concurrency 20 as though it were a model-only result.

## Timing definitions

Use a monotonic high-resolution clock for durations inside a provider attempt and database-generated UTC timestamps for cross-process lifecycle events. Persist UTC event times for audit plus monotonic elapsed offsets/durations scoped to one provider attempt. Never persist a raw monotonic clock reading as an absolute timestamp or subtract application-container wall clocks to calculate latency; wall clocks can jump, and monotonic clock origins are process/host-specific. Generate enqueue/start/completion lifecycle timestamps from the same PostgreSQL server clock. Record:

- UTC audit times for `enqueued_at`, `worker_started_at`, `request_started_at`, `session_connected_at`, `first_audio_frame_sent_at`, `last_audio_frame_sent_at`, `first_response_at`, `first_nonempty_transcript_at`, and `final_response_at` where observable;
- monotonic elapsed offsets from `request_started`, tagged with the attempt ID and clock domain;
- retry attempt number and timestamps, timeout/cancel event, provider request ID;
- audio duration, request payload byte count, region, client/runtime version and concurrency setting.

Derived metrics:

- queue wait = database timestamp at worker claim − database timestamp at enqueue (cross-process lifecycle latency; retain the wall-clock caveat);
- client-observed request total = monotonic final-response offset − monotonic request-start offset (includes upload/network/client processing);
- time to first transport byte/event = first event − request start, only when the HTTP/streaming client exposes that event;
- request-to-first-transcript = first transcript − request start (useful service/session metric, but not dictation TTFT when audio streaming starts later);
- end-to-end sample latency = database timestamp at final completion − database timestamp at enqueue (cross-process lifecycle latency; retain the wall-clock caveat);
- batch real-time factor = client-observed request total (monotonic request start to complete response, including upload/network) / audio duration;
- streaming reports TTFT, post-audio finalization, and stream wall time separately; do not rank streaming by batch real-time factor because the controlled replay deliberately includes real-time audio playback.

Queue wait and end-to-end lifecycle durations use the database clock to avoid host/container clock skew between API and worker processes; they remain wall-clock-derived and can be affected by a database host clock adjustment. Provider request, stream, and real-time factor durations use the worker's monotonic clock and are the precision latency measurements. Keep these metric classes labeled separately.

For streaming dictation, calculate elapsed values from monotonic offsets: `session_setup_ms = session_connected_offset − request_started_offset`, `TTFT_ms = first_nonempty_transcript_offset − first_audio_frame_offset`, and `post_audio_finalization_ms = final_transcript_offset − last_audio_frame_offset`. Also report stream wall time from first audio frame to final transcript; this includes audio playback duration. Replay audio at the same real-time pace and chunk schedule for every provider. These definitions separate connection setup, live transcription response, and endpointing/finalization delay.

For file/batch APIs returning one complete response, TTFT is unavailable; report time to complete response and total request duration instead. Some SDKs buffer the body, so they expose no earlier first-byte event; do not infer one. For true streaming, retain interim/final status and use the explicit TTFT and finalization definitions above. Do not label HTTP TTFB or full-response latency as first-word/first-token latency.

Aggregate p50/p95 and sample count by provider/config/category. Exclude no failures silently: show successful latency distribution and failure/timeout rate separately; describe how timeouts enter end-to-end views. Client-observed timing is not the vendor's internal processing time. Avoid claims of exact vendor latency from client clocks.

## Cost model

Prefer provider-reported usage when available; otherwise estimate from billed audio duration and a versioned price card. Persist price source/date/currency, rounding assumptions, billable duration, model and usage. Clearly mark estimates versus billed actuals. Failed/retried calls may still incur cost, so track attempt-level cost where it can be determined. Do not hard-code current pricing into metric formulas.

## Initial providers and streaming track

Initial provider candidates are OpenAI and Deepgram, selected as examples from the product request. Provider APIs, model IDs and streaming capabilities change; implement against current official SDK/API docs during the coding phase and record tested versions. Deepgram documents interim streaming results that may change before finalization: https://developers.deepgram.com/docs/understand-endpointing-interim-results. Select model/API pairs for the streaming track only after verifying that at least two return interim transcription events. Batch and streaming results remain distinct even when provided by the same vendor.

OpenAI's transcription API and model availability must likewise be checked against its current official documentation at implementation time. Do not infer streaming support or comparable partial-result semantics from a shared adapter interface.
