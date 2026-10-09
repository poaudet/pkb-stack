import { captureApi, CaptureApiError } from "./api.js";
import { strings } from "./strings.js";

const DRAFT_KEY = "pkb.capture.draft";
const SKIP_AI_KEY = "pkb.capture.skip_ai";
const CAPTURE_ID = /^[a-f0-9]{8,64}$/;
const ACTIVE_STATES = new Set(["queued", "processing"]);
const $ = (id) => document.getElementById(id);

const state = {
  config: null,
  currentId: null,
  pollTimer: null,
  pollStarted: 0,
  draftTimer: null,
  busy: false,
  routeKey: "composer",
  controller: new AbortController(),
};

function el(tag, props, ...children) {
  const element = document.createElement(tag);
  Object.assign(element, props || {});
  for (const child of children) {
    if (child !== null && child !== undefined) element.append(child);
  }
  return element;
}

function localDate() {
  const date = new Date();
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0")].join("-");
}

function updateCounter() {
  const length = $("capture-text").value.length;
  const limit = state.config ? state.config.max_chars : 8000;
  $("capture-count").textContent = `${length}/${limit}`;
  $("capture-submit").disabled = state.busy || length === 0;
  $("capture-submit").textContent = submitLabel();
}

function saveDraft() {
  clearTimeout(state.draftTimer);
  state.draftTimer = setTimeout(() => {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({
        text: $("capture-text").value,
        hint: $("capture-hint").value,
      }));
    } catch (_error) {
      $("capture-message").textContent = "Draft storage is unavailable in this browser.";
    }
  }, 500);
}

function restoreDraft() {
  try {
    const draft = JSON.parse(localStorage.getItem(DRAFT_KEY) || "{}");
    if (typeof draft.text === "string") $("capture-text").value = draft.text;
    if (typeof draft.hint === "string") $("capture-hint").value = draft.hint;
  } catch (_error) {
    localStorage.removeItem(DRAFT_KEY);
  }
  $("capture-skip-ai").checked = localStorage.getItem(SKIP_AI_KEY) === "true";
  updateCounter();
}

function clearDraft() {
  clearTimeout(state.draftTimer);
  localStorage.removeItem(DRAFT_KEY);
}

function setBusy(value) {
  state.busy = value;
  $("capture-submit").disabled = value || $("capture-text").value.length === 0;
  $("capture-submit").textContent = submitLabel();
}

function submitLabel() {
  return $("capture-skip-ai").checked || !state.config || !state.config.enabled
    || !state.config.model ? strings.saveWithoutAi : strings.analyze;
}

function configText(config) {
  if (!config.enabled || !config.model) {
    return strings.privacyUnavailable;
  }
  const privacy = config.local ? strings.privacyLocal : strings.privacyRemote;
  const host = config.host || "unknown host";
  const limit = config.daily_limit === 0
    ? `${config.calls_today} calls today`
    : `${config.calls_today}/${config.daily_limit} today`;
  return `AI: ${host} · ${privacy} · ${limit}`;
}

function routeSignal() {
  return { signal: state.controller.signal };
}

function activateRoute(key) {
  if (state.routeKey === key) return false;
  stopPolling();
  state.controller.abort();
  state.controller = new AbortController();
  state.routeKey = key;
  return true;
}

async function loadConfig(options = routeSignal()) {
  try {
    state.config = await captureApi.config(options);
    $("capture-privacy").textContent = configText(state.config);
    $("capture-privacy").title = state.config.resets_at
      ? `Usage resets at ${state.config.resets_at}` : "";
    $("capture-submit").hidden = false;
    $("capture-text").maxLength = state.config.max_chars;
    updateCounter();
  } catch (error) {
    if (error.name === "AbortError") return;
    state.config = null;
    $("capture-privacy").textContent = "AI configuration unavailable";
    $("capture-submit").hidden = false;
    showError(error);
  }
}

function appendErrorAction(container, label, callback, className = "secondary") {
  const button = el("button", { className, textContent: label });
  button.addEventListener("click", callback);
  container.append(button);
}

