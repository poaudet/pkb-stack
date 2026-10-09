import { captureApi } from "./capture/api.js";
import { schemaField } from "./capture/schemaform.js";

const $ = (id) => document.getElementById(id);
let cachedOwner = null;

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

async function loadOwner() {
  const [ownerResult, typesResult] = await Promise.all([
    captureApi.getSelf(),
    captureApi.entityTypes(),
  ]);
  cachedOwner = ownerResult;
  return {
    owner: ownerResult.self,
    conflicts: ownerResult.conflicts,
    person: (typesResult.types || []).find((type) => type.name === "person"),
  };
}

export function currentOwner() {
  return cachedOwner && cachedOwner.self;
}

export async function renderOwnerChip(container) {
  container.replaceChildren();
  try {
    const { owner } = await loadOwner();
    if (owner) {
      container.append(el("p", {
        className: "owner-chip",
        textContent: `Facts about you go to: ${owner.data.name}`,
      }));
      return;
    }
    const prompt = el("p", { className: "owner-chip owner-missing" });
    prompt.append(document.createTextNode("Set up the owner record to capture facts about you. "));
    const setup = el("button", { type: "button", className: "secondary", textContent: "Set up" });
    setup.addEventListener("click", () => openOwnerDialog());
    prompt.append(setup);
    container.append(prompt);
  } catch (_error) {
    container.append(el("p", { className: "meta", textContent: "Owner settings unavailable." }));
  }
}

function buildOwnerFields(person, owner, container) {
  const values = [];
  const schema = person && person.json_schema || { properties: {} };
  const data = owner && owner.data || {};
  for (const [name, property] of Object.entries(schema.properties || {})) {
    if (["name", "relationship", "contact"].includes(name)) continue;
    const field = schemaField(name, property, data[name],
      (schema.required || []).includes(name));
    container.append(field.wrapper);
    values.push([name, field]);
  }
  return values;
}

export async function renderOwnerForm(container, onSaved = () => {}) {
  container.replaceChildren();
  let result;
  try {
    result = await loadOwner();
  } catch (_error) {
    container.append(el("p", { role: "status", textContent: "Owner settings unavailable." }));
    return;
  }

  const form = el("form", { className: "owner-form" });
  const nameLabel = el("label", { htmlFor: "owner-name", textContent: "Name *" });
  const name = el("input", {
    id: "owner-name",
    type: "text",
    required: true,
    value: result.owner && result.owner.data.name || "",
    "aria-label": "Owner name",
  });
  form.append(nameLabel, name);
  const fields = buildOwnerFields(result.person, result.owner, form);
  const message = el("p", { className: "capture-error-text", role: "status" });
  const save = el("button", {
    type: "submit",
    textContent: result.owner ? "Save owner details" : "Set owner",
  });
  form.append(save, message);
  if (!result.owner) {
    form.prepend(el("p", {
      className: "capture-warning",
      textContent: "No owner record is configured. First-person facts cannot be linked yet.",
    }));
  }
  if (result.conflicts.length) {
    const warning = el("p", { className: "capture-warning", textContent: "Multiple owner records exist: " });
    result.conflicts.forEach((id, index) => {
      if (/^[a-f0-9]{8,64}$/.test(id)) {
        const link = el("a", { href: `#entity/${id}`, textContent: `record ${index + 1}` });
        warning.append(link);
        if (index < result.conflicts.length - 1) warning.append(document.createTextNode(", "));
      }
    });
    form.prepend(warning);
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    save.disabled = true;
    message.textContent = "";
    try {
      const data = {};
      for (const [fieldName, field] of fields) {
        const value = field.read();
        if (value !== undefined) data[fieldName] = value;
      }
      await captureApi.putSelf({ name: name.value, data });
      await Promise.all([
        renderOwnerChip($("capture-owner")),
        renderOwnerChip($("capture-owner-detail")),
      ]);
      await renderOwnerForm(container, onSaved);
      onSaved();
      if ($("owner-dialog").open) $("owner-dialog").close();
    } catch (error) {
      message.textContent = error.message || "Could not save the owner record.";
    } finally {
      save.disabled = false;
    }
  });
  container.append(form);
}

export async function openOwnerDialog(onSaved = () => {}) {
  const dialog = $("owner-dialog");
  dialog.replaceChildren(el("h2", { textContent: "Owner record" }));
  const form = el("div");
  dialog.append(form);
  const close = el("button", {
    type: "button",
    className: "secondary",
    textContent: "Close",
  });
  close.addEventListener("click", () => dialog.close());
  dialog.append(close);
  await renderOwnerForm(form, onSaved);
  dialog.showModal();
}
