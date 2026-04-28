# coplot Web Handoff

## What This Is

coplot is a local web workspace for LLM-assisted data science. The core product model is:

- `analysis.py` is durable, reproducible code.
- A live Python session holds exploratory in-memory state.
- Shell commands are for package installs and diagnostics.
- Chat is where the user talks to the LLM.
- Artifacts, especially plots, are first-class outputs and can be shown to the user and attached to multimodal model requests.

The current implementation is a dependency-light MVP using Python stdlib HTTP serving plus vanilla HTML/CSS/JS. It intentionally avoids FastAPI/React for now so the app can run immediately in this workspace.

Main files:

- `web-interface-spec.md`: original product spec.
- `coplot_web/server.py`: HTTP API, persistence, model calls, action execution, artifact serving.
- `coplot_web/session_worker.py`: persistent venv-backed Python execution worker.
- `coplot_web/static/index.html`: four-pane UI.
- `coplot_web/static/app.js`: frontend state/actions.
- `coplot_web/static/styles.css`: frontend styling.
- `reference/`: old TUI prototype material kept locally as behavioral reference, but excluded from GitHub.

Run command:

```bash
python3 coplot_web/server.py
```

The server binds to `0.0.0.0:8765`, so from the LAN it has been used as:

```text
http://192.168.1.161:8765
```

## Current Working Core

The app has four panes:

- Durable code editor for `analysis.py`.
- Chat pane with model settings gear.
- Terminal / live session pane with `Session`, `Shell`, and `Clear All`.
- Artifact pane with plot preview, prev/next, and pin.

Core behavior working as of this handoff:

- LLM durable code suggestions land in the editor via `coplot-edit` blocks.
- LLM scratch Python runs execute via `coplot-run` blocks.
- LLM shell commands execute via `coplot-shell` blocks.
- User can run full `analysis.py` in a persistent Python session.
- User can run ad hoc Python snippets in that same live session.
- User and agent shell commands run with the project `.venv/bin` first on `PATH`.
- The live Python session runs in project `.venv`, not in the coplot server interpreter.
- `Clear All` clears `analysis.py`, chat, transcript, artifact ledger, generated artifact files, and recreates `.venv`.
- New or modified PNG files in `plots/` created during live session execution are registered in `.agent-data/artifacts.jsonl`.
- Plot artifacts render in the artifact pane.
- Plot/image inspection prompts attach recent/pinned plot PNGs to the model request as `data:image/png;base64,...` image parts.

## Persistence Files

coplot writes:

```text
.agent-data/
  chat.jsonl
  transcript.jsonl
  artifacts.jsonl
  summary.md
plots/
analysis.py
.venv/
```

`coplot_web/config.json` persists the chat endpoint config as application config, not session state, and is intentionally not cleared by `Clear All`. The file is ignored by Git because it contains local endpoint/model preferences.

Current defaults:

- Endpoint: `localhost:8000`
- Model: `Qwen/Qwen3.6-35B-A3B-FP8`
- Max tokens: `8192`
- Reasoning/model thinking: off

Important: reasoning off is implemented with:

```json
"chat_template_kwargs": {"enable_thinking": false}
```

This was copied from the old prototype behavior. The generic `reasoning` object is only added if model thinking is enabled.

## How Agent Actions Work

The server prompt instructs the LLM to use fenced action blocks:

```text
```coplot-edit
[{"start_line": 0, "end_line": 0, "replacement": "print('hello')\n"}]
```
```

Line edits are applied directly to `analysis.py`. Semantics are copied from the old local prototype:

- line numbers are 1-based and inclusive
- `start_line: 0, end_line: 0` inserts at the beginning
- `end_line: 0` inserts before `start_line`

Scratch session execution:

```text
```coplot-run
print(df.shape)
```
```

Shell execution:

```text
```coplot-shell
python -m pip install numpy pandas matplotlib
```
```

Shell execution is currently command-by-command, not a persistent PTY.

## Python Session / Venv Design

The server process is not the analysis Python environment. `server.py` launches `session_worker.py` using:

```text
.venv/bin/python -u coplot_web/session_worker.py --plots-dir plots
```

The worker:

- maintains a persistent Python globals namespace
- accepts JSON requests over stdin
- writes JSON responses over stdout
- captures stdout/stderr
- captures exceptions as stderr

After each Python execution, the server records new or modified PNG files in `plots/` as plot artifacts.

Shell commands run in project root with:

- `VIRTUAL_ENV=/path/to/.venv`
- `.venv/bin` prepended to `PATH`
- `PYTHONHOME` removed

This is why `python`, `python3`, and `pip` should resolve into the project venv.

## Artifact Display And Multimodal Details

Artifact ledger entries look like:

```json
{
  "id": 1,
  "type": "plot",
  "path": "plots/example.png",
  "created_at": "...",
  "source": "session",
  "code": "...",
  "caption": "example.png",
  "pinned": false
}
```

The browser displays artifacts with:

