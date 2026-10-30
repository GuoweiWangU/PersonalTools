from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .core import canonical_json, utc_now


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:16]


class ControlStore:
    """Durable state for long-lived tasks, turns, plans, approvals and artifacts."""

    def __init__(self, db_path: Path):
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    goal_id TEXT,
                    step_id TEXT,
                    worker_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    session_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS agent_turns (
                    turn_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    reply_to_message_id TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    stop_reason TEXT,
                    FOREIGN KEY(task_id) REFERENCES agent_tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS turns_by_task ON agent_turns(task_id, created_at);
                CREATE TABLE IF NOT EXISTS goals (
                    goal_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plan_steps (
                    step_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    instructions TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    depends_json TEXT NOT NULL,
                    expected_json TEXT NOT NULL,
                    resources_json TEXT NOT NULL DEFAULT '[]',
                    task_id TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    UNIQUE(goal_id, position)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    valid INTEGER NOT NULL,
                    validation_error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS control_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    turn_id TEXT,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    turn_id TEXT,
                    pid INTEGER NOT NULL,
                    command_json TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS command_deliveries (
                    event_id TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resource_locks (
                    resource TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(plan_steps)").fetchall()}
            if "resources_json" not in columns:
                db.execute("ALTER TABLE plan_steps ADD COLUMN resources_json TEXT NOT NULL DEFAULT '[]'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, event_type: str, message: str, task_id: str | None = None,
              turn_id: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO control_events(task_id,turn_id,event_type,message,created_at) VALUES(?,?,?,?,?)",
                (task_id, turn_id, event_type, message, utc_now()),
            )

    def create_task(self, instructions: str, worker_id: str, cwd: Path,
                    goal_id: str | None = None, step_id: str | None = None,
                    reply_to_message_id: str | None = None) -> tuple[dict, dict]:
        task_id, turn_id = new_id("TASK-"), new_id("TURN-")
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(task_id,goal_id,step_id,worker_id,cwd,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'queued',?,?)",
                (task_id, goal_id, step_id, worker_id, str(cwd.resolve()), now, now),
            )
            db.execute(
                "INSERT INTO agent_turns(turn_id,task_id,instructions,status,reply_to_message_id,created_at) "
                "VALUES(?,?,?,'queued',?,?)",
                (turn_id, task_id, instructions, reply_to_message_id, now),
            )
            db.execute(
                "INSERT INTO control_events(task_id,turn_id,event_type,message,created_at) VALUES(?,?,?,?,?)",
                (task_id, turn_id, "task_created", instructions, now),
            )
        return self.get_task(task_id), self.get_turn(turn_id)

    def enqueue_turn(self, task_id: str, instructions: str,
                     reply_to_message_id: str | None = None) -> dict:
        task = self.get_task(task_id)
        if task["status"] in {"closed", "cancelled"}:
            raise ValueError(f"Task is {task['status']}: {task_id}")
        turn_id, now = new_id("TURN-"), utc_now()
        with self.connect() as db:
            active = db.execute(
                "SELECT turn_id FROM agent_turns WHERE task_id=? AND status IN('queued','running','waiting_approval')",
                (task_id,),
            ).fetchone()
            if active:
                raise ValueError(f"Task already has an active turn: {active['turn_id']}")
            db.execute(
                "INSERT INTO agent_turns(turn_id,task_id,instructions,status,reply_to_message_id,created_at) "
                "VALUES(?,?,?,'queued',?,?)", (turn_id, task_id, instructions, reply_to_message_id, now),
            )
            db.execute("UPDATE agent_tasks SET status='queued',updated_at=? WHERE task_id=?", (now, task_id))
            db.execute(
                "INSERT INTO control_events(task_id,turn_id,event_type,message,created_at) VALUES(?,?,?,?,?)",
                (task_id, turn_id, "turn_queued", instructions, now),
            )
        return self.get_turn(turn_id)

    def claim_turn(self, turn_id: str) -> tuple[dict, dict]:
        now = utc_now()
        with self.connect() as db:
            turn = db.execute("SELECT * FROM agent_turns WHERE turn_id=?", (turn_id,)).fetchone()
            if turn is None:
                raise KeyError(turn_id)
            if turn["status"] == "cancelled":
                raise ValueError(f"Turn was cancelled: {turn_id}")
            if turn["status"] != "queued":
                raise ValueError(f"Turn is not queued: {turn_id} ({turn['status']})")
            changed = db.execute(
                "UPDATE agent_turns SET status='running',started_at=?,heartbeat_at=? WHERE turn_id=? AND status='queued'",
                (now, now, turn_id),
            ).rowcount
            if changed != 1:
                raise ValueError(f"Turn could not be claimed: {turn_id}")
            db.execute("UPDATE agent_tasks SET status='running',updated_at=? WHERE task_id=?", (now, turn["task_id"]))
            db.execute(
                "INSERT INTO control_events(task_id,turn_id,event_type,message,created_at) VALUES(?,?,?,?,?)",
                (turn["task_id"], turn_id, "turn_started", "Worker started", now),
            )
        turn = self.get_turn(turn_id)
        return self.get_task(turn["task_id"]), turn

    def heartbeat(self, turn_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE agent_turns SET heartbeat_at=? WHERE turn_id=?", (utc_now(), turn_id))

    def save_session(self, task_id: str, session_id: str) -> None:
        if not session_id:
            raise ValueError("session_id must be non-empty")
        with self.connect() as db:
            current = db.execute("SELECT session_id FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(task_id)
            if current["session_id"] and current["session_id"] != session_id:
                raise ValueError("A task cannot silently switch its ACP session")
            db.execute(
                "UPDATE agent_tasks SET session_id=?,updated_at=? WHERE task_id=?",
                (session_id, utc_now(), task_id),
            )

    def complete_turn(self, turn_id: str, status: str, result: dict | None = None,
                      error: str | None = None, stop_reason: str | None = None) -> dict:
        if status not in {"succeeded", "blocked", "failed", "cancelled"}:
            raise ValueError(status)
        now = utc_now()
        task_status = {"succeeded": "waiting_input", "blocked": "blocked",
                       "failed": "failed", "cancelled": "cancelled"}[status]
        with self.connect() as db:
            turn = db.execute("SELECT task_id FROM agent_turns WHERE turn_id=?", (turn_id,)).fetchone()
            if turn is None:
                raise KeyError(turn_id)
            current_task = db.execute(
                "SELECT status FROM agent_tasks WHERE task_id=?", (turn["task_id"],)
            ).fetchone()
            if current_task["status"] in {"cancelled", "closed"}:
                task_status = current_task["status"]
            db.execute(
                "UPDATE agent_turns SET status=?,result_json=?,error=?,stop_reason=?,finished_at=?,heartbeat_at=? "
                "WHERE turn_id=?", (status, canonical_json(result) if result else None,
                                   error, stop_reason, now, now, turn_id),
            )
            db.execute(
                "UPDATE agent_tasks SET status=?,updated_at=? WHERE task_id=?",
                (task_status, now, turn["task_id"]),
            )
            db.execute(
                "INSERT INTO control_events(task_id,turn_id,event_type,message,created_at) VALUES(?,?,?,?,?)",
                (turn["task_id"], turn_id, "turn_finished", error or status, now),
            )
        return self.get_turn(turn_id)

    def get_task(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    def get_turn(self, turn_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM agent_turns WHERE turn_id=?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return dict(row)

    def list_tasks(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT t.*,(SELECT COUNT(*) FROM agent_turns u WHERE u.task_id=t.task_id) turn_count "
                "FROM agent_tasks t ORDER BY t.updated_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def task_turns(self, task_id: str, limit: int = 20) -> list[dict]:
        self.get_task(task_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM agent_turns WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def logs(self, task_id: str, limit: int = 20) -> list[dict]:
        self.get_task(task_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM control_events WHERE task_id=? ORDER BY event_id DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def close_task(self, task_id: str) -> dict:
        self.get_task(task_id)
        with self.connect() as db:
            active = db.execute(
                "SELECT turn_id FROM agent_turns WHERE task_id=? AND status IN('queued','running','waiting_approval')",
                (task_id,),
            ).fetchone()
            if active:
                raise ValueError("Cancel the active turn before closing this task")
            now = utc_now()
            db.execute(
                "UPDATE agent_tasks SET status='closed',closed_at=?,updated_at=? WHERE task_id=?",
                (now, now, task_id),
            )
        self.event("task_closed", "Task closed by user", task_id)
        return self.get_task(task_id)

    def cancel_task(self, task_id: str) -> list[str]:
        self.get_task(task_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT turn_id FROM agent_turns WHERE task_id=? AND status IN('queued','running','waiting_approval')",
                (task_id,),
            ).fetchall()
            now = utc_now()
            db.execute("UPDATE agent_tasks SET status='cancelled',updated_at=? WHERE task_id=?", (now, task_id))
            db.execute(
                "UPDATE agent_turns SET status='cancelled',finished_at=? WHERE task_id=? "
                "AND status='queued'", (now, task_id),
            )
        self.event("cancel_requested", "Cancellation requested by user", task_id)
        return [row["turn_id"] for row in rows]

    def create_approval(self, task_id: str, turn_id: str, request: dict) -> dict:
        approval_id = new_id("APPROVAL-")
        with self.connect() as db:
            db.execute(
                "INSERT INTO approvals(approval_id,task_id,turn_id,request_json,status,created_at) "
                "VALUES(?,?,?,?,'pending',?)",
                (approval_id, task_id, turn_id, canonical_json(request), utc_now()),
            )
            db.execute("UPDATE agent_turns SET status='waiting_approval' WHERE turn_id=?", (turn_id,))
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        return dict(row)

    def decide_approval(self, approval_id: str, approved: bool, reason: str = "") -> dict:
        with self.connect() as db:
            row = db.execute("SELECT status FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise KeyError(approval_id)
            if row["status"] != "pending":
                raise ValueError(f"Approval already decided: {approval_id}")
            status = "approved" if approved else "denied"
            db.execute(
                "UPDATE approvals SET status=?,reason=?,decided_at=? WHERE approval_id=?",
                (status, reason, utc_now(), approval_id),
            )
        return self.get_approval(approval_id)

    def pending_approval(self, task_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE task_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def register_artifacts(self, task_id: str, turn_id: str, entries: list,
                           allowed_roots: list[Path]) -> tuple[list[dict], list[str]]:
        manifests, errors = [], []
        for entry in entries:
            if isinstance(entry, str):
                entry = {"path": entry}
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                errors.append(f"Invalid artifact descriptor: {entry!r}")
                continue
            path = Path(entry["path"]).expanduser()
            error = None
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_file() or not any(resolved.is_relative_to(root.resolve()) for root in allowed_roots):
                    raise ValueError("Artifact is not a file inside an authorized root")
                digest = hashlib.sha256()
                with resolved.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                size = resolved.stat().st_size
            except (OSError, ValueError) as exc:
                resolved, size, digest, error = path, 0, None, str(exc)
            manifest = {
                "artifact_id": new_id("ART-"), "task_id": task_id, "turn_id": turn_id,
                "path": str(resolved), "kind": str(entry.get("kind", "file")),
                "size_bytes": size, "sha256": digest.hexdigest() if digest else "",
                "valid": error is None, "validation_error": error,
            }
            manifests.append(manifest)
            if error:
                errors.append(f"{path}: {error}")
        with self.connect() as db:
            for item in manifests:
                db.execute(
                    "INSERT INTO artifacts(artifact_id,task_id,turn_id,path,kind,size_bytes,sha256,valid,"
                    "validation_error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (item["artifact_id"], task_id, turn_id, item["path"], item["kind"],
                     item["size_bytes"], item["sha256"], int(item["valid"]),
                     item["validation_error"], utc_now()),
                )
        return manifests, errors

    def artifacts(self, task_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [{**dict(row), "valid": bool(row["valid"])} for row in rows]

    def create_goal(self, title: str, steps: list[dict]) -> dict:
        if not title.strip() or not steps:
            raise ValueError("Goal title and at least one step are required")
        goal_id = new_id("GOAL-")
        known = {str(step.get("id") or f"S{index + 1}") for index, step in enumerate(steps)}
        normalized = []
        for index, step in enumerate(steps):
            step_id = str(step.get("id") or f"S{index + 1}")
            instructions = step.get("instructions")
            if not isinstance(instructions, str) or not instructions.strip():
                raise ValueError(f"Step instructions are required: {step_id}")
            depends = step.get("depends_on", [])
            expected = step.get("expected_artifacts", [])
            resources = step.get("resources", [])
            if not isinstance(depends, list) or any(item not in known for item in depends):
                raise ValueError(f"Invalid dependencies for step: {step_id}")
            if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
                raise ValueError(f"Invalid expected artifacts for step: {step_id}")
            if not isinstance(resources, list) or any(not isinstance(item, str) or not item.strip() for item in resources):
                raise ValueError(f"Invalid resources for step: {step_id}")
            normalized.append({
                "step_id": step_id, "position": index, "instructions": instructions,
                "worker_id": step.get("worker_id", "agent"), "depends_on": depends,
                "expected_artifacts": expected, "resources": resources,
            })
        resolved, remaining = set(), set(known)
        while remaining:
            progressed = {step["step_id"] for step in normalized
                          if step["step_id"] in remaining and all(dep in resolved for dep in step["depends_on"])}
            if not progressed:
                raise ValueError("Goal plan contains a dependency cycle")
            resolved.update(progressed)
            remaining.difference_update(progressed)
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO goals(goal_id,title,status,plan_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (goal_id, title, "running", canonical_json(normalized), now, now),
            )
            for step in normalized:
                db.execute(
                    "INSERT INTO plan_steps(step_id,goal_id,position,instructions,worker_id,depends_json,"
                    "expected_json,resources_json,status) VALUES(?,?,?,?,?,?,?,?,'pending')",
                    (goal_id + ":" + step["step_id"], goal_id, step["position"], step["instructions"],
                     step["worker_id"], canonical_json(step["depends_on"]),
                     canonical_json(step["expected_artifacts"]), canonical_json(step["resources"])),
                )
        return self.get_goal(goal_id)

    def get_goal(self, goal_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
            steps = db.execute("SELECT * FROM plan_steps WHERE goal_id=? ORDER BY position", (goal_id,)).fetchall()
        if row is None:
            raise KeyError(goal_id)
        result = dict(row)
        result["steps"] = [dict(step) for step in steps]
        return result

    def ready_steps(self, goal_id: str) -> list[dict]:
        goal = self.get_goal(goal_id)
        done = {step["step_id"].split(":", 1)[1] for step in goal["steps"] if step["status"] == "succeeded"}
        ready = []
        for step in goal["steps"]:
            depends = json.loads(step["depends_json"])
            if step["status"] == "pending" and all(item in done for item in depends):
                ready.append(step)
        return ready

    def bind_step_task(self, step_id: str, task_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE plan_steps SET task_id=?,status='running' WHERE step_id=?", (task_id, step_id))

    def finish_step(self, step_id: str, succeeded: bool, error: str | None = None) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT goal_id FROM plan_steps WHERE step_id=?", (step_id,)).fetchone()
            if row is None:
                raise KeyError(step_id)
            db.execute(
                "UPDATE plan_steps SET status=?,error=? WHERE step_id=?",
                ("succeeded" if succeeded else "blocked", error, step_id),
            )
            goal_id = row["goal_id"]
            statuses = [item[0] for item in db.execute(
                "SELECT status FROM plan_steps WHERE goal_id=?", (goal_id,)
            ).fetchall()]
            status = "succeeded" if all(item == "succeeded" for item in statuses) else (
                "blocked" if any(item == "blocked" for item in statuses) else "running"
            )
            db.execute("UPDATE goals SET status=?,updated_at=? WHERE goal_id=?", (status, utc_now(), goal_id))
        return self.get_goal(goal_id)

    def recover_after_restart(self) -> list[dict]:
        """Running ACP calls cannot survive their parent; retain session and require explicit continuation."""
        recovered = []
        with self.connect() as db:
            rows = db.execute(
                "SELECT turn_id,task_id FROM agent_turns WHERE status IN('running','waiting_approval')"
            ).fetchall()
            now = utc_now()
            for row in rows:
                error = "Bridge restarted during execution; original session retained, continue explicitly"
                db.execute(
                    "UPDATE agent_turns SET status='blocked',error=?,finished_at=? WHERE turn_id=?",
                    (error, now, row["turn_id"]),
                )
                db.execute("UPDATE agent_tasks SET status='blocked',updated_at=? WHERE task_id=?", (now, row["task_id"]))
                db.execute("UPDATE approvals SET status='denied',reason=?,decided_at=? WHERE turn_id=? AND status='pending'",
                           ("Bridge restarted", now, row["turn_id"]))
                recovered.append(dict(row))
            db.execute("DELETE FROM resource_locks")
        return recovered

    def record_external_run(self, run_id: str, task_id: str, turn_id: str, pid: int,
                            command: list[str], cwd: Path) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO external_runs(run_id,task_id,turn_id,pid,command_json,cwd,status,started_at) "
                "VALUES(?,?,?,?,?,?,'running',?)",
                (run_id, task_id, turn_id, pid, canonical_json(command), str(cwd), utc_now()),
            )

    def finish_external_run(self, run_id: str, exit_code: int | None,
                            status: str = "finished") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE external_runs SET status=?,exit_code=?,finished_at=? WHERE run_id=?",
                (status, exit_code, utc_now(), run_id),
            )

    def command_response(self, event_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT response_json FROM command_deliveries WHERE event_id=?", (event_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_command_response(self, event_id: str, response: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO command_deliveries(event_id,response_json,created_at) VALUES(?,?,?)",
                (event_id, canonical_json(response), utc_now()),
            )

    def queued_turns(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM agent_turns WHERE status='queued' ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def reconcile_external_runs(self) -> list[dict]:
        """Record process liveness after restart; never infer a successful exit or restart a job."""
        reconciled = []
        with self.connect() as db:
            rows = db.execute("SELECT * FROM external_runs WHERE status='running'").fetchall()
            for row in rows:
                try:
                    os.kill(row["pid"], 0)
                    status = "detached_running"
                except ProcessLookupError:
                    status = "exit_unverified"
                except PermissionError:
                    status = "detached_running"
                db.execute("UPDATE external_runs SET status=? WHERE run_id=?", (status, row["run_id"]))
                reconciled.append({**dict(row), "recovery_status": status})
        return reconciled

    def step_resources(self, step_id: str | None) -> list[str]:
        if not step_id:
            return []
        with self.connect() as db:
            row = db.execute("SELECT resources_json FROM plan_steps WHERE step_id=?", (step_id,)).fetchone()
        return json.loads(row["resources_json"]) if row else []

    def acquire_resources(self, task_id: str, turn_id: str, resources: list[str]) -> str | None:
        """Atomically acquire all locks, or return the first conflicting resource."""
        with self.connect() as db:
            for resource in sorted(resources):
                owner = db.execute("SELECT task_id FROM resource_locks WHERE resource=?", (resource,)).fetchone()
                if owner and owner["task_id"] != task_id:
                    return resource
            db.execute("DELETE FROM resource_locks WHERE task_id=?", (task_id,))
            for resource in sorted(resources):
                db.execute(
                    "INSERT INTO resource_locks(resource,task_id,turn_id,acquired_at) VALUES(?,?,?,?)",
                    (resource, task_id, turn_id, utc_now()),
                )
        return None

    def release_resources(self, task_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM resource_locks WHERE task_id=?", (task_id,))