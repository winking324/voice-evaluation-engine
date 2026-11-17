"""Small LibriSpeech directory-to-manifest importer (audio remains in place)."""

from __future__ import annotations

import hashlib
import json
from importlib import import_module
from pathlib import Path

from voice_eval.datasets.manifest import DatasetLoadError


def build_librispeech_manifest(
    dataset_root: str | Path,
    output_path: str | Path,
    *,
    dataset_id: str = "librispeech-test-clean",
) -> Path:
    """Build a local manifest from LibriSpeech ``.trans.txt`` files.

    Audio paths are relative to ``dataset_root`` and are never copied. Duration
    extraction imports ``soundfile`` lazily, provided by the optional audio
    extra. The output manifest is deterministic for a fixed directory tree.
    """

    root = Path(dataset_root)
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise DatasetLoadError(f"LibriSpeech directory does not exist: {root}") from error
    if not root.is_dir():
        raise DatasetLoadError(f"LibriSpeech path is not a directory: {root}")

    try:
        soundfile = import_module("soundfile")
    except ImportError as error:
        raise DatasetLoadError(
            "LibriSpeech duration extraction requires the optional 'huggingface' extra"
        ) from error

    entries: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    transcript_files = sorted(root.rglob("*.trans.txt"))
    if not transcript_files:
        raise DatasetLoadError(f"no LibriSpeech .trans.txt files found under {root}")

    for transcript_file in transcript_files:
        try:
            lines = transcript_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise DatasetLoadError(
                f"cannot read transcript file {transcript_file}: {error}"
            ) from error
        for line_number, line in enumerate(lines, start=1):
            parts = line.split(maxsplit=1)
            if not parts:
                continue
            sample_id = parts[0]
            reference_text = parts[1] if len(parts) == 2 else ""
            if sample_id in seen_ids:
                raise DatasetLoadError(f"duplicate LibriSpeech utterance ID {sample_id!r}")
            seen_ids.add(sample_id)

            audio_path = transcript_file.parent / f"{sample_id}.flac"
            try:
                resolved_audio = audio_path.resolve(strict=True)
            except OSError as error:
                raise DatasetLoadError(
                    f"missing audio for {sample_id!r} at {audio_path} "
                    f"(transcript line {line_number})"
                ) from error
            if not resolved_audio.is_relative_to(root) or not resolved_audio.is_file():
                raise DatasetLoadError(
                    f"audio path escapes dataset root or is not a file: {audio_path}"
                )
            try:
                info = soundfile.info(str(resolved_audio))
            except Exception as error:
                raise DatasetLoadError(
                    f"cannot decode audio metadata for {audio_path}: {error}; "
                    "install the 'huggingface' extra and a compatible libsndfile decoder"
                ) from error
            if info.samplerate <= 0 or info.frames <= 0:
                raise DatasetLoadError(f"audio duration must be positive for {audio_path}")
            duration_ms = round(info.frames * 1000 / info.samplerate)
            if duration_ms <= 0:
                raise DatasetLoadError(
                    f"audio is too short to represent in milliseconds: {audio_path}"
                )
            entries.append(
                {
                    "sample_id": sample_id,
                    "audio_uri": resolved_audio.relative_to(root).as_posix(),
                    "audio_sha256": _hash_file(resolved_audio),
                    "duration_ms": duration_ms,
                    "reference_text": reference_text,
                }
            )

    if not entries:
        raise DatasetLoadError("LibriSpeech transcripts contain no utterances")

    manifest = {
        "dataset_id": dataset_id,
        "language": "en",
        "transcript_style": "verbatim",
        "samples": sorted(entries, key=lambda entry: str(entry["sample_id"])),
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as audio_file:
            for chunk in iter(lambda: audio_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise DatasetLoadError(f"cannot read LibriSpeech audio {path}: {error}") from error
    return digest.hexdigest()
