"""Optional, lazy Hugging Face Datasets adapter with pinned provenance."""

from __future__ import annotations

import hashlib
import math
import mimetypes
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from voice_eval.datasets.manifest import (
    DatasetLoadError,
    DatasetLoadResult,
    DatasetSample,
    DatasetSnapshot,
)
from voice_eval.domain.contracts import AudioInput

_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SAMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class HuggingFaceLoadRequest(BaseModel):
    """Explicit dataset selection and column mapping for the optional HF loader."""

    model_config = ConfigDict(extra="forbid", strict=True)

    dataset_id: StrictStr = Field(min_length=3, max_length=256)
    config_name: StrictStr | None
    split: StrictStr = Field(min_length=1, max_length=128)
    revision: StrictStr | None = None
    audio_column: StrictStr = "audio"
    text_column: StrictStr = "text"
    id_column: StrictStr | None = None
    duration_ms_column: StrictStr | None = None
    language_column: StrictStr | None = None
    language: StrictStr | None = None
    locale: StrictStr | None = None
    transcript_style: StrictStr | None = None
    license: StrictStr | None = None

    @field_validator("dataset_id", "split", "audio_column", "text_column")
    @classmethod
    def reject_blank_selection(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dataset selection fields must not be blank")
        return value


def load_huggingface_dataset(
    request: HuggingFaceLoadRequest,
    data_root: str | Path,
) -> DatasetLoadResult:
    """Load a pinned (or explicitly unpinned) HF split without eager downloads.

    Imports `datasets` and `huggingface_hub` only when this function is called.
    The cache directory is below ``data_root``. Audio files returned from the
    HF cache are kept there or copied beneath it so the existing AudioInput
    relative-key contract remains valid.
    """

    try:
        datasets = import_module("datasets")
    except ImportError as error:
        raise DatasetLoadError(
            "Hugging Face support is optional; install the 'huggingface' extra"
        ) from error

    root = Path(data_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
    except OSError as error:
        raise DatasetLoadError(f"cannot prepare data root {root}: {error}") from error
    cache_dir = root / "cache" / "huggingface"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not cache_dir.resolve(strict=True).is_relative_to(root):
        raise DatasetLoadError("Hugging Face cache directory escapes the configured data root")

    resolved_revision = _resolve_revision(request)
    pinned = request.revision is not None and _COMMIT_SHA.fullmatch(request.revision) is not None

    try:
        dataset = datasets.load_dataset(
            request.dataset_id,
            name=request.config_name,
            split=request.split,
            revision=resolved_revision,
            cache_dir=str(cache_dir),
            streaming=True,
        )
        dataset = dataset.cast_column(request.audio_column, datasets.Audio(decode=False))
    except Exception as error:
        raise DatasetLoadError(f"cannot load Hugging Face dataset split: {error}") from error

    try:
        datasets_version = version("datasets")
    except PackageNotFoundError:
        datasets_version = getattr(__import__("datasets"), "__version__", "unknown")

    snapshot = DatasetSnapshot(
        dataset_id=request.dataset_id,
        source="huggingface",
        config_name=request.config_name,
        split=request.split,
        requested_revision=request.revision,
        resolved_revision=resolved_revision,
        license=request.license,
        language=request.language,
        locale=request.locale,
        transcript_style=request.transcript_style,
        datasets_version=datasets_version,
        reproducible=pinned,
    )

    def samples() -> Iterator[DatasetSample]:
        seen_ids: set[str] = set()
        for index, row in enumerate(dataset):
            if not isinstance(row, Mapping):
                raise DatasetLoadError(f"Hugging Face row {index} is not a mapping")
            if request.audio_column not in row:
                raise DatasetLoadError(f"audio column {request.audio_column!r} is missing")
            if request.text_column not in row:
                raise DatasetLoadError(f"reference column {request.text_column!r} is missing")

            reference = row[request.text_column]
            if reference is not None and not isinstance(reference, str):
                raise DatasetLoadError(
                    f"reference column {request.text_column!r} must be text or null"
                )

            audio_value = row[request.audio_column]
            if not isinstance(audio_value, Mapping):
                raise DatasetLoadError(f"audio column at row {index} is not a path/bytes mapping")
            audio_path = _materialize_audio(audio_value, root, cache_dir, request.dataset_id)
            digest = _hash_file(audio_path)
            duration_ms = _duration_ms(row, request, audio_value, audio_path)
            media_type = _audio_media_type(audio_path)
            if media_type is None or not media_type.startswith("audio/"):
                raise DatasetLoadError(
                    f"cannot determine audio media type for Hugging Face row {index}"
                )

            sample_id = _sample_id(row, request, index, digest)
            if sample_id in seen_ids:
                raise DatasetLoadError(f"duplicate sample ID {sample_id!r} in selected split")
            seen_ids.add(sample_id)

            relative_key = audio_path.relative_to(root).as_posix()
            try:
                audio = AudioInput(
                    storage_key=relative_key,
                    sha256=digest,
                    duration_ms=duration_ms,
                    media_type=media_type,
                )
                language = (
                    request.language
                    if request.language is not None
                    else _optional_text(row, request.language_column)
                )
                yield DatasetSample(
                    sample_id=sample_id,
                    audio=audio,
                    reference_text=reference,
                    language=language,
                    locale=request.locale,
                    transcript_style=request.transcript_style,
                    tags=[],
                )
            except Exception as error:
                raise DatasetLoadError(f"invalid Hugging Face row {index}: {error}") from error

    return DatasetLoadResult(snapshot=snapshot, samples=samples())


def _resolve_revision(request: HuggingFaceLoadRequest) -> str:
    requested = request.revision
    if requested is not None and _COMMIT_SHA.fullmatch(requested):
        return requested.lower()

    try:
        hub = import_module("huggingface_hub")

        info = hub.HfApi().dataset_info(request.dataset_id, revision=requested or "main")
    except ImportError as error:
        raise DatasetLoadError(
            "Hugging Face revision resolution requires the 'huggingface' extra"
        ) from error
    except Exception as error:
        raise DatasetLoadError(f"cannot resolve Hugging Face dataset revision: {error}") from error

    resolved = getattr(info, "sha", None)
    if not isinstance(resolved, str) or not _COMMIT_SHA.fullmatch(resolved):
        raise DatasetLoadError("Hugging Face did not return a valid immutable commit SHA")
    return resolved.lower()


def _materialize_audio(
    value: Mapping[str, Any],
    data_root: Path,
    cache_dir: Path,
    dataset_id: str,
) -> Path:
    source_path = value.get("path")
    source_bytes = value.get("bytes")
    if source_bytes is not None and not isinstance(source_bytes, bytes):
        raise DatasetLoadError("Hugging Face audio bytes must be bytes or null")

    if isinstance(source_path, str) and source_path:
        path = Path(source_path)
        if not path.is_absolute():
            path = cache_dir / path
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            resolved = None
        if resolved is not None and resolved.is_file():
            if resolved.is_relative_to(data_root):
                return resolved
            return _copy_audio_into_root(resolved, data_root, dataset_id)

    if source_bytes is not None:
        return _write_audio_bytes(source_bytes, value.get("path"), data_root, dataset_id)

    raise DatasetLoadError(
        "HF audio must provide a readable local path or source bytes; "
        "configure its cache under data_root"
    )


def _copy_audio_into_root(source: Path, data_root: Path, dataset_id: str) -> Path:
    digest = _hash_file(source)
    suffix = _safe_audio_suffix(source.suffix)
    target = _audio_asset_path(data_root, dataset_id, digest, suffix)
    if target.exists():
        resolved_target = target.resolve(strict=True)
        if not resolved_target.is_relative_to(data_root):
            raise DatasetLoadError("Hugging Face audio cache path escapes the configured data root")
        return resolved_target
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.resolve(strict=True).is_relative_to(data_root):
        raise DatasetLoadError("Hugging Face audio cache path escapes the configured data root")
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        with source.open("rb") as source_file:
            shutil.copyfileobj(source_file, temporary)
    temporary_path.replace(target)
    resolved_target = target.resolve(strict=True)
    if not resolved_target.is_relative_to(data_root):
        raise DatasetLoadError("Hugging Face audio cache path escapes the configured data root")
    return resolved_target


def _write_audio_bytes(data: bytes, source_path: Any, data_root: Path, dataset_id: str) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    source_suffix = Path(source_path).suffix if isinstance(source_path, str) else ""
    target = _audio_asset_path(data_root, dataset_id, digest, _safe_audio_suffix(source_suffix))
    if target.exists():
        resolved_target = target.resolve(strict=True)
        if not resolved_target.is_relative_to(data_root):
            raise DatasetLoadError("Hugging Face audio cache path escapes the configured data root")
        return resolved_target
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.resolve(strict=True).is_relative_to(data_root):
        raise DatasetLoadError("Hugging Face audio cache path escapes the configured data root")
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    temporary_path.replace(target)
    resolved_target = target.resolve(strict=True)
    if not resolved_target.is_relative_to(data_root):
        raise DatasetLoadError("Hugging Face audio cache path escapes the configured data root")
    return resolved_target


def _audio_asset_path(data_root: Path, dataset_id: str, digest: str, suffix: str) -> Path:
    dataset_key = hashlib.sha256(dataset_id.encode("utf-8")).hexdigest()[:16]
    return data_root / "cache" / "hf_audio" / dataset_key / f"{digest}{suffix}"


def _safe_audio_suffix(value: str) -> str:
    suffix = value.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return ".audio"
    return suffix


def _audio_media_type(path: Path) -> str | None:
    detected = mimetypes.guess_type(path.name)[0]
    if detected is not None and detected.startswith("audio/"):
        return detected
    try:
        with path.open("rb") as audio_file:
            header = audio_file.read(12)
    except OSError as error:
        raise DatasetLoadError(f"cannot read audio file {path}: {error}") from error
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"fLaC"):
        return "audio/flac"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    if header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
    ):
        return "audio/mpeg"
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return "audio/mp4"
    return None


