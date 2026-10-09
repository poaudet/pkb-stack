function display(value) {
  if (value === undefined || value === null) return "—";
  if (Array.isArray(value)) return value.map((item) => String(item)).join(", ") || "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export function renderDiff(changes) {
  const table = document.createElement("table");
  table.className = "capture-diff";
  const body = document.createElement("tbody");
  for (const change of changes || []) {
    const row = document.createElement("tr");
    [change.field, display(change.old), display(change.new)].forEach((value, index) => {
      const cell = document.createElement(index === 0 ? "th" : "td");
      cell.textContent = String(value);
      row.append(cell);
    });
    body.append(row);
  }
  table.append(body);
  return table;
}
