const state = {
  data: null,
  mode: "session",
  artifactIndex: 0,
  viewedArtifactCount: 0,
  artifactNavigationPinned: false,
  pendingEditorSelection: null,
};

const $ = (selector) => document.querySelector(selector);

function setStatus(text) {
  $("#status").textContent = text;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || response.statusText);
  }
  return payload;
}

async function refresh() {
  state.data = await api("/api/state");
  render();
}

function render(options = {}) {
  const data = state.data;
  $("#project-root").textContent = data.project.root;
  $("#source-name").textContent = data.project.source_file;
  if (options.forceSource || document.activeElement !== $("#source-editor")) {
    setEditorValue(data.source);
  }
  renderChat(data.chat);
  renderTranscript(data.transcript);
  renderArtifacts(data.artifacts);
  renderModelSettings(data.model_settings);
  setStatus("Ready");
  applyPendingEditorSelection();
}

function renderChat(entries) {
  const log = $("#chat-log");
  log.innerHTML = "";
  if (!entries.length) {
    log.innerHTML = '<div class="empty">No chat yet.</div>';
    return;
  }
  for (const entry of entries) {
    const item = document.createElement("div");
    item.className = `message ${entry.role}`;
    item.dataset.time = formatTime(entry.created_at);
    item.innerHTML = `
      <div class="label">${escapeHtml(entry.role)}</div>
      <div class="content">${formatMarkdownLite(entry.content)}</div>
    `;
    log.appendChild(item);
  }
  log.scrollTop = log.scrollHeight;
}

function renderTranscript(entries) {
  const transcript = $("#transcript");
  transcript.innerHTML = "";
  if (!entries.length) {
    transcript.innerHTML = '<div class="empty">Run durable code, session snippets, or shell commands to create transcript entries.</div>';
    return;
  }
  for (const entry of entries) {
    const item = document.createElement("div");
    item.className = "entry";
    const kind = entry.kind || "session";
    const source = entry.source || "unknown";
    const input = entry.input || entry.code || "";
    const status = entry.ok ? "ok" : "fail";
    const outputs = [entry.stdout, entry.stderr].filter(Boolean).join("\n");
    const languageClass = kind === "shell" ? "shell-entry" : "session-entry";
    item.innerHTML = `
      <div class="label">${escapeHtml(kind)} · ${escapeHtml(source)} · <span class="${status}">${entry.ok ? "ok" : "failed"}</span> · ${entry.duration_ms || 0}ms</div>
      <pre class="transcript-input ${languageClass}">${escapeHtml(input)}</pre>
      ${outputs ? `<pre class="transcript-output ${languageClass}">${escapeHtml(outputs)}</pre>` : ""}
    `;
    transcript.appendChild(item);
  }
  transcript.scrollTop = transcript.scrollHeight;
}

function renderArtifacts(artifacts) {
  const plots = artifacts.filter((artifact) => artifact.type === "plot");
  $("#artifact-count").textContent = `${plots.length} plot${plots.length === 1 ? "" : "s"}`;
  if (!plots.length) {
    $("#artifact-view").innerHTML = '<div class="empty">No plot artifacts yet.</div>';
    $("#pin-artifact").disabled = true;
    state.viewedArtifactCount = 0;
    state.artifactNavigationPinned = false;
    return;
  }
  if (!state.artifactNavigationPinned || plots.length > state.viewedArtifactCount) {
    state.artifactIndex = plots.length - 1;
    state.artifactNavigationPinned = false;
  }
  state.viewedArtifactCount = plots.length;
  state.artifactIndex = Math.max(0, Math.min(state.artifactIndex, plots.length - 1));
  const artifact = plots[state.artifactIndex];
  $("#artifact-view").innerHTML = `
    <img alt="${escapeHtml(artifact.caption)}" src="/${encodeURI(artifact.path)}?v=${encodeURIComponent(artifact.created_at)}" />
    <div class="artifact-meta-overlay">${formatArtifactMeta(artifact)}</div>
  `;
  $("#artifact-view img").addEventListener("click", () => openArtifactFullscreen(artifact));
  $("#pin-artifact").disabled = false;
  $("#pin-artifact").textContent = artifact.pinned ? "◇" : "◆";
  $("#pin-artifact").title = artifact.pinned ? "Unpin artifact" : "Pin artifact";
  $("#pin-artifact").setAttribute("aria-label", artifact.pinned ? "Unpin artifact" : "Pin artifact");
}

function currentPlot() {
  const plots = state.data.artifacts.filter((artifact) => artifact.type === "plot");
  return plots[state.artifactIndex];
}

function renderModelSettings(settings) {
  $("#setting-endpoint-url").value = settings.endpoint_url || "http://localhost:11434";
  $("#setting-model").value = settings.model || "qwen3:8b";
  $("#setting-max-tokens").value = settings.max_tokens || 8192;
  $("#setting-temperature").value = settings.temperature ?? 0.2;
  $("#setting-timeout").value = settings.timeout_seconds || 1800;
  $("#setting-reasoning-enabled").checked = Boolean(settings.reasoning_enabled);
  $("#setting-reasoning-effort").value = settings.reasoning_effort || "medium";
}

