const state = {
  data: null,
  mode: "session",
  pendingEditorSelection: null,
  chatPending: false,
  pendingChatMessage: "",
  chatImages: [],
  chatAudio: [],
  pendingChatImages: [],
  pendingChatAudio: [],
  terminalPending: null,
  fullscreenArtifactIndex: null,
  settingsDialogShown: false,
  settingsFirstRun: false,
  editorDirty: false,
  lastServerSource: "",
  lastSourceMtimeNs: "0",
  endpointModels: [],
  pendingPollTimer: null,
  pendingChatBaselineLength: 0,
  audioRecorder: null,
  audioChunks: [],
  audioStream: null,
  audioRecordingStartedAt: 0,
  audioRecording: false,
};

const $ = (selector) => document.querySelector(selector);
const PENDING_POLL_INTERVAL_MS = 5000;
const AUTO_SCROLL_THRESHOLD_PX = 32;

function setStatus(text) {
  $("#status").textContent = text;
}

function syncPendingControls() {
  const workPending = state.chatPending || Boolean(state.terminalPending);
  const recordButton = $("#record-audio");
  $("#chat-input").disabled = state.chatPending;
  recordButton.disabled = false;
  recordButton.classList.toggle("recording", state.audioRecording);
  recordButton.classList.toggle("stopping", workPending);
  recordButton.title = workPending
    ? "Stop after the current action"
    : state.audioRecording ? "Stop recording" : "Record voice message";
  recordButton.setAttribute("aria-label", recordButton.title);
  $("#command-input").disabled = Boolean(state.terminalPending);
  $("#clear-transcript").disabled = state.chatPending || Boolean(state.terminalPending);
  $("#compact-context").disabled = state.chatPending || Boolean(state.terminalPending);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || response.statusText);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function refresh() {
  state.data = await api("/api/state");
  render();
}

function render(options = {}) {
  const data = state.data;
  updateLanguageTheme();
  $("#project-root").textContent = data.project.root;
  $("#source-name").textContent = data.project.source_file;
  updateRuntimeLabels();
  renderContextMeter(data.context_usage);
  const sourceMtimeNs = String(data.source_mtime_ns || "0");
  const staleSource = compareSourceMtime(sourceMtimeNs, state.lastSourceMtimeNs) < 0;
  if (!staleSource && (options.forceSource || (!state.editorDirty && document.activeElement !== $("#source-editor")))) {
    setEditorValue(data.source);
    state.lastServerSource = data.source;
    state.lastSourceMtimeNs = sourceMtimeNs;
    if (options.forceSource) state.editorDirty = false;
  }
  renderChat(data.chat);
  renderChatAttachments();
  renderTranscript(data.transcript, data.active_jobs || []);
  renderArtifacts(data.artifacts);
  renderModelSettings(data.model_settings);
  syncPendingControls();
  setStatus(isWorkPending() ? "Working" : "Ready");
  applyPendingEditorSelection();
  maybeOpenFirstRunSettings();
}

function renderContextMeter(usage) {
  const element = $("#context-meter");
  if (!usage) {
    element.textContent = "context --";
    return;
  }
  const estimated = Math.round((usage.estimated_tokens || 0) / 100) / 10;
  const limit = Math.round((usage.limit_tokens || 0) / 1000);
  element.textContent = `context ~${estimated}k/${limit}k (${usage.percent || 0}%)`;
  const breakdown = usage.breakdown || {};
  element.title = [
    "Approximate context size sent with the next model request.",
    `workspace: ~${Math.round((breakdown.workspace || 0) / 100) / 10}k`,
    `summary: ~${Math.round((breakdown.session_summary || 0) / 100) / 10}k`,
    `events: ~${Math.round((breakdown.recent_events || 0) / 100) / 10}k`,
    `code: ~${Math.round((breakdown.durable_code || 0) / 100) / 10}k`,
    `artifact ledger: ~${Math.round((breakdown.artifact_ledger || 0) / 100) / 10}k`,
  ].join("\n");
}

function compareSourceMtime(left, right) {
  const leftValue = BigInt(String(left || "0"));
  const rightValue = BigInt(String(right || "0"));
  if (leftValue < rightValue) return -1;
  if (leftValue > rightValue) return 1;
  return 0;
}

function renderChat(entries) {
  const log = $("#chat-log");
  const shouldStickToBottom = shouldAutoScrollToBottom(log);
  const previousScrollTop = log.scrollTop;
  log.innerHTML = "";
  if (!entries.length && !state.chatPending && !state.pendingChatMessage && !state.pendingChatImages.length && !state.pendingChatAudio.length) {
    log.innerHTML = '<div class="empty">No chat yet.</div>';
    restoreScrollPosition(log, shouldStickToBottom, previousScrollTop);
    return;
  }
  for (const entry of entries) {
    const item = document.createElement("div");
    item.className = `message ${entry.role}`;
    item.dataset.time = formatTime(entry.created_at);
    const attachmentsHtml = renderAttachmentChips(entry.attachments || []);
    item.innerHTML = `
      <div class="label">${escapeHtml(entry.role)}</div>
      ${attachmentsHtml}
      <div class="content">${formatMarkdownLite(entry.content)}</div>
    `;
    log.appendChild(item);
  }
  if (shouldRenderPendingUserMessage(entries)) {
    log.appendChild(renderPendingUserMessage(state.pendingChatMessage));
  }
  if (state.chatPending) {
    log.appendChild(renderTypingMessage());
  }
  restoreScrollPosition(log, shouldStickToBottom, previousScrollTop);
}

