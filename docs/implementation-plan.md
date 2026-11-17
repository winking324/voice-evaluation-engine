# Implementation plan and design review

## Delivery phases

### Phase 0 — scaffold and design (current)

- Create repository directories, README, `.gitignore`, and reviewed module design docs.
- Confirm scenario as English personal dictation.
- Keep exact dataset and target locale as explicit configuration decisions.

### Phase 1 — reproducible batch core

- Python packaging/configuration, domain entities, migrations and local developer setup.
- Manifest dataset loader and one pinned public English baseline subset.
- Provider interface and two adapters; secrets via environment; mocked adapter contract tests.
- Bounded asynchronous worker, run/sample state persistence and attempt-level timestamps.
- Versioned English scoring profile, JiWER adapter, per-sample output and aggregate counts.
- CLI or API-triggered 10-sample smoke run; include one deterministic noisy sample for pipeline demonstration.

### Phase 2 — web workflow and report

- FastAPI run/dataset/provider/progress/results endpoints.
- React UI for configuration, progress, leaderboard, per-sample inspection and export.
- PostgreSQL deployment path; SSE progress with polling fallback.
- Compare repeat runs and surface coverage, retries and errors.

### Phase 3 — benchmark quality

- Add permissioned in-domain English dictation set and critical entities.
- Add format-sensitive number/date/currency checks, noise/accent slices, confidence intervals and repeat protocol.
- Add streaming provider path with separately defined TTFT/finalization metrics.

### Phase 4 — throughput and operations

- Measure expected peak QPS and queue time targets.
- If database polling worker is insufficient, add durable broker and horizontally scaled workers, provider token buckets and operational dashboards.
- Add user access control, retention/deletion automation and object storage as deployment requires.

## Decisions required before implementation of benchmark fixtures

- Select exact initial dataset ID/revision/split, after checking license/access and provider submission permissions.
- Select target locale and dictation transcript style (verbatim vs lightly formatted); the initial language is English, but English variant is not yet set.
- Set provider model IDs, options, regions and cost source at implementation time against current official documentation.
- Decide if local development can start with SQLite or should require PostgreSQL. The design supports both, with PostgreSQL as shared deployment target.

These are not blockers for the scaffold or architecture. They are blockers for publishing a meaningful benchmark result, so the software must make them explicit rather than silently choosing defaults.

## Review against original requirements

| Requirement | Design coverage | Review result |
|---|---|---|
| Standardized interface for at least two services | Provider port and separate OpenAI/Deepgram adapters | Covered; vendor-specific options remain explicit |
| Ground-truth accuracy and batch evaluation | Dataset manifest, WER/CER, sample-level scores and batch worker | Covered; 10 samples are smoke-only |
| TTFT and total processing time | Streaming vs file API definitions; event timestamp capture and separate queue/request/end-to-end times | Covered without conflating incompatible timings |
| Leaderboard for accuracy, latency and cost | UI comparison dimensions, percentiles, cost provenance, configurable score | Covered; no opaque universal winner |
| Scalable code structure | Domain/application/adapters split and proposed tree | Covered |
| High-QPS bonus | Bounded concurrency now, durable broker/worker scaling later | Covered with realistic staged scope |
| No ground truth | Accuracy unavailable; disagreement only routes human review | Covered |
| More quality measurements | CER, entities, format, hallucinations, failure/coverage, correction effort | Covered and labeled by applicability |
| Noisy sample demonstration | Deterministic noisy slice and noise metadata | Covered |
| Simple UI | Dataset/configuration, progress, run results and exports | Covered |

## Review checklist before merge of implementation

- [ ] Every run pins dataset revision, sample IDs, provider model/options, normalization version, metric library version, and cost source.
- [ ] Raw text and event timestamps are retained; normalized results are derived, versioned outputs.
- [ ] The same normalization profile is applied to reference and all hypotheses; ambiguous numeric/date cases are not silently rewritten.
- [ ] No file endpoint is mislabeled as TTFT; streaming interim and final timings are separate.
- [ ] Aggregate WER uses summed edit counts and reference-token denominator.
- [ ] Failed/slow/retried calls and cost are visible; coverage accompanies all rankings.
- [ ] Public and private audio handling follows license/consent/provider policies; secrets and data are excluded from Git.
- [ ] UI clearly labels small or nonrepresentative sample results.
- [ ] Tests cover metric edge cases, retries/state transitions, provider response mapping and timing derivation; external provider calls are opt-in.

## Reference projects and sources

- AssemblyAI HF benchmarking: https://github.com/AssemblyAI-Solutions/huggingface-benchmarking
- JiWER: https://github.com/jitsi/jiwer
- Hugging Face dataset selection guide: https://huggingface.co/learn/audio-course/chapter5/choosing_dataset
- Hugging Face End-to-End Speech Benchmark dataset documentation: https://huggingface.co/datasets/esb/datasets
- AWS ASR evaluation guidance on normalization and corpus-aware evaluation: https://aws.amazon.com/blogs/machine-learning/evaluating-an-automatic-speech-recognition-service/
