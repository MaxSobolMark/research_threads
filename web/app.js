/* Research Threads — sidebar list + floating bubble field, fed by SSE. */

"use strict";

const shell = document.getElementById("shell");
const sideEl = document.getElementById("side");
const sideList = document.getElementById("side-list");
const collapseBtn = document.getElementById("side-collapse");
const openBtn = document.getElementById("side-open");
const field = document.getElementById("field");
const bubblesEl = document.getElementById("bubbles");
const fieldEmpty = document.getElementById("field-empty");
const backdrop = document.getElementById("backdrop");
const conn = document.getElementById("conn");
const connLabel = document.getElementById("conn-label");
const filterInput = document.getElementById("filter");

let snapshot = { threads: [], now: 0 };
let openThreadId = null;
let showArchived = false;

/* ------------------------------------------------------------------ utils */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Notes are signed by an agent or by a person; only the two agents get their
// own colour, so any other name is styled the same way.
function authorClass(author) {
  return author === "claude" || author === "codex" ? author : "human";
}

function age(ts) {
  if (!ts) return "";
  const d = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (d < 60) return "now";
  if (d < 3600) return Math.floor(d / 60) + "m";
  if (d < 86400) return Math.floor(d / 3600) + "h";
  if (d < 86400 * 7) return Math.floor(d / 86400) + "d";
  if (d < 86400 * 30) return Math.floor(d / 86400 / 7) + "w";
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function dayLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const midnight = t => new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime();
  const diff = (midnight(now) - midnight(d)) / 86400000;
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, {
    month: "long", day: "numeric",
    year: d.getFullYear() === now.getFullYear() ? undefined : "numeric",
  });
}

const STATUS_WORD = {
  "working": "working",
  "unread": "new message",
  "idle": "ready",
  "needs-attention": "needs input",
  "needs-permission": "needs permission",
  "closed": "closed",
};

/* An idle thread whose last reply hasn't been read reads as "new message"
   until the user jumps to its vterm (or dismisses it). */
function displayStatus(t) {
  return t.unread ? "unread" : t.status;
}

/* Work the thread's agent has in flight: subagents, monitors, background
   commands. Absent for Codex threads, which report none of it. */
const BG_KINDS = [
  ["agents", "⚙", "subagent"],
  ["monitors", "◷", "monitor"],
  ["commands", "▸", "background command"],
];

function bgParts(t, longForm) {
  const b = t.background;
  if (!b) return [];
  return BG_KINDS.filter(([k]) => b[k] > 0).map(([k, glyph, word]) => ({
    n: b[k], glyph,
    label: `${b[k]} ${longForm ? word : word.split(" ").pop()}${b[k] === 1 ? "" : "s"}`,
  }));
}

function bgBadges(t) {
  return bgParts(t, true).map(p =>
    `<span class="bg-badge" title="${esc(p.label)} running">${p.n}${p.glyph}</span>`).join("");
}

function bgSummary(t, longForm) {
  return bgParts(t, longForm).map(p => p.label).join(" · ");
}

function richText(s) {
  let out = esc(s);
  out = out.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  return out;
}

function hash01(str, salt) {
  let h = 2166136261 ^ (salt || 0);
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 100000) / 100000;
}

async function api(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  return resp.json();
}

function visibleThreads() {
  const q = filterInput.value.trim().toLowerCase();
  return snapshot.threads.filter(t =>
    !q || [t.name, t.cwd, t.agent, t.objective].some(v => (v || "").toLowerCase().includes(q)));
}

/* ---------------------------------------------------------------- sidebar */

const STATUS_RANK = {
  "needs-attention": 0, "needs-permission": 0, "unread": 1, "working": 2, "idle": 3,
};

function itemHTML(t) {
  const cls = ["item", t.open ? "" : "closed", t.id === openThreadId ? "selected" : ""].join(" ");
  return `
  <div class="${cls}" data-id="${t.id}" tabindex="0">
    <span class="dot" data-status="${esc(displayStatus(t))}"></span>
    ${t.pinned ? '<span class="pin-mark">●</span>' : ""}
    <span class="item-name">${esc(t.name)}</span>
    ${bgBadges(t)}
    <span class="item-age">${age(t.last_active_at)}</span>
  </div>`;
}