function shouldAutoScrollToBottom(element) {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= AUTO_SCROLL_THRESHOLD_PX;
}

function restoreScrollPosition(element, shouldStickToBottom, previousScrollTop) {
  if (shouldStickToBottom) {
    element.scrollTop = element.scrollHeight;
    return;
  }
  element.scrollTop = previousScrollTop;
}

function shouldRenderPendingUserMessage(entries) {
  const pendingAttachmentCount = state.pendingChatImages.length + state.pendingChatAudio.length;
  if (!state.pendingChatMessage && !pendingAttachmentCount) return false;
  const newEntries = entries.slice(state.pendingChatBaselineLength);
  const serverHasPendingMessage = newEntries.some(
    (entry) => entry.role === "user" &&
      entry.content === state.pendingChatMessage &&
      (entry.attachments || []).length === pendingAttachmentCount
  );
  return !serverHasPendingMessage;
}

function renderPendingUserMessage(content) {
  const item = document.createElement("div");
  item.className = "message user pending-user-message";
  item.innerHTML = `
    <div class="label">user</div>
    ${renderAttachmentChips([...state.pendingChatAudio, ...state.pendingChatImages])}
    <div class="content">${formatMarkdownLite(content)}</div>
  `;
  return item;
}

function renderAttachmentChips(attachments) {
  const visibleAttachments = (attachments || []).filter((attachment) => ["audio", "image"].includes(attachment.type));
  if (!visibleAttachments.length) return "";
  return `
    <div class="message-attachments">
      ${visibleAttachments.map((attachment) => `
        <span class="attachment-chip" title="${escapeHtml(formatAttachmentTitle(attachment))}">
          <span aria-hidden="true">${attachment.type === "audio" ? "●" : "▧"}</span>
          <span>${attachment.type === "audio" ? "WAV" : "PNG"}</span>
          ${attachment.duration_ms ? `<span>${escapeHtml(formatDuration(attachment.duration_ms))}</span>` : ""}
          <span>${escapeHtml(formatBytes(attachment.size_bytes || 0))}</span>
        </span>
      `).join("")}
    </div>
  `;
}

function renderChatAttachments() {
  const tray = $("#chat-attachments");
  if (!state.chatImages.length && !state.chatAudio.length) {
    tray.hidden = true;
    tray.innerHTML = "";
    return;
  }
  tray.hidden = false;
  const audioHtml = state.chatAudio.map((audio, index) => `
    <span class="attachment-chip composer-attachment" title="${escapeHtml(formatAttachmentTitle(audio))}">
      <span aria-hidden="true">●</span>
      <span>WAV</span>
      <span>${escapeHtml(formatDuration(audio.duration_ms))}</span>
      <span>${escapeHtml(formatBytes(audio.size_bytes))}</span>
      <button type="button" data-remove-audio="${index}" title="Remove audio" aria-label="Remove audio">×</button>
    </span>
  `).join("");
  const imageHtml = state.chatImages.map((image, index) => `
    <span class="attachment-chip composer-attachment" title="${escapeHtml(image.name || "Pasted PNG")}">
      <span aria-hidden="true">▧</span>
      <span>PNG</span>
      <span>${escapeHtml(formatBytes(image.size_bytes))}</span>
      <button type="button" data-remove-image="${index}" title="Remove image" aria-label="Remove image">×</button>
    </span>
  `).join("");
  tray.innerHTML = `${audioHtml}${imageHtml}`;
  tray.querySelectorAll("[data-remove-audio]").forEach((button) => {
    button.addEventListener("click", () => {
      state.chatAudio.splice(Number(button.dataset.removeAudio), 1);
      renderChatAttachments();
      $("#chat-input").focus();
    });
  });
  tray.querySelectorAll("[data-remove-image]").forEach((button) => {
    button.addEventListener("click", () => {
      state.chatImages.splice(Number(button.dataset.removeImage), 1);
      renderChatAttachments();
      $("#chat-input").focus();
    });
  });
}

function formatAttachmentTitle(attachment) {
  const parts = [attachment.mime_type || "image/png"];
  if (attachment.duration_ms) parts.push(formatDuration(attachment.duration_ms));
  parts.push(formatBytes(attachment.size_bytes || 0));
  return parts.join(" · ");
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  return `${Math.round(value / 102.4) / 10} KB`;
}

