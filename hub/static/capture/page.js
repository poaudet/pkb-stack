import { captureApi, CaptureApiError } from "./api.js";
import { strings } from "./strings.js";
import { closureDown, closureUp } from "./state.js";
import { schemaField } from "./schemaform.js";
import { renderDiff } from "./diff.js";
import { confirmDialog } from "./dialog.js";
import { renderOwnerChip } from "../owner.js";
import { refreshTypes } from "../types.js";

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
  capture: null,
  overrides: new Map(),
  selected: new Set(),
  labels: new Map(),
  typeCache: new Map(),
  keepNote: false,
  answers: new Map(),
  validation: null,
  validationPending: false,
  validationFailed: false,
  validateTimer: null,
  validateVersion: 0,
  undoReport: null,
  badgeTimer: null,
  resultId: null,
};

function el(tag, props, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key.startsWith("aria-")) element.setAttribute(key, value);
    else Object.assign(element, { [key]: value });
  }
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

function opOverride(id) {
  if (!state.overrides.has(String(id))) state.overrides.set(String(id), {});
  return state.overrides.get(String(id));
}

function setOverride(op, field, value) {
  const id = String(op.id);
  const key = field === "data" ? "data" : field === "patch" ? "patch" : "add_properties";
  const source = op[key] || {};
  const next = Object.assign({}, source);
  const current = Object.assign({}, opOverride(id)[key] || {});
  for (const [name, item] of Object.entries(value)) {
    if (JSON.stringify(item) === JSON.stringify(source[name])) delete current[name];
    else current[name] = item;
  }
  if (Object.keys(current).length) next[key] = current;
  else delete next[key];
  const override = opOverride(id);
  if (next[key]) override[key] = next[key];
  else delete override[key];
  if (!Object.keys(override).length) state.overrides.delete(id);
  scheduleValidation();
}

function setExtensionOverride(op, properties) {
  const id = String(op.id);
  const override = opOverride(id);
  if (JSON.stringify(properties) === JSON.stringify(op.add_properties || {})) {
    delete override.add_properties;
  } else {
    override.add_properties = properties;
  }
  if (!Object.keys(override).length) state.overrides.delete(id);
  scheduleValidation();
}

function inputForRef(op, fieldName, property, original, errorBox) {
  const values = Array.isArray(original) ? [...original] : [original];
  const wrap = el("div", { className: "reference-chips" });
  const fieldLabel = el("span", { className: "meta", textContent: fieldName });
  wrap.append(fieldLabel);
  values.forEach((value) => {
    let label = "Existing " + property["x-ref"];
    if (value && typeof value === "object" && value.$ref) {
      const referenced = (state.capture.plan.ops || []).find((item) => item.ref === value.$ref);
      const data = referenced && referenced.data || {};
      label = data.name || data.title || referenced && referenced.label || label;
    } else if (typeof value === "string" && state.labels.has(value)) {
      label = state.labels.get(value).label;
    }
    const chip = el("span", { className: "badge", textContent: label });
    const remove = el("button", {
      type: "button",
      className: "secondary chip-remove",
      textContent: "Remove",
      "aria-label": `Remove ${label}`,
    });
    remove.addEventListener("click", () => {
      values.splice(values.indexOf(value), 1);
      const next = Array.isArray(original) ? values : (values.length ? values[0] : null);
      setOverride(op, op.kind === "update_entity" ? "patch" : "data", { [fieldName]: next });
      chip.remove();
      remove.remove();
    });
    wrap.append(chip, remove);
  });
  if (errorBox) wrap.append(errorBox);
  return wrap;
}

