# agent-sentry frontend

A single static page that runs the agent-sentry skills. Left nav lists the discovered
skills (from `GET /skills`); the right pane runs the selected skill (`POST /run`) and renders
the returned briefing markdown.

No build step — plain HTML/CSS/JS. `marked` and `DOMPurify` are vendored under `vendor/`
(no CDN at runtime). The page calls the API cross-origin; the API allowlists this origin.

## Run

The easiest path is `./start-ui.sh` from the repo root: it starts the API + static server and
generates `frontend/config.js` from `.env`, so the page **auto-connects using the
`SENTRY_API_KEY` from `.env`** — you are never asked to paste it. `./stop-ui.sh` tears both
down and removes the generated config.

To run the pieces manually instead:

1. Start the API (from the repo root). Needs `SENTRY_API_KEY` and `ANTHROPIC_API_KEY` in
   `.env`:
   ```bash
   uvicorn src.sentry_api:app --host 127.0.0.1 --port 8787
   ```
2. Serve this directory:
   ```bash
   python -m http.server 8080 --directory frontend --bind 127.0.0.1
   ```
3. Open <http://127.0.0.1:8080>. If `config.js` was generated (e.g. by `start-ui.sh`) the page
   connects automatically. Otherwise the connect form appears as a fallback — paste the
   `SENTRY_API_KEY` value into the **API key** field and click **Connect**.
4. Pick a skill, optionally add a prompt / timeframe, and click **Run skill**. Leave
   **Dry run** checked to write the briefing but skip Slack/email relay + Graphiti.

### `config.js` (credentials from `.env`)

`start-ui.sh` writes `frontend/config.js` defining `window.SENTRY_CONFIG = { baseUrl, apiKey }`
from `.env`. `app.js` reads it on load, prefills the connect fields, hides the credential
inputs, and connects. The file embeds the `SENTRY_API_KEY`, so it is **gitignored and removed
on `stop-ui.sh`** — never commit it. Served only on the local `127.0.0.1` origin, this is the
same trust boundary as pasting the key by hand.

## Config

- The API allowlists the frontend origin via `SENTRY_UI_ORIGIN` (default
  `http://127.0.0.1:8080`). If you serve the page on another host/port, set that env var to
  match before starting the API, and update the API base URL field in the page.
- The page's CSP `connect-src` allows `http://127.0.0.1:8787` / `http://localhost:8787`. If
  the API runs elsewhere, update the `<meta http-equiv="Content-Security-Policy">` in
  `index.html` to include that origin.

## Security

- At runtime the API key lives only in a JavaScript variable for the page session — never in
  `localStorage`/`sessionStorage`, never logged. When loaded from `.env` it is sourced from the
  gitignored `config.js` (see above); when entered manually, refreshing the page clears it.
- Briefing content is LLM-generated markdown; it is sanitized with `DOMPurify.sanitize(...)`
  before being inserted as HTML. Status chips and all other text use `textContent`.
- The API stays bound to `127.0.0.1`. A public deployment needs a real auth review
  (`#bu-security-and-it`) — see the note in `src/sentry_api.py`.
