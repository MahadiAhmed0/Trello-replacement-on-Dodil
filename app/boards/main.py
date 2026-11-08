"""Wires the layers together: one FastAPI app with the web routes, one /mcp, the sign-in metadata
Claude.ai and ChatGPT use to find where a person signs in (RFC 9728), /healthz, and a POST /
self-check that opens the bucket with the app's own credential."""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from boards.application.use_cases import Boards
from boards.doors.identity import person_from
from boards.doors.mcp import build_mcp
from boards.doors.web import build_router

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
APPID = os.environ.get("APPID_PUBLIC_URL", "https://appid.dodil.io").rstrip("/")
POOL = os.environ.get("DODIL_USER_POOL", "")  # "<org>/<pool>", set by .dodil/deploy.yaml
METADATA = "/.well-known/oauth-protected-resource"
TABLES = ("workspaces", "workspace_members", "boards", "board_members", "board_lists", "cards",
          "card_links", "activity", "card_vectors")


def create_app(boards: Boards, self_check: Optional[Callable[[], dict]] = None) -> FastAPI:
    gate = {"mcp": None}

    @asynccontextmanager
    async def lifespan(_app):
        mcp_app = build_mcp(boards).streamable_http_app(
            streamable_http_path="/mcp", stateless_http=True, json_response=True,
            # Every call carries identity only the gateway can mint, so host checks add nothing.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        gate["mcp"] = mcp_app
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(title="Tasklane", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    def base(request: Request) -> str:
        h = request.headers
        return f"{h.get('x-forwarded-proto', 'https')}://{h.get('x-forwarded-host', h.get('host'))}"

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    checked = {"at": 0.0, "body": None}

    @app.post("/", include_in_schema=False)
    def check():
        """`dodil ignite invoke` hits POST /: prove the app reaches its own bucket. Row counts
        only, cached for 30 s, so it reveals nothing and can't be used to load the database."""
        if self_check is None:
            return {"ok": True, "tables": "not configured"}
        if time.time() - checked["at"] > 30 or checked["body"] is None:
            try:
                checked["body"] = {"ok": True, "tables": self_check()}
            except Exception as e:  # report, don't crash: this is the diagnostic route
                checked["body"] = {"ok": False, "error": type(e).__name__}
            checked["at"] = time.time()
        return JSONResponse(checked["body"], status_code=200 if checked["body"]["ok"] else 503)

    @app.get(METADATA, include_in_schema=False)
    @app.get(METADATA + "/mcp", include_in_schema=False)
    def metadata(request: Request):
        return {"resource": base(request) + "/mcp", "authorization_servers": [f"{APPID}/{POOL}"],
                "bearer_methods_supported": ["header"], "resource_name": "Tasklane"}

    class McpDoor:
        """No person -> 401 with the metadata link, which is what starts Claude's sign-in.
        (A class, so Starlette hands it the raw ASGI call instead of a Request.)"""

        async def __call__(self, scope, receive, send):
            headers = {k.decode("latin-1"): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            if person_from(headers) is None:
                body = json.dumps({"detail": "sign in first"}).encode()
                link = f'Bearer resource_metadata="{base(Request(scope))}{METADATA}/mcp"'
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"), (b"www-authenticate", link.encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            await gate["mcp"]({**scope, "path": "/mcp"}, receive, send)

    mcp_door = McpDoor()
    for path in ("/mcp", "/mcp/"):
        app.router.routes.append(Route(path, endpoint=mcp_door, methods=["GET", "POST", "DELETE"]))
    app.include_router(build_router(boards))
    return app


def _production() -> FastAPI:
    from boards.infrastructure.dodil import Database, ModelsAssistant
    from boards.infrastructure.repository import SqlRepository, SqlVectors

    db = Database()
    background = ThreadPoolExecutor(max_workers=2, thread_name_prefix="index")

    def counts() -> dict:
        def q(c):
            return {t: int(c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()[0])
                    for t in TABLES}
        return db.run(q)

    def warm_up():
        """Fetch the service token and open a few DataK3 connections before the first visitor
        does: otherwise the first page after a deploy pays for TLS handshakes and a token."""
        try:
            db.parallel(*[lambda: db.run(lambda c: c.execute("SELECT 1 AS ok").fetchone())] * 4)
        except Exception:
            logging.getLogger("tasklane").warning("warm-up failed; the first request will retry")

    if os.environ.get("BUCKET"):
        background.submit(warm_up)
    boards = Boards(SqlRepository(db), ModelsAssistant(), SqlVectors(db),
                    defer=lambda fn: background.submit(fn))
    return create_app(boards, self_check=counts)


app = _production()
