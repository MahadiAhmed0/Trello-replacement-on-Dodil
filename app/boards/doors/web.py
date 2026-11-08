"""The web door: JSON routes for the board page. Every route calls one use case and nothing else.

Cross-site requests: every state-changing /api call must carry the header X-Tasklane: 1. A browser
only lets another site send a custom header after a CORS preflight this app never approves, so a
forged form or fetch from elsewhere can't act with the person's session cookie.
"""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from boards.application.use_cases import Boards, NotFound
from boards.doors.identity import person_from
from boards.domain.model import Forbidden, Invalid, Person, Stale

STATIC = Path(__file__).parent / "static"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def build_router(boards: Boards) -> APIRouter:
    def me(request: Request) -> Person:
        p = person_from(request.headers)
        if p is None:
            raise HTTPException(401, "sign in first: /.dodil/auth/login")
        return p

    def acting(request: Request) -> Person:
        """A signed-in person making a change from this app's own page."""
        p = me(request)
        if request.headers.get("x-tasklane") != "1":
            raise HTTPException(403, "missing X-Tasklane header")
        return p

    r = APIRouter()
    read = Depends(me)
    write = Depends(acting)

    def run(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NotFound as e:
            raise HTTPException(404, str(e))
        except Forbidden as e:
            raise HTTPException(403, str(e))
        except Stale as e:
            raise HTTPException(409, str(e))
        except Invalid as e:
            raise HTTPException(422, str(e))

    class Name(BaseModel):
        name: str = Field(max_length=200)

    class BoardIn(BaseModel):
        title: str = Field(max_length=200)
        workspace_id: Optional[str] = None

    class ListIn(BaseModel):
        name: str = Field(max_length=200)
        is_done: bool = False

    class Invite(BaseModel):
        email: str = Field(max_length=254)
        role: str = "member"

    class RoleIn(BaseModel):
        role: str

    class Email(BaseModel):
        email: str = Field(max_length=254)

    class CardIn(BaseModel):
        title: str = Field(max_length=200)
        list_id: Optional[str] = None
        description: str = Field("", max_length=10_000)
        assignee: Optional[str] = None
        due_date: Optional[str] = None
        labels: list[str] = Field(default_factory=list, max_length=10)

    class CardEdit(BaseModel):
        version: Optional[int] = None
        title: Optional[str] = Field(None, max_length=200)
        description: Optional[str] = Field(None, max_length=10_000)
        assignee: Optional[str] = None  # "" unassigns
        due_date: Optional[str] = None  # "" clears
        labels: Optional[list[str]] = Field(None, max_length=10)

    class Move(BaseModel):
        list_id: str
        before: Optional[int] = None

    class Assign(BaseModel):
        assignee: Optional[str] = None

    class Blocker(BaseModel):
        blocked_by: int

    class Apply(BaseModel):
        apply: bool = False

    # ---------------------------------------------------------------- people & workspaces

    @r.get("/api/start")
    def start(board: Optional[str] = None, workspace: Optional[str] = None, p: Person = read):
        return run(boards.start, p, board, workspace)

    @r.get("/api/me")
    def whoami(p: Person = read):
        return run(boards.me, p)

    @r.post("/api/workspaces", status_code=201)
    def create_workspace(body: Name, p: Person = write):
        return run(boards.create_workspace, p, body.name)

    @r.get("/api/workspaces/{ws}/people")
    def people(ws: str, p: Person = read):
        return run(boards.workspace_people, p, ws)

    @r.post("/api/workspaces/{ws}/members", status_code=201)
    def invite(ws: str, body: Invite, p: Person = write):
        return run(boards.invite_member, p, body.email, body.role, ws)

    @r.put("/api/workspaces/{ws}/members/{email}")
    def change_role(ws: str, email: str, body: RoleIn, p: Person = write):
        return run(boards.change_role, p, email, body.role, ws)

    @r.delete("/api/workspaces/{ws}/members/{email}")
    def remove_member(ws: str, email: str, p: Person = write):
        return run(boards.remove_member, p, email, ws)

    # ---------------------------------------------------------------- boards

    @r.get("/api/boards")
    def my_boards(p: Person = read):
        return run(boards.my_boards, p)

    @r.post("/api/boards", status_code=201)
    def create_board(body: BoardIn, p: Person = write):
        return run(boards.create_board, p, body.title, body.workspace_id)

    @r.get("/api/boards/{board_id}")
    def show_board(board_id: str, p: Person = read):
        return run(boards.show_board, p, board_id)

    @r.get("/api/boards/{board_id}/activity")
    def activity(board_id: str, limit: int = 50, p: Person = read):
        return run(boards.board_activity, p, board_id, limit)

    @r.get("/api/boards/{board_id}/summary")
    def summary(board_id: str, p: Person = read):
        return run(boards.board_summary, p, board_id)

    @r.post("/api/boards/{board_id}/lists", status_code=201)
    def add_list(board_id: str, body: ListIn, p: Person = write):
        return run(boards.add_list, p, board_id, body.name, body.is_done)

    @r.post("/api/boards/{board_id}/people", status_code=201)
    def add_to_board(board_id: str, body: Email, p: Person = write):
        return run(boards.add_to_board, p, board_id, body.email)

    @r.delete("/api/boards/{board_id}/people/{email}")
    def remove_from_board(board_id: str, email: str, p: Person = write):
        return run(boards.remove_from_board, p, board_id, email)

    @r.post("/api/boards/{board_id}/cards", status_code=201)
    def add_card(board_id: str, body: CardIn, p: Person = write):
        return run(boards.add_card, p, board_id, body.title, body.list_id, body.description,
                   body.assignee, body.due_date, body.labels)

    # ---------------------------------------------------------------- cards

    @r.get("/api/cards/{number}")
    def card(number: int, p: Person = read):
        return run(boards.card_details, p, number)

    @r.patch("/api/cards/{number}")
    def edit_card(number: int, body: CardEdit, p: Person = write):
        fields = body.model_dump(exclude_unset=True)
        version = fields.pop("version", None)
        if "assignee" in fields and not fields["assignee"]:
            fields["assignee"] = "none"
        if "due_date" in fields and not fields["due_date"]:
            fields["due_date"] = "none"
        return run(boards.update_card, p, number, expected_version=version, **fields)

    @r.post("/api/cards/{number}/move")
    def move(number: int, body: Move, p: Person = write):
        return run(boards.move_card, p, number, body.list_id, body.before)

    @r.post("/api/cards/{number}/assign")
    def assign(number: int, body: Assign, p: Person = write):
        return run(boards.assign_card, p, number, body.assignee)

    @r.post("/api/cards/{number}/blockers", status_code=201)
    def block(number: int, body: Blocker, p: Person = write):
        return run(boards.block_card, p, number, body.blocked_by)

    @r.delete("/api/cards/{number}/blockers/{blocked_by}")
    def unblock(number: int, blocked_by: int, p: Person = write):
        return run(boards.unblock_card, p, number, blocked_by)

    @r.get("/api/cards/{number}/blocking")
    def blocking(number: int, p: Person = read):
        return run(boards.whats_blocking, p, str(number))

    @r.post("/api/cards/{number}/checklist")
    def checklist(number: int, body: Apply, p: Person = write):
        return run(boards.suggest_checklist, p, number, body.apply)

    @r.get("/api/cards/{number}/similar")
    def similar(number: int, p: Person = read):
        return run(boards.find_similar, p, number, None, 5)

    # ---------------------------------------------------------------- questions

    @r.get("/api/my-cards")
    def my_cards(days: int = 7, p: Person = read):
        return run(boards.my_cards, p, days)

    @r.get("/api/overdue")
    def overdue(board: Optional[str] = None, p: Person = read):
        return run(boards.list_overdue, p, board)

    @r.get("/api/search")
    def search(q: str, p: Person = read):
        return run(boards.search_cards, p, q)

    # ---------------------------------------------------------------- the page

    headers = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
               "Referrer-Policy": "same-origin", "Cache-Control": "no-store"}

    @r.get("/", response_class=HTMLResponse, include_in_schema=False)
    def page(request: Request):
        if person_from(request.headers) is None:
            return RedirectResponse("/.dodil/auth/login?return_to=/", status_code=303)
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers=headers)

    @r.get("/static/{name}", include_in_schema=False)
    def static(name: str, p: Person = read):
        allowed = {"app.js": "text/javascript", "app.css": "text/css", "logo.svg": "image/svg+xml"}
        if name not in allowed:
            raise HTTPException(404, "not found")
        return FileResponse(STATIC / name, media_type=allowed[name],
                            headers={"X-Content-Type-Options": "nosniff",
                                     "Cache-Control": "private, max-age=300"})

    return r