```text
/<artifact.path>?v=<created_at>
```

There was a bug where the backend treated the query string as part of the filename. `server.py` now uses `urllib.parse.urlparse(self.path).path` in `do_GET`.

For multimodal chat, `AgentService._user_content()` checks whether the user message mentions visual terms such as `plot`, `image`, `figure`, `chart`, `look at`, `inspect`, or `see it`. If yes, it attaches pinned plot artifacts plus the latest plot as OpenAI-compatible content parts:

```json
[
  {"type": "text", "text": "..."},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
]
```

This mirrors the old local prototype's `image_data_url` behavior.

## Known Rough Edges / Next Polish

Likely next-session work:

- UI polish: denser, cleaner pane layout; better typography; fewer raw JSON/code-block artifacts in chat.
- Show applied agent actions as explicit UI events, not only raw fenced blocks.
- Add action previews/confirmations for broad edits and risky shell commands.
- Streaming chat responses.
- Real PTY shell with xterm.js instead of command-by-command shell execution.
- Better transcript rendering and filtering.
- Show venv status in the UI.
- Better model settings validation and a “test connection” button.
- Better artifact controls: selected artifact attachment, clearer pin state, open/download buttons.
- Add cache busting for frontend JS/CSS or disable static caching during development.
- Add tests for `coplot-edit`, `coplot-run`, `coplot-shell`, artifact serving with query strings, venv shell path, and multimodal image attachment.

Potential core bugfix areas:

- Worker lifecycle around long-running code and interrupts is primitive.
- Shell timeout is fixed at 120 seconds.
- Package installs can exceed 120 seconds on slow networks.
- `Clear All` recreates `.venv`, which can take several seconds and currently has no progress UI.
- Only PNG files saved in `plots/` are registered for display and multimodal inspection.
- Chat calls are non-streaming and can block the request thread.
- The current multimodal heuristic is simple keyword matching.

## Current Mental Model For Future Work

Preserve these boundaries:

- Durable editor changes should be explicit and reproducible.
- Session runs are ad hoc and stateful.
- Shell commands mutate environment/files and should remain visibly separate.
- The model context must label durable code, chat, transcript, shell output, session output, environment, and artifacts separately.
- The analysis project owns `.venv`; coplot runtime should not install analysis packages into the server environment.

For bugfixing, start by reading `server.py` around:

- `ProjectState`
- `PythonSession`
- `ShellSession`
- `AgentService`
- `Handler.do_GET`
- `Handler.clear_session`

For UX iteration, start with:

- `static/index.html`
- `static/app.js`
- `static/styles.css`

## UX Iteration Handoff - 2026-04-26

This session focused on visual/interaction polish while preserving the dependency-light vanilla frontend.

Files changed:

- `coplot_web/server.py`
- `coplot_web/static/index.html`
- `coplot_web/static/app.js`
- `coplot_web/static/styles.css`

Validation run:

```bash
python3 -m py_compile coplot_web/server.py
node --check coplot_web/static/app.js
curl -s http://127.0.0.1:8765/
curl -s http://127.0.0.1:8765/api/state
```

The server was left running during the session with:

```bash
python3 coplot_web/server.py
```

at:

```text
http://127.0.0.1:8765
```

### Interaction Decisions

- `Cmd/Ctrl+Enter` runs selected code from the durable editor only.
- If no editor code is selected, `Cmd/Ctrl+Enter` does nothing.
- After an LLM `coplot-edit` block is applied, the newly inserted/replaced editor text is selected.
- `Cmd/Ctrl+T` attempts to focus the terminal input, but note that browser `Cmd+T` may be reserved for opening a new tab on macOS.
- Chat input:
  - `Enter` sends.
  - `Shift+Enter` inserts a newline.
  - The visible send button was removed.
- Terminal/session input:
  - `Enter` runs.
  - `Shift+Enter` inserts a newline.
  - The visible run button was removed.
- Clicking a plot opens a fullscreen dialog.
- Artifact pane defaults to the latest plot.
- If the user browses previous/next artifacts, that browsing position is respected until a new plot is created.
- When a new plot is created, the artifact pane jumps to the newest plot.

### Backend Selection Metadata

`server.py` now includes helper logic to compute character offsets for applied line edits:

- `offset_for_line(...)`
- `selection_for_line_edits(...)`

When `_run_actions()` applies a `coplot-edit`, the returned action includes:

```json
{"type": "edit_file", "status": "applied", "edits": [...], "selection": {"start": 0, "end": 12}}
```

The frontend uses this selection metadata after chat refresh to focus the editor and select the newly applied code.

### Visual Direction

Current visual direction:

- Dark, high-contrast, square-edged interface.
- No rounded corners globally; `styles.css` has a final `* { border-radius: 0 !important; }` override.
- Pane/title accents use color directly, not glow.
- Global accent is electric cyan: `--accent: #00e5ff`.
- Python/session color is bright turquoise: `--session: #00f5ff`.
- Shell color is warmer hot pink/red: `--shell: #ff3f72`.
- Terminal pane title is simply `Python` or `Shell`, changing with the selected mode.
- Buttons are icon-only, monochrome, accent-colored.
- Many old borders were replaced with subtle background changes or spacing.

The UI is intentionally experimental right now. The main design philosophy that emerged:

- Use spacing and hue instead of borders and nested cards.
- Keep panes clean and high contrast.
- Avoid busy layered containers, especially in the terminal transcript.
- Use content hue to communicate mode:
  - Python input/output: turquoise family.
  - Shell input/output: hot pink/red family.
  - Output is dimmer than its input, but same hue family.

### Chat Rendering

Chat messages now:

- have no borders;
- use subtle shaded backgrounds;
- right-align user messages;
- hide timestamps by default;
- show hover-only `HH:MM` timestamps in the opposite upper corner from message justification;
- replace legacy raw action blocks with small action notes:
  - `coplot-edit` -> `Applied editor update`
  - `coplot-run` -> `Ran session scratch code`
  - `coplot-shell` -> `Ran shell command`

The chat renderer is still a simple `formatMarkdownLite()` function, not a full Markdown implementation.

### Editor Highlighting

The durable editor remains a `textarea`, but it now has a highlighted `<pre>` mirror behind it:

```html
<div class="editor-shell">
  <pre id="source-highlight" aria-hidden="true"></pre>
  <textarea id="source-editor" ...></textarea>
</div>
```

The highlighter is lightweight custom JavaScript, not CodeMirror/Monaco. It currently highlights:

- Python keywords: hot pink/red
- strings: bright turquoise
- numbers: pure saturated yellow `#ffff00`
- function calls: white
- comments: muted blue-gray italic

Important bug fixed this session: quoted strings were briefly rendered as integer placeholders with square glyphs because string placeholders included digits and got number-highlighted before restoration. The placeholder system now uses all-letter keys via `placeholderKey()` / `placeholderIndex()`.

Known editor caveat: this is still not a real parser. It is good enough for visual MVP but will have edge cases. A real editor like CodeMirror or Monaco is still a future upgrade.

### Terminal Transcript

The transcript was flattened heavily:

- no dark gray card containers;
- no alternating transcript background blocks;
- larger vertical spacing between separate records;
- record title separated from its content;
- input brighter/heavier than output;
- output dimmer but in same hue family;
- command input has a pitch black background;
- terminal pane and transcript area are pitch black.

If the terminal starts looking layered again, check for older `.message, .entry` rules in `styles.css`; the transcript-specific selectors later in the file are intended to override them.

### Artifact Display

The artifact pane changed in several ways:

- Plot display background is pitch black.
- Plot drop shadow was removed.
- Plot preview in the artifact pane uses CSS-only dark adaptation:

```css
.artifact-view img {
  filter: invert(1) hue-rotate(180deg);
}
```

- Fullscreen plot preview disables that filter, so it shows original plot colors:

```css
.fullscreen-dialog img {
  filter: none;
}
```

- No temporary PNGs are written for dark-mode plots.
- Artifact metadata is no longer shown as raw JSON under the plot.
- Metadata now appears on hover as a terminal-style monospace overlay on an alpha black background.

The metadata is still useful because it identifies the current artifact id, path, source, caption, created time, and pinned state.

### Layout Height Fix

There was a page-level scroll issue where the app could scroll down slightly, cutting off the top `coplot` title and revealing the gradient background below. The likely cause was old `100vh` arithmetic plus grid gap:

```css
grid-template-rows: calc((100vh - 42px) / 2) calc((100vh - 42px) / 2);
height: calc(100vh - 42px);
gap: 1px;
```

The fix changed the app to a fixed-height flex shell:

- `html, body` are `height: 100%` and `overflow: hidden`.
- `body` is a vertical flex container.
- `.topbar` is `flex: 0 0 42px`.
- `.workspace` flexes to fill the remaining space.
- workspace rows are `minmax(0, 1fr) minmax(0, 1fr)` instead of `100vh` calculations.

### Browser Cache Note

Static files are currently served without explicit cache busting. During UX work, if the browser appears to ignore changes, hard refresh:

```text
Cmd+Shift+R / Ctrl+Shift+R
```

Adding cache busting or disabling static caching during development is still a good next polish task.

### Suggested Next UX Work

- Replace the lightweight textarea/highlight mirror with CodeMirror or Monaco for robust editing, selection, syntax, and keybinding support.
- Add a small visual cue or tooltip for the keyboard shortcuts.
- Add a better icon set instead of text glyph icons if the project accepts a dependency or local SVG/icon assets.
- Continue flattening and auditing old CSS rules; the stylesheet now contains old base rules plus later overrides.
- Consider separating CSS into reset/base/layout/components/theme sections to avoid override drift.
- Add visual tests or Playwright screenshots once the UI direction stabilizes.