function showError(error) {
  const box = $("capture-error");
  box.replaceChildren();
  box.hidden = false;
  const code = error instanceof CaptureApiError ? error.code : "";
  const message = error instanceof Error ? error.message : "The request failed.";
  if (code === "secret_detected") {
    box.append(el("p", { textContent: "Possible secret detected; the text was not sent." }));
    appendErrorAction(box, "Send anyway", () => {
      if (window.confirm(strings.secretConfirm)) submitCapture({ allowSecrets: true });
    });
    return;
  }
  if (code === "too_long") {
    box.append(el("p", { textContent: strings.tooLong }));
    appendErrorAction(box, strings.saveAsNote, saveLongText);
    return;
  }
  if (code === "llm_not_configured") {
    box.append(el("p", { textContent: "AI capture is unavailable. You can save this as a note." }));
    appendErrorAction(box, strings.saveAsNote, saveLongText);
    return;
  }
  if (code === "duplicate") {
    box.append(el("p", { textContent: "This text was applied recently; duplicate capture was blocked." }));
    return;
  }
  if (code === "rate_limited") {
    box.append(el("p", { textContent: `Daily AI call limit reached. Resets at ${error.details.resets_at || "midnight UTC"}.` }));
    return;
  }
  box.append(el("p", { textContent: message || "The request failed. Your draft is safe." }));
}

function clearError() {
  $("capture-error").replaceChildren();
  $("capture-error").hidden = true;
}

async function saveLongText() {
  const text = $("capture-text").value;
  setBusy(true);
  clearError();
  try {
    const response = await captureApi.createNote(text, routeSignal());
    clearDraft();
    $("capture-text").value = "";
    $("capture-hint").value = "";
    $("capture-message").replaceChildren(
      document.createTextNode("Saved as a note. "),
      el("a", { href: `#note/${encodeURIComponent(response.note.id)}`, textContent: "Open note" }),
    );
    updateCounter();
  } catch (error) {
    if (error.name !== "AbortError") showError(error);
  } finally {
    setBusy(false);
  }
}

async function submitCapture(options = {}) {
  const text = $("capture-text").value;
  const skipAi = $("capture-skip-ai").checked || !state.config
    || !state.config.enabled || !state.config.model;
  const maxChars = state.config ? state.config.max_chars : 8000;
  clearError();
  $("capture-message").textContent = "";
  if (!text.trim()) return;
  if (!skipAi && text.length > maxChars) {
    showError(new CaptureApiError(strings.tooLong, 400, "too_long"));
    return;
  }
  setBusy(true);
  try {
    const result = await captureApi.create({
      text,
      hint: $("capture-hint").value || undefined,
      as_of: $("capture-as-of").value || undefined,
      skip_ai: skipAi,
      ...(options.allowSecrets ? { allow_secrets: true } : {}),
    }, routeSignal());
    clearDraft();
    $("capture-text").value = "";
    $("capture-hint").value = "";
    updateCounter();
    await Promise.all([loadConfig(), loadHistory()]);
    if (result.status === 201 && result.note) {
      $("capture-message").replaceChildren(
        document.createTextNode("Saved as a note. "),
        el("a", { href: `#note/${encodeURIComponent(result.note.id)}`, textContent: "Open note" }),
      );
      return;
    }
    if (result.capture) {
      location.hash = `#capture/${encodeURIComponent(result.capture.id)}`;
      state.pollStarted = Date.now();
      routeCapture(location.hash);
    }
  } catch (error) {
    if (error.name !== "AbortError") showError(error);
  } finally {
    setBusy(false);
    updateCounter();
  }
}

function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function statusMessage(status, capture) {
  const box = $("capture-state");
  box.replaceChildren();
  box.hidden = false;
  const elapsed = state.pollStarted ? Math.floor((Date.now() - state.pollStarted) / 1000) : 0;
  const progress = el("progress", { "aria-label": "Capture processing" });
  box.append(progress, el("p", { role: "status", textContent: strings.waiting }));
  if (elapsed >= 60) {
    box.append(el("p", { textContent: strings.working }));
  }
  const stop = el("button", { className: "secondary", textContent: strings.stopWaiting });
  stop.addEventListener("click", () => {
    stopPolling();
    location.hash = "#capture";
  });
  box.append(stop);
  if (elapsed >= 300) {
    box.append(el("p", { textContent: "Polling stopped after five minutes; the capture continues." }));
    stopPolling();
  } else if (!document.hidden && ACTIVE_STATES.has(status)) {
    state.pollTimer = setTimeout(() => refreshCapture(), 2000);
  }
  if (capture) $("capture-title").focus({ preventScroll: true });
}

