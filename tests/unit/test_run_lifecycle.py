import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voice_eval.api.app import create_app
from voice_eval.application.runs import RunCapacityError, _percentile
from voice_eval.config import Settings
from voice_eval.datasets.manifest import load_manifest
from voice_eval.domain.contracts import AudioFrame, StreamingTranscriptEvent, TranscriptionResult
from voice_eval.persistence import RunRepository
from voice_eval.providers.errors import ProviderError, ProviderErrorCode


class FakeProvider:
    provider_id = "openai"

    def __init__(
        self,
        text: str = "hello world",
        fail: bool = False,
        provider_id: str = "openai",
    ) -> None:
        self.text, self.fail, self.provider_id = text, fail, provider_id
        self.calls = 0

    async def transcribe(self, audio, options):
        self.calls += 1
        if self.fail:
            raise RuntimeError("mock provider failure")
        return TranscriptionResult(
            provider_id=self.provider_id,
            model_id=options.model,
            text=self.text,
            request_id="req-test",
        )


def make_app(
    tmp_path: Path,
    *,
    reference: str | None = "hello world",
    fail: bool = False,
    sample_count: int = 1,
    providers=None,
):
    root = tmp_path / "data"
    (root / "manifests").mkdir(parents=True)
    (root / "audio.wav").write_bytes(b"fake audio")
    (root / "manifests" / "test.json").write_text(
        json.dumps(
            {
                "dataset_id": "test",
                "dataset_revision": "r1",
                "language": "en",
                "samples": [
                    {
                        "sample_id": f"s{index}",
                        "audio_uri": "audio.wav",
                        "duration_ms": 1000,
                        "reference_text": reference,
                        "media_type": "audio/wav",
                    }
                    for index in range(1, sample_count + 1)
                ],
            }
        )
    )
    settings = Settings(
        data_root=root,
        database_path=tmp_path / "runs.sqlite3",
        worker_queue_capacity=4,
        max_pending_runs=4,
    )

    async def loader(_):
        return b"fake audio"

    registry = providers if providers is not None else {"openai": FakeProvider(fail=fail)}
    return create_app(settings, providers=registry, audio_loader=loader)


def payload():
    return {
        "dataset_manifest": "manifests/test.json",
        "track": "batch",
        "sample_limit": 1,
        "providers": [{"provider_id": "openai", "model": "gpt-transcribe", "options": {}}],
    }


def wait_run(client: TestClient, run_id: str):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/runs/{run_id}").json()
        if result["status"] in {"succeeded", "failed", "cancelled"}:
            return result
        time.sleep(0.01)
    raise AssertionError("run worker did not finish")


def test_create_idempotency_and_wer(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path)) as client:
        first = client.post("/api/v1/runs", headers={"Idempotency-Key": "k1"}, json=payload())
        second = client.post("/api/v1/runs", headers={"Idempotency-Key": "k1"}, json=payload())
        assert first.status_code == 202 and second.status_code == 202
        assert first.json()["run_id"] == second.json()["run_id"]
        done = wait_run(client, first.json()["run_id"])
        assert done["status"] == "succeeded"
        assert done["leaderboard"][0]["wer"] == 0
        sample = client.get(f"/api/v1/runs/{done['run_id']}/samples").json()["items"][0]
        assert sample["providers"][0]["request_id"] == "req-test"


def test_percentile_uses_nearest_rank() -> None:
    assert _percentile([1, 2, 3, 4], 0.5) == 2
    assert _percentile([1, 2, 3, 4], 0.95) == 4


def test_leaderboard_wer_is_corpus_weighted(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    root = tmp_path / "data"
    (root / "audio-two.wav").write_bytes(b"second fake audio")
    (root / "manifests" / "test.json").write_text(
        json.dumps(
            {
                "dataset_id": "weighted",
                "samples": [
                    {
                        "sample_id": "short",
                        "audio_uri": "audio.wav",
                        "duration_ms": 1000,
                        "reference_text": "one",
                        "media_type": "audio/wav",
                    },
                    {
                        "sample_id": "long",
                        "audio_uri": "audio-two.wav",
                        "duration_ms": 1000,
                        "reference_text": "one two three",
                        "media_type": "audio/wav",
                    },
                ],
            }
        )
    )

    class WeightedProvider:
        provider_id = "openai"

        async def transcribe(self, audio, options):
            text = "" if audio.storage_key == "audio.wav" else "one two three"
            return TranscriptionResult(provider_id="openai", model_id=options.model, text=text)

    app.state.run_manager.providers["openai"] = WeightedProvider()
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs",
            headers={"Idempotency-Key": "weighted"},
            json={**payload(), "sample_limit": 2},
        )
        result = wait_run(client, created.json()["run_id"])
        assert result["leaderboard"][0]["wer"] == 0.25


