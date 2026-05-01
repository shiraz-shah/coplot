# coplot Handoff

## Product Model

coplot is a local web workspace for LLM-assisted data science. The app gives the user and agent shared access to durable analysis scripts, live language sessions, shell execution, chat, and plot artifacts while keeping reproducible code explicit.

The core boundaries matter:

- Durable scripts are the reproducible source of truth.
- Live sessions hold exploratory in-memory state.
- Shell commands are for packages, command-line tools, diagnostics, and pipelines.
- Chat is where the user asks for help and the agent explains/acts.
- Artifacts, especially plots saved in `plots/`, are first-class outputs and can be attached to model requests.

coplot is now shaped as an app that points at a workspace folder. The app source and the user's analysis workspace are intentionally separate.

## Current Codebase Shape

The Python package is `coplot/`.

Important files:

- `coplot/server.py`: HTTP server, project/workspace state, config stores, model calls, action parsing/execution, artifact serving.
- `coplot/session_worker.py`: persistent Python execution worker used by `PythonSession`.
- `coplot/static/index.html`: four-pane web UI.
- `coplot/static/app.js`: frontend state, editor behavior, settings flow, API calls.
- `coplot/static/styles.css`: frontend styling.
- `pyproject.toml`: package metadata and `coplot` console entrypoint.
- `setup.py`: compatibility shim for older local setuptools.
- `README.md`: current install/run overview.

Current launch paths:

```bash
python3 -m coplot ~/projects/project_name
coplot ~/projects/project_name
```

The installed console command points at:

```text
coplot.server:main
```

## Current Workspace Shape

A Python workspace currently creates/uses:

```text
analysis.py          # current durable Python file; next work replaces this with coplot.py
venv/                # visible project Python environment
plots/               # generated plot/image artifacts
coplot_data/
  config.json        # workspace model/settings config
  chat.jsonl
  transcript.jsonl
  artifacts.jsonl
  summary.md
```

Global defaults for new workspaces can be saved from the settings UI at:

```text
~/.config/coplot/defaults.json
```

No backward compatibility is required for existing `analysis.py`, old transcripts, old action block names, or old workspace state. This has only been used for local testing.

## Current Working Behavior

Current app features that should be preserved while adding R/shell durable files:

- Start coplot against an arbitrary workspace folder.
- Create workspace state outside the app package.
- Create/use visible `venv/` for Python analysis dependencies.
- Four-pane UI: durable editor, chat, session/shell transcript, artifacts/latest plot.
- First-run/settings flow with endpoint connect and model dropdown.
- Endpoint-aware reasoning control:
  - Ollama thinking off: `reasoning_effort: "none"`.
  - vLLM/SGLang thinking off: `chat_template_kwargs.enable_thinking = false`.
- Context window detection:
  - vLLM: `/v1/models` `max_model_len`.
  - Ollama: `/api/ps` `context_length` for loaded models.
  - Otherwise editable setting.
- Context meter with approximate section breakdown by code, chat, transcript, and artifacts.
- Clear context clears chat/transcript/summary without clearing durable code or artifacts.
- Editor dirty state prevents ad hoc session/shell actions from reverting unsaved manual edits.
- `Cmd/Ctrl+Enter` runs selected code, or the current line when nothing is selected, then moves the cursor to the next line.
- Stop means no more automatic agent follow-up turns. It does not interrupt the current model request or reset the Python session.
- Agent ad hoc code/shell outputs are returned to the model for up to three follow-up turns unless stopped.

## Current Backend Architecture

Start in `coplot/server.py`.

Important classes/functions:

- `ProjectState`: discovers/creates workspace paths.
- `ModelSettingsStore`: reads/writes built-in defaults, global defaults, and workspace config.
- `ChatStore`, `TranscriptStore`, `ArtifactStore`: JSONL persistence.
- `PythonSession`: starts `session_worker.py` inside workspace `venv/`, maintains persistent Python state, records changed PNGs.
- `ShellSession`: runs shell commands in workspace root with `venv/bin` first on `PATH`.
- `ContextBuilder`: builds the JSON payload included in the model system prompt.
- `AgentService`: builds the system prompt, calls the model, parses action blocks, runs actions, handles follow-up turns.
- `Handler`: HTTP API and static serving.

