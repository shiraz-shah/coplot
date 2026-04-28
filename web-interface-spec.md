# coplot Web Interface Spec

## Goal

Build a modern, minimal web interface for coplot: an LLM-assisted data science workspace where both the user and the agent work against a live local analytical session, while durable analysis code remains explicit and reproducible.

The web interface replaces the Textual TUI presentation layer. It must preserve the core interaction model already proven in the prototype:

- The editor holds durable reproducible code.
- The live language session holds in-memory analytical state.
- The terminal is for ad hoc commands, installation, diagnostics, and other one-off work.
- The chat pane is for formatted user/agent conversation.
- The artifact pane shows plots and lets the user move through plot history.
- Context sent to the LLM must clearly separate user requests, assistant chat, durable code, ad hoc execution, reproducible execution, stdout/stderr, and artifacts.

The new project should be a clean web implementation, not a port of the current TUI styling.

## Existing Prototype To Carry Forward

Current coplot prototype files and behavior:

- `analysis.py` is the durable source of truth.
- `.agent-data/chat.jsonl` stores persisted user, assistant, and system chat entries.
- `.agent-data/transcript.jsonl` stores executed code and outputs.
- `.agent-data/artifacts.jsonl` stores artifact metadata.
- `.agent-data/summary.md` is intended as compact restartable session state.
- `plots/` stores generated PNG plots.
- `PythonSession` executes code in a persistent in-process Python namespace.
- `TranscriptEntry.source` currently distinguishes:
  - `durable_script`: full editor/run-file execution.
  - `user_executed`: user ad hoc execution or selected editor lines.
  - `agent_executed`: agent scratch execution.
- The agent can currently emit:
  - `coplot-edit` blocks for durable line edits to `analysis.py`.
  - `coplot-run` blocks for scratch Python execution.
- The model prompt includes numbered `analysis.py`, recent chat, recent transcript, artifact metadata, and sometimes recent PNGs as multimodal data URLs when plot inspection is requested.
- The TUI has a context payload debug view and a rough context meter. The web app should keep both ideas, with better presentation.

Important lesson from the logs: Python execution is not a shell. A failed `!{sys.executable} -m pip install ...` attempt showed that the user and agent need a genuine terminal/shell surface for package installation and system commands, separate from Python session execution.

## Language Strategy: Python First, R Later

coplot should not be designed as a Python-only IDE. It should be designed as a live analytical session IDE with Python as the first implementation.

The product concepts should stay language-neutral:

- Durable source file.
- Live analytical session.
- Ad hoc execution.
- Reproducible execution.
- Shell terminal.
- Transcript.
- Artifact.
- Context payload.

Python MVP defaults:

- Durable source: `analysis.py`.
- Language: `python`.
- Session backend: current in-process `PythonSession`, or a Jupyter/IPykernel adapter if chosen.
- Package workflow: `pip`/`python -m pip` through the shell terminal or controlled subprocess execution.
- Plot capture: matplotlib PNG capture.
- Dataframe examples: pandas.

R future defaults:

- Durable source: `analysis.R`.
- Language: `r`.
- Session backend options: IRkernel, R subprocess, `radian`, or a custom R bridge using tools such as `callr` and `languageserver`.
- Package workflow: `install.packages()`, `renv`, system package hints, or shell commands where appropriate.
- Plot capture: R graphics devices, IRkernel rich display messages, or explicit artifact-saving helpers.
- Dataframe examples: `data.frame`, tibble, data.table.

Jupyter kernels may be useful adapters, especially because both IPykernel and IRkernel exist, but coplot should not make notebook concepts its internal product model. The app should own its own execution, transcript, artifact, and context schemas. Jupyter messages should be normalized into coplot events.

Avoid hardcoded Python assumptions in shared architecture:

- Do not assume the durable file is always `analysis.py`.
- Do not assume package installation always means `pip`.
- Do not assume plots always come from matplotlib.
- Do not assume errors are always Python tracebacks.
- Do not assume dataframe inspection always means pandas.
- Do not name the generic scratch action `python scratch`; name it session scratch and attach a language.

The right architecture is: Python first, language-neutral contracts from day one, R backend later without rewriting the UI or context engine.

