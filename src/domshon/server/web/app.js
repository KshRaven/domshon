"use strict";
const $ = (s, el = document) => el.querySelector(s);
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}
const q = (o) => new URLSearchParams(o).toString();
const S = { token: null, role: null, root: "main", path: "", ws: null, wsTimer: null, tokenMode: false };
const joinPath = (a, b) => [a, b].filter(Boolean).join("/");

/* ---------- per-browser preferences ---------- */
const PREF_DEFAULTS = { wrap: false, confirmDelete: true, iso: false, calm: false, parallel: 3 };
function loadPrefs() {
  try { return { ...PREF_DEFAULTS, ...JSON.parse(localStorage.getItem("ds_prefs") || "{}") }; }
  catch (e) { return { ...PREF_DEFAULTS }; }
}
function savePrefs() { try { localStorage.setItem("ds_prefs", JSON.stringify(S.prefs)); } catch (e) { /* ignore */ } }
function applyPrefs() { document.body.classList.toggle("calm", !!S.prefs.calm); }
S.prefs = loadPrefs();
applyPrefs();
const closeDialogs = () => document.querySelectorAll("dialog[open]").forEach((d) => d.close());

function fmtSize(n) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
}
const sz = (n) => (n ? fmtSize(n) : "0 B");
const fmtDate = (t) => (S.prefs.iso ? new Date(t * 1000).toISOString().replace("T", " ").slice(0, 19) : new Date(t * 1000).toLocaleString());
function toast(msg, err) {
  const t = h("div", { class: "toast" + (err ? " err" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), 4000);
}

/* ---------- in-page dialogs (no browser alert / confirm / prompt) ---------- */
// ask({title, message, input?: {label, value, validate?}, ok, danger, extra?})  (extra = a 3rd button that resolves false)
//   -> resolves to the typed text (input mode), true (confirm mode) or null when cancelled
function ask(o) {
  return new Promise((resolve) => {
    const d = $("#dlgAsk"), form = $("#askForm"), inp = $("#askInput");
    $("#askTitle").textContent = o.title || "";
    $("#askMsg").textContent = o.message || "";
    $("#askMsg").classList.toggle("hidden", !o.message);
    $("#askLabel").classList.toggle("hidden", !o.input);
    $("#askErr").textContent = "";
    if (o.input) { $("#askLabelText").textContent = o.input.label || ""; inp.value = o.input.value || ""; inp.type = o.input.type || "text"; }
    const ok = $("#askOk");
    ok.textContent = o.ok || "OK";
    ok.classList.toggle("danger-solid", !!o.danger);
    let result = null;
    const ex = $("#askExtra");
    ex.classList.toggle("hidden", !o.extra);
    ex.textContent = o.extra || "";
    ex.onclick = () => { result = false; d.close(); };
    d.onclose = () => resolve(result);
    $("#askCancel").onclick = () => d.close();
    form.onsubmit = (ev) => {
      ev.preventDefault();
      if (o.input) {
        const v = inp.value.trim();
        const bad = o.input.validate ? o.input.validate(v) : (v ? "" : "Please enter a value.");
        if (bad) { $("#askErr").textContent = bad; return; }
        result = v;
      } else result = true;
      d.close();
    };
    d.showModal();
    if (o.input) { inp.focus(); inp.select(); } else ok.focus();
  });
}
const confirmBox = async (title, message, ok = "CONFIRM", danger = true) => !!(await ask({ title, message, ok, danger }));
const promptBox = (title, label, value = "", ok = "OK", validate) => ask({ title, input: { label, value, validate }, ok });

/* ---------- auth ---------- */
function saveToken(t, remember) {
  localStorage.removeItem("ds_token"); sessionStorage.removeItem("ds_token");
  (remember ? localStorage : sessionStorage).setItem("ds_token", t);
}
function logout() {
  closeDialogs();
  localStorage.removeItem("ds_token"); sessionStorage.removeItem("ds_token");
  S.token = null;
  if (S.ws) { S.ws.onclose = null; S.ws.close(); S.ws = null; }
  clearTimeout(S.wsTimer);
  $("#app").classList.add("hidden"); $("#login").classList.remove("hidden");
}
async function api(method, url, body) {
  const opt = { method, headers: { Authorization: "Bearer " + S.token } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  if (r.status === 401) { logout(); throw new Error("Session expired"); }
  if (!r.ok) {
    let m = r.statusText;
    try { m = (await r.json()).detail || m; } catch (e) { /* ignore */ }
    throw new Error(m);
  }
  return r.json();
}

$("#toggleMode").onclick = () => {
  S.tokenMode = !S.tokenMode;
  $("#credFields").classList.toggle("hidden", S.tokenMode);
  $("#tokenField").classList.toggle("hidden", !S.tokenMode);
  $("#toggleMode").textContent = S.tokenMode ? "Use username & password" : "Use an API token instead";
};
$("#loginForm").onsubmit = async (ev) => {
  ev.preventDefault();
  $("#loginErr").textContent = "";
  try {
    let token;
    if (S.tokenMode) token = $("#lToken").value.trim();
    else {
      const r = await fetch("/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: $("#lUser").value, password: $("#lPass").value }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Login failed");
      token = d.token;
    }
    S.token = token;
    await api("GET", "/api/me");
    saveToken(token, $("#lRemember").checked);
    $("#lPass").value = "";
    await start();
  } catch (e) { S.token = null; $("#loginErr").textContent = e.message; }
};
$("#logout").onclick = logout;

async function start() {
  const me = await api("GET", "/api/me");
  S.role = me.role;
  S.me = me;
  $("#who").textContent = me.name;
  $("#adminTab").classList.toggle("hidden", me.role !== "admin");
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  const roots = (await api("GET", "/api/roots")).roots;
  const sel = $("#rootSel"); sel.replaceChildren(...roots.map((r) => h("option", { value: r }, r)));
  if (!roots.includes(S.root)) S.root = roots[0];
  sel.value = S.root;
  S.path = "";
  await refresh();
  connectWS();
  loadUsageNote();
}
async function loadUsageNote() {
  try {
    const d = await api("GET", "/api/usage");
    $("#usage").textContent = d.quota ? sz(d.used) + " / " + sz(d.quota) : sz(d.used) + " used";
  } catch (e) { /* not critical */ }
}
$("#rootSel").onchange = (e) => { S.root = e.target.value; S.path = ""; refresh(); };

/* ---------- account & settings ---------- */
function openAccount() {
  const me = S.me || {};
  const rows = [
    ["User", me.name], ["Role", me.role === "admin" ? "Administrator" : "Device / user"],
    ["Server", location.origin + (me.domain ? "  (" + me.domain + ")" : "")],
    ["Version", me.version], ["Mode", me.read_only ? "Read-only" : "Read / write"],
  ];
  $("#acctInfo").replaceChildren(...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v ?? "—")]));
  $("#acctAdmin").classList.toggle("hidden", S.role !== "admin");
  $("#acctNote").classList.toggle("hidden", S.role === "admin");
  $("#dlgAccount").showModal();
}
function openSettings() {
  $("#pConfirm").checked = !!S.prefs.confirmDelete;
  $("#pIso").checked = !!S.prefs.iso;
  $("#pCalm").checked = !!S.prefs.calm;
  $("#pPar").value = String(S.prefs.parallel);
  $("#btnServerSettings").classList.toggle("hidden", S.role !== "admin");
  $("#dlgSettings").showModal();
}
$("#btnAccount").onclick = openAccount;
$("#btnSettings").onclick = openSettings;
$("#prefForm").onchange = () => {
  S.prefs = { confirmDelete: $("#pConfirm").checked, iso: $("#pIso").checked, calm: $("#pCalm").checked,
              parallel: Number($("#pPar").value) || 3 };
  savePrefs(); applyPrefs(); refresh();
};
$("#btnServerSettings").onclick = () => { closeDialogs(); $("#adminTab").click(); };
document.querySelectorAll("dialog").forEach((d) => {
  d.addEventListener("click", (e) => { if (e.target === d && !d.classList.contains("sticky")) d.close(); });
  d.querySelectorAll("[data-close]").forEach((b) => { b.onclick = () => d.close(); });
});

