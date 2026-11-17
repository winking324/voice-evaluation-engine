# Web UI, API, and persistence model

## UI screens

### New evaluation

- Choose an available dataset/version/split and sample limit or select an approved local manifest.
- Choose the benchmark track: prerecorded batch or controlled streaming replay. For streaming, configure a common audio chunk size/feed pace; defaults are pinned and applied identically to all selected providers.
- Select providers/models and provider-specific options; validate missing credentials and unsupported combinations before launch.
- Choose scoring profile, locale, bounded concurrency and repeat count.
- Show estimated sample count/audio duration and an estimated cost range when available; require explicit launch action.

### Run progress

- Show queued/running/completed/failed/cancelled states and counts (completed, running, pending, retrying, failed/skipped).
- Show elapsed time and current provider/sample progress without exposing audio/transcript content unnecessarily.
- Support cancellation and resume/retry failed samples according to policy.

### Results and comparison

- Compare runs only when dataset revision/split/sample set, evaluation track, scoring profile and relevant configurations match or visibly explain differences.
- Keep batch and streaming leaderboards separate. Batch columns include normalized WER, CER where meaningful, p50/p95 request latency, estimated/actual cost per audio minute, success/timeout rate and coverage. Streaming columns additionally show TTFT p50/p95, post-audio finalization delay, session setup time and replay pacing.
- Per-sample table: audio playback where permitted, reference, raw hypotheses, normalized hypotheses, diff/alignment, latency, cost, status and provider error.
- Filter by dataset tags/slices and export CSV/JSON. Display sample count and caveat if corpus is small or nonrepresentative.

## API sketch (versioned under `/api/v1`)

- `GET /datasets` — available dataset descriptors and pinned revisions.
- `POST /runs` — validate and snapshot a run request including track and (for streaming) replay pacing; return `202` with `run_id`.
- `GET /runs/{run_id}` — run configuration snapshot, aggregate progress and state.
- `GET /runs/{run_id}/samples` — paginated per-sample results.
- `POST /runs/{run_id}/cancel` — request cooperative cancellation.
- `POST /runs/{run_id}/retry-failed` — create retry work under the same or explicit new run semantics.
- `GET /runs/{run_id}/events` — SSE progress stream; polling fallback for simpler deployments.
- `GET /runs/{run_id}/export?format=csv|json` — export report.
- `GET /providers` — provider capability/config schema; never return credentials.

The API is asynchronous for batch execution. Idempotency key on run creation prevents accidental duplicate submissions. Schema changes require explicit API versioning/migration policy.

## Data model (logical)

- `DatasetSnapshot`: dataset ID, source/revision, split, license/terms reference, manifest checksum, language/locale, created time.
- `EvaluationRun`: ID, idempotency key, dataset snapshot, batch/streaming track, scoring profile/version, immutable provider/config snapshots, replay pacing where applicable, concurrency, repeat index, state, timestamps, cancellation state.
- `EvaluationSample`: run ID + stable sample ID, audio checksum/duration/tags, state, attempt count, timestamps, error category.
- `ProviderAttempt`: sample/run/provider/model, exact options, request ID, event timestamps, retry number, usage/price snapshot, error and raw-response artifact reference.
- `TranscriptResult`: raw text, normalized text/profile version, optional segment/timing/confidence payload, reference text, metric counts/rates.
- `MetricAggregate`: run/provider/profile, denominators/counts, percentiles, coverage, failures, cost sum and uncertainty metadata.

Use foreign keys and uniqueness constraints for run/sample/provider/repeat identity. Store state transitions explicitly. Derived metrics are rebuildable from retained raw results and a pinned metric version. Avoid storing secrets in run snapshots; record a credential alias/tenant identifier only if operationally needed.

## Progress delivery

Workers persist state/count updates; SSE streams events from persisted or brokered state, and the frontend reconnects using an event cursor. Progress messages are best-effort hints; the run resource remains the source of truth. Coalesce frequent updates so a large batch does not generate one database write per tiny UI change.