## Project Environments And Dependencies

coplot runtime dependencies must be separate from analysis project dependencies.

The current prototype conflates:

- App/runtime dependencies needed to run the interface.
- Build/test dependencies needed to develop the app.
- Data analysis dependencies needed by the user's current project.

The end-user product should not work that way. coplot should be installed and updated like an application, while each analysis project owns its own environment.

coplot runtime:

- Installed outside analysis projects.
- Possible distribution paths: `pipx`, `uv tool install`, a standalone packaged app, or another app-style installer.
- Includes the web server, frontend assets, model integration, terminal bridge, editor integration, artifact handling, and other coplot runtime code.
- Should not require installing coplot runtime libraries into every project `.venv`.

Python project environments:

- Each Python analysis project should use a project-local `.venv/`.
- The Python session backend must run using the project interpreter, for example `project/.venv/bin/python`.
- The shell terminal should start with the project environment active, or make the selected interpreter/environment explicit.
- Python dependencies should use established project conventions:
  - `.venv/`
  - `requirements.txt`
  - `pyproject.toml`
  - `uv.lock`
  - other standard lock files where present.
- If a Python project does not already have a usable `.venv`, coplot should offer to create one for the user, and for new Python projects it should create one as part of project setup.

R project environments:

- Prefer `renv` for project-local R dependency management.
- If an R project has `renv.lock` or an existing `renv/` setup, coplot should use it.
- If an R project does not already have `renv`, coplot should offer to initialize it for the user, and for new R projects it should create an `renv` project environment when the user chooses project isolation.
- R sessions should start from the project root so `renv` can auto-activate.
- R package installation should use established R workflows:
  - `renv::install()`
  - `renv::restore()`
  - `install.packages()` when appropriate.
- coplot should show the active `.libPaths()` and whether `renv` is active.

R fallback:

- If `renv` is unavailable or the user declines project-local dependency management, fall back to the user's local R library.
- The fallback should avoid system-wide R libraries because users often lack permission to modify them and because system-wide installs make project reproducibility worse.
- coplot should make the fallback visible: "Using user-local R library" rather than pretending the project is isolated.

Project setup should be explicit but simple:

- New Python project: create project folder, create `.venv`, create `analysis.py`, create `.agent-data/`, create `plots/`.
- New R project: create project folder, initialize `renv` if selected, create `analysis.R`, create `.agent-data/`, create `plots/`.
- Existing Python project: detect `.venv`, interpreter, and dependency files; offer to create `.venv` if missing.
- Existing R project: detect `renv`; offer to initialize `renv` if missing; otherwise use the user's local R library as fallback.

Environment state should be part of model context when relevant:

- Active language.
- Active interpreter or executable.
- Python `.venv` path.
- R `.libPaths()` and `renv` status.
- Recent package installation attempts and outputs.
- Dependency files present in the project.

The guiding rule: coplot should expose established Python and R dependency practices, not invent a new package manager.

## Product Shape

The default screen is a four-pane working environment:

```text
┌──────────────────────────────┬──────────────────────────────┐
│ Durable Code Editor           │ Formatted LLM Chat            │
│ analysis.py                   │ user/assistant/system msgs    │
│ syntax, completion, run       │ streamed responses, actions   │
├──────────────────────────────┼──────────────────────────────┤
│ Terminal / Live Session       │ Artifacts / Latest Plot       │
│ shell + session commands      │ latest PNG + prev/next        │
│ stdout/stderr visible         │ metadata, attach-to-chat      │
└──────────────────────────────┴──────────────────────────────┘
```

The layout should be resizable and work on a laptop screen. Mobile is not a primary target, but the app should degrade to tabs or stacked panes without breaking.

## Pane Requirements

### 1. Chat Pane

Purpose: normal LLM conversation, formatted like current chat products.

Requirements:

- Render Markdown, fenced code blocks, lists, tables where practical, inline code, and model status.
- Stream assistant messages.
- Distinguish user, assistant, system notices, tool/action summaries, and execution results.
- Do not expose hidden chain of thought. If a model streams explicit reasoning fields, treat visibility as an advanced/debug option.
- Show when the agent applies editor edits or runs scratch code.
- Support legacy `coplot-edit` and `coplot-run` action blocks for prototype-session compatibility, but avoid making raw protocol blocks the primary UX.
- Let the user ask about the latest plot, selected plot, current dataframe, current code, recent errors, or prior output.
- Persist messages to `.agent-data/chat.jsonl` or the successor equivalent.

