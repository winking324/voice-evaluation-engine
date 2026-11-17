# Containerized development and testing

## Goal

Run the application and its tests without installing project Python, Node.js, PostgreSQL, or audio-processing dependencies into the host environment. Containers isolate runtime dependencies; project data and generated artifacts remain in explicitly named project volumes or ignored workspace directories. Docker Desktop/Engine and Compose are the only required host runtime dependencies.

Containerization is the default local development and test workflow once application code exists. This design stage documents the topology; executable Dockerfiles and Compose files are added with the first runnable application phase so their commands match real entry points.

## Compose topology

```text
Host browser ── 127.0.0.1:5173/8080 ── web container
                                           │ internal network
                                     API container ── worker container
                                           │                  │
                                           └──── PostgreSQL ──┘
                                               named volume

API/worker ── outbound HTTPS ── selected STT providers / Hugging Face
```

Planned services:

- `web`: React development server with hot reload in dev; built static assets served by a small web server in a production-like profile.
- `api`: FastAPI HTTP API and SSE progress endpoint.
- `worker`: bounded async evaluation workers; separate service from API so tests and load do not tie up request handling.
- `db`: PostgreSQL with a health check and a project-scoped named volume. No host port is published by default; optional local inspection binds only to `127.0.0.1`.
- `redis`: omitted from the MVP default stack; enabled only in a scale profile if a broker is later selected.

The browser itself cannot resolve Compose service names. The UI therefore uses relative `/api` URLs: the Vite dev server proxies API/SSE paths to `http://api:8000` from inside the web container, and the production web server reverse-proxies those paths to the API service. Browser-facing ports bind to loopback by default. The API port need not be published to the host except for optional local debugging, also bound to loopback. Database credentials and provider keys are injected as environment variables at runtime; they are never copied into an image layer.

## Isolation rules

1. **No host dependency installation.** Build and run commands use containers (`docker compose run/exec`); do not require `pip install`, global Python packages, npm global packages, or a host database.
2. **Project-specific Compose namespace.** Set a stable Compose project name such as `voice-eval-dev`. Compose prefixes named volumes and networks with this name so they do not collide with other projects. Do not set a global explicit `name:` or `external: true` on development/test volumes or networks; either can bypass project scoping.
3. **Keep datasets outside images.** Hugging Face cache uses a named volume such as `hf_cache`; private/local audio is supplied from `./data/raw` as a read-only bind mount when needed. Images must not contain datasets, runs, secrets, or local database files.
4. **Separate disposable test state.** Integration tests use a distinct Compose project name (for example `voice-eval-test`) and a fresh PostgreSQL service/volume. Test services wait for the database health check before running migrations/tests. The default test network is internal with no external egress; any live-provider smoke test uses a separate opt-in profile/network. The test command uses `--rm` for one-off containers. Cleanup targets only that named test project. Never run volume deletion against the development project as a generic cleanup step.
5. **Artifacts are explicit.** Evaluation outputs use an ignored `./data/runs` bind mount or a named volume with an explicit export command. Do not write generated files into arbitrary host paths.
6. **Credentials remain local.** Developers copy `.env.example` to ignored `.env`. Compose reads it at runtime; `.env` is excluded from Git and Docker build contexts. CI uses its secret store. Avoid `docker compose config` output in logs if it may interpolate secrets.
7. **No privileged container access.** Do not mount `/var/run/docker.sock`, use host networking, or run privileged containers. Use non-root users and only the capabilities and writable paths the service needs.

## Planned file layout

```text
Dockerfile.api              # Python API/worker image; same package, different command
Dockerfile.web              # Node build stage and minimal static serving stage
compose.yaml                # dev stack, health checks, project-scoped named volumes
compose.test.yaml           # isolated test DB and test command, no third-party calls by default
uv.lock                     # resolved Python dependency versions for repeatable builds
web/package-lock.json       # resolved frontend dependency versions for npm ci
.dockerignore               # excludes secrets, VCS, local data, caches and build artifacts
web/.dockerignore           # excludes frontend secrets, node_modules and build output
```

