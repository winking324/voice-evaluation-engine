"""Validated local dataset manifests and safe audio resolution."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from voice_eval.domain.contracts import AudioInput

_SAMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


class DatasetLoadError(ValueError):
    """Raised when a manifest or source dataset cannot be loaded safely."""


class ManifestSample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sample_id: StrictStr = Field(min_length=1, max_length=128)
    audio_uri: StrictStr = Field(min_length=1, max_length=2048)
    audio_sha256: StrictStr | None = None
    duration_ms: int = Field(gt=0)
    reference_text: StrictStr | None = None
    language: StrictStr | None = None
    locale: StrictStr | None = None
    tags: list[StrictStr] = Field(default_factory=list)
    media_type: StrictStr | None = None

    @field_validator("sample_id")
    @classmethod
    def validate_sample_id(cls, value: str) -> str:
        if not _SAMPLE_ID.fullmatch(value):
            raise ValueError("sample_id must use 1-128 ASCII letters, digits, '.', '_' or '-'")
        return value

    @field_validator("audio_sha256")
    @classmethod
    def validate_optional_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("audio_sha256 must be a 64-character hexadecimal digest")
        return value.lower() if value is not None else None


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    dataset_id: StrictStr = Field(min_length=1, max_length=200)
    dataset_revision: StrictStr | None = None
    license: StrictStr | None = None
    language: StrictStr | None = None
    locale: StrictStr | None = None
    transcript_style: StrictStr | None = None
    samples: list[ManifestSample] = Field(min_length=1)

    @field_validator("dataset_id")
    @classmethod
    def validate_dataset_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dataset_id must not be blank")
        return value

    @model_validator(mode="after")
    def sample_ids_are_unique(self) -> DatasetManifest:
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("sample_id values must be unique within a dataset manifest")
        return self


class DatasetSample(BaseModel):
    """Provider-neutral sample metadata with an already validated audio key."""

    model_config = ConfigDict(extra="forbid", strict=True)

    sample_id: str
    audio: AudioInput
    reference_text: str | None = None
    language: str | None = None
    locale: str | None = None
    transcript_style: str | None = None
    tags: list[str] = Field(default_factory=list)

    @property
    def has_ground_truth(self) -> bool:
        """An empty string is an available transcript for an intentional silence sample."""

        return self.reference_text is not None


class DatasetSnapshot(BaseModel):
    """Immutable dataset provenance to copy into an evaluation run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    dataset_id: str
    source: Literal["manifest", "huggingface"]
    config_name: str | None = None
    split: str | None = None
    requested_revision: str | None = None
    resolved_revision: str
    manifest_sha256: str | None = None
    license: str | None = None
    language: str | None = None
    locale: str | None = None
    transcript_style: str | None = None
    datasets_version: str | None = None
    reproducible: bool


@dataclass(frozen=True, slots=True)
class DatasetLoadResult:
    snapshot: DatasetSnapshot
    samples: Iterable[DatasetSample]


