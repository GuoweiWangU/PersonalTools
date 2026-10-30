from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .core import ALLOWED_TASK_TYPES, Orchestrator
from .workers import MockWorker, OpenAICompatibleWorker


def main() -> None:
    parser = argparse.ArgumentParser(description="Local model collaboration controller")
    parser.add_argument("--db", type=Path, default=Path("model_orchestration/runtime/tasks.db"))
    parser.add_argument("--artifacts", type=Path, default=Path("model_orchestration/runtime/artifacts"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--type", choices=sorted(ALLOWED_TASK_TYPES), required=True)
    create.add_argument("--role", required=True)
    create.add_argument("--input-ref", required=True)
    create.add_argument("--instructions", required=True)
    create.add_argument("--worker", default="mock")
    create.add_argument("--idempotency-key")

    run = subparsers.add_parser("run")
    run.add_argument("task_id")
    run.add_argument("--worker", default="mock")
    run.add_argument("--base-url", default=os.environ.get("WORKER_BASE_URL"))
    run.add_argument("--model", default=os.environ.get("WORKER_MODEL"))
    run.add_argument("--api-key-env", default="WORKER_API_KEY")
    run.add_argument("--timeout", type=int, default=600)

    subparsers.add_parser("list")
    show = subparsers.add_parser("show")
    show.add_argument("task_id")
    args = parser.parse_args()
    orchestrator = Orchestrator(args.db, args.artifacts)

    if args.command == "create":
        result = orchestrator.create_task(
            args.type, args.role, args.input_ref, args.instructions, args.worker, args.idempotency_key
        )
    elif args.command == "run":
        task = orchestrator.get_task(args.task_id)
        if args.worker == "mock":
            worker = MockWorker()
        elif args.worker == "openai":
            if not args.base_url or not args.model:
                raise ValueError("--base-url and --model are required for openai worker")
            if task["assigned_worker"] not in {"openai", args.model}:
                raise ValueError("Task assigned_worker does not match requested model worker")
            worker = OpenAICompatibleWorker(args.base_url, args.model, args.api_key_env, args.timeout)
        else:
            raise ValueError("Worker must be mock or openai")
        result = orchestrator.run_task(args.task_id, worker)
    elif args.command == "list":
        result = orchestrator.list_tasks()
    else:
        result = orchestrator.get_task(args.task_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