function renderSidebar() {
  const ts = visibleThreads();
  /* Archiving hides a thread even while its vterm is still running, so
     `archived` is checked before the open/closed split. */
  const active = ts.filter(t => t.open && !t.archived).sort((a, b) =>
    (b.pinned - a.pinned) ||
    ((STATUS_RANK[displayStatus(a)] ?? 4) - (STATUS_RANK[displayStatus(b)] ?? 4)) ||
    (b.last_active_at - a.last_active_at));
  const earlier = ts.filter(t => !t.open && !t.archived)
    .sort((a, b) => (b.pinned - a.pinned) || (b.last_active_at - a.last_active_at));
  const archived = ts.filter(t => t.archived)
    .sort((a, b) => b.last_active_at - a.last_active_at);

  let html = "";
  if (active.length)
    html += `<div class="side-section">Active <span class="n">${active.length}</span></div>`
          + active.map(itemHTML).join("");
  if (earlier.length)
    html += `<div class="side-section">Earlier <span class="n">${earlier.length}</span></div>`
          + earlier.map(itemHTML).join("");
  if (archived.length) {
    html += showArchived
      ? `<div class="side-section">Archived <span class="n">${archived.length}</span></div>`
        + archived.map(itemHTML).join("")
      : `<button class="show-archived">${archived.length} archived —</button>`;
  }
  if (!html) {
    html = `<div class="side-section">${filterInput.value.trim() ? "No matches." : "No threads yet."}</div>`;
  }
  sideList.innerHTML = html;
}

sideList.addEventListener("click", e => {
  if (e.target.closest(".show-archived")) { showArchived = true; renderSidebar(); return; }
  const el = e.target.closest("[data-id]");
  if (el) openPanel(Number(el.dataset.id), el);
});
sideList.addEventListener("keydown", e => {
  if (e.key === "Enter") {
    const el = e.target.closest("[data-id]");
    if (el) openPanel(Number(el.dataset.id), el);
  }
});
filterInput.addEventListener("input", render);

/* ------------------------------------------------------------ bubble field */

const positions = new Map();   // thread id -> {x, y}
const bubbleEls = new Map();   // thread id -> element

function bubbleRadius(t) {
  const urgent = t.status === "needs-attention" || t.status === "needs-permission";
  return 62 + Math.min(26, (t.note_count || 0) * 2.5) + (urgent ? 8 : 0);
}

function layoutBubbles(items) {
  const W = field.clientWidth, H = field.clientHeight;
  if (!W || !H) return;
  const cx = W / 2, cy = H / 2;

  for (const t of items) {
    if (!positions.has(t.id)) {
      const a = hash01(String(t.id), 1) * Math.PI * 2;
      const r = (0.15 + 0.3 * hash01(String(t.id), 2)) * Math.min(W, H);
      positions.set(t.id, { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r });
    }
  }
  for (const id of [...positions.keys()]) {
    if (!items.some(t => t.id === id)) positions.delete(id);
  }

  const rs = new Map(items.map(t => [t.id, bubbleRadius(t)]));
  for (let iter = 0; iter < 80; iter++) {
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const a = positions.get(items[i].id), b = positions.get(items[j].id);
        const min = rs.get(items[i].id) + rs.get(items[j].id) + 18;
        let dx = b.x - a.x, dy = b.y - a.y;
        let d = Math.hypot(dx, dy);
        if (d < 0.01) { dx = 1; dy = 0.5; d = 1; }
        if (d < min) {
          const push = (min - d) / d / 2;
          a.x -= dx * push; a.y -= dy * push;
          b.x += dx * push; b.y += dy * push;
        }
      }
    }
    for (const t of items) {
      const p = positions.get(t.id), r = rs.get(t.id), m = 14;
      p.x += (cx - p.x) * 0.015;
      p.y += (cy - p.y) * 0.015;
      p.x = Math.min(W - r - m, Math.max(r + m, p.x));
      p.y = Math.min(H - r - m, Math.max(r + m, p.y));
    }
  }
}

