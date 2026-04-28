from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
EDIT_BLOCK_RE = re.compile(r"```coplot-edit[ \t]*\n(?P<json>.*?)```", re.DOTALL | re.IGNORECASE)
RUN_BLOCK_RE = re.compile(r"```coplot-run[ \t]*\n(?P<code>.*?)```", re.DOTALL | re.IGNORECASE)
SHELL_BLOCK_RE = re.compile(r"```coplot-shell[ \t]*\n(?P<command>.*?)```", re.DOTALL | re.IGNORECASE)

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
    source_file: Path
    agent_data_dir: Path
    summary_file: Path
    chat_file: Path
    transcript_file: Path
    artifacts_file: Path
    model_settings_file: Path
    venv_dir: Path
    venv_python: Path
    plots_dir: Path

    @classmethod
    def discover(cls, root: Path) -> "ProjectState":
        agent_data_dir = root / ".agent-data"
        return cls(
            root=root,
            source_file=root / "analysis.py",
            agent_data_dir=agent_data_dir,
            summary_file=agent_data_dir / "summary.md",
            chat_file=agent_data_dir / "chat.jsonl",
            transcript_file=agent_data_dir / "transcript.jsonl",
            artifacts_file=agent_data_dir / "artifacts.jsonl",
            model_settings_file=CONFIG_FILE,
            venv_dir=root / ".venv",
            venv_python=root / ".venv" / "bin" / "python",
            plots_dir=root / "plots",
        )

    def ensure(self) -> None:
        self.agent_data_dir.mkdir(parents=True, exist_ok=True)
        self.plots_dir.mkdir(parents=True, exist_ok=True)
        self.source_file.touch(exist_ok=True)
        if not self.summary_file.exists():
            self.summary_file.write_text("# coplot Session Summary\n\n", encoding="utf-8")
        self.chat_file.touch(exist_ok=True)
        self.transcript_file.touch(exist_ok=True)
        self.artifacts_file.touch(exist_ok=True)
        self.ensure_venv()

    def ensure_venv(self) -> None:
        if self.venv_python.exists():
            return
        subprocess.run([sys.executable, "-m", "venv", str(self.venv_dir)], check=True)

    def recreate_venv(self) -> None:
        if self.venv_dir.exists():
            shutil.rmtree(self.venv_dir)
        self.ensure_venv()


def default_model_settings() -> dict[str, Any]:
    return {
        "endpoint_url": "localhost:8000",
        "model": "Qwen/Qwen3.6-35B-A3B-FP8",
        "max_tokens": 8192,
        "temperature": 0.2,
        "reasoning_enabled": False,
        "reasoning_effort": "medium",
        "timeout_seconds": 1800,
    }


class ModelSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, Any]:
        settings = default_model_settings()
        if self.path.exists():
            stored = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            settings.update({key: value for key, value in stored.items() if value is not None})
        return settings

    def write(self, values: dict[str, Any]) -> dict[str, Any]:
        current = self.read()
        allowed = set(default_model_settings())
        for key, value in values.items():
            if key in allowed:
                current[key] = value
        current["max_tokens"] = max(1, int(current["max_tokens"]))
        current["temperature"] = float(current["temperature"])
        current["reasoning_enabled"] = bool(current["reasoning_enabled"])
        current["timeout_seconds"] = max(1, int(current["timeout_seconds"]))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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


class ChatStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, role: str, content: str) -> dict[str, Any]:
        return append_jsonl(
            self.path,
            {
                "id": str(uuid4()),
                "created_at": utc_now_iso(),
                "role": role,
                "content": str(content),
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
        return read_jsonl(self.path, limit)


class ArtifactStore:
    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.root = root

    def list(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    def next_id(self) -> int:
        entries = self.list()
        return max([int(entry["id"]) for entry in entries], default=0) + 1

    def append(
        self,
        *,
        artifact_type: str,
        path: Path,
        source: str,
        code: str,
        caption: str,
        artifact_id: int | None = None,
    ) -> dict[str, Any]:
        try:
            display_path = str(path.resolve().relative_to(self.root))
        except ValueError:
            display_path = str(path)
        return append_jsonl(
            self.path,
            {
                "id": artifact_id or self.next_id(),
                "type": artifact_type,
                "path": display_path,
                "created_at": utc_now_iso(),
                "source": source,
                "code": code,
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


class PythonSession:
    def __init__(self, project: ProjectState, transcript: TranscriptStore, artifacts: ArtifactStore, plots_dir: Path) -> None:
        self.project = project
        self.transcript = transcript
        self.artifacts = artifacts
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
                str(ROOT / "coplot_web" / "session_worker.py"),
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
        start = time.perf_counter()
        before_pngs = self._png_snapshot()
        response = self._send_worker_request(
            {
                "code": code,
                "interactive": interactive,
            }
        )
        generated_artifacts = self._record_changed_png_artifacts(before_pngs, code)
        duration_ms = int((time.perf_counter() - start) * 1000)
        entry = self.transcript.append_session(
            language="python",
            source=source,
            code=code,
            stdout=str(response.get("stdout", "")),
            stderr=str(response.get("stderr", "")),
            ok=bool(response.get("ok")),
            duration_ms=duration_ms,
            artifacts=generated_artifacts,
        )
        return {"entry": entry, "artifacts": generated_artifacts}

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

    def _record_changed_png_artifacts(self, before: dict[Path, tuple[int, int]], code: str) -> list[dict[str, Any]]:
        after = self._png_snapshot()
        changed_paths = [
            path
            for path, signature in after.items()
            if before.get(path) != signature
        ]
        generated = []
        for path in sorted(changed_paths):
            generated.append(
                self.artifacts.append(
                    artifact_type="plot",
                    path=path,
                    source="session",
                    code=code,
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

class ShellSession:
    def __init__(self, project: ProjectState, transcript: TranscriptStore, cwd: Path) -> None:
        self.project = project
        self.transcript = transcript
        self.cwd = cwd

    def execute(self, command: str, *, source: str = "user_shell") -> dict[str, Any]:
        self.project.ensure_venv()
        env = os.environ.copy()
        bin_dir = str(self.project.venv_dir / "bin")
        env["VIRTUAL_ENV"] = str(self.project.venv_dir)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env.pop("PYTHONHOME", None)
        start = time.perf_counter()
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


class ContextBuilder:
    def __init__(self, project: ProjectState, chat: ChatStore, transcript: TranscriptStore, artifacts: ArtifactStore) -> None:
        self.project = project
        self.chat = chat
        self.transcript = transcript
        self.artifacts = artifacts

    def payload(self, current_request: str = "") -> dict[str, Any]:
        code = self.project.source_file.read_text(encoding="utf-8")
        numbered_code = "\n".join(f"{idx:4d}: {line}" for idx, line in enumerate(code.splitlines(), start=1))
        artifact_entries = self.artifacts.list()
        return {
            "current_user_request": current_request,
            "durable_code": {
                "path": self.project.source_file.name,
                "language": "python",
                "numbered": numbered_code,
            },
            "session_summary": self.project.summary_file.read_text(encoding="utf-8"),
            "recent_chat": self.chat.recent(20),
            "recent_transcript": self.transcript.recent(20),
            "artifacts": {
                "recent": artifact_entries[-20:],
                "pinned": [entry for entry in artifact_entries if entry.get("pinned")],
            },
            "environment": {
                "project_root": str(self.project.root),
                "language": "python",
                "python_executable": str(self.project.venv_python),
                "python_venv": str(self.project.venv_dir),
                "package_install_hint": "Use python -m pip install ... or pip install ...; shell PATH points at the project .venv.",
            },
        }


class AgentService:
    def __init__(
        self,
        project: ProjectState,
        chat: ChatStore,
        context_builder: ContextBuilder,
        session: PythonSession,
        shell: ShellSession,
        settings: ModelSettingsStore,
    ) -> None:
        self.project = project
        self.chat = chat
        self.context_builder = context_builder
        self.session = session
        self.shell = shell
        self.settings = settings

    def respond(self, message: str) -> dict[str, Any]:
        self.chat.append("user", message)
        settings = self.settings.read()
        model = str(settings["model"]).strip()
        if not model:
            content = "Model name is empty. Open chat settings and choose a model."
            assistant = self.chat.append("system", content)
            return {"message": assistant, "actions": []}

        context = self.context_builder.payload(message)
        prompt = (
            "You are coplot, an LLM-assisted data science workspace agent. Keep durable code, "
            "session scratch work, shell commands, and artifacts clearly separated.\n\n"
            "When durable code should change, emit a fenced coplot-edit JSON block with "
            "1-based inclusive line edits for analysis.py:\n"
            "```coplot-edit\n"
            "[{\"start_line\": 1, \"end_line\": 1, \"replacement\": \"print('new code')\\n\"}]\n"
            "```\n"
            "Use start_line 0 and end_line 0 to insert at the beginning of an empty file. "
            "When you need ad hoc Python inspection in the shared live session, emit:\n"
            "```coplot-run\n"
            "print(df.shape)\n"
            "```\n"
            "When you need an ad hoc shell command, emit:\n"
            "```coplot-shell\n"
            "python -m pip show pandas\n"
            "```\n"
            "Use durable edits for reproducible work, session runs for scratch Python, and shell "
            "commands for package or system checks. The Python session and shell both use the "
            "project .venv; install Python packages with python -m pip install ... so they land "
            "in that environment. The user will often ask you to visually inspect plots for "
            "feedback. You can only see plots if they have been saved as PNG files in the "
            "./plots/ folder. Therefore, always save plots as PNG files in ./plots/. Calling "
            "plt.show() may show a plot to the user in their local environment, but it does "
            "not make the plot visible to you. If plot images are attached to the user message, inspect the "
            "image directly instead of saying you cannot see it.\n\n"
            f"Context payload:\n{json.dumps(context, ensure_ascii=False)}"
        )
        user_content = self._user_content(message)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": int(settings["max_tokens"]),
            "temperature": float(settings["temperature"]),
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": bool(settings["reasoning_enabled"])},
        }
        if settings["reasoning_enabled"]:
            payload["reasoning"] = {"effort": str(settings["reasoning_effort"])}
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

    def _user_content(self, message: str) -> str | list[dict[str, Any]]:
        artifacts = self._image_artifacts_for_message(message)
        if not artifacts:
            return message
        parts: list[dict[str, Any]] = [{"type": "text", "text": message}]
        for artifact in artifacts:
            data_url = self._artifact_data_url(artifact)
            if data_url:
                parts.append({"type": "image_url", "image_url": {"url": data_url}})
        return parts if len(parts) > 1 else message

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
        pinned = [entry for entry in plots if entry.get("pinned")]
        latest = plots[-1:] if plots else []
        selected: list[dict[str, Any]] = []
        for artifact in [*pinned, *latest]:
            if artifact not in selected:
                selected.append(artifact)
        return selected[-4:]

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


project = ProjectState.discover(ROOT)
project.ensure()
chat_store = ChatStore(project.chat_file)
transcript_store = TranscriptStore(project.transcript_file)
artifact_store = ArtifactStore(project.artifacts_file, project.root)
model_settings_store = ModelSettingsStore(project.model_settings_file)
python_session = PythonSession(project, transcript_store, artifact_store, project.plots_dir)
shell_session = ShellSession(project, transcript_store, project.root)
context_builder = ContextBuilder(project, chat_store, transcript_store, artifact_store)
agent_service = AgentService(
    project,
    chat_store,
    context_builder,
    python_session,
    shell_session,
    model_settings_store,
)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        request_path = parsed.path
        if request_path == "/api/state":
            return self.send_json(self.state())
        if request_path == "/api/context":
            return self.send_json(context_builder.payload())
        if request_path.startswith("/plots/"):
            artifact_path = (project.root / request_path.lstrip("/")).resolve()
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
            "/api/run-shell": self.run_shell,
            "/api/chat": self.chat,
            "/api/model-settings": self.save_model_settings,
            "/api/artifact-pin": self.pin_artifact,
        }
        handler = routes.get(self.path)
        if handler is None:
            return self.send_error(HTTPStatus.NOT_FOUND)
        try:
            return handler(self.read_body())
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

    def state(self) -> dict[str, Any]:
        return {
            "project": {
                "root": str(project.root),
                "source_file": project.source_file.name,
                "runtime": "stdlib-http",
                "shell": "command-by-command",
                "venv": str(project.venv_dir),
                "python": str(project.venv_python),
                "model_settings_file": str(model_settings_store.path),
            },
            "source": project.source_file.read_text(encoding="utf-8"),
            "chat": chat_store.recent(100),
            "transcript": transcript_store.recent(100),
            "artifacts": artifact_store.list(),
            "model_settings": model_settings_store.read(),
        }

    def save_source(self, body: dict[str, Any]) -> None:
        project.source_file.write_text(str(body.get("source", "")), encoding="utf-8")
        self.send_json(self.state())

    def run_file(self, body: dict[str, Any]) -> None:
        source = str(body.get("source", project.source_file.read_text(encoding="utf-8")))
        project.source_file.write_text(source, encoding="utf-8")
        result = python_session.execute(source, source="durable_script")
        self.send_json({"result": result, "state": self.state()})

    def run_session(self, body: dict[str, Any]) -> None:
        code = str(body.get("code", ""))
        result = python_session.execute(code, source=str(body.get("source", "user_executed")), interactive=True)
        self.send_json({"result": result, "state": self.state()})

    def clear_session(self, body: dict[str, Any]) -> None:
        python_session.clear()
        project.source_file.write_text("", encoding="utf-8")
        project.chat_file.write_text("", encoding="utf-8")
        project.transcript_file.write_text("", encoding="utf-8")
        artifact_store.clear()
        self.clear_artifact_files()
        project.recreate_venv()
        self.send_json({"result": {"ok": True, "message": "Workspace cleared."}, "state": self.state()})

    def clear_artifact_files(self) -> None:
        root = project.plots_dir.resolve()
        for path in project.plots_dir.rglob("*"):
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or not path.is_file():
                continue
            path.unlink()

    def run_shell(self, body: dict[str, Any]) -> None:
        command = str(body.get("command", ""))
        result = shell_session.execute(command, source=str(body.get("source", "user_shell")))
        self.send_json({"result": result, "state": self.state()})

    def chat(self, body: dict[str, Any]) -> None:
        result = agent_service.respond(str(body.get("message", "")))
        self.send_json({"result": result, "state": self.state()})

    def save_model_settings(self, body: dict[str, Any]) -> None:
        settings = model_settings_store.write(body)
        self.send_json({"model_settings": settings, "state": self.state()})

    def pin_artifact(self, body: dict[str, Any]) -> None:
        updated = artifact_store.set_pinned(int(body["id"]), bool(body.get("pinned", True)))
        self.send_json({"artifact": updated, "state": self.state()})


def main() -> None:
    host = os.environ.get("COPLOT_HOST", "0.0.0.0")
    port = int(os.environ.get("COPLOT_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"coplot web interface: http://{host}:{port}", flush=True)
    print(f"Project root: {project.root}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
