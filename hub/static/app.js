"use strict";
const $ = (id) => document.getElementById(id);

function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  Object.assign(e, props || {});
  for (const k of kids) e.append(k);
  return e;
}

async function api(path, opts = {}, retried = false) {
  const headers = Object.assign({ "X-Actor": "ui" }, opts.headers || {});
  const tok = localStorage.getItem("hub_token");
  if (tok) headers["Authorization"] = "Bearer " + tok;
  if (opts.body) headers["Content-Type"] = "application/json";
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (r.status === 401 && !retried) {
    const t = window.prompt("API token required");
    if (t) {
      localStorage.setItem("hub_token", t);
      return api(path, opts, true);
    }
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((data.error && data.error.message) || r.statusText);
  return data;
}

function showTab(name) {
  for (const b of document.querySelectorAll("#tabs button")) {
    b.classList.toggle("active", b.dataset.tab === name);
  }
  for (const s of document.querySelectorAll("main > section")) {
    s.hidden = s.id !== "tab-" + name;
  }
  if (name === "browse") loadBrowse();
  if (name === "review") loadReview();
  if (name === "entities") loadEntities();
  if (name === "status") loadStatus();
}

function card(n, snippet) {
  const c = el("div", { className: "card" + (n.status !== "current" ? " old" : "") });
  c.append(el("h3", { textContent: n.title }));
  const meta = el("div", { className: "meta" });
  meta.append(el("span", { className: "badge", textContent: n.status }));
  meta.append(document.createTextNode((n.tags || []).join(", ")));
  if (n.score !== undefined) {
    meta.append(document.createTextNode(" · score " + n.score.toFixed(3)));
  }
  c.append(meta);
  if (snippet) c.append(el("div", { className: "snip", textContent: snippet }));
  c.addEventListener("click", () => (n.kind === "entity" ? openEntity(n.id) : openDetail(n.id)));
  return c;
}

let timer = null;
async function doSearch() {
  const q = $("q").value.trim();
  const box = $("hits");
  box.replaceChildren();
  if (!q) return;
  try {
    const res = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({ q, limit: 20, include_superseded: $("inc-sup").checked }),
    });
    for (const h of res.hits) box.append(card(h, h.snippet));
    if (!res.hits.length) box.append(el("p", { textContent: "No results." }));
  } catch (e) {
    box.append(el("p", { textContent: e.message }));
  }
}

async function openDetail(id) {
  const dlg = $("detail");
  const { note } = await api("/api/notes/" + encodeURIComponent(id));
  dlg.replaceChildren();
  const title = el("input", { type: "text", value: note.title });
  const content = el("textarea", { rows: 12, value: note.content });
  const tags = el("input", { type: "text", value: note.tags.join(", ") });
  const msg = el("p", { className: "meta" });
  const act = async (fn) => {
    try { await fn(); dlg.close(); doSearch(); loadBrowse(); } catch (e) { msg.textContent = e.message; }
  };
  const tagList = () => tags.value.split(",").map((t) => t.trim()).filter(Boolean);
  const save = el("button", { textContent: "Save edit" });
  save.addEventListener("click", () => act(() => api("/api/notes/" + id, { method: "PATCH",
    body: JSON.stringify({ title: title.value, content: content.value, tags: tagList() }) })));
  const sup = el("button", { className: "secondary", textContent: "Supersede with edited text" });
  sup.addEventListener("click", () => act(() => api("/api/notes/" + id + "/supersede", {
    method: "POST", body: JSON.stringify({ title: title.value, content: content.value, tags: tagList() }) })));
  const arch = el("button", { className: "secondary",
    textContent: note.status === "archived" ? "Restore" : "Archive" });
  arch.addEventListener("click", () => act(() => api("/api/notes/" + id + "/" +
    (note.status === "archived" ? "restore" : "archive"), { method: "POST", body: "{}" })));
  const del = el("button", { className: "danger", textContent: "Forget" });
  del.addEventListener("click", () => {
    if (window.confirm("Permanently delete this note and all traces?")) {
      act(() => api("/api/notes/" + id + "?confirm=true", { method: "DELETE" }));
    }
  });
  const close = el("button", { className: "secondary", textContent: "Close" });
  close.addEventListener("click", () => dlg.close());
  dlg.append(el("span", { className: "badge", textContent: note.status }), title, content, tags,
    save, sup, arch, del, close, msg);
  dlg.showModal();
}

