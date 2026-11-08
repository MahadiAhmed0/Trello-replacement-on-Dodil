// Tasklane web page. Plain JS, no build step. Every string from the server is put on the page
// with textContent (through h()), never innerHTML, so card text can't inject markup.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { me: null, wsId: null, boardId: null, board: null, panel: null };
const phone = window.matchMedia("(max-width: 720px)");

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon"); svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name); svg.append(use);
  return svg;
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (method !== "GET") opts.headers["X-Tasklane"] = "1";
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = "/.dodil/auth/login?return_to=/"; throw new Error("sign in"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : r.statusText;
    const err = new Error(msg); err.status = r.status; throw err;
  }
  return data;
}

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, 3500);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { if (e.message !== "sign in") toast(e.message); } };

function remember(key, value) { try { localStorage.setItem("tasklane." + key, value); } catch (_) {} }
function recall(key) { try { return localStorage.getItem("tasklane." + key); } catch (_) { return null; } }

const canEdit = () => state.board && state.board.role !== "viewer";
const ws = () => state.me.workspaces.find((w) => w.id === state.wsId);
const initials = (email) => (email || "?").split("@")[0].split(/[._+-]/).map((p) => p[0] || "").join("").slice(0, 2).toUpperCase();

// ------------------------------------------------------------------ dates people read

const DAY = 864e5;
const localDate = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
const startOfToday = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d; };
const daysFromToday = (iso) => Math.round((localDate(iso) - startOfToday()) / DAY);
const shortDate = (iso) => localDate(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });

function dueChip(c) {
  if (!c.due_date) return null;
  const d = daysFromToday(c.due_date);
  const title = "Due " + localDate(c.due_date).toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  if (c.done) return h("span", { class: "chip", title }, shortDate(c.due_date));
  if (c.overdue || d < 0) return h("span", { class: "chip late", title }, d === -1 ? "1 day late" : `${Math.max(1, -d)} days late`);
  const text = d === 0 ? "Due today" : d === 1 ? "Due tomorrow" : d < 7
    ? "Due " + localDate(c.due_date).toLocaleDateString(undefined, { weekday: "short" }) : "Due " + shortDate(c.due_date);
  return h("span", { class: "chip" + (d <= 2 ? " soon" : ""), title }, text);
}

// ------------------------------------------------------------------ loading

async function loadMe() {
  state.me = await api("GET", "/api/me");
  $("who").textContent = state.me.email;
  const wss = state.me.workspaces;
  if (!wss.some((w) => w.id === state.wsId)) {
    const saved = recall("ws");
    state.wsId = (wss.find((w) => w.id === saved) || wss[0] || {}).id || null;
  }
  renderPickers();
}

function renderPickers() {
  const wsPick = $("ws-pick"), boardPick = $("board-pick");
  wsPick.replaceChildren(...state.me.workspaces.map((w) =>
    h("option", { value: w.id, selected: w.id === state.wsId }, w.name)),
    h("option", { value: "__new" }, "+ New workspace…"));
  wsPick.hidden = !state.me.workspaces.length;
  const w = ws();
  const boards = w ? w.boards : [];
  if (!boards.some((b) => b.id === state.boardId)) {
    const saved = recall("board");
    state.boardId = (boards.find((b) => b.id === saved) || boards[0] || {}).id || null;
  }
  boardPick.replaceChildren(...boards.map((b) =>
    h("option", { value: b.id, selected: b.id === state.boardId }, b.title)));
  boardPick.hidden = !boards.length;
}

async function loadBoard() {
  if (!state.boardId) { state.board = null; return renderEmpty(); }
  state.board = await api("GET", "/api/boards/" + encodeURIComponent(state.boardId));
  remember("board", state.boardId);
  renderBoard();
}

async function refreshLateCount() {
  try {
    const late = await api("GET", "/api/overdue");
    const dot = $("late-count");
    dot.textContent = late.length > 9 ? "9+" : String(late.length);
    dot.hidden = !late.length;
  } catch (_) { /* the badge is a nicety; never block the page on it */ }
}