Current action parsing uses regexes near the top of `server.py` and is handled in `AgentService._run_actions()`.

Current action protocol is Python-centric and should be replaced, not preserved:

- `coplot-edit` edits `analysis.py`.
- `coplot-run` runs Python session code.
- `coplot-shell` runs shell commands.

## Current Frontend Architecture

Start in `coplot/static/app.js` and `coplot/static/index.html`.

Important frontend behavior:

- `render()` refreshes state from `/api/state` but avoids overwriting dirty editor contents.
- `postAndRefresh()` handles most write actions.
- The editor is currently a textarea plus syntax-highlight mirror. This is fragile but working.
- The active-line shade is a separate overlay; line height is pinned in CSS to avoid drift.
- `runSelectedEditorCode()` runs selected/current-line editor code and then advances the cursor.
- Settings modal handles endpoint connect, model selection, context window, reasoning toggle, save defaults, and workspace apply/proceed.

Replacing the editor with CodeMirror or Monaco is recommended soon, especially before line-number/editor complexity grows further.

## Next Target: Python/R/Shell Durable Files

Implement first-class durable files for Python, R, and shell in the same app. Do not add startup mode selection for now.

Durable files:

```text
coplot.py
coplot.R
coplot.sh
```

Decisions:

- No backward compatibility required for `analysis.py`.
- New workspaces should create all three durable files.
- Python durable code moves to `coplot.py`.
- R durable code lives in `coplot.R`.
- Durable shell/pipeline code lives in `coplot.sh`.
- Editor header should show tabs for all three files.
- The active editor tab determines the active execution mode:
  - `coplot.py` -> Python session
  - `coplot.R` -> R session
  - `coplot.sh` -> shell
- The session pane should auto-switch based on active editor tab. Avoid independent startup mode selection.
- When the LLM edits a durable file, the UI should switch to that file's tab automatically.
- Add line numbers to the code editor. Line numbering should match the model/user edit protocol.

## New Agent Action Protocol

Replace the current generic/legacy action names with explicit language/file-specific blocks. No legacy compatibility required.

Edit blocks:

```text
coplot-edit-py -> edits coplot.py
coplot-edit-r  -> edits coplot.R
coplot-edit-sh -> edits coplot.sh
```

Run blocks:

```text
coplot-run-py -> persistent Python session
coplot-run-r  -> persistent R session
coplot-run-sh -> shell execution from workspace root
```

Use lowercase block names in the prompt. Regexes may be case-insensitive, but examples should be lowercase.

Edit block format remains a JSON list of line edits:

````text
```coplot-edit-r
[
  {"start_line": 1, "end_line": 1, "replacement": "print('hello')\n"}
]
```
````

Line edit semantics:

- Line numbers are 1-based and inclusive.
- `start_line: 0, end_line: 0` inserts at the beginning of an empty file or before the first line.
- Keep this consistent with visible editor line numbers.

Run block examples:

````text
```coplot-run-py
print(df.head())
```
````

````text
```coplot-run-r
summary(df)
```
````

````text
```coplot-run-sh
head data/input.tsv
```
````

## R And renv Requirements

R support should be first-class and project-isolated.

Decisions:

- `R` must be installed and available on `PATH` for R support.
- The R package `renv` is required.
- If R or `renv` is missing, coplot should exit on startup with a clear terminal error message.
- For new workspaces, initialize/use project-local `renv`.
- R sessions should start from the workspace root so `renv` can activate.
- R package installation should use `renv::install(...)`.
- Do not silently fall back to user/global R libraries if `renv` is missing.

Suggested startup checks:

```bash
Rscript -e "quit(status = !requireNamespace('renv', quietly = TRUE))"
```

Suggested missing-renv message:

```text
coplot requires the R package renv for R workspaces.
Install it with:
  Rscript -e 'install.packages("renv")'
```

R plot instruction for the model:

```r
png("plots/name.png")
# plotting code
dev.off()
```

The same artifact registration mechanism can be used initially: record new/changed PNGs in `plots/` after execution.

## Shell Durable File Decisions

`coplot.sh` should support reproducible shell/bioinformatics workflows.

Suggested initial file content:

```bash
#!/usr/bin/env bash
set -euo pipefail
```

Run-file behavior for `coplot.sh`:

```bash
bash coplot.sh
```

The executable bit is not required if run with `bash coplot.sh`.

