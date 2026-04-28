# coplot

coplot is a local web workspace for LLM-assisted data science. It gives the user and agent a shared live Python session, shell command execution, chat, and plot artifacts while keeping durable analysis code explicit and reproducible.

The current MVP is intentionally dependency-light:

- Python standard library HTTP server
- Vanilla HTML, CSS, and JavaScript frontend
- A project-local `.venv/` for analysis dependencies such as pandas and matplotlib

## Run

From the project root:

```bash
python3 coplot_web/server.py
```

Then open:

```text
http://localhost:8765
```

On another machine on the same LAN, replace `localhost` with the machine's local IP address.

## Project Shape

- `coplot_web/server.py` serves the app, persists local state, calls an OpenAI-compatible chat endpoint, and executes agent actions.
- `coplot_web/session_worker.py` runs the persistent analysis Python session inside the project `.venv/`.
- `coplot_web/static/` contains the browser UI.
- `web-interface-spec.md` captures the product direction.
- `HANDOFF.md` captures the current implementation state and known rough edges.

PNG files created or modified in `plots/` by executed Python code are registered or updated as plot artifacts and shown in the artifact pane.

The session download action exports a zip containing `chat.jsonl`, `analysis.py`, and `plots/`.

## Local State

The app creates local workspace files that are intentionally not committed:

- `.venv/`
- `.agent-data/`
- `plots/`
- `analysis.py`
- generated plot/image files

This keeps GitHub focused on the coplot application source rather than one machine's current analysis session.

Model settings are application config, not session state. coplot reads and writes them at `coplot_web/config.json`. That file is intentionally ignored by Git because it contains local endpoint/model preferences.
