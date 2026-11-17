# Voice Evaluation Engine

A reproducible workbench for comparing hosted speech-to-text services on the same audio, dataset split, and scoring contract.

The MVP runs as one container: FastAPI serves the built React UI and API, executes a bounded in-process evaluation queue, and stores run state in SQLite. Docker Compose keeps Python, Node, audio libraries, and run data isolated from the host environment.

## Intended first benchmark

- Use case: English personal dictation.
- Initial provider candidates: OpenAI and Deepgram, behind provider-neutral batch and streaming interfaces where each API/model supports the capability.
- Evaluation tracks: prerecorded batch requests plus controlled real-time replay for measuring TTFT. Keep batch response latency and streaming TTFT in separate result tracks.
- Initial UI: a small web app for dataset/configuration selection, progress, and run comparison.

## Principles

- Keep raw provider output and request metadata; derive normalized scores without destroying evidence.
- Apply the same versioned normalization contract to every hypothesis and its reference.
- Report accuracy, latency distributions, cost, failures, and sample coverage separately; do not hide tradeoffs in one opaque score.
- Treat public datasets as reproducible baselines, not as proof of performance on the product's real users.
- Never commit API keys, private recordings, or downloaded datasets unless their license and handling policy explicitly permit it.
- Run the service and tests in Docker Compose so the host Python/Node/database environment remains untouched; keep development and test volumes isolated.

## Repository map

See [docs/architecture.md](docs/architecture.md) for the intended source tree and module responsibilities, and [docs/implementation-plan.md](docs/implementation-plan.md) for delivery phases.

## Quick start

Requirements: Docker Engine/Desktop and Docker Compose plugin **v2.24.0 or newer**. The Compose file uses `env_file.required` (introduced in v2.24); the bind-mount syntax is also supported by this minimum. Do not install Python, Node, or database dependencies on the host.

1. If `.env` does not exist, copy `.env.example` to `.env`. If you already have an `.env`, keep the current `OPENAI_API_KEY` and `DEEPGRAM_API_KEY` assignments unchanged and append the settings from `.env.example`. Do not replace your existing file with the example.
2. Put dataset manifests under `data/datasets/manifests/` and matching audio files under `data/datasets/audio/`, or set `DATASET_SOURCE_DIR` to another host folder with those two subdirectories. Manifest `audio_uri` paths are relative to `data/` (for example, `audio/sample.wav`). The mounts are read-only.
3. Start the app:

   ```bash
   docker compose --env-file .env -p voice-eval-dev up --build -d
   ```

   Open [http://127.0.0.1:8080](http://127.0.0.1:8080). The app container receives API keys only at runtime. Starting the app does not send audio; a provider call happens only when a run is explicitly started in the UI.

4. Run the mocked test suite in a separate container with no API credentials and no network:

   ```bash
   docker compose --env-file /dev/null -p voice-eval-test -f compose.test.yaml run --build --rm tests
   ```

5. Stop the app without deleting its named data volumes:

   ```bash
   docker compose --env-file .env -p voice-eval-dev down
   ```

The test container has `network_mode: none`, temporary database state, and explicitly empty provider keys. It cannot reach STT providers or the public internet. Image builds still need network access to fetch pinned base images and locked packages.

## Existing configuration and data

The current SQLite runtime reads `DATA_ROOT` and `DATABASE_PATH`; `DATABASE_URL` is a legacy setting and is ignored. Compose stores new runs in its project-scoped `run-data` volume. Existing records referenced by `DATABASE_URL` are not copied or migrated automatically, and the old database is left untouched. If you need those records, back them up and explicitly export/migrate them before switching. The repository does not currently provide a PostgreSQL-to-SQLite migration utility. Keep using the same Compose project name (`voice-eval-dev`) to reconnect to the same named volume. Do not run `down --volumes` on the development project unless you intentionally want to erase its saved runs and cache.

Container workflow and isolation rules are described in [docs/containerization.md](docs/containerization.md).
