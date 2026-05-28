"""Standalone Flask entrypoint for the Source of Truth (SOT) app.

The SOT app is a GitHub-backed knowledge base for a team. Pages, insights,
concepts, product info and how-tos all live as markdown files in a GitHub
repo — GitHub IS the database. PostgreSQL is used only as scratch space for
in-flight LLM extractions and dedup judgments.

Run locally:

    cp .env.example .env
    # fill in DATABASE_URL, OPENAI_API_KEY, SOT_GITHUB_REPO, SOT_GITHUB_PAT
    pip install -r requirements.txt
    flask --app app run --debug --port 5000
"""

import os
from pathlib import Path

# Load .env if present (no hard dependency on python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from datetime import datetime, timezone

from flask import Flask, redirect, url_for

# Import the blueprint. The module looks at env vars at import time, so .env
# must be loaded first.
from source_of_truth import blueprint as sot_blueprint, ensure_schema
from auth import enforce_auth, assert_safe_to_serve_open


# Safe HTML tags + attributes for markdown rendering. Anything not on these
# lists is stripped by bleach, which neutralises stored XSS via <script>,
# event handlers, javascript: URLs, etc.
_BLEACH_TAGS = [
    "p", "br", "hr", "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
    "span", "div",
]
_BLEACH_ATTRS = {
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
    "code": ["class"],
    "pre": ["class"],
    "span": ["class"],
    "div": ["class"],
}
_BLEACH_PROTOCOLS = ["http", "https", "mailto"]


def _markdown(value):
    """Render markdown to HTML, then sanitize.

    The Python `markdown` library passes raw HTML through by default, which
    combined with `| safe` in templates is a stored-XSS vector. We render
    first, then strip everything not on the bleach allowlist.
    """
    if not value:
        return ""
    try:
        import markdown as _md
        html = _md.markdown(value, extensions=["fenced_code", "tables", "nl2br"])
    except ImportError:
        from markupsafe import escape
        return "<p>" + str(escape(value)).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    try:
        import bleach
        return bleach.clean(
            html,
            tags=_BLEACH_TAGS,
            attributes=_BLEACH_ATTRS,
            protocols=_BLEACH_PROTOCOLS,
            strip=True,
        )
    except ImportError:
        # If bleach isn't installed (shouldn't happen — it's in
        # requirements.txt) fall back to escaping everything. Better to
        # render ugly than to render an XSS.
        from markupsafe import escape
        return str(escape(html))


def _timeago(value):
    """Render a datetime/iso-string as a relative '5m ago' / '3h ago' / '2d ago'."""
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    return value.strftime("%Y-%m-%d")


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")

    secret = os.environ.get("FLASK_SECRET_KEY")
    if not secret:
        # Generate a random per-process key. Safer default than a hardcoded
        # placeholder; means sessions don't survive a restart, which is fine
        # because this app doesn't use them.
        import secrets as _secrets
        secret = _secrets.token_urlsafe(32)
    app.config["SECRET_KEY"] = secret

    # Cap upload size at 12 MB. Without this, /api/ingest/files (10 files
    # per batch) is a trivial memory-exhaustion DoS.
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("SOT_MAX_UPLOAD_BYTES", str(12 * 1024 * 1024)))

    # Authentication — see auth.py for modes.
    app.before_request(enforce_auth)

    app.jinja_env.filters["timeago"] = _timeago
    app.jinja_env.filters["markdown"] = _markdown

    app.register_blueprint(sot_blueprint, url_prefix="/sot")

    # Bootstrap Postgres tables (idempotent).
    try:
        ensure_schema()
    except Exception as e:
        app.logger.warning("ensure_schema failed (run schema.sql manually): %s", e)

    @app.route("/")
    def index():
        return redirect(url_for("source_of_truth.dashboard"))

    return app


app = create_app()


if __name__ == "__main__":
    # Defaults are deliberately conservative:
    #   - bind to loopback only (SOT_HOST overrides; assert_safe_to_serve_open
    #     refuses non-loopback unless auth is on or SOT_ALLOW_OPEN=1).
    #   - debug=False; the Werkzeug debugger gives RCE on any host that can
    #     reach the port, so it must be opt-in via FLASK_DEBUG=1.
    host = os.environ.get("SOT_HOST", "127.0.0.1")
    debug = os.environ.get("FLASK_DEBUG", "").strip() in ("1", "true", "True")
    assert_safe_to_serve_open()
    app.run(host=host, port=int(os.environ.get("PORT", "5000")), debug=debug)
