# Concurrency, reliability, and security

## Concurrency model

Provider calls are primarily I/O-bound, so Python `asyncio` is appropriate when clients support nonblocking I/O. The MVP runs one app process with a bounded in-process worker pool. Wrap each provider with a semaphore/token bucket for its concurrency and request-rate limits. Also cap global in-flight calls, queue depth, samples per run, and total audio duration/bytes. When capacity is exhausted, return a documented `429` or `503` rather than accepting an unbounded backlog. If an SDK is synchronous, run it in a bounded thread pool; do not block the event loop. CPU-heavy resampling/noise mixing belongs in a process worker or separate preprocessing phase.

Persist run/sample jobs in SQLite (WAL mode) before acknowledging run creation. Keep transactions short and never hold a database lock while awaiting a provider. Use one application process/container against the SQLite queue; it is not a distributed lock or horizontally scalable queue. At startup, queued work can resume. Mark attempts left running by a crash as interrupted, retain their partial timing/error evidence, and retry only under explicit bounded retry policy. Do not splice partial streaming transcripts across attempts.

Bound the persisted queue and report overload clearly. Record actual per-provider in-flight limits and retry delays with each run. When measured throughput or concurrent writes exceed a single SQLite-backed process, move to PostgreSQL plus independent workers and a durable queue in a planned scale phase; do not add Redis or distributed leases to the personal MVP preemptively.

## Job and retry semantics

- States: queued → running → succeeded/failed/cancelled; retryable failures return to queued with attempt count and `next_attempt_at`.
- Use bounded exponential backoff with jitter for 429/5xx/transient network errors; honor `Retry-After` where available.
- Do not retry invalid input/auth errors automatically. Keep maximum attempts and overall run deadline configurable.
- Provider calls can succeed remotely while the app loses the response; exactly-once execution cannot be guaranteed. Keep attempts visible and account for possible duplicate provider charges.
- On app restart, mark previously running attempts interrupted before applying the configured retry policy. Never silently treat a partial attempt as complete.
- Cancellation is cooperative: stop scheduling new samples and cancel in-flight calls where supported, but record calls that completed despite cancellation.
- A failed streaming attempt restarts the same sample from the beginning as a new attempt; do not splice partial transcript events from one session into another attempt's final result.

## Performance measurement controls

- Use monotonic clocks for durations; UTC timestamps are for audit/correlation.
- Keep queue wait, provider request duration and end-to-end completion distinct.
- Pin benchmark concurrency and provider order policy. Record runtime/SDK version, region and machine/network context.
- Report successful-request latency distributions together with failure/timeouts and retries. Never discard slow failures in a way that makes a provider look faster.
- Repeat runs for latency-sensitive decisions and report variability. Client-side measurements include network/upload/client effects and cannot reveal vendor-internal latency exactly.
- Preserve raw event times and derive metrics with a versioned calculation; never hand-edit measured values.

## Security and data handling

- Keep provider keys in environment/secret manager; `.env` and audio/results directories are ignored by Git. Never return or log secrets.
- Treat audio and transcripts as potentially sensitive. Avoid content in routine logs, constrain access, set retention/deletion behavior, and show a clear notice that a run sends audio to selected third parties.
- Validate MIME/codec, duration, file size and manifest paths. Prevent path traversal and unsafe remote URL fetching/SSRF.
- Restrict uploads and storage locations; use non-public object storage and short-lived signed URLs if deployed remotely.
- Respect dataset licenses, consent and provider data-use/retention terms before sending samples. Do not silently upload a gated dataset.
- Store only provider request/response fields needed for evaluation; raw response retention should be configurable and documented.
- Container runtime follows [the containerization design](containerization.md): non-root app, no Docker socket/privileged mode, loopback-only published port, secrets injected at runtime, and datasets/artifacts mounted in explicit paths.

## Observability

Use structured logs and correlation IDs (`run_id`, sample ID, provider attempt ID); redact credentials and avoid transcript/audio payloads. Track queue depth, worker throughput, request latency histograms, provider error/rate-limit counts, retry counts, timeouts, and cost estimates. A local leaderboard is sufficient for the personal MVP; alerting is a later deployment concern.