function renderForm(op, sourceKey, onlyPresent = false) {
  const form = el("div", { className: "capture-edit-form" });
  const type = state.typeCache.get(op.type);
  const schema = type && type.json_schema || { properties: {}, required: [] };
  const original = op[sourceKey] || {};
  const overrides = opOverride(op.id)[sourceKey] || {};
  const values = Object.assign({}, original, overrides);
  const properties = Object.assign({}, schema.properties || {});
  for (const extension of state.capture.plan.ops || []) {
    if (extension.kind === "extend_entity_type" && extension.type === op.type) {
      Object.assign(properties,
        opOverride(extension.id).add_properties || extension.add_properties || {});
    }
  }
  const visible = new Set(Object.keys(values));
  for (const name of (schema.required || [])) visible.add(name);
  const renderField = (name) => {
    const property = properties[name];
    if (!property) return;
    const initial = values[name];
    if (property["x-ref"]) {
      form.append(inputForRef(op, name, property, initial || [], null));
      return;
    }
    const field = schemaField(name, property, initial, (schema.required || []).includes(name), String(op.id));
    field.input.dataset.fieldName = name;
    const error = el("small", { className: "capture-error-text", role: "status" });
    field.wrapper.append(error);
    form.append(field.wrapper);
    const saveValue = () => {
      try {
        const value = field.read();
        const emptyValue = value === undefined && Object.hasOwn(original, name)
          ? null : value;
        setOverride(op, sourceKey, { [name]: emptyValue });
        error.textContent = "";
      } catch (exception) {
        error.textContent = exception.message;
      }
    };
    field.input.addEventListener("input", saveValue);
    field.input.addEventListener("change", saveValue);
  };
  for (const name of visible) renderField(name);
  if (!onlyPresent) {
    const remaining = Object.keys(properties).filter((name) => !visible.has(name));
    if (remaining.length) {
      const more = el("details", { className: "more-fields" });
      more.append(el("summary", { textContent: "More fields" }));
      for (const name of remaining) {
        const property = properties[name];
        if (property["x-ref"]) {
          more.append(inputForRef(op, name, property, [], null));
          continue;
        }
        const field = schemaField(name, property, undefined, (schema.required || []).includes(name), String(op.id));
        field.input.dataset.fieldName = name;
        const error = el("small", { className: "capture-error-text", role: "status" });
        field.wrapper.append(error);
        more.append(field.wrapper);
        const saveValue = () => {
          try {
            setOverride(op, sourceKey, { [name]: field.read() });
            error.textContent = "";
          } catch (exception) {
            error.textContent = exception.message;
          }
        };
        field.input.addEventListener("input", saveValue);
        field.input.addEventListener("change", saveValue);
      }
      form.append(more);
    }
  }
  return form;
}

function editExtension(op) {
  const original = op.add_properties || {};
  const current = Object.assign({}, opOverride(op.id).add_properties || original);
  const box = el("div", { className: "capture-edit-form" });
  for (const [fieldName, property] of Object.entries(current)) {
    let currentName = fieldName;
    const name = el("input", {
      type: "text",
      value: fieldName,
      "aria-label": "New field name",
    });
    const description = el("input", {
      type: "text",
      value: property.description || "",
      "aria-label": "New field description",
    });
    const row = el("div", { className: "extension-field" });
    row.append(el("label", { textContent: "Name" }, name),
      el("p", { className: "meta", textContent: `Type: ${property.type}${property.format ? ` (${property.format})` : ""}` }),
      el("label", { textContent: "Description" }, description));
    const save = () => {
      const next = Object.assign({}, opOverride(op.id).add_properties || original);
      const value = Object.assign({}, next[currentName] || property, {
        description: description.value,
      });
      if (name.value !== currentName) delete next[currentName];
      next[name.value] = value;
      currentName = name.value;
      setExtensionOverride(op, next);
    };
    name.addEventListener("input", save);
    description.addEventListener("input", save);
    row.append(el("small", { className: "capture-error-text", role: "status" }));
    box.append(row);
  }
  return box;
}

function operationBadge(op) {
  const kinds = {
    create_entity: "Create",
    update_entity: "Update",
    supersede_entity: "Replace",
    create_entity_type: "New type",
    extend_entity_type: "Add field",
    create_note: "Save note",
  };
  return kinds[op.kind] || op.kind || op.op || "Operation";
}

function operationTitle(op) {
  const badge = operationBadge(op);
  if (op.label && String(op.label).toLowerCase().startsWith(badge.toLowerCase())) return op.label;
  const type = op.type || "";
  const info = state.typeCache.get(type);
  const key = info && info.unique_key || "name";
  const data = op.data || op.patch || {};
  const target = op.target && typeof op.target === "object" ? op.target : {};
  const value = data[key] || data.title || target.label || target.name;
  const label = value === undefined ? op.label : value;
  return [operationBadge(op), type, label].filter(Boolean).join(" — ");
}