function formatDuration(durationMs) {
  const seconds = Math.max(0, Math.round(Number(durationMs || 0) / 1000));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
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

function renderTranscript(entries, activeJobs = []) {
  const transcript = $("#transcript");
  const shouldStickToBottom = shouldAutoScrollToBottom(transcript);
  const previousScrollTop = transcript.scrollTop;
  transcript.innerHTML = "";
  const localPending = pendingTranscriptEntries(activeJobs);
  if (!entries.length && !activeJobs.length && !localPending.length) {
    transcript.innerHTML = '<div class="empty">Run durable code, session snippets, or shell commands to create transcript entries.</div>';
    restoreScrollPosition(transcript, shouldStickToBottom, previousScrollTop);
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
  for (const job of activeJobs) {
    transcript.appendChild(renderPendingTranscriptEntry(activeJobToPending(job)));
  }
  for (const pending of localPending) {
    transcript.appendChild(renderPendingTranscriptEntry(pending));
  }
  restoreScrollPosition(transcript, shouldStickToBottom, previousScrollTop);
}

function pendingTranscriptEntries(activeJobs = []) {
  if (!state.terminalPending) return [];
  const alreadyVisible = activeJobs.some((job) => {
    return (
      String(job.kind || "") === String(state.terminalPending.kind || "") &&
      String(job.source || "") === String(state.terminalPending.source || "") &&
      String(job.input || "") === String(state.terminalPending.input || "")
    );
  });
  return alreadyVisible ? [] : [state.terminalPending];
}

function activeJobToPending(job) {
  return {
    kind: job.kind || "session",
    source: job.source || "unknown",
    input: job.input || "",
    started_at: job.started_at || "",
  };
}

function renderPendingTranscriptEntry(pending) {
  const item = document.createElement("div");
  item.className = "entry pending-entry";
  const languageClass = pending.kind === "shell" ? "shell-entry" : "session-entry";
  const started = pending.started_at ? ` · started ${formatTime(pending.started_at)}` : "";
  item.innerHTML = `
    <div class="label">${escapeHtml(pending.kind)} · ${escapeHtml(pending.source)} · <span class="running">running</span>${started}</div>
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
    <img alt="${escapeHtml(artifact.caption)}" title="Open plot" src="/${encodeURI(artifact.path)}?v=${encodeURIComponent(artifact.created_at)}" />
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
  populateModelOptions(settings.model || "qwen3.6");
  setLanguageSelection(settings.language || state.data?.project?.language || "r");
  $("#setting-max-tokens").value = settings.max_tokens || 16384;
  $("#setting-context-window").value = settings.context_window_tokens || 32768;
  $("#setting-temperature").value = settings.temperature ?? 0.2;
  $("#setting-timeout").value = settings.timeout_seconds || 600;
  $("#setting-reasoning-enabled").checked = Boolean(settings.reasoning_enabled);
  $("#setting-reasoning-control").value = settings.reasoning_control || "auto";
  setSupportedModalities(settings.supported_modalities || []);
}

function readModelSettingsForm() {
  return {
    endpoint_url: $("#setting-endpoint-url").value.trim(),
    model: $("#setting-model").value.trim(),
    language: selectedSettingsLanguage(),
    max_tokens: Number($("#setting-max-tokens").value),
    context_window_tokens: Number($("#setting-context-window").value),
    temperature: Number($("#setting-temperature").value),
    timeout_seconds: Number($("#setting-timeout").value),
    reasoning_enabled: $("#setting-reasoning-enabled").checked,
    reasoning_control: $("#setting-reasoning-control").value || "auto",
    supported_modalities: readSupportedModalities(),
  };
}

function populateModelOptions(selectedModel, models = null) {
  const select = $("#setting-model");
  if (models) state.endpointModels = models;
  const selected = selectedModel || select.value || "qwen3.6";
  const ids = models && models.length ? models.map((model) => model.id).filter(Boolean) : [selected];
  if (!ids.includes(selected)) ids.unshift(selected);
  select.innerHTML = "";
  for (const id of ids) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = id;
    select.appendChild(option);
  }
  select.value = ids.includes(selected) ? selected : ids[0] || "";
}

function selectedModelContextWindow(models, selectedModel) {
  const match = (models || []).find((model) => model.id === selectedModel);
  const value = Number(match?.context_window_tokens || 0);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function normalizeModalities(modalities) {
  return [...new Set(
    (modalities || []).map((item) => String(item).trim().toLowerCase()).filter(Boolean)
  )].sort();
}

function setSupportedModalities(modalities) {
  const normalized = normalizeModalities(modalities);
  $("#setting-supported-modalities").value = normalized.join(",");
  $("#setting-modalities-display").textContent = normalized.length ? normalized.join(", ") : "Unknown";
}

function readSupportedModalities() {
  return normalizeModalities($("#setting-supported-modalities").value.split(","));
}

function setSettingsMessage(message, isError = false) {
  const element = $("#settings-message");
  element.textContent = message;
  element.classList.toggle("error", isError);
}

function selectedSettingsLanguage() {
  return document.querySelector("input[name='language']:checked")?.value || "r";
}

function setLanguageSelection(language) {
  const normalized = language === "python" ? "python" : "r";
  const input = $(`#setting-language-${normalized}`);
  if (input) input.checked = true;
  updateLanguageSwitchSummary(normalized);
  updateLanguageTheme(normalized);
}

function setLanguageSwitchDisabled(disabled) {
  document.querySelectorAll("input[name='language']").forEach((input) => {
    input.disabled = disabled;
  });
  $("#language-switch-toggle").disabled = disabled;
}

function updateLanguageSwitchSummary(language = selectedSettingsLanguage()) {
  const summary = $("#language-switch-summary");
  if (!summary) return;
  summary.textContent = language === "python"
    ? "Python session, venv, coplot.py"
    : "R session, renv, coplot.R";
}

function updateLanguageTheme(language = currentLanguage()) {
  const normalized = language === "python" ? "python" : "r";
  document.body.classList.toggle("language-python", normalized === "python");
  document.body.classList.toggle("language-r", normalized === "r");
}

function openSettingsDialog({ firstRun = false } = {}) {
  state.settingsFirstRun = firstRun;
  state.settingsDialogShown = true;
  renderModelSettings(state.data.model_settings);
  $("#settings-title").textContent = firstRun ? "Set Up Workspace" : "Chat Settings";
  $("#proceed-model-settings").textContent = firstRun ? "Proceed" : "Apply";
  $("#proceed-model-settings").title = firstRun
    ? "Save these settings for this workspace and enter coplot"
    : "Save these settings for this workspace";
  $("#close-model-settings").hidden = firstRun;
  setLanguageSwitchDisabled(!firstRun);
  $("#language-switch-panel").classList.toggle("locked", !firstRun);
  $("#language-switch-panel").title = firstRun ? "" : "Workspace language is locked after setup.";
  setSettingsMessage(firstRun ? "Choose a language, connect to your local model server, choose a model, then proceed." : "");
  $("#model-settings-dialog").showModal();
}

function maybeOpenFirstRunSettings() {
  if (!state.data?.project?.first_run || $("#model-settings-dialog").open) return;
  openSettingsDialog({ firstRun: true });
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
  const blocks = [];
  const protectBlock = (markup) => {
    const token = `@@COPLOT_BLOCK_${blocks.length}@@`;
    blocks.push(markup);
    return token;
  };
  html = html.replace(/```coplot-edit[\s\S]*?```/gi, () => protectBlock('<div class="action-note">Applied editor update</div>'));
  html = html.replace(/```coplot-run[\s\S]*?```/gi, () => protectBlock('<div class="action-note">Ran session scratch code</div>'));
  html = html.replace(/```coplot-shell[\s\S]*?```/gi, () => protectBlock('<div class="action-note">Ran shell command</div>'));
  html = html.replace(/```([\s\S]*?)```/g, (_, code) => protectBlock(`<pre class="output">${code}</pre>`));
  html = formatMarkdownBlocks(html);
  return html.replace(/@@COPLOT_BLOCK_(\d+)@@/g, (_, index) => blocks[Number(index)] || "");
}

function formatMarkdownBlocks(html) {
  const lines = html.split("\n");
  const blocks = [];
  for (let index = 0; index < lines.length; index += 1) {
    if (isMarkdownTableStart(lines, index)) {
      const table = collectMarkdownTable(lines, index);
      blocks.push({ type: "table", html: renderMarkdownTable(table.rows) });
      index = table.endIndex;
      continue;
    }
    blocks.push(markdownLineBlock(lines[index]));
  }
  return renderMarkdownBlocks(blocks);
}

function markdownLineBlock(line) {
  if (!line.trim()) return { type: "blank", html: "" };
  const heading = line.match(/^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/);
  if (heading) {
    return { type: "heading", html: `<div class="chat-heading"><strong>${formatMarkdownInline(heading[1])}</strong></div>` };
  }
  return { type: "text", html: formatMarkdownInline(line) };
}

function renderMarkdownBlocks(blocks) {
  const html = [];
  let pendingBlank = false;
  let previousType = "";
  for (const block of blocks) {
    if (block.type === "blank") {
      pendingBlank = html.length > 0;
      continue;
    }
    const isText = block.type === "text";
    const isPreviousText = previousType === "text";
    const isPreviousInline = previousType === "text" || previousType === "inlineBreak";
    if (pendingBlank && isText && isPreviousInline) {
      html.push("<br><br>");
      previousType = "inlineBreak";
    } else if (!pendingBlank && isText && isPreviousText) {
      html.push("<br>");
    }
    html.push(block.html);
    previousType = block.type;
    pendingBlank = false;
  }
  return html.join("");
}

function formatMarkdownInline(html) {
  const inlineCode = [];
  html = html.replace(/`([^`]+)`/g, (_, code) => {
    const token = `@@COPLOT_INLINE_${inlineCode.length}@@`;
    inlineCode.push(`<code>${code}</code>`);
    return token;
  });
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return html.replace(/@@COPLOT_INLINE_(\d+)@@/g, (_, index) => inlineCode[Number(index)] || "");
}

function isMarkdownTableStart(lines, index) {
  return index + 1 < lines.length && isMarkdownTableRow(lines[index]) && isMarkdownTableSeparator(lines[index + 1]);
}

function isMarkdownTableRow(line) {
  const cells = markdownTableCells(line);
  return cells.length >= 2;
}

function isMarkdownTableSeparator(line) {
  const cells = markdownTableCells(line);
  return cells.length >= 2 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}

function collectMarkdownTable(lines, startIndex) {
  const rows = [markdownTableCells(lines[startIndex])];
  let index = startIndex + 2;
  while (index < lines.length && isMarkdownTableRow(lines[index])) {
    rows.push(markdownTableCells(lines[index]));
    index += 1;
  }
  return { rows, endIndex: index - 1 };
}

function markdownTableCells(line) {
  let trimmed = line.trim();
  if (!trimmed.includes("|")) return [];
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
  return trimmed.split("|").map((cell) => cell.trim());
}

function renderMarkdownTable(rows) {
  const header = rows[0] || [];
  const body = rows.slice(1);
  const head = header.map((cell) => `<th>${formatMarkdownInline(cell)}</th>`).join("");
  const bodyRows = body.map((row) => `<tr>${header.map((_, index) => `<td>${formatMarkdownInline(row[index] || "")}</td>`).join("")}</tr>`).join("");
  return `<div class="chat-table-wrap"><table class="chat-table"><thead><tr>${head}</tr></thead><tbody>${bodyRows}</tbody></table></div>`;
}

function setEditorValue(value) {
  $("#source-editor").value = value;
  updateSourceHighlight();
  updateCurrentLineHighlight();
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
  renderTranscript(state.data.transcript, state.data.active_jobs || []);
}

function isWorkPending() {
  return state.chatPending || Boolean(state.terminalPending) || Boolean(state.data?.active_jobs?.length);
}

function startPendingPoll() {
  if (state.pendingPollTimer) return;
  state.pendingPollTimer = window.setInterval(async () => {
    if (!isWorkPending()) {
      stopPendingPoll();
      return;
    }
    try {
      state.data = await api("/api/state");
      render();
    } catch (error) {
      setStatus(error.message);
    }
  }, PENDING_POLL_INTERVAL_MS);
}

function stopPendingPoll() {
  if (!state.pendingPollTimer) return;
  window.clearInterval(state.pendingPollTimer);
  state.pendingPollTimer = null;
}

async function postAndRefresh(path, body, options = {}) {
  setStatus("Working");
  state.pendingChatBaselineLength = state.data?.chat?.length || 0;
  state.chatPending = Boolean(options.chatPending);
  state.pendingChatMessage = options.pendingChatMessage || "";
  state.pendingChatImages = options.pendingChatImages || [];
  state.pendingChatAudio = options.pendingChatAudio || [];
  state.terminalPending = options.terminalPending || null;
  renderPendingState();
  startPendingPoll();
  try {
    const payload = await api(path, { method: "POST", body: JSON.stringify(body) });
    state.data = payload.state || state.data;
    const actions = payload.result?.actions || [];
    state.pendingEditorSelection = findEditorSelection(actions);
    if (options.savedSource !== undefined) {
      state.editorDirty = false;
      state.lastServerSource = options.savedSource;
      state.lastSourceMtimeNs = String(state.data?.source_mtime_ns || state.lastSourceMtimeNs || "0");
      if (state.data) {
        state.data = { ...state.data, source: options.savedSource };
      }
    }
    state.chatPending = false;
    state.pendingChatMessage = "";
    state.pendingChatImages = [];
    state.pendingChatAudio = [];
    state.pendingChatBaselineLength = 0;
    state.terminalPending = null;
    stopPendingPoll();
    syncPendingControls();
    render({ forceSource: Boolean(options.forceSource) || options.savedSource !== undefined || Boolean(state.pendingEditorSelection) });
    return payload;
  } catch (error) {
    if (error.status === 409 && error.payload?.state) {
      state.data = error.payload.state;
      state.editorDirty = false;
      state.pendingChatMessage = "";
      state.pendingChatImages = [];
      state.pendingChatAudio = [];
      state.pendingChatBaselineLength = 0;
      state.terminalPending = null;
      state.chatPending = false;
      stopPendingPoll();
      syncPendingControls();
      render({ forceSource: true });
      setStatus(error.message);
      return error.payload;
    }
    state.chatPending = false;
    state.pendingChatMessage = "";
    state.pendingChatImages = [];
    state.pendingChatAudio = [];
    state.pendingChatBaselineLength = 0;
    state.terminalPending = null;
    stopPendingPoll();
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

function currentEditorLine() {
  const editor = $("#source-editor");
  const source = editor.value;
  const start = source.lastIndexOf("\n", Math.max(0, editor.selectionStart - 1)) + 1;
  const next = source.indexOf("\n", editor.selectionStart);
  const end = next === -1 ? source.length : next;
  return source.slice(start, end);
}

function nextLineStartAfterEditorExecution() {
  const editor = $("#source-editor");
  const source = editor.value;
  const anchor = editor.selectionStart === editor.selectionEnd ? editor.selectionStart : editor.selectionEnd;
  const nextBreak = source.indexOf("\n", anchor);
  if (nextBreak === -1) return source.length;
  return nextBreak + 1;
}

function moveEditorCursor(position) {
  const editor = $("#source-editor");
  const next = Math.max(0, Math.min(position, editor.value.length));
  editor.focus();
  editor.setSelectionRange(next, next);
  updateSourceHighlight();
}

async function runSelectedEditorCode() {
  const code = selectedEditorCode() || currentEditorLine();
  if (!code.trim()) return;
  const nextPosition = nextLineStartAfterEditorExecution();
  await postAndRefresh(
    "/api/run-session",
    { code, source: "user_executed" },
    { terminalPending: { kind: "session", source: "user_executed", input: code } },
  );
  moveEditorCursor(nextPosition);
}

function updateSourceHighlight() {
  const editor = $("#source-editor");
  $("#source-highlight").innerHTML = highlightSource(editor.value);
  updateLineNumbers();
  updateCurrentLineHighlight();
}

function updateLineNumbers() {
  const editor = $("#source-editor");
  const gutter = $("#source-line-numbers");
  if (!gutter) return;
  const lineCount = Math.max(1, editor.value.split("\n").length);
  gutter.textContent = Array.from({ length: lineCount }, (_, index) => String(index + 1)).join("\n");
  gutter.scrollTop = editor.scrollTop;
}

function currentEditorLineIndex() {
  const editor = $("#source-editor");
  return editor.value.slice(0, editor.selectionStart).split("\n").length - 1;
}

function updateCurrentLineHighlight() {
  const editor = $("#source-editor");
  const highlight = $("#current-line-highlight");
  if (!highlight) return;
  const style = getComputedStyle(editor);
  const lineHeight = parseFloat(style.lineHeight) || 20;
  const paddingTop = parseFloat(style.paddingTop) || 0;
  const lineIndex = currentEditorLineIndex();
  highlight.style.top = `${paddingTop + lineIndex * lineHeight - editor.scrollTop}px`;
  highlight.style.height = `${lineHeight}px`;
}

function currentLanguage() {
  return state.data?.project?.language || state.data?.model_settings?.language || "r";
}

function languageShortLabel() {
  return currentLanguage() === "r" ? "R" : "py";
}

function languageDisplayName() {
  return currentLanguage() === "r" ? "R" : "Python";
}

function sessionPlaceholder() {
  return currentLanguage() === "r" ? "head(df)" : "print(df.head())";
}

function shellPlaceholder() {
  return currentLanguage() === "r" ? 'Rscript -e "renv::status()"' : "python3 -m pip install pandas";
}

function updateRuntimeLabels() {
  const languageName = languageDisplayName();
  const sourceName = state.data?.project?.source_file || (currentLanguage() === "r" ? "coplot.R" : "coplot.py");
  $("#save-source").title = `Save ${sourceName}`;
  $("#save-source").setAttribute("aria-label", `Save ${sourceName}`);
  $("#session-title").textContent = state.mode === "session" ? `${languageName} session` : "Shell command";
  $("#mode-session").textContent = languageShortLabel();
  $("#mode-session").title = `${languageName} session`;
  $("#mode-session").setAttribute("aria-label", `${languageName} session`);
  $("#command-input").placeholder = state.mode === "session" ? sessionPlaceholder() : shellPlaceholder();
}

function highlightSource(source) {
  return currentLanguage() === "r" ? highlightR(source) : highlightPython(source);
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

function highlightR(source) {
  const lines = source.split("\n").map((line) => {
    const commentIndex = line.indexOf("#");
    const code = commentIndex >= 0 ? line.slice(0, commentIndex) : line;
    const comment = commentIndex >= 0 ? line.slice(commentIndex) : "";
    return `${highlightRCode(code)}${comment ? `<span class="tok-comment">${escapeHtml(comment)}</span>` : ""}`;
  });
  return `${lines.join("\n")}\n`;
}

function highlightRCode(source) {
  const strings = [];
  let html = source.replace(/("[^"\n]*(?:\\.[^"\n]*)*"|'[^'\n]*(?:\\.[^'\n]*)*')/g, (match) => {
    const index = strings.push(match) - 1;
    return `___DSSTR${placeholderKey(index)}___`;
  });
  html = escapeHtml(html);
  html = html.replace(/\b(FALSE|TRUE|NULL|NA|NaN|Inf|if|else|repeat|while|function|for|in|next|break|library|require)\b/g, '<span class="tok-keyword">$1</span>');
  html = html.replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="tok-number">$1</span>');
  html = html.replace(/\b([A-Za-z.][A-Za-z0-9._]*)\s*(?=\()/g, '<span class="tok-call">$1</span>');
  html = html.replace(/___DSSTR([A-Z]+)___/g, (_, key) => `<span class="tok-string">${escapeHtml(strings[placeholderIndex(key)])}</span>`);
  return html;
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
  const source = $("#source-editor").value;
  await postAndRefresh(
    "/api/save",
    { source, source_mtime_ns: state.lastSourceMtimeNs },
    { savedSource: source }
  );
});

$("#run-selected").addEventListener("click", async () => {
  await runSelectedEditorCode();
});

$("#run-file").addEventListener("click", async () => {
  const source = $("#source-editor").value;
  await postAndRefresh(
    "/api/run-file",
    { source, source_mtime_ns: state.lastSourceMtimeNs },
    { terminalPending: { kind: "session", source: "durable_script", input: source }, savedSource: source },
  );
});

$("#mode-session").addEventListener("click", () => {
  state.mode = "session";
  $("#mode-session").classList.add("active");
  $("#mode-shell").classList.remove("active");
  $("#command-form").classList.add("session-command");
  $("#command-form").classList.remove("shell-command");
  $(".session-pane").classList.add("python-mode");
  $(".session-pane").classList.remove("shell-mode");
  updateRuntimeLabels();
});

$("#mode-shell").addEventListener("click", () => {
  state.mode = "shell";
  $("#mode-shell").classList.add("active");
  $("#mode-session").classList.remove("active");
  $("#command-form").classList.add("shell-command");
  $("#command-form").classList.remove("session-command");
  $(".session-pane").classList.add("shell-mode");
  $(".session-pane").classList.remove("python-mode");
  updateRuntimeLabels();
});

$("#clear-session").addEventListener("click", async () => {
  state.settingsDialogShown = false;
  const payload = await postAndRefresh("/api/clear-session", {}, { forceSource: true });
  if (payload.result?.message) {
    setStatus(payload.result.message);
  }
});

$("#clear-transcript").addEventListener("click", async () => {
  await postAndRefresh("/api/clear-transcript", {});
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
  if (!input && !state.chatImages.length && !state.chatAudio.length) return;
  const images = state.chatImages.slice();
  const audio = state.chatAudio.slice();
  $("#chat-input").value = "";
  state.chatImages = [];
  state.chatAudio = [];
  renderChatAttachments();
  await postAndRefresh(
    "/api/chat",
    { message: input, images, audio },
    { chatPending: true, pendingChatMessage: input, pendingChatImages: images, pendingChatAudio: audio }
  );
});

$("#source-editor").addEventListener("input", updateSourceHighlight);
$("#source-editor").addEventListener("input", () => {
  state.editorDirty = $("#source-editor").value !== state.lastServerSource;
});
$("#source-editor").addEventListener("scroll", () => {
  const editor = $("#source-editor");
  const highlight = $("#source-highlight");
  const gutter = $("#source-line-numbers");
  highlight.scrollTop = editor.scrollTop;
  highlight.scrollLeft = editor.scrollLeft;
  if (gutter) gutter.scrollTop = editor.scrollTop;
  updateCurrentLineHighlight();
});
$("#source-editor").addEventListener("click", updateCurrentLineHighlight);
$("#source-editor").addEventListener("keyup", updateCurrentLineHighlight);

$("#chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#chat-form").requestSubmit();
  }
});

$("#chat-input").addEventListener("paste", async (event) => {
  const items = Array.from(event.clipboardData?.items || []);
  const imageItems = items.filter((item) => item.kind === "file" && item.type === "image/png");
  if (!imageItems.length) return;
  event.preventDefault();
  for (const item of imageItems) {
    const file = item.getAsFile();
    if (!file) continue;
    const dataUrl = await readFileAsDataUrl(file);
    state.chatImages.push({
      type: "image",
      mime_type: "image/png",
      name: file.name || "pasted-image.png",
      size_bytes: file.size,
      data_url: dataUrl,
    });
  }
  renderChatAttachments();
});

$("#record-audio").addEventListener("click", activateAudioControl);

async function activateAudioControl() {
  if (state.chatPending || state.terminalPending) {
    await stopCurrentWork();
    return;
  }
  if (state.audioRecording) {
    await stopAudioRecording();
    return;
  }
  await startAudioRecording();
}

async function stopCurrentWork() {
  await api("/api/stop", { method: "POST", body: JSON.stringify({}) });
  state.chatPending = false;
  state.terminalPending = null;
  await refresh();
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "")));
    reader.addEventListener("error", () => reject(reader.error || new Error("Failed to read pasted image.")));
    reader.readAsDataURL(file);
  });
}

async function startAudioRecording() {
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    setStatus("Audio recording is not supported in this browser.");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const recorder = new MediaRecorder(stream);
    state.audioStream = stream;
    state.audioChunks = [];
    state.audioRecorder = recorder;
    state.audioRecordingStartedAt = Date.now();
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data && event.data.size) state.audioChunks.push(event.data);
    });
    recorder.addEventListener("stop", handleAudioRecordingStopped, { once: true });
    recorder.start();
    state.audioRecording = true;
    syncPendingControls();
    setStatus("Recording");
  } catch (error) {
    setStatus(error.message || "Could not start audio recording.");
    cleanupAudioRecording();
  }
}

async function stopAudioRecording() {
  const recorder = state.audioRecorder;
  if (!recorder || recorder.state === "inactive") return;
  recorder.stop();
  state.audioRecording = false;
  syncPendingControls();
  setStatus("Processing audio");
}

async function handleAudioRecordingStopped() {
  try {
    const durationMs = Math.max(1, Date.now() - state.audioRecordingStartedAt);
    const recorded = new Blob(state.audioChunks, { type: state.audioRecorder?.mimeType || "audio/webm" });
    cleanupAudioRecording();
    const wavBlob = await recordedBlobTo16kWav(recorded);
    const dataUrl = await readFileAsDataUrl(wavBlob);
    const audio = {
      type: "audio",
      mime_type: "audio/wav",
      format: "wav",
      name: "voice-message.wav",
      duration_ms: durationMs,
      size_bytes: wavBlob.size,
      data_url: dataUrl,
    };
    state.chatAudio = [audio];
    renderChatAttachments();
    setStatus("Ready");
    if (!$("#chat-input").value.trim()) {
      $("#chat-form").requestSubmit();
    } else {
      $("#chat-input").focus();
    }
  } catch (error) {
    cleanupAudioRecording();
    setStatus(error.message || "Could not process audio recording.");
  }
}

function cleanupAudioRecording() {
  if (state.audioStream) {
    state.audioStream.getTracks().forEach((track) => track.stop());
  }
  state.audioStream = null;
  state.audioRecorder = null;
  state.audioChunks = [];
  state.audioRecordingStartedAt = 0;
  state.audioRecording = false;
  syncPendingControls();
}

async function recordedBlobTo16kWav(blob) {
  const arrayBuffer = await blob.arrayBuffer();
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass || typeof OfflineAudioContext === "undefined") {
    throw new Error("Audio conversion is not supported in this browser.");
  }
  const context = new AudioContextClass();
  try {
    const decoded = await context.decodeAudioData(arrayBuffer.slice(0));
    const resampled = await resampleTo16kMono(decoded);
    return encodePcm16Wav(resampled);
  } finally {
    await context.close();
  }
}

async function resampleTo16kMono(audioBuffer) {
  const sampleRate = 16000;
  const frameCount = Math.max(1, Math.ceil(audioBuffer.duration * sampleRate));
  const offline = new OfflineAudioContext(1, frameCount, sampleRate);
  const source = offline.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(offline.destination);
  source.start(0);
  return await offline.startRendering();
}

function encodePcm16Wav(audioBuffer) {
  const samples = audioBuffer.getChannelData(0);
  const bytesPerSample = 2;
  const dataSize = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true);
  view.setUint32(28, 16000 * bytesPerSample, true);
  view.setUint16(32, bytesPerSample, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, dataSize, true);

  let offset = 44;
  for (const sample of samples) {
    const clamped = Math.max(-1, Math.min(1, sample));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }

  return new Blob([buffer], { type: "audio/wav" });
}

function writeAscii(view, offset, text) {
  for (let index = 0; index < text.length; index += 1) {
    view.setUint8(offset + index, text.charCodeAt(index));
  }
}

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

  if (event.shiftKey && event.code === "Space") {
    event.preventDefault();
    await activateAudioControl();
    return;
  }

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
  openSettingsDialog({ firstRun: false });
});

$("#close-model-settings").addEventListener("click", () => {
  $("#model-settings-dialog").close();
  state.settingsDialogShown = false;
});

$("#setting-endpoint-url").addEventListener("input", () => {
  setSupportedModalities([]);
});

$("#connect-model-endpoint").addEventListener("click", async () => {
  const endpointUrl = $("#setting-endpoint-url").value.trim();
  setSettingsMessage("Connecting...");
  try {
    const result = await api("/api/model-endpoint", {
      method: "POST",
      body: JSON.stringify({ endpoint_url: endpointUrl }),
    });
    const current = $("#setting-model").value || state.data.model_settings.model;
    const models = result.models || [];
    const selected = models.some((model) => model.id === current) ? current : models[0]?.id || current;
    populateModelOptions(selected, models);
    const contextWindow = selectedModelContextWindow(models, selected);
    if (contextWindow) $("#setting-context-window").value = contextWindow;
    $("#setting-reasoning-control").value = result.reasoning_control || "auto";
    setSupportedModalities(result.supported_modalities || []);
    setSettingsMessage(`Connected. Reasoning control: ${result.reasoning_control || "auto"}.`);
  } catch (error) {
    setSettingsMessage(error.message, true);
  }
});

$("#setting-model").addEventListener("change", () => {
  const contextWindow = selectedModelContextWindow(state.endpointModels, $("#setting-model").value);
  if (contextWindow) $("#setting-context-window").value = contextWindow;
});

document.querySelectorAll("input[name='language']").forEach((input) => {
  input.addEventListener("change", () => {
    if (!input.checked) return;
    updateLanguageSwitchSummary(input.value);
    updateLanguageTheme(input.value);
  });
});

$("#language-switch-toggle").addEventListener("click", () => {
  if ($("#language-switch-toggle").disabled) return;
  const nextLanguage = selectedSettingsLanguage() === "r" ? "python" : "r";
  setLanguageSelection(nextLanguage);
});

$("#save-model-defaults").addEventListener("click", async () => {
  setSettingsMessage("Saving defaults...");
  try {
    await api("/api/model-settings-defaults", {
      method: "POST",
      body: JSON.stringify(readModelSettingsForm()),
    });
    setSettingsMessage("Saved as defaults for future workspaces.");
  } catch (error) {
    setSettingsMessage(error.message, true);
  }
});

$("#model-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const path = state.settingsFirstRun ? "/api/model-settings-proceed" : "/api/model-settings";
  await postAndRefresh(path, readModelSettingsForm());
  $("#model-settings-dialog").close();
  state.settingsFirstRun = false;
  state.settingsDialogShown = false;
});

$("#compact-context").addEventListener("click", async () => {
  await postAndRefresh("/api/compact-context", {}, { chatPending: true });
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
updateRuntimeLabels();