Use `coplot-run-sh` for ad hoc shell diagnostics and one-off shell work. Use `coplot-edit-sh` for durable shell pipeline edits.

## Prompt Rewrite Requirements

The system prompt needs to become explicit about all three languages and durable files.

It should include:

```text
You are coplot, an LLM-assisted data science workspace agent. Keep durable files, session scratch work, shell commands, and artifacts clearly separated.

Available durable files:
- coplot.py: Python analysis code
- coplot.R: R analysis code
- coplot.sh: reproducible shell/pipeline commands

Active durable file: <active file>
Active execution mode: <python|r|shell>
```

Action instructions should be explicit:

```text
Use coplot-edit-py only for coplot.py.
Use coplot-edit-r only for coplot.R.
Use coplot-edit-sh only for coplot.sh.

Use coplot-run-py for ad hoc Python in the persistent Python session.
Use coplot-run-r for ad hoc R in the persistent R session.
Use coplot-run-sh for ad hoc shell commands from the workspace root.
```

Preference rules:

```text
Prefer the active durable file/language unless the user explicitly asks for another language.
Do not edit inactive durable files unless the user request requires it.
Do not run R code in Python or Python code in R.
Use coplot.sh for reproducible shell pipelines.
Use shell commands for package/system checks and command-line tools.
```

Environment rules:

```text
Python uses workspace venv/. Install packages with python -m pip install ...
R uses project renv. Install packages with renv::install("pkg").
Shell commands run from the workspace root.
```

Plot rules:

```text
Save plots as PNG files in plots/.
Python: plt.savefig("plots/name.png")
R: png("plots/name.png"); ...; dev.off()
```

The context payload should include all durable files with numbered contents and the active file. Suggested shape:

```json
{
  "active_source_file": "coplot.R",
  "durable_code": [
    {"path": "coplot.py", "language": "python", "numbered": "..."},
    {"path": "coplot.R", "language": "r", "numbered": "..."},
    {"path": "coplot.sh", "language": "shell", "numbered": "..."}
  ]
}
```

## Suggested Implementation Plan

1. Update `ProjectState` for multiple source files: `coplot.py`, `coplot.R`, `coplot.sh`.
2. Remove `analysis.py` assumptions. No migration/backward compatibility required.
3. Add editor tabs and active source-file state to `/api/state` and frontend state.
4. Add line-number gutter to the editor and keep it scroll-synced.
5. Auto-switch session mode based on active editor tab.
6. Add/replace action regexes for six new block types.
7. Map `coplot-edit-*` blocks to the correct durable file.
8. Map `coplot-run-py` to the existing Python session.
9. Map `coplot-run-sh` to shell execution.
10. Add `coplot.sh` run-file behavior with `bash coplot.sh`.
11. Add R startup checks for `R` and `renv`.
12. Initialize/use project `renv` for workspaces.
13. Add persistent R session worker.
14. Record changed PNG artifacts after R execution, same as Python.
15. Update `ContextBuilder` to include all durable files and active file.
16. Rewrite the model prompt according to the protocol above.
17. Ensure agent edits switch the active editor tab to the edited file.
18. Keep stop/follow-up loop behavior: max three follow-up turns, stop prevents additional follow-ups.

## Validation Checklist

Basic checks:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/coplot-pycache python3 -m py_compile coplot/server.py coplot/session_worker.py coplot/__main__.py coplot/__init__.py
node --check coplot/static/app.js
python3 -m coplot --help
```

Workspace smoke tests:

```bash
python3 -m coplot /private/tmp/coplot-smoke --host 127.0.0.1 --port 8876 --no-open
curl -sS http://127.0.0.1:8876/api/state
```

Manual UI checks:

- Tabs show `coplot.py`, `coplot.R`, `coplot.sh`.
- Active tab changes session mode automatically.
- `Cmd/Ctrl+Enter` runs selected/current line in the active file's language/mode.
- Line numbers match model edit line numbers.
- LLM edits switch to the edited tab.
- Python run creates/uses `venv/`.
- R startup exits clearly if R or `renv` is missing.
- R run uses project `renv` when available.
- Shell run executes in workspace root.
- Plots saved in `plots/` appear in artifact pane.
- Stop prevents additional automatic follow-up turns without resetting Python/R sessions.
