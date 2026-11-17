# Design documents

These documents define the first implementation before code is added. The agreed initial product scenario is **English personal dictation**. Interfaces and stored metadata are designed to allow additional languages and domains later.

## Reading order

1. [Product scope and requirements](product-scope.md)
2. [Architecture and repository structure](architecture.md)
3. [Evaluation metrics and transcript normalization](evaluation-and-normalization.md)
4. [Providers, request execution, and latency measurement](providers-and-latency.md)
5. [Datasets and benchmark protocol](datasets-and-benchmark-protocol.md)
6. [Web UI, API, and persistence model](ui-api-and-data-model.md)
7. [Concurrency, reliability, and security](concurrency-reliability-security.md)
8. [Implementation plan and design review checklist](implementation-plan.md)

## Decision summary

- Backend: Python, FastAPI, Pydantic, SQLAlchemy/Alembic; provider calls are asynchronous where supported and bounded by per-provider concurrency limits.
- Frontend: React + TypeScript + Vite. The UI is a thin client over a versioned API.
- Initial persistence: PostgreSQL as the shared/deployable target; SQLite may be used for local development only if the implementation keeps database-specific behavior out of domain code.
- Initial background execution: database-backed run/sample state and a worker process. Redis + Celery/ARQ is an explicit scale-up option, not a required MVP dependency.
- Evaluation: JiWER as a tested metric implementation, wrapped by our own versioned normalization profiles. Store raw and normalized text and per-sample alignment counts.
- Datasets: Hugging Face Datasets adapters plus a manifest-based local dataset format. Dataset version, split, revision, license/access status, and sample IDs are immutable run metadata.
- Reports: per-provider and per-sample raw results, WER/CER where appropriate, latency percentiles, cost estimate, coverage, failures, and a configurable ranking view.

## Design status

This is a reviewable architecture proposal, not an implementation contract for a production SLA. Provider pricing/model identifiers and dataset access terms can change; pin and record the values used by each run. The initial dataset mix is intentionally left configurable until a concrete evaluation corpus is selected.