async function saveNote() {
  const msg = $("add-msg");
  try {
    const body = { content: $("add-content").value, title: $("add-title").value,
      tags: $("add-tags").value.split(",").map((t) => t.trim()).filter(Boolean) };
    if (llmOn && $("add-tidy").checked) body.preprocess = true;
    const r = await api("/api/notes", { method: "POST", body: JSON.stringify(body) });
    msg.textContent = r.pending ? "Queued for AI tidy-up. See Review." : "Saved.";
    if (r.pending) refreshReviewCount();
    $("add-content").value = ""; $("add-title").value = ""; $("add-tags").value = "";
  } catch (e) {
    msg.textContent = e.message;
  }
}

async function loadBrowse() {
  const list = $("b-list");
  list.replaceChildren();
  const p = new URLSearchParams({ limit: "100" });
  if ($("b-status").value) p.set("status", $("b-status").value);
  if ($("b-tag").value.trim()) p.set("tag", $("b-tag").value.trim());
  try {
    const res = await api("/api/notes?" + p.toString());
    for (const n of res.items) list.append(card(n));
    if (!res.items.length) list.append(el("p", { textContent: "No notes." }));
  } catch (e) {
    list.append(el("p", { textContent: e.message }));
  }
}

async function loadStatus() {
  try {
    const h = await api("/health");
    const s = await api("/api/stats");
    $("health").textContent = JSON.stringify({ health: h, stats: s }, null, 2);
    $("llm-badge").textContent = !h.llm.configured ? "AI tidy-up: off"
      : h.llm.local ? "LLM: local (" + h.llm.model + ")"
      : "LLM: remote: note text leaves this machine (" + h.llm.model + ")";
  } catch (e) {
    $("health").textContent = e.message;
  }
}

