from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..experiment.store import migrate_v5_schema


class LiveStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.lock = threading.RLock()
        self._init()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init(self):
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS live_assessments (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                status TEXT NOT NULL,
                current_step TEXT NOT NULL,
                request_json TEXT NOT NULL,
                evidence_json TEXT,
                classification_json TEXT,
                plan_json TEXT,
                responses_json TEXT,
                metrics_json TEXT,
                report_json TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS live_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id TEXT NOT NULL,
                at REAL NOT NULL,
                step TEXT NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (assessment_id) REFERENCES live_assessments(id)
            );
            CREATE TABLE IF NOT EXISTS live_approvals (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id TEXT NOT NULL,
                gate TEXT NOT NULL,
                at REAL NOT NULL,
                actor TEXT NOT NULL,
                approved INTEGER NOT NULL,
                note TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (assessment_id) REFERENCES live_assessments(id)
            );
            CREATE INDEX IF NOT EXISTS live_assessments_created_idx
                ON live_assessments(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS live_assessments_status_created_idx
                ON live_assessments(status, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS live_assessments_event_created_idx
                ON live_assessments(
                    json_extract(request_json, '$.event_id'),
                    created_at DESC,
                    id DESC
                );
            """)
            migrate_v5_schema(db)

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), default=str)

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        value = dict(row)
        for key in tuple(value):
            if key.endswith("_json"):
                value[key[:-5]] = json.loads(value.pop(key) or "null")
        return value

    def create(self, assessment_id: str, request: dict):
        now = time.time()
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO live_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (assessment_id, now, now, "running", "collect", self._dump(request),
                 None, None, None, self._dump([]), None, None, None),
            )
        self.event(assessment_id, "configure", "step_complete",
                   "Assessment configuration saved", {"request": request})

    def get(self, assessment_id: str):
        with self._connect() as db:
            return self._decode(db.execute(
                "SELECT * FROM live_assessments WHERE id=?", (assessment_id,)
            ).fetchone())

    def list(self):
        with self._connect() as db:
            return [self._decode(r) for r in db.execute(
                "SELECT * FROM live_assessments ORDER BY created_at DESC"
            ).fetchall()]

    def assessment_page(
        self,
        *,
        page: int,
        page_size: int,
        event_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        """Return one filtered page without loading full assessment payloads."""
        clauses = ["json_type(request_json, '$.institutions') IS NOT NULL"]
        values: list[Any] = []
        if event_id:
            clauses.append("json_extract(request_json, '$.event_id') = ?")
            values.append(event_id)
        if status == "awaiting_approval":
            clauses.append("status GLOB ?")
            values.append("awaiting_*_approval")
        elif status:
            clauses.append("status = ?")
            values.append(status)

        where = " AND ".join(clauses)
        with self._connect() as db:
            total = int(db.execute(
                f"SELECT COUNT(*) FROM live_assessments WHERE {where}",
                values,
            ).fetchone()[0])
            total_pages = (total + page_size - 1) // page_size
            resolved_page = min(page, total_pages) if total_pages else 1
            offset = (resolved_page - 1) * page_size
            rows = db.execute(
                "SELECT id, created_at, updated_at, status, current_step, "
                "request_json, classification_json, responses_json, metrics_json "
                f"FROM live_assessments WHERE {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*values, page_size, offset],
            ).fetchall()

        return {
            "rows": [self._decode(row) for row in rows],
            "page": resolved_page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def update(self, assessment_id: str, *, status: str | None = None,
               current_step: str | None = None, error: str | None = None,
               **fields):
        parts, values = ["updated_at=?"], [time.time()]
        for name, value in (("status", status), ("current_step", current_step),
                            ("error", error)):
            if value is not None:
                parts.append(f"{name}=?")
                values.append(value)
        allowed = {"evidence", "classification", "plan", "responses",
                   "metrics", "report"}
        for name, value in fields.items():
            if name not in allowed:
                raise ValueError(f"invalid field {name}")
            parts.append(f"{name}_json=?")
            values.append(self._dump(value))
        values.append(assessment_id)
        with self.lock, self._connect() as db:
            db.execute(f"UPDATE live_assessments SET {', '.join(parts)} WHERE id=?",
                       values)

    def event(self, assessment_id: str, step: str, kind: str, message: str,
              payload: dict | None = None):
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO live_events(assessment_id,at,step,kind,message,payload_json) "
                "VALUES (?,?,?,?,?,?)",
                (assessment_id, time.time(), step, kind, message,
                 self._dump(payload or {})),
            )

    def events(self, assessment_id: str):
        with self._connect() as db:
            rows = db.execute(
                "SELECT seq,at,step,kind,message,payload_json FROM live_events "
                "WHERE assessment_id=? ORDER BY seq", (assessment_id,)
            ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload_json"]),
                 "payload_json": None} for r in rows]

    def approve(self, assessment_id: str, gate: str, actor: str, approved: bool,
                note: str, payload: dict):
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO live_approvals(assessment_id,gate,at,actor,approved,note,payload_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (assessment_id, gate, time.time(), actor, int(approved), note,
                 self._dump(payload)),
            )
        self.event(assessment_id, gate, "approval",
                   f"{actor} {'approved' if approved else 'rejected'} {gate}",
                   {"actor": actor, "approved": approved, "note": note, **payload})

    def approvals(self, assessment_id: str):
        with self._connect() as db:
            rows = db.execute(
                "SELECT seq,gate,at,actor,approved,note,payload_json FROM live_approvals "
                "WHERE assessment_id=? ORDER BY seq", (assessment_id,)
            ).fetchall()
        return [{**dict(r), "approved": bool(r["approved"]),
                 "payload": json.loads(r["payload_json"])} for r in rows]
