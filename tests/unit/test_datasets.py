"""Synthetic, offline tests for manifest and optional HF dataset loading."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from voice_eval.datasets import (
    DatasetLoadError,
    HuggingFaceLoadRequest,
    build_librispeech_manifest,
    load_huggingface_dataset,
    load_manifest,
)


def _wav(path: Path) -> bytes:
    """Create a tiny valid mono WAV using only the standard library."""
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\0\0" * 800)
    return path.read_bytes()


def _manifest(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"dataset_id": "synthetic", "language": "en", "samples": entries}),
        encoding="utf-8",
    )
    return manifest_path


def _entry(sample_id: str, uri: str = "audio/sample.wav", **extra: object) -> dict[str, object]:
    return {"sample_id": sample_id, "audio_uri": uri, "duration_ms": 100, **extra}


def test_manifest_preserves_missing_reference_and_explicit_empty_reference(tmp_path: Path) -> None:
    _wav(tmp_path / "audio/sample.wav")
    path = _manifest(
        tmp_path,
        [_entry("missing"), _entry("silence", reference_text="")],
    )

    result = load_manifest(path, tmp_path)
    missing, silence = result.samples

    assert missing.reference_text is None
    assert not missing.has_ground_truth
    assert silence.reference_text == ""
    assert silence.has_ground_truth
    assert missing.audio.storage_key == "audio/sample.wav"
    assert result.snapshot.reproducible
    assert result.snapshot.resolved_revision.startswith("sha256:")


def test_manifest_rejects_bad_schema_and_duplicate_ids(tmp_path: Path) -> None:
    _wav(tmp_path / "audio/sample.wav")
    with pytest.raises(DatasetLoadError, match="invalid dataset manifest"):
        load_manifest(_manifest(tmp_path, [_entry("bad", reference_text=123)]), tmp_path)
    with pytest.raises(DatasetLoadError, match="invalid dataset manifest"):
        load_manifest(_manifest(tmp_path, [_entry("same"), _entry("same")]), tmp_path)


@pytest.mark.parametrize(
    "uri", ["../outside.wav", "%2e%2e/outside.wav", "/etc/passwd", "https://x/a.wav"]
)
def test_manifest_rejects_nonlocal_or_escaping_paths(tmp_path: Path, uri: str) -> None:
    with pytest.raises(DatasetLoadError):
        load_manifest(_manifest(tmp_path, [_entry("bad", uri)]), tmp_path)


def test_manifest_rejects_symlink_escape_and_checksum_mismatch(tmp_path: Path) -> None:
    _wav(tmp_path / "audio/sample.wav")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.wav"
    _wav(outside)
    (tmp_path / "audio" / "escape.wav").symlink_to(outside)
    with pytest.raises(DatasetLoadError, match="escapes"):
        load_manifest(_manifest(tmp_path, [_entry("escape", "audio/escape.wav")]), tmp_path)

    with pytest.raises(DatasetLoadError, match="mismatch"):
        load_manifest(_manifest(tmp_path, [_entry("hash", audio_sha256="0" * 64)]), tmp_path)


def test_manifest_rejects_missing_audio(tmp_path: Path) -> None:
    with pytest.raises(DatasetLoadError, match="does not exist"):
        load_manifest(_manifest(tmp_path, [_entry("missing")]), tmp_path)


def test_librispeech_builder_references_audio_and_loads_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_root = tmp_path / "LibriSpeech" / "test-clean"
    chapter = dataset_root / "1234" / "5678"
    audio = chapter / "1234-5678-0001.flac"
    _wav(audio)
    _wav(chapter / "1234-5678-0002.flac")
    (chapter / "5678.trans.txt").write_text(
        "1234-5678-0001  A short transcript.\n1234-5678-0002\n", encoding="utf-8"
    )

    fake_soundfile = types.ModuleType("soundfile")
    fake_soundfile.info = lambda _path: types.SimpleNamespace(frames=800, samplerate=8000)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "soundfile", fake_soundfile)
    output = build_librispeech_manifest(dataset_root, tmp_path / "generated.json")

    result = load_manifest(output, dataset_root)
    assert [sample.sample_id for sample in result.samples] == [
        "1234-5678-0001",
        "1234-5678-0002",
    ]
    assert result.samples[0].reference_text == "A short transcript."
    assert result.samples[1].reference_text == ""
    assert result.samples[0].audio.storage_key == "1234/5678/1234-5678-0001.flac"
    assert result.samples[0].audio.duration_ms == 100
    assert not (dataset_root / "cache").exists()


def test_librispeech_builder_reports_missing_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "test-clean"
    chapter = root / "1234" / "5678"
    chapter.mkdir(parents=True)
    (chapter / "5678.trans.txt").write_text("1234-5678-0001 Missing audio\n", encoding="utf-8")
    fake_soundfile = types.ModuleType("soundfile")
    monkeypatch.setitem(sys.modules, "soundfile", fake_soundfile)

    with pytest.raises(DatasetLoadError, match="missing audio"):
        build_librispeech_manifest(root, tmp_path / "manifest.json")


def _install_fake_hf(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, object]]
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class FakeAudio:
        def __init__(self, *, decode: bool) -> None:
            assert decode is False

    class FakeDataset(list):
        def cast_column(self, name: str, audio: FakeAudio) -> FakeDataset:
            assert name == "audio"
            assert isinstance(audio, FakeAudio)
            return self

    def load_dataset(*args: object, **kwargs: object) -> FakeDataset:
        calls.append({"args": args, **kwargs})
        return FakeDataset(rows)

    datasets_module = types.ModuleType("datasets")
    datasets_module.Audio = FakeAudio  # type: ignore[attr-defined]
    datasets_module.load_dataset = load_dataset  # type: ignore[attr-defined]
    datasets_module.__version__ = "fake-3.0"
    monkeypatch.setitem(sys.modules, "datasets", datasets_module)
    return calls


def test_huggingface_pinned_revision_records_provenance_and_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "source.wav"
    data = _wav(audio)
    calls = _install_fake_hf(
        monkeypatch,
        [{"audio": {"path": str(audio), "bytes": None}, "text": "hello", "duration": 100}],
    )
    revision = "a" * 40
    request = HuggingFaceLoadRequest(
        dataset_id="example/voice",
        config_name="clean",
        split="test",
        revision=revision,
        duration_ms_column="duration",
    )

    result = load_huggingface_dataset(request, tmp_path / "data")
    sample = next(iter(result.samples))

    assert result.snapshot.config_name == "clean"
    assert result.snapshot.split == "test"
    assert result.snapshot.resolved_revision == revision
    assert result.snapshot.datasets_version == "fake-3.0"
    assert result.snapshot.reproducible
    assert sample.reference_text == "hello"
    assert sample.audio.storage_key.startswith("cache/hf_audio/")
    assert Path(tmp_path / "data" / sample.audio.storage_key).read_bytes() == data
    assert calls[0]["revision"] == revision
    assert calls[0]["streaming"] is True


def test_huggingface_materializes_byte_only_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.wav"
    audio_bytes = _wav(source)
    _install_fake_hf(
        monkeypatch,
        [{"audio": {"path": None, "bytes": audio_bytes}, "text": "hello", "duration": 100}],
    )
    request = HuggingFaceLoadRequest(
        dataset_id="example/voice",
        config_name=None,
        split="test",
        revision="d" * 40,
        duration_ms_column="duration",
    )

    sample = next(iter(load_huggingface_dataset(request, tmp_path / "data").samples))

    assert sample.audio.media_type == "audio/wav"
    assert Path(tmp_path / "data" / sample.audio.storage_key).read_bytes() == audio_bytes


def test_huggingface_unpinned_revision_is_resolved_but_not_reproducible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "sample.wav"
    _wav(audio)
    _install_fake_hf(
        monkeypatch,
        [{"audio": {"path": str(audio), "bytes": None}, "text": None, "duration": 100}],
    )
    commit = "b" * 40

    class FakeApi:
        def dataset_info(self, dataset_id: str, *, revision: str) -> object:
            assert dataset_id == "example/voice"
            assert revision == "main"
            return types.SimpleNamespace(sha=commit)

    hub = types.ModuleType("huggingface_hub")
    hub.HfApi = FakeApi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    request = HuggingFaceLoadRequest(
        dataset_id="example/voice",
        config_name=None,
        split="validation",
        duration_ms_column="duration",
    )

    result = load_huggingface_dataset(request, tmp_path / "data")
    sample = next(iter(result.samples))

    assert result.snapshot.requested_revision is None
    assert result.snapshot.resolved_revision == commit
    assert not result.snapshot.reproducible
    assert sample.reference_text is None
    assert not sample.has_ground_truth


def test_huggingface_rejects_malformed_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "sample.wav"
    _wav(audio)
    _install_fake_hf(
        monkeypatch,
        [{"audio": {"path": str(audio), "bytes": None}, "text": 42, "duration": 100}],
    )
    request = HuggingFaceLoadRequest(
        dataset_id="example/voice",
        config_name=None,
        split="test",
        revision="c" * 40,
        duration_ms_column="duration",
    )

    with pytest.raises(DatasetLoadError, match="must be text or null"):
        next(iter(load_huggingface_dataset(request, tmp_path / "data").samples))


def test_huggingface_request_requires_fixed_config_and_split() -> None:
    with pytest.raises(ValueError):
        HuggingFaceLoadRequest(dataset_id="example/voice", split="test")  # type: ignore[call-arg]


def test_huggingface_rejects_cache_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_root / "cache").symlink_to(outside, target_is_directory=True)
    _install_fake_hf(monkeypatch, [])
    request = HuggingFaceLoadRequest(
        dataset_id="example/voice", config_name=None, split="test", revision="e" * 40
    )

    with pytest.raises(DatasetLoadError, match="cache directory escapes"):
        load_huggingface_dataset(request, data_root)