### 2. Durable Code Editor

Purpose: RStudio-style durable code for the analysis.

Requirements:

- Use a real web code editor such as Monaco or CodeMirror.
- Syntax highlighting for Python at MVP.
- Tab completion for Python symbols where feasible.
- Line numbers and stable line addressing, because agent edits currently rely on numbered lines.
- Commands:
  - Save file.
  - Run full file in the live language session as `durable_script`.
  - Run selected lines as `user_executed` unless explicitly promoted as durable.
  - Accept agent edits as patches or line edits.
  - Show diff/preview for agent edits before application if the change is non-trivial.
- Initial durable file can remain `analysis.py`.
- Do not hide the difference between code in the editor and code merely run in the terminal/session.

Future:

- Multiple files/tabs.
- R, Quarto, notebook-style cells.
- LSP-backed completions and diagnostics.

### 3. Terminal / Live Session Pane

Purpose: one-off work by user and agent.

This pane should not be a fake Python input only. It needs to support two related surfaces:

- A shell terminal for package installation and system commands.
- A language session command path for executing snippets against the shared in-memory namespace.

MVP may present them as tabs or a segmented control:

- `Session`: executes in the shared language namespace, initially Python.
- `Shell`: runs commands in the project environment, preferably in a PTY.

Requirements:

- User commands typed here are logged as ad hoc, not durable.
- Agent scratch commands are logged as agent ad hoc actions.
- Session execution should capture language, stdout, stderr, success/failure, duration, source, and generated artifacts.
- Shell execution should capture command, stdout, stderr, exit status, duration, working directory, and environment notes.
- Installing packages belongs in the shell path or through `subprocess` with clear logging, not via Jupyter magic syntax.
- Long-running or risky shell commands need user confirmation.
- The agent must be able to inspect recent outputs and errors from both session and shell transcripts.

Suggested transcript source vocabulary:

- `durable_script`: full editor file execution.
- `durable_selection`: selected editor execution intended as part of durable workflow, if added.
- `user_executed`: user ad hoc session code.
- `agent_executed`: agent ad hoc session code.
- `user_shell`: user ad hoc shell command.
- `agent_shell`: agent shell command.

### 4. Artifact / Plot Pane

Purpose: latest plot first, with history navigation.

Requirements:

- Show the latest generated plot as an image, not only a list entry.
- Provide previous/next controls through plot history.
- Show artifact id, caption, path, creation time, source, and pinned state.
- Let the user pin/unpin artifacts.
- Let the user attach the selected plot to the next model request.
- Support automatic attachment of recent or selected PNG artifacts when the user asks visual questions.
- Preserve the artifact ledger model: `id`, `type`, `path`, `created_at`, `source`, `code`, `caption`, `pinned`.
- The pane should update immediately after code execution creates new plots.

MVP artifact types:

- `plot` PNG.
- Optional `table` and `text` entries can wait until after the plot flow is solid.

## Backend Architecture

Build the backend around explicit services rather than UI callbacks:

- `ProjectState`: resolves paths, ensures directories, owns current project metadata.
- `ChatStore`: appends and reads chat messages.
- `TranscriptStore`: appends and reads language-session and shell execution records.
- `ArtifactStore`: appends and reads artifact records.
- `ExecutionBackend` / `LanguageSession`: language-neutral interface for executing code, interrupting, restarting, completion, symbol inspection, variable listing, and artifact capture.
- `PythonSession`: MVP implementation of `ExecutionBackend` using the current persistent Python namespace, or a Jupyter/IPykernel adapter if selected.
- `RSession`: future implementation of `ExecutionBackend` using IRkernel, R subprocess, `radian`, or another R bridge.
- `ShellSession`: PTY-backed shell execution in the project environment.
- `AgentService`: builds model context, streams responses, parses actions, executes approved actions.
- `ContextBuilder`: central owner of context layering and token budgeting.
- `WebSocket/EventBus`: streams chat tokens, execution output, editor changes, artifact updates, and status updates to the browser.

