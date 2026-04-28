const state = {
  data: null,
  mode: "session",
  pendingEditorSelection: null,
  chatPending: false,
  pendingChatMessage: "",
  terminalPending: null,
  fullscreenArtifactIndex: null,
};

const $ = (selector) => document.querySelector(selector);

function setStatus(text) {
  $("#status").textContent = text;
}

function syncPendingControls() {
  $("#chat-input").disabled = state.chatPending;
  $("#command-input").disabled = Boolean(state.terminalPending);
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
  syncPendingControls();
  setStatus("Ready");
  applyPendingEditorSelection();
}

function renderChat(entries) {
  const log = $("#chat-log");
  log.innerHTML = "";
  if (!entries.length && !state.chatPending && !state.pendingChatMessage) {
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
  if (state.pendingChatMessage) {
    log.appendChild(renderPendingUserMessage(state.pendingChatMessage));
  }
  if (state.chatPending) {
    log.appendChild(renderTypingMessage());
  }
  log.scrollTop = log.scrollHeight;
}

function renderPendingUserMessage(content) {
  const item = document.createElement("div");
  item.className = "message user pending-user-message";
  item.innerHTML = `
    <div class="label">user</div>
    <div class="content">${formatMarkdownLite(content)}</div>
  `;
  return item;
}

function renderTypingMessage() {
  const item = document.createElement("div");
  item.className = "message assistant pending-message";
  item.innerHTML = `
    <div class="label">assistant</div>
    <div class="typing-dots" aria-label="Assistant is thinking">
      <span></span><span></span><span></span>
    </div>
  `;
  return item;
}

function renderTranscript(entries) {
  const transcript = $("#transcript");
  transcript.innerHTML = "";
  if (!entries.length && !state.terminalPending) {
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
  if (state.terminalPending) {
    transcript.appendChild(renderPendingTranscriptEntry(state.terminalPending));
  }
  transcript.scrollTop = transcript.scrollHeight;
}

function renderPendingTranscriptEntry(pending) {
  const item = document.createElement("div");
  item.className = "entry pending-entry";
  const languageClass = pending.kind === "shell" ? "shell-entry" : "session-entry";
  item.innerHTML = `
    <div class="label">${escapeHtml(pending.kind)} · ${escapeHtml(pending.source)} · <span class="running">running</span></div>
    <pre class="transcript-input ${languageClass}">${escapeHtml(pending.input)}</pre>
    <div class="running-dots" aria-label="Command is running"><span></span><span></span><span></span></div>
  `;
  return item;
}

function renderArtifacts(artifacts) {
  const plots = sortedPlotArtifacts(artifacts);
  $("#artifact-count").textContent = `${plots.length} plot${plots.length === 1 ? "" : "s"}`;
  if (!plots.length) {
    $("#artifact-view").innerHTML = '<div class="empty">No plot artifacts yet.</div>';
    return;
  }
  const artifact = plots[plots.length - 1];
  $("#artifact-view").innerHTML = `
    <img alt="${escapeHtml(artifact.caption)}" src="/${encodeURI(artifact.path)}?v=${encodeURIComponent(artifact.created_at)}" />
    <div class="artifact-meta-overlay">${formatArtifactMeta(artifact)}</div>
  `;
  $("#artifact-view img").addEventListener("click", () => openArtifactFullscreen(plots.length - 1));
}

function sortedPlotArtifacts(artifacts) {
  return artifacts
    .filter((artifact) => artifact.type === "plot")
    .slice()
    .sort((left, right) => {
      const leftTime = Date.parse(left.created_at || "") || 0;
      const rightTime = Date.parse(right.created_at || "") || 0;
      if (leftTime !== rightTime) return leftTime - rightTime;
      return String(left.path || "").localeCompare(String(right.path || ""));
    });
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

function renderPendingState() {
  if (!state.data) return;
  syncPendingControls();
  renderChat(state.data.chat);
  renderTranscript(state.data.transcript);
}

async function postAndRefresh(path, body, options = {}) {
  setStatus("Working");
  state.chatPending = Boolean(options.chatPending);
  state.pendingChatMessage = options.pendingChatMessage || "";
  state.terminalPending = options.terminalPending || null;
  renderPendingState();
  try {
    const payload = await api(path, { method: "POST", body: JSON.stringify(body) });
    state.data = payload.state || state.data;
    const actions = payload.result?.actions || [];
    state.pendingEditorSelection = findEditorSelection(actions);
    state.chatPending = false;
    state.pendingChatMessage = "";
    state.terminalPending = null;
    syncPendingControls();
    render({ forceSource: Boolean(state.pendingEditorSelection) });
    return payload;
  } catch (error) {
    state.chatPending = false;
    state.pendingChatMessage = "";
    state.terminalPending = null;
    syncPendingControls();
    renderPendingState();
    setStatus(error.message);
    throw error;
  }
}

function selectedEditorCode() {
  const editor = $("#source-editor");
  if (editor.selectionStart === editor.selectionEnd) return "";
  return editor.value.slice(editor.selectionStart, editor.selectionEnd);
}

async function runSelectedEditorCode() {
  const code = selectedEditorCode();
  if (!code.trim()) return;
  await postAndRefresh(
    "/api/run-session",
    { code, source: "user_executed" },
    { terminalPending: { kind: "session", source: "user_executed", input: code } },
  );
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

function fullscreenPlots() {
  return state.data ? sortedPlotArtifacts(state.data.artifacts) : [];
}

function showFullscreenArtifact(index) {
  const plots = fullscreenPlots();
  if (!plots.length) return;
  state.fullscreenArtifactIndex = (index + plots.length) % plots.length;
  const artifact = plots[state.fullscreenArtifactIndex];
  const image = $("#fullscreen-artifact-image");
  image.src = `/${encodeURI(artifact.path)}?v=${encodeURIComponent(artifact.created_at)}`;
  image.alt = artifact.caption || "Plot artifact";
}

function openArtifactFullscreen(index) {
  showFullscreenArtifact(index);
  $("#artifact-fullscreen").showModal();
}

$("#save-source").addEventListener("click", async () => {
  await postAndRefresh("/api/save", { source: $("#source-editor").value });
});

$("#run-file").addEventListener("click", async () => {
  const source = $("#source-editor").value;
  await postAndRefresh(
    "/api/run-file",
    { source },
    { terminalPending: { kind: "session", source: "durable_script", input: source } },
  );
});

$("#mode-session").addEventListener("click", () => {
  state.mode = "session";
  $("#mode-session").classList.add("active");
  $("#mode-shell").classList.remove("active");
  $("#session-title").textContent = "Python session";
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
  $("#session-title").textContent = "Shell command";
  $("#command-input").placeholder = "python3 -m pip install pandas";
  $("#command-form").classList.add("shell-command");
  $("#command-form").classList.remove("session-command");
  $(".session-pane").classList.add("shell-mode");
  $(".session-pane").classList.remove("python-mode");
});

$("#clear-session").addEventListener("click", async () => {
  await postAndRefresh("/api/clear-session", {});
});

$("#download-session").addEventListener("click", () => {
  window.location.href = "/api/download-session";
});

$("#command-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#command-input").value.trim();
  if (!input) return;
  const path = state.mode === "session" ? "/api/run-session" : "/api/run-shell";
  const body = state.mode === "session" ? { code: input } : { command: input };
  const pending = {
    kind: state.mode === "session" ? "session" : "shell",
    source: state.mode === "session" ? "user_executed" : "user_shell",
    input,
  };
  await postAndRefresh(path, body, { terminalPending: pending });
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
  $("#chat-input").value = "";
  await postAndRefresh("/api/chat", { message: input }, { chatPending: true, pendingChatMessage: input });
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
  if ($("#artifact-fullscreen").open && event.key === "ArrowLeft") {
    event.preventDefault();
    showFullscreenArtifact((state.fullscreenArtifactIndex ?? 0) - 1);
    return;
  }

  if ($("#artifact-fullscreen").open && event.key === "ArrowRight") {
    event.preventDefault();
    showFullscreenArtifact((state.fullscreenArtifactIndex ?? 0) + 1);
    return;
  }

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

$("#refresh-context").addEventListener("click", async () => {
  const payload = await api("/api/context");
  $("#context-payload").textContent = JSON.stringify(payload, null, 2);
  $("#context-dialog").showModal();
});

$("#close-context").addEventListener("click", () => {
  $("#context-dialog").close();
});

$("#artifact-fullscreen").addEventListener("click", (event) => {
  $("#artifact-fullscreen").close();
  state.fullscreenArtifactIndex = null;
});

refresh().catch((error) => {
  setStatus(error.message);
});

$("#command-form").classList.add("session-command");
$(".session-pane").classList.add("python-mode");
$("#session-title").textContent = "Python session";