The API image should use a supported, explicitly version-pinned Python base, install dependencies from `uv.lock` in locked mode, include required audio tools such as FFmpeg inside the image, run as a non-root user, and use a health check. API and worker can share the same image to avoid dependency drift. Pin the PostgreSQL major version and upgrade it intentionally; do not use floating `latest` image tags. The web production image should use a pinned Node build image and contain only built static assets plus its minimal serving process. Use `npm ci` against the committed package lock. For development, source bind mounts and hot reload are acceptable, but dependency directories remain inside the container or a named volume rather than replacing host directories.

## Data and volume plan

| Data | Development | Test | Image |
|---|---|---|---|
| PostgreSQL data | `voice-eval-dev` named volume | Separate disposable `voice-eval-test` volume | Never |
| HF download cache | `voice-eval-dev` named volume | Optional isolated cache or disabled | Never |
| Local source audio | Read-only `./data/raw` mount when explicitly selected | Small fixtures generated or mounted read-only | Never |
| Run artifacts | Ignored `./data/runs` mount or named volume | Temporary; export only when requested | Never |
| Provider API keys | Runtime env/secret injection | Mocked tests by default; live credentials opt-in | Never |

Do not use a host-path bind mount for PostgreSQL's internal data directory. Use a Docker named volume, so container user/host filesystem differences do not corrupt a developer's local database files.

## Developer workflows (planned commands)

The exact commands are finalized with the runnable Compose files. Intended workflow:

```bash
# Build and start the project-scoped development stack
docker compose -p voice-eval-dev up --build

# Run unit/contract checks in a disposable application container
docker compose -p voice-eval-test -f compose.yaml -f compose.test.yaml run --rm test

# Stop development services without deleting their database or cache
docker compose -p voice-eval-dev down

# Remove only disposable test state when intentionally resetting tests
docker compose -p voice-eval-test -f compose.yaml -f compose.test.yaml down --volumes --remove-orphans
```

The test project must use a different database name, network and volume namespace from development. The test container depends on `db` with `condition: service_healthy`, and test-only networks are marked `internal: true` so mocked tests cannot accidentally call external providers. The routine stop command does not delete volumes. The destructive test cleanup is explicit and scoped to `voice-eval-test`; no `down --volumes` command should target the dev project by default.

## Test layers in containers

- Unit tests run in the API image/test target without network access to third-party APIs.
- Provider adapter contract tests use recorded/mocked responses and fake clocks; they do not consume API quota or send audio.
- Integration tests start a clean PostgreSQL container, wait for its health check, apply migrations, and exercise API/worker state transitions.
- Optional live smoke tests require explicit provider credentials and a tiny approved sample set; isolate them behind a separate Compose profile and make provider charges/data egress visible before execution.
- Performance/latency benchmarks run in a documented container resource profile. Capture CPU/memory limits, host CPU architecture, Docker runtime, concurrency and region. Container timing is client-observed and must not be presented as vendor-internal latency.

## Resource and network controls

- Set conservative CPU/memory limits for DB and application services in test profiles; make them configurable for load tests.
- Bound worker concurrency through environment configuration and align it with provider rate/concurrency limits.
- The database has no external published port by default; app containers connect using the Compose service name.
- Outbound HTTPS is required for selected providers and remote datasets. The application must never fetch arbitrary user-supplied URLs as audio without SSRF-safe allowlisting/validation.
- Keep health checks distinct from readiness: worker readiness requires database connectivity and a valid configuration, not successful provider credentials unless that run selected the provider.

## Container acceptance criteria

- A clean host with Docker Compose can start the app and run non-live tests without installing project dependencies or a local database.
- Starting/stopping this stack does not alter host Python/Node packages, other Compose projects, or developer audio files.
- Development and test projects use separate databases, networks and named volumes; test cleanup cannot remove development state.
- Images and logs contain no API keys, raw private audio or transcripts by default.
- Live-provider calls are opt-in and clearly distinguished from deterministic test runs.
- Repeating a run in the same image/configuration records image/app versions and benchmark environment so latency comparisons are interpretable.