/* ---------- text file editor ---------- */
const TEXT_EXT = new Set(("txt md markdown rst log csv tsv json jsonc json5 ipynb yml yaml toml ini cfg conf env properties xml html htm css scss sass less js mjs cjs jsx ts tsx vue svelte py pyw pyi rb php pl pm lua r jl go rs java kt kts scala swift c h cc cpp cxx hpp hxx cs vb fs m mm sh bash zsh fish ksh bat cmd ps1 psm1 psd1 sql graphql proto gradle cmake make mk dockerfile gitignore gitattributes editorconfig tex bib srt vtt service timer desktop reg nginx lock").split(" "));
const TEXT_NAMES = new Set(["makefile", "dockerfile", "license", "readme", "procfile", "gemfile", "rakefile", "vagrantfile", "cmakelists.txt"]);
function isTextish(name) {
  const n = name.toLowerCase();
  if (TEXT_NAMES.has(n)) return true;
  const i = n.lastIndexOf(".");
  return TEXT_EXT.has(i >= 0 ? n.slice(i + 1) : n);
}
const ED = { path: null, root: null, root_: null, enc: "utf-8", eol: "lf", mtime: null, clean: "" };
const edText = () => $("#edText");
const edDirty = () => edText().value !== ED.clean;
function edUpdate() {
  $("#edDirty").classList.toggle("hidden", !edDirty());
  const t = edText(), pre = t.value.slice(0, t.selectionStart).split("\n");
  $("#edPos").textContent = "Ln " + pre.length + ", Col " + (pre[pre.length - 1].length + 1);
  $("#edInfo").textContent = ED.enc.toUpperCase() + " · " + ED.eol.toUpperCase() + " · " + t.value.split("\n").length + " lines";
}
async function openEditor(full) {
  try {
    const d = await api("GET", "/api/text?" + q({ root: S.root, path: full }));
    Object.assign(ED, { path: full, root: S.root, enc: d.encoding, eol: d.eol, mtime: d.mtime, clean: d.content });
    $("#edName").textContent = S.root + "/" + full;
    edText().value = d.content;
    edText().classList.toggle("wrap", !!S.prefs.wrap);
    $("#dlgEdit").showModal();
    edText().setSelectionRange(0, 0); edText().scrollTop = 0; edText().focus();
    edUpdate();
  } catch (e) { toast(e.message, true); }
}
async function edSave() {
  if (ED.path == null) return true;
  const name = ED.path.toLowerCase();
  if (name.endsWith(".json") || name.endsWith(".ipynb")) {
    try { JSON.parse(edText().value); }
    catch (e) {
      if (!(await confirmBox("INVALID JSON", "This file is not valid JSON (" + e.message + "). Save it anyway?", "SAVE ANYWAY", false))) return false;
    }
  }
  try {
    const content = edText().value;
    const r = await api("PUT", "/api/text", { root: ED.root, path: ED.path, content, encoding: ED.enc, eol: ED.eol, mtime: ED.mtime });
    ED.mtime = r.mtime; ED.clean = content; edUpdate(); toast("Saved " + ED.path.split("/").pop());
    return true;
  } catch (e) {
    if (/changed on the server/.test(e.message) &&
        await confirmBox("FILE CHANGED", "This file was modified elsewhere after you opened it. Overwrite it with your version?", "OVERWRITE")) {
      ED.mtime = null; return edSave();
    }
    toast(e.message, true); return false;
  }
}
async function edClose() {
  if (edDirty()) {
    const keep = await ask({ title: "UNSAVED CHANGES", message: "Save changes to " + ED.path.split("/").pop() + " before closing?", ok: "SAVE", danger: false, extra: "DISCARD" });
    if (keep === null) return;
    if (keep === true && !(await edSave())) return;
  }
  ED.path = null; $("#dlgEdit").close();
}
$("#edSave").onclick = edSave;
$("#edClose").onclick = edClose;
$("#edWrap").onclick = () => { S.prefs.wrap = !S.prefs.wrap; savePrefs(); edText().classList.toggle("wrap", S.prefs.wrap); };
$("#dlgEdit").addEventListener("cancel", (e) => { e.preventDefault(); edClose(); });
edText().addEventListener("input", edUpdate);
["keyup", "click", "focus"].forEach((ev) => edText().addEventListener(ev, edUpdate));
edText().addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); edSave(); return; }
  if (e.key === "Tab" && !e.ctrlKey && !e.altKey) {
    e.preventDefault();
    const t = edText(), a = t.selectionStart, b = t.selectionEnd;
    if (a === b || !t.value.slice(a, b).includes("\n")) {
      t.setRangeText("    ", a, b, "end");
    } else {
      const ls = t.value.lastIndexOf("\n", a - 1) + 1, block = t.value.slice(ls, b);
      const out = e.shiftKey ? block.replace(/^( {1,4}|\t)/gm, "") : block.replace(/^/gm, "    ");
      t.setRangeText(out, ls, b, "select");
    }
    edUpdate();
  }
});
window.addEventListener("beforeunload", (e) => { if (ED.path != null && edDirty()) { e.preventDefault(); e.returnValue = ""; } });

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  $("#viewFiles").classList.toggle("hidden", b.dataset.view !== "files");
  $("#viewAdmin").classList.toggle("hidden", b.dataset.view !== "admin");
  if (b.dataset.view === "admin") loadAdmin();
});