function readOnlyPlan(capture) {
  const section = $("capture-plan");
  section.replaceChildren();
  section.hidden = false;
  const plan = capture.plan || {};
  section.append(el("h2", { tabIndex: -1, textContent: plan.summary || "Capture plan" }));
  const metadata = [];
  if (capture.llm_model) metadata.push(capture.llm_model);
  if (capture.prompt_tokens !== null || capture.completion_tokens !== null) {
    metadata.push(`${capture.prompt_tokens || 0}/${capture.completion_tokens || 0} tokens`);
  }
  if (metadata.length) section.append(el("p", { className: "meta", textContent: metadata.join(" · ") }));
  for (const warning of [...(plan.warnings || []), ...(capture.warnings || [])]) {
    section.append(el("p", { className: "capture-warning", textContent: warning }));
  }
  if ((plan.questions || []).length) {
    section.append(el("h3", { textContent: "Questions" }));
    const questions = el("ul");
    for (const question of plan.questions) questions.append(el("li", { textContent: question }));
    section.append(questions);
  }
  if ((plan.gaps || []).length) {
    section.append(el("h3", { textContent: "Missing records" }));
    const gaps = el("ul");
    for (const gap of plan.gaps) {
      gaps.append(el("li", { textContent: gap.text || gap.action || gap.kind }));
    }
    section.append(gaps);
  }
  const ops = el("div", { className: "capture-op-list" });
  for (const op of plan.ops || []) {
    const card = el("article", { className: "card capture-op" });
    card.append(el("h3", { textContent: op.label || op.op || op.kind || "Operation" }));
    card.append(el("span", { className: "badge", textContent: op.op || op.kind || "operation" }));
    if (op.status) card.append(el("span", { className: "badge", textContent: op.status }));
    if (op.why) card.append(el("p", { className: "meta", textContent: op.why }));
    for (const message of op.errors || []) {
      card.append(el("p", { className: "capture-error-text", textContent: message }));
    }
    for (const warning of op.warnings || []) {
      card.append(el("p", { className: "capture-warning", textContent: warning }));
    }
    const details = {};
    for (const key of ["type", "target", "data", "patch", "diff"]) {
      if (op[key] !== undefined) details[key] = op[key];
    }
    if (Object.keys(details).length) {
      card.append(el("pre", { textContent: JSON.stringify(details, null, 2) }));
    }
    ops.append(card);
  }
  if (ops.childElementCount) section.append(ops);
  const heading = section.querySelector("h2");
  heading.focus({ preventScroll: true });
}

function renderCapture(capture) {
  $("capture-state").replaceChildren();
  $("capture-state").hidden = true;
  $("capture-plan").replaceChildren();
  $("capture-plan").hidden = true;
  $("capture-message").textContent = "";
  $("capture-error").hidden = true;
  $("capture-title").textContent = `Capture · ${capture.state}`;

  if (ACTIVE_STATES.has(capture.state)) {
    if (!state.pollStarted) {
      state.pollStarted = Date.parse(capture.created_at) || Date.now();
    }
    statusMessage(capture.state, capture);
    return;
  }
  stopPolling();
  if (capture.state === "ready" || capture.state === "applied") {
    readOnlyPlan(capture);
    if (capture.state === "applied") {
      $("capture-message").textContent = "This capture has been applied.";
    }
    return;
  }
  const box = $("capture-state");
  box.hidden = false;
  if (capture.state === "failed") {
    box.append(el("p", { role: "status", textContent: capture.error || "Capture could not be analyzed." }));
    appendErrorAction(box, strings.regenerate, () => regenerate(capture.id));
    if (capture.text) appendErrorAction(box, strings.saveAsNote, () => saveFailedCapture(capture.id));
    if (capture.text) appendErrorAction(box, strings.editText, () => editCapture(capture));
    return;
  }
  if (capture.state === "rejected" || capture.state === "undone") {
    box.append(el("p", { role: "status", textContent: `Capture ${capture.state}.` }));
    appendErrorAction(box, strings.startNew, () => { location.hash = "#capture"; });
    return;
  }
  box.append(el("p", { textContent: `Capture state: ${capture.state}` }));
}

async function showCapture(id, submitted = false) {
  stopPolling();
  state.currentId = id;
  state.pollStarted = submitted ? Date.now() : 0;
  $("capture-composer").hidden = true;
  $("capture-history-panel").hidden = true;
  $("capture-detail").hidden = false;
  $("capture-title").textContent = "Capture";
  $("capture-state").replaceChildren();
  $("capture-state").hidden = false;
  $("capture-state").textContent = strings.waiting;
  try {
    const capture = await captureApi.get(id, routeSignal());
    renderCapture(capture);
  } catch (error) {
    if (error.name === "AbortError") return;
    $("capture-state").replaceChildren(el("p", { textContent: error.message }));
    showError(error);
  }
}