function renderOp(op) {
  const card = el("article", { className: "card capture-op", id: `capture-op-${op.id}` });
  card.dataset.opId = String(op.id);
  const heading = el("h3", { textContent: operationTitle(op) });
  card.append(heading);
  const riskLabels = {
    schema_change: "New field",
    new_type: "New type",
    auto_gap: "Missing record",
    similar_type: "Similar type exists",
    overwrites_text: "Overwrites text",
    converted: "Converted",
  };
  if (op.risk) card.append(el("span", {
    className: "badge",
    textContent: riskLabels[op.risk] || op.risk,
  }));
  if (op.gap && op.gap.kind === "auto_gap") {
    card.append(el("span", { className: "badge", textContent: "Missing record" }));
  }
  const status = el("span", { className: "badge capture-status-badge", textContent: op.status || "unknown" });
  card.append(status);
  if (Object.keys(opOverride(op.id)).some((key) => key !== "include")) {
    card.append(el("span", { className: "badge", textContent: "Edited" }));
  }
  if (op.why) card.append(el("p", { className: "meta capture-why", textContent: op.why }));
  const validation = el("div", { className: "op-validation", "aria-live": "polite" });
  for (const error of op.errors || []) validation.append(el("p", { className: "capture-error-text", textContent: error }));
  for (const warning of op.warnings || []) validation.append(el("p", { className: "capture-warning", textContent: warning }));
  card.append(validation);
  if (op.requires && op.requires.length) {
    for (const requirement of op.requires) {
      const required = (state.capture.plan.ops || []).find((item) => String(item.id) === String(requirement));
      card.append(el("p", { className: "meta", textContent: `Needs: ${required && required.label || "another operation"}` }));
    }
  }
  const checkbox = el("input", {
    type: "checkbox",
    checked: state.selected.has(String(op.id)),
    disabled: op.status !== "valid" && !state.selected.has(String(op.id)),
    "aria-label": op.label || op.type || "Select operation",
  });
  checkbox.dataset.opSelect = String(op.id);
  checkbox.addEventListener("change", () => changeSelection(op, checkbox.checked));
  const selectionLabel = el("label", { className: "capture-op-select" }, checkbox,
    document.createTextNode(" Include this operation"));
  card.prepend(selectionLabel);

  if (op.kind === "create_entity" || op.kind === "supersede_entity") {
    card.append(renderForm(op, "data"));
  } else if (op.kind === "update_entity") {
    if (op.diff && op.diff.length) card.append(renderDiff(op.diff));
    const edit = el("details", { className: "edit-changes" });
    edit.append(el("summary", { textContent: "Edit changes" }));
    edit.append(renderForm(op, "patch", true));
    card.append(edit);
  } else if (op.kind === "extend_entity_type") {
    card.append(editExtension(op));
  } else if (op.kind === "create_entity_type") {
    const schema = op.json_schema || {};
    const required = new Set(schema.required || []);
    for (const [name, property] of Object.entries(schema.properties || {})) {
      card.append(el("p", {
        className: "meta",
        textContent: `${name} — ${property.type}${required.has(name) ? " · required" : ""}`
          + (property.description ? ` · ${property.description}` : ""),
      }));
    }
  }
  return card;
}

function requestBody() {
  const ops = [];
  for (const op of state.capture.plan.ops || []) {
    const override = state.overrides.get(String(op.id)) || {};
    const include = state.selected.has(String(op.id));
    const item = { id: String(op.id), include };
    for (const key of ["data", "patch", "add_properties"]) {
      if (override[key]) item[key] = override[key];
    }
    if (include || Object.keys(item).length > 1) ops.push(item);
  }
  const body = {};
  if (ops.length) body.ops = ops;
  if (state.keepNote !== Boolean(state.capture.plan.keep_note)) body.keep_note = state.keepNote;
  if ((state.capture.plan.ops || []).some((op) =>
    op.kind === "create_entity_type" && state.selected.has(String(op.id)))) {
    body.allow_new_types = true;
  }
  return body;
}

function updateActionBar() {
  const bar = $("capture-action-bar");
  if (!bar) return;
  const ops = state.capture.plan.ops || [];
  const selected = ops.filter((op) => state.selected.has(String(op.id)));
  const invalid = selected.some((op) => op.status !== "valid")
    || Boolean(state.validation && !state.validation.selection.ok)
    || state.validationPending || state.validationFailed;
  const count = bar.querySelector("#capture-selected-count");
  if (count) count.textContent = `${selected.length} of ${ops.length} selected`;
  const apply = bar.querySelector("#capture-apply");
  if (apply) apply.disabled = !selected.length || invalid || state.busy;
}

