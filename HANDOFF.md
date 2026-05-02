# coplot Handoff

## Current Direction

coplot is the Python-first app. Keep it Python-only. Do not follow older plans to
add first-class R and shell durable files inside this repo.

The R sibling app should be developed separately as `coplotr`, with the same
product/API/frontend architecture where practical, but with an R runtime,
`analysis.R`/`coplot.R` decisions made there, and `renv`/`jsonlite` support.

## Product Model

coplot is a local web workspace for LLM-assisted Python data science. It gives
the user and agent shared access to:

- a durable Python script
- a persistent live Python session
- shell command execution from the workspace root
- chat
- plot/image artifacts

The core boundary is deliberate:

- durable code is the reproducible source of truth
- live session code is scratch/exploratory state
- shell is for packages, files, diagnostics, and command-line tools
- artifacts are explicit files under `coplot/plots/`

## Codebase Shape

Important files:

- `coplot/server.py`: HTTP server, workspace state, JSONL stores, model calls,
  action parsing/execution, active job state, artifact serving.
- `coplot/session_worker.py`: persistent Python execution worker.
- `coplot/static/index.html`: four-pane web UI.
- `coplot/static/app.js`: frontend state, editor behavior, polling, settings flow.
- `coplot/static/styles.css`: frontend styling.
- `pyproject.toml`: package metadata and `coplot` console entrypoint.
- `setup.py`: compatibility shim for older local setuptools.
- `README.md`: install/run overview.

Launch paths:

```bash
python3 -m coplot ~/projects/project_name
coplot ~/projects/project_name
```

The installed console command points at:

```text
coplot.server:main
```

## Workspace Shape

New workspaces create/use this visible layout:

```text
coplot.py              # durable Python source
coplot/
  config.json          # workspace model/settings config
  chat.jsonl
  transcript.jsonl
  artifacts.jsonl
  summary.md
  plots/               # generated PNG plot/image artifacts
  venv/                # workspace Python environment
```

Global defaults for new workspaces can be saved from the settings UI at:

```text
~/.config/coplot/defaults.json
```

There is no migration/backward-compatibility layer for the older local-testing
workspace shape (`analysis.py`, root `venv/`, root `plots/`, `coplot_data/`).

## Agent Protocol

Keep the generic action fence names:

```text
coplot-edit  -> edits coplot.py
coplot-run   -> runs Python in the persistent Python session
coplot-shell -> runs shell commands from the workspace root
```

Edit block format is a JSON list of line edits:

````text
```coplot-edit
[
  {"start_line": 1, "end_line": 1, "replacement": "print('hello')\n"}
]
```
````

Line edit semantics:

- line numbers are 1-based and inclusive
- `start_line: 0, end_line: 0` inserts at the beginning of an empty file or
  before the first line

Run examples:

````text
```coplot-run
print(df.head())
```
````

````text
```coplot-shell
python -m pip show pandas
```
````

Prompt rules to preserve:

- durable edits go to `coplot.py`
- scratch Python goes through `coplot-run`
- shell is for package/system/file checks
- Python packages should be installed into `coplot/venv/`
- plots must be saved as PNGs under `coplot/plots/`
- do not rely on `plt.show()` for artifacts

## Active Work State

The app now exposes backend-visible running jobs in `/api/state`:

```json
"active_jobs": [
  {
    "id": "...",
    "kind": "session",
    "language": "python",
    "source": "agent_executed",
    "input": "...",
    "started_at": "...",
    "status": "running"
  }
]
```

`PythonSession.execute()` and `ShellSession.execute()` register an active job
before blocking and clear it in `finally`. The frontend polls `/api/state` while
chat/session/shell work is pending and renders active jobs as running transcript
entries.

This helps long-running agent or user session/shell actions. It does not yet
make model-call phases or intermediate agent turns fully live; a future shared
coplot/coplotr improvement should add an `active_agent` state with model turn
phase/progress.

## Current Backend Architecture

Start in `coplot/server.py`.

Important classes/functions:

- `ProjectState`: discovers/creates workspace paths.
- `ModelSettingsStore`: reads/writes built-in defaults, global defaults, and
  workspace config.
- `ChatStore`, `TranscriptStore`, `ArtifactStore`: JSONL persistence.
- `ActiveJobStore`: in-memory running session/shell jobs exposed through state.
- `PythonSession`: starts `session_worker.py` inside `coplot/venv/`, maintains
  persistent Python state, records changed PNGs.
- `ShellSession`: runs shell commands in workspace root with `coplot/venv/bin`
  first on `PATH`.
- `ContextBuilder`: builds the JSON payload included in the model system prompt.
- `AgentService`: builds the system prompt, calls the model, parses action
  blocks, runs actions, handles follow-up turns.
- `Handler`: HTTP API and static/artifact serving.

Artifact URLs support both current paths (`/coplot/plots/...`) and legacy
`/plots/...` URL mapping for compatibility inside the UI.

## Current Frontend Architecture

Start in `coplot/static/app.js` and `coplot/static/index.html`.

Important behavior:

- `render()` refreshes state from `/api/state` but avoids overwriting dirty
  editor contents.
- `postAndRefresh()` handles most write actions and starts polling while work is
  pending.
- The editor is a textarea plus syntax-highlight mirror.
- `Cmd/Ctrl+Enter` runs selected code, or the current line when nothing is
  selected, then moves the cursor to the next line.
- The transcript renders completed entries and backend `active_jobs`.
- The artifact pane shows the latest plot and opens fullscreen on click.
- Settings modal handles endpoint connect, model selection, context window,
  reasoning toggle, save defaults, and workspace apply/proceed.

Replacing the editor with CodeMirror or Monaco remains a good future upgrade.

## Validation Checklist

Basic checks:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/coplot-pycache python3 -m py_compile coplot/server.py coplot/session_worker.py coplot/__main__.py coplot/__init__.py
node --check coplot/static/app.js
python3 -m coplot --help
```

Workspace smoke:

```bash
python3 -m coplot /private/tmp/coplot-smoke --host 127.0.0.1 --port 8876 --no-open
curl -sS http://127.0.0.1:8876/api/state
```

Manual checks:

- workspace creates `coplot.py` and `coplot/`
- Python session uses `coplot/venv/`
- shell PATH includes `coplot/venv/bin`
- plots saved in `coplot/plots/` appear in the artifact pane
- long session/shell runs appear as active transcript jobs
- stop prevents additional automatic agent follow-up turns without resetting the
  Python session
