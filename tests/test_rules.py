"""The board's rules, through the use cases: roles, visibility, cards, history, dependencies, AI.
No Dodil account needed: DataK3 and Dodil Models are in-memory fakes (tests/fakes.py)."""
from datetime import date, timedelta

import pytest

from boards.application.use_cases import Boards, NotFound
from boards.domain.model import Forbidden, Invalid, Person, Stale
from fakes import FakeAI, MemoryRepo, MemoryVectors

TODAY = date(2026, 11, 9)
OWNER, MEMBER, VIEWER, OUTSIDER, OTHER_BOARD = (Person(e) for e in (
    "ana@acme.io", "bo@acme.io", "cy@acme.io", "dee@else.io", "eli@acme.io"))


@pytest.fixture
def app():
    s = Boards(MemoryRepo(), FakeAI(), MemoryVectors(), today=lambda: TODAY)
    ws = s.create_workspace(OWNER, "Acme")
    for p, role in ((MEMBER, "member"), (VIEWER, "viewer"), (OTHER_BOARD, "member")):
        s.invite_member(OWNER, p.email, role)
    ops = s.create_board(OWNER, "Ops")
    for p in (MEMBER, VIEWER):
        s.add_to_board(OWNER, "Ops", p.email)
    s.create_board(OWNER, "Secret plans")  # only the owner is on this one
    return s, ws["id"], ops["id"]


# ------------------------------------------------------------------ people and roles

def test_only_the_owner_manages_people(app):
    s, _, _ = app
    for who in (MEMBER, VIEWER):
        with pytest.raises(Forbidden):
            s.invite_member(who, "new@acme.io", "member")
        with pytest.raises(Forbidden):
            s.change_role(who, VIEWER.email, "member")
        with pytest.raises(Forbidden):
            s.remove_member(who, VIEWER.email)
        with pytest.raises(Forbidden):
            s.add_to_board(who, "Ops", OTHER_BOARD.email)


def test_owner_cannot_be_invited_twice_or_demoted(app):
    s, _, _ = app
    with pytest.raises(Invalid):
        s.invite_member(OWNER, "x@acme.io", "owner")
    with pytest.raises(Invalid):
        s.invite_member(OWNER, OWNER.email, "member")
    with pytest.raises(Invalid):
        s.remove_member(OWNER, OWNER.email)


def test_a_viewer_can_read_but_never_change(app):
    s, _, _ = app
    card = s.add_card(MEMBER, "Ops", "Send invoice")
    assert s.show_board(VIEWER, "Ops")["role"] == "viewer"
    assert s.card_details(VIEWER, card["number"])["title"] == "Send invoice"
    attempts = [
        lambda: s.add_card(VIEWER, "Ops", "nope"),
        lambda: s.move_card(VIEWER, card["number"], "Done"),
        lambda: s.update_card(VIEWER, card["number"], title="changed"),
        lambda: s.assign_card(VIEWER, card["number"], "me"),
        lambda: s.add_list(VIEWER, "Ops", "Later"),
        lambda: s.create_board(VIEWER, "Mine"),
        lambda: s.suggest_checklist(VIEWER, card["number"], apply=True),
    ]
    for attempt in attempts:
        with pytest.raises(Forbidden):
            attempt()
    assert s.suggest_checklist(VIEWER, card["number"])["applied"] is False  # read-only is fine


def test_nobody_sees_a_board_they_are_not_on(app):
    s, _, _ = app
    secret = s.add_card(OWNER, "Secret plans", "Acquire competitor")
    for who in (MEMBER, VIEWER, OTHER_BOARD, OUTSIDER):
        titles = [b["title"] for b in s.my_boards(who)]
        assert "Secret plans" not in titles
        with pytest.raises(NotFound):
            s.show_board(who, "Secret plans")
        with pytest.raises(NotFound):
            s.card_details(who, secret["number"])
        with pytest.raises(NotFound):
            s.move_card(who, secret["number"], "Done")
        assert all(c["number"] != secret["number"] for c in s.search_cards(who, "acquire"))
        assert s.list_overdue(who) == [] or all(c["board"] != "Secret plans"
                                                 for c in s.list_overdue(who))
    assert s.my_boards(OUTSIDER) == []


def test_removed_members_lose_access_at_once(app):
    s, _, _ = app
    s.remove_member(OWNER, MEMBER.email)
    assert s.my_boards(MEMBER) == []
    with pytest.raises(NotFound):
        s.show_board(MEMBER, "Ops")


def test_role_change_takes_effect(app):
    s, _, _ = app
    s.change_role(OWNER, VIEWER.email, "member")
    assert s.add_card(VIEWER, "Ops", "now I can")["title"] == "now I can"
    s.change_role(OWNER, MEMBER.email, "viewer")
    with pytest.raises(Forbidden):
        s.add_card(MEMBER, "Ops", "not any more")


