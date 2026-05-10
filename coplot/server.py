from __future__ import annotations

import contextlib
import argparse
import binascii
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import base64
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULTS_FILE = Path(os.environ.get("COPLOT_DEFAULTS_FILE", Path.home() / ".config" / "coplot" / "defaults.json"))
EDIT_BLOCK_RE = re.compile(r"```coplot-edit[ \t]*\n(?P<json>.*?)```", re.DOTALL | re.IGNORECASE)
RUN_BLOCK_RE = re.compile(r"```coplot-run[ \t]*\n(?P<code>.*?)```", re.DOTALL | re.IGNORECASE)
SHELL_BLOCK_RE = re.compile(r"```coplot-shell[ \t]*\n(?P<command>.*?)```", re.DOTALL | re.IGNORECASE)
TRANSCRIPT_OUTPUT_LIMIT_BYTES = 8 * 1024
TRANSCRIPT_OUTPUT_EDGE_BYTES = 4 * 1024
MAX_CHAT_IMAGE_BYTES = 20 * 1024 * 1024
TRANSCRIPT_TRUNCATION_MARKER = (
    "\n\n[Output truncated: showing first 4 KiB and last 4 KiB. "
    "Run a more specific command to inspect more.]\n\n"
)
COMPACTION_PROMPT = """Compact the current coplot session for future model turns.

The current durable code file is will be visible to the future model and is the source of truth.

Write a concise working-memory summary containing only information needed to continue the analysis that is not already obvious from the durable code.

Include:
- the user's current goal and preferences
- domain facts discovered in chat
- data facts discovered in terminal output but not encoded in code
- decisions made and why
- unresolved issues, next steps, or user-requested changes
- important feedback from inspection of plots
- environment/package/path quirks

Omit:
- raw terminal logs
- verbose chat history
- code already present in the durable file
- generic descriptions of coplot
- outdated or superseded attempts

Keep it brief, factual, and useful for the next assistant turn."""
project: "ProjectState"
chat_store: "ChatStore"
transcript_store: "TranscriptStore"
artifact_store: "ArtifactStore"
model_settings_store: "ModelSettingsStore"
session: "ExecutionSession"
shell_session: "ShellSession"
context_builder: "ContextBuilder"
agent_service: "AgentService"
active_job_store: "ActiveJobStore"
workspace_first_run = False
stop_requested = False

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit is not None:
        lines = lines[-limit:]
    return [json.loads(line) for line in lines]