def test_missing_ground_truth_is_unscored(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path, reference=None)) as client:
        created = client.post("/api/v1/runs", headers={"Idempotency-Key": "k2"}, json=payload())
        done = wait_run(client, created.json()["run_id"])
        item = client.get(f"/api/v1/runs/{done['run_id']}/samples").json()["items"][0]
        assert item["providers"][0]["score"] is None
        assert done["leaderboard"][0]["wer"] is None


def test_dataset_catalog_explains_run_time_fingerprint(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path)) as client:
        result = client.get("/api/v1/datasets")
        assert result.status_code == 200
        dataset = result.json()["items"][0]
        assert dataset["resolved_revision"] is None
        assert dataset["revision_status"] == "computed_on_run"
        assert dataset["reproducible"] is True
        assert "computes a content fingerprint" in dataset["revision_note"]


def test_dataset_catalog_previews_declared_audio_checksums(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    root = tmp_path / "data"
    manifest_path = root / "manifests" / "test.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["samples"][0]["audio_sha256"] = hashlib.sha256(
        (root / "audio.wav").read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    expected = load_manifest(manifest_path, root).snapshot.resolved_revision
    with TestClient(app) as client:
        dataset = client.get("/api/v1/datasets").json()["items"][0]
        assert dataset["resolved_revision"] == expected
        assert dataset["revision_status"] == "precomputed"
        assert dataset["reproducible"] is True


def test_dataset_catalog_ignores_manifest_symlink_outside_data_root(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "manifests").mkdir(parents=True)
    outside_manifest = tmp_path / "private.json"
    outside_manifest.write_text(
        json.dumps(
            {
                "dataset_id": "private-metadata",
                "samples": [
                    {
                        "sample_id": "s1",
                        "audio_uri": "outside.wav",
                        "duration_ms": 1000,
                        "reference_text": "private",
                        "media_type": "audio/wav",
                    }
                ],
            }
        )
    )
    try:
        (root / "manifests" / "leak.json").symlink_to(outside_manifest)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    settings = Settings(data_root=root, database_path=tmp_path / "runs.sqlite3")
    with TestClient(create_app(settings, providers={})) as client:
        assert client.get("/api/v1/datasets").json()["items"] == []


def test_provider_failure_is_recorded_and_retry_is_idempotent(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path, fail=True)) as client:
        created = client.post(
            "/api/v1/runs", headers={"Idempotency-Key": "k3"}, json={**payload(), "max_attempts": 1}
        )
        done = wait_run(client, created.json()["run_id"])
        assert done["status"] == "failed"
        samples = client.get(f"/api/v1/runs/{done['run_id']}/samples").json()["items"]
        assert samples[0]["providers"][0]["error"]["code"] == "provider_error"
        assert len(samples[0]["providers"]) == 1
        assert samples[0]["providers"][0]["attempt_no"] == 1
        rejected = client.post(
            f"/api/v1/runs/{done['run_id']}/retry-failed", headers={"Idempotency-Key": "retry1"}
        )
        assert rejected.status_code == 409


class FailsTwiceThenSucceeds:
    provider_id = "openai"

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, audio, options):
        self.calls += 1
        if self.calls <= 2:
            raise RuntimeError("temporary test failure")
        return TranscriptionResult(provider_id="openai", model_id=options.model, text="hello world")


def test_explicit_retry_budget_counts_prior_attempts_once(tmp_path: Path) -> None:
    provider = FailsTwiceThenSucceeds()
    app = make_app(tmp_path, providers={"openai": provider})
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs",
            headers={"Idempotency-Key": "retry-twice"},
            json={**payload(), "max_attempts": 3},
        )
        run_id = created.json()["run_id"]
        assert wait_run(client, run_id)["status"] == "failed"
        for retry_key, expected_status in (("retry-1", "failed"), ("retry-2", "succeeded")):
            retried = client.post(
                f"/api/v1/runs/{run_id}/retry-failed",
                headers={"Idempotency-Key": retry_key},
            )
            assert retried.status_code == 202
            assert wait_run(client, run_id)["status"] == expected_status
        attempts = client.get(f"/api/v1/runs/{run_id}/samples").json()["items"][0]["providers"]
        assert [attempt["attempt_no"] for attempt in attempts] == [1, 2, 3]
        assert provider.calls == 3


