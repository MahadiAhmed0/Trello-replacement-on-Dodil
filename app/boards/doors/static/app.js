// Tasklane web page. Plain JS, no build step. Every string from the server is put on the page
// with textContent (through h()), never innerHTML, so card text can't inject markup.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { me: null, wsId: null, boardId: null, board: null, panel: null };

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
const initials = (email) => (email || "?").split("@")[0].split(/[._-]/).map((p) => p[0] || "").join("").slice(0, 2).toUpperCase();
const today = () => new Date().toISOString().slice(0, 10);

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
    h("option", { value: w.id, selected: w.id === state.wsId }, `${w.name} (${w.role})`)),
    h("option", { value: "__new" }, "+ New workspace"));
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

async function refresh() {
  await loadMe();
  await loadBoard();
  if (state.panel) await openPanel(state.panel, true);
}

// ------------------------------------------------------------------ empty states

function renderEmpty() {
  $("board").hidden = true;
  const box = $("empty"); box.hidden = false;
  const w = ws();
  if (!w) {
    const input = h("input", { placeholder: "Workspace name, e.g. Acme team", required: true, maxlength: 200 });
    box.replaceChildren(h("h1", {}, "Welcome to Tasklane"),
      h("p", {}, "Start a workspace for your team. You'll be its owner and can invite people. ",
        "If someone invited you, ask them to check the email they used: it must be ", h("b", {}, state.me.email), "."),
      h("form", { onsubmit: guard(async (e) => { e.preventDefault();
        const made = await api("POST", "/api/workspaces", { name: input.value });
        state.wsId = made.id; remember("ws", made.id); await refresh(); }) },
        input, h("button", { class: "primary" }, "Create workspace")));
    return;
  }
  if (w.role === "viewer") {
    box.replaceChildren(h("h1", {}, w.name), h("p", {}, "You're a viewer here and no boards have been shared with you yet."));
    return;
  }
  box.replaceChildren(h("h1", {}, w.name), h("p", {}, "No boards yet. Create the first one."), newBoardForm());
}

function newBoardForm() {
  const input = h("input", { placeholder: "Board title, e.g. Ops", required: true, maxlength: 200 });
  return h("form", { onsubmit: guard(async (e) => { e.preventDefault();
    const b = await api("POST", "/api/boards", { title: input.value, workspace_id: state.wsId });
    state.boardId = b.id; await refresh(); }) },
    input, h("button", { class: "primary" }, "Create board"));
}

// ------------------------------------------------------------------ the board

function cardTile(c, { draggable = false, withBoard = false } = {}) {
  const chips = [];
  if (c.overdue) chips.push(h("span", { class: "chip late" }, "Overdue " + c.due_date));
  else if (c.due_date && !c.done) {
    const days = (new Date(c.due_date) - new Date(today())) / 864e5;
    chips.push(h("span", { class: "chip" + (days <= 2 ? " soon" : "") }, "Due " + c.due_date));
  }
  if (c.blocked_by && c.blocked_by.length) chips.push(h("span", { class: "chip blocked" }, "Blocked by " + c.blocked_by.map((n) => "#" + n).join(", ")));
  for (const l of c.labels || []) chips.push(h("span", { class: "chip label" }, l));
  if (c.assignee) chips.push(h("span", { class: "avatar", title: c.assignee }, initials(c.assignee)));
  return h("div", { class: "card", tabindex: 0, role: "button", draggable: draggable ? "true" : null,
    dataset: { number: c.number },
    "aria-label": `Card ${c.number}: ${c.title}`,
    onclick: () => openCard(c.number),
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCard(c.number); } },
    ondragstart: (e) => { e.dataTransfer.setData("text/plain", String(c.number)); e.dataTransfer.effectAllowed = "move"; e.currentTarget.classList.add("dragging"); },
    ondragend: (e) => e.currentTarget.classList.remove("dragging") },
  h("span", { class: "num" }, "#" + c.number + (withBoard ? " · " + c.board : "")),
  h("span", { class: "title" }, c.title),
  chips.length ? h("div", { class: "chips" }, chips) : null);
}

