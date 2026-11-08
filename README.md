# Tasklane

Team task boards you can remix into your own Dodil organization and have running in minutes,
and use from Claude or ChatGPT with real actions.

- **Workspaces and roles.** An owner invites people as `member` or `viewer`. Viewers can read but
  never change anything. Only the owner manages people.
- **Boards → lists → cards.** Cards have a title, description, assignee, due date and labels, a
  short number ("card 12"), and move between lists by drag and drop.
- **History.** Every change to a card is recorded: who, when, and each field's before and after.
- **MCP.** One `/mcp` endpoint with action tools (`add_card`, `move_card`, `assign_card`,
  `my_cards`, `list_overdue`, `whats_blocking`...). Claude sees only the boards its person sees.
- **Extras.** AI checklists and daily board summaries (Dodil Models), "find similar cards"
  (vector search in DataK3), "blocked by" dependencies with "what is blocking X?", and overdue
  cards flagged automatically.

Built on Dodil only: **DataK3** for all data, an **appid user pool** for sign-in, **Ignite**
to run it, **Dodil Models** for AI, **Dodil Git CI** to deploy. No other services, no API keys.

## Remix it (one command)

```bash
dodil git remix <this-org>/tasklane --to-org <your-org> --name <your-repo>
```

Keep `<your-repo>` to **18 characters or less** (the bucket `<repo>-data` must fit in 25).
The remix creates the repo, the DataK3 bucket `<repo>-data` with every table from
[`schema.sql`](schema.sql), the user pool `<repo>-users`, and a service account that can use the
bucket and Dodil Models. CI then tests, builds and deploys the app to Ignite.

### The one step after the remix

**Make sure the app's sign-in callback is on the user pool.** The deploy registers it itself
(Dodil Git 1.6.11 and later). Open the deploy log: if it says the callback was **"not
registered"**, run the command that line prints, once. It has this shape (use the host from
`dodil ignite app get <your-repo>`, copied exactly):

```bash
dodil appid settings set <your-repo>-users --redirect-uris https://<app-host>/.dodil/auth/callback
```

That's it. Open `https://<app-host>/`, sign up with your email, and create your workspace: the
first person to create a workspace owns it.

Optional:

- **Connect Claude:** add `https://<app-host>/mcp` as a custom connector in Claude (or ChatGPT)
  and sign in. The client registers itself and waits for approval. Approve it once:
  `dodil appid client list <your-repo>-users`, then
  `dodil appid client approve <your-repo>-users <client-id>`.
- **Recommended hardening:** require verified emails, so nobody can sign up with someone else's
  address and inherit their invitations:
  `dodil appid settings set <your-repo>-users --require-email-verification`
  (see [Security notes](#security-notes)).

## Using it

**On the web:** pick a workspace and board at the top. Drag cards between lists (or open a card
and change its list). Click a card to edit it, see its history, add "blocked by" links, ask the
assistant for a checklist or similar cards. "My plate", "Overdue", "People", "Daily summary" and
"Activity" open in the side panel. The owner invites people under **People** and puts them on
boards.

**From Claude or ChatGPT**, for example:

| You say | Tool it calls |
|---|---|
| What's on my plate this week? | `my_cards(days=7)` |
| Add a card "Send invoice" to Doing on the Ops board | `add_card(board="Ops", title="Send invoice", list_name="Doing")` |
| Move card 12 to Done | `move_card(number=12, list_name="Done")` |
| What's overdue? | `list_overdue()` |
| Assign 14 to bo@acme.io, due Friday | `assign_card`, `update_card` |
| What is blocking the launch? | `whats_blocking(card="launch")` |
| Turn card 9 into a checklist | `checklist_for_card(number=9, apply=true)` |
| Find cards like 9 | `find_similar(number=9)` |
| Summarize the Ops board for today | `board_summary(board="Ops")` |
| Invite cy@acme.io as a viewer and put them on Ops | `invite_member`, `add_to_board` |

All tools: `whoami`, `my_boards`, `show_board`, `card_details`, `my_cards`, `list_overdue`,
`search_cards`, `whats_blocking`, `board_activity`, `workspace_people`, `board_summary`,
`find_similar`, `checklist_for_card`, `add_card`, `move_card`, `assign_card`, `update_card`,
`block_card`, `unblock_card`, `create_workspace`, `create_board`, `add_list`, `invite_member`,
`change_role`, `remove_member`, `add_to_board`, `remove_from_board`.

## How it's built

Python, FastAPI, light Domain-Driven Design. The web routes and the MCP tools are two thin doors
onto the **same use cases**, so every rule lives in one place. Details and the reasons behind
them: **[docs/DESIGN.md](docs/DESIGN.md)**.

```
app/boards/
  domain/            Workspace, Board, BoardList, Card: the rules (no database, no HTTP)
  application/       use cases (create_board, add_card, move_card, assign_card, my_cards,
                     list_overdue, whats_blocking, board_summary...) and the ports they need
  infrastructure/    DataK3 repository + vectors, Dodil Models, service-account token
  doors/             web.py (JSON + page), mcp.py (tools), identity.py (who is signing in)
  main.py            wiring, one /mcp, sign-in metadata, /healthz, POST / self-check
tests/               the rules through the use cases and through both doors
schema.sql           the tables (created by the remix)
.dodil/remix.yaml    what a remix creates
.dodil/deploy.yaml   how CI deploys every copy
.github/workflows/   test -> build -> deploy (Dodil Git CI)
```

## Run the tests and the app locally

```bash
cd app
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
python -m pytest -q ../tests                      # no Dodil account needed: fakes for data and AI
```

To run the server against a bucket, set `BUCKET`, `DODIL_SERVICE_ACCOUNT_ID`,
`DODIL_SERVICE_ACCOUNT_SECRET` and `DEV_USER=you@example.com` (acts as that person without the
gateway; ignored whenever `DODIL_USER_POOL` is set), then
`uvicorn boards.main:app --port 8080`. Use a test bucket, never the live one.

## Security notes

- Sign-in is entirely Dodil's: the gateway signs people in and passes their identity to the app.
  The app has no passwords, sessions or login code, and refuses any route without a person
  (except `/healthz`, the OAuth metadata, and a `POST /` self-check that returns row counts only).
- Workspace invitations are matched by **email**. A new Dodil user pool allows public sign-up and
  does not require email verification by default, so someone could sign up with an invited
  person's address before they do. Turn on `--require-email-verification` on the pool (above),
  or switch sign-up off (`--allow-signup=false`) and create accounts yourself with
  `dodil appid user create`.
- Web changes need a same-site header (CSRF), the page has a strict Content-Security-Policy and
  never renders card text as HTML, and AI output is only a suggestion until a person applies it.

## What I'd improve next

Idempotency keys for card creation, comments and attachments, archiving cards, live updates
between people on the same board, and graph-native dependency queries for very large boards.

## Licence

MIT. See [LICENSE](LICENSE).
