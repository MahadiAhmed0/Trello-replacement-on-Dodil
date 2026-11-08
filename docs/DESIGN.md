# Tasklane design notes

Short notes on the decisions that shape the code. The code itself is the long version.

## Layers (light DDD)

```
app/boards/
  domain/model.py          Workspace, Board, BoardList, Card and their rules. Pure Python.
  application/ports.py     What the use cases need: Repository, Assistant (AI), VectorIndex.
  application/use_cases.py Every action, as one class (Boards). Visibility is decided here.
  infrastructure/          DataK3 (Postgres wire), Dodil Models, service-account token.
  doors/web.py             JSON routes + the page   -> Boards
  doors/mcp.py             MCP tools                -> Boards
  doors/identity.py        The person the Dodil gateway signed in (X-Dodil-User).
  main.py                  Wiring, /mcp, sign-in metadata, /healthz, POST / self-check.
```

The two doors are thin: they turn a request into one use-case call and turn the domain's
exceptions into HTTP codes or MCP tool errors. Nothing in a door checks a role. That is why a rule
like "a viewer can't edit" can't be skipped by switching from the web page to Claude: there is
only one place it is enforced, and both doors go through it. The tests run the same scenarios
through both doors to prove it.

## People and visibility

- **Workspace** = an owner plus invited people, each `member` or `viewer`. Only the owner
  invites, changes roles or removes people (`Workspace.require_owner`).
- **Board** belongs to one workspace. You see a board only if you are **on** it (the workspace
  owner is on every board). Your role on a board is your workspace role. Being on a board is
  managed by the owner too, so "only the owner manages members" holds at both levels.
- Every read starts from `Boards._view(person)`: the person's workspaces, then the boards they're
  on, then those boards' lists. Everything else (cards, search, overdue, similar cards, summaries)
  is filtered through that view. A board or card outside it is **404, not 403**, so you can't
  probe for things you aren't allowed to know exist.
- The MCP door acts as the signed-in person (the gateway passes their identity on every call),
  so Claude sees exactly what that person sees.

## Cards

- **Numbers.** "Move card 12 to Done" needs a short, stable number. DataK3 has no sequences, so
  a card number is *claimed* with a plain `INSERT` on the primary key (`max + 1`). Two people
  adding at once: one INSERT wins, the other gets `23505` and retries with the next number. An
  upsert here would let both writers "win" and overwrite each other.
- **No lost updates.** Each card carries a `version`. Every write is
  `UPDATE ... WHERE card_no = ? AND version = ?`; if nothing matched, someone else got there
  first and the caller gets 409 "changed since you opened it" (the web page reloads the card).
- **Order inside a list** is a float `sort_key`. Dropping a card between two others takes the
  midpoint, so a drag writes one row instead of renumbering the list.
- **Lists** default to To do / Doing / Done. A list can be marked `is_done`; overdue and "what's
  on my plate" ignore cards in done lists.

## History

Every change writes an `activity` row: who (the person's email from the gateway), when (UTC),
what (`action`), and the exact field changes as JSON `[{field, before, after}]`. Moves, edits,
assignments, blockers, invitations and role changes are all recorded. The card dialog and the
`card_details` MCP tool show a card's full history; `board_activity` shows a board's.

## Extras

- **AI checklist** (`checklist_for_card`): Dodil Models (`kimi-k2.6`) suggests steps. It writes
  nothing unless asked (`apply=true`), and then only for someone allowed to edit the card.
- **Find similar** (`find_similar`): card text is embedded with `jina-embeddings-v4` into a
  `VECTOR(2048)` column in the same bucket and searched with pgvector's `<=>`. The KNN query is
  filtered to the caller's boards **and** every hit is re-checked against the view, so the index
  can never leak a card. Embedding runs in a background thread so adding a card stays fast;
  cards without a vector are backfilled on the next search.
- **Daily summary** (`board_summary`): the facts (per-list counts, overdue, due soon, blocked,
  last 24 hours) are computed in code; Models only turns them into prose. If Models is down you
  still get the facts.
- **Dependencies**: "blocked by" links between cards on the same board. Loops are refused.
  `whats_blocking` walks the chain and lists every *open* blocker, nearest first.
- **Overdue** is computed on every read (due date before today, not in a done list) and flagged
  in red on the board, in the API and in MCP results.

## Security choices

- **No login code.** The Dodil gateway signs people in through the app's user pool and injects
  `X-Dodil-User`; it strips any copy a client sends. The app refuses every route without an
  identity except `/healthz`, the OAuth metadata, and the `POST /` self-check (row counts only).
- **DEV_USER** (act as someone locally) is ignored whenever `DODIL_USER_POOL` is set, so it can't
  open the deployed app to anonymous users by mistake.
- **CSRF:** state-changing web calls must carry `X-Tasklane: 1`. Browsers only send a custom
  header cross-site after a CORS preflight, which this app never approves.
- **XSS:** the page builds the DOM with `textContent` only and ships a strict
  Content-Security-Policy (`script-src 'self'`, no inline script).
- **Prompt injection:** card text sent to Models is labelled as data, AI output is only a
  suggestion, and tools never act on what a card says.
- **Secrets:** the only credential is the service account the remix generates; it reaches the
  app through CI secrets referenced in `.dodil/deploy.yaml`. Nothing secret is in the repo.

## What I would improve next

- Idempotency keys on `add_card` so a client that lost the reply can retry safely.
- Comments and attachments (DataK3 object storage) on cards.
- Card archive/delete with an undo window.
- Graph-native dependency queries (`graph_khop`) once boards get large.
- Live updates (SSE) so two people on one board see each other's drags immediately.
