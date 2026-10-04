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
  c.addEventListener("click", () => openDetail(n.id));
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
    await api("/api/notes", { method: "POST", body: JSON.stringify(body) });
    msg.textContent = "Saved.";
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