for (const b of document.querySelectorAll("#tabs button")) {
  b.addEventListener("click", () => showTab(b.dataset.tab));
}
$("q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(doSearch, 300); });
$("inc-sup").addEventListener("change", doSearch);
$("add-save").addEventListener("click", saveNote);
$("b-status").addEventListener("change", loadBrowse);
$("b-tag").addEventListener("input", loadBrowse);
$("backup").addEventListener("click", async () => {
  try { const r = await api("/api/admin/backup", { method: "POST", body: "{}" }); $("backup-msg").textContent = "Wrote " + r.file; }
  catch (e) { $("backup-msg").textContent = e.message; }
});
$("token-save").addEventListener("click", () => { localStorage.setItem("hub_token", $("token").value); loadStatus(); });

let llmOn = false;
let reviewTimer = null;

async function refreshReviewCount() {
  if (!llmOn) return;
  try {
    const res = await api("/api/pending?limit=200");
    const open = res.items.filter((p) => ["queued", "processing", "ready", "failed"].includes(p.state));
    $("review-count").textContent = open.length ? "(" + open.length + ")" : "";
    clearTimeout(reviewTimer);
    if (open.some((p) => p.state === "queued" || p.state === "processing")) {
      reviewTimer = setTimeout(() => {
        refreshReviewCount();
        if (!$("tab-review").hidden && !$("review-detail").open) loadReview();
      }, 3000);
    }
  } catch (e) { /* ignore */ }
}

async function loadReview() {
  const list = $("r-list");
  list.replaceChildren();
  try {
    const res = await api("/api/pending?limit=100");
    const items = res.items.filter((p) => ["queued", "processing", "ready", "failed"].includes(p.state));
    for (const p of items) {
      const c = el("div", { className: "card" });
      c.append(el("h3", { textContent: p.suggestion.title || "(untitled)" }));
      c.append(el("div", { className: "meta" }, el("span", { className: "badge", textContent: p.state }),
        document.createTextNode(p.error ? " " + p.error : "")));
      if (p.state === "ready" || p.state === "failed") c.addEventListener("click", () => openPending(p.id));
      list.append(c);
    }
    if (!items.length) list.append(el("p", { textContent: "Nothing to review." }));
  } catch (e) {
    list.append(el("p", { textContent: e.message }));
  }
  refreshReviewCount();
}

async function openPending(id) {
  const dlg = $("review-detail");
  const { pending: p } = await api("/api/pending/" + encodeURIComponent(id));
  dlg.replaceChildren();
  const title = el("input", { type: "text", value: p.suggestion.title || "" });
  const content = el("textarea", { rows: 10, value: p.suggestion.content || "" });
  const tags = el("input", { type: "text", value: p.suggestion.tags.join(", ") });
  const msg = el("p", { className: "meta" });
  const tagList = () => tags.value.split(",").map((t) => t.trim()).filter(Boolean);
  const replace = new Map();
  const act = async (fn) => {
    try { await fn(); dlg.close(); loadReview(); } catch (e) { msg.textContent = e.message; }
  };
  dlg.append(el("span", { className: "badge", textContent: p.state }));
  if (p.error) dlg.append(el("p", { className: "meta", textContent: "Error: " + p.error }));
  dlg.append(el("h4", { textContent: "Original" }), el("pre", { textContent: p.raw_content }),
    el("h4", { textContent: "Suggestion" }), title, content, tags);
  const missing = (p.warnings && p.warnings.missing) || [];
  if (missing.length) {
    dlg.append(el("h4", { textContent: "Possibly missing from the suggestion" }));
    const ul = el("ul");
    for (const m of missing) ul.append(el("li", { textContent: m }));
    dlg.append(ul);
  }
  if (p.related.length) {
    dlg.append(el("h4", { textContent: "Related notes" }));
    for (const r of p.related) {
      const cb = el("input", { type: "checkbox" });
      cb.addEventListener("change", () => (cb.checked ? replace.set(r.id, 1) : replace.delete(r.id)));
      const sim = r.similarity === null ? "keyword" : r.similarity.toFixed(2);
      dlg.append(el("label", {}, cb, document.createTextNode(" Replace this note: " + r.title + " (" + sim + ")")));
    }
  }
  const approve = el("button", { textContent: "Approve" });
  approve.addEventListener("click", () => act(() => api("/api/pending/" + id + "/approve", {
    method: "POST", body: JSON.stringify({ title: title.value, content: content.value,
      tags: tagList(), supersede: [...replace.keys()] }) })));
  const reject = el("button", { className: "danger", textContent: "Reject" });
  reject.addEventListener("click", () => act(() => api("/api/pending/" + id + "/reject", { method: "POST", body: "{}" })));
  const regen = el("button", { className: "secondary", textContent: "Regenerate" });
  regen.addEventListener("click", () => act(() => api("/api/pending/" + id + "/regenerate", { method: "POST", body: "{}" })));
  const close = el("button", { className: "secondary", textContent: "Close" });
  close.addEventListener("click", () => dlg.close());
  dlg.append(approve, reject, regen, close, msg);
  dlg.showModal();
}

function summarize(data) {
  return Object.entries(data).map(([k, v]) => k + ": " + (typeof v === "object" ? JSON.stringify(v) : v)).join("; ");
}

async function openEntity(id) {
  const dlg = $("entity-dialog");
  const { entity } = await api("/api/entities/" + encodeURIComponent(id));
  dlg.replaceChildren();
  const msg = el("p", { className: "meta" });
  const act = async (fn) => {
    try { await fn(); dlg.close(); loadEntities(); doSearch(); } catch (e) { msg.textContent = e.message; }
  };
  const arch = el("button", { className: "secondary",
    textContent: entity.status === "archived" ? "Restore" : "Archive" });
  arch.addEventListener("click", () => act(() => api("/api/entities/" + id + "/" +
    (entity.status === "archived" ? "restore" : "archive"), { method: "POST", body: "{}" })));
  const del = el("button", { className: "danger", textContent: "Forget" });
  del.addEventListener("click", () => {
    if (window.confirm("Permanently delete this record and all traces?")) {
      act(() => api("/api/entities/" + id + "?confirm=true", { method: "DELETE" }));
    }
  });
  const close = el("button", { className: "secondary", textContent: "Close" });
  close.addEventListener("click", () => dlg.close());
  dlg.append(el("span", { className: "badge", textContent: entity.type + " · " + entity.status }),
    el("pre", { textContent: JSON.stringify(entity.data, null, 2) }), arch, del, close, msg);
  dlg.showModal();
}

let entityTypes = [];

async function loadEntities() {
  const sel = $("e-type");
  try {
    entityTypes = (await api("/api/entity-types")).types;
    const cur = sel.value;
    sel.replaceChildren(...entityTypes.map((t) => el("option", { value: t.name, textContent: t.name + " (" + t.count + ")" })));
    if (cur) sel.value = cur;
    await loadEntityRows();
    const act = $("e-activity");
    act.replaceChildren();
    const res = await api("/api/activity?limit=20");
    for (const a of res.items) {
      act.append(el("div", { className: "meta", textContent: a.at + " " + a.actor + " " + a.action + " " + a.target_kind }));
    }
  } catch (e) {
    $("e-table").textContent = e.message;
  }
}

async function loadEntityRows() {
  const box = $("e-table");
  box.replaceChildren();
  if (!sel_type()) return;
  const res = await api("/api/entities?limit=100&type=" + encodeURIComponent(sel_type()));
  for (const e of res.items) {
    const c = el("div", { className: "card" + (e.status !== "current" ? " old" : "") });
    c.append(el("div", { className: "snip", textContent: summarize(e.data) }));
    c.append(el("div", { className: "meta", textContent: e.status + " · " + e.source }));
    c.addEventListener("click", () => openEntity(e.id));
    box.append(c);
  }
  if (!res.items.length) box.append(el("p", { textContent: "No records." }));
}

function sel_type() { return $("e-type").value; }

function fieldInput(name, prop) {
  const t = prop.type;
  if (t === "boolean") return { input: el("input", { type: "checkbox" }), get: (i) => i.checked };
  if (t === "number" || t === "integer") {
    return { input: el("input", { type: "number", step: t === "integer" ? "1" : "any" }),
      get: (i) => (i.value === "" ? undefined : Number(i.value)) };
  }
  if (t === "array" && prop.items && ["string", "number", "integer", "boolean"].includes(prop.items.type)) {
    return { input: el("input", { type: "text", placeholder: "comma separated" }),
      get: (i) => {
        const v = i.value.split(",").map((x) => x.trim()).filter(Boolean);
        if (!v.length) return undefined;
        return prop.items.type === "string" ? v : v.map((x) => (prop.items.type === "boolean" ? x === "true" : Number(x)));
      } };
  }
  if (t === "string") {
    const fmt = prop.format === "date" ? "date" : prop.format === "date-time" ? "datetime-local" : "text";
    return { input: el("input", { type: fmt }), get: (i) => i.value || undefined };
  }
  return { input: el("textarea", { rows: 3, placeholder: "JSON" }),
    get: (i) => (i.value.trim() ? JSON.parse(i.value) : undefined) };
}

function openNewEntity() {
  const type = entityTypes.find((t) => t.name === sel_type());
  if (!type) return;
  const dlg = $("entity-dialog");
  dlg.replaceChildren(el("h4", { textContent: "New " + type.name }));
  const fields = [];
  for (const [name, prop] of Object.entries(type.json_schema.properties)) {
    const f = fieldInput(name, prop);
    fields.push([name, f]);
    dlg.append(el("label", { textContent: name + ((type.json_schema.required || []).includes(name) ? " *" : "") }), f.input);
  }
  const msg = el("p", { className: "meta" });
  const save = el("button", { textContent: "Create" });
  save.addEventListener("click", async () => {
    try {
      const data = {};
      for (const [name, f] of fields) {
        const v = f.get(f.input);
        if (v !== undefined) data[name] = v;
      }
      await api("/api/entities", { method: "POST", body: JSON.stringify({ type: type.name, data }) });
      dlg.close();
      loadEntities();
    } catch (e) { msg.textContent = e.message; }
  });
  const close = el("button", { className: "secondary", textContent: "Cancel" });
  close.addEventListener("click", () => dlg.close());
  dlg.append(save, close, msg);
  dlg.showModal();
}

function openNewType() {
  const dlg = $("entity-dialog");
  const area = el("textarea", { rows: 14, value: JSON.stringify({ name: "", description: "",
    json_schema: { type: "object", required: [], properties: { title: { type: "string" } } },
    text_template: "{title}", unique_key: null }, null, 2) });
  const msg = el("p", { className: "meta" });
  const save = el("button", { textContent: "Create type" });
  save.addEventListener("click", async () => {
    try {
      await api("/api/entity-types", { method: "POST", body: area.value });
      dlg.close();
      loadEntities();
    } catch (e) { msg.textContent = e.message; }
  });
  const close = el("button", { className: "secondary", textContent: "Cancel" });
  close.addEventListener("click", () => dlg.close());
  dlg.replaceChildren(el("h4", { textContent: "New entity type" }), area, save, close, msg);
  dlg.showModal();
}

$("e-type").addEventListener("change", () => loadEntityRows().catch((e) => { $("e-table").textContent = e.message; }));
$("e-new").addEventListener("click", openNewEntity);
$("e-newtype").addEventListener("click", openNewType);

(async () => {
  try {
    const h = await api("/health");
    llmOn = !!h.llm.configured;
  } catch (e) { llmOn = false; }
  $("add-tidy-row").hidden = !llmOn;
  $("tab-review-btn").hidden = !llmOn;
  refreshReviewCount();
})();
