from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .agent_store import ControlStore


class ApprovalBroker:
    def __init__(self, store: ControlStore, timeout: int = 3600):
        self.store = store
        self.timeout = timeout
        self.condition = threading.Condition()

    def request(self, task_id: str, turn_id: str, request: dict) -> tuple[dict, Callable[[], bool]]:
        approval = self.store.create_approval(task_id, turn_id, request)

        def wait() -> bool:
            deadline = threading.TIMEOUT_MAX if self.timeout <= 0 else self.timeout
            with self.condition:
                decided = self.condition.wait_for(
                    lambda: self.store.get_approval(approval["approval_id"])["status"] != "pending",
                    timeout=deadline,
                )
            if not decided and self.store.get_approval(approval["approval_id"])["status"] == "pending":
                self.decide(approval["approval_id"], False, "Approval timed out")
            return self.store.get_approval(approval["approval_id"])["status"] == "approved"
        return approval, wait

    def decide(self, approval_id: str, approved: bool, reason: str = "") -> dict:
        approval = self.store.decide_approval(approval_id, approved, reason)
        with self.condition:
            self.condition.notify_all()
        return approval


class AgentRuntime:
    """Asynchronous executor with durable task state and plan progression."""

    def __init__(self, store: ControlStore, agent, roots: list[Path], ssh_hosts: set[str],
                 notifier: Callable[[str, dict], None] | None = None, max_workers: int = 2,
                 approval_timeout: int = 3600):
        self.store = store
        self.agent = agent
        self.roots = [root.resolve() for root in roots]
        self.ssh_hosts = ssh_hosts
        self.notifier = notifier or (lambda event, data: None)
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent-turn")
        self.approvals = ApprovalBroker(store, approval_timeout)
        self.running_clients: dict[str, object] = {}
        self.client_lock = threading.Lock()
        self.recovered = store.recover_after_restart()
        self.external_runs = store.reconcile_external_runs()
        for queued in store.queued_turns():
            self.submit(queued["turn_id"])

    def create_task(self, instructions: str, cwd: Path | None = None, worker_id: str = "agent",
                    goal_id: str | None = None, step_id: str | None = None,
                    reply_to_message_id: str | None = None) -> tuple[dict, dict]:
        cwd = (cwd or self.roots[0]).resolve()
        if not any(cwd == root or cwd.is_relative_to(root) for root in self.roots):
            raise PermissionError(f"Task cwd is outside authorized roots: {cwd}")
        task, turn = self.store.create_task(
            instructions, worker_id, cwd, goal_id, step_id, reply_to_message_id,
        )
        self.submit(turn["turn_id"])
        return task, turn

    def continue_task(self, task_id: str, instructions: str,
                      reply_to_message_id: str | None = None) -> dict:
        turn = self.store.enqueue_turn(task_id, instructions, reply_to_message_id)
        self.submit(turn["turn_id"])
        return turn

    def submit(self, turn_id: str) -> None:
        self.pool.submit(self._execute, turn_id)

    def _execute(self, turn_id: str) -> None:
        task = turn = None
        try:
            task, turn = self.store.claim_turn(turn_id)
        except ValueError:
            return
        try:
            resources = self.store.step_resources(task.get("step_id"))
            waiting_for = None
            while resources:
                conflict = self.store.acquire_resources(task["task_id"], turn_id, resources)
                if conflict is None:
                    break
                if conflict != waiting_for:
                    self.store.event("resource_wait", f"Waiting for resource: {conflict}",
                                     task["task_id"], turn_id)
                    waiting_for = conflict
                if self.store.get_task(task["task_id"])["status"] == "cancelled":
                    raise RuntimeError("Task cancellation requested")
                time.sleep(0.2)
            if waiting_for is not None:
                self.store.event("resource_acquired", "Required resources acquired", task["task_id"], turn_id)

            def approval_handler(request: dict) -> bool:
                approval, wait = self.approvals.request(task["task_id"], turn_id, request)
                self.notifier("approval", {"approval": approval, "task": task, "turn": turn})
                approved = wait()
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE agent_turns SET status='running',heartbeat_at=CURRENT_TIMESTAMP WHERE turn_id=? "
                        "AND status='waiting_approval'", (turn_id,),
                    )
                return approved

            def log_handler(message: str) -> None:
                self.store.event("worker_log", message, task["task_id"], turn_id)
                self.store.heartbeat(turn_id)

            result = self.agent.run_turn(
                task, turn, self.roots, self.ssh_hosts, approval_handler, log_handler,
            )
            stop_reason = result.pop("_stop_reason", None)
            manifests, artifact_errors = self.store.register_artifacts(
                task["task_id"], turn_id, result["artifacts"], self.roots,
            )
            result["artifacts"] = manifests
            if artifact_errors:
                result["blocking_issues"] = [*result["blocking_issues"], *artifact_errors]
                if result["status"] == "succeeded":
                    result["status"] = "blocked"
                    result["summary"] += " Artifact verification failed."
            if task.get("step_id") and result["status"] == "succeeded":
                expected_error = self._verify_expected(task["step_id"], manifests)
                if expected_error:
                    result["status"] = "blocked"
                    result["blocking_issues"].append(expected_error)
            error = "; ".join(result["blocking_issues"]) if result["status"] == "blocked" else None
            finished = self.store.complete_turn(turn_id, result["status"], result, error, stop_reason)
            if task.get("step_id"):
                goal = self.store.finish_step(task["step_id"], result["status"] == "succeeded", error)
                if result["status"] == "succeeded":
                    self._schedule_ready(goal["goal_id"])
            self.notifier("turn_finished", {"task": self.store.get_task(task["task_id"]),
                                            "turn": finished, "result": result})
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            try:
                cancelled = task and self.store.get_task(task["task_id"])["status"] == "cancelled"
                outcome = "cancelled" if cancelled else "failed"
                finished = self.store.complete_turn(turn_id, outcome, error=detail)
                if task and task.get("step_id") and not cancelled:
                    self.store.finish_step(task["step_id"], False, detail)
                self.notifier("turn_finished", {
                    "task": self.store.get_task(task["task_id"]), "turn": finished,
                    "result": {"status": outcome, "summary": "Task cancelled" if cancelled else "Agent execution failed",
                               "artifacts": [], "unknowns": [], "blocking_issues": [] if cancelled else [detail],
                               "error": detail},
                })
            except Exception as nested:
                self.store.event("internal_error", f"{type(nested).__name__}: {nested}", turn_id=turn_id)
        finally:
            if task:
                self.store.release_resources(task["task_id"])

    def _verify_expected(self, step_id: str, manifests: list[dict]) -> str | None:
        with self.store.connect() as db:
            row = db.execute("SELECT expected_json FROM plan_steps WHERE step_id=?", (step_id,)).fetchone()
        expected = json.loads(row["expected_json"]) if row else []
        actual = {item["path"] for item in manifests if item["valid"]}
        missing = [path for path in expected if str(Path(path).expanduser().resolve()) not in actual]
        return "Expected artifacts missing or unverified: " + ", ".join(missing) if missing else None

    def create_goal(self, title: str, steps: list[dict]) -> dict:
        goal = self.store.create_goal(title, steps)
        self._schedule_ready(goal["goal_id"])
        return goal

    def _schedule_ready(self, goal_id: str) -> None:
        for step in self.store.ready_steps(goal_id):
            short_id = step["step_id"].split(":", 1)[1]
            task, _ = self.create_task(
                step["instructions"], worker_id=step["worker_id"], goal_id=goal_id,
                step_id=step["step_id"],
            )
            self.store.bind_step_task(step["step_id"], task["task_id"])
            self.store.event("step_scheduled", f"Scheduled {short_id} as {task['task_id']}", task["task_id"])

    def decide_approval(self, task_id: str, approved: bool, reason: str = "") -> dict:
        pending = self.store.pending_approval(task_id)
        if pending is None:
            raise ValueError(f"No pending approval for task: {task_id}")
        return self.approvals.decide(pending["approval_id"], approved, reason)

    def cancel(self, task_id: str) -> list[str]:
        turn_ids = self.store.cancel_task(task_id)
        for turn_id in turn_ids:
            cancel = getattr(self.agent, "cancel_turn", None)
            if cancel:
                cancel(turn_id)
        return turn_ids

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