function renderBoard() {
  const b = state.board, edit = canEdit();
  $("empty").hidden = true; $("board").hidden = false;
  $("board-title").textContent = b.title;
  $("board-role").textContent = b.role;
  $("new-board").hidden = !edit;
  const lanes = b.lists.map((l) => {
    const cards = h("div", { class: "cards" }, l.cards.map((c) => cardTile(c, { draggable: edit })));
    const lane = h("section", { class: "lane", "aria-label": l.name },
      h("div", { class: "lane-head" }, l.name, h("span", { class: "count" }, l.cards.length),
        l.is_done ? h("span", { class: "done" }, "done") : null),
      cards, edit ? addCardForm(l) : null);
    if (edit) wireDrop(lane, cards, l);
    return lane;
  });
  if (edit) {
    const input = h("input", { placeholder: "+ Add a list", maxlength: 200, "aria-label": "New list name" });
    lanes.push(h("form", { class: "new-lane", onsubmit: guard(async (e) => { e.preventDefault();
      if (!input.value.trim()) return;
      await api("POST", `/api/boards/${b.id}/lists`, { name: input.value }); await loadBoard(); }) }, input));
  }
  $("lanes").replaceChildren(...lanes);
}

function addCardForm(list) {
  const input = h("input", { placeholder: "+ Add a card", maxlength: 200, "aria-label": "New card in " + list.name });
  return h("form", { class: "add-card", onsubmit: guard(async (e) => { e.preventDefault();
    if (!input.value.trim()) return;
    await api("POST", `/api/boards/${state.board.id}/cards`, { title: input.value, list_id: list.id });
    await loadBoard(); }) }, input);
}