def append_jsonl(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def file_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return 0


def assert_source_not_stale(expected_mtime_ns: Any) -> None:
    if expected_mtime_ns is None:
        return
    expected = int(expected_mtime_ns)
    current = file_mtime_ns(project.source_file)
    if expected > 0 and current > 0 and current != expected:
        raise SourceConflictError(
            "Source changed on disk after this editor snapshot was loaded. "
            "Refresh before saving to avoid overwriting newer edits."
        )


class SourceConflictError(RuntimeError):
    pass


def apply_line_edits(source: str, edits: list[dict[str, Any]]) -> str:
    if not edits:
        return source

    lines = source.splitlines(keepends=True)
    for edit in sorted(edits, key=lambda item: int(item["start_line"]), reverse=True):
        start_line = int(edit["start_line"])
        end_line = int(edit["end_line"])
        replacement = str(edit.get("replacement", ""))
        if start_line < 0:
            raise ValueError("start_line must be 0 or greater")
        if start_line == 0 and end_line != 0:
            raise ValueError("start_line 0 is only valid with end_line 0")
        if end_line != 0 and end_line < start_line:
            raise ValueError("end_line must be 0 or greater than or equal to start_line")

        replacement_lines = replacement.splitlines(keepends=True)
        if replacement and not replacement.endswith(("\n", "\r")):
            replacement_lines[-1] = f"{replacement_lines[-1]}\n"

        if start_line == 0 and end_line == 0:
            start_index = 0
            end_index = 0
        elif end_line == 0:
            start_index = max(0, start_line - 1)
            end_index = start_index
        else:
            start_index = start_line - 1
            end_index = end_line

        if not lines and start_line == 1 and end_line in {0, 1}:
            start_index = 0
            end_index = 0
        if start_index > len(lines):
            raise ValueError("start_line is past the end of the editor")
        if end_index > len(lines):
            raise ValueError("end_line is past the end of the editor")
        lines[start_index:end_index] = replacement_lines

    return "".join(lines)


def offset_for_line(lines: list[str], line_number: int) -> int:
    if line_number <= 0:
        return 0
    return sum(len(line) for line in lines[: max(0, line_number - 1)])


def selection_for_line_edits(source: str, edits: list[dict[str, Any]]) -> dict[str, int] | None:
    if not edits:
        return None

    before_lines = source.splitlines(keepends=True)
    start_offsets: list[int] = []
    replacement_lengths: list[int] = []
    for edit in edits:
        start_line = int(edit["start_line"])
        replacement = str(edit.get("replacement", ""))
        start_offsets.append(offset_for_line(before_lines, start_line))
        replacement_lengths.append(len(replacement))

    if not start_offsets:
        return None
    start = min(start_offsets)
    end = max(offset + length for offset, length in zip(start_offsets, replacement_lengths))
    if end <= start:
        return None
    return {"start": start, "end": end}


@dataclass(frozen=True)
class ProjectState:
    root: Path
    agent_data_dir: Path
    summary_file: Path
    chat_file: Path
    transcript_file: Path
    artifacts_file: Path
    model_settings_file: Path
    plots_dir: Path
    chat_images_dir: Path
    language: str = "python"

    @classmethod
    def discover(cls, root: Path) -> "ProjectState":
        root = root.expanduser().resolve()
        agent_data_dir = root / "coplot"
        return cls(
            root=root,
            agent_data_dir=agent_data_dir,
            summary_file=agent_data_dir / "summary.md",
            chat_file=agent_data_dir / "chat.jsonl",
            transcript_file=agent_data_dir / "transcript.jsonl",
            artifacts_file=agent_data_dir / "artifacts.jsonl",
            model_settings_file=agent_data_dir / "config.json",
            plots_dir=agent_data_dir / "plots",
            chat_images_dir=agent_data_dir / "chat_images",
        )

    @property
    def source_file(self) -> Path:
        return self.root / ("coplot.R" if self.language == "r" else "coplot.py")

    @property
    def venv_dir(self) -> Path:
        return self.agent_data_dir / "venv"

    @property
    def venv_python(self) -> Path:
        return self.venv_dir / "bin" / "python"

    @property
    def renv_dir(self) -> Path:
        return self.agent_data_dir / "renv"

    @property
    def renv_lock_file(self) -> Path:
        return self.agent_data_dir / "renv.lock"

    @property
    def r_executable(self) -> str:
        return shutil.which("Rscript") or "Rscript"

    def with_language(self, language: str) -> "ProjectState":
        return ProjectState(
            root=self.root,
            agent_data_dir=self.agent_data_dir,
            summary_file=self.summary_file,
            chat_file=self.chat_file,
            transcript_file=self.transcript_file,
            artifacts_file=self.artifacts_file,
            model_settings_file=self.model_settings_file,
            plots_dir=self.plots_dir,
            chat_images_dir=self.chat_images_dir,
            language=normalize_language(language),
        )

    def ensure_base(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.agent_data_dir.mkdir(parents=True, exist_ok=True)
        self.plots_dir.mkdir(parents=True, exist_ok=True)
        self.chat_images_dir.mkdir(parents=True, exist_ok=True)
        if not self.summary_file.exists():
            self.summary_file.write_text("# coplot Session Summary\n\n", encoding="utf-8")
        self.chat_file.touch(exist_ok=True)
        self.transcript_file.touch(exist_ok=True)
        self.artifacts_file.touch(exist_ok=True)

    def ensure_runtime(self) -> None:
        self.ensure_base()
        self.ensure_source_file()
        if self.language == "r":
            self.ensure_renv()
            return
        self.ensure_venv()

    def ensure_source_file(self) -> None:
        if self.source_file.exists():
            return
        if self.language == "r":
            self.source_file.write_text(
                "#!/usr/bin/env Rscript\n"
                "\n"
                "source(\"coplot/renv/activate.R\")\n",
                encoding="utf-8",
            )
            return
        self.source_file.touch()

    def ensure_venv(self) -> None:
        if self.venv_python.exists():
            return
        subprocess.run([sys.executable, "-m", "venv", str(self.venv_dir)], check=True)

    def ensure_renv(self) -> None:
        executable = shutil.which("Rscript")
        if executable is None:
            raise RuntimeError("R mode requires Rscript on PATH.")
        expression = (
            "if (!requireNamespace('jsonlite', quietly = TRUE)) "
            "stop('R mode requires the jsonlite package. Install it with install.packages(\"jsonlite\").'); "
            "if (!requireNamespace('renv', quietly = TRUE)) "
            "stop('R mode requires the renv package. Install it with install.packages(\"renv\").'); "
            "lockfile <- 'coplot/renv.lock'; "
            "if (!file.exists('coplot/renv/activate.R')) "
            "renv::init(project = getwd(), bare = TRUE, restart = FALSE, settings = list(external.libraries = character())); "
            "source('coplot/renv/activate.R'); "
            "renv::install('jsonlite'); "
            "renv::snapshot(project = getwd(), lockfile = lockfile, prompt = FALSE); "
            "renv::record(list(jsonlite = list(Package = 'jsonlite', Version = as.character(utils::packageVersion('jsonlite')), Source = 'Repository', Repository = 'CRAN')), lockfile = lockfile, project = getwd())"
        )
        env = self._renv_env()
        subprocess.run([executable, "--vanilla", "-e", expression], cwd=self.root, env=env, check=True)

    def _renv_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["RENV_PATHS_RENV"] = "coplot/renv"
        env["RENV_PATHS_LIBRARY"] = str(self.renv_dir / "library")
        env["RENV_PATHS_LOCKFILE"] = "coplot/renv.lock"
        return env

    def recreate_runtime(self) -> None:
        if self.language == "r":
            self.ensure_renv()
            return
        if self.venv_dir.exists():
            shutil.rmtree(self.venv_dir)
        self.ensure_venv()


def normalize_language(value: Any) -> str:
    language = str(value or "python").strip().lower()
    if language in {"py", "python"}:
        return "python"
    if language in {"r", "R"}:
        return "r"
    raise ValueError("language must be either 'python' or 'r'")


def default_model_settings() -> dict[str, Any]:
    return {
        "endpoint_url": "http://localhost:11434",
        "model": "qwen3.6",
        "language": "r",
        "workspace_setup_complete": False,
        "max_tokens": 16384,
        "temperature": 0.2,
        "reasoning_enabled": False,
        "reasoning_control": "auto",
        "context_window_tokens": 32768,
        "timeout_seconds": 600,
    }


class ModelSettingsStore:
    def __init__(self, path: Path, defaults_path: Path) -> None:
        self.path = path
        self.defaults_path = defaults_path

    def read(self) -> dict[str, Any]:
        settings = default_model_settings()
        if self.defaults_path.exists():
            stored_defaults = json.loads(self.defaults_path.read_text(encoding="utf-8") or "{}")
            stored_defaults.pop("language", None)
            stored_defaults.pop("workspace_setup_complete", None)
            settings.update({key: value for key, value in stored_defaults.items() if value is not None})
        if self.path.exists():
            stored = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            settings.update({key: value for key, value in stored.items() if value is not None})
        settings.pop("reasoning_effort", None)
        return settings

    def write(self, values: dict[str, Any]) -> dict[str, Any]:
        current = default_model_settings()
        if self.path.exists():
            stored = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            current.update({key: value for key, value in stored.items() if value is not None})
        current = self._clean({**current, **values})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return self.read()

    def write_defaults(self, values: dict[str, Any]) -> dict[str, Any]:
        values = dict(values)
        values.pop("language", None)
        values.pop("workspace_setup_complete", None)
        current = default_model_settings()
        if self.defaults_path.exists():
            stored = json.loads(self.defaults_path.read_text(encoding="utf-8") or "{}")
            stored.pop("language", None)
            stored.pop("workspace_setup_complete", None)
            current.update({key: value for key, value in stored.items() if value is not None})
        current = self._clean({**current, **values})
        current["workspace_setup_complete"] = False
        self.defaults_path.parent.mkdir(parents=True, exist_ok=True)
        self.defaults_path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return current

    def ensure_workspace_config(self) -> None:
        if self.path.exists():
            return
        self.write(self.read())

    def _clean(self, values: dict[str, Any]) -> dict[str, Any]:
        current = default_model_settings()
        allowed = set(current)
        for key, value in values.items():
            if key in allowed:
                current[key] = value
        current["max_tokens"] = max(1, int(current["max_tokens"]))
        current["language"] = normalize_language(current.get("language", "python"))
        current["workspace_setup_complete"] = bool(current.get("workspace_setup_complete"))
        current["temperature"] = float(current["temperature"])
        current["reasoning_enabled"] = bool(current["reasoning_enabled"])
        current["reasoning_control"] = str(current.get("reasoning_control") or "auto")
        current["context_window_tokens"] = max(1, int(current["context_window_tokens"]))
        current["timeout_seconds"] = max(1, int(current["timeout_seconds"]))
        return current

    def request_url(self) -> str:
        endpoint = str(self.read()["endpoint_url"]).rstrip("/")
        if "://" not in endpoint:
            endpoint = f"http://{endpoint}"
        if endpoint.endswith("/chat/completions"):
            return endpoint
        if endpoint.endswith("/v1"):
            return f"{endpoint}/chat/completions"
        return f"{endpoint}/v1/chat/completions"

    def models_url(self, endpoint_url: str | None = None) -> str:
        endpoint = str(endpoint_url or self.read()["endpoint_url"]).rstrip("/")
        if "://" not in endpoint:
            endpoint = f"http://{endpoint}"
        if endpoint.endswith("/chat/completions"):
            endpoint = endpoint[: -len("/chat/completions")]
        if endpoint.endswith("/v1"):
            return f"{endpoint}/models"
        return f"{endpoint}/v1/models"


def normalize_endpoint_url(endpoint_url: str) -> str:
    endpoint = endpoint_url.strip().rstrip("/")
    if "://" not in endpoint:
        endpoint = f"http://{endpoint}"
    return endpoint


def detect_reasoning_control_from_endpoint(endpoint_url: str) -> str:
    parsed = urllib.parse.urlparse(normalize_endpoint_url(endpoint_url))
    host = parsed.hostname or ""
    if parsed.port == 11434 or host.endswith(".ollama"):
        return "ollama"
    return "chat_template_kwargs"


def detect_reasoning_control(endpoint_url: str, models: list[dict[str, Any]]) -> str:
    endpoint_guess = detect_reasoning_control_from_endpoint(endpoint_url)
    owners = {str(model.get("owned_by", "")).lower() for model in models}
    if "vllm" in owners:
        return "chat_template_kwargs"
    if "library" in owners:
        return "ollama"
    return endpoint_guess


def resolve_reasoning_control(settings: dict[str, Any]) -> str:
    control = str(settings.get("reasoning_control") or "auto")
    if control == "auto":
        return detect_reasoning_control_from_endpoint(str(settings.get("endpoint_url", "")))
    if control in {"ollama", "chat_template_kwargs", "none"}:
        return control
    return "chat_template_kwargs"


def fetch_models(endpoint_url: str, timeout: int = 10) -> dict[str, Any]:
    url = model_settings_store.models_url(endpoint_url)
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    models = data.get("data", [])
    if not isinstance(models, list):
        models = []
    normalized = [
        {
            "id": str(model.get("id") or model.get("name") or model.get("model") or ""),
            "owned_by": str(model.get("owned_by") or ""),
            "context_window_tokens": model.get("max_model_len") or model.get("context_length"),
        }
        for model in models
        if model.get("id") or model.get("name") or model.get("model")
    ]
    reasoning_control = detect_reasoning_control(endpoint_url, normalized)
    if reasoning_control == "ollama":
        merge_ollama_loaded_context(endpoint_url, normalized, timeout=timeout)
    return {
        "models": normalized,
        "reasoning_control": reasoning_control,
    }


def merge_ollama_loaded_context(endpoint_url: str, models: list[dict[str, Any]], timeout: int = 10) -> None:
    endpoint = normalize_endpoint_url(endpoint_url)
    parsed = urllib.parse.urlparse(endpoint)
    base = f"{parsed.scheme}://{parsed.netloc}"
    url = f"{base}/api/ps"
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return
    loaded = data.get("models", [])
    if not isinstance(loaded, list):
        return
    context_by_name = {
        str(model.get("model") or model.get("name") or ""): model.get("context_length")
        for model in loaded
        if model.get("context_length")
    }
    for model in models:
        context_length = context_by_name.get(str(model.get("id", "")))
        if context_length:
            model["context_window_tokens"] = context_length


def estimate_tokens(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return max(1, int(len(text) / 4))


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def truncate_middle_utf8(text: str, head_bytes: int, tail_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= head_bytes + tail_bytes:
        return text
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore")
    return f"{head}{TRANSCRIPT_TRUNCATION_MARKER}{tail}"


def truncate_transcript_output(stdout: str, stderr: str) -> tuple[str, str]:
    stdout = str(stdout)
    stderr = str(stderr)
    if utf8_len(stdout) + utf8_len(stderr) <= TRANSCRIPT_OUTPUT_LIMIT_BYTES:
        return stdout, stderr

    if stdout and stderr:
        combined = f"{stdout}\n{stderr}"
        return truncate_middle_utf8(
            combined,
            TRANSCRIPT_OUTPUT_EDGE_BYTES,
            TRANSCRIPT_OUTPUT_EDGE_BYTES,
        ), ""
    if stderr:
        return "", truncate_middle_utf8(
            stderr,
            TRANSCRIPT_OUTPUT_EDGE_BYTES,
            TRANSCRIPT_OUTPUT_EDGE_BYTES,
        )
    return truncate_middle_utf8(
        stdout,
        TRANSCRIPT_OUTPUT_EDGE_BYTES,
        TRANSCRIPT_OUTPUT_EDGE_BYTES,
    ), ""


def truncate_transcript_entry(entry: dict[str, Any]) -> dict[str, Any]:
    copied = dict(entry)
    copied["stdout"], copied["stderr"] = truncate_transcript_output(
        str(copied.get("stdout", "")),
        str(copied.get("stderr", "")),
    )
    return copied


def context_token_breakdown(payload: dict[str, Any]) -> dict[str, int]:
    return {
        "durable_code": estimate_tokens(payload.get("durable_code", "")),
        "session_summary": estimate_tokens(payload.get("session_summary", "")),
        "recent_chat": estimate_tokens(payload.get("recent_chat", [])),
        "recent_transcript": estimate_tokens(payload.get("recent_transcript", [])),
        "artifacts": estimate_tokens(payload.get("artifacts", {})),
        "environment": estimate_tokens(payload.get("environment", {})),
    }


class ChatStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def replace(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )

    def append(self, role: str, content: str, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return append_jsonl(
            self.path,
            {
                "id": str(uuid4()),
                "created_at": utc_now_iso(),
                "role": role,
                "content": str(content),
                "attachments": attachments or [],
            },
        )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return read_jsonl(self.path, limit)


class TranscriptStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append_session(
        self,
        *,
        language: str,
        source: str,
        code: str,
        stdout: str,
        stderr: str,
        ok: bool,
        duration_ms: int,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        stdout, stderr = truncate_transcript_output(stdout, stderr)
        return append_jsonl(
            self.path,
            {
                "id": str(uuid4()),
                "created_at": utc_now_iso(),
                "kind": "session",
                "language": language,
                "source": source,
                "input": code,
                "code": code,
                "stdout": stdout,
                "stderr": stderr,
                "ok": ok,
                "duration_ms": duration_ms,
                "artifacts": [artifact["id"] for artifact in artifacts],
            },
        )

    def append_shell(
        self,
        *,
        source: str,
        command: str,
        stdout: str,
        stderr: str,
        ok: bool,
        exit_code: int,
        duration_ms: int,
        cwd: Path,
    ) -> dict[str, Any]:
        stdout, stderr = truncate_transcript_output(stdout, stderr)
        return append_jsonl(
            self.path,
            {
                "id": str(uuid4()),
                "created_at": utc_now_iso(),
                "kind": "shell",
                "source": source,
                "input": command,
                "stdout": stdout,
                "stderr": stderr,
                "ok": ok,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "cwd": str(cwd),
            },
        )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return [truncate_transcript_entry(entry) for entry in read_jsonl(self.path, limit)]


class ArtifactStore:
    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.root = root

    def list(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    def next_id(self) -> int:
        entries = self.list()
        return max([int(entry["id"]) for entry in entries], default=0) + 1

    def display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return str(path)

    def upsert_path(
        self,
        *,
        artifact_type: str,
        path: Path,
        source: str,
        caption: str,
    ) -> dict[str, Any]:
        display_path = self.display_path(path)
        entries = self.list()
        existing = next((entry for entry in entries if entry.get("path") == display_path), None)
        updated = {
            "id": int(existing["id"]) if existing else self.next_id(),
            "type": artifact_type,
            "path": display_path,
            "created_at": utc_now_iso(),
            "source": source,
            "caption": caption,
            "pinned": bool(existing.get("pinned")) if existing else False,
        }
        kept = [entry for entry in entries if entry.get("path") != display_path]
        kept.append(updated)
        self.path.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in kept),
            encoding="utf-8",
        )
        return updated

    def append(
        self,
        *,
        artifact_type: str,
        path: Path,
        source: str,
        caption: str,
        artifact_id: int | None = None,
    ) -> dict[str, Any]:
        return append_jsonl(
            self.path,
            {
                "id": artifact_id or self.next_id(),
                "type": artifact_type,
                "path": self.display_path(path),
                "created_at": utc_now_iso(),
                "source": source,
                "caption": caption,
                "pinned": False,
            },
        )

    def set_pinned(self, artifact_id: int, pinned: bool) -> dict[str, Any] | None:
        entries = self.list()
        updated: dict[str, Any] | None = None
        for entry in entries:
            if int(entry["id"]) == artifact_id:
                entry["pinned"] = pinned
                updated = entry
        self.path.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        return updated

    def clear(self) -> None:
        self.path.write_text("", encoding="utf-8")


class ActiveJobStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def begin(self, *, kind: str, language: str | None = None, source: str, input_text: str) -> str:
        job_id = str(uuid4())
        payload = {
            "id": job_id,
            "kind": kind,
            "language": language,
            "source": source,
            "input": input_text,
            "started_at": utc_now_iso(),
            "status": "running",
        }
        with self._lock:
            self._jobs[job_id] = payload
        return job_id

    def finish(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                [dict(job) for job in self._jobs.values()],
                key=lambda job: str(job.get("started_at", "")),
            )

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()


class ExecutionSession(Protocol):
    language: str

    def clear(self) -> None: ...

    def stop(self) -> None: ...

    def execute(self, code: str, *, source: str, interactive: bool = False) -> dict[str, Any]: ...


class PythonSession:
    language = "python"

    def __init__(
        self,
        project: ProjectState,
        transcript: TranscriptStore,
        artifacts: ArtifactStore,
        active_jobs: ActiveJobStore,
        plots_dir: Path,
    ) -> None:
        self.project = project
        self.transcript = transcript
        self.artifacts = artifacts
        self.active_jobs = active_jobs
        self.plots_dir = plots_dir
        self.process: subprocess.Popen[str] | None = None

    def clear(self) -> None:
        self.stop()

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process = None

    def _ensure_worker(self) -> subprocess.Popen[str]:
        if self.process is not None and self.process.poll() is None:
            return self.process
        self.project.ensure_venv()
        env = self._venv_env()
        self.process = subprocess.Popen(
            [
                str(self.project.venv_python),
                "-u",
                str(Path(__file__).resolve().parent / "session_worker.py"),
                "--plots-dir",
                str(self.plots_dir),
            ],
            cwd=self.project.root,
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return self.process

    def _venv_env(self) -> dict[str, str]:
        env = os.environ.copy()
        bin_dir = str(self.project.venv_dir / "bin")
        env["VIRTUAL_ENV"] = str(self.project.venv_dir)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env.pop("PYTHONHOME", None)
        return env

    def execute(self, code: str, *, source: str, interactive: bool = False) -> dict[str, Any]:
        job_id = self.active_jobs.begin(kind="session", language=self.language, source=source, input_text=code)
        start = time.perf_counter()
        try:
            before_pngs = self._png_snapshot()
            response = self._send_worker_request(
                {
                    "code": code,
                    "interactive": interactive,
                }
            )
            generated_artifacts = self._record_changed_png_artifacts(before_pngs)
            duration_ms = int((time.perf_counter() - start) * 1000)
            entry = self.transcript.append_session(
                language=self.language,
                source=source,
                code=code,
                stdout=str(response.get("stdout", "")),
                stderr=str(response.get("stderr", "")),
                ok=bool(response.get("ok")),
                duration_ms=duration_ms,
                artifacts=generated_artifacts,
            )
            return {"entry": entry, "artifacts": generated_artifacts}
        finally:
            self.active_jobs.finish(job_id)

    def _png_snapshot(self) -> dict[Path, tuple[int, int]]:
        snapshot: dict[Path, tuple[int, int]] = {}
        root = self.project.plots_dir.resolve()
        if not root.exists():
            return snapshot
        for path in root.rglob("*"):
            if path.suffix.lower() != ".png" or not path.is_file():
                continue
            resolved = path.resolve()
            stat = resolved.stat()
            snapshot[resolved] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def _record_changed_png_artifacts(self, before: dict[Path, tuple[int, int]]) -> list[dict[str, Any]]:
        after = self._png_snapshot()
        changed_paths = [
            path
            for path, signature in after.items()
            if before.get(path) != signature
        ]
        generated = []
        for path in sorted(changed_paths):
            generated.append(
                self.artifacts.upsert_path(
                    artifact_type="plot",
                    path=path,
                    source="session",
                    caption=path.name,
                )
            )
        return generated

    def _send_worker_request(self, request: dict[str, Any]) -> dict[str, Any]:
        process = self._ensure_worker()
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("Python session worker pipes are unavailable")
        process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr is not None else ""
            self.process = None
            return {"stdout": "", "stderr": f"Python session worker stopped.\n{stderr}", "ok": False, "artifacts": []}
        return json.loads(line)


class RSession:
    language = "r"

    def __init__(
        self,
        project: ProjectState,
        transcript: TranscriptStore,
        artifacts: ArtifactStore,
        active_jobs: ActiveJobStore,
        plots_dir: Path,
    ) -> None:
        self.project = project
        self.transcript = transcript
        self.artifacts = artifacts
        self.active_jobs = active_jobs
        self.plots_dir = plots_dir
        self.process: subprocess.Popen[str] | None = None

    def clear(self) -> None:
        self.stop()

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process = None

    def _ensure_worker(self) -> subprocess.Popen[str]:
        if self.process is not None and self.process.poll() is None:
            return self.process
        self.project.ensure_runtime()
        env = self.project._renv_env()
        self.process = subprocess.Popen(
            [
                self.project.r_executable,
                "--vanilla",
                str(Path(__file__).resolve().parent / "r_session_worker.R"),
                "--plots-dir",
                str(self.plots_dir),
            ],
            cwd=self.project.root,
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return self.process

    def execute(self, code: str, *, source: str, interactive: bool = False) -> dict[str, Any]:
        job_id = self.active_jobs.begin(kind="session", language=self.language, source=source, input_text=code)
        start = time.perf_counter()
        try:
            before_pngs = self._png_snapshot()
            response = self._send_worker_request(
                {
                    "code": code,
                    "interactive": interactive,
                }
            )
            generated_artifacts = self._record_changed_png_artifacts(before_pngs)
            duration_ms = int((time.perf_counter() - start) * 1000)
            entry = self.transcript.append_session(
                language=self.language,
                source=source,
                code=code,
                stdout=str(response.get("stdout", "")),
                stderr=str(response.get("stderr", "")),
                ok=bool(response.get("ok")),
                duration_ms=duration_ms,
                artifacts=generated_artifacts,
            )
            return {"entry": entry, "artifacts": generated_artifacts}
        finally:
            self.active_jobs.finish(job_id)

    def _png_snapshot(self) -> dict[Path, tuple[int, int]]:
        snapshot: dict[Path, tuple[int, int]] = {}
        root = self.project.plots_dir.resolve()
        if not root.exists():
            return snapshot
        for path in root.rglob("*"):
            if path.suffix.lower() != ".png" or not path.is_file():
                continue
            resolved = path.resolve()
            stat = resolved.stat()
            snapshot[resolved] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def _record_changed_png_artifacts(self, before: dict[Path, tuple[int, int]]) -> list[dict[str, Any]]:
        after = self._png_snapshot()
        changed_paths = [
            path
            for path, signature in after.items()
            if before.get(path) != signature
        ]
        generated = []
        for path in sorted(changed_paths):
            generated.append(
                self.artifacts.upsert_path(
                    artifact_type="plot",
                    path=path,
                    source="session",
                    caption=path.name,
                )
            )
        return generated

    def _send_worker_request(self, request: dict[str, Any]) -> dict[str, Any]:
        process = self._ensure_worker()
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("R session worker pipes are unavailable")
        process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr is not None else ""
            self.process = None
            return {"stdout": "", "stderr": f"R session worker stopped.\n{stderr}", "ok": False, "artifacts": []}
        return json.loads(line)

class ShellSession:
    def __init__(self, project: ProjectState, transcript: TranscriptStore, active_jobs: ActiveJobStore, cwd: Path) -> None:
        self.project = project
        self.transcript = transcript
        self.active_jobs = active_jobs
        self.cwd = cwd

    def execute(self, command: str, *, source: str = "user_shell") -> dict[str, Any]:
        job_id = self.active_jobs.begin(kind="shell", source=source, input_text=command)
        self.project.ensure_runtime()
        env = os.environ.copy()
        if self.project.language == "python":
            bin_dir = str(self.project.venv_dir / "bin")
            env["VIRTUAL_ENV"] = str(self.project.venv_dir)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
            env.pop("PYTHONHOME", None)
        start = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=self.cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
                executable="/bin/bash",
            )
            duration_ms = int((time.perf_counter() - start) * 1000)
            return self.transcript.append_shell(
                source=source,
                command=command,
                stdout=completed.stdout,
                stderr=completed.stderr,
                ok=completed.returncode == 0,
                exit_code=completed.returncode,
                duration_ms=duration_ms,
                cwd=self.cwd,
            )
        finally:
            self.active_jobs.finish(job_id)


class ContextBuilder:
    def __init__(self, project: ProjectState, chat: ChatStore, transcript: TranscriptStore, artifacts: ArtifactStore) -> None:
        self.project = project
        self.chat = chat
        self.transcript = transcript
        self.artifacts = artifacts

    def payload(self, current_request: str = "") -> dict[str, Any]:
        code = self.project.source_file.read_text(encoding="utf-8") if self.project.source_file.exists() else ""
        numbered_code = "\n".join(f"{idx:4d}: {line}" for idx, line in enumerate(code.splitlines(), start=1))
        artifact_entries = self.artifacts.list()
        environment = self.environment_payload()
        return {
            "current_user_request": current_request,
            "durable_code": {
                "path": self.project.source_file.name,
                "language": self.project.language,
                "numbered": numbered_code,
            },
            "session_summary": self.project.summary_file.read_text(encoding="utf-8"),
            "recent_chat": self.chat.recent(20),
            "recent_transcript": self.transcript.recent(20),
            "artifacts": {
                "recent": artifact_entries[-20:],
                "pinned": [entry for entry in artifact_entries if entry.get("pinned")],
            },
            "environment": environment,
        }

    def environment_payload(self) -> dict[str, Any]:
        if self.project.language == "r":
            return {
                "project_root": str(self.project.root),
                "language": "r",
                "r_executable": self.project.r_executable,
                "r_renv": str(self.project.renv_dir),
                "r_renv_lockfile": str(self.project.renv_lock_file),
                "package_install_hint": (
                    "Use renv::install(...) for project packages, prefer GitHub sources for "
                    "Bioconductor-style packages when available, then run "
                    "renv::snapshot(lockfile = 'coplot/renv.lock', prompt = FALSE). "
                    "R mode requires renv and jsonlite."
                ),
            }
        return {
            "project_root": str(self.project.root),
            "language": "python",
            "python_executable": str(self.project.venv_python),
            "python_venv": str(self.project.venv_dir),
            "package_install_hint": "Use python -m pip install ... or pip install ...; shell PATH points at the project venv.",
        }


class AgentService:
    def __init__(
        self,
        project: ProjectState,
        chat: ChatStore,
        context_builder: ContextBuilder,
        session: ExecutionSession,
        shell: ShellSession,
        settings: ModelSettingsStore,
    ) -> None:
        self.project = project
        self.chat = chat
        self.context_builder = context_builder
        self.session = session
        self.shell = shell
        self.settings = settings

    def respond(self, message: str, images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        global stop_requested
        stop_requested = False
        attachments = self._save_chat_images(images or [])
        self.chat.append("user", message, attachments=attachments)
        actions: list[dict[str, Any]] = []
        assistant = self._request_and_apply(message, action_feedback="", attachments=attachments)
        pending_actions = assistant["actions"]
        actions.extend(pending_actions)
        followup_count = 0
        while self._actions_need_followup(pending_actions) and followup_count < 3 and not stop_requested:
            followup_count += 1
            feedback = self._action_feedback(pending_actions)
            assistant = self._request_and_apply(
                "Continue from the executed action results. If no more action is needed, answer the user.",
                action_feedback=feedback,
            )
            pending_actions = assistant["actions"]
            actions.extend(pending_actions)
        if stop_requested:
            self.chat.append("system", "Agent stopped by user.")
        return {"message": assistant["message"], "actions": actions, "stopped": stop_requested}

    def compact_context(self) -> dict[str, Any]:
        settings = self.settings.read()
        model = str(settings["model"]).strip()
        if not model:
            raise RuntimeError("Model name is empty. Open chat settings and choose a model.")

        context = self.context_builder.payload("Compact context")
        prompt = f"{COMPACTION_PROMPT}\n\nContext payload:\n{json.dumps(context, ensure_ascii=False)}"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Create the compacted working-memory summary now."},
            ],
            "max_tokens": int(settings["max_tokens"]),
            "temperature": float(settings["temperature"]),
            "stream": False,
        }
        self._apply_reasoning_settings(payload, settings)
        request = urllib.request.Request(
            self.settings.request_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=int(settings["timeout_seconds"])) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = self._extract_chat_text(data)
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Context compaction failed: {exc}") from exc

        summary = content.strip()
        self.project.summary_file.write_text(summary + "\n", encoding="utf-8")
        self.project.transcript_file.write_text("", encoding="utf-8")
        system_message = {
            "id": str(uuid4()),
            "created_at": utc_now_iso(),
            "role": "system",
            "content": f"Context compacted.\n\n{summary}",
        }
        self.chat.replace([system_message])
        return {"message": system_message, "summary": summary}

    def _save_chat_images(self, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for image in images:
            mime_type = str(image.get("mime_type") or image.get("type") or "").strip().lower()
            data_url = str(image.get("data_url") or "")
            if mime_type != "image/png" or not data_url.startswith("data:image/png;base64,"):
                raise ValueError("Only pasted PNG images are supported.")
            try:
                raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
            except (binascii.Error, IndexError) as exc:
                raise ValueError("Pasted PNG image data is invalid.") from exc
            if len(raw) > MAX_CHAT_IMAGE_BYTES:
                raise ValueError("Pasted PNG image is too large.")
            image_id = str(uuid4())
            filename = f"{image_id}.png"
            path = self.project.chat_images_dir / filename
            path.write_bytes(raw)
            attachments.append(
                {
                    "id": image_id,
                    "type": "image",
                    "mime_type": "image/png",
                    "path": str(path.resolve().relative_to(self.project.root.resolve())),
                    "size_bytes": len(raw),
                }
            )
        return attachments

    def _request_and_apply(
        self,
        message: str,
        *,
        action_feedback: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        settings = self.settings.read()
        model = str(settings["model"]).strip()
        if not model:
            content = "Model name is empty. Open chat settings and choose a model."
            assistant = self.chat.append("system", content)
            return {"message": assistant, "actions": []}

        context = self.context_builder.payload(message)
        if action_feedback:
            context["action_feedback"] = action_feedback
        prompt = self._system_prompt(context)
        user_content = self._user_content(message, attachments=attachments or [])
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": int(settings["max_tokens"]),
            "temperature": float(settings["temperature"]),
            "stream": False,
        }
        self._apply_reasoning_settings(payload, settings)
        request = urllib.request.Request(
            self.settings.request_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=int(settings["timeout_seconds"])) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = self._extract_chat_text(data)
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
            content = f"Model request failed: {exc}"

        assistant = self.chat.append("assistant", content)
        actions = self._run_actions(content)
        return {"message": assistant, "actions": actions}

    def _system_prompt(self, context: dict[str, Any]) -> str:
        edit_example = (
            "[{\"start_line\": 1, \"end_line\": 1, \"replacement\": \"print('new code')\\n\"}]"
            if self.project.language == "python"
            else "[{\"start_line\": 1, \"end_line\": 1, \"replacement\": \"print('new code')\\n\"}]"
        )
        if self.project.language == "r":
            run_example = "print(dim(df))"
            shell_example = "Rscript -e \"renv::status()\""
            runtime_rules = (
                "When you need ad hoc R inspection in the shared live session, emit:\n"
                "```coplot-run\n"
                f"{run_example}\n"
                "```\n"
                "When you need an ad hoc shell command, emit:\n"
                "```coplot-shell\n"
                f"{shell_example}\n"
                "```\n"
                "Use durable edits for reproducible work, session runs for scratch R, and shell "
                "commands for package or system checks. The R session runs from the project root "
                "with renv initialized. Install R packages with renv::install(...) so they land "
                "in ./coplot/renv/. Do not use install.packages(), BiocManager::install(), "
                "devtools, or remotes unless the user explicitly asks or renv::install(...) cannot "
                "handle the source. Prefer GitHub sources for Bioconductor-style packages when "
                "available, for example renv::install(\"joey711/phyloseq\"). After installing or "
                "upgrading R packages, run renv::snapshot(lockfile = \"coplot/renv.lock\", prompt = FALSE) "
                "to update ./coplot/renv.lock. "
                "R mode requires renv and jsonlite. Save plots as PNG files in ./coplot/plots/ using "
                "png(...); ...; dev.off(), ggsave(...), or another explicit file-writing API. "
                "Do not use interactive graphics devices or viewers such as quartz(), X11(), "
                "windows(), dev.new(), or plot panes; they can open local GUI windows, block "
                "execution, and prevent further agent iteration. Interactive graphics devices "
                "do not make plots visible to you."
            )
        else:
            run_example = "print(df.shape)"
            shell_example = "python -m pip show pandas"
            runtime_rules = (
                "When you need ad hoc Python inspection in the shared live session, emit:\n"
                "```coplot-run\n"
                f"{run_example}\n"
                "```\n"
                "When you need an ad hoc shell command, emit:\n"
                "```coplot-shell\n"
                f"{shell_example}\n"
                "```\n"
                "Use durable edits for reproducible work, session runs for scratch Python, and shell "
                "commands for package or system checks. The Python session and shell both use the "
                "project venv; install Python packages with python -m pip install ... so they land "
                "in that environment. Save plots as PNG files in ./coplot/plots/ using plt.savefig(...) "
                "or another explicit file-writing API, then close figures with plt.close(...). "
                "Do not call plt.show(); it can open a local GUI window, block execution, and prevent "
                "further agent iteration. Calling plt.show() does not make the plot visible to you."
            )
        return (
            "You are coplot, an LLM-assisted data science workspace agent. Keep durable code, "
            "session scratch work, shell commands, and artifacts clearly separated. This workspace "
            f"is locked to {self.project.language.upper() if self.project.language == 'r' else 'Python'}; "
            "do not switch languages or add language names to coplot action fences.\n\n"
            "When durable code should change, emit a fenced coplot-edit JSON block for "
            f"{self.project.source_file.name}. The block must be a JSON list of edits using 1-based line "
            "numbers against the current editor contents. end_line is inclusive:\n"
            "```coplot-edit\n"
            f"{edit_example}\n"
            "```\n"
            "Use start_line N and end_line 0 to insert before line N. Use start_line 0 "
            "and end_line 0 to insert at the beginning of an empty file. "
            f"Use coplot-run freely for fast exploration. Once coplot-run gives you the desired result, "
            f"add durable code to {self.project.source_file.name} with coplot-edit. Remember, the user "
            f"must be able to get the same result as you by simply running {self.project.source_file.name}. "
            f"{runtime_rules} The user will often ask you to visually inspect plots for feedback. "
            "You can only see plots if they have been saved as PNG files in the ./coplot/plots/ folder. "
            "If plot images are attached to the user message, inspect the image directly instead of saying "
            "you cannot see it.\n\n"
            f"Context payload:\n{json.dumps(context, ensure_ascii=False)}"
        )

    def _actions_need_followup(self, actions: list[dict[str, Any]]) -> bool:
        if not actions:
            return False
        recent = actions[-6:]
        return any(action.get("type") in {"edit_file", "execute_session", "execute_shell"} for action in recent)

    def _action_feedback(self, actions: list[dict[str, Any]]) -> str:
        chunks = []
        for action in actions[-6:]:
            if action.get("type") == "edit_file":
                chunks.append(
                    json.dumps(
                        {
                            "type": "edit_file",
                            "status": action.get("status"),
                            "selection": action.get("selection"),
                            "error": action.get("error"),
                            "message": (
                                f"Edit {action.get('status')} in {self.project.source_file.name}. "
                                "The updated durable code is now visible in the context payload."
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
                continue
            if action.get("type") not in {"execute_session", "execute_shell"}:
                continue
            result = action.get("result", {})
            entry = truncate_transcript_entry(result.get("entry", result))
            chunks.append(
                json.dumps(
                    {
                        "type": action.get("type"),
                        "ok": entry.get("ok"),
                        "stdout": entry.get("stdout", ""),
                        "stderr": entry.get("stderr", ""),
                        "artifacts": result.get("artifacts", []),
                    },
                    ensure_ascii=False,
                )
            )
        return "\n".join(chunks)

    def _apply_reasoning_settings(self, payload: dict[str, Any], settings: dict[str, Any]) -> None:
        control = resolve_reasoning_control(settings)
        reasoning_enabled = bool(settings["reasoning_enabled"])
        if control == "none":
            return
        if control == "ollama":
            if not reasoning_enabled:
                payload["reasoning_effort"] = "none"
            return
        if control == "chat_template_kwargs":
            payload["chat_template_kwargs"] = {"enable_thinking": reasoning_enabled}

    def _user_content(self, message: str, *, attachments: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        if attachments:
            parts: list[dict[str, Any]] = [{"type": "text", "text": message or "Please inspect the attached image."}]
            for attachment in attachments:
                data_url = self._chat_attachment_data_url(attachment)
                if data_url:
                    parts.append({"type": "image_url", "image_url": {"url": data_url}})
            return parts if len(parts) > 1 else message

        artifacts = self._image_artifacts_for_message(message)
        if not artifacts:
            return message
        parts = [{"type": "text", "text": message}]
        for artifact in artifacts:
            data_url = self._artifact_data_url(artifact)
            if data_url:
                parts.append({"type": "image_url", "image_url": {"url": data_url}})
        return parts if len(parts) > 1 else message

    def _chat_attachment_data_url(self, attachment: dict[str, Any]) -> str | None:
        if attachment.get("mime_type") != "image/png":
            return None
        path_value = str(attachment.get("path", ""))
        if not path_value:
            return None
        path = (self.project.root / path_value).resolve()
        if not path.is_file() or not path.is_relative_to(self.project.chat_images_dir.resolve()):
            return None
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def _image_artifacts_for_message(self, message: str) -> list[dict[str, Any]]:
        lowered = message.lower()
        visual_terms = {
            "plot",
            "image",
            "png",
            "figure",
            "chart",
            "graph",
            "visual",
            "look at",
            "see it",
            "inspect",
        }
        if not any(term in lowered for term in visual_terms):
            return []
        plots = [entry for entry in artifact_store.list() if entry.get("type") == "plot"]
        plots.sort(key=lambda entry: (str(entry.get("created_at", "")), str(entry.get("path", ""))))
        return plots[-1:] if plots else []

    def _artifact_data_url(self, artifact: dict[str, Any]) -> str | None:
        path_value = str(artifact.get("path", ""))
        if not path_value:
            return None
        path = (self.project.root / path_value).resolve()
        if not path.is_file() or not path.is_relative_to(self.project.plots_dir.resolve()):
            return None
        mime_type = "image/png" if path.suffix.lower() == ".png" else "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _extract_chat_text(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices", [])
        if not choices:
            raise KeyError("Model response did not contain chat choices.")
        choice = choices[0]
        message = choice.get("message", {})
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            return f"Model refused the request: {refusal.strip()}"
        finish_reason = choice.get("finish_reason", "unknown")
        fields = sorted(key for key, value in message.items() if value)
        return (
            "Model response did not contain final chat message text "
            f"(finish_reason={finish_reason}, message_fields={fields})."
        )

    def _run_actions(self, content: str) -> list[dict[str, Any]]:
        actions = []
        for match in EDIT_BLOCK_RE.finditer(content):
            raw = match.group("json").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
                if not isinstance(data, list):
                    raise ValueError("coplot-edit block must contain a JSON list")
                before = self.project.source_file.read_text(encoding="utf-8")
                selection = selection_for_line_edits(before, data)
                after = apply_line_edits(before, data)
                self.project.source_file.write_text(after, encoding="utf-8")
                actions.append({"type": "edit_file", "status": "applied", "edits": data, "selection": selection})
            except Exception as exc:
                actions.append({"type": "edit_file", "status": "failed", "error": str(exc), "raw": raw})
                self.chat.append("system", f"Failed to apply coplot-edit block: {exc}")

        for match in RUN_BLOCK_RE.finditer(content):
            code = match.group("code").strip()
            if code:
                actions.append({"type": "execute_session", "result": self.session.execute(code, source="agent_executed")})

        for match in SHELL_BLOCK_RE.finditer(content):
            command = match.group("command").strip()
            if command:
                actions.append({"type": "execute_shell", "result": self.shell.execute(command, source="agent_shell")})
        return actions


def configure_app(workspace_root: Path) -> None:
    global project
    global chat_store
    global transcript_store
    global artifact_store
    global model_settings_store
    global session
    global shell_session
    global context_builder
    global agent_service
    global active_job_store
    global workspace_first_run

    discovered = ProjectState.discover(workspace_root)
    discovered.ensure_base()
    model_settings_store = ModelSettingsStore(discovered.model_settings_file, DEFAULTS_FILE)
    model_settings_store.ensure_workspace_config()
    settings = model_settings_store.read()
    language = infer_workspace_language(discovered, settings)
    project = discovered.with_language(language)
    workspace_first_run = not is_workspace_setup_complete(discovered, settings, language)
    if not workspace_first_run:
        model_settings_store.write({"language": language, "workspace_setup_complete": True})
        project.source_file.touch(exist_ok=True)
    chat_store = ChatStore(project.chat_file)
    transcript_store = TranscriptStore(project.transcript_file)
    artifact_store = ArtifactStore(project.artifacts_file, project.root)
    active_job_store = ActiveJobStore()
    configure_runtime_services()


def infer_workspace_language(project_state: ProjectState, settings: dict[str, Any]) -> str:
    language = normalize_language(settings.get("language", "python"))
    if bool(settings.get("workspace_setup_complete")):
        return language
    if (project_state.root / "coplot.R").exists() and not (project_state.root / "coplot.py").exists():
        return "r"
    return language


def is_workspace_setup_complete(project_state: ProjectState, settings: dict[str, Any], language: str) -> bool:
    if bool(settings.get("workspace_setup_complete")):
        return True
    if not project_state.model_settings_file.exists():
        return False
    source_file = project_state.root / ("coplot.R" if language == "r" else "coplot.py")
    if source_file.exists():
        return True
    return (project_state.root / "coplot.py").exists() or (project_state.root / "coplot.R").exists()


def configure_runtime_services() -> None:
    global session
    global shell_session
    global context_builder
    global agent_service

    session = create_execution_session(project)
    shell_session = ShellSession(project, transcript_store, active_job_store, project.root)
    context_builder = ContextBuilder(project, chat_store, transcript_store, artifact_store)
    agent_service = AgentService(
        project,
        chat_store,
        context_builder,
        session,
        shell_session,
        model_settings_store,
    )


def create_execution_session(project_state: ProjectState) -> ExecutionSession:
    if project_state.language == "r":
        return RSession(project_state, transcript_store, artifact_store, active_job_store, project_state.plots_dir)
    return PythonSession(project_state, transcript_store, artifact_store, active_job_store, project_state.plots_dir)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), format % args))

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        request_path = parsed.path
        if request_path == "/api/state":
            return self.send_json(self.state())
        if request_path == "/api/context":
            return self.send_json(context_builder.payload())
        if request_path == "/api/download-session":
            return self.download_session()
        if request_path.startswith("/plots/") or request_path.startswith("/coplot/plots/"):
            artifact_path = (project.root / request_path.lstrip("/")).resolve()
            if request_path.startswith("/plots/"):
                artifact_path = (project.plots_dir / request_path.removeprefix("/plots/")).resolve()
            if not artifact_path.is_relative_to(project.plots_dir.resolve()):
                return self.send_error(HTTPStatus.FORBIDDEN)
            return self.serve_file(artifact_path)
        return super().do_GET()

    def do_POST(self) -> None:
        routes = {
            "/api/save": self.save_source,
            "/api/run-file": self.run_file,
            "/api/run-session": self.run_session,
            "/api/clear-session": self.clear_session,
            "/api/clear-transcript": self.clear_transcript,
            "/api/run-shell": self.run_shell,
            "/api/chat": self.chat,
            "/api/stop": self.stop_agent,
            "/api/compact-context": self.compact_context,
            "/api/clear-context": self.clear_context,
            "/api/model-settings": self.save_model_settings,
            "/api/model-settings-defaults": self.save_model_settings_defaults,
            "/api/model-settings-proceed": self.proceed_model_settings,
            "/api/model-endpoint": self.model_endpoint,
            "/api/artifact-pin": self.pin_artifact,
        }
        handler = routes.get(self.path)
        if handler is None:
            return self.send_error(HTTPStatus.NOT_FOUND)
        try:
            return handler(self.read_body())
        except SourceConflictError as exc:
            return self.send_json({"error": str(exc), "state": self.state()}, status=409)
        except subprocess.TimeoutExpired:
            return self.send_json({"error": "Command timed out after 120 seconds"}, status=408)
        except Exception as exc:
            traceback.print_exc()
            return self.send_json({"error": str(exc)}, status=500)

    def serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        content_type = "image/png" if path.suffix.lower() == ".png" else "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def download_session(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            if project.chat_file.exists():
                archive.write(project.chat_file, "chat.jsonl")
            if project.source_file.exists():
                archive.write(project.source_file, project.source_file.name)
            if project.plots_dir.exists():
                archive.writestr("coplot/plots/", "")
                for path in sorted(project.plots_dir.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.resolve().relative_to(project.root.resolve()))
            if project.chat_images_dir.exists():
                archive.writestr("coplot/chat_images/", "")
                for path in sorted(project.chat_images_dir.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.resolve().relative_to(project.root.resolve()))
        data = buffer.getvalue()
        filename = f"coplot-session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def state(self) -> dict[str, Any]:
        context_payload = context_builder.payload()
        estimated_context_tokens = estimate_tokens(context_payload)
        context_breakdown = context_token_breakdown(context_payload)
        context_window_tokens = int(model_settings_store.read().get("context_window_tokens", 32768))
        return {
            "project": {
                "root": str(project.root),
                "source_file": project.source_file.name,
                "language": project.language,
                "runtime": "stdlib-http",
                "shell": "command-by-command",
                "venv": str(project.venv_dir) if project.language == "python" else None,
                "python": str(project.venv_python) if project.language == "python" else None,
                "renv": str(project.renv_dir) if project.language == "r" else None,
                "renv_lockfile": str(project.renv_lock_file) if project.language == "r" else None,
                "r": project.r_executable if project.language == "r" else None,
                "model_settings_file": str(model_settings_store.path),
                "defaults_file": str(model_settings_store.defaults_path),
                "first_run": workspace_first_run,
            },
            "source": project.source_file.read_text(encoding="utf-8") if project.source_file.exists() else "",
            "source_mtime_ns": str(file_mtime_ns(project.source_file)),
            "chat": chat_store.recent(100),
            "transcript": transcript_store.recent(100),
            "active_jobs": active_job_store.list(),
            "artifacts": artifact_store.list(),
            "model_settings": model_settings_store.read(),
            "context_usage": {
                "estimated_tokens": estimated_context_tokens,
                "limit_tokens": context_window_tokens,
                "percent": min(100, round((estimated_context_tokens / context_window_tokens) * 100)),
                "breakdown": context_breakdown,
            },
        }

    def save_source(self, body: dict[str, Any]) -> None:
        assert_source_not_stale(body.get("source_mtime_ns"))
        project.source_file.write_text(str(body.get("source", "")), encoding="utf-8")
        self.send_json(self.state())

    def run_file(self, body: dict[str, Any]) -> None:
        assert_source_not_stale(body.get("source_mtime_ns"))
        source = str(body.get("source", project.source_file.read_text(encoding="utf-8")))
        project.source_file.write_text(source, encoding="utf-8")
        result = session.execute(source, source="durable_script")
        self.send_json({"result": result, "state": self.state()})

    def run_session(self, body: dict[str, Any]) -> None:
        code = str(body.get("code", ""))
        result = session.execute(code, source=str(body.get("source", "user_executed")), interactive=True)
        self.send_json({"result": result, "state": self.state()})

    def clear_session(self, body: dict[str, Any]) -> None:
        session.clear()
        active_job_store.clear()
        project.source_file.write_text("", encoding="utf-8")
        project.chat_file.write_text("", encoding="utf-8")
        project.transcript_file.write_text("", encoding="utf-8")
        project.summary_file.write_text("# coplot Session Summary\n\n", encoding="utf-8")
        artifact_store.clear()
        self.clear_artifact_files()
        self.clear_chat_image_files()
        project.recreate_runtime()
        self.send_json({"result": {"ok": True, "message": "Workspace cleared."}, "state": self.state()})

    def clear_transcript(self, body: dict[str, Any]) -> None:
        project.transcript_file.write_text("", encoding="utf-8")
        self.send_json({"result": {"ok": True, "message": "Transcript cleared."}, "state": self.state()})

    def clear_context(self, body: dict[str, Any]) -> None:
        self.compact_context(body)

    def compact_context(self, body: dict[str, Any]) -> None:
        result = agent_service.compact_context()
        self.send_json({"result": result, "state": self.state()})

    def clear_artifact_files(self) -> None:
        root = project.plots_dir.resolve()
        for path in project.plots_dir.rglob("*"):
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or not path.is_file():
                continue
            path.unlink()

    def clear_chat_image_files(self) -> None:
        root = project.chat_images_dir.resolve()
        for path in project.chat_images_dir.rglob("*"):
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or not path.is_file():
                continue
            path.unlink()

    def run_shell(self, body: dict[str, Any]) -> None:
        command = str(body.get("command", ""))
        result = shell_session.execute(command, source=str(body.get("source", "user_shell")))
        self.send_json({"result": result, "state": self.state()})

    def chat(self, body: dict[str, Any]) -> None:
        images = body.get("images", [])
        if not isinstance(images, list):
            images = []
        result = agent_service.respond(str(body.get("message", "")), images=images)
        self.send_json({"result": result, "state": self.state()})

    def stop_agent(self, body: dict[str, Any]) -> None:
        global stop_requested
        stop_requested = True
        self.send_json({"result": {"ok": True, "message": "Stop requested."}, "state": self.state()})

    def save_model_settings(self, body: dict[str, Any]) -> None:
        if "language" in body and normalize_language(body["language"]) != project.language:
            return self.send_json({"error": "Workspace language cannot be changed after setup."}, status=400)
        settings = model_settings_store.write(body)
        self.send_json({"model_settings": settings, "state": self.state()})

    def save_model_settings_defaults(self, body: dict[str, Any]) -> None:
        settings = model_settings_store.write_defaults(body)
        self.send_json({"model_settings": settings, "state": self.state()})

    def proceed_model_settings(self, body: dict[str, Any]) -> None:
        global project
        global workspace_first_run
        language = normalize_language(body.get("language", model_settings_store.read().get("language", "python")))
        if not workspace_first_run and language != project.language:
            return self.send_json({"error": "Workspace language cannot be changed after setup."}, status=400)
        project = ProjectState.discover(project.root).with_language(language)
        project.ensure_runtime()
        configure_runtime_services()
        settings = model_settings_store.write({**body, "language": language, "workspace_setup_complete": True})
        workspace_first_run = False
        self.send_json({"model_settings": settings, "state": self.state()})

    def model_endpoint(self, body: dict[str, Any]) -> None:
        endpoint_url = str(body.get("endpoint_url", ""))
        if not endpoint_url.strip():
            return self.send_json({"error": "Endpoint URL is empty."}, status=400)
        try:
            result = fetch_models(endpoint_url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return self.send_json({"error": f"Could not connect to model endpoint: {exc}"}, status=400)
        self.send_json(result)

    def pin_artifact(self, body: dict[str, Any]) -> None:
        updated = artifact_store.set_pinned(int(body["id"]), bool(body.get("pinned", True)))
        self.send_json({"artifact": updated, "state": self.state()})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="coplot",
        description="Start a local coplot workspace.",
    )
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="Folder containing the data and analysis workspace.",
    )
    parser.add_argument("--host", default=os.environ.get("COPLOT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("COPLOT_PORT", "8765")))
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_app(Path(args.workspace))
    host = args.host
    port = args.port
    server = ThreadingHTTPServer((host, port), Handler)
    display_host = "localhost" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{port}"
    print(f"coplot web interface: {url}", flush=True)
    print(f"Project root: {project.root}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        session.stop()


if __name__ == "__main__":
    main()