def test_only_workspace_people_can_be_put_on_a_board(app):
    s, _, _ = app
    with pytest.raises(Invalid):
        s.add_to_board(OWNER, "Ops", OUTSIDER.email)


# ------------------------------------------------------------------ cards

def test_cards_have_all_their_fields_and_short_numbers(app):
    s, _, _ = app
    c = s.add_card(MEMBER, "ops board", "Send invoice", list_name="doing",
                   description="For October", assignee="me", due_date="2026-11-12",
                   labels=["Finance", "finance", "urgent"])
    assert c["number"] == 1 and c["list"] == "Doing" and c["board"] == "Ops"
    assert c["assignee"] == MEMBER.email and c["due_date"] == "2026-11-12"
    assert c["labels"] == ["finance", "urgent"]
    assert s.add_card(MEMBER, "Ops", "Second")["number"] == 2


def test_a_card_number_taken_by_another_writer_is_retried(app):
    s, _, _ = app
    s.repo.steal_next_number = True
    c = s.add_card(MEMBER, "Ops", "Mine")
    assert c["number"] == 2 and s.repo.cards[1].title == "someone else's card"


def test_move_card_by_number_and_list_name(app):
    s, _, _ = app
    for t in ("a", "b", "c"):
        s.add_card(MEMBER, "Ops", t)
    moved = s.move_card(MEMBER, "#2", "done")
    assert moved["list"] == "Done" and moved["done"]
    with pytest.raises(Invalid):
        s.move_card(MEMBER, 2, "Someday")


def test_drag_and_drop_order_is_kept(app):
    s, _, _ = app
    for t in ("a", "b", "c"):
        s.add_card(MEMBER, "Ops", t)
    s.move_card(MEMBER, 3, "To do", before=1)          # c above a
    s.move_card(MEMBER, 2, "To do", before=3)          # b above c
    order = [c["title"] for c in s.show_board(MEMBER, "Ops")["lists"][0]["cards"]]
    assert order == ["b", "c", "a"]


def test_assignee_must_be_able_to_work_on_the_board(app):
    s, _, _ = app
    c = s.add_card(MEMBER, "Ops", "x")
    with pytest.raises(Invalid):
        s.assign_card(MEMBER, c["number"], OTHER_BOARD.email)   # not on Ops
    with pytest.raises(Invalid):
        s.assign_card(MEMBER, c["number"], VIEWER.email)        # viewers don't do cards
    assert s.assign_card(MEMBER, c["number"], OWNER.email)["assignee"] == OWNER.email
    assert s.assign_card(MEMBER, c["number"], "none")["assignee"] is None


def test_every_change_is_recorded_with_who_when_and_what(app):
    s, _, _ = app
    c = s.add_card(MEMBER, "Ops", "Send invoice")
    s.update_card(OWNER, c["number"], title="Send the invoice", due_date="2026-11-20")
    s.move_card(MEMBER, c["number"], "Doing")
    s.assign_card(OWNER, c["number"], MEMBER.email)
    history = s.card_details(VIEWER, c["number"])["history"]
    assert [h["actor"] for h in history] == [OWNER.email, MEMBER.email, OWNER.email, MEMBER.email]
    assert all(h["at"] for h in history)
    changes = {(d["field"], str(d["before"]), str(d["after"])) for h in history for d in h["changes"]}
    assert ("title", "Send invoice", "Send the invoice") in changes
    assert ("due_date", "None", "2026-11-20") in changes
    assert ("list", "To do", "Doing") in changes
    assert ("assignee", "None", MEMBER.email) in changes


def test_an_edit_on_a_stale_version_is_refused(app):
    s, _, _ = app
    c = s.add_card(MEMBER, "Ops", "x")
    s.update_card(OWNER, c["number"], expected_version=c["version"], title="y")
    with pytest.raises(Stale):
        s.update_card(MEMBER, c["number"], expected_version=c["version"], title="z")
    assert s.card_details(MEMBER, c["number"])["title"] == "y"


def test_bad_input_is_refused(app):
    s, _, _ = app
    with pytest.raises(Invalid):
        s.add_card(MEMBER, "Ops", "   ")
    with pytest.raises(Invalid):
        s.add_card(MEMBER, "Ops", "x", due_date="next friday")
    with pytest.raises(Invalid):
        s.add_card(MEMBER, "Ops", "x", labels=["<script>"])
    with pytest.raises(Invalid):
        s.move_card(MEMBER, "twelve", "Done")


# ------------------------------------------------------------------ questions

