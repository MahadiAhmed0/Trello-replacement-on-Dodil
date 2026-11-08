"""Tasklane's records in DataK3 tables (schema.sql), over the Postgres wire.

Writes follow the DataK3 rules: a card number is claimed with a plain INSERT (one writer wins,
the others get 23505), an edit is an UPDATE guarded by the version it read, and rows that are
created once are inserted once, then updated, so an upsert never overwrites their insert-only
columns.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable, Optional

import psycopg

from boards.application.ports import Duplicate, Event
from boards.domain.model import Board, BoardList, Card, Role, Workspace
from boards.infrastructure.dodil import Database

CARD_COLS = ("card_no, board_id, list_id, title, description, assignee, due_date, labels, "
             "sort_key, version, created_by")


def _card(r) -> Card:
    return Card(number=int(r[0]), board_id=r[1], list_id=r[2], title=r[3],
                description=r[4] or "", assignee=r[5], due_date=r[6],
                labels=tuple(json.loads(r[7] or "[]")), sort_key=float(r[8]),
                version=int(r[9]), created_by=r[10] or "")


def _event(r) -> Event:
    return Event(id=r[0], workspace_id=r[1], board_id=r[2],
                 card_number=int(r[3]) if r[3] is not None else None, actor=r[4], action=r[5],
                 changes=json.loads(r[6] or "[]"), at=r[7])


def _insert_or_update(c, insert: str, insert_args: tuple, update: str, update_args: tuple):
    try:
        c.execute(insert, insert_args)
    except psycopg.errors.UniqueViolation:
        c.execute(update, update_args)


class SqlRepository:
    def __init__(self, db: Optional[Database] = None):
        self.db = db or Database()

    def _all(self, sql: str, args: tuple = ()) -> list:
        return self.db.run(lambda c: c.execute(sql, args).fetchall())

    def _one(self, sql: str, args: tuple = ()):
        return self.db.run(lambda c: c.execute(sql, args).fetchone())

    def _do(self, sql: str, args: tuple = ()) -> None:
        self.db.run(lambda c: c.execute(sql, args))

    # ------------------------------------------------------------------ workspaces

    def _workspaces(self, rows) -> list:
        if not rows:
            return []
        ids = [r[0] for r in rows]
        members: dict = {}
        for ws_id, email, role in self._all(
                "SELECT workspace_id, email, role FROM workspace_members "
                "WHERE workspace_id = ANY(%s)", (ids,)):
            members.setdefault(ws_id, {})[email] = Role(role)
        return [Workspace(id=r[0], name=r[1], owner_email=r[2], members=members.get(r[0], {}))
                for r in rows]

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        rows = self._all("SELECT id, name, owner_email FROM workspaces WHERE id = %s",
                         (workspace_id,))
        found = self._workspaces(rows)
        return found[0] if found else None

    def workspaces_for(self, email: str) -> list:
        ids = [r[0] for r in self._all(
            "SELECT id FROM workspaces WHERE owner_email = %s "
            "UNION SELECT workspace_id FROM workspace_members WHERE email = %s", (email, email))]
        if not ids:
            return []
        return self._workspaces(self._all(
            "SELECT id, name, owner_email FROM workspaces WHERE id = ANY(%s)", (ids,)))

    def save_workspace(self, ws: Workspace) -> None:
        def write(c):
            _insert_or_update(
                c, "INSERT INTO workspaces (id, name, owner_email, created_at) "
                   "VALUES (%s, %s, %s, now())", (ws.id, ws.name, ws.owner_email),
                "UPDATE workspaces SET name = %s, owner_email = %s WHERE id = %s",
                (ws.name, ws.owner_email, ws.id))
            for email, role in ws.members.items():
                key = f"{ws.id}:{email}"
                _insert_or_update(
                    c, "INSERT INTO workspace_members (id, workspace_id, email, role, added_at) "
                       "VALUES (%s, %s, %s, %s, now())", (key, ws.id, email, role.value),
                    "UPDATE workspace_members SET role = %s WHERE id = %s", (role.value, key))
        self.db.run(write)

    def remove_workspace_member(self, workspace_id: str, email: str) -> None:
        self._do("DELETE FROM workspace_members WHERE id = %s", (f"{workspace_id}:{email}",))

    # ------------------------------------------------------------------ boards & lists

    def _boards(self, rows) -> list:
        if not rows:
            return []
        ids = [r[0] for r in rows]
        members: dict = {}
        for board_id, email in self._all(
                "SELECT board_id, email FROM board_members WHERE board_id = ANY(%s)", (ids,)):
            members.setdefault(board_id, set()).add(email)
        return [Board(id=r[0], workspace_id=r[1], title=r[2], created_by=r[3],
                      members=members.get(r[0], set())) for r in rows]

    def get_board(self, board_id: str) -> Optional[Board]:
        found = self._boards(self._all(
            "SELECT id, workspace_id, title, created_by FROM boards WHERE id = %s", (board_id,)))
        return found[0] if found else None

    def boards_in(self, workspace_ids: Iterable[str]) -> list:
        ids = list(workspace_ids)
        if not ids:
            return []
        return self._boards(self._all(
            "SELECT id, workspace_id, title, created_by FROM boards "
            "WHERE workspace_id = ANY(%s)", (ids,)))

    def save_board(self, board: Board) -> None:
        def write(c):
            _insert_or_update(
                c, "INSERT INTO boards (id, workspace_id, title, created_by, created_at) "
                   "VALUES (%s, %s, %s, %s, now())",
                (board.id, board.workspace_id, board.title, board.created_by),
                "UPDATE boards SET title = %s WHERE id = %s", (board.title, board.id))
            have = {r[0] for r in c.execute(
                "SELECT email FROM board_members WHERE board_id = %s", (board.id,)).fetchall()}
            for email in sorted(board.members - have):
                try:
                    c.execute("INSERT INTO board_members (id, board_id, email, added_at) "
                              "VALUES (%s, %s, %s, now())", (f"{board.id}:{email}", board.id, email))
                except psycopg.errors.UniqueViolation:
                    pass
        self.db.run(write)

    def remove_board_member(self, board_id: str, email: str) -> None:
        self._do("DELETE FROM board_members WHERE id = %s", (f"{board_id}:{email}",))

    def lists_on(self, board_ids: Iterable[str]) -> list:
        ids = list(board_ids)
        if not ids:
            return []
        return [BoardList(id=r[0], board_id=r[1], name=r[2], sort_key=float(r[3]),
                          is_done=bool(r[4]))
                for r in self._all("SELECT id, board_id, name, sort_key, is_done FROM board_lists "
                                   "WHERE board_id = ANY(%s)", (ids,))]

    def save_list(self, lst: BoardList) -> None:
        def write(c):
            _insert_or_update(
                c, "INSERT INTO board_lists (id, board_id, name, sort_key, is_done) "
                   "VALUES (%s, %s, %s, %s, %s)",
                (lst.id, lst.board_id, lst.name, lst.sort_key, lst.is_done),
                "UPDATE board_lists SET name = %s, sort_key = %s, is_done = %s WHERE id = %s",
                (lst.name, lst.sort_key, lst.is_done, lst.id))
        self.db.run(write)

    # ------------------------------------------------------------------ cards

    def get_card(self, number: int) -> Optional[Card]:
        r = self._one(f"SELECT {CARD_COLS} FROM cards WHERE card_no = %s", (number,))
        return _card(r) if r else None

    def cards_on(self, board_ids: Iterable[str]) -> list:
        ids = list(board_ids)
        if not ids:
            return []
        return [_card(r) for r in self._all(
            f"SELECT {CARD_COLS} FROM cards WHERE board_id = ANY(%s)", (ids,))]

    def max_card_number(self) -> int:
        r = self._one("SELECT max(card_no) AS n FROM cards")
        return int(r[0]) if r and r[0] is not None else 0

    def insert_card(self, card: Card) -> None:
        def write(c):
            try:
                c.execute(
                    "INSERT INTO cards (card_no, board_id, list_id, title, description, assignee, "
                    "due_date, labels, sort_key, version, created_by, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())",
                    (card.number, card.board_id, card.list_id, card.title, card.description,
                     card.assignee, card.due_date, json.dumps(list(card.labels)), card.sort_key,
                     card.version, card.created_by))
            except psycopg.errors.UniqueViolation:
                raise Duplicate(card.number)
        self.db.run(write)

    def update_card(self, card: Card, expected_version: int) -> bool:
        r = self._one(
            "UPDATE cards SET list_id = %s, title = %s, description = %s, assignee = %s, "
            "due_date = %s, labels = %s, sort_key = %s, version = %s, updated_at = now() "
            "WHERE card_no = %s AND version = %s RETURNING card_no",
            (card.list_id, card.title, card.description, card.assignee, card.due_date,
             json.dumps(list(card.labels)), card.sort_key, card.version, card.number,
             expected_version))
        return r is not None

    # ------------------------------------------------------------------ links

    def links_on(self, board_ids: Iterable[str]) -> list:
        ids = list(board_ids)
        if not ids:
            return []
        return [(int(a), int(b)) for a, b in self._all(
            "SELECT card_no, blocked_by FROM card_links WHERE board_id = ANY(%s)", (ids,))]

    def add_link(self, board_id: str, card: int, blocked_by: int, by: str) -> None:
        def write(c):
            try:
                c.execute("INSERT INTO card_links (id, board_id, card_no, blocked_by, created_by, "
                          "created_at) VALUES (%s, %s, %s, %s, %s, now())",
                          (f"{card}<{blocked_by}", board_id, card, blocked_by, by))
            except psycopg.errors.UniqueViolation:
                pass
        self.db.run(write)

    def remove_link(self, card: int, blocked_by: int) -> None:
        self._do("DELETE FROM card_links WHERE id = %s", (f"{card}<{blocked_by}",))

    # ------------------------------------------------------------------ history

    def log(self, e: Event) -> None:
        self._do("INSERT INTO activity (id, workspace_id, board_id, card_no, actor, action, "
                 "changes, event_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                 (e.id, e.workspace_id, e.board_id, e.card_number, e.actor, e.action,
                  json.dumps(e.changes, default=str), e.at))

    def card_history(self, number: int, limit: int = 100) -> list:
        return [_event(r) for r in self._all(
            "SELECT id, workspace_id, board_id, card_no, actor, action, changes, event_at "
            "FROM activity WHERE card_no = %s ORDER BY event_at DESC LIMIT %s", (number, limit))]

    def board_activity(self, board_id: str, since: Optional[datetime], limit: int) -> list:
        if since is None:
            rows = self._all(
                "SELECT id, workspace_id, board_id, card_no, actor, action, changes, event_at "
                "FROM activity WHERE board_id = %s ORDER BY event_at DESC LIMIT %s",
                (board_id, limit))
        else:
            rows = self._all(
                "SELECT id, workspace_id, board_id, card_no, actor, action, changes, event_at "
                "FROM activity WHERE board_id = %s AND event_at >= %s "
                "ORDER BY event_at DESC LIMIT %s", (board_id, since, limit))
        return [_event(r) for r in rows]


class SqlVectors:
    """Card embeddings in a VECTOR column of the same bucket: KNN with pgvector's <=> (cosine)."""

    def __init__(self, db: Optional[Database] = None):
        self.db = db or Database()

    @staticmethod
    def _lit(vector: list) -> str:
        return "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"

    def upsert(self, number: int, board_id: str, vector: list) -> None:
        self.db.run(lambda c: c.execute(
            "INSERT INTO card_vectors (card_no, board_id, embedding) VALUES (%s, %s, %s::VECTOR) "
            "ON CONFLICT (card_no) DO UPDATE SET board_id = EXCLUDED.board_id, "
            "embedding = EXCLUDED.embedding", (number, board_id, self._lit(vector))))

    def has(self, number: int) -> bool:
        return self.db.run(lambda c: c.execute(
            "SELECT card_no FROM card_vectors WHERE card_no = %s", (number,)).fetchone()) is not None

    def nearest(self, vector: list, board_ids: list, k: int) -> list:
        if not board_ids:
            return []
        lit = self._lit(vector)
        rows = self.db.run(lambda c: c.execute(
            "SELECT card_no, embedding <=> %s::VECTOR AS distance FROM card_vectors "
            "WHERE board_id = ANY(%s) ORDER BY embedding <=> %s::VECTOR LIMIT %s",
            (lit, board_ids, lit, k)).fetchall())
        return [(int(n), float(d)) for n, d in rows]
