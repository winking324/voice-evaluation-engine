# Dataset Loader MVP

The dataset loader accepts local JSON manifests and, when the optional
`huggingface` extra is installed, Hugging Face Datasets selections. It returns
provider-neutral `DatasetSample` objects and a `DatasetSnapshot` that should be
copied into the evaluation run metadata.

## Local manifest

Audio files must live under the configured data root. `audio_uri` is a
canonical relative path; absolute paths, URL schemes, traversal, and symlinks
that resolve outside the root are rejected. A missing `reference_text` (or
JSON `null`) means ground truth is unavailable. An empty string is retained as
an intentional empty transcript.

```json
{
  "dataset_id": "my-dictation-samples",
  "dataset_revision": "v1",
  "language": "en",
  "locale": "en-US",
  "transcript_style": "verbatim",
  "samples": [
    {
      "sample_id": "meeting-001",
      "audio_uri": "audio/meeting-001.wav",
      "duration_ms": 4200,
      "reference_text": "Let's meet at 10 tomorrow.",
      "tags": ["dictation", "noisy"]
    }
  ]
}
```

```python
from voice_eval.datasets import load_manifest

result = load_manifest("data/manifest.json", data_root="data")
for sample in result.samples:
    # Resolve sample.audio.storage_key only relative to the configured root.
    print(sample.sample_id, sample.has_ground_truth, sample.audio.sha256)
run_metadata = {"dataset": result.snapshot.model_dump(mode="json")}
```

The local snapshot revision is content-addressed from the manifest and audio
checksums. The loader computes checksums during loading and validates an
optional `audio_sha256` supplied in each manifest entry. Supported manifest
fields and constraints are defined by `DatasetManifest` and `ManifestSample`.

## LibriSpeech `test-clean` directory

The small importer reads each chapter's `.trans.txt`, checks its sibling FLAC
files, computes SHA-256 and duration, and writes a manifest without copying
audio. Pass the extracted `test-clean` directory as the data root. The
`huggingface` extra supplies lazy `soundfile` duration support; the directory
must be readable. In a container, mount the corpus read-only and write the
manifest to a separate writable path:

```bash
docker run --rm \
  --mount type=bind,src=/path/to/LibriSpeech/test-clean,dst=/corpus,readonly \
  --mount type=bind,src="$PWD/data",dst=/output \
  your-built-image \
  python -c 'from voice_eval.datasets import build_librispeech_manifest; build_librispeech_manifest("/corpus", "/output/librispeech-test-clean.json")'
```

The container image and command are illustrative; the importer itself is
independent of Docker. The repository does not include the corpus or make a
license selection on the user's behalf.

## Optional Hugging Face source

Install `voice-evaluation-engine[huggingface]` to enable the adapter. Specify
dataset ID, configuration (use `null` only when the dataset has no config),
split, and column mapping. A 40-character commit SHA marks the selection as
reproducible. A branch or omitted revision is resolved to a commit SHA for
provenance, but remains marked non-reproducible because the caller did not pin
that immutable revision. For a dataset with no ground truth, set
`text_column=None`; every loaded sample then has `reference_text=None` and
`has_ground_truth=False`. If a text column name is configured, a missing value
in any row remains an error.

```python
from voice_eval.datasets import HuggingFaceLoadRequest, load_huggingface_dataset

request = HuggingFaceLoadRequest(
    dataset_id="owner/dataset",
    config_name="default",
    split="test",
    revision="0123456789abcdef0123456789abcdef01234567",
    audio_column="audio",
    text_column="text",
    duration_ms_column="duration_ms",  # optional; otherwise inferred from audio
)
result = load_huggingface_dataset(request, data_root="data")
```

For audio-only datasets, pass `text_column=None` in the request constructor.

HF streaming is enabled. Audio cached outside the configured root is copied to
`data/cache/hf_audio/` so returned audio keys still resolve under the trusted
root. Do not commit datasets or audio files to Git. The adapter does not choose
a public dataset or assert its license; callers are responsible for dataset
selection and license review. Tests use synthetic local fixtures and do not
download public datasets.
