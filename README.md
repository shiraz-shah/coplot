# coplot

coplot is a local web workspace for LLM-assisted data science. It gives the user and agent a shared live Python session, shell command execution, chat, and plot artifacts while keeping durable analysis code explicit and reproducible.

The current MVP is intentionally dependency-light:

- Python standard library HTTP server
- Vanilla HTML, CSS, and JavaScript frontend
- A workspace-local `venv/` for analysis dependencies such as pandas and matplotlib

## Install

During development:

```bash
python3 -m pip install -e .
```

From GitHub as an app:

```bash
pipx install git+https://github.com/yourname/coplot.git
```

## Run

Point coplot at a workspace folder containing the data you want to analyze:

```bash
coplot ~/projects/project_name
```

You can also run the package module directly:

```bash
python3 -m coplot ~/projects/project_name
```

Then open the URL printed by the server, usually:

```text
http://localhost:8765
```

On another machine on the same LAN, replace `localhost` with the machine's local IP address.

## Project Shape

- `coplot/server.py` serves the app, persists local state, calls an OpenAI-compatible chat endpoint, and executes agent actions.
- `coplot/session_worker.py` runs the persistent analysis Python session inside the workspace `venv/`.
- `coplot/static/` contains the browser UI.
- `web-interface-spec.md` captures the product direction.
- `HANDOFF.md` captures the current implementation state and known rough edges.

PNG files created or modified in `plots/` by executed Python code are registered or updated as plot artifacts and shown in the artifact pane.

The session download action exports a zip containing `chat.jsonl`, `analysis.py`, and `plots/`.

## Local State

The app creates local workspace files that are intentionally not committed:

- `venv/`
- `coplot_data/`
- `plots/`
- `analysis.py`
- generated plot/image files

This keeps GitHub focused on the coplot application source rather than one machine's current analysis session. Each workspace owns its own environment and coplot data.

Workspace model settings live in `coplot_data/config.json`. The settings dialog can also save defaults for future workspaces at `~/.config/coplot/defaults.json`.