/* ---------- file browser ---------- */
let refreshTimer = null;
async function refresh() {
  try {
    const d = await api("GET", "/api/files?" + q({ root: S.root, path: S.path }));
    renderCrumbs(); renderRows(d.items);
  } catch (e) { toast(e.message, true); if (S.path) { S.path = ""; } }
}
const refreshSoon = () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 250); };

function renderCrumbs() {
  const parts = S.path ? S.path.split("/") : [];
  const els = [h("a", { onclick: () => go("") }, S.root)];
  parts.forEach((p, i) => {
    els.push(h("span", {}, "/"));
    els.push(h("a", { onclick: () => go(parts.slice(0, i + 1).join("/")) }, p));
  });
  $("#crumbs").replaceChildren(...els);
}
function go(p) { S.path = p; refresh(); }

function renderRows(items) {
  $("#empty").classList.toggle("hidden", items.length > 0);
  const rows = [];
  if (S.path) {
    rows.push(h("tr", {}, h("td", { class: "name", colspan: 4 },
      h("a", { onclick: () => go(S.path.split("/").slice(0, -1).join("/")) }, h("span", { class: "ico" }, "↰"), ".."))));
  }
  for (const it of items) {
    const full = joinPath(S.path, it.name);
    const isDir = it.type === "dir";
    rows.push(h("tr", {},
      h("td", { class: "name" }, h("a", { onclick: () => (isDir ? go(full) : isTextish(it.name) ? openEditor(full) : download(full)) },
        h("span", { class: "ico" }, isDir ? "▣" : "▤"), it.name)),
      h("td", { class: "r muted" }, isDir ? "" : fmtSize(it.size)),
      h("td", { class: "muted" }, fmtDate(it.mtime)),
      h("td", { class: "r act" },
        isDir ? null : h("button", { title: "Open in editor", onclick: () => openEditor(full) }, "✐"),
        h("button", { title: "Download", onclick: () => download(full) }, "↓"),
        h("button", { title: "Rename / move", onclick: () => rename(full, it.name) }, "✎"),
        h("button", { class: "del", title: "Delete", onclick: () => del(full, it.name) }, "✕"))));
  }
  $("#rows").replaceChildren(...rows);
}

