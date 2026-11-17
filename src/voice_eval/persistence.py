"""Small SQLite repository for one-process evaluation runs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any


class _ClosingConnection(sqlite3.Connection):
    """Close the handle after the usual transaction context completes."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class RunRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, idem_key TEXT UNIQUE, request_hash TEXT NOT NULL,
              status TEXT NOT NULL, track TEXT NOT NULL, dataset TEXT NOT NULL,
              config TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
              finished_at TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS samples (
              run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
              sample_id TEXT NOT NULL, audio TEXT NOT NULL, reference_text TEXT,
              status TEXT NOT NULL, PRIMARY KEY(run_id,sample_id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL, sample_id TEXT NOT NULL, provider_id TEXT NOT NULL,
              model TEXT NOT NULL, options TEXT NOT NULL, attempt_no INTEGER NOT NULL,
              status TEXT NOT NULL, request_id TEXT, transcript TEXT, score TEXT,
              latency_ms REAL, ttft_ms REAL, finalization_ms REAL, error_code TEXT,
              error_message TEXT,
              FOREIGN KEY(run_id,sample_id) REFERENCES samples(run_id,sample_id)
            );
            CREATE INDEX IF NOT EXISTS attempts_run_idx ON attempts(run_id, sample_id);
            CREATE TABLE IF NOT EXISTS retry_requests (
              run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
              idempotency_key TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(run_id,idempotency_key)
            );
            """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, factory=_ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def create(self, run: dict[str, Any], samples: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(id,idem_key,request_hash,status,track,dataset,config,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    run["run_id"],
                    run["idempotency_key"],
                    run["request_hash"],
                    "queued",
                    run["track"],
                    json.dumps(run["dataset"]),
                    json.dumps(run["config"]),
                    run["created_at"],
                ),
            )
            db.executemany(
                "INSERT INTO samples(run_id,sample_id,audio,reference_text,status) "
                "VALUES(?,?,?,?,?)",
                [
                    (
                        run["run_id"],
                        s["sample_id"],
                        json.dumps(s["audio"]),
                        s["reference_text"],
                        "queued",
                    )
                    for s in samples
                ],
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            run = dict(row)
            run["dataset"] = json.loads(run["dataset"])
            run["config"] = json.loads(run["config"])
            return run

    def get_by_idempotency_key(self, idem_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT id FROM runs WHERE idem_key=?", (idem_key,)).fetchone()
        return self.get_run(row["id"]) if row is not None else None

    def samples(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM samples WHERE run_id=? ORDER BY sample_id", (run_id,)
            ).fetchall()
            output = []
            for row in rows:
                item = dict(row)
                item["audio"] = json.loads(item["audio"])
                item["attempts"] = [
                    dict(a)
                    for a in db.execute(
                        "SELECT * FROM attempts WHERE run_id=? AND sample_id=? "
                        "ORDER BY attempt_no,provider_id",
                        (run_id, row["sample_id"]),
                    ).fetchall()
                ]
                for attempt in item["attempts"]:
                    for key in ("score", "options"):
                        if attempt[key] is not None:
                            attempt[key] = json.loads(attempt[key])
                output.append(item)
            return output

    def list_runs(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id FROM runs"
                + (" WHERE status=?" if status else "")
                + " ORDER BY created_at",
                ((status,) if status else ()),
            ).fetchall()
        result = []
        for row in rows:
            run = self.get_run(row["id"])
            if run is not None:
                result.append(run)
        return result

    def count_active_runs(self) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM runs WHERE status IN ('queued','running','cancelling')"
            ).fetchone()
            return int(row[0])

    def update_run(self, run_id: str, **values: Any) -> None:
        if not values:
            return
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                (*values.values(), run_id),
            )

    def update_sample(self, run_id: str, sample_id: str, status: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE samples SET status=? WHERE run_id=? AND sample_id=?",
                (status, run_id, sample_id),
            )

    def retry(self, run_id: str, retry_key: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return False
            prior = db.execute(
                "SELECT 1 FROM retry_requests WHERE run_id=? AND idempotency_key=?",
                (run_id, retry_key),
            ).fetchone()
            if prior is not None:
                return False
            db.execute(
                "INSERT INTO retry_requests(run_id,idempotency_key) VALUES(?,?)",
                (run_id, retry_key),
            )
            db.execute(
                "UPDATE runs SET status='queued',started_at=NULL,finished_at=NULL,"
                "cancel_requested=0 WHERE id=?",
                (run_id,),
            )
            db.execute(
                "UPDATE samples SET status='queued' WHERE run_id=? "
                "AND status IN ('failed','interrupted')",
                (run_id,),
            )
            return True

    def has_retry_key(self, run_id: str, retry_key: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM retry_requests WHERE run_id=? AND idempotency_key=?",
                    (run_id, retry_key),
                ).fetchone()
                is not None
            )

    def add_attempt(
        self,
        run_id: str,
        sample_id: str,
        provider_id: str,
        model: str,
        options: dict[str, Any],
    ) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) FROM attempts "
                "WHERE run_id=? AND sample_id=? AND provider_id=?",
                (run_id, sample_id, provider_id),
            ).fetchone()
            attempt_no = int(row[0]) + 1
            cursor = db.execute(
                "INSERT INTO attempts(run_id,sample_id,provider_id,model,options,attempt_no,"
                "status) "
                "VALUES(?,?,?,?,?,?,?)",
                (run_id, sample_id, provider_id, model, json.dumps(options), attempt_no, "running"),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return the attempt identifier")
            return cursor.lastrowid

    def finish_attempt(self, attempt_id: int, **values: Any) -> None:
        for key in ("score",):
            if key in values and values[key] is not None:
                values[key] = json.dumps(values[key])
        with self.connect() as db:
            db.execute(
                "UPDATE attempts SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                (*values.values(), attempt_id),
            )

    def recovery(self) -> list[str]:
        with self.connect() as db:
            db.execute(
                "UPDATE attempts SET status='interrupted',error_code='interrupted',"
                "error_message='Process stopped during attempt' WHERE status='running'"
            )
            db.execute(
                "UPDATE samples SET status='interrupted' WHERE status IN ('running','retrying')"
            )
            db.execute(
                "UPDATE runs SET status='interrupted',finished_at=datetime('now') "
                "WHERE status IN ('running','cancelling')"
            )
            return [r[0] for r in db.execute("SELECT id FROM runs WHERE status='queued'")]