function makeBubbleEl(t) {
  const el = document.createElement("div");
  el.className = "bubble entering";
  el.dataset.id = t.id;
  const dur = (6 + 4 * hash01(String(t.id), 3)).toFixed(2);
  const del = (-8 * hash01(String(t.id), 4)).toFixed(2);
  const dx = (3 + 5 * hash01(String(t.id), 5)).toFixed(1);
  const dy = (3 + 5 * hash01(String(t.id), 6)).toFixed(1);
  el.innerHTML = `
    <div class="bub-drift" style="--dur:${dur}s; --del:${del}s; --dx:${dx}px; --dy:${dy}px">
      <div class="bub-core" tabindex="0">
        <span class="bub-agent"></span>
        <span class="bub-name"></span>
        <span class="bub-status"></span>
      </div>
    </div>`;
  el.addEventListener("click", () => openPanel(t.id));
  el.querySelector(".bub-core").addEventListener("keydown", e => {
    if (e.key === "Enter") openPanel(t.id);
  });
  setTimeout(() => el.classList.remove("entering"), 700);
  return el;
}

function renderField() {
  const open = visibleThreads().filter(t => t.open && !t.archived);
  fieldEmpty.hidden = open.length > 0;
  layoutBubbles(open);

  for (const [id, el] of bubbleEls) {
    if (!open.some(t => t.id === id)) {
      bubbleEls.delete(id);
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 450);
    }
  }

  for (const t of open) {
    let el = bubbleEls.get(t.id);
    if (!el) {
      el = makeBubbleEl(t);
      bubbleEls.set(t.id, el);
      bubblesEl.appendChild(el);
    }
    const r = bubbleRadius(t), p = positions.get(t.id);
    el.style.width = el.style.height = 2 * r + "px";
    el.style.left = p.x - r + "px";
    el.style.top = p.y - r + "px";
    const core = el.querySelector(".bub-core");
    const st = displayStatus(t);
    core.dataset.status = st;
    core.title = t.objective || "";
    el.querySelector(".bub-name").textContent = t.name;
    const bg = bgSummary(t, false);
    el.querySelector(".bub-status").textContent =
      (STATUS_WORD[st] || st) + (bg ? " · " + bg : "");
    const agentEl = el.querySelector(".bub-agent");
    agentEl.textContent = t.agent || "";
    agentEl.className = "bub-agent " + (t.agent || "");
    el.style.visibility = t.id === openThreadId ? "hidden" : "";
  }
}

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    renderField();
    if (expander) setRect(expander, expandedRect());
  }, 150);
});

/* --------------------------------------------------------- sidebar toggle */

function setCollapsed(collapsed) {
  shell.classList.toggle("collapsed", collapsed);
  openBtn.hidden = !collapsed;
  localStorage.setItem("rt-side-collapsed", collapsed ? "1" : "");
  setTimeout(renderField, 380); // field width changed
}
collapseBtn.addEventListener("click", () => setCollapsed(true));
openBtn.addEventListener("click", () => setCollapsed(false));
if (localStorage.getItem("rt-side-collapsed")) setCollapsed(true);

/* ------------------------------------------- detail view: bubble opens up */

let expander = null;
let panelBody = null;

const STATUS_VAR = {
  "working": "var(--working)", "unread": "var(--unread)", "idle": "var(--ready)",
  "needs-attention": "var(--attn)", "needs-permission": "var(--perm)",
  "closed": "var(--closed)",
};

function originRect(id, fallbackEl) {
  const bub = bubbleEls.get(id);
  if (bub) return bub.querySelector(".bub-core").getBoundingClientRect();
  if (fallbackEl) return fallbackEl.getBoundingClientRect();
  const f = field.getBoundingClientRect();
  return { left: f.left + f.width / 2 - 30, top: f.top + f.height / 2 - 30,
           width: 60, height: 60 };
}

function setRect(el, r) {
  el.style.left = r.left + "px";
  el.style.top = r.top + "px";
  el.style.width = r.width + "px";
  el.style.height = r.height + "px";
}

function expandedRect() {
  const f = field.getBoundingClientRect();
  const w = Math.min(720, f.width * 0.92);
  const h = f.height * 0.94;
  return { left: f.left + (f.width - w) / 2, top: f.top + (f.height - h) / 2,
           width: w, height: h };
}