def _duration_ms(
    row: Mapping[str, Any],
    request: HuggingFaceLoadRequest,
    audio_value: Mapping[str, Any],
    audio_path: Path,
) -> int:
    if request.duration_ms_column is not None:
        if request.duration_ms_column not in row:
            raise DatasetLoadError(f"duration column {request.duration_ms_column!r} is missing")
        value = row[request.duration_ms_column]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise DatasetLoadError("duration_ms column must contain a positive number")
        if not math.isfinite(value) or value <= 0:
            raise DatasetLoadError("duration_ms column must contain a positive finite number")
        return round(value)

    try:
        soundfile = import_module("soundfile")

        info = soundfile.info(str(audio_path))
    except ImportError as error:
        raise DatasetLoadError(
            "duration inference requires the 'huggingface' extra; or configure duration_ms_column"
        ) from error
    except Exception as error:
        raise DatasetLoadError(
            "cannot infer audio duration; configure a duration_ms_column for this dataset"
        ) from error
    if info.samplerate <= 0 or info.frames <= 0:
        raise DatasetLoadError("audio duration must be positive")
    return round(info.frames * 1000 / info.samplerate)


def _sample_id(
    row: Mapping[str, Any],
    request: HuggingFaceLoadRequest,
    index: int,
    audio_sha256: str,
) -> str:
    if request.id_column is not None:
        if request.id_column not in row:
            raise DatasetLoadError(f"sample ID column {request.id_column!r} is missing")
        value = row[request.id_column]
        if value is None or isinstance(value, bool) or not isinstance(value, str | int):
            raise DatasetLoadError("sample ID values must be non-empty strings or integers")
        sample_id = str(value)
    else:
        sample_id = f"sample-{index:08d}-{audio_sha256[:12]}"
    if not _SAMPLE_ID.fullmatch(sample_id):
        raise DatasetLoadError(f"invalid sample ID {sample_id!r}")
    return sample_id


def _optional_text(row: Mapping[str, Any], column: str | None) -> str | None:
    if column is None:
        return None
    if column not in row:
        raise DatasetLoadError(f"language column {column!r} is missing")
    value = row[column]
    if value is not None and not isinstance(value, str):
        raise DatasetLoadError(f"language column {column!r} must be text or null")
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as audio_file:
            for chunk in iter(lambda: audio_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise DatasetLoadError(f"cannot read audio file {path}: {error}") from error
    return digest.hexdigest()
