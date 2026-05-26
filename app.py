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


def _markdown(value):
    """Render markdown to HTML using `markdown` if available; else escape only."""
    if not value:
        return ""
    try:
        import markdown as _md
        return _md.markdown(value, extensions=["fenced_code", "tables", "nl2br"])
    except ImportError:
        from markupsafe import escape
        return "<p>" + str(escape(value)).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


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
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
