"""Create a reproducible, locally stored noisy variant of one manifest sample."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voice_eval.datasets.manifest import (
    DatasetLoadError,
    DatasetManifest,
    ManifestSample,
    _canonical_storage_key,
    _hash_file,
    _resolve_audio_path,
)

NOISE_ALGORITHM_VERSION = "gaussian-white-splitmix64-box-muller-v1"
_MAX_SEED = (1 << 63) - 1
_PCM16_PEAK = 1.0 - 1.0 / 32768.0


@dataclass(frozen=True, slots=True)
class NoisySampleArtifact:
    sample_id: str
    audio_path: Path
    manifest_path: Path
    audio_sha256: str
    actual_snr_db: float


def create_noisy_sample(
    manifest_path: str | Path,
    sample_id: str,
    data_root: str | Path,
    *,
    snr_db: float,
    seed: int,
) -> NoisySampleArtifact:
    """Write a deterministic PCM16 WAV and a one-sample manifest beneath root.

    The source audio and manifest are only read. If the mixed waveform would
    clip, a single gain is applied to the entire mixture, preserving SNR.
    """

    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= _MAX_SEED:
        raise DatasetLoadError("seed must be an integer between 0 and 2^63-1")
    if isinstance(snr_db, bool):
        raise DatasetLoadError("snr_db must be a finite number between -20 and 60")
    try:
        snr = float(snr_db)
    except (TypeError, ValueError) as error:
        raise DatasetLoadError("snr_db must be a finite number between -20 and 60") from error
    if not math.isfinite(snr) or not -20 <= snr <= 60:
        raise DatasetLoadError("snr_db must be a finite number between -20 and 60")

    try:
        root = Path(data_root).resolve(strict=True)
    except OSError as error:
        raise DatasetLoadError(f"data root does not exist: {data_root}") from error
    if not root.is_dir():
        raise DatasetLoadError(f"data root is not a directory: {root}")
    try:
        source_manifest_path = Path(manifest_path).resolve(strict=True)
    except OSError as error:
        raise DatasetLoadError(f"source manifest does not exist: {manifest_path}") from error
    if not source_manifest_path.is_relative_to(root):
        raise DatasetLoadError("source manifest must be beneath the configured data root")

    try:
        manifest_bytes = source_manifest_path.read_bytes()
        manifest = DatasetManifest.model_validate_json(manifest_bytes)
    except Exception as error:
        raise DatasetLoadError(f"invalid source manifest: {error}") from error
    source_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    source_entries = {entry.sample_id: entry for entry in manifest.samples}
    source_entry = source_entries.get(sample_id)
    if source_entry is None:
        raise DatasetLoadError(f"sample_id not found in source manifest: {sample_id}")
    source_audio_key = _canonical_storage_key(source_entry.audio_uri)
    source_path = _resolve_audio_path(source_audio_key, root)
    source_audio_sha256 = _hash_file(source_path)
    if source_entry.audio_sha256 is not None and source_entry.audio_sha256 != source_audio_sha256:
        raise DatasetLoadError(f"audio_sha256 mismatch for sample {sample_id!r}")

    try:
        import numpy as np
        import soundfile as sf
    except ImportError as error:
        raise DatasetLoadError(
            "noisy sample generation requires the 'audio' extra (numpy and soundfile)"
        ) from error

    try:
        signal, sample_rate = sf.read(source_path, dtype="float64", always_2d=True)
    except Exception as error:
        raise DatasetLoadError(f"cannot decode source audio {source_audio_key}: {error}") from error
    if signal.size == 0 or not np.isfinite(signal).all():
        raise DatasetLoadError("source audio must contain finite, non-empty samples")
    signal_rms = float(np.sqrt(np.mean(np.square(signal))))
    if not math.isfinite(signal_rms) or signal_rms <= 0:
        raise DatasetLoadError("source audio must have non-zero RMS")
    duration_ms = round(signal.shape[0] * 1000 / sample_rate)
    if duration_ms <= 0:
        raise DatasetLoadError("decoded source audio is shorter than one millisecond")

    identity = {
        "source_manifest_sha256": source_manifest_sha256,
        "source_sample_id": sample_id,
        "source_audio_sha256": source_audio_sha256,
        "algorithm": NOISE_ALGORITHM_VERSION,
        "snr_db": snr,
        "seed": seed,
    }
    identity_json = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    identity_hash = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
    output_sample_id = f"noisy-{identity_hash[:24]}"

    noise = _gaussian_noise(signal.size, seed, np)
    noise = noise.reshape(signal.shape)
    noise_rms = float(np.sqrt(np.mean(np.square(noise))))
    noise *= signal_rms / (10.0 ** (snr / 20.0) * noise_rms)
    mixed = signal + noise
    peak = float(np.max(np.abs(mixed)))
    gain = min(1.0, _PCM16_PEAK / peak) if peak > 0 else 1.0
    mixed *= gain
    measured_noise = mixed - signal * gain
    actual_snr_db = 20.0 * math.log10(
        float(np.sqrt(np.mean(np.square(signal * gain))))
        / float(np.sqrt(np.mean(np.square(measured_noise))))
    )

    # Keep outputs in the normal catalog directories so a later API scan can
    # discover the generated manifest when this root is mounted there.
    audio_key = f"audio/generated/noisy-samples/{identity_hash}.wav"
    output_manifest_key = f"manifests/noisy-sample-{identity_hash}.json"
    audio_path = _safe_output_path(root, audio_key)
    output_manifest_path = _safe_output_path(root, output_manifest_key)
    if output_manifest_path == source_manifest_path or audio_path == source_path:
        raise DatasetLoadError("generated output path collides with a source file")
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    output_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_contained(audio_path, root)
    _ensure_contained(output_manifest_path, root)

    temp_audio_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{audio_path.name}.",
            suffix=".tmp",
            dir=audio_path.parent,
            delete=False,
        ) as temp_audio:
            temp_audio_path = Path(temp_audio.name)
            sf.write(temp_audio, mixed, sample_rate, format="WAV", subtype="PCM_16")
            temp_audio.flush()
            os.fsync(temp_audio.fileno())
        os.replace(temp_audio_path, audio_path)
    except Exception as error:
        if temp_audio_path is not None:
            temp_audio_path.unlink(missing_ok=True)
        raise DatasetLoadError(f"cannot write generated WAV: {error}") from error
    output_sha256 = _sha256(audio_path)
    output_tags = [
        *source_entry.tags,
        f"source_sample_id:{sample_id}",
        "noise_type:gaussian_white",
        f"snr_db:{snr:g}",
        f"seed:{seed}",
        f"noise_algorithm:{NOISE_ALGORITHM_VERSION}",
        f"output_gain_db:{20.0 * math.log10(gain):.8f}",
        f"output_sha256:{output_sha256}",
    ]
    one_sample_manifest = DatasetManifest(
        dataset_id=f"{manifest.dataset_id[:183]}-noisy",
        dataset_revision=(
            f"{NOISE_ALGORITHM_VERSION};manifest={source_manifest_sha256};"
            f"identity={identity_hash}"
        ),
        license=manifest.license,
        language=manifest.language,
        locale=manifest.locale,
        transcript_style=manifest.transcript_style,
        samples=[
            ManifestSample(
                sample_id=output_sample_id,
                audio_uri=audio_key,
                audio_sha256=output_sha256,
                duration_ms=duration_ms,
                reference_text=source_entry.reference_text,
                language=source_entry.language,
                locale=source_entry.locale,
                tags=output_tags,
                media_type="audio/wav",
            )
        ],
    )
    _atomic_write(output_manifest_path, one_sample_manifest.model_dump_json(indent=2).encode("utf-8"))
    return NoisySampleArtifact(
        sample_id=output_sample_id,
        audio_path=audio_path,
        manifest_path=output_manifest_path,
        audio_sha256=output_sha256,
        actual_snr_db=actual_snr_db,
    )


def _gaussian_noise(size: int, seed: int, np: Any) -> Any:
    """Versioned SplitMix64 + Box-Muller stream, independent of NumPy RNG APIs."""
    mask = (1 << 64) - 1
    state = seed & mask
    values: list[float] = []
    while len(values) < size:
        state = (state + 0x9E3779B97F4A7C15) & mask
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        z ^= z >> 31
        u1 = ((z >> 11) + 0.5) / (1 << 53)
        state = (state + 0x9E3779B97F4A7C15) & mask
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        z ^= z >> 31
        u2 = ((z >> 11) + 0.5) / (1 << 53)
        radius = math.sqrt(-2.0 * math.log(u1))
        angle = 2.0 * math.pi * u2
        values.extend((radius * math.cos(angle), radius * math.sin(angle)))
    return np.fromiter(values[:size], dtype=np.float64, count=size)


def _safe_output_path(root: Path, key: str) -> Path:
    path = root.joinpath(*key.split("/"))
    try:
        resolved_parent = path.parent.resolve(strict=False)
    except OSError as error:
        raise DatasetLoadError(f"cannot resolve output path {key}: {error}") from error
    if not resolved_parent.is_relative_to(root):
        raise DatasetLoadError(f"output path escapes the configured data root: {key}")
    return resolved_parent / path.name


def _ensure_contained(path: Path, root: Path) -> None:
    if not path.parent.resolve(strict=True).is_relative_to(root):
        raise DatasetLoadError(f"output path escapes the configured data root: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except OSError as error:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise DatasetLoadError(f"cannot write generated manifest: {error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="source JSON manifest beneath data-root")
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--snr-db", required=True, type=float)
    parser.add_argument("--seed", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        artifact = create_noisy_sample(
            args.manifest,
            args.sample_id,
            args.data_root,
            snr_db=args.snr_db,
            seed=args.seed,
        )
    except DatasetLoadError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({
        "sample_id": artifact.sample_id,
        "manifest_path": str(artifact.manifest_path),
        "audio_path": str(artifact.audio_path),
        "audio_sha256": artifact.audio_sha256,
        "actual_snr_db": round(artifact.actual_snr_db, 4),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