function wireDrop(lane, cards, list) {
  lane.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; lane.classList.add("drop"); });
  lane.addEventListener("dragleave", (e) => { if (!lane.contains(e.relatedTarget)) lane.classList.remove("drop"); });
  lane.addEventListener("drop", guard(async (e) => {
    e.preventDefault(); lane.classList.remove("drop");
    const number = Number(e.dataTransfer.getData("text/plain"));
    if (!number) return;
    const below = [...cards.querySelectorAll(".card:not(.dragging)")].find((el) => {
      const r = el.getBoundingClientRect(); return e.clientY < r.top + r.height / 2; });
    const before = below ? Number(below.dataset.number) : null;
    if (before === number) return;
    await api("POST", `/api/cards/${number}/move`, { list_id: list.id, before });
    await loadBoard();
  }));
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
    title: h("input", { value: c.title, maxlength: 200, disabled: dis, "aria-label": "Title" }),
    description: h("textarea", { disabled: dis, maxlength: 10000 }, c.description || ""),
    assignee: h("select", { disabled: dis },
      h("option", { value: "" }, "Unassigned"),
      [...new Set([...people.map((p) => p.email), ...(c.assignee ? [c.assignee] : [])])].map((e) =>
        h("option", { value: e, selected: e === c.assignee }, e))),
    due: h("input", { type: "date", value: c.due_date || "", disabled: dis }),
    labels: h("input", { value: (c.labels || []).join(", "), placeholder: "bug, urgent", disabled: dis }),
    list: h("select", { disabled: dis }, (onBoard ? state.board.lists : []).map((l) =>
      h("option", { value: l.id, selected: l.id === c.list_id }, l.name))),
  };
  const dialog = $("card-dialog"), form = $("card-form");

  const save = guard(async () => {
    const body = { version: c.version, title: f.title.value, description: f.description.value,
      assignee: f.assignee.value, due_date: f.due.value,
      labels: f.labels.value.split(",").map((s) => s.trim()).filter(Boolean) };
    try { await api("PATCH", "/api/cards/" + c.number, body); }
    catch (e) { if (e.status === 409) { toast(e.message); await loadBoard(); return openCard(c.number); } throw e; }
    if (f.list.value && f.list.value !== c.list_id) await api("POST", `/api/cards/${c.number}/move`, { list_id: f.list.value });
    dialog.close(); toast("Saved #" + c.number); await loadBoard();
  });

  const blockerInput = h("input", { type: "number", min: 1, placeholder: "card #", "aria-label": "Blocked by card number", disabled: dis });
  const blockers = h("div", {},
    c.blocked_by.length ? h("ul", { class: "plain" }, c.blocked_by.map((n) => h("li", { class: "row" },
      h("span", { class: "grow" }, "Blocked by #" + n),
      edit ? h("button", { type: "button", class: "ghost danger", onclick: guard(async () => {
        await api("DELETE", `/api/cards/${c.number}/blockers/${n}`); await loadBoard(); openCard(c.number); }) }, "Remove") : null)))
      : h("p", { class: "muted" }, "Nothing blocks this card."),
    c.blocks.length ? h("p", { class: "muted" }, "This card blocks " + c.blocks.map((n) => "#" + n).join(", ") + ".") : null,
    edit ? h("div", { class: "row" }, blockerInput, h("button", { type: "button", onclick: guard(async () => {
      if (!blockerInput.value) return;
      await api("POST", `/api/cards/${c.number}/blockers`, { blocked_by: Number(blockerInput.value) });
      await loadBoard(); openCard(c.number); }) }, "Add blocker")) : null,
    h("button", { type: "button", class: "ghost", onclick: guard(async () => {
      const r = await api("GET", `/api/cards/${c.number}/blocking`);
      aiOut.replaceChildren(h("h3", {}, "What's in the way"), r.blocking.length
        ? h("div", {}, r.blocking.map((x) => cardTile(x, { withBoard: true }))) : h("p", { class: "muted" }, "Nothing open is blocking this card.")); }) },
      "Show the whole chain"));

  const aiOut = h("div", {});
  const ai = h("div", { class: "row" },
    h("button", { type: "button", onclick: guard(async (e) => {
      e.target.disabled = true;
      try {
        const r = await api("POST", `/api/cards/${c.number}/checklist`, { apply: false });
        aiOut.replaceChildren(h("h3", {}, "Suggested checklist"), h("ul", {}, r.checklist.map((s) => h("li", {}, s))),
          edit ? h("button", { type: "button", class: "primary", onclick: guard(async () => {
            await api("POST", `/api/cards/${c.number}/checklist`, { apply: true });
            await loadBoard(); openCard(c.number); }) }, "Add to description") : null);
      } finally { e.target.disabled = false; } }) }, "Turn into a checklist"),
    h("button", { type: "button", onclick: guard(async (e) => {
      e.target.disabled = true;
      try {
        const r = await api("GET", `/api/cards/${c.number}/similar`);
        aiOut.replaceChildren(h("h3", {}, "Similar cards"), r.length ? h("div", {}, r.map((x) => cardTile(x, { withBoard: true })))
          : h("p", { class: "muted" }, "Nothing similar yet."));
      } finally { e.target.disabled = false; } }) }, "Find similar cards"));

  form.replaceChildren(
    h("div", { class: "dlg-head" }, h("span", { class: "num" }, "#" + c.number), f.title,
      h("button", { type: "button", class: "ghost", "aria-label": "Close", onclick: () => dialog.close() }, "✕")),
    h("p", { class: "muted" }, `${c.board} · ${c.list}`, c.overdue ? h("span", { class: "chip late" }, " overdue") : null,
      dis ? " · read only" : null),
    h("div", { class: "grid" },
      h("label", {}, "List", f.list), h("label", {}, "Assignee", f.assignee),
      h("label", {}, "Due date", f.due), h("label", {}, "Labels (comma separated)", f.labels)),
    h("label", { class: "field" }, "Description", f.description),
    edit ? h("div", { class: "dlg-foot" }, h("button", { type: "button", onclick: () => dialog.close() }, "Cancel"),
      h("button", { type: "button", class: "primary", onclick: save }, "Save")) : null,
    h("div", { class: "dlg-section" }, h("h3", {}, "Dependencies"), blockers),
    h("div", { class: "dlg-section" }, h("h3", {}, "Assistant (Dodil Models)"), ai, aiOut),
    h("div", { class: "dlg-section" }, h("h3", {}, "History"), h("div", { class: "history" }, c.history.map(eventRow))));
  if (!dialog.open) dialog.showModal();
}