function showDiff(card, changes) {
  let table = card.querySelector(".capture-diff");
  if (!changes || !changes.length) {
    table?.remove();
    return;
  }
  if (!table) {
    table = renderDiff(changes);
    const edit = card.querySelector(".edit-changes");
    if (edit) card.insertBefore(table, edit);
    else card.append(table);
    return;
  }
  const replacement = renderDiff(changes);
  table.replaceWith(replacement);
}

function updateValidationView() {
  const active = document.activeElement;
  const activeCard = active && active.closest("[data-op-id]");
  const activeOp = activeCard && activeCard.dataset.opId;
  const activeKey = active && (active.dataset.opSelect || active.dataset.fieldName
    || active.getAttribute("aria-label"));
  const activeStart = active && typeof active.selectionStart === "number" ? active.selectionStart : null;
  const ops = state.capture.plan.ops || [];
  for (const op of ops) {
    const card = document.querySelector(`[data-op-id="${CSS.escape(String(op.id))}"]`);
    if (!card) continue;
    const checkbox = card.querySelector("[data-op-select]");
    if (checkbox) {
      checkbox.disabled = op.status !== "valid" && !state.selected.has(String(op.id));
      checkbox.checked = state.selected.has(String(op.id));
    }
    if (op.kind === "update_entity") showDiff(card, op.diff);
    const replacement = renderOp(op);
    card.replaceWith(replacement);
  }
  if (activeOp && activeKey) {
    const card = document.querySelector(`[data-op-id="${CSS.escape(activeOp)}"]`);
    const control = card && [...card.querySelectorAll("input, textarea, select")]
      .find((item) => item.dataset.opSelect === activeKey
        || item.dataset.fieldName === activeKey
        || item.getAttribute("aria-label") === activeKey);
    if (control) {
      control.focus({ preventScroll: true });
      if (activeStart !== null && typeof control.setSelectionRange === "function") {
        control.setSelectionRange(activeStart, activeStart);
      }
    }
  }
  updateActionBar();
}

function scheduleValidation() {
  if (!state.capture || state.capture.state !== "ready") return;
  state.validationPending = true;
  updateActionBar();
  clearTimeout(state.validateTimer);
  state.validateTimer = setTimeout(() => validatePlan(), 600);
}

async function validatePlan() {
  if (!state.capture || state.capture.state !== "ready") return;
  state.validationPending = true;
  updateActionBar();
  const version = ++state.validateVersion;
  try {
    const result = await captureApi.validate(state.capture.id, requestBody(), routeSignal());
    if (version !== state.validateVersion) return;
    const updates = new Map(result.ops.map((op) => [String(op.id), op]));
    for (const op of state.capture.plan.ops || []) {
      const checked = updates.get(String(op.id));
      if (!checked) continue;
      for (const key of ["status", "errors", "warnings", "diff", "requires", "risk", "label"]) {
        if (checked[key] !== undefined) op[key] = checked[key];
      }
    }
    state.validation = result;
    state.validationPending = false;
    state.validationFailed = false;
    updateValidationView();
  } catch (error) {
    if (error.name !== "AbortError") {
      state.validationPending = false;
      state.validationFailed = true;
      const message = $("capture-edit-message");
      if (message) message.textContent = "Could not validate these edits. Apply is disabled until validation succeeds.";
      const apply = $("capture-apply");
      if (apply) apply.disabled = true;
    }
  }
}