function readModelSettingsForm() {
  return {
    endpoint_url: $("#setting-endpoint-url").value.trim(),
    model: $("#setting-model").value.trim(),
    max_tokens: Number($("#setting-max-tokens").value),
    temperature: Number($("#setting-temperature").value),
    timeout_seconds: Number($("#setting-timeout").value),
    reasoning_enabled: $("#setting-reasoning-enabled").checked,
    reasoning_effort: $("#setting-reasoning-effort").value,
  };
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatArtifactMeta(artifact) {
  const fields = [
    ["id", artifact.id],
    ["caption", artifact.caption],
    ["path", artifact.path],
    ["source", artifact.source],
    ["created", formatTime(artifact.created_at)],
    ["pinned", artifact.pinned ? "yes" : "no"],
  ];
  return fields
    .map(([key, value]) => `<div><span>${escapeHtml(key)}</span>${escapeHtml(value ?? "")}</div>`)
    .join("");
}

function formatMarkdownLite(value) {
  let html = escapeHtml(value);
  html = html.replace(/```coplot-edit[\s\S]*?```/gi, '<div class="action-note">Applied editor update</div>');
  html = html.replace(/```coplot-run[\s\S]*?```/gi, '<div class="action-note">Ran session scratch code</div>');
  html = html.replace(/```coplot-shell[\s\S]*?```/gi, '<div class="action-note">Ran shell command</div>');
  html = html.replace(/```([\s\S]*?)```/g, "<pre class=\"output\">$1</pre>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\n/g, "<br>");
  return html;
}

function setEditorValue(value) {
  $("#source-editor").value = value;
  updateSourceHighlight();
}

function applyPendingEditorSelection() {
  if (!state.pendingEditorSelection) return;
  const { start, end } = state.pendingEditorSelection;
  const editor = $("#source-editor");
  editor.focus();
  editor.setSelectionRange(start, end);
  state.pendingEditorSelection = null;
}

function findEditorSelection(actions = []) {
  for (const action of actions) {
    if (action.type === "edit_file" && action.status === "applied" && action.selection) {
      return action.selection;
    }
  }
  return null;
}

async function postAndRefresh(path, body) {
  setStatus("Working");
  const payload = await api(path, { method: "POST", body: JSON.stringify(body) });
  state.data = payload.state || state.data;
  const actions = payload.result?.actions || [];
  state.pendingEditorSelection = findEditorSelection(actions);
  render({ forceSource: Boolean(state.pendingEditorSelection) });
  return payload;
}

function selectedEditorCode() {
  const editor = $("#source-editor");
  if (editor.selectionStart === editor.selectionEnd) return "";
  return editor.value.slice(editor.selectionStart, editor.selectionEnd);
}

async function runSelectedEditorCode() {
  const code = selectedEditorCode();
  if (!code.trim()) return;
  await postAndRefresh("/api/run-session", { code, source: "user_executed" });
}

function updateSourceHighlight() {
  $("#source-highlight").innerHTML = highlightPython($("#source-editor").value);
}

function highlightPython(source) {
  const lines = source.split("\n").map((line) => {
    const commentIndex = line.indexOf("#");
    const code = commentIndex >= 0 ? line.slice(0, commentIndex) : line;
    const comment = commentIndex >= 0 ? line.slice(commentIndex) : "";
    return `${highlightPythonCode(code)}${comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : ""}`;
  });
  return `${lines.join("\n")}\n`;
}

function highlightPythonCode(source) {
  const strings = [];
  let html = source.replace(/("""[\s\S]*?"""|'''[\s\S]*?'''|"[^"\n]*(?:\\.[^"\n]*)*"|'[^'\n]*(?:\\.[^'\n]*)*')/g, (match) => {
    const index = strings.push(match) - 1;
    return `___DSSTR${placeholderKey(index)}___`;
  });
  html = escapeHtml(html);
  html = html.replace(/\b(False|None|True|and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield)\b/g, '<span class="tok-keyword">$1</span>');
  html = html.replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="tok-number">$1</span>');
  html = html.replace(/\b([A-Za-z_][A-Za-z0-9_]*)\s*(?=\()/g, '<span class="tok-call">$1</span>');
  html = html.replace(/___DSSTR([A-Z]+)___/g, (_, key) => `<span class="tok-string">${escapeHtml(strings[placeholderIndex(key)])}</span>`);
  return html;
}

function placeholderKey(index) {
  let value = index;
  let key = "";
  do {
    key = String.fromCharCode(65 + (value % 26)) + key;
    value = Math.floor(value / 26) - 1;
  } while (value >= 0);
  return key;
}

function placeholderIndex(key) {
  let value = 0;
  for (const character of key) {
    value = value * 26 + character.charCodeAt(0) - 64;
  }
  return value - 1;
}

function openArtifactFullscreen(artifact) {
  const image = $("#fullscreen-artifact-image");
  image.src = `/${encodeURI(artifact.path)}?v=${encodeURIComponent(artifact.created_at)}`;
  image.alt = artifact.caption || "Plot artifact";
  $("#artifact-fullscreen").showModal();
}

$("#save-source").addEventListener("click", async () => {
  await postAndRefresh("/api/save", { source: $("#source-editor").value });
});

$("#run-file").addEventListener("click", async () => {
  await postAndRefresh("/api/run-file", { source: $("#source-editor").value });
});

$("#mode-session").addEventListener("click", () => {
  state.mode = "session";
  $("#mode-session").classList.add("active");
  $("#mode-shell").classList.remove("active");
  $("#session-title").textContent = "Python";
  $("#session-mode-label").textContent = "";
  $("#command-input").placeholder = "print(df.head())";
  $("#command-form").classList.add("session-command");
  $("#command-form").classList.remove("shell-command");
  $(".session-pane").classList.add("python-mode");
  $(".session-pane").classList.remove("shell-mode");
});

$("#mode-shell").addEventListener("click", () => {
  state.mode = "shell";
  $("#mode-shell").classList.add("active");
  $("#mode-session").classList.remove("active");
  $("#session-title").textContent = "Shell";
  $("#session-mode-label").textContent = "";
  $("#command-input").placeholder = "python3 -m pip install pandas";
  $("#command-form").classList.add("shell-command");
  $("#command-form").classList.remove("session-command");
  $(".session-pane").classList.add("shell-mode");
  $(".session-pane").classList.remove("python-mode");
});

$("#clear-session").addEventListener("click", async () => {
  await postAndRefresh("/api/clear-session", {});
});

$("#command-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#command-input").value.trim();
  if (!input) return;
  const path = state.mode === "session" ? "/api/run-session" : "/api/run-shell";
  const body = state.mode === "session" ? { code: input } : { command: input };
  await postAndRefresh(path, body);
  $("#command-input").value = "";
});

$("#command-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#command-form").requestSubmit();
  }
});