function eventRow(e) {
  return h("div", { class: "event" },
    h("time", { datetime: e.at }, e.at ? new Date(e.at).toLocaleString() : ""),
    h("div", {}, h("b", {}, e.actor), " ", e.action),
    (e.changes || []).filter((d) => d.field !== "description").map((d) =>
      h("div", { class: "diff" }, `${d.field}: ${fmt(d.before)} → ${fmt(d.after)}`)),
    (e.changes || []).some((d) => d.field === "description") ? h("div", { class: "diff" }, "description changed") : null);
}
const fmt = (v) => v === null || v === undefined || v === "" ? "—" : Array.isArray(v) ? (v.join(", ") || "—") : String(v);

// ------------------------------------------------------------------ side panels

async function openPanel(kind, quiet) {
  state.panel = kind;
  const body = $("panel-body"), title = $("panel-title");
  $("panel").hidden = false;
  if (!quiet) body.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  const list = (cards, none) => cards.length ? cards.map((c) => cardTile(c, { withBoard: true })) : [h("p", { class: "muted" }, none)];
  if (kind === "plate") {
    title.textContent = "My plate this week";
    const r = await api("GET", "/api/my-cards?days=7");
    body.replaceChildren(h("h3", {}, "Overdue"), ...list(r.overdue, "Nothing overdue."),
      h("h3", {}, "Due by " + r.until), ...list(r.due_soon, "Nothing due this week."),
      h("h3", {}, "No due date"), ...list(r.no_due_date, "None."),
      ...(r.later.length ? [h("h3", {}, "Later"), ...list(r.later, "")] : []));
  } else if (kind === "overdue") {
    title.textContent = "Overdue";
    body.replaceChildren(...list(await api("GET", "/api/overdue"), "Nothing is overdue. 🎉"));
  } else if (kind === "activity" && state.board) {
    title.textContent = "Activity · " + state.board.title;
    const ev = await api("GET", `/api/boards/${state.board.id}/activity?limit=100`);
    body.replaceChildren(...(ev.length ? ev.map(eventRow) : [h("p", { class: "muted" }, "No activity yet.")]));
  } else if (kind === "summary" && state.board) {
    title.textContent = "Daily summary · " + state.board.title;
    const r = await api("GET", `/api/boards/${state.board.id}/summary`);
    const f = r.facts;
    body.replaceChildren(
      r.summary ? h("div", { class: "summary" }, r.summary) : h("p", { class: "muted" }, "The AI summary is unavailable right now; here are the facts."),
      h("h3", {}, "Cards per list"), h("p", {}, Object.entries(f.per_list).map(([k, v]) => `${k}: ${v}`).join(" · ")),
      h("h3", {}, "Overdue"), h("ul", { class: "plain" }, (f.overdue.length ? f.overdue : ["none"]).map((x) => h("li", {}, x))),
      h("h3", {}, "Blocked"), h("ul", { class: "plain" }, (f.blocked.length ? f.blocked : ["none"]).map((x) => h("li", {}, x))),
      h("h3", {}, "Last 24 hours"), h("ul", { class: "plain" }, (f.last_24h.length ? f.last_24h : ["no changes"]).map((x) => h("li", {}, x))));
  } else if (kind === "people") {
    await renderPeople(body, title);
  } else if (kind === "search") {
    title.textContent = "Search";
    const q = $("q").value.trim();
    body.replaceChildren(...list(await api("GET", "/api/search?q=" + encodeURIComponent(q)), "No cards match “" + q + "”."));
  } else {
    closePanel();
  }
}