async function download(full) {
  try {
    const { ticket } = await api("GET", "/api/ticket");
    const a = h("a", { href: "/api/download?" + q({ root: S.root, path: full, ticket }) });
    document.body.append(a); a.click(); a.remove();
  } catch (e) { toast(e.message, true); }
}
async function rename(full, name) {
  const nn = await promptBox("RENAME / MOVE", "New name or path (relative to this root)", full, "RENAME");
  if (!nn || nn === full) return;
  try { await api("POST", "/api/move", { root: S.root, src: full, dst: nn.replace(/^\/+/, "") }); refresh(); }
  catch (e) { toast(e.message, true); }
}
async function del(full, name) {
  if (S.prefs.confirmDelete && !(await confirmBox("DELETE", "Delete '" + name + "'? This cannot be undone.", "DELETE"))) return;
  try { await api("DELETE", "/api/files?" + q({ root: S.root, path: full })); refresh(); }
  catch (e) { toast(e.message, true); }
}
$("#btnMkdir").onclick = async () => {
  const n = await promptBox("NEW FOLDER", "Folder name", "", "CREATE",
    (v) => (!v ? "Please enter a name." : /[\\:*?"<>|]/.test(v) ? "Those characters are not allowed: \\ : * ? \" < > |" : ""));
  if (!n) return;
  try { await api("POST", "/api/mkdir", { root: S.root, path: joinPath(S.path, n) }); refresh(); }
  catch (e) { toast(e.message, true); }
};
$("#btnRefresh").onclick = refresh;
$("#btnUpload").onclick = () => $("#fileInput").click();
$("#btnUploadDir").onclick = () => $("#dirInput").click();
$("#fileInput").onchange = (e) => { enqueue([...e.target.files].map((f) => ({ file: f, rel: f.name }))); e.target.value = ""; };
$("#dirInput").onchange = (e) => {
  enqueue([...e.target.files].map((f) => ({ file: f, rel: f.webkitRelativePath || f.name }))); e.target.value = "";
};

/* ---------- drag & drop ---------- */
let dragDepth = 0;
const hasFiles = (ev) => ev.dataTransfer && [...ev.dataTransfer.types].includes("Files");
window.addEventListener("dragenter", (ev) => { if (!hasFiles(ev)) return; ev.preventDefault(); dragDepth++; if (S.token) $("#drop").classList.remove("hidden"); });
window.addEventListener("dragover", (ev) => { if (hasFiles(ev)) ev.preventDefault(); });
window.addEventListener("dragleave", (ev) => { if (!hasFiles(ev)) return; dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("#drop").classList.add("hidden"); });
window.addEventListener("drop", async (ev) => {
  if (!hasFiles(ev)) return;
  ev.preventDefault(); dragDepth = 0; $("#drop").classList.add("hidden");
  if (!S.token || $("#viewFiles").classList.contains("hidden")) return;
  const root = S.root, base = S.path;
  const { files, dirs } = await collectDrop(ev.dataTransfer.items);
  for (const d of dirs) { try { await api("POST", "/api/mkdir", { root, path: joinPath(base, d) }); } catch (e) { /* ignore */ } }
  enqueue(files, root, base);
});
async function collectDrop(items) {
  const out = [], dirs = [];
  // must be read synchronously, before any await
  const entries = [...items].map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
  async function walk(entry) {
    if (entry.isFile) {
      const f = await new Promise((ok, no) => entry.file(ok, no));
      out.push({ file: f, rel: entry.fullPath.replace(/^\//, "") });
    } else if (entry.isDirectory) {
      dirs.push(entry.fullPath.replace(/^\//, ""));
      const rd = entry.createReader();
      let batch;
      do {
        batch = await new Promise((ok, no) => rd.readEntries(ok, no));
        for (const e of batch) await walk(e);
      } while (batch.length);
    }
  }
  for (const e of entries) await walk(e);
  return { files: out, dirs };
}

/* ---------- upload queue ---------- */
const queue = []; let active = 0;
function enqueue(list, root = S.root, base = S.path) {
  for (const { file, rel } of list) {
    const bar = h("progress", { max: 100, value: 0 });
    const label = h("span", {}, "queued");
    const el = h("div", { class: "qi" }, h("div", { class: "n" }, h("span", {}, rel), label), bar);
    $("#queue").append(el);
    queue.push({ file, root, dest: joinPath(base, rel), bar, label });
  }
  if (list.length) $("#queue").classList.remove("hidden");
  pump();
}
function pump() {
  while (active < (S.prefs.parallel || 3) && queue.length) {
    const job = queue.shift(); active++;
    upload(job).finally(() => {
      active--;
      if (!queue.length && !active) { refreshSoon(); loadUsageNote(); setTimeout(() => { if (!queue.length && !active) { $("#queue").classList.add("hidden"); $("#queue").replaceChildren(); } }, 4000); }
      pump();
    });
  }
}
function upload(job) {
  return new Promise((resolve) => {
    const x = new XMLHttpRequest();
    x.open("PUT", "/api/files?" + q({ root: job.root, path: job.dest }));
    x.setRequestHeader("Authorization", "Bearer " + S.token);
    x.setRequestHeader("X-DS-Mtime", String(job.file.lastModified / 1000));
    x.upload.onprogress = (e) => { if (e.lengthComputable) { job.bar.value = (e.loaded / e.total) * 100; job.label.textContent = Math.round(job.bar.value) + "%"; } };
    x.onload = () => {
      if (x.status === 401) logout();
      if (x.status >= 200 && x.status < 300) { job.bar.value = 100; job.label.textContent = "✓"; }
      else { let m = x.statusText; try { m = JSON.parse(x.responseText).detail || m; } catch (e) { /* ignore */ } job.label.textContent = "✕ " + m; toast(job.dest + ": " + m, true); }
      resolve();
    };
    x.onerror = () => { job.label.textContent = "✕ network"; resolve(); };
    x.send(job.file);
  });
}

/* ---------- live updates ---------- */
async function connectWS() {
  clearTimeout(S.wsTimer);
  if (!S.token) return;
  try {
    const { ticket } = await api("GET", "/api/ticket");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws?ticket=${encodeURIComponent(ticket)}`);
    S.ws = ws;
    ws.onopen = () => { const l = $("#live"); l.textContent = "● LIVE"; l.className = "pill on"; clearInterval(S.ping); S.ping = setInterval(() => { try { ws.send("ping"); } catch (e) { /* closed */ } }, 25000); };
    ws.onmessage = (m) => {
      const d = JSON.parse(m.data);
      if (d.event === "change" && d.root === S.root) refreshSoon();
    };
    ws.onclose = () => { clearInterval(S.ping); const l = $("#live"); l.textContent = "● OFFLINE"; l.className = "pill off"; S.wsTimer = setTimeout(connectWS, 4000); };
  } catch (e) { S.wsTimer = setTimeout(connectWS, 6000); }
}

/* ---------- admin ---------- */
const FIELDS = [
  ["domain", "Public domain", "text"], ["storage_root", "Storage location (absolute path)", "text"],
  ["mounts", "Extra synced folders  name=/abs/path;name2=/abs/path", "text"],
  ["admin_user", "Admin username", "text"], ["max_upload_mb", "Max upload MB (0 = unlimited)", "number"],
  ["session_hours", "Session hours", "number"],
  ["quota_mb", "Cloud storage quota MB (0 = unlimited; main storage only)", "number"],
  ["min_free_mb", "Always keep this many MB of the disk free (0 = off)", "number"],
  ["blocked_extensions", "Blocked file types, e.g. .exe,.php", "text"],
  ["user_writes", "Device users may upload / change files (off = admin only)", "checkbox"],
  ["read_only", "Read-only mode (blocks all writes)", "checkbox"],
  ["lockdown", "Lockdown (only admin session allowed; device tokens rejected)", "checkbox"],
];
async function loadAdmin() {
  try {
    const d = await api("GET", "/api/admin/settings");
    const s = d.settings, form = $("#settingsForm");
    const inputs = FIELDS.map(([k, label, type]) => {
      let val = s[k];
      if (k === "mounts") val = Object.entries(val || {}).map(([a, b]) => a + "=" + b).join(";");
      const input = type === "checkbox" ? h("input", { type, id: "f_" + k, checked: !!val }) : h("input", { type, id: "f_" + k, value: val ?? "" });
      return type === "checkbox" ? h("label", { class: "check" }, input, label) : h("label", {}, label, input);
    });
    form.replaceChildren(...inputs, h("button", { class: "btn primary", type: "submit" }, "APPLY LIVE"),
      h("p", { class: "muted small" }, `host ${s.host}:${s.port} (restart to change)`));
    const du = d.disk || {};
    $("#disk").textContent = du.total ? `Disk: ${fmtSize(du.used)} used / ${fmtSize(du.total)} (${fmtSize(du.free)} free)` : "";
    loadTokens();
    loadStorage();
  } catch (e) { toast(e.message, true); }
}
async function loadStorage() {
  try {
    const d = await api("GET", "/api/admin/usage");
    const total = d.quota || (d.disk && d.disk.total) || 1;
    const label = d.quota ? `Cloud storage: ${sz(d.used)} of ${sz(d.quota)} quota`
                          : `Cloud storage: ${sz(d.used)} used (no quota set)`;
    const kids = [h("div", {}, label), h("progress", { max: total, value: Math.min(d.used, total) })];
    if (d.disk && d.disk.total) {
      kids.push(h("div", { class: "muted" }, `Disk free: ${sz(d.disk.free)}` + (d.reserve ? ` (reserve ${sz(d.reserve)})` : "")));
    }
    if (d.top && d.top.length) {
      kids.push(h("table", { class: "mini" }, h("tbody", {}, d.top.map((t) =>
        h("tr", {}, h("td", {}, (t.type === "dir" ? "▣ " : "▤ ") + t.name), h("td", { class: "r muted" }, sz(t.size)))))));
    }
    $("#storage").replaceChildren(...kids);
  } catch (e) { $("#storage").textContent = ""; }
}
$("#settingsForm").onsubmit = async (ev) => {
  ev.preventDefault();
  const changes = {};
  for (const [k, , type] of FIELDS) {
    const el = $("#f_" + k);
    changes[k] = type === "checkbox" ? el.checked : type === "number" ? Number(el.value) : el.value;
  }
  try { await api("PATCH", "/api/admin/settings", { changes }); toast("Settings applied"); loadAdmin(); }
  catch (e) { toast(e.message, true); }
};
$("#pwForm").onsubmit = async (ev) => {
  ev.preventDefault();
  try { await api("POST", "/api/admin/password", { new_password: $("#newPw").value }); $("#newPw").value = ""; toast("Password changed"); }
  catch (e) { toast(e.message, true); }
};
async function loadTokens() {
  const d = await api("GET", "/api/admin/tokens");
  $("#tokRows").replaceChildren(...d.tokens.map((t) => h("tr", {},
    h("td", {}, t.name), h("td", { class: "muted" }, new Date(t.created * 1000).toLocaleDateString()),
    h("td", { class: "r" }, h("button", { class: "btn small danger", onclick: async () => { if (!(await confirmBox("REVOKE TOKEN", "Revoke '" + t.name + "'? That device will no longer be able to sign in.", "REVOKE"))) return; try { await api("DELETE", "/api/admin/tokens/" + t.id); } catch (e) { toast(e.message, true); } loadTokens(); } }, "REVOKE")))));
}
$("#tokForm").onsubmit = async (ev) => {
  ev.preventDefault();
  try {
    const d = await api("POST", "/api/admin/tokens", { name: $("#tokName").value || "device" });
    const box = $("#newTok"); box.classList.remove("hidden"); box.textContent = "Copy now (shown once): " + d.token;
    $("#tokName").value = ""; loadTokens();
  } catch (e) { toast(e.message, true); }
};
$("#rotate").onclick = async () => {
  if (!(await confirmBox("ROTATE SECRET", "Invalidate ALL admin sessions? Everyone, including you on other devices, must sign in again.", "ROTATE"))) return;
  try { const d = await api("POST", "/api/admin/rotate-secret"); S.token = d.token; saveToken(d.token, !!localStorage.getItem("ds_token")); toast("Secret rotated"); }
  catch (e) { toast(e.message, true); }
};

/* ---------- boot ---------- */
(async function init() {
  S.token = localStorage.getItem("ds_token") || sessionStorage.getItem("ds_token");
  if (S.token) { try { await start(); return; } catch (e) { S.token = null; } }
  $("#login").classList.remove("hidden");
})();