Recommended web stack can be chosen in the new project, but a pragmatic shape is:

- Python backend: FastAPI or similar.
- Browser frontend: React or Svelte with Monaco/CodeMirror.
- Real-time transport: WebSocket or Server-Sent Events for model streaming and execution events.
- Terminal: xterm.js connected to a backend PTY for shell commands.

## Execution Model

The app must keep these execution paths separate:

1. Full editor run:
   - Input: current `analysis.py`.
   - Executes in shared language session.
   - Transcript source: `durable_script`.
   - Should be considered reproducible, assuming file is saved.

2. Selected editor run:
   - Input: selected editor lines.
   - Executes in shared language session.
   - Transcript source: `user_executed` initially, or `durable_selection` if later introduced.
   - Must not imply the entire durable file has run.

3. User session terminal command:
   - Input: typed snippet.
   - Executes in shared language session.
   - Transcript source: `user_executed`.
   - Ad hoc by default.

4. Agent session scratch command:
   - Input: parsed/approved agent action.
   - Executes in shared language session.
   - Transcript source: `agent_executed`.
   - Ad hoc by default.

5. User shell command:
   - Input: shell terminal.
   - Executes in project environment.
   - Transcript source: `user_shell`.
   - Ad hoc by default.

6. Agent shell command:
   - Input: parsed/approved agent action.
   - Executes in project environment.
   - Transcript source: `agent_shell`.
   - Requires policy checks and confirmation for risky operations.

## Context Engineering Requirements

This is core product functionality, not a detail.

Every model request must make the following boundaries obvious:

- Current user request.
- Recent user/assistant chat.
- System notices.
- Current durable code, with line numbers.
- Recent durable executions.
- Recent user ad hoc session executions, labeled with language.
- Recent agent ad hoc session executions, labeled with language.
- Recent shell commands and outputs.
- Latest errors and warnings.
- Artifact index, selected artifact, pinned artifacts.
- Session summary.
- Environment notes, including installed packages when relevant.

Do not dump unlimited raw logs. Use layered context:

- Durable code: current full `analysis.py` for MVP.
- Summary: `.agent-data/summary.md`.
- Pinned context: user/agent-marked facts, constraints, and important artifacts.
- Recent transcript: bounded recent execution records.
- Recent chat: bounded recent messages.
- Artifact index: metadata for recent and pinned artifacts.
- Image payloads: only selected/recent plots when relevant.

The web app should include a context inspector that shows the exact next model payload in a readable form, preserving the TUI debug-context capability.

Suggested prompt contract:

- The assistant may propose durable code changes as structured actions.
- The assistant may request ad hoc session or shell execution as structured actions.
- Durable edits and ad hoc execution must be labeled separately in the UI and transcript.
- The assistant should prefer durable edits for results that matter and scratch execution for inspection.

Prefer a structured action protocol over raw fenced blocks in the long term. The existing `coplot-edit` and `coplot-run` blocks should be treated as legacy compatibility input from the prototype. The backend should normalize them into explicit coplot action objects.

## Agent Action Protocol

Internal normalized actions should look roughly like:

```json
{
  "type": "edit_file",
  "target": "analysis.py",
  "edits": [
    {
      "start_line": 3,
      "end_line": 5,
      "replacement": "print('new code')\n"
    }
  ],
  "durability": "durable"
}
```

```json
{
  "type": "execute_session",
  "language": "python",
  "code": "print(df.shape)",
  "source": "agent_executed",
  "durability": "ad_hoc"
}
```

```json
{
  "type": "execute_shell",
  "command": "python -m pip install scikit-learn",
  "source": "agent_shell",
  "durability": "ad_hoc",
  "requires_confirmation": true
}
```

The UI should show these as understandable agent actions, not as mysterious hidden tool calls.

## Persistence Format

The current JSONL files are good for MVP and should carry forward:

```text
.agent-data/
  summary.md
  chat.jsonl
  transcript.jsonl
  artifacts.jsonl
```

Extend transcript records to support both language-session execution and shell:

```json
{
  "id": "uuid",
  "created_at": "2026-04-26T12:00:00+00:00",
  "kind": "session",
  "language": "python",
  "source": "agent_executed",
  "input": "print(df.shape)",
  "stdout": "(100, 2)\n",
  "stderr": "",
  "ok": true,
  "duration_ms": 18,
  "artifacts": []
}
```

```json
{
  "id": "uuid",
  "created_at": "2026-04-26T12:01:00+00:00",
  "kind": "shell",
  "source": "agent_shell",
  "input": "python -m pip install scikit-learn",
  "stdout": "...",
  "stderr": "",
  "ok": true,
  "exit_code": 0,
  "duration_ms": 64271,
  "cwd": "/project"
}
```

Keep compatibility readers for the existing `code` field if importing old prototype sessions.

## Plot Capture

Python MVP behavior:

- The model should save plots explicitly as PNG files in `plots/`.
- After Python execution, detect new or modified PNG files in `plots/`.
- Upsert artifact ledger entries for changed PNGs so each plot path appears once.

The web app should additionally:

- Push an artifact update event to the browser.
- Display the latest plot immediately.
- Keep the plot history navigable.
- Let selected/pinned plots be included in model context.

Future:

- Rich display capture from Jupyter kernel messages.
- R plot capture through graphics devices, IRkernel display data, or explicit artifact helpers.
- Tables as browsable artifacts.
- HTML widgets where safe.

## Safety And Permissions

The app runs locally but still needs visible control boundaries.

Require confirmation for agent-initiated:

- Package installation.
- File deletion or overwrite outside managed project files.
- Long-running shell commands.
- Network calls beyond the configured model endpoint.
- Commands that send local data outside the machine.
- Environment mutation outside the project virtual environment.

Durable file edits should either:

- Auto-apply when small and low-risk, with clear undo/diff; or
- Require preview confirmation when broad, destructive, or touching multiple files.

User-typed terminal commands do not need confirmation, but must still be logged.

## MVP Acceptance Criteria

The first useful web version is done when:

- The app opens in a browser.
- The default screen has chat, code editor, terminal/session, and artifact panes.
- `analysis.py` can be edited, saved, and run in a shared live Python session.
- User ad hoc session commands execute against the same in-memory namespace.
- A shell terminal can run commands in the project environment.
- Chat messages stream from the configured local OpenAI-compatible model.
- Agent durable edits can update `analysis.py`.
- Agent scratch session commands can run and are visibly logged.
- Session stdout/stderr/errors are captured in transcript records with language labels.
- Matplotlib plots are saved as PNG artifacts and displayed in the artifact pane.
- Previous/next plot navigation works.
- The model context distinguishes durable code, user chat, assistant chat, session output, shell output, agent scratch work, user scratch work, language, and artifacts.
- A context inspector shows the next model payload.

## Non-MVP

Do not spend the first web milestone on:

- Recreating the polished TUI visual style.
- Full R execution support. However, the MVP architecture must not block adding R later.
- Full notebook import/export.
- Multi-user collaboration.
- Cloud deployment.
- Google Docs-style simultaneous editing.
- Deep IDE project navigation.
- Perfect LSP integration.

## Open Design Questions

- Should MVP use the current in-process `PythonSession`, or switch immediately to a Jupyter kernel backend for richer outputs and safer execution boundaries?
- Should the execution contract be called `LanguageSession`, `ExecutionBackend`, or something else?
- Should the shell terminal be a persistent PTY from day one, or should MVP begin with command-by-command shell execution?
- Should agent edits auto-apply as they do now, or should the web version default to diff preview?
- Should selected editor execution remain `user_executed`, or should it receive a distinct `durable_selection` source?
- Which frontend stack should the new project use?

Recommended defaults:

- Use FastAPI, WebSockets, React, Monaco, and xterm.js unless there is a strong reason to prefer another stack.
- Keep the current in-process Python session for the first thin slice, but design the execution interface so a Jupyter kernel can replace it.
- Keep Python as the first implementation, but use language-neutral names and transcript fields from the start.
- Use diff preview for agent edits in the web UI.
- Use a persistent PTY for shell if feasible; otherwise start with command-by-command shell execution and make the limitation explicit.
