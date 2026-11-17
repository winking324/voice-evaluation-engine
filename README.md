# Voice Evaluation Engine

A reproducible workbench for comparing hosted speech-to-text services on the same audio, dataset split, and scoring contract.

This repository is currently at the design and scaffold stage. See [the design index](docs/README.md) before implementation.

## Intended first benchmark

- Use case: English personal dictation.
- Initial provider adapters: OpenAI and Deepgram, behind a provider-neutral interface.
- Initial mode: prerecorded audio batch evaluation. Streaming latency is a separately defined follow-up track.
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

Container workflow and isolation rules are described in [docs/containerization.md](docs/containerization.md). Executable Dockerfiles and Compose files will be added with the first runnable application phase.
