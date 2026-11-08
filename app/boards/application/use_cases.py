"""Every action a person can take. The web routes and the MCP tools both call these and nothing
else, so a rule cannot be skipped by using the other door.

Each use case takes the acting Person first. What a person may SEE is decided here, by their own
identity, so Claude connected as Ana sees exactly the boards Ana sees on the web page. A board or
card you can't see answers "not found", never "forbidden": its existence is not leaked.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from boards.application.ports import Assistant, Duplicate, Event, Repository, VectorIndex
from boards.domain.model import (DEFAULT_LISTS, Board, BoardList, Card, Forbidden, Invalid,
                                 Person, Role, Stale, Workspace, blocking_chain, clean_title,
                                 normalize_email, sort_key_between, would_cycle)

log = logging.getLogger("tasklane")


class NotFound(Exception):
    pass


def _id() -> str:
    return uuid.uuid4().hex[:12]


def parse_due(value, today: Optional[date] = None) -> Optional[date]:
    """'2026-11-20', 'today', 'tomorrow', '' / 'none' (no due date)."""
    if value is None or isinstance(value, date):
        return value
    v = str(value).strip().lower()
    if v in ("", "none", "null"):
        return None
    today = today or date.today()
    if v == "today":
        return today
    if v == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        raise Invalid(f"due date {value!r}: use YYYY-MM-DD, 'today' or 'tomorrow'")


def parse_number(value) -> int:
    m = re.fullmatch(r"\s*#?\s*(\d{1,12})\s*", str(value))
    if not m:
        raise Invalid(f"{value!r} is not a card number (cards are numbered: 12, #12)")
    return int(m.group(1))


class _View:
    """What one person can see right now: their workspaces, boards and lists."""

    def __init__(self, person: Person, workspaces: list, boards: list, lists: list):
        self.person = person
        self.workspaces = {w.id: w for w in workspaces}
        self.boards = {b.id: b for b in boards
                       if b.workspace_id in self.workspaces
                       and b.role_of(person, self.workspaces[b.workspace_id]) is not None}
        self.lists = {l.id: l for l in lists if l.board_id in self.boards}

    def ws_of(self, board: Board) -> Workspace:
        return self.workspaces[board.workspace_id]

    def role_on(self, board: Board) -> Role:
        return board.role_of(self.person, self.ws_of(board))

    def lists_of(self, board_id: str) -> list:
        return sorted((l for l in self.lists.values() if l.board_id == board_id),
                      key=lambda l: l.sort_key)

    def done_ids(self) -> set:
        return {l.id for l in self.lists.values() if l.is_done}


class Boards:
    def __init__(self, repo: Repository, assistant: Optional[Assistant] = None,
                 vectors: Optional[VectorIndex] = None,
                 today: Callable[[], date] = date.today,
                 defer: Callable[[Callable[[], None]], None] = lambda fn: fn()):
        self.repo, self.assistant, self.vectors = repo, assistant, vectors
        self.today, self.defer = today, defer

    # ------------------------------------------------------------------ seeing things

    def _view(self, person: Person) -> _View:
        workspaces = self.repo.workspaces_for(person.email)
        boards = self.repo.boards_in([w.id for w in workspaces])
        lists = self.repo.lists_on([b.id for b in boards])
        return _View(person, workspaces, boards, lists)

    def _workspace(self, view: _View, ref: Optional[str]) -> Workspace:
        if not ref:
            if len(view.workspaces) == 1:
                return next(iter(view.workspaces.values()))
            if not view.workspaces:
                raise NotFound("you are not in a workspace yet: create one first")
            raise Invalid("you are in several workspaces; say which: "
                          + ", ".join(f"{w.name} ({w.id})" for w in view.workspaces.values()))
        ref = str(ref).strip()
        if ref in view.workspaces:
            return view.workspaces[ref]
        hits = [w for w in view.workspaces.values() if w.name.lower() == ref.lower()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise Invalid(f"several workspaces are called {ref!r}; use the id: "
                          + ", ".join(w.id for w in hits))
        raise NotFound(f"no workspace {ref!r} that you are in")

    def _board(self, view: _View, ref: str) -> Board:
        """A board by id or by title (case-insensitive), among the boards this person can see."""
        ref = str(ref or "").strip()
        if not ref:
            raise Invalid("which board? give its title or id")
        if ref in view.boards:
            return view.boards[ref]
        low = ref.lower().removesuffix(" board").strip()
        exact = [b for b in view.boards.values() if b.title.lower() in (ref.lower(), low)]
        hits = exact or [b for b in view.boards.values() if low in b.title.lower()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise Invalid(f"{ref!r} matches several boards: "
                          + "; ".join(f"{b.title} ({b.id}, {view.ws_of(b).name})" for b in hits))
        raise NotFound(f"no board {ref!r} that you can see")

    def _list(self, view: _View, board: Board, ref: Optional[str]) -> BoardList:
        lists = view.lists_of(board.id)
        if not lists:
            raise Invalid("this board has no lists")
        if ref is None or str(ref).strip() == "":
            return lists[0]
        r = str(ref).strip()
        for l in lists:
            if l.id == r or l.name.lower() == r.lower():
                return l
        raise Invalid(f"no list {ref!r} on {board.title}; lists are "
                      + ", ".join(l.name for l in lists))

    def _card(self, view: _View, number) -> tuple:
        n = parse_number(number)
        card = self.repo.get_card(n)
        if card is None or card.board_id not in view.boards:
            raise NotFound(f"no card {n} that you can see")
        return view.boards[card.board_id], card

    def _card_out(self, view: _View, card: Card, blockers: Optional[list] = None) -> dict:
        board = view.boards[card.board_id]
        lst = view.lists.get(card.list_id)
        return {
            "number": card.number, "title": card.title, "description": card.description,
            "board_id": board.id, "board": board.title,
            "list_id": card.list_id, "list": lst.name if lst else None,
            "done": bool(lst and lst.is_done),
            "assignee": card.assignee,
            "due_date": card.due_date.isoformat() if card.due_date else None,
            "overdue": card.is_overdue(self.today(), view.done_ids()),
            "labels": list(card.labels), "version": card.version,
            **({"blocked_by": blockers} if blockers is not None else {}),
        }

    def _log(self, ws_id: str, board_id: Optional[str], card: Optional[int], actor: Person,
             action: str, changes: Optional[list] = None) -> None:
        self.repo.log(Event(id=uuid.uuid4().hex, workspace_id=ws_id, board_id=board_id,
                            card_number=card, actor=actor.email, action=action,
                            changes=changes or [], at=datetime.now(timezone.utc)))

    def _open_blockers(self, view: _View, cards: dict) -> dict:
        """card number -> numbers of the OPEN cards directly blocking it."""
        done = view.done_ids()
        out = {}
        for c, b in self.repo.links_on(list(view.boards)):
            blocker = cards.get(b)
            if blocker is not None and blocker.list_id not in done:
                out.setdefault(c, []).append(b)
        return out

    # ------------------------------------------------------------------ workspaces & people

    def create_workspace(self, person: Person, name: str) -> dict:
        ws = Workspace(id=_id(), name=clean_title(name, "workspace"), owner_email=person.email)
        self.repo.save_workspace(ws)
        self._log(ws.id, None, None, person, f"created workspace {ws.name!r}")
        return {"id": ws.id, "name": ws.name, "role": Role.OWNER.value}

    def me(self, person: Person) -> dict:
        view = self._view(person)
        return {"email": person.email, "workspaces": [
            {"id": w.id, "name": w.name, "role": w.role_of(person).value,
             "boards": [{"id": b.id, "title": b.title} for b in view.boards.values()
                        if b.workspace_id == w.id]}
            for w in view.workspaces.values()]}

    def workspace_people(self, person: Person, workspace: Optional[str] = None) -> dict:
        view = self._view(person)
        ws = self._workspace(view, workspace)
        return {"workspace_id": ws.id, "name": ws.name, "your_role": ws.role_of(person).value,
                "people": [{"email": e, "role": r.value}
                           for e, r in sorted(ws.everyone().items())]}

    def invite_member(self, person: Person, email: str, role: str = "member",
                      workspace: Optional[str] = None) -> dict:
        view = self._view(person)
        ws = self._workspace(view, workspace)
        email = ws.invite(person, email, _role(role))
        self.repo.save_workspace(ws)
        self._log(ws.id, None, None, person, f"invited {email} as {role}")
        return {"workspace_id": ws.id, "email": email, "role": role,
                "next": "they sign in with this email; then put them on boards with add_to_board"}

    def change_role(self, person: Person, email: str, role: str,
                    workspace: Optional[str] = None) -> dict:
        view = self._view(person)
        ws = self._workspace(view, workspace)
        before = ws.change_role(person, email, _role(role))
        email = normalize_email(email)
        self.repo.save_workspace(ws)
        self._log(ws.id, None, None, person, f"changed {email}'s role",
                  [{"field": "role", "before": before.value, "after": role}])
        return {"workspace_id": ws.id, "email": email, "role": role}

    def remove_member(self, person: Person, email: str, workspace: Optional[str] = None) -> dict:
        view = self._view(person)
        ws = self._workspace(view, workspace)
        ws.remove(person, email)
        email = normalize_email(email)
        self.repo.remove_workspace_member(ws.id, email)
        for b in self.repo.boards_in([ws.id]):
            if email in b.members:
                self.repo.remove_board_member(b.id, email)
        self._log(ws.id, None, None, person, f"removed {email} from the workspace")
        return {"workspace_id": ws.id, "removed": email}

    # ------------------------------------------------------------------ boards & lists

    def create_board(self, person: Person, title: str, workspace: Optional[str] = None) -> dict:
        view = self._view(person)
        ws = self._workspace(view, workspace)
        if ws.require_member(person) is Role.VIEWER:
            raise Forbidden("viewers can't create boards")
        board = Board(id=_id(), workspace_id=ws.id, title=clean_title(title, "board"),
                      created_by=person.email, members={person.email})
        self.repo.save_board(board)
        for i, (name, done) in enumerate(DEFAULT_LISTS):
            self.repo.save_list(BoardList(id=_id(), board_id=board.id, name=name,
                                          sort_key=float(i + 1) * 1024, is_done=done))
        self._log(ws.id, board.id, None, person, f"created board {board.title!r}")
        return {"id": board.id, "title": board.title, "workspace_id": ws.id}

    def my_boards(self, person: Person) -> list:
        view = self._view(person)
        return [{"id": b.id, "title": b.title, "workspace": view.ws_of(b).name,
                 "workspace_id": b.workspace_id, "role": view.role_on(b).value}
                for b in sorted(view.boards.values(), key=lambda b: b.title.lower())]

    def show_board(self, person: Person, board: str) -> dict:
        view = self._view(person)
        b = self._board(view, board)
        cards = {c.number: c for c in self.repo.cards_on([b.id])}
        blockers = self._open_blockers(view, cards)
        lists = []
        for l in view.lists_of(b.id):
            in_list = sorted((c for c in cards.values() if c.list_id == l.id),
                             key=lambda c: (c.sort_key, c.number))
            lists.append({"id": l.id, "name": l.name, "is_done": l.is_done,
                          "cards": [self._card_out(view, c, blockers.get(c.number, []))
                                    for c in in_list]})
        ws = view.ws_of(b)
        return {"id": b.id, "title": b.title, "workspace_id": ws.id, "workspace": ws.name,
                "role": view.role_on(b).value, "lists": lists,
                "people": [{"email": e, "role": r.value} for e, r in sorted(b.people(ws).items())]}

    def add_to_board(self, person: Person, board: str, email: str) -> dict:
        view = self._view(person)
        b = self._board(view, board)
        email = b.add_person(person, view.ws_of(b), email)
        self.repo.save_board(b)
        self._log(b.workspace_id, b.id, None, person, f"put {email} on the board")
        return {"board_id": b.id, "email": email}

    def remove_from_board(self, person: Person, board: str, email: str) -> dict:
        view = self._view(person)
        b = self._board(view, board)
        email = b.remove_person(person, view.ws_of(b), email)
        self.repo.remove_board_member(b.id, email)
        self._log(b.workspace_id, b.id, None, person, f"took {email} off the board")
        return {"board_id": b.id, "removed": email}

    def add_list(self, person: Person, board: str, name: str, is_done: bool = False) -> dict:
        view = self._view(person)
        b = self._board(view, board)
        b.require_edit(person, view.ws_of(b))
        name = clean_title(name, "list")
        existing = view.lists_of(b.id)
        if any(l.name.lower() == name.lower() for l in existing):
            raise Invalid(f"{b.title} already has a list called {name!r}")
        last = existing[-1].sort_key if existing else None
        lst = BoardList(id=_id(), board_id=b.id, name=name,
                        sort_key=sort_key_between(last, None), is_done=bool(is_done))
        self.repo.save_list(lst)
        self._log(b.workspace_id, b.id, None, person, f"added list {name!r}")
        return {"id": lst.id, "name": lst.name, "board_id": b.id, "is_done": lst.is_done}

    def board_activity(self, person: Person, board: str, limit: int = 50) -> list:
        view = self._view(person)
        b = self._board(view, board)
        return [_event_out(e) for e in self.repo.board_activity(b.id, None, min(limit, 200))]

    # ------------------------------------------------------------------ cards

    def add_card(self, person: Person, board: str, title: str, list_name: Optional[str] = None,
                 description: str = "", assignee: Optional[str] = None, due_date=None,
                 labels: Optional[list] = None) -> dict:
        view = self._view(person)
        b = self._board(view, board)
        b.require_edit(person, view.ws_of(b))
        lst = self._list(view, b, list_name)
        if assignee:
            assignee = self._assignable(view, b, assignee)
        in_list = [c for c in self.repo.cards_on([b.id]) if c.list_id == lst.id]
        last = max((c.sort_key for c in in_list), default=None)
        card = Card(number=0, board_id=b.id, list_id=lst.id, title="", created_by=person.email,
                    sort_key=sort_key_between(last, None))
        changes = card.edit(title=title, description=description, assignee=assignee,
                            due_date=parse_due(due_date, self.today()), labels=labels or [])
        for _ in range(8):  # a card number is claimed by a plain INSERT
            card.number = self.repo.max_card_number() + 1
            try:
                self.repo.insert_card(card)
                break
            except Duplicate:
                continue
        else:
            raise Invalid("busy: could not claim a card number, try again")
        self._log(b.workspace_id, b.id, card.number, person,
                  f"added card #{card.number} to {lst.name}",
                  [{"field": "list", "before": None, "after": lst.name}] + changes)
        self._index(card)
        return self._card_out(view, card, [])

    def card_details(self, person: Person, number) -> dict:
        view = self._view(person)
        b, card = self._card(view, number)
        cards = {c.number: c for c in self.repo.cards_on([b.id])}
        blockers = self._open_blockers(view, cards)
        blocks = sorted(c for c, bl in blockers.items() if card.number in bl)
        out = self._card_out(view, card, blockers.get(card.number, []))
        out["blocks"] = blocks
        out["history"] = [_event_out(e) for e in self.repo.card_history(card.number)]
        return out

    def update_card(self, person: Person, number, expected_version: Optional[int] = None,
                    **fields) -> dict:
        """Change any of title, description, assignee, due_date, labels. Fields left out stay."""
        view = self._view(person)
        b, card = self._card(view, number)
        b.require_edit(person, view.ws_of(b))
        if expected_version is not None and int(expected_version) != card.version:
            raise Stale(f"card {card.number} changed since you opened it; reload it")
        # None means "leave it"; to clear, send "" or "none" (due_date) / "none" (assignee).
        fields = {k: v for k, v in fields.items() if v is not None}
        if "due_date" in fields:
            fields["due_date"] = parse_due(fields["due_date"], self.today())
        if "assignee" in fields:
            who = str(fields["assignee"]).strip()
            fields["assignee"] = (None if who.lower() in ("", "none", "nobody", "unassigned")
                                  else self._assignable(view, b, who))
        version = card.version
        changes = card.edit(**fields)
        if not changes:
            return self._card_out(view, card)
        self._save(card, version)
        self._log(b.workspace_id, b.id, card.number, person, f"edited card #{card.number}", changes)
        if any(c["field"] in ("title", "description") for c in changes):
            self._index(card)
        return self._card_out(view, card)

    def assign_card(self, person: Person, number, assignee: Optional[str]) -> dict:
        """Assign a card to someone on its board ('me' for yourself), or None to unassign."""
        return self.update_card(person, number, assignee=assignee or "none")

    def move_card(self, person: Person, number, list_name: str,
                  before: Optional[int] = None) -> dict:
        """Move a card to a list (by name or id). With `before`, drop it just above that card;
        otherwise it goes to the bottom of the list."""
        view = self._view(person)
        b, card = self._card(view, number)
        b.require_edit(person, view.ws_of(b))
        target = self._list(view, b, list_name)
        in_list = sorted((c for c in self.repo.cards_on([b.id])
                          if c.list_id == target.id and c.number != card.number),
                         key=lambda c: (c.sort_key, c.number))
        if before is not None and str(before) != "":
            n = parse_number(before)
            idx = next((i for i, c in enumerate(in_list) if c.number == n), None)
            if idx is None:
                raise Invalid(f"card {n} is not in {target.name}")
            prev = in_list[idx - 1].sort_key if idx > 0 else None
            key = sort_key_between(prev, in_list[idx].sort_key)
        else:
            key = sort_key_between(in_list[-1].sort_key if in_list else None, None)
        before_list = view.lists.get(card.list_id)
        if card.list_id == target.id and abs(card.sort_key - key) < 1e-9:
            return self._card_out(view, card)
        version = card.version
        card.list_id, card.sort_key = target.id, key
        self._save(card, version)
        if before_list is None or before_list.id != target.id:
            self._log(b.workspace_id, b.id, card.number, person,
                      f"moved card #{card.number} to {target.name}",
                      [{"field": "list", "before": before_list.name if before_list else None,
                        "after": target.name}])
        else:
            self._log(b.workspace_id, b.id, card.number, person,
                      f"reordered card #{card.number} in {target.name}")
        return self._card_out(view, card)

    def _save(self, card: Card, version: int) -> None:
        card.version = version + 1
        if not self.repo.update_card(card, version):
            raise Stale(f"card {card.number} was changed by someone else just now; reload it")

    def _assignable(self, view: _View, board: Board, who: str) -> str:
        email = view.person.email if str(who).strip().lower() == "me" else normalize_email(who)
        role = board.people(view.ws_of(board)).get(email)
        if role is None:
            raise Invalid(f"{email} is not on {board.title}; the owner can add them first")
        if role is Role.VIEWER:
            raise Invalid(f"{email} is a viewer and can't work on cards")
        return email

    # ------------------------------------------------------------------ questions

    def my_cards(self, person: Person, days: int = 7) -> dict:
        """What's on my plate: open cards assigned to me, grouped by when they're due."""
        view = self._view(person)
        today, done = self.today(), view.done_ids()
        horizon = today + timedelta(days=max(0, int(days)))
        mine = [c for c in self.repo.cards_on(list(view.boards))
                if c.assignee == person.email and c.list_id not in done]
        out = {"today": today.isoformat(), "until": horizon.isoformat(),
               "overdue": [], "due_soon": [], "no_due_date": [], "later": []}
        for c in sorted(mine, key=lambda c: (c.due_date or date.max, c.number)):
            bucket = ("no_due_date" if c.due_date is None else "overdue" if c.due_date < today
                      else "due_soon" if c.due_date <= horizon else "later")
            out[bucket].append(self._card_out(view, c))
        return out

    def list_overdue(self, person: Person, board: Optional[str] = None,
                     mine_only: bool = False) -> list:
        view = self._view(person)
        ids = [self._board(view, board).id] if board else list(view.boards)
        today, done = self.today(), view.done_ids()
        hits = [c for c in self.repo.cards_on(ids) if c.is_overdue(today, done)
                and (not mine_only or c.assignee == person.email)]
        return [dict(self._card_out(view, c), days_late=(today - c.due_date).days)
                for c in sorted(hits, key=lambda c: (c.due_date, c.number))]

    def search_cards(self, person: Person, text: str, board: Optional[str] = None,
                     include_done: bool = True) -> list:
        view = self._view(person)
        q = (text or "").strip().lower()
        if not q:
            raise Invalid("search for what?")
        ids = [self._board(view, board).id] if board else list(view.boards)
        done = view.done_ids()
        hits = [c for c in self.repo.cards_on(ids)
                if (include_done or c.list_id not in done)
                and (q in c.title.lower() or q in c.description.lower()
                     or any(q == l for l in c.labels) or q.lstrip("#") == str(c.number))]
        return [self._card_out(view, c) for c in sorted(hits, key=lambda c: c.number)][:50]

    # ------------------------------------------------------------------ dependencies

    def block_card(self, person: Person, number, blocked_by) -> dict:
        """Record that card `number` can't move until card `blocked_by` is done."""
        view = self._view(person)
        b, card = self._card(view, number)
        b.require_edit(person, view.ws_of(b))
        b2, blocker = self._card(view, blocked_by)
        if b2.id != b.id:
            raise Invalid("a card can only be blocked by a card on the same board")
        graph: dict = {}
        for c, bl in self.repo.links_on([b.id]):
            graph.setdefault(c, set()).add(bl)
        if blocker.number in graph.get(card.number, set()):
            return {"card": card.number, "blocked_by": blocker.number, "already": True}
        if would_cycle(card.number, blocker.number, graph):
            raise Invalid(f"#{blocker.number} already waits on #{card.number}; "
                          "that would be a loop")
        self.repo.add_link(b.id, card.number, blocker.number, person.email)
        self._log(b.workspace_id, b.id, card.number, person,
                  f"marked #{card.number} blocked by #{blocker.number}",
                  [{"field": "blocked_by", "before": None, "after": blocker.number}])
        return {"card": card.number, "blocked_by": blocker.number}

    def unblock_card(self, person: Person, number, blocked_by) -> dict:
        view = self._view(person)
        b, card = self._card(view, number)
        b.require_edit(person, view.ws_of(b))
        n = parse_number(blocked_by)
        if (card.number, n) not in set(self.repo.links_on([b.id])):
            raise Invalid(f"#{card.number} is not blocked by #{n}")
        self.repo.remove_link(card.number, n)
        self._log(b.workspace_id, b.id, card.number, person,
                  f"#{card.number} no longer blocked by #{n}",
                  [{"field": "blocked_by", "before": n, "after": None}])
        return {"card": card.number, "unblocked_from": n}

    def whats_blocking(self, person: Person, card: str) -> dict:
        """Every open card standing in the way of this one, directly or further down the chain.
        `card` is a number, or words from the title ("the launch")."""
        view = self._view(person)
        try:
            b, target = self._card(view, card)
        except Invalid:
            hits = [c for c in self.search_cards(person, str(card).removeprefix("the "),
                                                 include_done=False)]
            if not hits:
                raise NotFound(f"no open card matching {card!r}")
            if len(hits) > 1:
                return {"ambiguous": True, "matches": [
                    {"number": h["number"], "title": h["title"], "board": h["board"]}
                    for h in hits[:10]]}
            b, target = self._card(view, hits[0]["number"])
        cards = {c.number: c for c in self.repo.cards_on([b.id])}
        graph: dict = {}
        for c, bl in self.repo.links_on([b.id]):
            graph.setdefault(c, set()).add(bl)
        done = view.done_ids()
        open_cards = {n for n, c in cards.items() if c.list_id not in done}
        chain = blocking_chain(target.number, graph, open_cards)
        return {"card": self._card_out(view, target),
                "blocked": bool(chain),
                "blocking": [dict(self._card_out(view, cards[n]),
                                  direct=n in graph.get(target.number, set())) for n in chain]}

    # ------------------------------------------------------------------ AI (Dodil Models)

    def _ai(self) -> Assistant:
        if self.assistant is None:
            raise Invalid("no AI assistant configured")
        return self.assistant

    def suggest_checklist(self, person: Person, number, apply: bool = False) -> dict:
        """AI checklist for a card. With apply=True it is appended to the card's description
        (members and owners only); otherwise it is only a suggestion and changes nothing."""
        view = self._view(person)
        b, card = self._card(view, number)
        if apply:
            b.require_edit(person, view.ws_of(b))
        steps = [s for s in self._ai().checklist(card.title, card.description) if s][:10]
        if not steps:
            raise Invalid("the assistant had no checklist for this card")
        out = {"card": card.number, "checklist": steps, "applied": False}
        if apply:
            block = "Checklist:\n" + "\n".join(f"- [ ] {s}" for s in steps)
            desc = f"{card.description}\n\n{block}".strip()
            updated = self.update_card(person, card.number, description=desc)
            out.update(applied=True, description=updated["description"])
        return out

    def find_similar(self, person: Person, number=None, text: Optional[str] = None,
                     k: int = 5) -> list:
        """Cards like this one (or like `text`), by meaning, across the boards you can see."""
        if self.vectors is None:
            raise Invalid("similarity search is not configured")
        view = self._view(person)
        exclude = None
        if number is not None and str(number) != "":
            _, card = self._card(view, number)
            exclude, query = card.number, _card_text(card)
        elif text and text.strip():
            query = text.strip()
        else:
            raise Invalid("give a card number or some text")
        self._backfill(view)
        vector = self._ai().embed(query)
        k = max(1, min(int(k), 20))
        hits = self.vectors.nearest(vector, list(view.boards), k + 1)
        out = []
        for n, distance in hits:
            if n == exclude:
                continue
            card = self.repo.get_card(n)
            if card is None or card.board_id not in view.boards:
                continue  # never trust the index for visibility: re-check every hit
            out.append(dict(self._card_out(view, card), similarity=round(1 - float(distance), 3)))
        return out[:k]

    def board_summary(self, person: Person, board: str) -> dict:
        """A short daily summary of a board: what moved, what is late, what is stuck."""
        view = self._view(person)
        b = self._board(view, board)
        shown = self.show_board(person, b.id)
        today = self.today()
        since = datetime.now(timezone.utc) - timedelta(days=1)
        events = self.repo.board_activity(b.id, since, 100)
        cards = [c for l in shown["lists"] for c in l["cards"]]
        facts = {
            "board": b.title, "date": today.isoformat(),
            "per_list": {l["name"]: len(l["cards"]) for l in shown["lists"]},
            "overdue": [f"#{c['number']} {c['title']} (due {c['due_date']}, "
                        f"{c['assignee'] or 'unassigned'})" for c in cards if c["overdue"]],
            "due_next_2_days": [f"#{c['number']} {c['title']} ({c['due_date']})" for c in cards
                                if c["due_date"] and not c["done"] and
                                today.isoformat() <= c["due_date"] <=
                                (today + timedelta(days=2)).isoformat()],
            "blocked": [f"#{c['number']} {c['title']} (by "
                        + ", ".join(f"#{n}" for n in c["blocked_by"]) + ")"
                        for c in cards if c.get("blocked_by")],
            "last_24h": [f"{e.actor}: {e.action}" for e in events][:40],
        }
        text = None
        if self.assistant is not None:
            try:
                text = self.assistant.summarize(_facts_text(facts))
            except Exception:  # the facts are still useful when Models is unavailable
                log.exception("summary failed")
        return {"summary": text, "facts": facts}

    def _index(self, card: Card) -> None:
        if self.vectors is None or self.assistant is None:
            return
        snapshot, ai, vectors = card.copy(), self.assistant, self.vectors

        def job():
            try:
                vectors.upsert(snapshot.number, snapshot.board_id, ai.embed(_card_text(snapshot)))
            except Exception:
                log.exception("could not index card %s", snapshot.number)
        self.defer(job)

    def _backfill(self, view: _View, limit: int = 25) -> None:
        """Embed a few cards that have no vector yet (cards made before AI was configured)."""
        missing = [c for c in self.repo.cards_on(list(view.boards))
                   if not self.vectors.has(c.number)][:limit]
        for c in missing:
            try:
                self.vectors.upsert(c.number, c.board_id, self._ai().embed(_card_text(c)))
            except Exception:
                log.exception("backfill failed for %s", c.number)
                break


def _role(value: str) -> Role:
    try:
        return Role(str(value).strip().lower())
    except ValueError:
        raise Invalid("role must be 'member' or 'viewer'")


def _card_text(c: Card) -> str:
    return " ".join(x for x in (c.title, c.description, " ".join(c.labels)) if x)[:4000]


def _event_out(e: Event) -> dict:
    return {"at": e.at.isoformat() if e.at else None, "actor": e.actor, "action": e.action,
            "card": e.card_number, "changes": e.changes}


def _facts_text(f: dict) -> str:
    lines = [f"Board: {f['board']} - {f['date']}",
             "Cards per list: " + ", ".join(f"{k}: {v}" for k, v in f["per_list"].items())]
    for key, title in (("overdue", "Overdue"), ("due_next_2_days", "Due in the next 2 days"),
                       ("blocked", "Blocked"), ("last_24h", "Changes in the last 24 hours")):
        lines.append(f"{title}: " + ("; ".join(f[key]) if f[key] else "none"))
    return "\n".join(lines)
