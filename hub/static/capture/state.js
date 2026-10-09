function idOf(value) {
  return String(value);
}

export function closureUp(selected, ops) {
  const byId = new Map(ops.map((op) => [idOf(op.id), op]));
  const result = new Set([...selected].map(idOf));
  const pending = [...result];
  while (pending.length) {
    const op = byId.get(pending.pop());
    for (const required of (op && op.requires) || []) {
      const id = idOf(required);
      if (!result.has(id)) {
        result.add(id);
        pending.push(id);
      }
    }
  }
  return result;
}

export function closureDown(selected, ops, removed) {
  const result = new Set([...selected].map(idOf));
  const pending = [...removed].map(idOf);
  while (pending.length) {
    const id = pending.pop();
    if (!result.delete(id)) continue;
    for (const op of ops) {
      const opId = idOf(op.id);
      if (result.has(opId) && (op.requires || []).some((required) => idOf(required) === id)) {
        pending.push(opId);
      }
    }
  }
  for (const op of ops) {
    if (!op.gap || !result.has(idOf(op.id))) continue;
    const neededBy = op.gap.needed_by || op.needed_by || [];
    if (neededBy.length && neededBy.every((required) => !result.has(idOf(required)))) {
      result.delete(idOf(op.id));
    }
  }
  return result;
}

export function invalidRequirements(op, ops) {
  const byId = new Map(ops.map((candidate) => [idOf(candidate.id), candidate]));
  return (op.requires || []).map(idOf)
    .map((id) => byId.get(id))
    .filter((required) => required && required.status !== "valid");
}