async function openPanel(id, fromEl) {
  if (expander) closePanel(true);
  openThreadId = id;
  const t = snapshot.threads.find(x => x.id === id);
  const start = originRect(id, fromEl);
  const startStatus = t ? displayStatus(t) : "closed";
  const bub = bubbleEls.get(id);
  if (bub) bub.style.visibility = "hidden";

  expander = document.createElement("div");
  expander.className = "expander";
  expander.style.setProperty("--st", STATUS_VAR[startStatus] || "var(--closed)");
  setRect(expander, start);
  panelBody = document.createElement("div");
  panelBody.className = "panel-body";
  expander.appendChild(panelBody);
  document.body.appendChild(expander);

  expander.getBoundingClientRect(); // commit start geometry before morphing
  expander.classList.add("open");
  setRect(expander, expandedRect());

  backdrop.hidden = false;
  history.replaceState(null, "", "#t" + id);
  renderSidebar();
  await refreshPanel();
}

function closePanel(immediate) {
  if (!expander) { openThreadId = null; return; }
  const id = openThreadId;
  openThreadId = null;
  const ex = expander, body = panelBody;
  expander = null;
  panelBody = null;
  backdrop.hidden = true;
  history.replaceState(null, "", location.pathname);
  renderSidebar();

  const bub = bubbleEls.get(id);
  const done = () => {
    ex.remove();
    if (bub) bub.style.visibility = "";
  };
  if (immediate) { done(); return; }

  ex.classList.remove("open");        // contents fade, radius returns to 50%
  body.style.opacity = "0";
  if (bub) {
    setRect(ex, originRect(id));      // shrink back into the bubble's spot
  } else {
    ex.classList.add("vanishing");    // no home to return to — melt away
    const r = ex.getBoundingClientRect();
    setRect(ex, { left: r.left + r.width / 2 - 30, top: r.top + r.height / 2 - 30,
                  width: 60, height: 60 });
  }
  setTimeout(done, 520);
}

function panelFocused() {
  return expander != null && expander.contains(document.activeElement) &&
    /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
}

function blockHTML(kind, label, text, metaText) {
  const has = Boolean(text && text.trim());
  return `
  <div class="block ${kind}">
    <div class="block-label">${label}
      <span class="meta">${metaText || ""}</span>
      <button class="block-edit" data-edit="${kind}" title="edit">edit</button>
    </div>
    <div class="block-text ${has ? "" : "placeholder"}" data-view="${kind}">${
      has ? richText(text) : (kind === "objective"
        ? "No objective yet — set one, or have the agent run <code>rt start</code>."
        : "No status yet — the agent keeps this updated with <code>rt update</code>.")
    }</div>
  </div>`;
}