function changeSelection(op, checked) {
  const id = String(op.id);
  if (checked) {
    const proposed = closureUp(new Set([...state.selected, id]), state.capture.plan.ops || []);
    const invalid = [...proposed].map((required) =>
      (state.capture.plan.ops || []).find((item) => String(item.id) === required))
      .filter((item) => item && item.status !== "valid");
    if (invalid.length) {
      state.selected.delete(id);
      const checkbox = document.querySelector(`[data-op-select="${CSS.escape(id)}"]`);
      if (checkbox) checkbox.checked = false;
      $("capture-edit-message").textContent = `Cannot include this operation until ${invalid.map((item) => item.label || "a required operation").join(", ")} is valid.`;
      return;
    }
    const before = new Set(state.selected);
    state.selected = proposed;
    const added = [...state.selected].filter((required) => !before.has(required) && required !== id)
      .map((required) => (state.capture.plan.ops || []).find((item) => String(item.id) === required))
      .filter(Boolean);
    if (added.length) $("capture-edit-message").textContent =
      `Also selected: ${added.map((item) => item.label || item.type).join(", ")}.`;
  } else {
    const before = new Set(state.selected);
    state.selected = closureDown(state.selected, state.capture.plan.ops || [], new Set([id]));
    const removed = [...before].filter((item) => !state.selected.has(item));
    if (removed.length > 1) $("capture-edit-message").textContent =
      `Also unselected: ${removed.slice(1).map((removedId) =>
        (state.capture.plan.ops || []).find((item) => String(item.id) === removedId)?.label || "dependent operation"
      ).join(", ")}.`;
  }
  updateValidationView();
  scheduleValidation();
}

function appendPlanQuestions(section, capture) {
  const questions = capture.plan.questions || [];
  if (!questions.length) return;
  section.append(el("h3", { textContent: "Questions" }));
  const box = el("div", { className: "capture-questions" });
  for (const question of questions) {
    const label = el("label", { textContent: question });
    const answer = el("textarea", {
      rows: 2,
      "aria-label": question,
      value: state.answers.get(question) || "",
    });
    answer.addEventListener("input", () => {
      state.answers.set(question, answer.value);
      const button = box.querySelector("#capture-regenerate-answers");
      if (button) button.disabled = ![...state.answers.values()].some((value) => value.trim());
    });
    label.append(answer);
    box.append(label);
  }
  const regenerateButton = el("button", {
    type: "button",
    className: "secondary",
    id: "capture-regenerate-answers",
    textContent: "Regenerate with answers",
    disabled: ![...state.answers.values()].some((value) => value.trim()),
  });
  regenerateButton.addEventListener("click", () => regenerate(capture.id, true));
  box.append(regenerateButton);
  section.append(box);
}

function appendGaps(section, capture) {
  const gaps = capture.plan.gaps || [];
  if (!gaps.length) return;
  section.append(el("h3", { textContent: "Missing records and gaps" }));
  const box = el("div", { className: "capture-gaps" });
  for (const gap of gaps) {
    const row = el("article", { className: "card" });
    row.append(el("p", {
      textContent: gap.text || gap.message || gap.action || gap.kind || "Plan gap",
    }));
    if (gap.action === "set_owner" || gap.kind === "SELF") {
      const name = el("input", { type: "text", "aria-label": "Owner name" });
      const save = el("button", { type: "button", textContent: "Save and re-analyze" });
      const message = el("p", { role: "status", className: "capture-error-text" });
      save.addEventListener("click", async () => {
        if (!name.value.trim()) {
          message.textContent = "Enter a name first.";
          return;
        }
        save.disabled = true;
        try {
          await captureApi.putSelf({ name: name.value.trim(), data: {} });
          await Promise.all([
            renderOwnerChip($("capture-owner")),
            renderOwnerChip($("capture-owner-detail")),
          ]);
          await regenerate(capture.id);
        } catch (error) {
          message.textContent = error.message || "Could not set the owner.";
        } finally {
          save.disabled = false;
        }
      });
      row.append(el("label", { textContent: "Owner name" }, name), save, message);
    }
    box.append(row);
  }
  section.append(box);
}

function appendKeepNote(section, capture) {
  const wrap = el("label", { className: "keep-note-option" });
  const checkbox = el("input", { type: "checkbox", checked: state.keepNote });
  checkbox.setAttribute("aria-label", "Also keep my text as a note");
  checkbox.addEventListener("change", () => {
    state.keepNote = checkbox.checked;
    scheduleValidation();
  });
  wrap.append(checkbox, document.createTextNode(" Also keep my text as a note"));
  section.append(wrap);
  if (capture.plan.note) {
    const note = capture.plan.note;
    section.append(el("p", {
      className: "meta",
      textContent: `${note.title || "Capture note"}${note.tags ? ` · ${(note.tags || []).join(", ")}` : ""} (saved text is the original capture)`,
    }));
  }
}

