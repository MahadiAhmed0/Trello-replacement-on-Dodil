"""Both doors, web and MCP, over the same use cases: sign-in, roles, CSRF, and the example
requests from the brief ("add a card 'Send invoice' to Doing on the Ops board", "move card 12 to
Done", "what's overdue?", "what's on my plate this week?")."""
import importlib
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from boards.application.use_cases import Boards
from boards.domain.model import Person
from boards.main import create_app
from fakes import FakeAI, MemoryRepo, MemoryVectors

OWNER, MEMBER, VIEWER, OUTSIDER = (Person(e) for e in
                                   ("ana@acme.io", "bo@acme.io", "cy@acme.io", "dee@else.io"))


def who(p, **extra):
    return {"X-Dodil-User": json.dumps({"sub": "id-" + p.email, "email": p.email, **extra})}


def act(p):
    return {**who(p), "X-Tasklane": "1"}


@pytest.fixture
def svc():
    s = Boards(MemoryRepo(), FakeAI(), MemoryVectors(), today=lambda: date(2026, 11, 9))
    s.create_workspace(OWNER, "Acme")
    s.invite_member(OWNER, MEMBER.email, "member")
    s.invite_member(OWNER, VIEWER.email, "viewer")
    b = s.create_board(OWNER, "Ops")
    s.add_to_board(OWNER, "Ops", MEMBER.email)
    s.add_to_board(OWNER, "Ops", VIEWER.email)
    return s, b["id"]


@pytest.fixture
def client(svc):
    s, bid = svc
    with TestClient(create_app(s)) as c:
        yield c, s, bid


# ------------------------------------------------------------------ web door

def test_everything_but_health_and_metadata_needs_sign_in(client):
    c, _, bid = client
    assert c.get("/healthz").status_code == 200
    assert c.get("/.well-known/oauth-protected-resource").status_code == 200
    for path in ("/api/me", "/api/boards", f"/api/boards/{bid}", "/api/my-cards",
                 "/static/app.js"):
        assert c.get(path).status_code == 401, path
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/.dodil/auth/login")


def test_the_page_and_its_assets_load_with_a_strict_policy(client):
    c, _, _ = client
    r = c.get("/", headers=who(MEMBER))
    assert r.status_code == 200 and "Tasklane" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/static/app.js", headers=who(MEMBER)).status_code == 200
    assert c.get("/static/../main.py", headers=who(MEMBER)).status_code == 404


def test_changes_need_the_same_site_header(client):
    c, _, bid = client
    body = {"title": "forged"}
    assert c.post(f"/api/boards/{bid}/cards", json=body, headers=who(MEMBER)).status_code == 403
    assert c.post(f"/api/boards/{bid}/cards", json=body, headers=act(MEMBER)).status_code == 201


def test_web_roles_hold(client):
    c, _, bid = client
    assert c.get("/api/boards", headers=who(VIEWER)).json()[0]["role"] == "viewer"
    assert c.post(f"/api/boards/{bid}/cards", json={"title": "t"},
                  headers=act(VIEWER)).status_code == 403
    assert c.get(f"/api/boards/{bid}", headers=who(OUTSIDER)).status_code == 404
    assert c.post("/api/workspaces/x/members", json={"email": "z@acme.io"},
                  headers=act(MEMBER)).status_code == 404


def test_drag_and_drop_and_a_stale_edit_over_http(client):
    c, _, bid = client
    board = c.get(f"/api/boards/{bid}", headers=who(MEMBER)).json()
    done = next(l for l in board["lists"] if l["name"] == "Done")
    card = c.post(f"/api/boards/{bid}/cards", json={"title": "x"}, headers=act(MEMBER)).json()
    r = c.post(f"/api/cards/{card['number']}/move", json={"list_id": done["id"]},
               headers=act(MEMBER))
    assert r.status_code == 200 and r.json()["list"] == "Done"
    ok = c.patch(f"/api/cards/{card['number']}", headers=act(MEMBER),
                 json={"version": r.json()["version"], "title": "y"})
    assert ok.status_code == 200
    stale = c.patch(f"/api/cards/{card['number']}", headers=act(OWNER),
                    json={"version": r.json()["version"], "title": "z"})
    assert stale.status_code == 409