async function refreshPanel() {
  if (openThreadId == null || !panelBody) return;
  if (panelFocused()) return; // don't clobber typing
  const t = await api("GET", `/api/threads/${openThreadId}`);
  if (t.error) { closePanel(); return; }
  const st = displayStatus(t);
  if (expander) expander.style.setProperty("--st", STATUS_VAR[st] || "var(--closed)");

  let tl = "", lastDay = "";
  for (const n of t.notes || []) {
    const day = dayLabel(n.ts);
    if (day !== lastDay) { tl += `<div class="tl-day">${day}</div>`; lastDay = day; }
    const when = new Date(n.ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    let content;
    if (n.kind === "plot" && n.path) {
      content = `<div class="tl-plot">
        <a href="/plots/${esc(n.path)}" target="_blank"><img src="/plots/${esc(n.path)}" loading="lazy" alt=""></a>
        ${n.text ? `<div class="caption">${richText(n.text)}</div>` : ""}
      </div>`;
    } else {
      content = richText(n.text);
    }
    tl += `
    <div class="tl-item">
      <span class="tl-when">${esc(when)}</span>
      <div class="tl-content"><span class="tl-author ${authorClass(n.author)}">${esc(n.author || "")}</span>${content}</div>
    </div>`;
  }
  if (!(t.notes || []).length)
    tl = `<div class="tl-day">No notes yet — agents post with <code>rt note</code> / <code>rt plot</code>.</div>`;

  const statusAge = t.status_since && t.status !== "closed"
    ? (t.unread ? ` ${age(t.status_since)} ago` : ` for ${age(t.status_since)}`) : "";
  const firstSeen = new Date(t.created_at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  const isVterm = t.key.startsWith("vterm:");

  panelBody.innerHTML = `
    <div class="panel-head">
      <span class="dot" data-status="${esc(st)}"></span>
      <input class="panel-name" id="rename" value="${esc(t.name)}" spellcheck="false">
      <button class="panel-close" id="close" title="close (esc)">×</button>
    </div>
    <p class="panel-sub">
      <span class="agent ${esc(t.agent || "")}">${esc(t.agent || "agent unknown")}</span>
      · ${STATUS_WORD[st] || esc(st)}${statusAge}<br>
      ${bgSummary(t, true) ? `<span class="bg-line">${esc(bgSummary(t, true))} in flight</span><br>` : ""}
      <span class="cwd">${esc(t.cwd || "")}</span>
    </p>
    <div class="panel-actions">
      ${isVterm && t.open ? `<button class="btn primary" data-act="focus">open in emacs</button>` : ""}
      ${t.unread ? `<button class="btn" data-act="mark_read">mark read</button>` : ""}
      <button class="btn" data-act="${t.pinned ? "unpin" : "pin"}">${t.pinned ? "unpin" : "pin"}</button>
      <button class="btn" data-act="${t.archived ? "unarchive" : "archive"}">${t.archived ? "unarchive" : "archive"}</button>
    </div>
    ${blockHTML("objective", "Objective", t.objective)}
    ${blockHTML("status", "Current status", t.status_text,
                t.status_updated_at ? "updated " + age(t.status_updated_at) + " ago" : "")}
    <div class="composer">
      <textarea id="composer" placeholder="Add a note…"></textarea>
      <div class="composer-foot"><button class="btn primary" id="add-note">add note</button></div>
    </div>
    <div class="tl">${tl}</div>
    <p class="panel-footnote">
      ${t.session_count} session${t.session_count === 1 ? "" : "s"} · first seen ${esc(firstSeen)}
    </p>`;

  panelBody.querySelector("#close").onclick = () => closePanel();

  panelBody.querySelectorAll("[data-act]").forEach(btn => {
    btn.onclick = async () => {
      await api("POST", `/api/threads/${t.id}/${btn.dataset.act}`, {});
      if (btn.dataset.act !== "focus") refreshPanel();
    };
  });

  panelBody.querySelectorAll(".block-edit").forEach(btn => {
    btn.onclick = () => {
      const kind = btn.dataset.edit;
      const view = panelBody.querySelector(`[data-view="${kind}"]`);
      const current = kind === "objective" ? (t.objective || "") : (t.status_text || "");
      const ta = document.createElement("textarea");
      ta.value = current;
      view.replaceWith(ta);
      ta.focus();
      const save = async () => {
        const text = ta.value.trim();
        if (text && text !== current) {
          await api("POST", `/api/${kind}`, { thread_id: t.id, text });
        }
        refreshPanel();
      };
      ta.onblur = save;
      ta.onkeydown = e => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ta.blur();
        if (e.key === "Escape") { ta.onblur = null; refreshPanel(); }
      };
    };
  });

  const rename = panelBody.querySelector("#rename");
  rename.onchange = async () => {
    const name = rename.value.trim();
    if (name && name !== t.name) await api("POST", `/api/threads/${t.id}/rename`, { name });
    rename.blur();
  };
  rename.onkeydown = e => { if (e.key === "Enter") rename.blur(); };

  const composer = panelBody.querySelector("#composer");
  const submitNote = async () => {
    const text = composer.value.trim();
    if (!text) return;
    composer.value = "";
    await api("POST", "/api/notes", { thread_id: t.id, text });
    refreshPanel();
  };
  panelBody.querySelector("#add-note").onclick = submitNote;
  composer.onkeydown = e => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submitNote();
  };
}

backdrop.addEventListener("click", () => closePanel());
document.addEventListener("keydown", e => {
  if (e.key === "Escape") closePanel();
  if (e.key === "/" && document.activeElement === document.body) {
    e.preventDefault();
    filterInput.focus();
  }
});

/* ------------------------------------------------------------------ render */

function render() {
  renderSidebar();
  renderField();
}

/* -------------------------------------------------------------------- SSE */

function connect() {
  const es = new EventSource("/api/events");
  es.addEventListener("state", e => {
    snapshot = JSON.parse(e.data);
    conn.classList.add("live");
    connLabel.textContent = "live";
    render();
    if (openThreadId != null) refreshPanel();
  });
  es.onerror = () => {
    conn.classList.remove("live");
    connLabel.textContent = "reconnecting";
    es.close();
    setTimeout(connect, 2500);
  };
}

connect();
setInterval(() => { if (!panelFocused()) render(); }, 30000);

const deepLink = location.hash.match(/^#t(\d+)$/);
if (deepLink) openPanel(Number(deepLink[1]));