def load_manifest(
    manifest_path: str | Path,
    data_root: str | Path,
    *,
    sample_limit: int | None = None,
) -> DatasetLoadResult:
    """Load a JSON manifest, resolving every audio path beneath ``data_root``.

    The returned revision is content-addressed from the manifest and resolved
    audio checksums. Missing references are preserved as ``None``; malformed
    non-string references are rejected by validation.
    """

    path = Path(manifest_path)
    try:
        manifest_bytes = path.read_bytes()
    except OSError as error:
        raise DatasetLoadError(f"cannot read dataset manifest {path}: {error}") from error

    try:
        manifest = DatasetManifest.model_validate_json(manifest_bytes)
    except Exception as error:
        raise DatasetLoadError(f"invalid dataset manifest: {error}") from error

    root = Path(data_root)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise DatasetLoadError(f"data root does not exist: {root}") from error
    if not resolved_root.is_dir():
        raise DatasetLoadError(f"data root is not a directory: {resolved_root}")

    loaded_samples: list[DatasetSample] = []
    fingerprint = hashlib.sha256()
    fingerprint.update(manifest_bytes)

    entries = manifest.samples if sample_limit is None else manifest.samples[:sample_limit]
    if sample_limit is not None and sample_limit <= 0:
        raise DatasetLoadError("sample_limit must be greater than zero")

    for entry in entries:
        key = _canonical_storage_key(entry.audio_uri)
        audio_path = _resolve_audio_path(key, resolved_root)
        actual_sha256 = _hash_file(audio_path)
        if entry.audio_sha256 is not None and actual_sha256 != entry.audio_sha256:
            raise DatasetLoadError(
                f"audio_sha256 mismatch for sample {entry.sample_id!r}: {entry.audio_uri!r}"
            )

        media_type = entry.media_type or mimetypes.guess_type(audio_path.name)[0]
        if media_type is None or not media_type.startswith("audio/"):
            raise DatasetLoadError(
                f"cannot determine an audio media type for sample {entry.sample_id!r}; "
                "set media_type in the manifest"
            )

        try:
            audio = AudioInput(
                storage_key=key,
                sha256=actual_sha256,
                duration_ms=entry.duration_ms,
                media_type=media_type,
            )
            sample = DatasetSample(
                sample_id=entry.sample_id,
                audio=audio,
                reference_text=entry.reference_text,
                language=entry.language or manifest.language,
                locale=entry.locale or manifest.locale,
                transcript_style=manifest.transcript_style,
                tags=entry.tags,
            )
        except Exception as error:
            raise DatasetLoadError(f"invalid sample {entry.sample_id!r}: {error}") from error

        fingerprint.update(entry.sample_id.encode("utf-8"))
        fingerprint.update(bytes.fromhex(actual_sha256))
        loaded_samples.append(sample)

    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    resolved_revision = f"sha256:{fingerprint.hexdigest()}"
    snapshot = DatasetSnapshot(
        dataset_id=manifest.dataset_id,
        source="manifest",
        requested_revision=manifest.dataset_revision,
        resolved_revision=resolved_revision,
        manifest_sha256=manifest_sha256,
        license=manifest.license,
        language=manifest.language,
        locale=manifest.locale,
        transcript_style=manifest.transcript_style,
        reproducible=True,
    )
    return DatasetLoadResult(snapshot=snapshot, samples=loaded_samples)


def _canonical_storage_key(value: str) -> str:
    """Decode and validate a local, relative key before touching the filesystem."""

    if value != value.strip():
        raise DatasetLoadError("audio_uri must not have surrounding whitespace")
    decoded = value
    try:
        for _ in range(8):
            next_value = unquote(decoded, errors="strict")
            if next_value == decoded:
                break
            decoded = next_value
        else:
            raise DatasetLoadError("audio_uri has too many nested URL-encoding layers")
    except UnicodeDecodeError as error:
        raise DatasetLoadError("audio_uri has invalid URL encoding") from error

    parts = decoded.split("/")
    if (
        decoded.startswith(("/", "\\"))
        or "\\" in decoded
        or urlsplit(decoded).scheme
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise DatasetLoadError("audio_uri must be a canonical relative local path")
    return PurePosixPath(decoded).as_posix()


def _resolve_audio_path(storage_key: str, data_root: Path) -> Path:
    candidate = data_root.joinpath(*storage_key.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise DatasetLoadError(f"audio file does not exist: {storage_key}") from error
    if not resolved.is_relative_to(data_root):
        raise DatasetLoadError(f"audio path escapes the configured data root: {storage_key}")
    if not resolved.is_file():
        raise DatasetLoadError(f"audio path is not a file: {storage_key}")
    return resolved


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as audio_file:
            for chunk in iter(lambda: audio_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise DatasetLoadError(f"cannot read audio file {path}: {error}") from error
    return digest.hexdigest()
