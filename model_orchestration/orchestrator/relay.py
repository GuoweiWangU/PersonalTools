from __future__ import annotations

import json
import sqlite3
from typing import Mapping

from .core import Orchestrator, canonical_json
from .workers import Worker

PROTOCOL = "model-relay-v1"
TASK_FIELDS = (
    "task_id", "idempotency_key", "target_bridge", "assigned_worker",
    "task_type", "role", "input_ref", "instructions",
)


class RelayMessageError(ValueError):
    pass


def validate_task_envelope(envelope: dict) -> dict:
    if envelope.get("protocol") != PROTOCOL or envelope.get("kind") != "task":
        raise RelayMessageError("Expected a model-relay-v1 task message")
    for field in TASK_FIELDS:
        if not isinstance(envelope.get(field), str) or not envelope[field].strip():
            raise RelayMessageError(f"Task field must be a non-empty string: {field}")
    return envelope


def encode_relay_message(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


class RelayBridge:
    """Route relay tasks to isolated local workers without calling a chat service."""

    def __init__(self, bridge_id: str, controller: Orchestrator, workers: Mapping[str, Worker]):
        if not bridge_id.strip():
            raise ValueError("bridge_id must be non-empty")
        if not workers:
            raise ValueError("At least one worker must be registered")
        self.bridge_id = bridge_id
        self.controller = controller
        self.workers = dict(workers)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS relay_deliveries (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.controller.db_path, timeout=30)

    def handle_event(self, event: dict) -> dict | None:
        return self.handle_message(
            event.get("content", ""),
            event_id=event.get("event_id") or event.get("message_id"),
            reply_to_message_id=event.get("message_id"),
        )

    def handle_message(self, content: str, event_id: str | None = None,
                       reply_to_message_id: str | None = None) -> dict | None:
        try:
            envelope = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(envelope, dict) or envelope.get("protocol") != PROTOCOL:
            return None
        if envelope.get("kind") != "task" or envelope.get("target_bridge") != self.bridge_id:
            return None

        delivery_id = event_id or f"local:{envelope.get('task_id')}:{envelope.get('idempotency_key')}"
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT response_json FROM relay_deliveries WHERE event_id = ?", (delivery_id,)
            ).fetchone()
        if previous:
            return json.loads(previous[0])

        try:
            task_message = validate_task_envelope(envelope)
            response = self._execute(task_message, reply_to_message_id)
        except Exception as exc:
            response = self._error_response(envelope, reply_to_message_id, exc)

        with self._connect() as connection:
            connection.execute(
                "INSERT INTO relay_deliveries (event_id, task_id, response_json) VALUES (?, ?, ?)",
                (delivery_id, response["task_id"], canonical_json(response)),
            )
        return response

    def _execute(self, envelope: dict, reply_to_message_id: str | None) -> dict:
        worker_id = envelope["assigned_worker"]
        worker = self.workers.get(worker_id)
        if worker is None:
            raise RelayMessageError(f"Worker is not registered on {self.bridge_id}: {worker_id}")
        task = self.controller.create_task(
            task_type=envelope["task_type"],
            role=envelope["role"],
            input_ref=envelope["input_ref"],
            instructions=envelope["instructions"],
            assigned_worker=worker_id,
            idempotency_key=envelope["idempotency_key"],
            task_id=envelope["task_id"],
        )
        task = self.controller.run_task(task["task_id"], worker)
        result = json.loads(task["result_json"]) if task["result_json"] else {
            "status": "failed",
            "summary": "Worker did not produce a valid result",
            "artifacts": [],
            "unknowns": [],
            "blocking_issues": [],
        }
        response = {
            "protocol": PROTOCOL,
            "kind": "result",
            "task_id": task["task_id"],
            "idempotency_key": task["idempotency_key"],
            "bridge_id": self.bridge_id,
            "worker_id": worker_id,
            **{field: result[field] for field in (
                "status", "summary", "artifacts", "unknowns", "blocking_issues"
            )},
            "error": task["error"],
            "reply_to_message_id": envelope.get("reply_to_message_id") or reply_to_message_id,
        }
        return response

    def _error_response(self, envelope: dict, reply_to_message_id: str | None,
                        error: Exception) -> dict:
        detail = f"{type(error).__name__}: {error}"
        return {
            "protocol": PROTOCOL,
            "kind": "result",
            "task_id": envelope.get("task_id") or "UNKNOWN",
            "idempotency_key": envelope.get("idempotency_key") or "UNKNOWN",
            "bridge_id": self.bridge_id,
            "worker_id": envelope.get("assigned_worker") or "UNKNOWN",
            "status": "failed",
            "summary": "Relay task was rejected before worker execution",
            "artifacts": [],
            "unknowns": [],
            "blocking_issues": [detail],
            "error": detail,
            "reply_to_message_id": envelope.get("reply_to_message_id") or reply_to_message_id,
        }
