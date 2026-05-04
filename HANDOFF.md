# coplot Handoff

## Current Direction

coplot supports Python and R in one codebase, but each workspace is locked to
one language at first-run setup. R is currently the default. Do not prompt the
model to choose between languages during normal use. The startup settings dialog
owns that choice, then the system prompt is rendered as either Python-only or
R-only for that workspace.

## Product Model

coplot is a local web workspace for LLM-assisted data science. It gives
the user and agent shared access to:

- a durable Python or R script
- a persistent live Python or R session
- shell command execution from the workspace root
- chat
- plot/image artifacts

The core boundary is deliberate:

- durable code is the reproducible source of truth
- live session code is scratch/exploratory state
- shell is for packages, files, diagnostics, and command-line tools
- artifacts are explicit files under `coplot/plots/`
- context compaction writes a concise working-memory summary to
  `coplot/summary.md`, replaces chat history with a system message containing
  that summary, and clears transcript history; it preserves source code,
  artifacts, and the selected-language session
- terminal/session stdout and stderr are middle-truncated per transcript entry
  before storage, UI rendering, context payloads, and action follow-up feedback;
  oversized output keeps the first 4 KiB and last 4 KiB with a marker between

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
coplot.py or coplot.R  # durable source selected at first-run setup
coplot/
  config.json          # workspace model/settings config
  chat.jsonl
  transcript.jsonl
  artifacts.jsonl
  summary.md
  plots/               # generated PNG plot/image artifacts
  chat_images/         # pasted PNGs attached to chat turns
  venv/                # Python workspace environment, Python mode only
  renv/                # R workspace environment, R mode only
  renv.lock            # R lockfile, R mode only
```

Fresh R workspaces create `coplot.R` with:

```r
#!/usr/bin/env Rscript

source("coplot/renv/activate.R")
```

R setup initializes `coplot/renv/`, installs `jsonlite` into that project
library, snapshots, and records `jsonlite` in `coplot/renv.lock`. The R session
worker loads global `jsonlite` before activating `renv` so JSON IPC works during
bootstrap.

Global defaults for new workspaces can be saved from the settings UI at:

```text
~/.config/coplot/defaults.json
```

There is no migration/backward-compatibility layer for the older local-testing
workspace shape (`analysis.py`, root `venv/`, root `plots/`, `coplot_data/`).

## Agent Protocol

Keep the generic action fence names:

```text
coplot-edit  -> edits coplot.py or coplot.R, depending on workspace language
coplot-run   -> runs code in the persistent selected-language session
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

- durable edits go to `coplot.py` in Python mode or `coplot.R` in R mode
- scratch code goes through `coplot-run`
- use `coplot-run` freely for exploration, but whenever code creates useful
  analysis state, also update the durable source with `coplot-edit`
- shell is for package/system/file checks
- Python packages should be installed into `coplot/venv/`
- R packages should be installed with `renv::install(...)`; prefer GitHub
  remotes for Bioconductor-style packages when possible, avoid `BiocManager`,
  `devtools`, or `remotes` unless needed, and snapshot with
  `renv::snapshot(lockfile = "coplot/renv.lock", prompt = FALSE)` after installs
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

`PythonSession.execute()`, `RSession.execute()`, and `ShellSession.execute()`
register an active job before blocking and clear it in `finally`. The frontend
polls `/api/state` every 5 seconds while chat/session/shell work is pending and
renders active jobs as running transcript entries.

This helps long-running agent or user session/shell actions. It does not yet
make model-call phases or intermediate agent turns fully live; a future
improvement should add an `active_agent` state with model turn
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
- `RSession`: starts `r_session_worker.R` with `Rscript --vanilla`, loads
  `jsonlite`, activates `coplot/renv/`, maintains persistent R state, records
  changed PNGs.
- `ShellSession`: runs shell commands in workspace root; Python mode puts
  `coplot/venv/bin` first on `PATH`.
- `ContextBuilder`: builds the JSON payload included in the model system prompt.
- `AgentService`: builds the system prompt, calls the model, parses action
  blocks, runs actions, handles follow-up turns. Pasted PNGs are stored under
  `coplot/chat_images/`; only metadata stays in `chat.jsonl`, while the image is
  attached to the model request for that user turn.