def test_garbage_identity_is_no_identity(client):
    c, _, _ = client
    for raw in ("not json", "[]", json.dumps({"sub": "abc"}), json.dumps({"email": "nope"})):
        assert c.get("/api/me", headers={"X-Dodil-User": raw}).status_code == 401


def test_dev_user_is_ignored_behind_the_gateway(monkeypatch):
    import boards.doors.identity as identity
    monkeypatch.setenv("DEV_USER", "dev@acme.io")
    monkeypatch.setenv("DODIL_USER_POOL", "acme/tasklane-users")
    assert importlib.reload(identity).person_from({}) is None
    monkeypatch.delenv("DODIL_USER_POOL")
    assert importlib.reload(identity).person_from({}) == Person("dev@acme.io")
    monkeypatch.delenv("DEV_USER")
    importlib.reload(identity)


# ------------------------------------------------------------------ MCP door

MCP_ACCEPT = {"accept": "application/json, text/event-stream"}


def mcp(c, p, name, **args):
    h = {**who(p), **MCP_ACCEPT}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}}}
    assert c.post("/mcp", json=init, headers=h).status_code == 200
    r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": name, "arguments": args}})
    return r.json()["result"]


def payload(result):
    assert not result.get("isError"), result
    sc = result.get("structuredContent")
    if sc is not None:
        return sc.get("result", sc)
    return json.loads(result["content"][0]["text"])


def test_mcp_needs_a_person_and_points_at_sign_in(client):
    c, _, _ = client
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]


def test_mcp_tools_are_actions_not_tables(client):
    c, _, _ = client
    h = {**who(MEMBER), **MCP_ACCEPT}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    c.post("/mcp", json=init, headers=h)
    tools = {t["name"] for t in c.post("/mcp", headers=h, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]}
    assert {"move_card", "assign_card", "add_card", "my_cards", "list_overdue",
            "whats_blocking", "board_summary"} <= tools
    assert not any(t.startswith(("sql", "query", "table")) for t in tools)


def test_the_brief_examples_over_mcp(client):
    c, s, _ = client
    added = payload(mcp(c, MEMBER, "add_card", board="Ops board", title="Send invoice",
                        list_name="Doing", assignee="me", due_date="2026-11-12"))
    assert added["list"] == "Doing" and added["board"] == "Ops"
    n = added["number"]
    plate = payload(mcp(c, MEMBER, "my_cards", days=7))
    assert [x["number"] for x in plate["due_soon"]] == [n]
    moved = payload(mcp(c, MEMBER, "move_card", number=n, list_name="Done"))
    assert moved["list"] == "Done"
    payload(mcp(c, MEMBER, "add_card", board="Ops", title="Old", due_date="2026-11-01"))
    overdue = payload(mcp(c, MEMBER, "list_overdue"))["cards"]
    assert [x["title"] for x in overdue] == ["Old"]
    history = payload(mcp(c, VIEWER, "card_details", number=n))["history"]
    assert {h["actor"] for h in history} == {MEMBER.email}


def test_mcp_sees_only_what_the_person_sees(client):
    c, s, _ = client
    s.create_board(OWNER, "Board meeting")
    secret = s.add_card(OWNER, "Board meeting", "Layoffs plan")
    boards = payload(mcp(c, MEMBER, "my_boards"))["boards"]
    assert [b["title"] for b in boards] == ["Ops"]
    r = mcp(c, MEMBER, "card_details", number=secret["number"])
    assert r.get("isError") and "no card" in r["content"][0]["text"]
    r = mcp(c, OUTSIDER, "show_board", board="Ops")
    assert r.get("isError")


def test_mcp_viewer_is_refused_with_a_reason(client):
    c, _, _ = client
    r = mcp(c, VIEWER, "add_card", board="Ops", title="nope")
    assert r.get("isError") and "viewer" in r["content"][0]["text"]
    r = mcp(c, MEMBER, "invite_member", email="new@acme.io")
    assert r.get("isError") and "owner" in r["content"][0]["text"]
