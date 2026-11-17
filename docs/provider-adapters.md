# Provider adapter MVP

## Supported configurations

| Provider | Selected batch model | Selected streaming model | Streaming input |
| --- | --- | --- | --- |
| OpenAI | `gpt-transcribe` | `gpt-live-transcribe` | Mono PCM16, 24 kHz; client commits each audio turn |
| Deepgram | `nova-3` | `nova-3` on `/v1/listen` WebSocket | Raw frames with encoding, sample rate and channel count |

These are supported configurations, not implicit model substitutions: each request uses the exact model in `TranscriptionOptions` or `StreamingTranscriptionOptions`. OpenAI batch transcription maps `timestamps=True` only for an explicitly selected legacy `whisper-1` request, whose timestamp support is scheduled for deprecation; the selected `gpt-transcribe` benchmark configuration does not request timestamps. Deepgram can return word timings when timestamps are requested. No adapter rewrites transcript text or estimates missing metadata.

OpenAI batch transcription uses the File transcription endpoint and returns a complete response, so batch TTFT is unavailable. Its Realtime transcription session emits text deltas followed by a completed transcript event. Deepgram streaming maps `is_final` and `speech_final` separately: the former marks an immutable segment, while the latter indicates endpointing. `finish_audio()` sends Deepgram `CloseStream`, which flushes pending audio and lets the service deliver final results and metadata before it closes. The adapter emits `is_terminal` only when it receives that post-close metadata and the socket closes normally; a close without metadata is a typed failure. Neither vendor's per-event model is rewritten into the other vendor's semantics. Provider attempt timing follows [the shared timing definitions](providers-and-latency.md#timing-definitions); lifecycle queue timing follows the documented single-process SQLite clock rules there.

Audio bytes are supplied through an injected trusted `AudioLoader`. Provider classes do not resolve storage keys or fetch arbitrary URLs. Inject an owned storage adapter that enforces the configured storage root boundary.

Install the optional `providers` extra (`pip install -e '.[providers]'`) to use the streaming paths; batch paths use the existing `httpx` dependency. SDK/API calls have bounded request/connect timeouts, map HTTP and WebSocket failures to `ProviderError`, retain provider request IDs when present (on the terminal event for streaming), and do not include credentials or response bodies in error messages.

## Official API references checked

- [OpenAI file transcription](https://developers.openai.com/api/docs/guides/speech-to-text)
- [OpenAI Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)
- [Deepgram prerecorded transcription](https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded)
- [Deepgram live WebSocket API](https://developers.deepgram.com/reference/speech-to-text/listen-streaming)
- [Deepgram interim results and endpointing](https://developers.deepgram.com/docs/understand-endpointing-interim-results)

Model IDs and supported features can change. Review these references when updating the supported configurations; do not silently substitute models in a benchmark result. Deepgram's `metadata.duration` describes audio duration, not service processing time or billable usage, so this MVP does not map it into those result fields.
