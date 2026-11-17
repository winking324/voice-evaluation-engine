# Implementation plan and design review

## Delivery phases

### Phase 0 — scaffold and design (current)

- Create repository directories, README, `.gitignore`, and reviewed module design docs, including container isolation strategy.
- Confirm scenario as English personal dictation.
- Confirm a local LibriSpeech `test-clean` subset is prepared; treat it as clean read-speech baseline, not in-domain dictation.
- Keep dataset and target locale provenance explicit in every run.

### Phase 1 — reproducible evaluation core

- Python packaging/configuration, domain entities and provider-neutral contracts.
- Resolve and commit Python/frontend dependency lockfiles; container builds use locked installs.
- One Docker Compose app container serving API and built Web UI, with a bounded in-process worker pool and SQLite WAL database on a named volume; no host Python/Node/database installation.
- Separate test Compose project with temporary SQLite state, synthetic fixtures and mocked provider calls by default.
- Manifest/local LibriSpeech loader plus optional pinned Hugging Face loader; mount prepared audio read-only and never copy licensed audio into Git/image.
- At least two batch adapters and two comparable streaming-capable provider/model configurations, verified against current official docs; keep batch and controlled-streaming results separate.
- Secrets via runtime environment; mocked adapter contract tests for batch responses, streaming interim/final/terminal events and failures.
- Bounded asynchronous run/sample scheduler, persisted state and attempt-level timing; app restart must expose interrupted attempts and apply explicit retry policy.
- Versioned English scoring profile, JiWER adapter, per-sample output and corpus-weighted aggregates.
- Controlled real-time audio replay with identical sample rate, chunking and pacing across streaming providers; record TTFT and input-sealed finalization latency.
- API-triggered 10-sample smoke run from the prepared test-clean baseline; include a deterministically transformed noisy sample if it can be generated reproducibly. A smoke run validates the pipeline, not a provider winner.

### Phase 2 — usable web workflow and report

- FastAPI dataset/run/provider/progress/results endpoints.
- React UI served by the same app origin for configuration, progress, leaderboard, per-sample inspection and export.
- Local history and exports; SSE progress with polling fallback if useful.
- Compare repeat runs and surface coverage, retries and errors.
- Document containerized startup, stop, data reset and optional live-provider smoke workflows.

### Phase 3 — benchmark quality

- Add permissioned in-domain English dictation set and critical entities.
- Add format-sensitive number/date/currency checks, noise/accent slices, confidence intervals and repeat protocol.
- Add more streaming providers, long-running session cases and endpointing/chunking sensitivity analysis.

### Phase 4 — measured throughput expansion

- Measure expected peak QPS, queue wait and SQLite write contention.
- Only if single-process limits are reached, migrate persistent queue/state to PostgreSQL and add a separate worker service or durable broker.
- Add user access control, retention/deletion automation and object storage only if deployment needs them.
- Add production image hardening/scanning and CI container build checks if the project becomes shared or deployed.

## Decisions required before publishing a benchmark result

- Record the exact dataset revision/split, locale and transcript style. The prepared LibriSpeech `test-clean` audio is a reproducible English read-speech baseline and does not represent personal dictation.
- Record provider model IDs, options, regions and price source against current official documentation at run time.
- Confirm dataset terms permit submitting the selected sample audio to each selected external provider; show that egress explicitly before live evaluation.
- Keep locale-sensitive numeric/date normalization conservative; ambiguous forms are not silently rewritten.

These are not blockers for implementing the pipeline. They are required metadata and user-facing checks before interpreting or sharing benchmark claims.

## Review against original requirements

| Requirement | Design coverage | Review result |
|---|---|---|
| Standardized interface for at least two services | Provider port and separate OpenAI/Deepgram adapters | Covered; vendor-specific options remain explicit |
| Ground-truth accuracy and batch evaluation | Dataset manifest, WER/CER, sample-level scores and bounded batch runner | Covered; 10 samples are smoke-only |
| TTFT and total processing time | Batch response latency and streaming TTFT/finalization use separate definitions; at least two streaming configurations are required | Covered; batch-only runs report TTFT unavailable |
| Leaderboard for accuracy, latency and cost | UI comparison dimensions, percentiles, cost provenance and configurable score | Covered; no opaque universal winner |
| Scalable code structure | Domain/application/adapters split | Covered |
| High-QPS bonus | Bounded concurrency and queue now; PostgreSQL/distributed workers only after measured need | Covered with a single-instance limit stated |
| No ground truth | Accuracy unavailable; disagreement only routes human review | Covered |
| More quality measurements | CER, entities, formatting, hallucinations, failure/coverage, correction effort | Covered and labeled by applicability |
| Noisy sample demonstration | Deterministic noisy sample and transformation metadata | Covered |
| Simple UI | Dataset/configuration, progress, run results and exports | Covered |

A batch-only run does not satisfy the original real-time dictation comparison. Full initial success requires a controlled streaming replay path and comparable TTFT/finalization metrics for at least two eligible provider/model configurations. If a selected model has no comparable streaming mode, report TTFT as unavailable for it and select another eligible configuration for the real-time track.

## Review checklist before merge of implementation

- [ ] Every run pins dataset revision, sample IDs, provider model/options, normalization version, metric library version and price source.
- [ ] Latency percentile estimator is fixed/versioned; sample counts and failures are reported with p50/p95.
- [ ] Raw text and event timestamps are retained; normalized results are derived, versioned outputs.
- [ ] The same normalization profile is applied to references and all hypotheses; ambiguous numeric/date cases are not silently rewritten.
- [ ] No file endpoint is mislabeled as TTFT; streaming interim, stable-segment, terminal and finalization timing are distinct.
- [ ] Aggregate WER uses summed edit counts and reference-token denominator.
- [ ] Failed/slow/retried calls and costs are visible; coverage accompanies all rankings.
- [ ] Public and private audio handling follows license/consent/provider policies; secrets and data are excluded from Git and images.
- [ ] UI labels small or nonrepresentative sample results.
- [ ] Development and test Compose projects use separate state; test cleanup cannot remove development data.
- [ ] No credentials, private audio, dataset cache or run output are included in build contexts/images.
- [ ] Routine Compose shutdown preserves development state; live-provider tests are opt-in.
- [ ] Tests cover metric edge cases, retry/state transitions, provider response mapping and timing derivation; external calls are opt-in.

## Reference projects and sources

- AssemblyAI HF benchmarking: https://github.com/AssemblyAI-Solutions/huggingface-benchmarking
- JiWER: https://github.com/jitsi/jiwer
- Hugging Face dataset selection guide: https://huggingface.co/learn/audio-course/chapter5/choosing_dataset
- Hugging Face End-to-End Speech Benchmark dataset documentation: https://huggingface.co/datasets/esb/datasets
- AWS ASR evaluation guidance on normalization and corpus-aware evaluation: https://aws.amazon.com/blogs/machine-learning/evaluating-an-automatic-speech-recognition-service/
