# Concurrency, reliability, and security

## Concurrency model

The workload is primarily I/O-bound provider requests, so Python `asyncio` is appropriate when provider clients support nonblocking I/O. Wrap each provider with a semaphore/token bucket to enforce its concurrency and request-rate limits. Also cap global concurrency and queued work. If an SDK is synchronous, run calls in a bounded thread pool; do not block the event loop. CPU-heavy resampling/noise mixing belongs in a process worker or separate preprocessing phase.

MVP can use a dedicated worker process polling database-backed sample jobs, with atomic claim/lease and bounded async task groups. This avoids coupling batch lifetime to an HTTP request and supports restart recovery. Local SQLite is restricted to one worker for development/smoke runs; use PostgreSQL for concurrent worker claims. This is acceptable at modest single-instance throughput, but requires careful claim/lease semantics and polling backoff. At higher QPS or multi-instance scale, use a durable broker (e.g. Redis + Celery/ARQ) and independently scale workers. Queue choice is an implementation decision after expected load and deployment environment are known; do not introduce broker operations before needed.

## Job and retry semantics

- States: queued → running → succeeded/failed/cancelled; retryable failures return to queued with attempt count and `next_attempt_at`.
- Use bounded exponential backoff with jitter for 429/5xx/transient network errors; honor `Retry-After` where available.
- Do not retry invalid input/auth errors automatically. Keep maximum attempts and overall run deadline configurable.
- Provider call may succeed remotely while worker loses response; exactly-once execution cannot be guaranteed. Use internal idempotency and make duplicate attempts visible; use provider idempotency only where explicitly supported.
- A worker lease expires after crash; another worker may reclaim a sample. Keep attempt records to account for duplicate provider charges.
- Cancellation is cooperative: stop scheduling new samples and cancel in-flight calls where supported, but record calls that completed despite cancellation.

## Performance measurement controls

- Use monotonic clocks for durations; UTC timestamps are for audit/correlation.
- Keep queue wait, provider request duration and end-to-end completion distinct.
- Pin the benchmark concurrency and provider order policy. Record runtime/SDK version, region and machine/network context.
- Report successful-request latency distributions together with failure/timeouts and retries. Never discard slow failures in a way that makes a provider look faster.
- Repeat runs for latency-sensitive decisions and report variability. Client-side measurements include network/upload/client effects and cannot reveal vendor-internal latency exactly.
- Preserve raw event times and derive metrics with a versioned calculation; never hand-edit measured values.

## Security and data handling

- Keep provider keys in environment/secret manager; `.env` and audio/results directories are ignored by Git. Never return or log secrets.
- Treat audio and transcripts as potentially sensitive. Avoid content in routine logs, constrain access, set retention/deletion behavior, and show a clear notice that a run sends audio to selected third parties.
- Validate MIME/codec, duration, file size and manifest paths. Prevent path traversal and unsafe remote URL fetching/SSRF.
- Restrict upload types and storage locations; use non-public object storage and short-lived signed URLs if deployed remotely.
- Respect dataset licenses, consent and provider data-use/retention terms before sending samples. Do not silently upload a gated dataset.
- Store only provider request/response fields needed for evaluation; raw response retention should be configurable and documented.

## Observability

Use structured logs and correlation IDs (`run_id`, sample ID, provider attempt ID); redact credentials and avoid transcript/audio payloads. Track queue depth, worker throughput, request latency histogram, provider error/rate-limit counts, retry counts, timeouts, and cost estimates. Alerting is a later deployment concern; dashboards should make stuck jobs and repeated provider failures visible.