async function refresh() {
  await loadMe();
  await loadBoard();
  refreshLateCount();
  if (state.panel) await openPanel(state.panel, true);
}

// ------------------------------------------------------------------ empty states

function renderEmpty() {
  $("board").hidden = true;
  const box = $("empty"); box.hidden = false;
  const w = ws();
  const mark = h("img", { class: "mark", src: "/static/logo.svg", alt: "" });
  if (!w) {
    const input = h("input", { placeholder: "e.g. Acme team", required: true, maxlength: 200, "aria-label": "Workspace name" });
    box.replaceChildren(mark, h("h1", {}, "Start your team's workspace"),
      h("p", {}, "You'll own it: invite people, decide who can edit, and set up boards."),
      h("p", { class: "note" }, "Waiting for an invite? Ask the owner to invite exactly ", h("b", {}, state.me.email), "."),
      h("form", { onsubmit: guard(async (e) => { e.preventDefault();
        const made = await api("POST", "/api/workspaces", { name: input.value });
        state.wsId = made.id; remember("ws", made.id); await refresh(); }) },
        input, h("button", { class: "primary" }, "Create workspace")));
    return;
  }
  if (w.role === "viewer") {
    box.replaceChildren(mark, h("h1", {}, w.name), h("p", {}, "You can view boards here, and none has been shared with you yet. The owner can put you on one."));
    return;
  }
  box.replaceChildren(mark, h("h1", {}, "Your first board"),
    h("p", {}, "A board holds lists of cards. It starts with To do, Doing and Done; add your own lists any time."),
    newBoardForm());
}

function newBoardForm() {
  const input = h("input", { placeholder: "e.g. Ops", required: true, maxlength: 200, "aria-label": "Board title" });
  return h("form", { onsubmit: guard(async (e) => { e.preventDefault();
    const b = await api("POST", "/api/boards", { title: input.value, workspace_id: state.wsId });
    state.boardId = b.id; await refresh(); }) },
    input, h("button", { class: "primary" }, "Create board"));
}

// ------------------------------------------------------------------ the board

function cardTile(c, { movable = false, withBoard = false } = {}) {
  const chips = [dueChip(c)];
  if (c.blocked_by && c.blocked_by.length) chips.push(h("span", { class: "chip blocked" }, "Blocked by " + c.blocked_by.map((n) => "#" + n).join(", ")));
  for (const l of c.labels || []) chips.push(h("span", { class: "chip label" }, l));
  if (c.assignee) chips.push(h("span", { class: "avatar" + (c.assignee === state.me.email ? " me" : ""), title: c.assignee }, initials(c.assignee)));
  const late = c.overdue, blocked = c.blocked_by && c.blocked_by.length;
  const el = h("div", { class: "card" + (movable ? " movable" : "") + (late ? " is-late" : blocked ? " is-blocked" : "") + (c.done ? " done" : ""),
    tabindex: 0, role: "button", dataset: { number: c.number },
    "aria-label": `Card ${c.number}: ${c.title}` + (late ? ", overdue" : "") + (blocked ? ", blocked" : ""),
    onclick: () => { if (!drag.suppressClick) openCard(c.number); },
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCard(c.number); } } },
  h("div", { class: "meta" }, h("span", { class: "num" }, "#" + c.number),
    withBoard ? h("span", { class: "where" }, `${c.board} · ${c.list}`) : null),
  h("span", { class: "title" }, c.title),
  chips.some(Boolean) ? h("div", { class: "chips" }, chips) : null);
  if (movable) enableDrag(el, c.number);
  return el;
}

