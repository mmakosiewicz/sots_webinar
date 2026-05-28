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

## How a typical ingest flow looks

1. Open `/sot/ingest` and paste a URL (or text, or upload a `.md` / `.docx` / `.pdf`).
2. The app fetches + chunks the content and asks the LLM to extract candidate cards.
3. For each candidate, a dedup pre-filter (overlap coefficient) shortlists similar existing cards, and a cheap LLM judges SAME / PARTIAL / DIFFERENT.
4. You see a review page with all candidates grouped by bucket. For each, choose to accept, skip, edit, or merge into an existing card.
5. The accepted items are committed to GitHub via the Contents API.

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
