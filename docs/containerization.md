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
3. **Protect the prepared dataset.** Set `DATASET_SOURCE_DIR` to a host directory containing `manifests/` and `audio/`. Compose mounts those subdirectories read-only at `/data/manifests` and `/data/audio`; manifest audio paths such as `audio/sample.wav` therefore resolve beneath `DATA_ROOT=/data`. Writable named volumes at `/data/runs` and `/data/cache` keep database/artifacts and cache separate. Missing source subdirectories are created empty so the app can still start. The test service uses synthetic fixtures only and has no dataset mount.
4. **Keep generated data out of the image.** SQLite files, Hugging Face cache, audio, transcripts, and run exports belong in explicit volumes or read-only mounts, never in image layers or Git.
5. **Credentials remain local.** If `.env` already exists, preserve its `OPENAI_API_KEY` and `DEEPGRAM_API_KEY` assignments and append settings from `.env.example`; do not replace the file. Compose injects this file only into the runtime app. The build context excludes `.env` and `.env.*`; Dockerfiles do not use build args or `COPY` for secrets. The test Compose file has no `env_file`, explicitly empties provider keys, and the documented test command disables default Compose interpolation with `--env-file /dev/null`.
6. **No privileged access.** Do not mount the Docker socket, use host networking, or run privileged containers. Run as a non-root user and grant only the writable paths the app needs.
7. **No arbitrary URL fetches.** Audio is selected from validated local dataset manifests or an approved source; do not fetch user-supplied audio URLs from the server.

## Docker image and Compose files

```text
Dockerfile              # pinned Node builder, Python runtime and test targets
compose.yaml             # single non-root app service, loopback port and named volumes
compose.test.yaml        # standalone test target with no credentials/network
uv.lock                  # locked Python dependencies
web/package-lock.json    # locked frontend dependencies
.dockerignore            # excludes secrets, VCS, datasets, caches and run output
```

The Dockerfile pins Python `3.11.18`, Node `26.13.1` and uv `0.13.8`; `web/package-lock.json` and `uv.lock` lock application dependencies. The final image contains the built static UI and Python app, runs as UID/GID `10001`, uses a read-only root filesystem, drops Linux capabilities, and exposes only the app port. It installs `libsndfile1` plus the Python `audio` and `providers` extras needed for WAV/FLAC streaming replay and WebSocket adapters. Do not install PostgreSQL, a message broker, Node, or a process supervisor in the runtime image. The one API process owns the in-process worker concurrency and SQLite writes.

The app serves `/app/web/dist` through FastAPI on the same origin as `/api/v1`. Compose sets `DATA_ROOT=/data`, `DATABASE_PATH=/data/runs/voice-evaluation.sqlite3`, and `WEB_DIST_DIR=/app/web/dist`. The named `run-data` volume stores SQLite and generated state; `hf-cache` stores the optional dataset cache. The configured dataset source contains both `manifests/` and `audio/`; Compose mounts each read-only beneath `/data`, preserving the paths referenced by manifests. Queue and concurrency limits come from `WORKER_QUEUE_CAPACITY`, `MAX_PENDING_RUNS`, `MAX_SAMPLES_PER_RUN`, and `MAX_WORKER_CONCURRENCY`.

The current SQLite app reads `DATABASE_PATH`. `DATABASE_URL` is legacy and ignored. Existing database content is not automatically copied or migrated to the Compose volume; keep the source database untouched and make an explicit backup/export/import plan first. No PostgreSQL-to-SQLite migration command is provided yet. The example file intentionally contains no API-key assignments: preserve existing key lines and values when adding the other settings.

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

Use explicit project names so test commands cannot target the development volumes:

```bash
# Build and start the app; only the app port is published to loopback.
docker compose --env-file .env -p voice-eval-dev up --build -d

# Run deterministic tests in an isolated project with no live provider keys.
docker compose --env-file /dev/null -p voice-eval-test -f compose.test.yaml run --build --rm tests

# Stop the app and preserve local run data.
docker compose --env-file .env -p voice-eval-dev down

# Intentionally erase only disposable test state.
docker compose --env-file /dev/null -p voice-eval-test -f compose.test.yaml down --remove-orphans
```

The test service uses `network_mode: none`; it cannot reach external APIs. The image build itself still requires network access to fetch base images and dependencies. Provider credentials are runtime-only, and live-provider calls happen only when explicitly requested by starting a run. The app port is bound to loopback only.

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
