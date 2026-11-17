# Product scope and requirements

## Problem

Choose an STT service for a high-quality English dictation experience by replaying identical audio through multiple providers and comparing output quality, speed, and estimated cost. The tool must make the evidence inspectable so a leaderboard does not overstate what a small or mismatched sample can prove.

## Users and jobs

- An engineer selects a dataset or uploads a permitted manifest, chooses providers/models/options, and launches a batch.
- The engineer watches queued/running/completed/failed progress and can stop a run.
- The engineer compares aggregate and per-sample transcripts, listens to audio, inspects errors and latency, and exports results.
- The engineer reruns a pinned configuration and compares it to a previous run.

## MVP scope

1. Prerecorded audio evaluation using at least two provider adapters (initial candidates: OpenAI and Deepgram).
2. A dataset manifest with audio reference, reference transcript, language/locale, stable sample ID, duration and tags.
3. Batch execution with bounded concurrency, provider-specific rate limits, retries for transient errors, timeout, cancellation and resumable sample states.
4. Raw response preservation and normalized transcript scoring (WER; CER as a supplementary metric, especially for future non-space-delimited languages).
5. Per-sample request/response timing, success/error status and estimated cost, plus run-level p50/p95 and coverage.
6. A simple frontend to select datasets/providers/options, launch jobs, see progress and compare results.
7. Export JSON/CSV and retain enough run metadata for reproducibility.

## Out of scope for the first milestone

- Claiming that one provider is universally “best”; scores are conditional on corpus, locale, task, network, model and options.
- Live microphone ingestion or production dictation UX. Streaming benchmark is a separate track with different timing definitions.
- Training/fine-tuning ASR models, human annotation platform, or automatic adjudication by an LLM.
- Multi-tenant public service, billing, or autoscaling worker fleet. The design leaves room for these, but MVP concurrency is bounded.

## Success criteria

- A clean local deployment can run the same pinned set of audio samples through two providers and produce a complete report.
- The report shows raw transcripts and per-sample WER errors, not just a score.
- Timing values come from monotonic clocks and have explicit start/end definitions; queue wait is separated from provider request duration.
- Failures, retries, timeouts, skipped samples, and incomplete coverage remain visible and are not silently omitted from comparison.
- A noisy sample can be tagged and analyzed separately. A 10-sample smoke run demonstrates the pipeline only; any comparative claim needs a representative, sufficiently large evaluation set.

## Assumptions and open product data

- First target is English personal dictation, confirmed during design.
- Exact locale (e.g. en-US/en-GB), dictation style (verbatim vs. polished), acceptable handling of fillers, and product-specific vocabulary remain dataset/configuration decisions.
- Public corpora are useful baselines but may be read speech or meeting speech rather than personal dictation. A small, permissioned, human-verified in-domain set should eventually anchor selection.
- Audio may be sent to third-party APIs. Dataset selection and any uploaded audio must satisfy applicable license, consent, privacy and provider retention requirements.
