from __future__ import annotations

import json
import os
import urllib.request
from typing import Protocol


class Worker(Protocol):
    def run(self, payload: dict) -> dict: ...


def validate_result(result: dict) -> dict:
    if not isinstance(result, dict):
        raise ValueError("Worker result must be a JSON object")
    required = ("status", "summary", "artifacts", "unknowns", "blocking_issues")
    missing = [field for field in required if field not in result]
    if missing:
        raise ValueError(f"Worker result missing fields: {missing}")
    if result["status"] not in {"succeeded", "blocked", "failed"}:
        raise ValueError("Worker result status must be succeeded, blocked, or failed")
    for field in ("artifacts", "unknowns", "blocking_issues"):
        if not isinstance(result[field], list):
            raise ValueError(f"Worker result {field} must be a list")
    if not isinstance(result["summary"], str) or not result["summary"].strip():
        raise ValueError("Worker result summary must be a non-empty string")
    return result


def parse_worker_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if lines[0].strip().lower() in {"```", "```json"}:
            text = "\n".join(lines[1:-1]).strip()
    return json.loads(text)


class MockWorker:
    """Deterministic local worker for framework tests; does not call a model."""

    def run(self, payload: dict) -> dict:
        return validate_result({
            "status": "succeeded",
            "summary": f"Mock worker accepted {payload['task_type']} task {payload['task_id']}",
            "artifacts": [],
            "unknowns": [],
            "blocking_issues": [],
            "worker_observation": {
                "role": payload["role"],
                "input_ref": payload["input_ref"],
                "instructions": payload["instructions"],
            },
        })


class OpenAICompatibleWorker:
    """Call an OpenAI-compatible chat endpoint without storing credentials in task records."""

    def __init__(self, base_url: str, model: str, api_key_env: str, timeout: int = 600):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout = timeout

    def run(self, payload: dict) -> dict:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"Missing API key environment variable: {self.api_key_env}")
        system = (
            "You are an isolated task worker. Return only one JSON object with fields: "
            "status (succeeded|blocked|failed), summary (string), artifacts (array), "
            "unknowns (array), blocking_issues (array). Do not execute commands or invent file writes."
        )
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0.6,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            envelope = json.load(response)
        content = envelope["choices"][0]["message"]["content"]
        return validate_result(parse_worker_json(content))
