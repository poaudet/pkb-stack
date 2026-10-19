function equal(left, right) {
  if (left === right) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => equal(value, right[index]));
  }
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key, index) => key === rightKeys[index] && equal(left[key], right[key]));
}

export function diffEntity(original, edited, required = []) {
  const patch = {};
  const requiredFields = new Set(required);
  for (const key of new Set([...Object.keys(original), ...Object.keys(edited)])) {
    const value = edited[key];
    if (value === undefined) {
      if (Object.hasOwn(original, key) && !requiredFields.has(key)) patch[key] = null;
      continue;
    }
    if (!equal(original[key], value)) patch[key] = value;
  }
  return patch;
}

function localDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function scalarInput(name, property, initial, refOptions) {
  const input = document.createElement("input");
  const kind = property.type;
  if (kind === "boolean") {
    input.type = "checkbox";
    input.checked = Boolean(initial);
    input.indeterminate = initial === undefined;
    input.addEventListener("change", () => { input.indeterminate = false; });
  } else if (kind === "number" || kind === "integer") {
    input.type = "number";
    input.step = kind === "integer" ? "1" : "any";
    if (property.minimum !== undefined) input.min = property.minimum;
    if (property.maximum !== undefined) input.max = property.maximum;
    input.value = initial ?? "";
  } else if (property.format === "date") {
    input.type = "date";
    input.value = initial ?? "";
  } else if (property.format === "date-time") {
    input.type = "datetime-local";
    input.value = localDateTime(initial);
  } else {
    input.type = "text";
    input.value = initial ?? "";
  }
  if (property["x-ref"]) {
    const listId = `entity-ref-${name}`;
    input.setAttribute("list", listId);
    const datalist = document.createElement("datalist");
    datalist.id = listId;
    for (const option of refOptions[property["x-ref"]] || []) {
      const item = document.createElement("option");
      item.value = option.id;
      item.label = option.label;
      datalist.append(item);
    }
    return {
      input,
      datalist,
      read: () => input.value === "" ? undefined : input.value,
    };
  }
  return {
    input,
    read() {
      if (kind === "boolean") return input.indeterminate ? undefined : input.checked;
      if (kind === "number" || kind === "integer") {
        return input.value === "" ? undefined : Number(input.value);
      }
      if (property.format === "date-time") {
        return input.value ? new Date(input.value).toISOString() : undefined;
      }
      return input.value === "" ? undefined : input.value;
    },
  };
}

function fieldControl(name, property, initial, refOptions) {
  if (Array.isArray(property.enum)) {
    const input = document.createElement("select");
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "Choose…";
    input.append(empty);
    for (const [index, value] of property.enum.entries()) {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = value;
      input.append(option);
    }
    input.value = String(property.enum.findIndex((value) => value === initial));
    if (initial === undefined) input.value = "";
    return {
      input,
      read: () => input.value === "" ? undefined : property.enum[Number(input.value)],
    };
  }
  if (property.type === "array" && property.items) {
    const input = document.createElement("textarea");
    input.rows = 4;
    input.value = Array.isArray(initial) ? initial.join("\n") : "";
    return {
      input,
      read() {
        const items = input.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
        if (!items.length) return undefined;
        if (property.items.type === "string") return items;
        if (property.items.type === "boolean") return items.map((item) => item === "true");
        return items.map(Number);
      },
    };
  }
  if (property.type === "object" && property.properties) {
    const fieldset = document.createElement("fieldset");
    const fields = {};
    for (const [childName, childProperty] of Object.entries(property.properties)) {
      const label = document.createElement("label");
      label.textContent = childName;
      const child = scalarInput(
        `${name}-${childName}`, childProperty, initial?.[childName], refOptions,
      );
      label.append(child.input);
      if (child.datalist) label.append(child.datalist);
      fieldset.append(label);
      fields[childName] = child;
    }
    return {
      input: fieldset,
      read() {
        const value = {};
        for (const [childName, child] of Object.entries(fields)) {
          const item = child.read();
          if (item !== undefined) value[childName] = item;
        }
        return Object.keys(value).length ? value : undefined;
      },
    };
  }
  if (property.type === "string" && property.maxLength > 120
      && !property.format && !property.enum) {
    const input = document.createElement("textarea");
    input.rows = 4;
    input.maxLength = property.maxLength;
    input.value = initial ?? "";
    return { input, read: () => input.value === "" ? undefined : input.value };
  }
  if (["string", "number", "integer", "boolean"].includes(property.type)) {
    return scalarInput(name, property, initial, refOptions);
  }
  const input = document.createElement("textarea");
  input.rows = 4;
  input.value = initial === undefined ? "" : JSON.stringify(initial, null, 2);
  return {
    input,
    read() {
      return input.value.trim() ? JSON.parse(input.value) : undefined;
    },
  };
}

export function createEntityEditor(schema, data, refOptions = {}) {
  const form = document.createElement("form");
  const fields = {};
  const required = new Set(schema.required || []);
  for (const [name, property] of Object.entries(schema.properties || {})) {
    const id = `entity-field-${name}`;
    const label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = `${name}${required.has(name) ? " *" : ""}`;
    const control = fieldControl(name, property, data[name], refOptions);
    control.input.id = id;
    if (required.has(name) && "required" in control.input) control.input.required = true;
    form.append(label, control.input);
    if (control.datalist) form.append(control.datalist);
    if (property.description) {
      const help = document.createElement("small");
      help.textContent = property.description;
      form.append(help);
    }
    fields[name] = control;
  }
  return {
    form,
    values() {
      const values = {};
      for (const [name, field] of Object.entries(fields)) {
        const value = field.read();
        if (value !== undefined) values[name] = value;
      }
      return values;
    },
    required: [...required],
  };
}