$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#chat-input").value.trim();
  if (!input) return;
  await postAndRefresh("/api/chat", { message: input });
  $("#chat-input").value = "";
});

$("#source-editor").addEventListener("input", updateSourceHighlight);
$("#source-editor").addEventListener("scroll", () => {
  const editor = $("#source-editor");
  const highlight = $("#source-highlight");
  highlight.scrollTop = editor.scrollTop;
  highlight.scrollLeft = editor.scrollLeft;
});

$("#chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#chat-form").requestSubmit();
  }
});

document.addEventListener("keydown", async (event) => {
  const commandOrControl = event.metaKey || event.ctrlKey;
  if (!commandOrControl) return;

  if (event.key === "Enter") {
    event.preventDefault();
    await runSelectedEditorCode();
  }

  if (event.key.toLowerCase() === "t") {
    event.preventDefault();
    $("#command-input").focus();
  }
});

$("#open-model-settings").addEventListener("click", () => {
  renderModelSettings(state.data.model_settings);
  $("#model-settings-dialog").showModal();
});

$("#close-model-settings").addEventListener("click", () => {
  $("#model-settings-dialog").close();
});

$("#model-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await postAndRefresh("/api/model-settings", readModelSettingsForm());
  $("#model-settings-dialog").close();
});

$("#prev-artifact").addEventListener("click", () => {
  state.artifactIndex -= 1;
  state.artifactNavigationPinned = true;
  renderArtifacts(state.data.artifacts);
});

$("#next-artifact").addEventListener("click", () => {
  state.artifactIndex += 1;
  state.artifactNavigationPinned = true;
  renderArtifacts(state.data.artifacts);
});

$("#pin-artifact").addEventListener("click", async () => {
  const artifact = currentPlot();
  if (!artifact) return;
  await postAndRefresh("/api/artifact-pin", { id: artifact.id, pinned: !artifact.pinned });
});

$("#refresh-context").addEventListener("click", async () => {
  const payload = await api("/api/context");
  $("#context-payload").textContent = JSON.stringify(payload, null, 2);
  $("#context-dialog").showModal();
});

$("#close-context").addEventListener("click", () => {
  $("#context-dialog").close();
});

$("#close-artifact-fullscreen").addEventListener("click", () => {
  $("#artifact-fullscreen").close();
});

$("#artifact-fullscreen").addEventListener("click", (event) => {
  if (event.target === $("#artifact-fullscreen")) {
    $("#artifact-fullscreen").close();
  }
});

refresh().catch((error) => {
  setStatus(error.message);
});

$("#command-form").classList.add("session-command");
$(".session-pane").classList.add("python-mode");
$("#session-title").textContent = "Python";
$("#session-mode-label").textContent = "";
