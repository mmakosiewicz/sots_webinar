# Source of Truth (SOT)

> ⚠️ **Security notice.** This is a single-team admin tool with no multi-user
> identity model. It ships with three auth modes: `none` (loopback only),
> HTTP Basic, and shared-token. **The app refuses to start on a non-loopback
> host unless auth is configured.** See the [Security](#security) section
> before exposing this anywhere.

A small Flask app for building a team **knowledge base** where:

- **GitHub is the database.** Every page, insight, concept, product fact, and how-to is a markdown file in a GitHub repo. Anyone with repo access can read or edit on github.com directly.
- **An LLM curates incoming content.** Paste a URL, text, or a file — the app fetches & chunks it, asks an LLM to extract structured items into four buckets (insights / concepts / product info / how-tos), runs a dedup judge against your existing cards, and shows you the proposed changes. You decide what to keep, edit, skip, or merge.
- **Postgres is only scratch space** for in-flight extractions and the dedup-judge verdict cache. Nothing about the knowledge base itself lives in the DB.

This is a stripped-down standalone version of an internal tool. The extraction prompts are written for a specific brand ("Ahrefs") — you can keep them as-is to see how they work, or rewrite the system prompt in `source_of_truth.py` (`EXTRACT_SYSTEM`) for your own brand / domain.

---

## Stack

- Flask 3 + Jinja templates (Tailwind via CDN, light theme)
- Postgres (psycopg2) — `sot_pending_updates`, `sot_dup_judgments`
- Any OpenAI-compatible chat-completion endpoint (OpenAI, OpenRouter, local proxy, …)
- GitHub Contents API for reads + writes (no git clone needed)
- `trafilatura` for URL fetching, `python-docx` + `pypdf` for file uploads

---

## Security

The original version of this app lived behind a workspace SSO layer. The
standalone version has no built-in identity model — every route is either
open, behind HTTP Basic, or behind a shared token. Mitigations baked in:

- **Default-deny on exposure.** `app.py` binds to `127.0.0.1` and refuses to
  start on a public host unless `SOT_AUTH_MODE` is set or you explicitly
  pass `SOT_ALLOW_OPEN=1`.
- **Werkzeug debug is off by default** (it's interactive RCE). Opt in with
  `FLASK_DEBUG=1` — never on a host strangers can reach.
- **SSRF guard on URL ingest.** `/api/ingest` resolves the host and rejects
  loopback / private / link-local / reserved IPs. Non-http(s) schemes are
  refused. Redirects are disabled (`allow_redirects=False`) so a 302 can't
  sneak the fetcher into an internal address.
- **Markdown output is sanitized.** The Jinja `markdown` filter renders
  through `bleach` with a small tag/attribute allowlist — no `<script>`,
  no event handlers, no `javascript:` URLs.
- **Path traversal on `<path:path>` routes is restricted.** `/howto/...`
  routes only accept paths matching `howtos/<slug>.md`.
- **Upload size is capped** (`MAX_CONTENT_LENGTH`, default 12 MB).

No CSRF protection ships in the box. If you wire this up with auth + cookies
for a multi-user setting, add Flask-WTF / CSRFProtect yourself.

## Quickstart

1. **Create a GitHub repo** that will hold the knowledge base. It can be public or private. Generate a fine-grained PAT (or classic PAT) with **Contents: read and write** on that repo.

2. **Postgres** — any Postgres 12+. Create a database.

3. **LLM endpoint** — point at OpenAI or any OpenAI-compatible proxy. The defaults assume OpenRouter slugs (`anthropic/claude-sonnet-4.5`, `anthropic/claude-haiku-4.5`); change `SOT_EXTRACT_MODEL` / `SOT_JUDGE_MODEL` in `.env` if you're hitting OpenAI directly (e.g. `gpt-4o`, `gpt-4o-mini`).

4. **Install + configure:**

   ```bash
   git clone https://github.com/mmakosiewicz/sots_webinar.git
   cd sots_webinar
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt

   cp .env.example .env
   # edit .env — fill DATABASE_URL, OPENAI_API_KEY, SOT_GITHUB_REPO, SOT_GITHUB_PAT
   ```

5. **(Optional) load the schema by hand:**

   ```bash
   psql "$DATABASE_URL" -f schema.sql
   ```

   `app.py` also calls `ensure_schema()` on startup, so this is usually unnecessary.

6. **Run:**

   ```bash
   python app.py
   # or
   flask --app app run --debug --port 5000
   ```

   Visit <http://localhost:5000/> — you'll be redirected to the dashboard at `/sot/`.

---

## How it works — a 30-second tour

The app turns "I just read something worth remembering" into a curated, dedup-aware GitHub repo of knowledge cards. Three actors are involved every cycle:

1. **You** — paste a URL, drop a file, or type a note.
2. **The curator LLM** — reads the source, extracts up to four kinds of items (insights, concepts, product info, how-tos), and proposes them as cards.
3. **The dedup judge** — an overlap-coefficient pre-filter plus a cheap LLM judge that compares every proposed card to your existing library and flags duplicates or partial overlaps.

You see every proposal before anything is written. Nothing lands in GitHub without your click.

## A typical session

### 1. Land on the dashboard (`/sot/`)

Tile counts for each bucket (insights / concepts / product info / how-tos), the five most recent cards in each, and a search box at the top. Any in-flight extraction shows up as a "Pending" card linking to its review screen.

The **Reindex** button rebuilds `index.json` by walking the GitHub repo — use this after a teammate edits files directly on github.com.

### 2. Ingest content (`/sot/ingest`)

Four input modes (tabs across the top):

- **URLs (up to 10)** — paste one per line. The default. Processed serially, ~10–20s per URL.
- **Files (up to 10)** — drag-and-drop `.md`, `.docx`, or `.pdf`. Total request body capped at 12 MB by `MAX_CONTENT_LENGTH`.
- **Single URL** — same as bulk but for a one-off.
- **Raw text** — paste an article body, study summary, anything text-shaped.

Click **Extract Facts**. The progress panel shows live status like `chunk 2/3 · checking for look-alikes…`. When the worker finishes you're routed to the review page.

Under the hood, per source: fetch → chunk (≤11 K chars each, cap 5 chunks per source) → LLM extraction → dedup pre-filter + judge. The cap on chunks is a deliberate cost ceiling; how much you actually spend per source depends entirely on which model you've set `SOT_EXTRACT_MODEL` to. The original deployment ran on Claude Sonnet 4.5 and budgeted roughly $0.05 per chunk; if you point this at `gpt-4o-mini` or similar it'll be an order of magnitude cheaper.

### 3. Review proposals (`/sot/review/<id>`)

The review screen groups candidates **by source** (URL or filename), with a per-source success/failure summary at the top — helpful when one of ten URLs failed to fetch.

Each candidate card shows:

- The proposed content (claim / concept / how-to steps), with bucket icons (🧩 concepts, 📦 products, 📄 insights, 🗂 how-tos)
- A status pill on the right:
  - ⚠️ **look-alike** — dedup judge says this is the same as an existing card (+ confidence %)
  - ⏈ **partial overlap** — related but not duplicate
  - ● **new** — no nearby card found
- For look-alikes / partial overlaps: a "Closest:" line with the existing card's heading and the judge's one-line rationale

For each candidate you pick one of:

- **Add as new** — write it as a fresh card
- **Skip** — drop it
- **Merge sources → `<existing card>`** — only shown for **insights** and **product info** when a look-alike was found. Appends the new source URL to the existing card's "Also cited:" footer instead of creating a duplicate. Concepts and how-tos don't have a merge action; they're either new or skipped.

The default selection is set by the dedup verdict: SAME defaults to skip (or merge, where available), DIFFERENT defaults to accept. You override with the radio buttons.

**Select all** / **Clear all** at the top of the page applies to every visible candidate.

Hit **Apply**. Accepted items are committed to the GitHub repo via the Contents API — one commit per file. The pending row in Postgres flips to `applied` and disappears from the queue.

### 4. Browse, search, edit

- `/sot/insights`, `/sot/concepts`, `/sot/products` — every card in one scrolling page each (one markdown file in the repo per bucket).
- `/sot/howtos` — list of how-to guides; each is its own file.
- `/sot/howto/edit/howtos/<slug>.md` — form editor for how-to guides (title, description, steps, tags). The other three buckets are append-only via review.
- `/sot/search?q=...` — substring match over `index.json` titles, summaries, categories.

### 5. Teammates editing on GitHub directly

Anyone with repo access can edit any `.md` file on github.com. After they push, hit **Reindex** on the dashboard to refresh `index.json` so the new content surfaces in search and the dashboard counts.

## When things go wrong

- **"No GitHub PAT configured"** at the top of the dashboard → `SOT_GITHUB_PAT` env var is empty or unset, or the PAT lacks `Contents: read+write` on `SOT_GITHUB_REPO`.
- **Ingestion stuck on "extracting…"** → check `OPENAI_API_KEY` and that `OPENAI_BASE_URL` is reachable. The worker is a daemon thread inside the Flask process; if Flask restarts, in-flight extractions die and their pending rows stay in `extracting` status (visible on the dashboard).
- **"refused: host resolves to non-public address"** on a URL ingest → the SSRF guard is doing its job; the app refuses to fetch loopback / private / link-local / reserved addresses. Set `SOT_HOST=127.0.0.1` with auth disabled if you're trying to fetch from a service on your own machine — but more likely, just use the right public URL.
- **413 on file upload** → you hit the 12 MB total request cap. Bump `SOT_MAX_UPLOAD_BYTES` if you really need to.
- **App refuses to start: "SOT_AUTH_MODE=none but SOT_HOST is not loopback"** → you bound to a non-loopback host without configuring auth. See the [Security](#security) section.

---

## Repo layout in GitHub (what the app writes)

```
<your-sot-repo>/
├── index.json                # auto-rebuilt by the app; lists every page
├── pages/
│   ├── insights.md           # one ### heading per insight card
│   ├── concepts.md           # one ### heading per concept card
│   └── products.md           # one ### heading per product-info card
├── howtos/
│   └── <slug>.md             # one file per how-to guide
```

Teammates can edit any of these files directly on GitHub. Hit **Reindex** in the dashboard after manual edits to rebuild `index.json`.

---

## Configuration reference

| Env var | Purpose | Required |
|---|---|---|
| `DATABASE_URL` | Postgres conninfo / URL | yes |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | no (default `https://api.openai.com/v1`) |
| `OPENAI_API_KEY` | API key for that endpoint | yes |
| `SOT_EXTRACT_MODEL` | Model for the curator LLM | no |
| `SOT_JUDGE_MODEL` | Model for the dedup judge | no |
| `SOT_GITHUB_REPO` | `owner/repo` that stores the knowledge base | yes |
| `SOT_GITHUB_PAT` | PAT with Contents:read+write on that repo | yes |
| `SOT_REFERENCE_PATH` | Optional local mirror of the index | no |
| `SOT_HOST` | Bind host (default `127.0.0.1`) | no |
| `PORT` | Bind port (default 5000) | no |
| `FLASK_DEBUG` | `1` = Werkzeug debugger on (NEVER on public hosts) | no |
| `FLASK_SECRET_KEY` | Flask session secret (random per-process if unset) | no |
| `SOT_MAX_UPLOAD_BYTES` | Max request body size (default 12 MB) | no |
| `SOT_AUTH_MODE` | `none` / `basic` / `token` (default `none`) | no |
| `SOT_AUTH_USER`, `SOT_AUTH_PASSWORD` | Basic-auth credentials | when `mode=basic` |
| `SOT_AUTH_TOKEN` | Shared token | when `mode=token` |
| `SOT_ALLOW_OPEN` | `1` = allow `mode=none` on a non-loopback host | no |

## What's intentionally simplified vs. the original

- **Search**: the original app uses hybrid Postgres FTS + OpenAI embeddings via a shared search lib. This standalone version ships with a simple substring search over titles/summaries from `index.json`. Swap in your own search if you want fancier ranking.
- **Auth**: HTTP Basic / shared-token only. No multi-user identity, no SSO. Run behind your own reverse proxy if you need richer auth.
- **No background workers**: ingestion runs in a daemon thread inside the Flask process. Fine for personal / small-team use; for higher volume, move the extraction step into a proper job queue.

---

## License

No license provided. All rights reserved by the author. Use as a reference / starting point.
