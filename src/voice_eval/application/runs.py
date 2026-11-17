"""Run orchestration: persistence is kept outside provider network awaits."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import statistics
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from voice_eval.application.audio_frames import decode_pcm16_frames
from voice_eval.application.streaming import replay_stream
from voice_eval.config import Settings
from voice_eval.datasets.manifest import DatasetLoadError, load_manifest
from voice_eval.domain.contracts import (
    AudioInput,
    StreamingTranscriptionOptions,
    TranscriptionOptions,
)
from voice_eval.evaluation.normalization import ENGLISH_LEXICAL_V1, RAW_TEXT_V1
from voice_eval.evaluation.scoring import score_transcript
from voice_eval.persistence import RunRepository
from voice_eval.providers.audio import AudioLoader
from voice_eval.providers.errors import ProviderError


class RunConflict(ValueError):
    pass


class RunCapacityError(RuntimeError):
    pass


class RunManager:
    def __init__(
        self,
        settings: Settings,
        repo: RunRepository,
        providers: dict[str, Any],
        audio_loader: AudioLoader,
    ) -> None:
        self.settings, self.repo, self.providers, self.audio_loader = (
            settings,
            repo,
            providers,
            audio_loader,
        )
        self.queue: asyncio.Queue[str] = asyncio.Queue(settings.worker_queue_capacity)
        self.global_semaphore = asyncio.Semaphore(settings.max_worker_concurrency)
        self.worker: asyncio.Task[None] | None = None
        self._stopping = False
        self._backlog: list[str] = []
        self.retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._creation_locks: dict[str, tuple[asyncio.Lock, int]] = {}

    async def start(self) -> None:
        pending = self.repo.recovery()
        self._backlog = pending
        self.worker = asyncio.create_task(self._work())
        self._refill_queue()

    async def stop(self) -> None:
        self._stopping = True
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        seen: set[int] = set()
        for provider in self.providers.values():
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    continue

    async def create(
        self, body: dict[str, Any], idem_key: str | None
    ) -> tuple[dict[str, Any], bool]:
        if not idem_key:
            raise RunConflict("Idempotency-Key header is required")
        entry = self._creation_locks.get(idem_key)
        if entry is None:
            lock = asyncio.Lock()
            self._creation_locks[idem_key] = (lock, 1)
        else:
            lock, users = entry
            self._creation_locks[idem_key] = (lock, users + 1)
        try:
            async with lock:
                return await self._create_unlocked(body, idem_key)
        finally:
            lock, users = self._creation_locks[idem_key]
            if users == 1:
                del self._creation_locks[idem_key]
            else:
                self._creation_locks[idem_key] = (lock, users - 1)

    async def _create_unlocked(
        self, body: dict[str, Any], idem_key: str
    ) -> tuple[dict[str, Any], bool]:
        if not idem_key:
            raise RunConflict("Idempotency-Key header is required")
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        existing = self.repo.get_by_idempotency_key(idem_key)
        if existing:
            if existing["request_hash"] != digest:
                raise RunConflict("Idempotency-Key was already used with a different request")
            return existing, False
        if self.repo.count_active_runs() >= self.settings.max_pending_runs or self.queue.full():
            raise RunCapacityError("run capacity is full")
        manifest_ref = body["dataset_manifest"]
        manifest_path = (self.settings.data_root / manifest_ref).resolve()
        root = self.settings.data_root.resolve()
        if not manifest_path.is_relative_to(root) or not manifest_path.is_file():
            raise DatasetLoadError("dataset manifest must be a file beneath configured data_root")
        loaded = await asyncio.to_thread(
            load_manifest,
            manifest_path,
            root,
            sample_limit=body["sample_limit"],
        )
        samples = list(loaded.samples)
        if not samples:
            raise DatasetLoadError("dataset has no samples")
        run_id, now = str(uuid.uuid4()), datetime.now(UTC).isoformat()
        snapshot = loaded.snapshot.model_dump(mode="json")
        snapshot.update(
            {
                "manifest_ref": manifest_ref,
                "sample_count": len(samples),
                "ground_truth_count": sum(s.reference_text is not None for s in samples),
            }
        )
        config = {k: v for k, v in body.items() if k != "dataset_manifest"}
        run = {
            "run_id": run_id,
            "idempotency_key": idem_key,
            "request_hash": digest,
            "track": body["track"],
            "dataset": snapshot,
            "config": config,
            "created_at": now,
        }
        try:
            self.repo.create(
                run,
                [
                    {
                        "sample_id": s.sample_id,
                        "audio": s.audio.model_dump(mode="json"),
                        "reference_text": s.reference_text,
                    }
                    for s in samples
                ],
            )
        except sqlite3.IntegrityError as exc:
            raced = self.repo.get_by_idempotency_key(idem_key)
            if raced is None:
                raise
            if raced["request_hash"] != digest:
                raise RunConflict(
                    "Idempotency-Key was already used with a different request"
                ) from exc
            return raced, False
        try:
            self.queue.put_nowait(run_id)
        except asyncio.QueueFull:
            self.repo.update_run(run_id, status="failed", finished_at=datetime.now(UTC).isoformat())
            raise RunCapacityError("worker queue is full") from None
        return self.repo.get_run(run_id), True  # type: ignore[return-value]

    async def _work(self) -> None:
        while not self._stopping:
            run_id = await self.queue.get()
            try:
                await self._execute(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                run = self.repo.get_run(run_id)
                if run and run["status"] in {"running", "cancelling"}:
                    for sample in self.repo.samples(run_id):
                        if sample["status"] in {"queued", "running", "retrying"}:
                            self.repo.update_sample(run_id, sample["sample_id"], "interrupted")
                    self.repo.update_run(
                        run_id, status="failed", finished_at=datetime.now(UTC).isoformat()
                    )
            finally:
                self.queue.task_done()
                self._refill_queue()

    def _refill_queue(self) -> None:
        while self._backlog and not self.queue.full():
            self.queue.put_nowait(self._backlog[0])
            self._backlog.pop(0)

    async def _execute(self, run_id: str) -> None:
        run = self.repo.get_run(run_id)
        if not run or run["status"] != "queued":
            return
        self.repo.update_run(run_id, status="running", started_at=datetime.now(UTC).isoformat())
        candidates = [
            s
            for s in self.repo.samples(run_id)
            if s["status"] in {"queued", "interrupted", "failed"}
        ]
        concurrency = min(run["config"].get("concurrency", 1), self.settings.max_worker_concurrency)
        next_sample = iter(candidates)

        async def process_available() -> None:
            while True:
                try:
                    sample = next(next_sample)
                except StopIteration:
                    return
                async with self.global_semaphore:
                    await self._process_sample(run_id, run, sample)

        await asyncio.gather(*(process_available() for _ in range(concurrency)))
        samples = self.repo.samples(run_id)
        if any(s["status"] == "cancelled" for s in samples):
            status = "cancelled"
        elif any(s["status"] in {"failed", "interrupted"} for s in samples):
            status = "failed"
        else:
            status = "succeeded"
        self.repo.update_run(run_id, status=status, finished_at=datetime.now(UTC).isoformat())

    async def _process_sample(
        self, run_id: str, run: dict[str, Any], sample: dict[str, Any]
    ) -> None:
        if sample["status"] not in {"queued", "interrupted", "failed"}:
            return
        state = self.repo.get_run(run_id)
        if state and state["cancel_requested"]:
            self.repo.update_sample(run_id, sample["sample_id"], "cancelled")
            return
        self.repo.update_sample(run_id, sample["sample_id"], "running")
        outcomes: list[dict[str, Any]] = []
        for provider in run["config"]["providers"]:
            state = self.repo.get_run(run_id)
            if state and state["cancel_requested"]:
                break
            provider_attempts = [
                attempt
                for attempt in sample["attempts"]
                if attempt["provider_id"] == provider["provider_id"]
            ]
            if provider_attempts and provider_attempts[-1]["status"] == "succeeded":
                outcomes.append({"status": "succeeded", "retryable": False})
                continue
            prior = sum(attempt["status"] != "interrupted" for attempt in provider_attempts)
            outcome = {"status": "failed"}
            attempt_budget = max(0, run["config"].get("max_attempts", 1) - prior)
            for attempt_index in range(attempt_budget):
                outcome = await self._attempt(run, sample, provider)
                if outcome["status"] == "succeeded" or not outcome.get("retryable", False):
                    break
                if attempt_index + 1 < attempt_budget:
                    self.repo.update_sample(run_id, sample["sample_id"], "retrying")
                    await self.retry_sleep(min(0.5 * (2**attempt_index), 5.0))
            outcomes.append(outcome)
        final_state = self.repo.get_run(run_id)
        status = (
            "cancelled"
            if final_state and final_state["cancel_requested"]
            else "failed"
            if any(o["status"] == "failed" for o in outcomes)
            else "succeeded"
        )
        self.repo.update_sample(run_id, sample["sample_id"], status)

    async def _attempt(
        self, run: dict[str, Any], sample: dict[str, Any], provider_cfg: dict[str, Any]
    ) -> dict[str, Any]:
        provider_id, model = provider_cfg["provider_id"], provider_cfg["model"]
        provider = self.providers.get(
            provider_id + "-stream" if run["track"] == "streaming" else provider_id
        )
        attempt_id = self.repo.add_attempt(
            run["id"],
            sample["sample_id"],
            provider_id,
            model,
            provider_cfg["options"],
        )
        start = time.perf_counter()
        try:
            if provider is None:
                raise RuntimeError(f"provider {provider_id} is not configured")
            audio = AudioInput.model_validate(sample["audio"])
            if run["track"] == "streaming":
                streaming_provider = provider
                if streaming_provider is None:
                    raise RuntimeError(f"streaming provider {provider_id} is not configured")
                stream_cfg = run["config"]["streaming"]
                raw = await self.audio_loader(audio)
                frames = decode_pcm16_frames(
                    raw,
                    media_type=audio.media_type,
                    target_sample_rate_hz=stream_cfg["sample_rate_hz"],
                    chunk_duration_ms=stream_cfg["chunk_duration_ms"],
                )
                stream_options = StreamingTranscriptionOptions(
                    model=model,
                    sample_rate_hz=stream_cfg["sample_rate_hz"],
                    channels=stream_cfg["channels"],
                    encoding=stream_cfg["encoding"],
                    language=provider_cfg.get("language"),
                    locale=provider_cfg.get("locale"),
                    endpointing_ms=stream_cfg.get("endpointing_ms"),
                    provider_options=provider_cfg["options"],
                )
                replay = await replay_stream(
                    streaming_provider,
                    stream_options,
                    frames,
                    completion_timeout_ms=stream_cfg["completion_timeout_ms"],
                    sleep=lambda delay: asyncio.sleep(
                        delay / stream_cfg.get("pacing_multiplier", 1.0)
                    ),
                )
                text = "".join(event.event.text for event in replay.events if event.event.is_final)
                request_id = replay.terminal_request_id
                ttft_ms = replay.ttft_ms
                finalization_ms = replay.post_audio_finalization_ms
                elapsed = replay.request_total_ms
            else:
                batch_options = TranscriptionOptions(
                    model=model,
                    language=provider_cfg.get("language"),
                    locale=provider_cfg.get("locale"),
                    timestamps=provider_cfg.get("timestamps", False),
                    provider_options=provider_cfg["options"],
                )
                result = await provider.transcribe(audio, batch_options)
                text, request_id = result.text, result.request_id
                ttft_ms = finalization_ms = None
                elapsed = (time.perf_counter() - start) * 1000
            profile = (
                ENGLISH_LEXICAL_V1
                if run["config"]["normalization_profile"] == "english-lexical"
                else RAW_TEXT_V1
            )
            score = (
                score_transcript(sample["reference_text"], text, profile)
                if sample["reference_text"] is not None
                else None
            )
            score_doc = (
                None
                if score is None
                else {
                    "profile_id": score.profile_id,
                    "profile_version": score.profile_version,
                    "normalized_reference": score.normalized_reference,
                    "normalized_hypothesis": score.normalized_hypothesis,
                    "wer": score.wer,
                    "cer": score.cer,
                    "word_counts": score.words.__dict__
                    if hasattr(score.words, "__dict__")
                    else {
                        k: getattr(score.words, k)
                        for k in (
                            "substitutions",
                            "deletions",
                            "insertions",
                            "reference_units",
                            "hypothesis_units",
                        )
                    },
                    "char_counts": {
                        k: getattr(score.characters, k)
                        for k in (
                            "substitutions",
                            "deletions",
                            "insertions",
                            "reference_units",
                            "hypothesis_units",
                        )
                    },
                }
            )
            self.repo.finish_attempt(
                attempt_id,
                status="succeeded",
                request_id=request_id,
                transcript=text,
                score=score_doc,
                latency_ms=elapsed,
                ttft_ms=ttft_ms,
                finalization_ms=finalization_ms,
            )
            return {"status": "succeeded", "retryable": False}
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            code = exc.code.value if isinstance(exc, ProviderError) else "provider_error"
            message = str(exc)
            self.repo.finish_attempt(
                attempt_id,
                status="failed",
                request_id=exc.request_id if isinstance(exc, ProviderError) else None,
                latency_ms=elapsed,
                error_code=code,
                error_message=message[:500],
            )
            return {
                "status": "failed",
                "retryable": isinstance(exc, ProviderError) and exc.retryable,
            }

    def progress(self, run_id: str) -> dict[str, Any]:
        samples = self.repo.samples(run_id)
        counts = {
            key: sum(s["status"] == key for s in samples)
            for key in (
                "queued",
                "running",
                "retrying",
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            )
        }
        return {
            "total_samples": len(samples),
            "queued_samples": counts["queued"],
            "running_samples": counts["running"],
            "retrying_samples": counts["retrying"],
            "succeeded_samples": counts["succeeded"],
            "failed_samples": counts["failed"],
            "cancelled_samples": counts["cancelled"],
            "interrupted_samples": counts["interrupted"],
        }

    def run_view(self, run_id: str) -> dict[str, Any] | None:
        run = self.repo.get_run(run_id)
        if not run:
            return None
        samples = self.repo.samples(run_id)
        configs = run["config"]["providers"]
        leaderboard = []
        for cfg in configs:
            attempts = [
                a
                for s in samples
                for a in s["attempts"]
                if a["provider_id"] == cfg["provider_id"] and a["status"] == "succeeded"
            ]
            scores = [a["score"] for a in attempts if a["score"]]
            latency = [a["latency_ms"] for a in attempts if a["latency_ms"] is not None]
            word_errors = sum(
                (s.get("word_counts") or {}).get("substitutions", 0)
                + (s.get("word_counts") or {}).get("deletions", 0)
                + (s.get("word_counts") or {}).get("insertions", 0)
                for s in scores
            )
            word_ref = sum((s.get("word_counts") or {}).get("reference_units", 0) for s in scores)
            char_errors = sum(
                (s.get("char_counts") or {}).get("substitutions", 0)
                + (s.get("char_counts") or {}).get("deletions", 0)
                + (s.get("char_counts") or {}).get("insertions", 0)
                for s in scores
            )
            char_ref = sum((s.get("char_counts") or {}).get("reference_units", 0) for s in scores)
            all_attempts = [
                a for s in samples for a in s["attempts"] if a["provider_id"] == cfg["provider_id"]
            ]
            ttft = [a["ttft_ms"] for a in all_attempts if a["ttft_ms"] is not None]
            finalization = [
                a["finalization_ms"] for a in all_attempts if a["finalization_ms"] is not None
            ]
            leaderboard.append(
                {
                    "provider_id": cfg["provider_id"],
                    "model": cfg["model"],
                    "status": run["status"],
                    "sample_count": len(samples),
                    "success_count": len(attempts),
                    "failure_count": sum(
                        a["provider_id"] == cfg["provider_id"] and a["status"] == "failed"
                        for s in samples
                        for a in s["attempts"]
                    ),
                    "coverage": len(attempts) / len(samples) if samples else 0,
                    "wer": word_errors / word_ref if word_ref else None,
                    "cer": char_errors / char_ref if char_ref else None,
                    "latency_p50_ms": _percentile(latency, 0.5),
                    "latency_p95_ms": _percentile(latency, 0.95),
                    "ttft_p50_ms": _percentile(ttft, 0.5),
                    "ttft_p95_ms": _percentile(ttft, 0.95),
                    "post_audio_finalization_p50_ms": _percentile(finalization, 0.5),
                    "estimated_cost_usd": None,
                }
            )
        return {
            "run_id": run["id"],
            "status": run["status"],
            "track": run["track"],
            "dataset": run["dataset"],
            "providers": configs,
            "normalization_profile": run["config"]["normalization_profile"],
            "concurrency": run["config"].get("concurrency", 1),
            "max_attempts": run["config"].get("max_attempts", 1),
            "streaming": run["config"].get("streaming"),
            "progress": self.progress(run_id),
            "leaderboard": leaderboard,
            "created_at": run["created_at"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "cancel_requested": bool(run["cancel_requested"]),
        }

    def retry_failed(self, run_id: str, retry_key: str | None) -> dict[str, Any] | None:
        run = self.repo.get_run(run_id)
        if run is None:
            return None
        if not retry_key:
            raise RunConflict("Idempotency-Key header is required")
        if self.repo.has_retry_key(run_id, retry_key):
            return self.run_view(run_id)
        if self.queue.full():
            raise RunCapacityError("worker queue is full")
        retryable = [
            s for s in self.repo.samples(run_id) if s["status"] in {"failed", "interrupted"}
        ]
        limit = run["config"].get("max_attempts", 1)
        attempt_limit_reached = False
        for sample in retryable:
            for provider in run["config"]["providers"]:
                attempts = [
                    attempt
                    for attempt in sample["attempts"]
                    if attempt["provider_id"] == provider["provider_id"]
                ]
                charged_attempts = sum(attempt["status"] != "interrupted" for attempt in attempts)
                if attempts and attempts[-1]["status"] != "succeeded" and charged_attempts >= limit:
                    attempt_limit_reached = True
        if attempt_limit_reached:
            raise RunConflict("max_attempts reached for a failed sample")
        if not retryable:
            raise RunConflict("run has no failed samples to retry")
        if not self.repo.retry(run_id, retry_key):
            return self.run_view(run_id)
        self.queue.put_nowait(run_id)
        return self.run_view(run_id)


def _mean(values: list[float | None]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return statistics.mean(vals) if vals else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(min(1.0, max(0.0, fraction)) * len(ordered)))
    return ordered[rank - 1]
