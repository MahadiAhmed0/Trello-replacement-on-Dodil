"""In-memory stand-ins for DataK3 and Dodil Models, so the rules are tested in a second with no
account. They follow the same contracts as infrastructure/: a card number is claimed by a plain
insert (Duplicate on a clash) and an update is guarded by the version it read."""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone

from boards.application.ports import Duplicate


class MemoryRepo:
    def __init__(self):
        self.workspaces, self.boards, self.lists, self.cards = {}, {}, {}, {}
        self.links, self.events = set(), []
        self.steal_next_number = False  # simulate another writer claiming a number first

    # batched reads
    def visible_to(self, email):
        workspaces = self.workspaces_for(email)
        boards = self.boards_in([w.id for w in workspaces])
        return workspaces, boards, self.lists_on([b.id for b in boards])

    def contents(self, board_ids):
        ids = list(board_ids)
        return self.cards_on(ids), self.links_on(ids)

    # workspaces
    def get_workspace(self, ws_id):
        return copy.deepcopy(self.workspaces.get(ws_id))

    def workspaces_for(self, email):
        return [copy.deepcopy(w) for w in self.workspaces.values()
                if w.owner_email == email or email in w.members]

    def save_workspace(self, ws):
        self.workspaces[ws.id] = copy.deepcopy(ws)

    def remove_workspace_member(self, ws_id, email):
        self.workspaces[ws_id].members.pop(email, None)

    # boards
    def get_board(self, board_id):
        return copy.deepcopy(self.boards.get(board_id))

    def boards_in(self, ws_ids):
        ws_ids = set(ws_ids)
        return [copy.deepcopy(b) for b in self.boards.values() if b.workspace_id in ws_ids]

    def save_board(self, board):
        self.boards[board.id] = copy.deepcopy(board)

    def remove_board_member(self, board_id, email):
        self.boards[board_id].members.discard(email)

    def lists_on(self, board_ids):
        board_ids = set(board_ids)
        return [copy.deepcopy(l) for l in self.lists.values() if l.board_id in board_ids]

    def save_list(self, lst):
        self.lists[lst.id] = copy.deepcopy(lst)

    # cards
    def get_card(self, number):
        return copy.deepcopy(self.cards.get(number))

    def cards_on(self, board_ids):
        board_ids = set(board_ids)
        return [copy.deepcopy(c) for c in self.cards.values() if c.board_id in board_ids]

    def max_card_number(self):
        return max(self.cards, default=0)

    def insert_card(self, card):
        if self.steal_next_number:
            self.steal_next_number = False
            stolen = copy.deepcopy(card)
            stolen.title = "someone else's card"
            self.cards[card.number] = stolen
        if card.number in self.cards:
            raise Duplicate(card.number)
        self.cards[card.number] = copy.deepcopy(card)

    def update_card(self, card, expected_version):
        cur = self.cards.get(card.number)
        if cur is None or cur.version != expected_version:
            return False
        self.cards[card.number] = copy.deepcopy(card)
        return True

    # links
    def links_on(self, board_ids):
        nums = {n for n, c in self.cards.items() if c.board_id in set(board_ids)}
        return [(a, b) for a, b in self.links if a in nums]

    def add_link(self, board_id, card, blocked_by, by):
        self.links.add((card, blocked_by))

    def remove_link(self, card, blocked_by):
        self.links.discard((card, blocked_by))

    # history
    def log(self, event):
        self.events.append(copy.deepcopy(event))

    def card_history(self, number, limit=100):
        return [e for e in reversed(self.events) if e.card_number == number][:limit]

    def board_activity(self, board_id, since, limit):
        out = [e for e in reversed(self.events) if e.board_id == board_id
               and (since is None or e.at >= since)]
        return out[:limit]


class FakeAI:
    """Embeds by word overlap: texts sharing words point the same way."""
    VOCAB = ["invoice", "bill", "pay", "launch", "website", "deploy", "hire", "design", "bug"]

    def __init__(self):
        self.summaries = []

    def checklist(self, title, description):
        return ["Gather what's needed", f"Do: {title}", "Check it with the team"]

    def summarize(self, facts):
        self.summaries.append(facts)
        return "Summary: " + facts.splitlines()[0]

    def embed(self, text):
        words = text.lower()
        return [1.0 if w in words else 0.0 for w in self.VOCAB] + [0.01]


class MemoryVectors:
    def __init__(self):
        self.rows = {}

    def upsert(self, number, board_id, vector):
        self.rows[number] = (board_id, vector)

    def has(self, number):
        return number in self.rows

    def nearest(self, vector, board_ids, k):
        def cos(a, b):
            na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
            return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0
        hits = [(n, 1 - cos(vector, v)) for n, (b, v) in self.rows.items() if b in board_ids]
        return sorted(hits, key=lambda x: x[1])[:k]


def now():
    return datetime.now(timezone.utc)
