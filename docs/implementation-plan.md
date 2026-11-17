# Implementation plan and design review

## Delivery phases

### Phase 0 — scaffold and design (current)

- Create repository directories, README, `.gitignore`, and reviewed module design docs, including container isolation strategy.
- Confirm scenario as English personal dictation.
- Keep exact dataset and target locale as explicit configuration decisions.

### Phase 1 — reproducible batch core

- Python packaging/configuration, domain entities and migrations.
- Resolve and commit Python/frontend dependency lockfiles; container builds use locked installs rather than floating dependency resolution.
- Dockerfiles and Compose development stack for API, worker, web UI and PostgreSQL; no host Python/Node/database dependency installation.
- Isolated Compose test project with disposable database state and mocked external provider calls by default.
- Manifest dataset loader and one pinned public English baseline subset.
- Separate batch/streaming provider contracts and at least two streaming-capable provider/model adapters, verified against current API docs; keep prerecorded batch results in a separate track.
- Secrets via environment; mocked adapter contract tests for batch responses and streaming interim/final events.
- Bounded asynchronous worker, run/sample state persistence and attempt-level timestamps.
- Versioned English scoring profile, JiWER adapter, per-sample output and aggregate counts.
- Controlled real-time audio replay with identical sample rate, chunking and pacing across streaming providers; record TTFT and post-audio finalization latency.
- CLI or API-triggered 10-sample smoke run; include one deterministic noisy sample for pipeline demonstration.

### Phase 2 — web workflow and report

- FastAPI run/dataset/provider/progress/results endpoints.
- React UI for configuration, progress, leaderboard, per-sample inspection and export.
- PostgreSQL deployment path; SSE progress with polling fallback.
- Compare repeat runs and surface coverage, retries and errors.
- Document containerized startup, stop, data reset and optional live-provider smoke workflows.

### Phase 3 — benchmark quality

- Add permissioned in-domain English dictation set and critical entities.
- Add format-sensitive number/date/currency checks, noise/accent slices, confidence intervals and repeat protocol.
- Add more streaming providers, long-running session cases and endpointing/chunking sensitivity analysis.

### Phase 4 — throughput and operations

- Measure expected peak QPS and queue time targets.
- If database polling worker is insufficient, add durable broker and horizontally scaled workers, provider token buckets and operational dashboards.
- Add user access control, retention/deletion automation and object storage as deployment requires.
- Add production image hardening/scanning and CI container build checks; keep developer Compose configuration separate from deployment manifests.

## Decisions required before implementation of benchmark fixtures

- Select exact initial dataset ID/revision/split, after checking license/access and provider submission permissions.
- Select target locale and dictation transcript style (verbatim vs lightly formatted); the initial language is English, but English variant is not yet set.
- Set provider model IDs, options, regions and cost source at implementation time against current official documentation.
- The local Compose app/test runtime uses PostgreSQL. SQLite is limited to isolated unit-level experiments and does not stand in for concurrent-worker integration tests.

These are not blockers for the scaffold or architecture. They are blockers for publishing a meaningful benchmark result, so the software must make them explicit rather than silently choosing defaults.

## Review against original requirements

| Requirement | Design coverage | Review result |
|---|---|---|
| Standardized interface for at least two services | Provider port and separate OpenAI/Deepgram adapters | Covered; vendor-specific options remain explicit |
| Ground-truth accuracy and batch evaluation | Dataset manifest, WER/CER, sample-level scores and batch worker | Covered; 10 samples are smoke-only |
| TTFT and total processing time | Batch response latency and streaming TTFT/finalization use separate definitions; two streaming-capable configurations are required | Covered in Phase 1; batch-only runs are explicitly partial |
| Leaderboard for accuracy, latency and cost | UI comparison dimensions, percentiles, cost provenance, configurable score | Covered; no opaque universal winner |
| Scalable code structure | Domain/application/adapters split and proposed tree | Covered |
| High-QPS bonus | Bounded concurrency now, durable broker/worker scaling later | Covered with realistic staged scope |
| No ground truth | Accuracy unavailable; disagreement only routes human review | Covered |
| More quality measurements | CER, entities, format, hallucinations, failure/coverage, correction effort | Covered and labeled by applicability |
| Noisy sample demonstration | Deterministic noisy slice and noise metadata | Covered |
| Simple UI | Dataset/configuration, progress, run results and exports | Covered |

The first batch-only run is not, by itself, a complete success against the original real-time dictation objective. Full initial success requires the Phase 1 controlled streaming replay path and comparable TTFT/finalization metrics for at least two eligible provider/model configurations. If a selected provider has no comparable streaming transcription mode, report TTFT as unavailable for it and select another eligible configuration for the real-time track rather than substituting a batch-response time.

## Review checklist before merge of implementation

- [ ] Every run pins dataset revision, sample IDs, provider model/options, normalization version, metric library version, and cost source.
- [ ] Latency percentile estimator is fixed/versioned; sample counts and failures are reported with p50/p95.
- [ ] Raw text and event timestamps are retained; normalized results are derived, versioned outputs.
- [ ] The same normalization profile is applied to reference and all hypotheses; ambiguous numeric/date cases are not silently rewritten.
- [ ] No file endpoint is mislabeled as TTFT; streaming interim and final timings are separate.
- [ ] Aggregate WER uses summed edit counts and reference-token denominator.
- [ ] Failed/slow/retried calls and cost are visible; coverage accompanies all rankings.
- [ ] Public and private audio handling follows license/consent/provider policies; secrets and data are excluded from Git.
- [ ] UI clearly labels small or nonrepresentative sample results.
- [ ] Dev and test Compose project names, networks and volumes are separate; test cleanup cannot delete dev data.
- [ ] No credentials, private audio, dataset cache or run output are included in container build contexts/images.
- [ ] Routine Compose shutdown preserves dev DB/cache; live API smoke tests are opt-in.
- [ ] Tests cover metric edge cases, retries/state transitions, provider response mapping and timing derivation; external provider calls are opt-in.

## Reference projects and sources

- AssemblyAI HF benchmarking: https://github.com/AssemblyAI-Solutions/huggingface-benchmarking
- JiWER: https://github.com/jitsi/jiwer
- Hugging Face dataset selection guide: https://huggingface.co/learn/audio-course/chapter5/choosing_dataset
- Hugging Face End-to-End Speech Benchmark dataset documentation: https://huggingface.co/datasets/esb/datasets
- AWS ASR evaluation guidance on normalization and corpus-aware evaluation: https://aws.amazon.com/blogs/machine-learning/evaluating-an-automatic-speech-recognition-service/
