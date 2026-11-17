# Architecture and repository structure

## MVP topology

```text
Browser ── same-origin HTTP/SSE ── single app process
                                    ├── FastAPI API
                                    ├── built React UI
                                    ├── bounded asyncio run workers
                                    ├── SQLite (persistent local queue/results)
                                    ├── dataset loader / scoring
                                    └── batch + streaming STT provider adapters
```

The API validates a run request, snapshots its configuration, persists the job, and returns a run ID. A bounded worker pool in the same process performs provider calls and persists progress and results; request lifetimes do not own a batch. The process serves the UI and API from one origin. The default local app is a single instance with a SQLite database on a named volume.

This deployment intentionally favors simple personal use while preserving run state across ordinary restarts. SQLite and in-process workers do not provide multi-instance coordination or horizontal scaling. Keep the boundaries behind repositories and worker interfaces so a measured scale need can introduce PostgreSQL and a separate worker/broker later without changing provider or scoring contracts.

For local development and validation, run the app and its tests through Docker Compose. Runtime dependencies stay inside the image; SQLite state, dataset caches and run artifacts use project-scoped volumes or explicit host mounts. See [containerized development and testing](containerization.md).

## Proposed repository tree

```text
.
├── docs/                         # Product, architecture, metrics, API and ops design
├── Dockerfile                    # Multi-stage UI build and Python application image
├── compose.yaml                  # Single app service with project-scoped volumes
├── compose.test.yaml             # Isolated, non-live test command
├── src/voice_eval/
│   ├── api/                      # FastAPI routers, request/response schemas
│   ├── application/              # Use cases: create/cancel/run evaluation, compare runs
│   ├── domain/                   # Provider-neutral entities, state and metric contracts
│   ├── providers/                # Provider interface, OpenAI adapter, Deepgram adapter
│   ├── datasets/                 # HF/local manifest readers, validation, audio metadata
│   ├── evaluation/               # Normalizers, WER/CER, latency/cost aggregation, ranking
│   ├── workers/                  # In-process bounded workers, retries and cancellation
│   ├── persistence/              # SQLite models, repositories and migrations as needed
│   ├── storage/                  # Local storage abstraction for permitted audio/artifacts
│   └── config.py                 # Typed configuration; secrets come from environment
├── web/                          # React + TypeScript UI; built into the app image
├── data/
│   ├── manifests/                # Small, redistributable metadata examples only
│   ├── raw/                      # Ignored local datasets/audio when copied locally
│   ├── cache/                    # Ignored HF/audio cache
│   └── runs/                     # Ignored local run artifacts
├── tests/
│   ├── unit/                     # Normalization, metrics, state, policy tests
│   ├── contract/                 # Provider request/response adapter tests (mocked)
│   └── integration/              # SQLite/API/worker flows; external calls opt-in
├── scripts/                      # Dataset checks, local run/export helpers
├── pyproject.toml
├── .env.example
├── .dockerignore
└── README.md
```

The scaffold phase creates repository directories and design docs; implementation adds files as modules are built. `domain` must not import vendor SDKs, FastAPI, or persistence details. `application` orchestrates ports; adapters implement those ports. This dependency direction keeps scoring and run state deterministic and vendor-neutral.

## Core boundaries

- **API**: validate user intent, enforce limits, expose run state and results. It does not await an entire batch inline.
- **Application**: creates immutable run snapshots, schedules samples, drives state transitions, and coordinates cancellation.
- **Provider port**: accepts audio plus normalized request options and returns a standard provider result or typed error.
- **Dataset port**: resolves a pinned dataset split into stable sample records. It never silently moves a dataset revision.
- **Evaluation**: pure functions from raw reference/hypothesis and a versioned profile to score records; aggregation is independent of the UI.
- **Persistence**: stores inputs, states, raw outputs and derived metrics. Run inputs and raw outputs are append-only; recalculation writes a new metric version.
- **Worker**: claims persisted jobs in the single app process, applies bounded concurrency and updates progress without holding database transactions across provider calls.
- **Frontend**: submits run configuration, subscribes/polls progress, and renders raw and aggregate evidence. It does not calculate authoritative metrics.

## Persistence and artifact strategy

Use SQLite in WAL mode for the personal single-instance MVP, stored on a project-scoped Docker named volume. Keep database transactions short, configure a busy timeout, and do not use multiple API processes or containers against the same queue. Treat source audio as external or read-only mounted data; persist its relative storage key, checksum, format metadata and access policy rather than copying large audio into relational rows. Keep provider responses and per-sample artifacts minimal and retention configurable. Public dataset audio should be fetched/cache-managed or mounted, not copied into the repository.

If measured queueing or write contention requires multiple instances, move shared state to PostgreSQL and run independent workers through a durable queue; do this as a deliberate later scale step, not as an MVP dependency.

## Architectural references

- AssemblyAI's `huggingface-benchmarking` demonstrates provider-specific adapters over HF datasets and per-provider parallel execution; use it as a feature reference, not as a dependency or an assumed production architecture: https://github.com/AssemblyAI-Solutions/huggingface-benchmarking
- Hugging Face End-to-End Speech Benchmark shows the value of standardized datasets, diagnostic subsets, and explicit normalized/unnormalized references: https://huggingface.co/spaces/esb/leaderboard
- JiWER is a focused metric library; keep normalization policy and run metadata in this project: https://github.com/jitsi/jiwer
