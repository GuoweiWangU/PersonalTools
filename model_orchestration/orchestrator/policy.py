from __future__ import annotations

import json
import re
import shlex
from pathlib import Path


HIGH_RISK_PATTERNS = (
    r"\bsudo\b", r"\bsu\s+-", r"\bmkfs(?:\.|\b)", r"\bshutdown\b", r"\breboot\b",
    r"\bdd\s+.*\bof=/dev/", r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+)",
    r"\bgit\s+push\b", r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba|z|k)?sh\b",
    r"\bchmod\s+-R\s+/\b", r"\bchown\s+-R\s+/\b", r"\bsystemctl\s+(?!--user\b)",
)
SENSITIVE_PATH = re.compile(r"(?:^|/)(?:\.ssh|\.aws|\.config/gcloud|\.gnupg)(?:/|$)|/etc/(?:passwd|shadow)\b")


def command_text(raw_input: dict) -> str:
    command = raw_input.get("command", "")
    if isinstance(command, list):
        return shlex.join(str(item) for item in command)
    return str(command)


def normalize_command(raw_input: dict) -> list[str]:
    command = raw_input.get("command", "")
    if isinstance(command, list) and all(isinstance(item, str) for item in command):
        return command
    if isinstance(command, str) and command.strip():
        return ["/bin/sh", "-lc", command]
    raise ValueError("Terminal command must be a string or an argument list")


class ExecutionPolicy:
    """Policy checks for ACP requests. This is not an operating-system sandbox."""

    def __init__(self, allowed_roots: list[Path], ssh_hosts: set[str] | None = None):
        if not allowed_roots:
            raise ValueError("At least one authorized root is required")
        self.allowed_roots = [root.expanduser().resolve() for root in allowed_roots]
        self.ssh_hosts = {host.strip() for host in (ssh_hosts or set()) if host.strip()}

    def check_path(self, value: str, write: bool = False) -> Path:
        path = Path(value).expanduser().resolve(strict=False)
        if not any(path == root or path.is_relative_to(root) for root in self.allowed_roots):
            raise PermissionError(f"Path is outside authorized roots: {path}")
        if write and SENSITIVE_PATH.search(str(path)):
            raise PermissionError(f"Refusing to modify sensitive path: {path}")
        return path

    def check_cwd(self, value: str | None) -> Path:
        return self.check_path(value) if value else self.allowed_roots[0]

    def permission_decision(self, tool_call: dict) -> tuple[str, str]:
        """Return allow, deny, or approve plus a machine-readable reason."""
        kind = str(tool_call.get("kind") or "other")
        raw = tool_call.get("rawInput") or {}
        title = str(tool_call.get("title") or "ACP tool call")
        if kind in {"read", "search", "think"}:
            return "allow", "Read/search operation"
        if kind in {"edit", "write", "move"}:
            paths = [item.get("path") for item in tool_call.get("locations", []) if item.get("path")]
            for key in ("path", "file_path", "filePath", "source", "destination"):
                if isinstance(raw.get(key), str):
                    paths.append(raw[key])
            try:
                for path in paths:
                    self.check_path(path, write=True)
            except PermissionError as exc:
                return "deny", str(exc)
            return ("allow", "File change inside authorized roots") if paths else ("approve", title)
        if kind == "delete":
            return "approve", title
        if kind == "execute":
            return self.command_decision(raw, title)
        return "deny", f"Unsupported tool kind for automatic execution: {kind}"

    def command_decision(self, raw: dict, title: str = "Command") -> tuple[str, str]:
        text = command_text(raw)
        if not text.strip():
            return "deny", "Empty command"
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in HIGH_RISK_PATTERNS):
            return "approve", title
        try:
            self.check_cwd(raw.get("cwd"))
            self._check_shell_paths(text)
            self._check_ssh(text)
        except (PermissionError, ValueError) as exc:
            return "deny", str(exc)
        return "allow", "Command passes command, path and SSH policy"

    def _check_shell_paths(self, text: str) -> None:
        for match in re.finditer(r"(?:^|[;|&]\s*|\s)(?:cd|pushd)\s+([^\s;|&]+)", text):
            value = match.group(1).strip("'\"")
            path = Path(value).expanduser()
            if not path.is_absolute():
                continue
            self.check_path(str(path), write=True)
        for match in re.finditer(r"(?:^|\s)(/[^\s;'\"|&<>]+)", text):
            value = match.group(1)
            if value.startswith(("/bin/", "/usr/bin/", "/usr/local/bin/", "/opt/homebrew/bin/")):
                continue
            if SENSITIVE_PATH.search(value):
                raise PermissionError(f"Refusing sensitive path in command: {value}")
        if re.search(r"(?:^|[\s/'\"])\.\.(?:/|\b)", text):
            raise PermissionError("Parent-directory traversal in commands requires approval")

    def _check_ssh(self, text: str) -> None:
        try:
            tokens = shlex.split(text)
        except ValueError as exc:
            raise ValueError(f"Cannot parse shell command: {exc}") from exc
        for index, token in enumerate(tokens):
            if Path(token).name != "ssh":
                continue
            host = None
            skip = False
            for candidate in tokens[index + 1:]:
                if skip:
                    skip = False
                    continue
                if candidate in {"-p", "-l", "-i", "-o", "-F", "-b", "-c", "-D", "-E", "-e", "-I", "-J", "-L", "-m", "-O", "-Q", "-R", "-S", "-W", "-w"}:
                    skip = True
                    continue
                if candidate.startswith("-"):
                    continue
                host = candidate.rsplit("@", 1)[-1]
                break
            if not host or host not in self.ssh_hosts:
                raise PermissionError(f"SSH host is not allowlisted: {host or 'missing'}")

    def audit_record(self, request: dict) -> dict:
        tool = request.get("toolCall", {})
        return {
            "tool_call_id": tool.get("toolCallId"), "kind": tool.get("kind"),
            "title": tool.get("title"), "raw_input": tool.get("rawInput"),
            "locations": tool.get("locations"), "options": request.get("options", []),
        }

    @staticmethod
    def reason_text(decision: tuple[str, str]) -> str:
        return json.dumps({"decision": decision[0], "reason": decision[1]}, ensure_ascii=False)
