# AGENTS.md — guidance for AI agents adapting this app

If you're an LLM porting `sots_webinar` to a hosted environment (Letaido, an internal Flask host, a sandboxed PaaS, etc.), read this first. The mistakes below have all been made before.

## 1. Don't fetch arbitrary URLs with raw `requests`

`_fetch_url` uses `requests.get(...)` directly. In any hosted environment with an outbound firewall or aggressive bot blocking (e.g. Ahrefs, Cloudflare-fronted sites), this will return 403 or be blocked entirely.

If the host provides a URL-fetching skill / helper (e.g. with snapshot fallbacks), **route `_fetch_url` through that instead**. Don't trickle per-domain firewall exceptions — it won't scale to user-pasted URLs.

**Important**: `_fetch_url` also calls `_is_url_safe()` to block loopback / private / link-local addresses (SSRF guard). If you replace the fetch internals, **preserve that check** or you reopen a vulnerability that's been deliberately closed.

## 2. Check schema docs before wrapping a write API

If you replace direct `api.github.com` calls with a typed connector / SDK wrapper, **read the wrapper's `content` field docs**. Many wrappers (incl. the Letaido `github.create_or_update_file` capability) accept **plain UTF-8 text** and base64-encode internally. The raw GitHub Contents API expects base64.

Double-encoding silently succeeds — the commit lands but the file is unreadable base64 gibberish. There's no error, no exception. You only notice when a human opens the repo.

**Rule: before wrapping any write capability, dump the full args schema and read every field's description.**

## 3. Preserve method-aware 404 handling

`_api()` in `_sot_github.py` only treats `404` as "file doesn't exist, return None" **when the method is GET**. Writes (PUT/POST/DELETE) surface 404 as an error. This is correct: a 404 on a write means the repo isn't visible to the PAT (wrong scope, wrong name, or doesn't exist).

If you swap to a typed wrapper that throws on any 404, that's fine. If you write your own helper, **keep the GET vs. write distinction** — collapsing them either spams error logs on every "does this file exist yet?" check, or worse, marks failed writes as success.

## 4. PAT permissions: aggregate before generating

Minimum perms:
- **Contents: Read and write**
- **Metadata: Read** (auto-added)

A "Contents: Read-only" PAT makes every write return a 403 mis-labeled as `rate_limited: Resource not accessible by personal access token` — yes, GitHub mis-labels this; it is NOT a rate limit. Fix the perm, don't wait.

## 5. Background daemon threads = silent failures

`append_insights`, `append_concepts`, `append_products`, `save_howto`, `delete_howto_file` all spawn `threading.Thread(daemon=True)` and `print(...)` errors. The HTTP route returns 200 the moment the thread is dispatched — the user sees "applied" while the actual commit may not have run yet (or has failed).

Worse: **daemon threads die when Flask reloads.** Editing app code in dev kills any in-flight write thread silently, and the pending row stays in `extracting`.

For non-trivial use: make at least one write synchronous, surface tail of prints in the dashboard, or track per-commit status in `sot_pending_updates` (extra column) and poll.

## 6. Database choice

Hosted envs with shared DBs may run the app as a non-owner role. `CREATE INDEX IF NOT EXISTS` against a table you don't own raises `must be owner of table`. Either create tables under the same role the app runs as, or wrap `ensure_schema()` in try/except.

## 7. Template namespacing

`templates/base.html` is a generic name. If mounted inside a host app that also has its own `base.html`, child templates resolve to the wrong one. Rename to `sot_base.html` and update `extends`.

## 8. Jinja filters

Templates use `| markdown` and `| timeago`. Upstream `app.py` registers both. If you reuse only the blueprint, register them on the host via `blueprint.record_once(...)`, and check the host doesn't have an incompatible `timeago` (some expect unix-float, not datetime).

**The `markdown` filter is bleach-sanitized** with a small allowlist (see `_markdown()` in `app.py`). If you replace it with the upstream `markdown` library directly, you reopen a stored-XSS hole: anything a user / LLM puts in a card will render as HTML.

## 9. Env vars (set BEFORE importing `source_of_truth.py`)

The module reads several env vars at import time. Set them in the process environment before any `import source_of_truth` happens.

- `SOT_GITHUB_REPO` — `owner/repo`
- `SOT_GITHUB_PAT` (or secret-store reference) — Contents read+write
- `SOT_BRAND_NAME` — optional, focuses the extraction prompt on one brand's products
- `DATABASE_URL` — Postgres conninfo
- `OPENAI_BASE_URL` / `OPENAI_API_KEY` — provider or proxy
- `SOT_EXTRACT_MODEL` — heavy model
- `SOT_JUDGE_MODEL` — cheap model

## 10. JSON response_format

`_extract_one_chunk` requests `response_format={"type":"json_object"}`. Not every model honours it — Claude Sonnet 4.5 returns markdown-fenced JSON. `_parse_extraction_json` already strips fences, but be aware on model swap.

## 11. Security commitments (don't strip these on port)

This version closes several issues that an earlier internal version had. Each one is a small surface a porter can easily undo:

- **`_is_url_safe()`** — called from `_fetch_url` before any HTTP request. Blocks `127.0.0.1`, private RFC1918 ranges, link-local, AWS-metadata IP. Uses `allow_redirects=False` so a 302 can't sneak past it. **If you swap fetch implementations, run the new URL through `_is_url_safe()` first.**
- **`_validate_howto_path()`** — regex-validates the `<path:filepath>` route variable on how-to edit / delete endpoints to `^howtos/<slug>\.md$`. Without this, a path-traversal can write or delete arbitrary repo files.
- **Bleach-sanitized `_markdown()` filter** — see item 8.
- **`auth.py` startup guard** — refuses to start if `SOT_HOST` is non-loopback and `SOT_AUTH_MODE=none` (unless `SOT_ALLOW_OPEN=1` is explicitly set). If you mount the blueprint inside an already-authed host app, you can drop `auth.py` — but know it's there.
- **`MAX_CONTENT_LENGTH = 12 MB`** — set in `app.py`. Without it, uploads can DoS the process.
- **Per-process random `SECRET_KEY`** — `FLASK_SECRET_KEY` env var if you want one stable across restarts; default is random.

## 12. Judge-cost scales with library size

The dedup judge runs against the top-K (default 3) candidates per proposed card, picked by overlap-coefficient pre-filter. With 100 cards in the library, that's fine. With 5000 cards across all buckets, every ingest sends ~3×(items_proposed) calls to the judge LLM.

Two knobs:
- `SOT_JUDGE_MODEL` — keep this on a cheap model (Haiku-class). The pre-filter is what's expensive to get wrong, not the per-call cost of the judge.
- Tune the overlap threshold in `_sot_dedup.py` (currently `0.40`). Higher → fewer judge calls, more accept-as-new false positives. Lower → more judge calls, cleaner library.

If you're running a large library: **measure the judge call count per ingest before deploying** (log it from `find_lookalikes`). Catching this in dev is cheap; learning about it from a $40 bill is not.

---

**Bottom line:** every silent-failure mode here has bitten an agent before. If something looks like it works but the user can't see the result, suspect this file's items in order.
