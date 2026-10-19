import { apiRequest, captureApi } from "./capture/api.js";
import { confirmDialog } from "./capture/dialog.js";

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

export async function refreshTypes(options) {
  const result = await captureApi.entityTypes(options);
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
    const line = el("li");
    line.append(el("strong", { textContent: name }));
    line.append(document.createTextNode(` — ${field.type}`));
    if (required.has(name)) line.append(el("span", { className: "badge", textContent: "Required" }));
    if (name === type.unique_key) line.append(el("span", { className: "badge", textContent: "Unique" }));
    if (field.description) line.append(document.createTextNode(` — ${field.description}`));
    if (field["x-origin"]) line.append(el("span", {
      className: "badge",
      textContent: field["x-origin"],
    }));
    rows.append(line);
  }
  return rows;
}

async function showHistory(name, parent) {
  parent.querySelector(".type-history")?.remove();
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
  const option = [...$("e-type").options].find((item) => item.value === name);
  if (option) option.textContent = `${name} (v${type.schema_version || 1}) · ${
    localStorage.getItem("pkb.showArchived") === "true"
      ? `${type.counts?.current || 0} current · ${type.counts?.archived || 0} archived`
      : type.count
  }`;
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
  const policyForm = el("form", { className: "type-policy" });
  const policyValues = {};
  const help = {
    searchable: "Include this type in search indexing.",
    capture_visible: "Allow Smart Capture to see and change this type.",
    agent_visible: "Allow agent tools and MCP to see and change this type.",
    auditable: "Keep audit history for entity changes.",
  };
  for (const key of ["searchable", "capture_visible", "agent_visible", "auditable"]) {
    const label = el("label");
    const input = el("input", { type: "checkbox" });
    input.checked = type.policy?.[key] ?? (key === "searchable" ? type.embed : true);
    label.append(input, document.createTextNode(key.replaceAll("_", " ")));
    label.title = help[key];
    policyForm.append(label, el("small", { textContent: help[key] }));
    policyValues[key] = input;
  }
  const savePolicy = el("button", { type: "submit", textContent: "Save policies" });
  const policyMessage = el("p", { role: "status", className: "meta" });
  policyForm.append(savePolicy, policyMessage);
  policyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const next = Object.fromEntries(
      Object.entries(policyValues).map(([key, input]) => [key, input.checked]),
    );
    if (type.policy?.auditable && !next.auditable
        && !await confirmDialog("Disable audit history",
          "Future entity changes for this type will no longer be recorded in audit history.",
          "Disable audit")) return;
    savePolicy.disabled = true;
    try {
      await apiRequest(`/api/entity-types/${encodeURIComponent(type.name)}`, {
        method: "PATCH", body: JSON.stringify({ policy: next }),
      });
      await refreshTypes();
      renderTypeDetails(type.name, container);
    } catch (error) {
      policyMessage.textContent = error.message;
    } finally {
      savePolicy.disabled = false;
    }
  });
  details.append(el("h4", { textContent: "Type policies" }), policyForm);
  if (!type.builtin) {
    const remove = el("button", {
      type: "button", className: "danger", textContent: "Delete type…",
    });
    remove.addEventListener("click", () => openDeleteType(type));
    const danger = el("section", { className: "type-danger-zone" });
    danger.append(el("h4", { textContent: "Danger zone" }), remove);
    details.append(danger);
  }
  details.append(actions);
  container.append(details);
}

function allRefs(schema, name) {
  if (!schema || typeof schema !== "object") return false;
  if (schema["x-ref"] === name) return true;
  return Object.values(schema).some((value) => allRefs(value, name));
}

function openDeleteType(type) {
  const dialog = $("type-dialog");
  const counts = type.counts || { current: type.count || 0, archived: 0, superseded: 0 };
  const total = Object.values(counts).reduce((sum, count) => sum + count, 0);
  const references = types
    .filter((other) => other.name !== type.name && allRefs(other.json_schema, type.name))
    .map((other) => other.name);
  const form = el("form", { className: "delete-type-form" });
  const nameInput = el("input", { type: "text", required: true, autocomplete: "off" });
  const nameLabel = el("label", { textContent: "Type the entity type name to confirm" });
  nameLabel.append(nameInput);
  form.append(
    el("p", { textContent: `Entities: ${counts.current || 0} current · ${counts.archived || 0} archived · ${counts.superseded || 0} superseded` }),
  );
  const exportButton = el("button", { type: "button", className: "secondary", textContent: "Export JSON first" });
  exportButton.addEventListener("click", async () => {
    exportButton.disabled = true;
    try {
      let offset = 0;
      let totalItems = 0;
      const items = [];
      do {
        const page = await apiRequest(
          `/api/entities?type=${encodeURIComponent(type.name)}&limit=200&offset=${offset}`,
        );
        items.push(...page.items);
        totalItems = page.total;
        offset += page.items.length;
      } while (offset < totalItems && offset > 0);
      const link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([JSON.stringify(items, null, 2)], {
        type: "application/json",
      }));
      link.download = `${type.name}-entities.json`;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (error) {
      message.textContent = error.message;
    } finally {
      exportButton.disabled = false;
    }
  });
  form.append(exportButton);
  let cascade;
  let convert;
  if (total) {
    cascade = el("input", { type: "checkbox" });
    const cascadeLabel = el("label", {
      textContent: `Also permanently delete ${total} ${total === 1 ? "entity" : "entities"}`,
    });
    cascadeLabel.prepend(cascade);
    form.append(cascadeLabel);
    convert = el("input", { type: "checkbox" });
    const convertLabel = el("label", { textContent: "Convert entities to notes before deleting" });
    convertLabel.prepend(convert);
    form.append(convertLabel);
  }
  form.append(nameLabel);
  const message = el("p", { role: "status", className: "capture-error-text" });
  const submit = el("button", { type: "submit", className: "danger", textContent: "Delete type" });
  const cancel = el("button", { type: "button", className: "secondary", textContent: "Cancel" });
  cancel.addEventListener("click", () => dialog.close());
  const blocked = type.builtin || references.length > 0;
  if (blocked) {
    message.textContent = type.builtin
      ? "Built-in types cannot be deleted."
      : `Referenced by: ${references.join(", ")}. Remove these references first.`;
    submit.hidden = true;
  }
  form.append(message, submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (nameInput.value !== type.name) {
      message.textContent = "Type name must match exactly.";
      return;
    }
    if (total && !cascade.checked) {
      message.textContent = "Confirm permanent deletion of the listed entities.";
      return;
    }
    submit.disabled = true;
    const params = new URLSearchParams({ confirm: "true" });
    if (cascade?.checked) params.set("cascade", "true");
    if (convert?.checked) params.set("convert_to_notes", "true");
    try {
      const result = await apiRequest(
        `/api/entity-types/${encodeURIComponent(type.name)}?${params}`,
        { method: "DELETE" },
      );
      dialog.close();
      window.dispatchEvent(new CustomEvent("entity-type-deleted", { detail: result }));
    } catch (error) {
      const reasons = error.details?.reasons;
      message.textContent = reasons
        ? `Blocked: ${reasons.map((reason) => reason.kind).join(", ")}.`
        : error.message;
      if (reasons?.some((reason) => reason.kind !== "entities")) submit.hidden = true;
    } finally {
      submit.disabled = false;
    }
  });
  dialog.replaceChildren(
    el("h2", { textContent: "Delete entity type" }),
    el("p", {}, el("strong", { textContent: type.description || type.name })),
    el("p", { textContent: "This may break an app that depends on this type." }),
    form, cancel,
  );
  dialog.showModal();
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