async function renderPeople(body, title) {
  const w = ws();
  if (!w) { title.textContent = "People"; body.replaceChildren(h("p", { class: "muted" }, "Create a workspace first.")); return; }
  const r = await api("GET", `/api/workspaces/${w.id}/people`);
  const owner = r.your_role === "owner";
  title.textContent = "People · " + r.name;
  const rows = r.people.map((p) => h("div", { class: "row" },
    h("span", { class: "grow" }, p.email, p.email === state.me.email ? " (you)" : ""),
    owner && p.role !== "owner"
      ? [h("select", { "aria-label": "Role of " + p.email, onchange: guard(async (e) => {
          await api("PUT", `/api/workspaces/${w.id}/members/${encodeURIComponent(p.email)}`, { role: e.target.value });
          toast("Role updated"); await refresh(); }) },
          ["member", "viewer"].map((x) => h("option", { value: x, selected: x === p.role }, x))),
        h("button", { type: "button", class: "ghost danger", onclick: guard(async () => {
          if (!confirm(`Remove ${p.email} from ${r.name} and all its boards?`)) return;
          await api("DELETE", `/api/workspaces/${w.id}/members/${encodeURIComponent(p.email)}`); await refresh(); }) }, "Remove")]
      : h("span", { class: "badge" }, p.role)));
  const parts = [h("h3", {}, "Workspace"), ...rows];
  if (owner) {
    const email = h("input", { type: "email", placeholder: "name@company.com", required: true, class: "grow", "aria-label": "Email to invite" });
    const role = h("select", { "aria-label": "Role" }, h("option", { value: "member" }, "member"), h("option", { value: "viewer" }, "viewer"));
    parts.push(h("form", { class: "row", onsubmit: guard(async (e) => { e.preventDefault();
      await api("POST", `/api/workspaces/${w.id}/members`, { email: email.value, role: role.value });
      toast(`Invited ${email.value}. They sign in with that email.`); await refresh(); }) },
      email, role, h("button", { class: "primary" }, "Invite")));
    parts.push(h("p", { class: "muted" }, "Invited people sign in to this app with that email address. Then put them on the boards they should see."));
  }
  const b = state.board;
  if (b && b.workspace_id === w.id) {
    parts.push(h("h3", {}, "On the board “" + b.title + "”"));
    for (const p of b.people) parts.push(h("div", { class: "row" }, h("span", { class: "grow" }, p.email), h("span", { class: "badge" }, p.role),
      owner && p.role !== "owner" ? h("button", { type: "button", class: "ghost danger", onclick: guard(async () => {
        await api("DELETE", `/api/boards/${b.id}/people/${encodeURIComponent(p.email)}`); await refresh(); }) }, "Take off") : null));
    const off = r.people.filter((p) => !b.people.some((q) => q.email === p.email));
    if (owner && off.length) {
      const pick = h("select", { class: "grow", "aria-label": "Person to add to the board" }, off.map((p) => h("option", { value: p.email }, `${p.email} (${p.role})`)));
      parts.push(h("div", { class: "row" }, pick, h("button", { type: "button", onclick: guard(async () => {
        await api("POST", `/api/boards/${b.id}/people`, { email: pick.value }); await refresh(); }) }, "Add to board")));
    }
  }
  body.replaceChildren(...parts);
}

function closePanel() { state.panel = null; $("panel").hidden = true; }

// ------------------------------------------------------------------ wiring

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-panel]");
  if (btn) guard(openPanel)(btn.dataset.panel);
});
$("panel-close").addEventListener("click", closePanel);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("card-dialog").open) closePanel(); });
$("search").addEventListener("submit", (e) => { e.preventDefault(); if ($("q").value.trim()) guard(openPanel)("search"); });
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

guard(refresh)();