function renderBoard() {
  const b = state.board, edit = canEdit();
  $("empty").hidden = true; $("board").hidden = false;
  $("board-title").textContent = b.title;
  $("board-role").textContent = b.role;
  $("new-board").hidden = !(ws() && ws().role !== "viewer");
  const lanes = b.lists.map((l) => {
    const cards = h("div", { class: "cards" },
      l.cards.length ? l.cards.map((c) => cardTile(c, { movable: edit }))
        : h("div", { class: "lane-empty" }, edit ? "No cards. Drop one here or add one below." : "No cards here."));
    return h("section", { class: "lane", "aria-label": l.name, dataset: { listId: l.id } },
      h("div", { class: "lane-head" }, h("span", {}, l.name), h("span", { class: "count" }, l.cards.length),
        l.is_done ? h("span", { class: "done" }, "Done list") : null),
      cards, edit ? addCardForm(l) : null);
  });
  if (edit) {
    const input = h("input", { placeholder: "+ Add a list", maxlength: 200, "aria-label": "New list name" });
    lanes.push(h("form", { class: "new-lane", onsubmit: guard(async (e) => { e.preventDefault();
      if (!input.value.trim()) return;
      await api("POST", `/api/boards/${b.id}/lists`, { name: input.value }); await loadBoard(); }) }, input));
  }
  $("lanes").replaceChildren(...lanes);
  renderLaneTabs();
}

function renderLaneTabs() {
  const tabs = $("lane-tabs");
  const lanes = [...$("lanes").querySelectorAll(".lane")];
  tabs.replaceChildren(...state.board.lists.map((l, i) => h("button", { type: "button",
    onclick: () => lanes[i].scrollIntoView({ behavior: "smooth", inline: "start", block: "nearest" }) },
    l.name + " ", h("span", { class: "n" }, l.cards.length))));
  const mark = () => {
    const box = $("lanes").getBoundingClientRect();
    let best = 0, bestDist = Infinity;
    lanes.forEach((ln, i) => { const d = Math.abs(ln.getBoundingClientRect().left - box.left - 16); if (d < bestDist) { bestDist = d; best = i; } });
    [...tabs.children].forEach((t, i) => t.setAttribute("aria-current", String(i === best)));
    tabs.children[best]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  };
  $("lanes").onscroll = () => { cancelAnimationFrame(mark.raf); mark.raf = requestAnimationFrame(mark); };
  mark();
}

function addCardForm(list) {
  const input = h("input", { placeholder: "+ Add a card", maxlength: 200, "aria-label": "New card in " + list.name, enterkeyhint: "done" });
  return h("form", { class: "add-card", onsubmit: guard(async (e) => { e.preventDefault();
    if (!input.value.trim()) return;
    const title = input.value; input.value = "";
    await api("POST", `/api/boards/${state.board.id}/cards`, { title, list_id: list.id });
    await loadBoard();
    const again = [...document.querySelectorAll(".add-card input")].find((x) => x.getAttribute("aria-label") === "New card in " + list.name);
    if (again && !phone.matches) again.focus(); }) }, input);
}

// ------------------------------------------------------------------ drag and drop
// Pointer events, so one path serves mouse, pen and touch. Mouse: drag after 5px of movement.
// Touch: press and hold ~250ms, so an ordinary swipe still scrolls the lists.

const drag = { active: null, suppressClick: false };

function enableDrag(card, number) {
  card.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || drag.active) return;
    const touch = e.pointerType !== "mouse";
    const start = { x: e.clientX, y: e.clientY };
    let timer = null, live = false;
    const begin = (x, y) => { live = true; startDrag(card, number, x, y); if (navigator.vibrate) navigator.vibrate(8); };
    const move = (ev) => {
      if (!live) {
        const dist = Math.hypot(ev.clientX - start.x, ev.clientY - start.y);
        if (touch) { if (dist > 8) stop(); return; }   // it's a scroll, not a hold
        if (dist < 5) return;
        begin(ev.clientX, ev.clientY);
      }
      moveDrag(ev.clientX, ev.clientY);
    };
    const up = (ev) => { const was = live; stop(); if (was) endDrag(ev.type !== "pointercancel"); };
    const stop = () => {
      clearTimeout(timer);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
    if (touch) timer = setTimeout(() => begin(start.x, start.y), 250);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  });
  card.addEventListener("contextmenu", (e) => { if (drag.active) e.preventDefault(); });
}

// While a touch drag is live, stop the page from scrolling under the finger.
document.addEventListener("touchmove", (e) => { if (drag.active) e.preventDefault(); }, { passive: false });