def test_queued_cancel_and_queue_capacity(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "manifests").mkdir(parents=True)
    (root / "audio.wav").write_bytes(b"fake audio")
    (root / "manifests" / "test.json").write_text(
        json.dumps(
            {
                "dataset_id": "test",
                "samples": [
                    {
                        "sample_id": "s1",
                        "audio_uri": "audio.wav",
                        "duration_ms": 1000,
                        "reference_text": "hello",
                        "media_type": "audio/wav",
                    }
                ],
            }
        )
    )
    settings = Settings(
        data_root=root,
        database_path=tmp_path / "queue.sqlite3",
        worker_queue_capacity=1,
        max_pending_runs=3,
    )

    async def loader(_):
        return b"fake audio"

    app = create_app(settings, providers={"openai": FakeProvider()}, audio_loader=loader)
    client = TestClient(app)
    created = client.post("/api/v1/runs", headers={"Idempotency-Key": "q1"}, json=payload())
    assert created.status_code == 202
    cancelled = client.post(f"/api/v1/runs/{created.json()['run_id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    full = client.post("/api/v1/runs", headers={"Idempotency-Key": "q2"}, json=payload())
    assert full.status_code == 503


def test_restart_marks_running_work_interrupted(tmp_path: Path) -> None:
    repo = RunRepository(tmp_path / "restart.sqlite3")
    run = {
        "run_id": "r1",
        "idempotency_key": "key",
        "request_hash": "hash",
        "track": "batch",
        "dataset": {},
        "config": {},
        "created_at": "2026-01-01T00:00:00Z",
    }
    repo.create(run, [{"sample_id": "s1", "audio": {}, "reference_text": None}])
    repo.update_run("r1", status="running")
    repo.update_sample("r1", "s1", "running")
    assert repo.recovery() == []
    assert repo.get_run("r1")["status"] == "interrupted"
    assert repo.samples("r1")[0]["status"] == "interrupted"


def test_repository_closes_connection_after_transaction(tmp_path: Path) -> None:
    repo = RunRepository(tmp_path / "close.sqlite3")
    connection = repo.connect()
    with connection as db:
        db.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_decode_flac_and_resample_to_pcm16() -> None:
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    pytest.importorskip("soxr")
    from io import BytesIO

    from voice_eval.application.audio_frames import decode_pcm16_frames

    source = BytesIO()
    sf.write(source, np.zeros(1600, dtype="float32"), 16000, format="FLAC")
    frames = decode_pcm16_frames(
        source.getvalue(),
        media_type="audio/flac",
        target_sample_rate_hz=24000,
        chunk_duration_ms=100,
    )
    assert len(frames) == 1
    assert frames[0].duration_ms == 100
    assert len(frames[0].data) == 2400 * 2


class GateProvider:
    provider_id = "openai"

    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0
        self.both_entered = asyncio.Event()

    async def transcribe(self, audio, options):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active == 2:
            self.both_entered.set()
        try:
            await asyncio.wait_for(self.both_entered.wait(), timeout=1)
            await asyncio.sleep(0.01)
        finally:
            self.active -= 1
        return TranscriptionResult(provider_id="openai", model_id=options.model, text="hello world")


def test_run_concurrency_is_bounded_and_effective(tmp_path: Path) -> None:
    app = make_app(tmp_path, sample_count=4)
    provider = GateProvider()
    app.state.run_manager.providers["openai"] = provider
    run_payload = {**payload(), "sample_limit": 4, "concurrency": 2}
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs", headers={"Idempotency-Key": "parallel"}, json=run_payload
        )
        done = wait_run(client, created.json()["run_id"])
        assert done["status"] == "succeeded"
        assert provider.maximum_active == 2
        assert provider.maximum_active <= 2