async function refreshCapture() {
  stopPolling();
  if (!state.currentId || document.hidden) return;
  try {
    const capture = await captureApi.get(state.currentId, routeSignal());
    renderCapture(capture);
  } catch (error) {
    if (error.name === "AbortError") return;
    $("capture-state").replaceChildren(el("p", { textContent: error.message }));
  }
}

async function regenerate(id) {
  try {
    const capture = await captureApi.regenerate(id, {}, routeSignal());
    state.pollStarted = Date.now();
    renderCapture(capture);
    await loadHistory();
  } catch (error) {
    if (error.name !== "AbortError") showError(error);
  }
}

async function saveFailedCapture(id) {
  try {
    const result = await captureApi.saveNote(id, routeSignal());
    $("capture-state").replaceChildren(
      el("p", { textContent: "Saved as a note." }),
      el("a", { href: `#note/${encodeURIComponent(result.note.id)}`, textContent: "Open note" }),
    );
    await loadHistory();
  } catch (error) {
    if (error.name !== "AbortError") showError(error);
  }
}

function editCapture(capture) {
  $("capture-text").value = capture.text || "";
  $("capture-hint").value = capture.hint || "";
  updateCounter();
  saveDraft();
  location.hash = "#capture";
}

function historyEntry(item) {
  const link = el("a", {
    className: "card capture-history-item",
    href: `#capture/${encodeURIComponent(item.id)}`,
  });
  link.append(el("span", { className: "badge", textContent: item.state }));
  link.append(el("span", { className: "badge", textContent: item.source || "ui" }));
  link.append(el("span", { className: "capture-history-summary", textContent: item.summary || "(no summary)" }));
  link.append(el("span", { className: "meta", textContent: item.created_at || "" }));
  return link;
}

async function loadHistory(options = routeSignal()) {
  const list = $("capture-history");
  list.replaceChildren();
  try {
    const result = await captureApi.list({ limit: 30 }, options);
    let outstanding = 0;
    for (const item of result.captures) {
      list.append(historyEntry(item));
      if (["queued", "processing", "ready", "failed"].includes(item.state)) outstanding += 1;
    }
    $("capture-badge").textContent = outstanding ? String(outstanding) : "";
    if (!result.captures.length) list.append(el("p", { textContent: strings.noHistory }));
  } catch (error) {
    if (error.name !== "AbortError") list.append(el("p", { textContent: error.message }));
  }
}

function showComposer() {
  stopPolling();
  state.currentId = null;
  $("capture-detail").hidden = true;
  $("capture-composer").hidden = false;
  $("capture-history-panel").hidden = false;
  clearError();
  $("capture-message").textContent = "";
  restoreDraft();
}

export async function initCapture() {
  $("capture-as-of").value = localDate();
  $("capture-text").addEventListener("input", () => {
    updateCounter();
    saveDraft();
  });
  $("capture-hint").addEventListener("input", saveDraft);
  $("capture-skip-ai").addEventListener("change", () => {
    localStorage.setItem(SKIP_AI_KEY, String($("capture-skip-ai").checked));
    updateCounter();
  });
  $("capture-as-of").addEventListener("change", () => {
    $("capture-as-of").setCustomValidity("");
  });
  $("capture-submit").addEventListener("click", () => submitCapture());
  $("capture-text").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      submitCapture();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPolling();
    else if (state.currentId) refreshCapture();
  });
  restoreDraft();
  await Promise.all([
    loadConfig(routeSignal()),
    loadHistory(routeSignal()),
  ]);
}

export function leaveCapture() {
  stopPolling();
  state.controller.abort();
  state.controller = new AbortController();
  state.routeKey = null;
}

export function routeCapture(hash) {
  const match = /^#capture(?:\/([^/]+))?$/.exec(hash);
  let id = null;
  try {
    id = match && match[1] ? decodeURIComponent(match[1]) : null;
  } catch (_error) {
    showComposer();
    return;
  }
  if (id && CAPTURE_ID.test(id)) {
    const changed = activateRoute("capture:" + id);
    if (changed || id !== state.currentId) showCapture(id);
  } else {
    if (activateRoute("composer") || state.currentId !== null) showComposer();
  }
}