function startDrag(card, number, x, y) {
  const r = card.getBoundingClientRect();
  const ghost = card.cloneNode(true);
  ghost.classList.add("drag-ghost");
  ghost.style.width = r.width + "px";   // CSSOM writes are allowed by the page's CSP
  ghost.style.left = r.left + "px"; ghost.style.top = r.top + "px";
  document.body.append(ghost);
  const slot = h("div", { class: "slot" }); slot.style.height = r.height + "px";
  card.after(slot);
  card.classList.add("dragging");
  document.body.classList.add("is-dragging");
  drag.active = { card, number, ghost, slot, dx: x - r.left, dy: y - r.top, x, y, lane: card.closest(".lane") };
  scrollLoop();
}

function moveDrag(x, y) {
  const d = drag.active; if (!d) return;
  d.x = x; d.y = y;
  d.ghost.style.left = x - d.dx + "px"; d.ghost.style.top = y - d.dy + "px";
  const under = document.elementFromPoint(x, y);
  const lane = under && under.closest(".lane");
  document.querySelectorAll(".lane.drop").forEach((l) => l !== lane && l.classList.remove("drop"));
  if (!lane) return;
  lane.classList.add("drop"); d.lane = lane;
  const list = lane.querySelector(".cards");
  const below = [...list.querySelectorAll(".card:not(.dragging)")].find((el) => {
    const b = el.getBoundingClientRect(); return y < b.top + b.height / 2; });
  list.querySelector(".lane-empty")?.remove();
  if (below) { if (d.slot.nextElementSibling !== below) list.insertBefore(d.slot, below); }
  else if (list.lastElementChild !== d.slot) list.append(d.slot);
}

function scrollLoop() {
  const d = drag.active; if (!d) return;
  const lanes = $("lanes"), box = lanes.getBoundingClientRect(), edge = 48;
  if (d.x < box.left + edge) lanes.scrollLeft -= 14;
  else if (d.x > box.right - edge) lanes.scrollLeft += 14;
  const list = d.lane && d.lane.querySelector(".cards");
  if (list) {
    const lb = list.getBoundingClientRect();
    if (d.y < lb.top + 32) list.scrollTop -= 10; else if (d.y > lb.bottom - 32) list.scrollTop += 10;
  }
  requestAnimationFrame(scrollLoop);
}

async function endDrag(commit) {
  const d = drag.active; drag.active = null;
  document.body.classList.remove("is-dragging");
  document.querySelectorAll(".lane.drop").forEach((l) => l.classList.remove("drop"));
  d.ghost.remove();
  drag.suppressClick = true; setTimeout(() => { drag.suppressClick = false; }, 50);
  const lane = d.slot.closest(".lane");
  const next = d.slot.nextElementSibling;
  const before = next && next.classList.contains("card") ? Number(next.dataset.number) : null;
  const prev = d.slot.previousElementSibling;
  const unchanged = lane === d.card.closest(".lane") && (next === d.card || prev === d.card);
  if (!commit || !lane || unchanged) { d.slot.remove(); d.card.classList.remove("dragging"); return; }
  d.slot.replaceWith(d.card); d.card.classList.remove("dragging");   // optimistic: it lands now
  try {
    await api("POST", `/api/cards/${d.number}/move`, { list_id: lane.dataset.listId, before });
    const list = state.board.lists.find((l) => l.id === lane.dataset.listId);
    if (list && !phone.matches) toast(`Moved #${d.number} to ${list.name}`);
  } catch (e) { toast(e.message); }
  await loadBoard();
  refreshLateCount();
}

// ------------------------------------------------------------------ one card