function appendPlanActions(section, capture) {
  const bar = el("div", { className: "capture-action-bar", id: "capture-action-bar" });
  bar.append(el("span", { id: "capture-selected-count", textContent: "0 selected" }));
  const apply = el("button", { type: "button", id: "capture-apply", textContent: "Apply selected" });
  apply.addEventListener("click", () => applyCapture(capture));
  const reject = el("button", {
    type: "button",
    className: "secondary",
    textContent: "Reject",
  });
  reject.addEventListener("click", async () => {
    if (!await confirmDialog("Reject capture", "Discard this plan? The raw text remains available until its retention period ends.", "Reject")) return;
    try {
      await captureApi.reject(capture.id, routeSignal());
      await loadHistory();
      location.hash = "#capture";
    } catch (error) { showError(error); }
  });
  const menu = el("details", { className: "capture-plan-menu" });
  menu.append(el("summary", { textContent: "More" }));
  const regenerateButton = el("button", {
    type: "button",
    className: "secondary",
    textContent: "Regenerate",
  });
  regenerateButton.addEventListener("click", () => regenerate(capture.id));
  const saveNote = el("button", {
    type: "button",
    className: "secondary",
    textContent: strings.saveAsNote,
  });
  saveNote.addEventListener("click", () => saveFailedCapture(capture.id));
  menu.append(regenerateButton, saveNote);
  bar.append(apply, reject, menu);
  section.append(bar);
  updateActionBar();
}

async function renderReadyPlan(capture, shouldValidate = true) {
  state.capture = capture;
  state.validationPending = shouldValidate;
  state.validationFailed = false;
  const options = routeSignal();
  const section = $("capture-plan");
  section.replaceChildren();
  section.hidden = false;
  let types = [];
  try { types = await refreshTypes(options); } catch (_error) { /* server validation remains authoritative */ }
  if (options.signal.aborted) return;
  state.typeCache = new Map(types.map((type) => [type.name, type]));
  const ids = new Set();
  for (const op of capture.plan.ops || []) {
    const type = state.typeCache.get(op.type);
    for (const [field, value] of Object.entries(op.data || op.patch || {})) {
      const property = type && type.json_schema.properties[field];
      if (property && property["x-ref"]) {
        for (const item of (Array.isArray(value) ? value : [value])) {
          if (typeof item === "string" && /^[a-f0-9]{8,64}$/.test(item)) ids.add(item);
        }
      }
    }
  }
  if (ids.size) {
    try {
      const response = await captureApi.labels([...ids], options);
      for (const [id, label] of Object.entries(response.labels || {})) state.labels.set(id, label);
    } catch (_error) { /* unresolved references use type labels, never raw ids */ }
  }
  if (options.signal.aborted) return;
  section.append(el("h2", { tabIndex: -1, textContent: capture.plan.summary || "Capture plan" }));
  const metadata = [];
  if (capture.llm_model) metadata.push(capture.llm_model);
  if (capture.prompt_tokens !== null || capture.completion_tokens !== null) {
    metadata.push(`${capture.prompt_tokens || 0}/${capture.completion_tokens || 0} tokens`);
  }
  if (metadata.length) section.append(el("p", { className: "meta", textContent: metadata.join(" · ") }));
  for (const warning of [...(capture.plan.warnings || []), ...(capture.warnings || [])]) {
    section.append(el("p", { className: "capture-warning", textContent: warning }));
  }
  appendPlanQuestions(section, capture);
  appendGaps(section, capture);
  const ops = el("div", { className: "capture-op-list" });
  for (const op of capture.plan.ops || []) ops.append(renderOp(op));
  if (ops.childElementCount) section.append(ops);
  appendKeepNote(section, capture);
  section.append(el("p", { id: "capture-edit-message", className: "meta", role: "status", "aria-live": "polite" }));
  appendPlanActions(section, capture);
  section.querySelector("h2").focus({ preventScroll: true });
  if (shouldValidate) await validatePlan();
}

