import hashlib
import json
import math

import numpy as np
import pytest
import soundfile as sf

from voice_eval.datasets.manifest import DatasetLoadError, load_manifest
from voice_eval.datasets.noisy_sample import create_noisy_sample


@pytest.fixture
def source_sample(tmp_path):
    data_root = tmp_path / "data"
    (data_root / "audio").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    rate = 16000
    seconds = 2
    time = np.arange(rate * seconds) / rate
    signal = 0.18 * np.sin(2 * math.pi * 440 * time)
    source_audio = data_root / "audio" / "speech.wav"
    sf.write(source_audio, signal, rate, subtype="PCM_16")
    source_sha = hashlib.sha256(source_audio.read_bytes()).hexdigest()
    manifest_path = data_root / "manifests" / "source.json"
    manifest_path.write_text(json.dumps({
        "dataset_id": "fixture",
        "dataset_revision": "v1",
        "license": "CC-BY-4.0",
        "language": "en",
        "locale": "en-US",
        "transcript_style": "verbatim",
        "samples": [{
            "sample_id": "speech-1",
            "audio_uri": "audio/speech.wav",
            "audio_sha256": source_sha,
            "duration_ms": 2000,
            "reference_text": "A short reference.",
            "tags": ["fixture"],
            "media_type": "audio/wav",
        }],
    }))
    return data_root, manifest_path, source_audio


def test_seeded_noise_is_deterministic_and_emits_loadable_manifest(source_sample):
    data_root, manifest_path, source_audio = source_sample
    source_before = source_audio.read_bytes()
    manifest_before = manifest_path.read_bytes()
    first = create_noisy_sample(manifest_path, "speech-1", data_root, snr_db=12, seed=42)
    first_audio = first.audio_path.read_bytes()
    second = create_noisy_sample(manifest_path, "speech-1", data_root, snr_db=12, seed=42)

    assert first.sample_id == second.sample_id
    assert first.audio_sha256 == second.audio_sha256
    assert first_audio == second.audio_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert source_audio.read_bytes() == source_before
    assert manifest_path.read_bytes() == manifest_before
    assert first.audio_path.is_relative_to(data_root)
    assert first.manifest_path.is_relative_to(data_root)

    loaded = load_manifest(first.manifest_path, data_root)
    [sample] = loaded.samples
    assert sample.sample_id == first.sample_id
    assert sample.reference_text == "A short reference."
    assert sample.language == "en"
    assert sample.locale == "en-US"
    assert sample.audio.sha256 == hashlib.sha256(first_audio).hexdigest()
    assert loaded.snapshot.license == "CC-BY-4.0"
    generated_manifest = json.loads(first.manifest_path.read_text())
    tags = generated_manifest["samples"][0]["tags"]
    assert "source_sample_id:speech-1" in tags
    assert "noise_type:gaussian_white" in tags
    assert "snr_db:12" in tags
    assert "seed:42" in tags
    assert any(tag.startswith("noise_algorithm:") for tag in tags)
    assert f"output_sha256:{first.audio_sha256}" in tags
    assert generated_manifest["samples"][0]["audio_sha256"] == first.audio_sha256


def test_pcm16_output_has_requested_snr_with_hot_source_without_clipping(source_sample):
    data_root, manifest_path, _ = source_sample
    rate = 16000
    hot = np.tile(np.array([0.94, -0.93, 0.75, -0.7]), rate // 4)
    hot_path = data_root / "audio" / "hot.wav"
    sf.write(hot_path, hot, rate, subtype="PCM_16")
    hot_sha = hashlib.sha256(hot_path.read_bytes()).hexdigest()
    raw_manifest = json.loads(manifest_path.read_text())
    raw_manifest["samples"][0].update(audio_uri="audio/hot.wav", audio_sha256=hot_sha)
    manifest_path.write_text(json.dumps(raw_manifest))

    artifact = create_noisy_sample(manifest_path, "speech-1", data_root, snr_db=15, seed=7)
    source, _ = sf.read(hot_path, dtype="float64", always_2d=True)
    mixed, output_rate = sf.read(artifact.audio_path, dtype="float64", always_2d=True)
    assert output_rate == rate
    assert np.max(np.abs(mixed)) <= 1.0
    assert any(tag.startswith("output_gain_db:-") for tag in json.loads(artifact.manifest_path.read_text())["samples"][0]["tags"])
    # The recorded value is used to undo the whole-mixture headroom gain.
    tags = json.loads(artifact.manifest_path.read_text())["samples"][0]["tags"]
    gain_db = float(next(tag.split(":", 1)[1] for tag in tags if tag.startswith("output_gain_db:")))
    gain = 10 ** (gain_db / 20)
    estimated_noise = mixed / gain - source
    measured_snr = 20 * np.log10(np.sqrt(np.mean(source**2)) / np.sqrt(np.mean(estimated_noise**2)))
    assert measured_snr == pytest.approx(15, abs=0.35)


@pytest.mark.parametrize("snr_db", [-20.01, 60.01, float("nan"), float("inf")])
def test_rejects_invalid_snr_without_touching_source(source_sample, snr_db):
    data_root, manifest_path, source_audio = source_sample
    audio_before, manifest_before = source_audio.read_bytes(), manifest_path.read_bytes()
    with pytest.raises(DatasetLoadError, match="snr_db"):
        create_noisy_sample(manifest_path, "speech-1", data_root, snr_db=snr_db, seed=0)
    assert source_audio.read_bytes() == audio_before
    assert manifest_path.read_bytes() == manifest_before


def test_rejects_bad_seed_and_zero_rms_source(source_sample):
    data_root, manifest_path, _ = source_sample
    with pytest.raises(DatasetLoadError, match="seed"):
        create_noisy_sample(manifest_path, "speech-1", data_root, snr_db=10, seed=-1)
    silence_path = data_root / "audio" / "silence.wav"
    sf.write(silence_path, np.zeros(1600), 16000, subtype="PCM_16")
    payload = json.loads(manifest_path.read_text())
    payload["samples"][0].update(
        audio_uri="audio/silence.wav",
        audio_sha256=hashlib.sha256(silence_path.read_bytes()).hexdigest(),
    )
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(DatasetLoadError, match="non-zero RMS"):
        create_noisy_sample(manifest_path, "speech-1", data_root, snr_db=10, seed=1)


def test_rejects_manifest_outside_data_root_and_output_symlink_escape(source_sample, tmp_path):
    data_root, manifest_path, _ = source_sample
    outside_manifest = tmp_path / "outside.json"
    outside_manifest.write_bytes(manifest_path.read_bytes())
    with pytest.raises(DatasetLoadError, match="source manifest must be beneath"):
        create_noisy_sample(outside_manifest, "speech-1", data_root, snr_db=10, seed=1)

    output_parent = data_root / "audio" / "generated"
    output_parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DatasetLoadError, match="output path escapes"):
        create_noisy_sample(manifest_path, "speech-1", data_root, snr_db=10, seed=1)
