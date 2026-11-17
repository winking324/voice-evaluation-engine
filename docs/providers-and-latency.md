# Provider adapters, request execution, and latency measurement

## Provider-neutral contract

Conceptual port:

```python
class SpeechToTextProvider(Protocol):
    provider_id: str

    async def transcribe(
        self, audio: AudioInput, options: TranscriptionOptions
    ) -> TranscriptionResult: ...
```

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

Use a monotonic high-resolution clock for durations and UTC wall-clock timestamps for audit/correlation. Record event timestamps rather than storing only a derived number:

- `enqueued_at`, `worker_started_at`, `request_started_at`, `first_response_at`, `first_nonempty_transcript_at`, `final_response_at`;
- retry attempt number and timestamps, timeout/cancel event, provider request ID;
- audio duration, request payload byte count, region, client/runtime version and concurrency setting.

Derived metrics:

- queue wait = worker start − enqueue;
- client-observed request total = final response − request start (includes upload/network/client processing);
- time to first response byte/event = first response − request start;
- time to first non-empty transcript = first transcript − request start;
- end-to-end sample latency = final completion − enqueue;
- real-time factor = request processing duration / audio duration (state exact numerator definition).

For file/batch APIs returning one complete response, TTFT is unavailable; report first response and total request duration instead. For true streaming, define TTFT as first non-empty partial transcript, retain interim/final status, and separately record finalization latency. Do not label HTTP TTFB or full-response latency as first-word/first-token latency.

Aggregate p50/p95 and sample count by provider/config/category. Exclude no failures silently: show successful latency distribution and failure/timeout rate separately; describe how timeouts enter end-to-end views. Client-observed timing is not the vendor's internal processing time. Avoid claims of exact vendor latency from client clocks.

## Cost model

Prefer provider-reported usage when available; otherwise estimate from billed audio duration and a versioned price card. Persist price source/date/currency, rounding assumptions, billable duration, model and usage. Clearly mark estimates versus billed actuals. Failed/retried calls may still incur cost, so track attempt-level cost where it can be determined. Do not hard-code current pricing into metric formulas.

## Initial providers and streaming track

Initial adapters are OpenAI and Deepgram, selected as examples from the product request. Provider APIs, model IDs and streaming capabilities change; implement against current official SDK/API docs during the coding phase and record tested versions. Deepgram documents interim streaming results that may change before finalization; this supports a separate real-time track but is not equivalent to a one-shot file response: https://developers.deepgram.com/docs/understand-endpointing-interim-results

OpenAI's transcription API and model availability must likewise be checked against its current official documentation at implementation time. Do not infer streaming support or comparable partial-result semantics from a shared adapter interface.
