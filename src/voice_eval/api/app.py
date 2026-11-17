"""Versioned API for creating and observing local evaluation runs."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from voice_eval.application.runs import RunCapacityError, RunConflict, RunManager
from voice_eval.config import Settings, get_settings
from voice_eval.datasets.manifest import DatasetLoadError
from voice_eval.persistence import RunRepository
from voice_eval.providers.audio import AudioLoader
from voice_eval.providers.deepgram import DeepgramProvider, DeepgramStreamingProvider
from voice_eval.providers.openai import OpenAIProvider, OpenAIRealtimeProvider


class ProviderSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: Literal["openai", "deepgram"]
    model: str = Field(min_length=1, max_length=100)
    options: dict[str, Any] = Field(default_factory=dict)
    language: str | None = None
    locale: str | None = None
    timestamps: bool = False

    @field_validator("options")
    @classmethod
    def reject_credentials(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = {
            "api_key",
            "apikey",
            "authorization",
            "token",
            "secret",
            "api-key",
            "client_secret",
            "password",
        }

        def visit(item: Any) -> bool:
            if isinstance(item, dict):
                return any(
                    str(key).lower().replace("-", "_") in forbidden or visit(child)
                    for key, child in item.items()
                )
            if isinstance(item, list):
                return any(visit(child) for child in item)
            return False

        if visit(value):
            raise ValueError("provider options must not contain credentials")
        return value


class StreamingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_rate_hz: int = Field(default=24000, ge=24000, le=24000)
    channels: Literal[1] = 1
    encoding: Literal["linear16", "pcm16"] = "linear16"
    chunk_duration_ms: int = Field(default=100, gt=0, le=2000)
    completion_timeout_ms: int = Field(default=30000, gt=0, le=120000)
    endpointing_ms: int | None = Field(default=None, gt=0)
    pacing_multiplier: float = Field(default=1.0, gt=0, le=10)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_manifest: str = Field(min_length=1, max_length=1024)
    track: Literal["batch", "streaming"] = "batch"
    sample_limit: int = Field(default=10, gt=0, le=10000)
    providers: list[ProviderSelection] = Field(min_length=1, max_length=4)
    normalization_profile: Literal["english-lexical", "raw-text"] = "english-lexical"
    concurrency: int = Field(default=1, gt=0, le=16)
    max_attempts: int = Field(default=1, gt=0, le=5)
    streaming: StreamingSettings | None = None

    @field_validator("providers")
    @classmethod
    def provider_ids_must_be_unique(cls, value: list[ProviderSelection]) -> list[ProviderSelection]:
        ids = [provider.provider_id for provider in value]
        if len(ids) != len(set(ids)):
            raise ValueError("each provider may appear only once in a run")
        return value

    @model_validator(mode="after")
    def track_configuration_matches(self) -> CreateRunRequest:
        if self.track == "streaming" and self.streaming is None:
            raise ValueError("streaming settings are required for streaming track")
        if self.track == "batch" and self.streaming is not None:
            raise ValueError("streaming settings are valid only for streaming track")
        return self


def _audio_loader(root: Path) -> AudioLoader:
    async def load(audio: Any) -> bytes:
        path = (root / audio.storage_key).resolve(strict=True)
        if not path.is_relative_to(root.resolve()):
            raise ValueError("audio key escaped data root")
        if path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("audio file exceeds the 100 MiB input limit")
        return await asyncio.to_thread(path.read_bytes)

    return load


def create_app(
    settings: Settings | None = None,
    *,
    providers: dict[str, Any] | None = None,
    repo: RunRepository | None = None,
    audio_loader: AudioLoader | None = None,
) -> FastAPI:
    selected = settings or get_settings()
    selected.data_root.mkdir(parents=True, exist_ok=True)
    repository = repo or RunRepository(selected.database_path)
    loader = audio_loader or _audio_loader(selected.data_root)
    configured = providers if providers is not None else _make_providers(selected, loader)
    manager = RunManager(selected, repository, configured, loader)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await manager.start()
        yield
        await manager.stop()

    app = FastAPI(title=selected.app_name, lifespan=lifespan)
    app.state.run_manager = manager
    app.state.run_repository = repository

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/datasets")
    async def datasets() -> dict[str, Any]:
        items = []
        data_root = selected.data_root.resolve(strict=True)
        manifest_dir = data_root / "manifests"
        if manifest_dir.is_dir():
            for path in sorted(manifest_dir.glob("*.json")):
                try:
                    from voice_eval.datasets.manifest import DatasetManifest

                    resolved_path = path.resolve(strict=True)
                    if not resolved_path.is_relative_to(data_root) or not resolved_path.is_file():
                        continue
                    manifest_bytes = resolved_path.read_bytes()
                    manifest = DatasetManifest.model_validate_json(manifest_bytes)
                    has_declared_audio_hashes = all(
                        sample.audio_sha256 is not None for sample in manifest.samples
                    )
                    preview_revision = None
                    if has_declared_audio_hashes:
                        fingerprint = hashlib.sha256()
                        fingerprint.update(manifest_bytes)
                        for sample in manifest.samples:
                            if sample.audio_sha256 is None:
                                continue
                            fingerprint.update(sample.sample_id.encode("utf-8"))
                            fingerprint.update(bytes.fromhex(sample.audio_sha256))
                        preview_revision = f"sha256:{fingerprint.hexdigest()}"
                    items.append(
                        {
                            "dataset_id": manifest.dataset_id,
                            "manifest_ref": path.relative_to(data_root).as_posix(),
                            "declared_revision": manifest.dataset_revision,
                            "resolved_revision": preview_revision,
                            "revision_status": (
                                "precomputed" if has_declared_audio_hashes else "computed_on_run"
                            ),
                            "revision_note": (
                                "Revision preview uses declared audio checksums."
                                if has_declared_audio_hashes
                                else "Run creation validates audio and computes "
                                "a content fingerprint."
                            ),
                            "license": manifest.license,
                            "language": manifest.language,
                            "locale": manifest.locale,
                            "sample_count": len(manifest.samples),
                            "ground_truth_count": sum(
                                s.reference_text is not None for s in manifest.samples
                            ),
                            "reproducible": True,
                        }
                    )
                except Exception:
                    continue
        return {"items": items}

    @app.get("/api/v1/providers")
    async def provider_list() -> dict[str, Any]:
        items = []
        for pid in ("openai", "deepgram"):
            batch_ready, stream_ready = pid in configured, pid + "-stream" in configured
            items.append(
                {
                    "provider_id": pid,
                    "capabilities": [
                        name
                        for name, ready in (("batch", batch_ready), ("streaming", stream_ready))
                        if ready
                    ],
                    "configured": batch_ready or stream_ready,
                    "default_models": {
                        "batch": "gpt-transcribe" if pid == "openai" else "nova-3",
                        "streaming": "gpt-live-transcribe" if pid == "openai" else "nova-3",
                    },
                }
            )
        return {"items": items}

    @app.post("/api/v1/runs", status_code=202)
    async def create_run(
        body: CreateRunRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if body.sample_limit > selected.max_samples_per_run:
            raise HTTPException(422, "sample_limit exceeds configured maximum")
        if body.concurrency > selected.max_worker_concurrency:
            raise HTTPException(422, "concurrency exceeds configured worker limit")
        missing = [
            selection.provider_id
            for selection in body.providers
            if selection.provider_id + ("-stream" if body.track == "streaming" else "")
            not in configured
        ]
        if missing:
            raise HTTPException(422, f"provider capability is not configured: {', '.join(missing)}")
        try:
            result, _ = await manager.create(body.model_dump(mode="json"), idempotency_key)
            view = manager.run_view(result["id"])
            assert view is not None
            return view
        except RunConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except RunCapacityError as exc:
            raise HTTPException(503, str(exc)) from exc
        except DatasetLoadError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        result = manager.run_view(run_id)
        if result is None:
            raise HTTPException(404, "run not found")
        return result

    @app.get("/api/v1/runs/{run_id}/progress")
    async def progress(run_id: str) -> dict[str, Any]:
        run = repository.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return {
            "run_id": run_id,
            "status": run["status"],
            "progress": manager.progress(run_id),
            "created_at": run["created_at"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
        }

    @app.get("/api/v1/runs/{run_id}/samples")
    async def samples(
        run_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        if repository.get_run(run_id) is None:
            raise HTTPException(404, "run not found")
        all_samples = repository.samples(run_id)
        items = []
        for s in all_samples[offset : offset + limit]:
            items.append(
                {
                    "sample_id": s["sample_id"],
                    "status": s["status"],
                    "reference_text": s["reference_text"],
                    "providers": [
                        {
                            "provider_id": a["provider_id"],
                            "model": a["model"],
                            "attempt_no": a["attempt_no"],
                            "status": a["status"],
                            "request_id": a["request_id"],
                            "transcript": a["transcript"],
                            "score": a["score"],
                            "latency_ms": a["latency_ms"],
                            "ttft_ms": a["ttft_ms"],
                            "post_audio_finalization_ms": a["finalization_ms"],
                            "error": {"code": a["error_code"], "message": a["error_message"]}
                            if a["error_code"]
                            else None,
                        }
                        for a in s["attempts"]
                    ],
                }
            )
        return {"items": items, "limit": limit, "offset": offset, "total": len(all_samples)}

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def cancel(run_id: str) -> dict[str, Any]:
        run = repository.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run["status"] == "queued":
            repository.update_run(
                run_id, status="cancelled", finished_at=datetime.now(UTC).isoformat()
            )
            for sample in repository.samples(run_id):
                repository.update_sample(run_id, sample["sample_id"], "cancelled")
        elif run["status"] == "running":
            repository.update_run(run_id, status="cancelling", cancel_requested=1)
        return manager.run_view(run_id)  # type: ignore[return-value]

    @app.post("/api/v1/runs/{run_id}/retry-failed", status_code=202)
    async def retry_failed(
        run_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")
    ) -> dict[str, Any]:
        try:
            result = manager.retry_failed(run_id, idempotency_key)
            if result is None:
                raise HTTPException(404, "run not found")
            return result
        except RunConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except RunCapacityError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/v1/runs/{run_id}/export")
    async def export_run(run_id: str, format: Literal["json", "csv"] = "json") -> Response:
        if repository.get_run(run_id) is None:
            raise HTTPException(404, "run not found")
        all_samples = repository.samples(run_id)
        payload: dict[str, Any] = {
            "run": manager.run_view(run_id),
            "samples": {
                "items": [
                    {
                        "sample_id": s["sample_id"],
                        "status": s["status"],
                        "reference_text": s["reference_text"],
                        "providers": [
                            {
                                "provider_id": a["provider_id"],
                                "model": a["model"],
                                "attempt_no": a["attempt_no"],
                                "status": a["status"],
                                "request_id": a["request_id"],
                                "transcript": a["transcript"],
                                "score": a["score"],
                                "latency_ms": a["latency_ms"],
                                "ttft_ms": a["ttft_ms"],
                                "post_audio_finalization_ms": a["finalization_ms"],
                                "error": {"code": a["error_code"], "message": a["error_message"]}
                                if a["error_code"]
                                else None,
                            }
                            for a in s["attempts"]
                        ],
                    }
                    for s in all_samples
                ],
                "total": len(all_samples),
            },
        }
        if format == "json":
            return Response(json.dumps(payload, ensure_ascii=False), media_type="application/json")
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(
            [
                "sample_id",
                "provider_id",
                "model",
                "attempt_no",
                "status",
                "transcript",
                "wer",
                "cer",
                "latency_ms",
                "ttft_ms",
                "error_code",
            ]
        )
        for sample in payload["samples"]["items"]:
            for attempt in sample["providers"]:
                score = attempt["score"] or {}
                error = attempt["error"] or {}
                writer.writerow(
                    [
                        sample["sample_id"],
                        attempt["provider_id"],
                        attempt["model"],
                        attempt["attempt_no"],
                        attempt["status"],
                        attempt["transcript"],
                        score.get("wer"),
                        score.get("cer"),
                        attempt["latency_ms"],
                        attempt["ttft_ms"],
                        error.get("code"),
                    ]
                )
        return Response(stream.getvalue(), media_type="text/csv")

    if selected.web_dist_dir is not None:
        web_dist_dir = selected.web_dist_dir.resolve(strict=True)
        if not web_dist_dir.is_dir():
            raise ValueError(f"configured web_dist_dir is not a directory: {web_dist_dir}")
        app.mount("/", StaticFiles(directory=web_dist_dir, html=True), name="web")

    return app


def _make_providers(settings: Settings, loader: AudioLoader) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if settings.openai_api_key:
        result["openai"] = OpenAIProvider(settings.openai_api_key, loader)
        result["openai-stream"] = OpenAIRealtimeProvider(settings.openai_api_key)
    if settings.deepgram_api_key:
        result["deepgram"] = DeepgramProvider(settings.deepgram_api_key, loader)
        result["deepgram-stream"] = DeepgramStreamingProvider(settings.deepgram_api_key)
    return result


app = create_app()
