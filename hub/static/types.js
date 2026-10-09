import { captureApi } from "./capture/api.js";

const $ = (id) => document.getElementById(id);
let types = [];

function el(tag, properties = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(properties)) {
    if (key.startsWith("aria-")) node.setAttribute(key, value);
    else Object.assign(node, { [key]: value });
  }
  for (const child of children) {
    if (child) node.append(child);
  }
  return node;
}

export function typeByName(name) {
  return types.find((type) => type.name === name);
}

export async function refreshTypes() {
  const result = await captureApi.entityTypes();
  types = result.types || [];
  return types;
}

export function cachedTypes() {
  return types;
}

function typeSummary(type) {
  const required = new Set(type.json_schema.required || []);
  const rows = document.createElement("ul");
  for (const [name, field] of Object.entries(type.json_schema.properties || {})) {
    const origin = field["x-origin"];
    const line = el("li", {
      textContent: `${name} — ${field.type}${required.has(name) ? " !" : ""}`
        + `${name === type.unique_key ? " *" : ""}`
        + (field.description ? ` — ${field.description}` : "")
        + (origin ? ` (${origin})` : ""),
    });
    rows.append(line);
  }
  return rows;
}

async function showHistory(name, parent) {
  const historyBox = el("div", { className: "type-history" });
  historyBox.append(el("h4", { textContent: "Type history" }));
  parent.append(historyBox);
  try {
    const response = await captureApi.typeHistory(name);
    for (const entry of response.history || []) {
      const changes = [
        ...(entry.change.added || []).map((field) => `added ${field}`),
        ...(entry.change.removed || []).map((field) => `removed ${field}`),
      ];
      const row = el("p", {
        className: "meta",
        textContent: `v${entry.version} · ${entry.at} · ${entry.actor} · ${changes.join(", ") || "created"}`,
      });
      if (entry.capture_uid && /^[a-f0-9]{8,64}$/.test(entry.capture_uid)) {
        row.append(document.createTextNode(" "));
        row.append(el("a", {
          href: `#capture/${entry.capture_uid}`,
          textContent: "Open capture",
        }));
      }
      historyBox.append(row);
    }
  } catch (_error) {
    historyBox.append(el("p", { role: "status", textContent: "Type history is unavailable." }));
  }
}

export function renderTypeDetails(name, container) {
  container.replaceChildren();
  const type = typeByName(name);
  if (!type) return;
  const details = el("details", { className: "type-details" });
  details.append(el("summary", {
    textContent: `Type details · v${type.schema_version || 1}`,
  }));
  details.append(typeSummary(type));
  const actions = el("div", { className: "type-actions" });
  const add = el("button", { type: "button", className: "secondary", textContent: "Add field" });
  add.addEventListener("click", () => openAddField(type.name));
  const history = el("button", { type: "button", className: "secondary", textContent: "History" });
  history.addEventListener("click", () => showHistory(type.name, details));
  actions.append(add, history);
  details.append(actions);
  container.append(details);
}

function fieldDefinition(kind, description) {
  const definition = { description };
  if (kind === "list") return Object.assign(definition, { type: "array", items: { type: "string" } });
  if (kind === "date" || kind === "date-time") {
    return Object.assign(definition, { type: "string", format: kind });
  }
  return Object.assign(definition, {
    type: { text: "string", number: "number", integer: "integer", boolean: "boolean" }[kind],
  });
}

export function openAddField(typeName, onSaved = async () => {}) {
  const dialog = $("type-dialog");
  const name = el("input", { type: "text", required: true, autocomplete: "off" });
  name.setAttribute("aria-label", "Field name");
  name.placeholder = "lowercase_name";
  const kind = el("select", { "aria-label": "Field type" });
  for (const [value, label] of [
    ["text", "Text"], ["number", "Number"], ["integer", "Whole number"],
    ["boolean", "Yes/No"], ["date", "Date"], ["date-time", "Date & time"],
    ["list", "List of text"],
  ]) kind.append(new Option(label, value));
  const description = el("textarea", { rows: 2, required: true });
  description.setAttribute("aria-label", "Field description");
  const nameError = el("small", { className: "capture-error-text", role: "status" });
  const descriptionError = el("small", { className: "capture-error-text", role: "status" });
  const message = el("p", { role: "status", className: "capture-error-text" });
  const save = el("button", { type: "submit", textContent: "Add field" });
  const form = el("form", { className: "add-field-form" });
  for (const [label, input, help] of [
    ["Name", name, nameError], ["Type", kind, null], ["Description", description, descriptionError],
  ]) {
    const fieldLabel = el("label", { textContent: label });
    fieldLabel.append(input);
    form.append(fieldLabel);
    if (help) form.append(help);
  }
  form.append(message, save);
  dialog.replaceChildren(el("h2", { textContent: `Add field to ${typeName}` }), form);
  const close = el("button", { type: "button", className: "secondary", textContent: "Cancel" });
  close.addEventListener("click", () => dialog.close());
  dialog.append(close);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    message.textContent = "";
    save.disabled = true;
    try {
      const response = await captureApi.extendType(typeName, {
        add_properties: { [name.value]: fieldDefinition(kind.value, description.value) },
        confirm: true,
      });
      await refreshTypes();
      dialog.close();
      renderTypeDetails(typeName, $("e-type-details"));
      await onSaved(response);
    } catch (error) {
      const propertyMessage = error.message || "Could not add the field.";
      message.textContent = "";
      if (propertyMessage.toLowerCase().includes("description")) {
        descriptionError.textContent = propertyMessage;
        nameError.textContent = "";
      } else if (propertyMessage.toLowerCase().includes("property name")
          || propertyMessage.toLowerCase().includes("reserved")
          || propertyMessage.toLowerCase().includes("existing")) {
        nameError.textContent = propertyMessage;
        descriptionError.textContent = "";
      } else {
        message.textContent = propertyMessage;
      }
    } finally {
      save.disabled = false;
    }
  });
  dialog.showModal();
}

export async function openTypeRoute(name) {
  if (!/^[a-z][a-z0-9_]{0,40}$/.test(name)) return;
  if (!types.length) await refreshTypes();
  const type = typeByName(name);
  if (!type) return;
  $("e-type").value = name;
  renderTypeDetails(name, $("e-type-details"));
  location.hash = "#entities";
  $("tab-entities").hidden = false;
  document.querySelector('#tabs button[data-tab="entities"]').classList.add("active");
  for (const section of document.querySelectorAll("main > section")) {
    if (section.id.startsWith("tab-")) section.hidden = section.id !== "tab-entities";
  }
}