async function openCard(number) {
  const c = await api("GET", "/api/cards/" + number).catch((e) => { toast(e.message); return null; });
  if (!c) return;
  const onBoard = state.board && state.board.id === c.board_id;
  const people = onBoard ? state.board.people.filter((p) => p.role !== "viewer") : [];
  const edit = onBoard ? canEdit() : false;
  const dis = !edit;
  const f = {
    title: h("input", { class: "dlg-title", value: c.title, maxlength: 200, disabled: dis, "aria-label": "Title" }),
    description: h("textarea", { disabled: dis, maxlength: 10000, placeholder: edit ? "Add details, links, a checklist…" : "" }, c.description || ""),
    assignee: h("select", { disabled: dis },
      h("option", { value: "" }, "Unassigned"),
      [...new Set([...people.map((p) => p.email), ...(c.assignee ? [c.assignee] : [])])].map((e) =>
        h("option", { value: e, selected: e === c.assignee }, e === state.me.email ? e + " (me)" : e))),
    due: h("input", { type: "date", value: c.due_date || "", disabled: dis }),
    labels: h("input", { value: (c.labels || []).join(", "), placeholder: "bug, urgent", disabled: dis, autocapitalize: "none" }),
    list: h("select", { disabled: dis }, (onBoard ? state.board.lists : []).map((l) =>
      h("option", { value: l.id, selected: l.id === c.list_id }, l.name))),
  };
  const dialog = $("card-dialog"), form = $("card-form");

  const save = guard(async (e) => {
    const btn = e && e.currentTarget; if (btn) btn.disabled = true;
    try {
      const body = { version: c.version, title: f.title.value, description: f.description.value,
        assignee: f.assignee.value, due_date: f.due.value,
        labels: f.labels.value.split(",").map((s) => s.trim()).filter(Boolean) };
      try { await api("PATCH", "/api/cards/" + c.number, body); }
      catch (err) { if (err.status === 409) { toast(err.message); await loadBoard(); return openCard(c.number); } throw err; }
      if (f.list.value && f.list.value !== c.list_id) await api("POST", `/api/cards/${c.number}/move`, { list_id: f.list.value });
      dialog.close(); toast("Saved #" + c.number); await loadBoard(); refreshLateCount();
    } finally { if (btn) btn.disabled = false; }
  });

  const aiOut = h("div", {});
  const busy = (btn, label) => { btn.disabled = true; const old = btn.lastChild.textContent; btn.lastChild.textContent = label; return () => { btn.disabled = false; btn.lastChild.textContent = old; }; };

  const blockerInput = h("input", { type: "number", min: 1, inputmode: "numeric", placeholder: "Card #", "aria-label": "Blocked by card number", disabled: dis });
  const blockers = h("div", {},
    c.blocked_by.length ? h("ul", { class: "plain" }, c.blocked_by.map((n) => h("li", { class: "row" },
      h("span", { class: "grow" }, h("span", { class: "chip blocked" }, "Blocked by #" + n)),
      edit ? h("button", { type: "button", class: "quiet danger", onclick: guard(async () => {
        await api("DELETE", `/api/cards/${c.number}/blockers/${n}`); await loadBoard(); openCard(c.number); }) }, "Remove") : null)))
      : h("p", { class: "note" }, "Nothing blocks this card."),
    c.blocks.length ? h("p", { class: "note" }, "This card blocks " + c.blocks.map((n) => "#" + n).join(", ") + ".") : null,
    h("div", { class: "row" },
      edit ? [blockerInput, h("button", { type: "button", onclick: guard(async () => {
        if (!blockerInput.value) return;
        await api("POST", `/api/cards/${c.number}/blockers`, { blocked_by: Number(blockerInput.value) });
        await loadBoard(); openCard(c.number); }) }, "Add blocker")] : null,
      h("button", { type: "button", class: "quiet", onclick: guard(async () => {
        const r = await api("GET", `/api/cards/${c.number}/blocking`);
        aiOut.replaceChildren(h("h3", {}, "Everything in the way"), r.blocking.length
          ? h("div", {}, r.blocking.map((x) => cardTile(x, { withBoard: true }))) : h("p", { class: "note" }, "Nothing open is blocking this card.")); }) },
        "Show the whole chain")));

  const ai = h("div", { class: "row" },
    h("button", { type: "button", onclick: guard(async (e) => {
      const done = busy(e.currentTarget, "Thinking…");
      try {
        const r = await api("POST", `/api/cards/${c.number}/checklist`, { apply: false });
        aiOut.replaceChildren(h("h3", {}, "Suggested checklist"), h("ul", { class: "steps" }, r.checklist.map((s) => h("li", {}, s))),
          edit ? h("button", { type: "button", class: "primary", onclick: guard(async (ev) => {
            ev.currentTarget.disabled = true;
            await api("POST", `/api/cards/${c.number}/checklist`, { apply: true });
            await loadBoard(); openCard(c.number); }) }, "Add to description") : null);
      } finally { done(); } }) }, icon("summary"), h("span", {}, "Turn into a checklist")),
    h("button", { type: "button", onclick: guard(async (e) => {
      const done = busy(e.currentTarget, "Searching…");
      try {
        const r = await api("GET", `/api/cards/${c.number}/similar`);
        aiOut.replaceChildren(h("h3", {}, "Similar cards"), r.length ? h("div", {}, r.map((x) => cardTile(x, { withBoard: true })))
          : h("p", { class: "note" }, "Nothing similar yet."));
      } finally { done(); } }) }, icon("search"), h("span", {}, "Find similar")));

  form.replaceChildren(
    h("div", { class: "dlg-head" }, h("span", { class: "num" }, "#" + c.number),
      h("span", { class: "crumb" }, `${c.board} · ${c.list}` + (dis ? " · view only" : "")),
      c.overdue ? h("span", { class: "chip late" }, "Overdue") : null,
      h("button", { type: "button", class: "icon-btn", "aria-label": "Close", onclick: () => dialog.close() }, icon("close"))),
    h("div", { class: "dlg-scroll" },
      f.title,
      h("div", { class: "grid" },
        h("label", {}, "List", f.list), h("label", {}, "Assignee", f.assignee),
        h("label", {}, "Due date", f.due), h("label", {}, "Labels, comma separated", f.labels)),
      h("label", { class: "field" }, "Description", f.description),
      h("div", { class: "dlg-section" }, h("h3", {}, "Blocked by"), blockers),
      h("div", { class: "dlg-section" }, h("h3", {}, "Assistant"), ai, aiOut),
      h("div", { class: "dlg-section" }, h("h3", {}, "History"), h("div", { class: "history" }, c.history.map(eventRow)))),
    edit ? h("div", { class: "dlg-foot" }, h("button", { type: "button", onclick: () => dialog.close() }, "Cancel"),
      h("button", { type: "button", class: "primary", onclick: save }, "Save changes")) : null);
  if (!dialog.open) dialog.showModal();
  if (phone.matches) form.querySelector(".dlg-scroll").scrollTop = 0; else if (edit) f.title.focus();
}