- `Handler`: HTTP API and static/artifact serving.

Artifact URLs support both current paths (`/coplot/plots/...`) and legacy
`/plots/...` URL mapping for compatibility inside the UI.

## Current Frontend Architecture

Start in `coplot/static/app.js` and `coplot/static/index.html`.

Important behavior:

- `render()` refreshes state from `/api/state` but avoids overwriting dirty
  editor contents. It also ignores older source snapshots using
  `source_mtime_ns`, which prevents stale polling responses from replacing a
  newer saved file.
- Manual save and run-file writes send the editor's last-seen
  `source_mtime_ns` as a string token; the backend rejects stale writes with
  HTTP 409 instead of overwriting newer agent/server edits. Keep this token out
  of JavaScript `Number`, because nanosecond mtimes exceed the safe integer
  range.
- `postAndRefresh()` handles most write actions and starts polling while work is
  pending.
- Chat and transcript panes only auto-scroll when already near the bottom, so
  polling does not reset the user's scroll position.
- The editor is a textarea plus syntax-highlight mirror.
- `Cmd/Ctrl+Enter` runs selected code, or the current line when nothing is
  selected, then moves the cursor to the next line.
- The transcript renders completed entries and backend `active_jobs`.
- The artifact pane shows the latest plot and opens fullscreen on click.
- Settings modal handles the prominent R/Python language switch, endpoint
  connect, model selection, context window, reasoning toggle, save defaults, and
  workspace apply/proceed.
- The UI accent is turquoise in R mode and orange-yellow in Python mode.

Replacing the editor with CodeMirror or Monaco remains a good future upgrade.

## Unreproduced Issues

- Possible action rendering/execution mismatch: user observed chat messages
  rendering action notes such as "Ran session scratch code" or "Applied editor
  update", but the expected terminal/editor effect did not appear and the model
  repeated similar intent statements. This is not currently reproducible. The
  frontend action notes are only display replacements for text that looks like
  `coplot-run`, `coplot-shell`, or `coplot-edit` fences; they do not prove the
  backend parsed or executed the action. If it happens again, compare
  `coplot/chat.jsonl` raw assistant content, `coplot/transcript.jsonl`, source
  file changes, and any `Failed to apply coplot-edit block` system messages.
  If chat has a valid backend action fence but transcript/source did not change,
  inspect the backend parser/execution path next.
- Source/editor synchronization remains a place to be careful. Recent fixes add
  stale-save rejection and post-save editor preservation, but if disappearing or
  reverting editor changes recur, avoid more local patches at first. Instead,
  formalize the source sync contract as a small state machine: server source has
  a version token, browser editor has a base version token, local dirty edits are
  preserved, agent edits replace the editor only when no unsaved local edits
  exist, saves require matching base/server versions, and conflict handling must
  not discard local text. Then audit `render()`, polling, save, run-file, and
  agent edit flows against that contract.

## Validation Checklist

Basic checks:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/coplot-pycache python3 -m py_compile coplot/server.py coplot/session_worker.py coplot/__main__.py coplot/__init__.py
node --check coplot/static/app.js
Rscript --vanilla -e "invisible(parse('coplot/r_session_worker.R')); cat('ok\n')"
python3 -m coplot --help
```

Workspace smoke:

```bash
python3 -m coplot /private/tmp/coplot-smoke --host 127.0.0.1 --port 8876 --no-open
curl -sS http://127.0.0.1:8876/api/state
```

Manual checks:

- first-run setup creates `coplot.py` and `coplot/venv/` in Python mode, or
  `coplot.R`, `coplot/renv/`, and `coplot/renv.lock` in R mode
- Python session uses `coplot/venv/`
- R session uses `coplot/renv/`; setup records `jsonlite` in `coplot/renv.lock`
- shell PATH includes `coplot/venv/bin` in Python mode
- plots saved in `coplot/plots/` appear in the artifact pane
- long selected-language session/shell runs appear as active transcript jobs
- stop prevents additional automatic agent follow-up turns without resetting the
  selected-language session
- edit-only agent actions trigger automatic follow-up and action feedback, so
  the model gets an explicit "edit applied" signal before continuing; this helps
  avoid repeated stale positional edits after the user says "proceed"
