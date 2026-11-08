"""Tasklane's language and its rules. No database, no HTTP: plain Python you can test in a second.

A rule written here holds for the web page and for Claude alike, because both doors only ever
reach the domain through the use cases in application/.

    Workspace  an owner and the people they invited, each with a role
      Board    belongs to one workspace; only the people put on it can see it
        List   an ordered column on a board ("To do", "Doing", "Done", or your own)
          Card a numbered task: title, description, assignee, due date, labels
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum
from typing import Iterable, Optional

DEFAULT_LISTS = (("To do", False), ("Doing", False), ("Done", True))  # (name, is_done)
MAX_TITLE = 200
MAX_DESCRIPTION = 10_000
MAX_LABELS = 10
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LABEL = re.compile(r"^[a-z0-9][a-z0-9 _\-]{0,29}$")


class Role(str, Enum):
    OWNER = "owner"
    MEMBER = "member"
    VIEWER = "viewer"


class Forbidden(Exception):
    """The person may not do this. Web: 403. MCP: a tool error."""


class Invalid(Exception):
    """The request breaks a rule of the board itself. Web: 422. MCP: a tool error."""


class Stale(Exception):
    """The card changed since the caller read it (optimistic concurrency). Web: 409."""


@dataclass(frozen=True)
class Person:
    """Who is acting: taken from the gateway's sign-in, never from a request body."""
    email: str


def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not EMAIL.match(e):
        raise Invalid(f"{email!r} is not an email address")
    return e


def clean_title(title: str, what: str = "card") -> str:
    t = " ".join((title or "").split())
    if not t:
        raise Invalid(f"a {what} needs a title")
    if len(t) > MAX_TITLE:
        raise Invalid(f"a {what} title is at most {MAX_TITLE} characters")
    return t


def clean_labels(labels: Optional[Iterable[str]]) -> tuple:
    out = []
    for raw in labels or ():
        label = " ".join(str(raw).strip().lower().split())
        if not label:
            continue
        if not LABEL.match(label):
            raise Invalid(f"label {raw!r}: use up to 30 letters, digits, spaces, '-' or '_'")
        if label not in out:
            out.append(label)
    if len(out) > MAX_LABELS:
        raise Invalid(f"a card has at most {MAX_LABELS} labels")
    return tuple(out)


# --------------------------------------------------------------------------- workspace


@dataclass
class Workspace:
    id: str
    name: str
    owner_email: str
    members: dict = field(default_factory=dict)  # email -> Role (never the owner)

    def role_of(self, person: Person) -> Optional[Role]:
        if person.email == self.owner_email:
            return Role.OWNER
        return self.members.get(person.email)

    def require_member(self, person: Person) -> Role:
        role = self.role_of(person)
        if role is None:
            raise Forbidden("you are not in this workspace")
        return role

    def require_owner(self, person: Person) -> None:
        if self.role_of(person) is not Role.OWNER:
            raise Forbidden("only the workspace owner manages members")

    def invite(self, by: Person, email: str, role: Role) -> str:
        self.require_owner(by)
        email = normalize_email(email)
        if role is Role.OWNER:
            raise Invalid("a workspace has one owner")
        if email == self.owner_email:
            raise Invalid("that person already owns this workspace")
        self.members[email] = role
        return email

    def change_role(self, by: Person, email: str, role: Role) -> Role:
        self.require_owner(by)
        email = normalize_email(email)
        if role is Role.OWNER:
            raise Invalid("a workspace has one owner")
        if email not in self.members:
            raise Invalid(f"{email} is not in this workspace")
        before = self.members[email]
        self.members[email] = role
        return before

    def remove(self, by: Person, email: str) -> None:
        self.require_owner(by)
        email = normalize_email(email)
        if email == self.owner_email:
            raise Invalid("the owner can't be removed")
        if email not in self.members:
            raise Invalid(f"{email} is not in this workspace")
        del self.members[email]

    def everyone(self) -> dict:
        return {self.owner_email: Role.OWNER, **self.members}


# --------------------------------------------------------------------------- board