def test_whats_on_my_plate_this_week(app):
    s, _, _ = app
    d = lambda n: (TODAY + timedelta(days=n)).isoformat()
    s.add_card(MEMBER, "Ops", "late", assignee="me", due_date=d(-2))
    s.add_card(MEMBER, "Ops", "soon", assignee="me", due_date=d(3))
    s.add_card(MEMBER, "Ops", "later", assignee="me", due_date=d(30))
    s.add_card(MEMBER, "Ops", "whenever", assignee="me")
    s.add_card(MEMBER, "Ops", "not mine", assignee=OWNER.email, due_date=d(1))
    done = s.add_card(MEMBER, "Ops", "finished", assignee="me", due_date=d(1))
    s.move_card(MEMBER, done["number"], "Done")
    plate = s.my_cards(MEMBER, days=7)
    titles = {k: [c["title"] for c in plate[k]] for k in ("overdue", "due_soon", "no_due_date", "later")}
    assert titles == {"overdue": ["late"], "due_soon": ["soon"], "no_due_date": ["whenever"],
                      "later": ["later"]}


def test_whats_overdue_ignores_done_cards(app):
    s, _, _ = app
    late = s.add_card(MEMBER, "Ops", "late", due_date="2026-11-01")
    fixed = s.add_card(MEMBER, "Ops", "late but done", due_date="2026-11-01")
    s.move_card(MEMBER, fixed["number"], "Done")
    s.add_card(MEMBER, "Ops", "future", due_date="2026-12-01")
    overdue = s.list_overdue(MEMBER)
    assert [c["number"] for c in overdue] == [late["number"]]
    assert overdue[0]["days_late"] == 8 and overdue[0]["overdue"]


# ------------------------------------------------------------------ dependencies

def test_what_is_blocking_the_launch(app):
    s, _, _ = app
    launch = s.add_card(MEMBER, "Ops", "Launch the website")
    copy = s.add_card(MEMBER, "Ops", "Write copy")
    review = s.add_card(MEMBER, "Ops", "Legal review")
    s.block_card(MEMBER, launch["number"], copy["number"])
    s.block_card(MEMBER, copy["number"], review["number"])
    r = s.whats_blocking(VIEWER, "the launch")
    assert [c["number"] for c in r["blocking"]] == [copy["number"], review["number"]]
    assert r["blocking"][0]["direct"] and not r["blocking"][1]["direct"]
    s.move_card(MEMBER, review["number"], "Done")
    assert [c["number"] for c in s.whats_blocking(VIEWER, launch["number"])["blocking"]] == [copy["number"]]
    assert s.show_board(MEMBER, "Ops")["lists"][0]["cards"][0]["blocked_by"] == [copy["number"]]


def test_dependency_loops_and_cross_board_links_are_refused(app):
    s, _, _ = app
    a = s.add_card(OWNER, "Ops", "a")
    b = s.add_card(OWNER, "Ops", "b")
    other = s.add_card(OWNER, "Secret plans", "c")
    s.block_card(OWNER, a["number"], b["number"])
    with pytest.raises(Invalid):
        s.block_card(OWNER, b["number"], a["number"])
    with pytest.raises(Invalid):
        s.block_card(OWNER, a["number"], a["number"])
    with pytest.raises(Invalid):
        s.block_card(OWNER, a["number"], other["number"])
    with pytest.raises(NotFound):  # a member can't even name a card they can't see
        s.block_card(MEMBER, a["number"], other["number"])


# ------------------------------------------------------------------ AI extras

def test_checklist_is_a_suggestion_until_applied(app):
    s, _, _ = app
    c = s.add_card(MEMBER, "Ops", "Send invoice")
    r = s.suggest_checklist(MEMBER, c["number"])
    assert r["checklist"] and not r["applied"]
    assert s.card_details(MEMBER, c["number"])["description"] == ""
    r = s.suggest_checklist(MEMBER, c["number"], apply=True)
    assert r["applied"] and "- [ ] Do: Send invoice" in r["description"]


def test_similar_cards_respect_visibility(app):
    s, _, _ = app
    a = s.add_card(MEMBER, "Ops", "Pay the invoice from Globex")
    s.add_card(MEMBER, "Ops", "Fix login bug")
    s.add_card(OWNER, "Secret plans", "Invoice the acquisition target")
    hits = s.find_similar(MEMBER, number=a["number"])
    assert [h["title"] for h in hits] == ["Fix login bug"]  # the secret invoice never shows
    assert all(h["board"] == "Ops" for h in hits)
    assert all(h["number"] != a["number"] for h in hits)
    owner_hits = s.find_similar(OWNER, text="invoice")
    assert {h["board"] for h in owner_hits} == {"Ops", "Secret plans"}


def test_daily_summary_uses_only_the_board(app):
    s, _, _ = app
    s.add_card(MEMBER, "Ops", "Overdue thing", due_date="2026-11-01")
    s.add_card(OWNER, "Secret plans", "Do not leak me")
    r = s.board_summary(VIEWER, "Ops")
    assert r["summary"].startswith("Summary: Board: Ops")
    assert any("Overdue thing" in x for x in r["facts"]["overdue"])
    assert "Do not leak me" not in s.assistant.summaries[-1]