function renderResult(capture) {
  const section = $("capture-plan");
  section.replaceChildren();
  section.hidden = false;
  section.append(el("h2", { textContent: "Capture results" }));
  if (state.lastApply && String(state.resultId) === String(capture.id)) {
    const list = el("ul");
    for (const item of state.lastApply.applied || []) {
      const row = el("li");
      row.append(document.createTextNode(`${operationBadge(item)} · ${item.label || item.type || "Updated"}`));
      let href = null;
      if (item.kind === "create_entity" || item.kind === "update_entity" || item.kind === "supersede_entity") {
        if (/^[a-f0-9]{8,64}$/.test(item.id || "")) href = `#entity/${item.id}`;
      } else if (item.kind === "create_note" && /^[a-f0-9]{8,64}$/.test(item.id || "")) {
        href = `#note/${item.id}`;
      } else if (item.kind === "extend_entity_type" && /^[a-z][a-z0-9_]{0,40}$/.test(item.type || "")) {
        href = `#types/${item.type}`;
      }
      if (href) row.append(document.createTextNode(" "), el("a", { href, textContent: "Open" }));
      list.append(row);
    }
    section.append(list);
    if ((state.lastApply.skipped || []).length) {
      section.append(el("h3", { textContent: "Skipped" }));
      for (const skipped of state.lastApply.skipped) {
        const id = typeof skipped === "string" ? skipped : skipped.op_id || skipped.id;
        const op = (capture.plan.ops || []).find((item) => String(item.id) === String(id));
        section.append(el("p", {
          textContent: typeof skipped === "string"
            ? `${op && op.label || op && op.type || "Operation"} was skipped.`
            : `${skipped.label || op && op.label || "Operation"}: ${skipped.reason || "skipped"}`,
        }));
      }
    }
  } else {
    section.append(el("p", { textContent: "This capture was applied. Results are no longer available in this browser session." }));
  }
  if (state.undoReport) {
    const report = state.undoReport;
    section.append(el("h3", { textContent: "Undo report" }));
    const labels = (report.undone || []).map((id) =>
      (capture.plan.ops || []).find((op) => String(op.id) === String(id)))
      .filter(Boolean).map((op) => op.label || op.type || operationBadge(op));
    section.append(el("p", {
      textContent: `Undone: ${labels.join(", ") || `${(report.undone || []).length} item(s)`}`,
    }));
    for (const [key, description] of [["kept_modified", "Kept because modified"], ["kept_in_use", "Kept because in use"], ["types_left", "Type changes left"]]) {
      if ((report[key] || []).length) section.append(el("p", {
        className: "capture-warning",
        textContent: `${description}: ${report[key].length} item(s).`,
      }));
    }
  }
  const actions = el("div", { className: "capture-result-actions" });
  if (capture.state === "applied") {
    const undo = el("button", { type: "button", className: "danger", textContent: "Undo" });
    undo.addEventListener("click", () => undoCapture(capture));
    actions.append(undo);
  }
  const newCapture = el("button", { type: "button", className: "secondary", textContent: "New capture" });
  newCapture.addEventListener("click", () => {
    state.lastApply = null;
    state.undoReport = null;
    state.resultId = null;
    location.hash = "#capture";
  });
  actions.append(newCapture);
  section.append(actions);
}