function eventRow(e) {
  const when = e.at ? new Date(e.at) : null;
  return h("div", { class: "event" },
    h("time", { datetime: e.at, title: when ? when.toLocaleString() : "" }, when ? relTime(when) : ""),
    h("div", {}, h("b", {}, e.actor === state.me.email ? "You" : e.actor), " ", e.action),
    (e.changes || []).filter((d) => d.field !== "description").map((d) =>
      h("div", { class: "diff" }, `${d.field.replace("_", " ")}: ${fmt(d.before)} → ${fmt(d.after)}`)),
    (e.changes || []).some((d) => d.field === "description") ? h("div", { class: "diff" }, "description changed") : null);
}
const fmt = (v) => v === null || v === undefined || v === "" ? "—" : Array.isArray(v) ? (v.join(", ") || "—") : String(v);
function relTime(d) {
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  if (s < 7 * 86400) return Math.floor(s / 86400) + " d ago";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// ------------------------------------------------------------------ side panels

function calm(title, text) { return h("div", { class: "calm" }, h("b", {}, title), text); }

async function openPanel(kind, quiet) {
  state.panel = kind;
  document.querySelectorAll("[data-panel]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.panel === kind)));
  const body = $("panel-body"), title = $("panel-title");
  $("panel").hidden = false; $("scrim").hidden = !phone.matches;
  if (!quiet) body.replaceChildren(h("p", { class: "note" }, "Loading…"));
  const list = (cards) => cards.map((c) => cardTile(c, { withBoard: true }));
  if (kind === "plate") {
    title.textContent = "My plate this week";
    const r = await api("GET", "/api/my-cards?days=7");
    const total = r.overdue.length + r.due_soon.length + r.no_due_date.length + r.later.length;
    body.replaceChildren(...(total ? [
      ...(r.overdue.length ? [h("h3", {}, "Late"), ...list(r.overdue)] : []),
      ...(r.due_soon.length ? [h("h3", {}, "Due by " + shortDate(r.until)), ...list(r.due_soon)] : []),
      ...(r.no_due_date.length ? [h("h3", {}, "No due date"), ...list(r.no_due_date)] : []),
      ...(r.later.length ? [h("h3", {}, "Later"), ...list(r.later)] : []),
    ] : [calm("Your plate is clear.", "Nothing open is assigned to you.")]));
  } else if (kind === "overdue") {
    title.textContent = "Overdue";
    const cards = await api("GET", "/api/overdue");
    body.replaceChildren(...(cards.length ? list(cards) : [calm("Nothing is late.", "Every open card with a due date is on time.")]));
  } else if (kind === "activity" && state.board) {
    title.textContent = "Activity · " + state.board.title;
    const ev = await api("GET", `/api/boards/${state.board.id}/activity?limit=100`);
    body.replaceChildren(...(ev.length ? ev.map(eventRow) : [calm("No activity yet.", "Changes to this board will show up here.")]));
  } else if (kind === "summary" && state.board) {
    title.textContent = "Today on " + state.board.title;
    if (!quiet) body.replaceChildren(h("p", { class: "note" }, "Reading the board and writing a summary. This can take up to a minute."));
    const r = await api("GET", `/api/boards/${state.board.id}/summary`);
    const f = r.facts;
    const facts = (items, none) => h("ul", { class: "plain" }, (items.length ? items : [none]).map((x) => h("li", {}, x)));
    body.replaceChildren(
      r.summary ? h("div", { class: "summary" }, r.summary) : h("p", { class: "note" }, "The written summary is unavailable right now. Here are the facts."),
      h("h3", {}, "Cards per list"), h("p", {}, Object.entries(f.per_list).map(([k, v]) => `${k} ${v}`).join("  ·  ")),
      h("h3", {}, "Late"), facts(f.overdue, "Nothing late."),
      h("h3", {}, "Blocked"), facts(f.blocked, "Nothing blocked."),
      h("h3", {}, "Last 24 hours"), facts(f.last_24h, "No changes."));
  } else if (kind === "people") {
    await renderPeople(body, title);
  } else if (kind === "search") {
    title.textContent = "Search";
    const q = $("q").value.trim();
    const cards = await api("GET", "/api/search?q=" + encodeURIComponent(q));
    body.replaceChildren(...(cards.length ? list(cards) : [calm("No matches.", `No card mentions “${q}”.`)]));
  } else {
    closePanel();
  }
}

async function renderPeople(body, title) {
  const w = ws();
  const foot = h("div", { class: "panel-foot" }, h("span", { class: "grow" }, "Signed in as " + state.me.email), h("a", { class: "link", href: "/.dodil/auth/logout" }, "Sign out"));
  if (!w) { title.textContent = "People"; body.replaceChildren(calm("No workspace yet.", "Create one to invite people."), foot); return; }
  const r = await api("GET", `/api/workspaces/${w.id}/people`);
  const owner = r.your_role === "owner";
  title.textContent = "People · " + r.name;
  const avatar = (email) => h("span", { class: "avatar" + (email === state.me.email ? " me" : "") }, initials(email));
  const rows = r.people.map((p) => h("div", { class: "person" }, avatar(p.email),
    h("span", { class: "grow", title: p.email }, p.email, p.email === state.me.email ? " (you)" : ""),
    owner && p.role !== "owner"
      ? [h("select", { "aria-label": "Role of " + p.email, onchange: guard(async (e) => {
          await api("PUT", `/api/workspaces/${w.id}/members/${encodeURIComponent(p.email)}`, { role: e.target.value });
          toast("Role updated"); await refresh(); }) },
          ["member", "viewer"].map((x) => h("option", { value: x, selected: x === p.role }, x))),
        h("button", { type: "button", class: "icon-btn danger", "aria-label": "Remove " + p.email, title: "Remove from workspace", onclick: guard(async () => {
          if (!confirm(`Remove ${p.email} from ${r.name} and all its boards?`)) return;
          await api("DELETE", `/api/workspaces/${w.id}/members/${encodeURIComponent(p.email)}`); await refresh(); }) }, icon("close"))]
      : h("span", { class: "badge" }, p.role)));
  const parts = [h("h3", {}, "Workspace"), ...rows];
  if (owner) {
    const email = h("input", { type: "email", placeholder: "name@company.com", required: true, class: "grow", "aria-label": "Email to invite", autocapitalize: "none" });
    const role = h("select", { "aria-label": "Role" }, h("option", { value: "member" }, "member"), h("option", { value: "viewer" }, "viewer"));
    parts.push(h("h3", {}, "Invite"), h("form", { class: "row", onsubmit: guard(async (e) => { e.preventDefault();
      await api("POST", `/api/workspaces/${w.id}/members`, { email: email.value, role: role.value });
      toast(`Invited ${email.value}`); await refresh(); }) },
      email, role, h("button", { class: "primary" }, "Invite")));
    parts.push(h("p", { class: "note" }, "They sign in to this app with that exact email. Members can edit; viewers can only look. Then put them on the boards they should see."));
  }
  const b = state.board;
  if (b && b.workspace_id === w.id) {
    parts.push(h("h3", {}, "On " + b.title));
    for (const p of b.people) parts.push(h("div", { class: "person" }, avatar(p.email), h("span", { class: "grow", title: p.email }, p.email), h("span", { class: "badge" }, p.role),
      owner && p.role !== "owner" ? h("button", { type: "button", class: "quiet danger", onclick: guard(async () => {
        await api("DELETE", `/api/boards/${b.id}/people/${encodeURIComponent(p.email)}`); await refresh(); }) }, "Take off") : null));
    const off = r.people.filter((p) => !b.people.some((q) => q.email === p.email));
    if (owner && off.length) {
      const pick = h("select", { class: "grow", "aria-label": "Person to add to the board" }, off.map((p) => h("option", { value: p.email }, `${p.email} (${p.role})`)));
      parts.push(h("div", { class: "row" }, pick, h("button", { type: "button", onclick: guard(async () => {
        await api("POST", `/api/boards/${b.id}/people`, { email: pick.value }); await refresh(); }) }, "Add to board")));
    }
  }
  parts.push(foot);
  body.replaceChildren(...parts);
}

function closePanel() {
  state.panel = null; $("panel").hidden = true; $("scrim").hidden = true;
  document.querySelectorAll("[data-panel]").forEach((b) => b.setAttribute("aria-pressed", "false"));
}

// ------------------------------------------------------------------ wiring

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-panel]");
  if (!btn) return;
  if (state.panel === btn.dataset.panel) return closePanel();   // a second tap closes it
  guard(openPanel)(btn.dataset.panel);
});
$("panel-close").addEventListener("click", closePanel);
$("scrim").addEventListener("click", closePanel);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("card-dialog").open) closePanel();
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !$("card-dialog").open) { e.preventDefault(); $("q").focus(); }
});
$("search").addEventListener("submit", (e) => { e.preventDefault(); if ($("q").value.trim()) { $("q").blur(); guard(openPanel)("search"); } });
$("ws-pick").addEventListener("change", guard(async (e) => {
  if (e.target.value === "__new") {
    const name = prompt("Name of the new workspace");
    if (name && name.trim()) { const w = await api("POST", "/api/workspaces", { name }); state.wsId = w.id; }
  } else { state.wsId = e.target.value; state.boardId = null; }
  remember("ws", state.wsId); await refresh();
}));
$("board-pick").addEventListener("change", guard(async (e) => { state.boardId = e.target.value; await loadBoard(); }));
$("new-board").addEventListener("click", guard(async () => {
  const title = prompt("Title of the new board");
  if (!title || !title.trim()) return;
  const b = await api("POST", "/api/boards", { title, workspace_id: state.wsId });
  state.boardId = b.id; await refresh();
}));
phone.addEventListener("change", () => { if (state.board) renderBoard(); if (state.panel) $("scrim").hidden = !phone.matches; });

guard(refresh)();
