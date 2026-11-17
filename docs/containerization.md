# Containerized development and testing

## Goal

Run the application and tests in Docker Compose so the host Python, Node, audio libraries, and database remain untouched. Docker Engine/Desktop and Compose are the only host runtime dependencies. The MVP is a personal, single-instance workbench; keep its default stack to one application container and persistent local data.

## MVP topology

```text
Host browser ── 127.0.0.1:8080 ── app container
                                      ├── FastAPI API + built React assets
                                      ├── bounded asyncio evaluation workers
                                      └── SQLite database on a named volume
                                             │
                                             ├── read-only local dataset mount
                                             └── opt-in outbound HTTPS to selected STT/HF services
```

The `app` service runs one API process. It serves the UI and API from one origin, stores run/job state in SQLite, and schedules provider calls through a bounded in-process worker pool. It does not require PostgreSQL, Redis, a separate web container, or a separate worker container for the MVP. The database, Hugging Face cache, and generated run artifacts live in explicit Docker volumes. User-provided dataset folders are mounted read-only.

The app listens on a loopback-published port by default. Provider credentials are injected at runtime and only used when a user explicitly starts a live evaluation. Tests use fake providers and do not require credentials or external network access.

## Isolation rules

1. **No host dependency installation.** Build and run commands use containers (`docker compose build/up/run/exec`); do not require host `pip`, Node, PostgreSQL, or audio libraries.
2. **Project-scoped state.** Use distinct Compose project names such as `voice-eval-dev` and `voice-eval-test`. Keep development data in a named volume; tests use a temporary database path or a separate disposable test volume. Test cleanup must never target the development project.
3. **Protect the prepared dataset.** Mount the dataset source read-only. The test-clean LibriSpeech directory may be supplied through a host path variable, for example `/Users/shanhui/Workspaces/datasets/LibriSpeech/test-clean/LibriSpeech`; do not copy it into the repository or image. A missing optional mount should not prevent the app from starting.
4. **Keep generated data out of the image.** SQLite files, Hugging Face cache, audio, transcripts, and run exports belong in explicit volumes or read-only mounts, never in image layers or Git.
5. **Credentials remain local.** Developers copy `.env.example` to ignored `.env`. Compose passes keys at runtime; Dockerfiles must not use build args or `COPY` for secrets. Avoid printing fully interpolated Compose configuration in logs.
6. **No privileged access.** Do not mount the Docker socket, use host networking, or run privileged containers. Run as a non-root user and grant only the writable paths the app needs.
7. **No arbitrary URL fetches.** Audio is selected from validated local dataset manifests or an approved source; do not fetch user-supplied audio URLs from the server.

## Planned files and image

```text
Dockerfile              # multi-stage frontend build + Python application image
compose.yaml             # single app service, health check, project-scoped volumes
compose.test.yaml        # isolated test command/database path and internal network
uv.lock                  # locked Python dependencies
web/package-lock.json    # locked frontend dependencies
.dockerignore            # excludes secrets, VCS, datasets, caches and run output
web/.dockerignore        # excludes frontend secrets, node_modules and build output
```

Use explicitly versioned base images and locked dependency installs (`uv sync --locked`, `npm ci`). The final image contains the built static UI and Python app, runs as non-root, and exposes only the app port. Do not install PostgreSQL, a message broker, or a process supervisor in the MVP image. Keep the number of server processes at one so the in-process worker concurrency and SQLite write ownership are predictable.

The app image installs the Python `audio` extra for controlled streaming replay. It supplies SoundFile/NumPy for bounded WAV/FLAC decoding and soxr for resampling to the configured PCM16 replay rate. Mount the writable state volume at `/data/runs`; set `DATA_ROOT=/data` and `DATABASE_PATH=/data/runs/voice-evaluation.sqlite3`. The SQLite MVP reads `DATABASE_PATH`; the older SQLAlchemy-oriented `DATABASE_URL` example is not used by this runtime. Queue and concurrency limits are configured with `WORKER_QUEUE_CAPACITY`, `MAX_PENDING_RUNS`, `MAX_SAMPLES_PER_RUN`, and `MAX_WORKER_CONCURRENCY`. Keep corpus inputs on a separate read-only mount.

## Data and volumes

| Data | Development | Test | Image |
|---|---|---|---|
| SQLite database and run state | Project-scoped named volume | Temporary path or separate disposable volume | Never |
| Hugging Face cache | Optional project-scoped named volume | Disabled or temporary isolated cache | Never |
| Local audio/dataset | Explicit read-only bind mount | Synthetic fixtures only by default | Never |
| Exports | Explicit ignored host directory or named volume | Temporary; export only on request | Never |
| Provider API keys | Runtime environment injection | Absent for normal tests | Never |

Use SQLite in WAL mode with a configurable busy timeout and short transactions. Do not place the database in the source tree or mount a database file from an arbitrary host path. A clean `docker compose down` preserves named development volumes; deleting the dev data requires an explicit, project-scoped `down --volumes` command.

## Workflows

The final commands are implemented alongside the runnable app, but should follow this shape:

```bash
# Build and start the app; only the app port is published to loopback.
docker compose -p voice-eval-dev up --build

# Run deterministic tests in an isolated project with no live provider keys.
docker compose -p voice-eval-test -f compose.yaml -f compose.test.yaml run --rm app pytest

# Stop the app and preserve local run data.
docker compose -p voice-eval-dev down

# Intentionally erase only disposable test state.
docker compose -p voice-eval-test -f compose.yaml -f compose.test.yaml down --volumes --remove-orphans
```

The test network should be internal so mocked tests cannot call third-party services. Live-provider smoke tests are an explicit separate command/profile with credentials and a small approved sample set; show that audio leaves the machine and may incur provider charges before sending it.

## Concurrency and scaling boundary

A single app instance provides bounded asynchronous concurrency and a persistent run queue. Queue size, samples per run, audio duration/bytes, and in-flight calls must all have configurable limits. A full queue returns an explicit `429` or `503`; it never grows without bound. At startup, recover queued jobs and mark interrupted attempts so a restart cannot silently lose or splice partial provider output.

SQLite and the in-process worker pool are intended for this personal MVP and one app instance. They do not provide horizontal worker scaling or multi-instance high-QPS throughput. Measure actual queueing and database contention before changing topology. If a measured workload needs multiple app/worker replicas, move job state to PostgreSQL and use a durable broker or database-backed distributed worker claim; that later scale path is not part of the default stack.

## Container acceptance criteria

- A clean host with Docker Compose can build/start the app and run non-live tests without installing project dependencies or a local database.
- Starting/stopping the app does not alter host Python/Node packages, prepared datasets, other Compose projects, or local audio files.
- Tests use isolated database/cache state and cannot delete development data.
- The image and routine logs contain no API keys, private audio, or transcript payloads by default.
- Live-provider calls are opt-in and use only the selected dataset/sample set.
- Run records include app/container version and benchmark settings so client-observed latency has enough context.