async function applyCapture(capture) {
  updateActionBar();
  const selected = (capture.plan.ops || []).filter((op) => state.selected.has(String(op.id)));
  if (!selected.length || selected.some((op) => op.status !== "valid")) return;
  const schemaOps = selected.filter((op) => ["schema_change", "new_type"].includes(op.risk));
  if (schemaOps.length) {
    const descriptions = schemaOps.flatMap((op) => {
      const properties = opOverride(op.id).add_properties || op.add_properties
        || op.json_schema && op.json_schema.properties || {};
      return Object.entries(properties).map(([field, property]) =>
        `${op.type || op.name}.${field} (${property.type}) — ${property.description || ""}`);
    });
    const message = descriptions.length ? descriptions.join("\n") : schemaOps.map((op) => op.name || op.type).join("\n");
    if (!await confirmDialog("Confirm schema changes", message, "Add field(s)")) return;
  }
  const overwrite = selected.find((op) => op.risk === "overwrites_text");
  if (overwrite && !await confirmDialog("Confirm text replacement",
    `This change overwrites ${((overwrite.diff || []).map((change) => change.field).filter(Boolean).join(", "))
      || "text"} in ${overwrite.label || overwrite.type}. Continue?`, "Overwrite")) return;
  const body = requestBody();
  body.allow_new_types = selected.some((op) => op.kind === "create_entity_type");
  state.busy = true;
  updateActionBar();
  try {
    state.lastApply = await captureApi.apply(capture.id, body, routeSignal());
    state.resultId = String(capture.id);
    state.undoReport = null;
    await Promise.all([loadHistory(), refreshTypes()]);
    await refreshCapture();
  } catch (error) {
    if (error instanceof CaptureApiError && error.code === "dependency") {
      const missing = error.details.missing || [];
      const ids = missing.flatMap((item) => item.requires || []);
      for (const id of ids) state.selected.add(String(id));
      $("capture-edit-message").textContent = "Required operations were added to the selection. Review and apply again.";
      await validatePlan();
    } else {
      const opId = error.details && error.details.op_id;
      if (opId) {
        const card = document.querySelector(`[data-op-id="${CSS.escape(String(opId))}"]`);
        if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      showError(error);
    }
  } finally {
    state.busy = false;
    updateActionBar();
  }
}

async function undoCapture(capture) {
  if (!await confirmDialog("Undo capture", "Undo the changes made by this capture?", "Undo")) return;
  try {
    state.undoReport = await captureApi.undo(capture.id, routeSignal());
    state.capture = null;
    await refreshTypes();
    await refreshCapture();
    await loadHistory();
  } catch (error) { showError(error); }
}

async function renderCapture(capture) {
  $("capture-state").replaceChildren();
  $("capture-state").hidden = true;
  $("capture-plan").replaceChildren();
  $("capture-plan").hidden = true;
  $("capture-message").textContent = "";
  $("capture-error").hidden = true;
  $("capture-title").textContent = `Capture · ${capture.state}`;
  if (state.capture && String(state.capture.id) !== String(capture.id)) {
    state.lastApply = null;
    state.undoReport = null;
    state.resultId = null;
  }

  if (ACTIVE_STATES.has(capture.state)) {
    if (!state.pollStarted) {
      state.pollStarted = Date.parse(capture.created_at) || Date.now();
    }
    statusMessage(capture.state, capture);
    return;
  }
  stopPolling();
  if (capture.state === "ready") {
    if (!state.capture || String(state.capture.id) !== String(capture.id)) {
      state.overrides = new Map();
      state.selected = new Set((capture.plan.ops || [])
        .filter((op) => op.include).map((op) => String(op.id)));
      state.answers = new Map();
      state.keepNote = Boolean(capture.plan.keep_note);
      state.validation = null;
      state.validationPending = false;
      state.validationFailed = false;
      state.lastApply = null;
      state.undoReport = null;
      state.resultId = null;
    }
    for (const [question, answer] of Object.entries(capture.answers || {})) {
      if (!state.answers.has(question)) state.answers.set(question, answer);
    }
    await renderReadyPlan(capture, true);
    return;
  }
  if (capture.state === "applied") {
    state.capture = capture;
    renderResult(capture);
    $("capture-message").textContent = "This capture has been applied.";
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
    if (capture.state === "undone" && state.undoReport) {
      renderResult(capture);
      return;
    }
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
    await renderCapture(capture);
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
    await renderCapture(capture);
  } catch (error) {
    if (error.name === "AbortError") return;
    $("capture-state").replaceChildren(el("p", { textContent: error.message }));
  }
}

async function regenerate(id, withAnswers = false) {
  try {
    const answers = {};
    if (withAnswers) {
      for (const [question, answer] of state.answers) {
        if (answer.trim()) answers[question] = answer.trim();
      }
    }
    const capture = await captureApi.regenerate(id, withAnswers ? { answers } : {}, routeSignal());
    state.pollStarted = Date.now();
    await renderCapture(capture);
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
  clearTimeout(state.badgeTimer);
  try {
    const result = await captureApi.list({ limit: 30 }, options);
    let needsReview = 0;
    for (const item of result.captures) {
      list.append(historyEntry(item));
      if (item.state === "ready") needsReview += 1;
    }
    $("capture-badge").textContent = needsReview ? String(needsReview) : "";
    if (!result.captures.length) list.append(el("p", { textContent: strings.noHistory }));
    if (!document.hidden) state.badgeTimer = setTimeout(() => loadHistory(), 60000);
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
    else {
      if (state.currentId) refreshCapture();
      loadHistory();
    }
  });
  restoreDraft();
  await Promise.all([
    loadConfig(routeSignal()),
    loadHistory(routeSignal()),
    renderOwnerChip($("capture-owner")),
    renderOwnerChip($("capture-owner-detail")),
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