@dataclass
class Board:
    id: str
    workspace_id: str
    title: str
    created_by: str
    members: set = field(default_factory=set)  # emails put on this board

    def role_of(self, person: Person, ws: Workspace) -> Optional[Role]:
        """Your role on a board is your workspace role, if you are on the board at all.
        The workspace owner is on every board of their workspace."""
        role = ws.role_of(person)
        if role is Role.OWNER:
            return role
        if role is None or person.email not in self.members:
            return None
        return role

    def require_view(self, person: Person, ws: Workspace) -> Role:
        role = self.role_of(person, ws)
        if role is None:
            raise Forbidden("you are not on this board")
        return role

    def require_edit(self, person: Person, ws: Workspace) -> None:
        if self.require_view(person, ws) is Role.VIEWER:
            raise Forbidden("viewers can't change the board")

    def add_person(self, by: Person, ws: Workspace, email: str) -> str:
        ws.require_owner(by)
        email = normalize_email(email)
        if ws.role_of(Person(email)) is None:
            raise Invalid(f"invite {email} to the workspace first")
        self.members.add(email)
        return email

    def remove_person(self, by: Person, ws: Workspace, email: str) -> str:
        ws.require_owner(by)
        email = normalize_email(email)
        if email not in self.members:
            raise Invalid(f"{email} is not on this board")
        self.members.discard(email)
        return email

    def people(self, ws: Workspace) -> dict:
        """Everyone who can see this board, with their role."""
        out = {ws.owner_email: Role.OWNER}
        for email in self.members:
            role = ws.role_of(Person(email))
            if role is not None:
                out[email] = role
        return out


@dataclass
class BoardList:
    id: str
    board_id: str
    name: str
    sort_key: float
    is_done: bool = False


# --------------------------------------------------------------------------- card

EDITABLE = ("title", "description", "assignee", "due_date", "labels")


@dataclass
class Card:
    number: int
    board_id: str
    list_id: str
    title: str
    description: str = ""
    assignee: Optional[str] = None
    due_date: Optional[date] = None
    labels: tuple = ()
    sort_key: float = 0.0
    version: int = 1
    created_by: str = ""

    def edit(self, **changes) -> list:
        """Apply field changes; return what actually changed as [{field, before, after}]."""
        unknown = set(changes) - set(EDITABLE)
        if unknown:
            raise Invalid(f"unknown card field(s): {', '.join(sorted(unknown))}")
        cleaned = {}
        if "title" in changes:
            cleaned["title"] = clean_title(changes["title"])
        if "description" in changes:
            d = (changes["description"] or "").strip()
            if len(d) > MAX_DESCRIPTION:
                raise Invalid(f"a description is at most {MAX_DESCRIPTION} characters")
            cleaned["description"] = d
        if "assignee" in changes:
            a = changes["assignee"]
            cleaned["assignee"] = normalize_email(a) if a else None
        if "due_date" in changes:
            cleaned["due_date"] = changes["due_date"]
        if "labels" in changes:
            cleaned["labels"] = clean_labels(changes["labels"])
        diff = []
        for name, after in cleaned.items():
            before = getattr(self, name)
            if before != after:
                diff.append({"field": name, "before": _plain(before), "after": _plain(after)})
                setattr(self, name, after)
        return diff

    def is_overdue(self, today: date, done_list_ids: set) -> bool:
        return bool(self.due_date and self.due_date < today and self.list_id not in done_list_ids)

    def copy(self) -> "Card":
        return replace(self)


def _plain(v):
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, tuple):
        return list(v)
    return v


def blocking_chain(start: int, blockers_of: dict, open_cards: set) -> list:
    """Every OPEN card that blocks `start`, directly or through other cards, nearest first.
    blockers_of maps a card number to the numbers it is blocked by. Cycles are tolerated."""
    seen, order, frontier = {start}, [], [start]
    while frontier:
        nxt = []
        for n in frontier:
            for b in sorted(blockers_of.get(n, ())):
                if b in seen:
                    continue
                seen.add(b)
                nxt.append(b)
                if b in open_cards:
                    order.append(b)
        frontier = nxt
    return order


def would_cycle(card: int, blocker: int, blockers_of: dict) -> bool:
    """True if making `card` blocked by `blocker` closes a loop (blocker already waits on card)."""
    if card == blocker:
        return True
    seen, stack = set(), [blocker]
    while stack:
        n = stack.pop()
        if n == card:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(blockers_of.get(n, ()))
    return False


def sort_key_between(before: Optional[float], after: Optional[float]) -> float:
    """A position between two neighbours, so a drag writes one row, not the whole list."""
    if before is None and after is None:
        return 1024.0
    if before is None:
        return after - 1024.0
    if after is None:
        return before + 1024.0
    return (before + after) / 2.0
