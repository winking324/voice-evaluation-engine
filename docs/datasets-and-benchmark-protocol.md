# Datasets and benchmark protocol

## Dataset format

Use a provider-independent manifest so benchmark runs can use Hugging Face datasets or permissioned local recordings:

```json
{
  "dataset_id": "example-en-dictation",
  "dataset_revision": "immutable-revision-or-content-hash",
  "license": "recorded license/terms reference",
  "language": "en",
  "locale": "en-US",
  "transcript_style": "dictation",
  "samples": [{
    "sample_id": "stable-id",
    "audio_uri": "local-or-approved-object-uri",
    "audio_sha256": "...",
    "reference_text": "...",
    "duration_ms": 12345,
    "tags": ["noise:street", "speaker:accent-category"]
  }]
}
```

Speaker tags should be non-identifying and only included when lawful, necessary and supported by dataset terms. A manifest should distinguish reference transcript style (verbatim, normalized, punctuated) and locale. Never put licensed audio in Git. Verify terms before download, redistribution, or submission to a third-party API.

## Candidate public data

- **LibriSpeech**: useful reproducible English baseline; clean audiobook read speech is unlike personal dictation.
- **Common Voice**: useful accent/speaker diversity; much of it is prompted reading. Its current releases are distributed through Mozilla Data Collective rather than Hugging Face; the old Hugging Face `common_voice_17_0` page is empty. Treat it as an optional external dataset source, not an assumed one-click HF dataset.
- **GigaSpeech**: broad English domains and styles, but its current access terms limit use to non-commercial research/education. This personal non-commercial test may use it only if the current terms, including third-party API submission, permit the intended use. Do not use it for a commercial benchmark unless the rights holder's current terms explicitly permit that use.
- **AMI**: spontaneous meeting audio and challenging conditions; meeting turn-taking is not personal dictation.
- **FLEURS**: multilingual evaluation coverage; not a substitute for in-domain English dictation data.
- **Noise robustness**: apply deterministic, documented noise mixing to clean speech as a controlled stress subset, or use a properly licensed noisy corpus. Record source noise, SNR, mixing seed, audio transformation and output checksum. Do not mix noisy and clean samples into a single opaque headline.

Hugging Face's dataset selection guide compares corpora by domain, speaking style, punctuation/casing and license: https://huggingface.co/learn/audio-course/chapter5/choosing_dataset. Its End-to-End Speech Benchmark bundles multiple datasets and documents access conditions and diagnostic data: https://huggingface.co/datasets/esb/datasets. Mozilla's current Common Voice releases are listed on https://commonvoice.mozilla.org/en/datasets; the access change is described at https://community.mozilladatacollective.com/faq-can-i-get-the-common-voice-or-other-mdc-datasets-from-other-platforms-like-github-or-hugging-face/.

## Recommended initial suite

1. Tiny smoke suite (about 10 samples) including at least one noisy sample to validate the end-to-end pipeline; results are not evidence for a provider winner.
2. Pinned English public baseline with an official test split and available references.
3. A small permissioned English personal-dictation validation set with human-verified transcripts, varied speakers, accents, speaking rates, names/numbers/dates, and realistic background conditions. Keep it separated from any tuning or prompt experimentation.
4. Explicit slices for clean/noisy, accent groups where appropriate and critical entities. Report sample counts per slice.

The actual public dataset version and in-domain corpus are a run configuration decision, not silently assumed by the software. Public read-speech results should be titled as such.

## Protocol for reproducibility

- Pin dataset repository revision/config/split, stable sample IDs, license/terms record, audio checksums, and selection seed.
- Keep a stable test split; do not repeatedly tune prompts/configurations against it and report it as unseen evaluation.
- Normalize audio only through a documented deterministic preprocessing pipeline. Record original/transformed checksum, sample rate, channels, codec and duration.
- Run every provider on identical samples with fixed options and concurrency. Interleave provider order or repeat blocks to reduce temporal load bias.
- Store raw hypotheses and references; record scoring profile version.
- Publish successes, skipped/ineligible samples, errors, retries, costs and timing percentiles with coverage.
- For uncertain differences, rerun and/or use paired bootstrap confidence intervals. A 10-sample run is for smoke verification only.

## No ground truth

Without human-verified reference transcripts, WER/CER and accuracy rankings are unavailable. The system can still compare latency, availability and estimated costs; transcript disagreement can flag clips for human review, but consensus is not truth. LLM-based judgments are exploratory and must be labeled separately from reference-based accuracy.
