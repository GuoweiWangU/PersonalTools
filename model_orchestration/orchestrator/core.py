from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .workers import Worker, validate_result

ALLOWED_TASK_TYPES = {
    "data_audit", "data_split", "code_review", "document_edit",
    "experiment_prepare", "experiment_evaluate", "generic",
}
TERMINAL_STATUSES = {"succeeded", "blocked", "failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Orchestrator:
    def __init__(self, db_path: Path, artifacts_root: Path):
        self.db_path = db_path.resolve()
        self.artifacts_root = artifacts_root.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    task_type TEXT NOT NULL,
                    role TEXT NOT NULL,
                    input_ref TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    assigned_worker TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def create_task(self, task_type: str, role: str, input_ref: str, instructions: str,
                    assigned_worker: str, idempotency_key: str | None = None,
                    task_id: str | None = None) -> dict:
        if task_type not in ALLOWED_TASK_TYPES:
            raise ValueError(f"Unsupported task type: {task_type}")
        if not all(value.strip() for value in (role, input_ref, instructions, assigned_worker)):
            raise ValueError("role, input_ref, instructions, and assigned_worker must be non-empty")
        if task_id is not None and (not task_id.strip() or any(char.isspace() for char in task_id)):
            raise ValueError("task_id must be non-empty and contain no whitespace")
        identity = {
            "task_type": task_type, "role": role, "input_ref": input_ref,
            "instructions": instructions, "assigned_worker": assigned_worker,
        }
        key = idempotency_key or hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing:
                return dict(existing)
            task_id = task_id or "TASK-" + uuid.uuid4().hex[:16]
            now = utc_now()
            connection.execute("""
                INSERT INTO tasks (
                    task_id, idempotency_key, task_type, role, input_ref, instructions,
                    assigned_worker, status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """, (task_id, key, task_type, role, input_ref, instructions, assigned_worker, now, now))
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    def list_tasks(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def run_task(self, task_id: str, worker: Worker) -> dict:
        task = self.get_task(task_id)
        if task["status"] in {"succeeded", "blocked"}:
            return task
        if task["status"] == "running":
            raise RuntimeError(f"Task is already running: {task_id}")
        with self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET status='running', attempts=attempts+1, error=NULL, updated_at=? WHERE task_id=?",
                (utc_now(), task_id),
            )
        payload = {
            "task_id": task_id, "task_type": task["task_type"], "role": task["role"],
            "input_ref": task["input_ref"], "instructions": task["instructions"],
            "output_contract": "JSON with status, summary, artifacts, unknowns, blocking_issues",
        }
        try:
            result = validate_result(worker.run(payload))
            task_dir = self.artifacts_root / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "input.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (task_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            status = result["status"]
            error = "; ".join(result["blocking_issues"]) if status == "blocked" else None
            with self._connect() as connection:
                connection.execute(
                    "UPDATE tasks SET status=?, result_json=?, error=?, updated_at=? WHERE task_id=?",
                    (status, canonical_json(result), error, utc_now(), task_id),
                )
        except Exception as exc:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE tasks SET status='failed', error=?, updated_at=? WHERE task_id=?",
                    (f"{type(exc).__name__}: {exc}", utc_now(), task_id),
                )
        return self.get_task(task_id)
