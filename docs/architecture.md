# Architecture and repository structure

## High-level design

```text
React UI ── HTTP API / progress stream ── FastAPI
                                            │
                             ┌──────────────┴──────────────┐
                             │ Run orchestration + database │
                             └──────────────┬──────────────┘
                                            │ bounded work queue
                                      Evaluation workers
                                     ┌──────┴──────┐
                                Dataset loader  Provider adapters
                                     │                │
                                Audio storage      STT vendors
                                     └──────┬─────────┘
                                   Scoring + reporting
```

The API validates and persists a run request, returns a run ID, and exposes status/results. Workers own long-running provider calls; HTTP request lifetimes must not own an entire batch. The first deployment can use one API process and one worker process. Later, worker replicas can scale independently.

## Proposed repository tree

```text
.
├── docs/                         # Product, architecture, metrics, API and ops design
├── src/voice_eval/
│   ├── api/                      # FastAPI routers, request/response schemas, auth boundary
│   ├── application/              # Use cases: create/cancel/run evaluation, compare runs
│   ├── domain/                   # Provider-neutral entities, status transitions, metric contracts
│   ├── providers/                # Provider interface, OpenAI adapter, Deepgram adapter
│   ├── datasets/                 # HF/local manifest readers, validation, audio metadata
│   ├── evaluation/               # Normalizers, WER/CER, latency/cost aggregation, ranking
│   ├── workers/                  # Claim/execute samples, concurrency and retry policies
│   ├── persistence/              # SQLAlchemy models, repositories, migrations
│   ├── storage/                  # Local/object storage abstraction for permitted audio/artifacts
│   └── config.py                 # Typed configuration; secrets come from environment/secret store
├── web/                          # React + TypeScript UI
├── migrations/                   # Alembic database migrations
├── data/
│   ├── manifests/                # Small, redistributable metadata examples only
│   ├── raw/                      # Ignored local datasets/audio
│   ├── cache/                    # Ignored HF/audio cache
│   └── runs/                     # Ignored local run artifacts
├── tests/
│   ├── unit/                     # Normalization, metrics, state, policy tests
│   ├── contract/                 # Provider request/response adapter contract tests (mocked)
│   └── integration/              # DB/API/worker flows; external calls opt-in
├── scripts/                      # Dataset checks, local run/export helpers
├── pyproject.toml
├── .env.example
└── README.md
```

The scaffold phase creates these directories and documentation only; implementation can add files as each module is built. `domain` must not import vendor SDKs, FastAPI or persistence details. `application` orchestrates ports; adapters implement those ports. This dependency direction keeps scoring and run state deterministic and vendor-neutral.

## Core boundaries

- **API**: parse and validate user intent, enforce file/config limits, provide run state and result views. It does not call providers inline.
- **Application**: creates immutable run snapshots, schedules samples, drives state transitions, and coordinates cancellation.
- **Provider port**: accepts an audio source plus normalized request options and returns a standard provider result or typed error.
- **Dataset port**: resolves a pinned dataset split into stable sample records. It never silently moves a dataset revision.
- **Evaluation**: pure functions from raw reference/hypothesis and a versioned profile to score records; aggregation is independent of the UI.
- **Persistence**: stores inputs, states, raw outputs and derived metrics. Run inputs and raw outputs are append-only; recalculation writes a new metric version.
- **Frontend**: submits run configuration, subscribes/polls progress, renders raw and aggregate evidence. It does not calculate authoritative metrics.

## Persistence and artifact strategy

Use PostgreSQL in shared deployments for concurrent workers and durable job state. Local SQLite can simplify development and single-worker smoke runs, but do not rely on SQLite locking behavior for concurrency claims. Store large audio outside relational rows; persist an opaque storage URI, checksum, format metadata and access policy. Keep provider responses and per-sample JSON artifacts with retention configurable. Public dataset audio should be fetched/cache-managed, not copied into the repository.

## Architectural references

- AssemblyAI's `huggingface-benchmarking` demonstrates provider-specific adapters over HF datasets and per-provider parallel execution; use it as a feature reference, not as a dependency or an assumed production architecture: https://github.com/AssemblyAI-Solutions/huggingface-benchmarking
- Hugging Face End-to-End Speech Benchmark shows the value of standardized datasets, diagnostic subsets, and explicit normalized/unnormalized references: https://huggingface.co/spaces/esb/leaderboard
- JiWER is a focused metric library; keep normalization policy and run metadata in this project: https://github.com/jitsi/jiwer
