export function fieldControl(name, property, value, idPrefix = "") {
  let input;
  const kind = property.type;
  if (property.enum) {
    input = document.createElement("select");
    input.append(new Option("Choose…", ""));
    for (const option of property.enum) {
      input.append(new Option(String(option), String(option)));
    }
  } else if (kind === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
  } else if (kind === "number" || kind === "integer") {
    input = document.createElement("input");
    input.type = "number";
    input.step = kind === "integer" ? "1" : "any";
  } else if (kind === "string" && ["notes", "description", "text", "content"].includes(name)) {
    input = document.createElement("textarea");
    input.rows = 3;
  } else if (kind === "string") {
    input = document.createElement("input");
    input.type = property.format === "date" ? "date" : "text";
    if (property.format === "date-time") input.placeholder = "YYYY-MM-DDTHH:mm:ssZ";
  } else if (kind === "array" && property.items && ["string", "number", "integer", "boolean"].includes(property.items.type)) {
    input = document.createElement("input");
    input.type = "text";
    input.placeholder = "Comma-separated values";
  } else {
    input = document.createElement("textarea");
    input.rows = 3;
    input.placeholder = "JSON";
  }

  input.id = `${idPrefix ? `${idPrefix}-` : ""}field-${name}`;
  input.setAttribute("aria-label", name);
  if (kind === "boolean" && !property.enum) {
    input.checked = value === true;
  } else if (kind === "array" && property.items) {
    input.value = Array.isArray(value) ? value.map((item) =>
      item && typeof item === "object" ? "" : String(item)).filter(Boolean).join(", ") : "";
  } else if (input instanceof HTMLTextAreaElement && kind !== "string") {
    input.value = value === undefined ? "" : JSON.stringify(value, null, 2);
  } else {
    input.value = value === undefined || value === null ? "" : String(value);
  }

  return {
    input,
    read() {
      if (kind === "boolean" && !property.enum) return input.checked;
      if (kind === "number" || kind === "integer") {
        if (!input.value.trim()) return undefined;
        const number = Number(input.value);
        if (!Number.isFinite(number) || (kind === "integer" && !Number.isInteger(number))) {
          throw new Error(`${name} must be a ${kind}.`);
        }
        return number;
      }
      if (kind === "array" && property.items) {
        const values = input.value.split(",").map((item) => item.trim()).filter(Boolean);
        if (property.items.type === "string") return values;
        return values.map((item) => {
          if (property.items.type === "boolean") {
            if (!["true", "false"].includes(item.toLowerCase())) {
              throw new Error(`${name} values must be true or false.`);
            }
            return item.toLowerCase() === "true";
          }
          const number = Number(item);
          if (!Number.isFinite(number)
              || (property.items.type === "integer" && !Number.isInteger(number))) {
            throw new Error(`${name} contains an invalid number.`);
          }
          return number;
        });
      }
      if (kind === "string" || property.enum) return input.value || undefined;
      if (!input.value.trim()) return undefined;
      try {
        return JSON.parse(input.value);
      } catch (_error) {
        throw new Error(`${name} must contain valid JSON.`);
      }
    },
  };
}

export function schemaField(name, property, value, required = false, idPrefix = "") {
  const label = document.createElement("label");
  label.htmlFor = `${idPrefix ? `${idPrefix}-` : ""}field-${name}`;
  label.append(document.createTextNode(name + (required ? " *" : "")));
  const control = fieldControl(name, property, value, idPrefix);
  control.input.required = required;
  const wrapper = document.createElement("div");
  wrapper.className = "schema-field";
  wrapper.append(label, control.input);
  if (property.description) {
    const help = document.createElement("small");
    help.className = "meta";
    help.textContent = property.description;
    wrapper.append(help);
  }
  return { wrapper, ...control };
}