def test_explicit_empty_provider_registry_stays_empty(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data", database_path=tmp_path / "empty.sqlite3")
    app = create_app(settings, providers={})
    assert app.state.run_manager.providers == {}
    with TestClient(app) as client:
        rejected = client.post("/api/v1/runs", headers={"Idempotency-Key": "empty"}, json=payload())
        assert rejected.status_code == 422


def test_manager_stop_closes_each_unique_provider_once(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    class ClosableProvider:
        def __init__(self) -> None:
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1

    provider = ClosableProvider()
    manager = app.state.run_manager
    manager.providers = {"batch": provider, "stream": provider}
    asyncio.run(manager.stop())
    assert provider.close_calls == 1


def test_concurrent_same_idempotency_key_creates_one_run(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    manager = app.state.run_manager
    manager.settings.max_pending_runs = 1

    async def create_twice():
        return await asyncio.gather(
            manager.create(payload(), "concurrent-key"),
            manager.create(payload(), "concurrent-key"),
        )

    first, second = asyncio.run(create_twice())
    assert first[0]["id"] == second[0]["id"]
    assert sorted((first[1], second[1])) == [False, True]


def test_concurrent_distinct_keys_cannot_over_admit_capacity(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    manager = app.state.run_manager
    manager.settings.max_pending_runs = 1

    async def create_twice():
        return await asyncio.gather(
            manager.create(payload(), "capacity-a"),
            manager.create(payload(), "capacity-b"),
            return_exceptions=True,
        )

    results = asyncio.run(create_twice())
    assert sum(isinstance(result, tuple) for result in results) == 1
    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], RunCapacityError)
    assert manager.repo.count_active_runs() == 1


class RetryableThenSuccessProvider:
    provider_id = "openai"

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, audio, options):
        self.calls += 1
        if self.calls == 1:
            raise ProviderError(
                ProviderErrorCode.RATE_LIMIT,
                "retry later",
                retryable=True,
                provider_id="openai",
            )
        return TranscriptionResult(provider_id="openai", model_id=options.model, text="hello world")


def test_retryable_provider_error_retries_with_bounded_attempts(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    provider = RetryableThenSuccessProvider()
    app.state.run_manager.providers["openai"] = provider

    async def no_wait(_):
        return None

    app.state.run_manager.retry_sleep = no_wait
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs",
            headers={"Idempotency-Key": "retryable"},
            json={**payload(), "max_attempts": 2},
        )
        done = wait_run(client, created.json()["run_id"])
        assert done["status"] == "succeeded"
        attempts = client.get(f"/api/v1/runs/{done['run_id']}/samples").json()["items"][0][
            "providers"
        ]
        assert len(attempts) == 2
        assert [attempt["status"] for attempt in attempts] == ["failed", "succeeded"]


def test_retry_failed_keeps_successful_provider_attempts(tmp_path: Path) -> None:
    first = FakeProvider()
    second = FakeProvider(fail=True, provider_id="deepgram")
    app = make_app(tmp_path, providers={"openai": first, "deepgram": second})
    run_payload = {
        **payload(),
        "max_attempts": 2,
        "providers": [
            {"provider_id": "openai", "model": "gpt-transcribe", "options": {}},
            {"provider_id": "deepgram", "model": "nova-3", "options": {}},
        ],
    }
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs", headers={"Idempotency-Key": "partial"}, json=run_payload
        )
        run_id = created.json()["run_id"]
        assert wait_run(client, run_id)["status"] == "failed"
        assert first.calls == second.calls == 1
        second.fail = False
        retried = client.post(
            f"/api/v1/runs/{run_id}/retry-failed",
            headers={"Idempotency-Key": "partial-retry"},
        )
        assert retried.status_code == 202
        assert wait_run(client, run_id)["status"] == "succeeded"
        assert first.calls == 1
        assert second.calls == 2


class FakeStreamingSession:
    async def send_audio(self, frame):
        return None

    async def finish_audio(self):
        return None

    async def aclose(self):
        return None

    async def events(self):
        yield StreamingTranscriptEvent(
            text="hello world",
            is_final=True,
            is_terminal=True,
            request_id="stream-req",
        )


class FakeStreamingProvider:
    provider_id = "openai"

    async def open_session(self, options):
        return FakeStreamingSession()


def test_streaming_run_records_terminal_ttft_and_provider_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import voice_eval.application.runs as run_module

    monkeypatch.setattr(
        run_module,
        "decode_pcm16_frames",
        lambda *args, **kwargs: [AudioFrame(sequence=0, data=b"\x00\x00", duration_ms=1)],
    )
    app = make_app(
        tmp_path,
        providers={"openai-stream": FakeStreamingProvider()},
    )
    stream_payload = {
        **payload(),
        "track": "streaming",
        "streaming": {
            "sample_rate_hz": 24000,
            "channels": 1,
            "encoding": "linear16",
            "chunk_duration_ms": 1,
            "completion_timeout_ms": 1000,
            "pacing_multiplier": 10,
        },
    }
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs", headers={"Idempotency-Key": "stream"}, json=stream_payload
        )
        done = wait_run(client, created.json()["run_id"])
        attempt = client.get(f"/api/v1/runs/{done['run_id']}/samples").json()["items"][0][
            "providers"
        ][0]
        assert done["status"] == "succeeded"
        assert attempt["request_id"] == "stream-req"
        assert attempt["latency_ms"] is not None
        assert attempt["ttft_ms"] is not None
        assert attempt["post_audio_finalization_ms"] is not None
