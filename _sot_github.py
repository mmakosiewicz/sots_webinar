"""GitHub-backed storage for Source of Truth. GitHub IS the database."""

import base64
import json
import re
import urllib.request
import urllib.error
import os
import threading

# Configure via environment:
#   SOT_GITHUB_REPO   e.g. "yourname/your-sot-repo"
#   SOT_GITHUB_PAT    fine-grained PAT with Contents read/write on that repo
REPO = os.environ.get("SOT_GITHUB_REPO", "")
API_BASE = f"https://api.github.com/repos/{REPO}/contents" if REPO else ""
REFERENCE_PATH = os.environ.get("SOT_REFERENCE_PATH", "")  # optional local mirror

_lock = threading.Lock()


def _get_pat():
    pat = os.environ.get("SOT_GITHUB_PAT", "").strip()
    return pat or None


def _api(method, path, data=None):
    pat = _get_pat()
    if not pat:
        return None, "No GitHub PAT configured"
    url = f"{API_BASE}/{path}" if path else API_BASE.rsplit("/contents", 1)[0]
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "SourceOfTruth-Agent",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        if e.code == 404 and method == "GET":
            return None, None
        return None, f"GitHub {e.code}: {err_body[:300]}"
    except Exception as e:
        return None, str(e)


def _get_sha(path):
    data, _ = _api("GET", path)
    return data["sha"] if data and "sha" in data else None


def _read_file(path):
    """Read a file's content from GitHub. Returns (content_string, sha) or (None, None)."""
    data, err = _api("GET", path)
    if not data or "content" not in data:
        return None, None
    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return content, data.get("sha")


def _write_file(path, content, message):
    sha = _get_sha(path)
    encoded = base64.b64encode(content.encode()).decode()
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    data, err = _api("PUT", path, payload)
    return err is None, err


def _delete_file(path, message):
    sha = _get_sha(path)
    if not sha:
        return True, None
    data, err = _api("DELETE", path, {"message": message, "sha": sha})
    return err is None, err


def _slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower().strip()).strip('-')[:80]


# ── Index management ───────────────────────────────────────────

def _build_index_entry(path, title, category, entry_type, summary=""):
    return {
        "path": path,
        "title": title,
        "category": category,
        "type": entry_type,  # "page" or "howto"
        "summary": summary[:200] if summary else "",
    }


def _read_index():
    content, sha = _read_file("index.json")
    if content:
        try:
            return json.loads(content), sha
        except json.JSONDecodeError:
            pass
    return [], None


def _write_index(index, message="Update index"):
    content = json.dumps(index, indent=2)
    _write_file("index.json", content, message)
    # Optionally regenerate a local reference mirror if a path is configured
    if REFERENCE_PATH:
        _regenerate_reference(index)


def _update_index(path, title, category, entry_type, summary=""):
    """Add or update an entry in index.json."""
    index, _ = _read_index()
    entry = _build_index_entry(path, title, category, entry_type, summary)
    # Replace existing or append
    index = [e for e in index if e["path"] != path]
    index.append(entry)
    index.sort(key=lambda e: (e["type"], e.get("category", ""), e["title"]))
    _write_index(index, f"index: update {title}")


def _remove_from_index(path):
    """Remove an entry from index.json."""
    index, _ = _read_index()
    index = [e for e in index if e["path"] != path]
    _write_index(index, f"index: remove {path}")


def _regenerate_reference(index):
    """Rebuild ~/workspace/sot_reference.md from the index. Buckets: insights + concepts + products + howtos."""
    try:
        insights_entry = next((e for e in index if e.get("type") == "insights"), None)
        concepts_entry = next((e for e in index if e.get("type") == "concepts"), None)
        products_entry = next((e for e in index if e.get("type") == "products"), None)
        howtos = [e for e in index if e.get("type") == "howto"]

        lines = ["# Source of Truth — Quick Reference", ""]
        lines.append(
            f"*{insights_entry['count'] if insights_entry else 0} insights, "
            f"{concepts_entry['count'] if concepts_entry else 0} concepts, "
            f"{products_entry['count'] if products_entry else 0} product facts, "
            f"{len(howtos)} how-tos*"
        )
        lines.append(f"*GitHub: github.com/{REPO}*\n")

        if insights_entry:
            lines.append("\n## Insights & Stats\n")
            lines.append(f"File: `{insights_entry['path']}`")
            lines.append(f"{insights_entry['count']} quotable facts.\n")

        if concepts_entry:
            lines.append("\n## Concepts & Mechanisms\n")
            lines.append(f"File: `{concepts_entry['path']}`")
            lines.append(f"{concepts_entry['count']} explainers.\n")

        if products_entry:
            lines.append("\n## Product Info\n")
            lines.append(f"File: `{products_entry['path']}`")
            lines.append(f"{products_entry['count']} product facts.\n")

        if howtos:
            lines.append("\n## Ahrefs How-To Guides\n")
            for h in howtos:
                lines.append(f"### {h['title']}")
                lines.append(f"File: `{h['path']}`")
                if h.get("summary"):
                    lines.append(h["summary"])
                lines.append("")

        lines.append("\n---")
        lines.append("*To read full content, fetch the file from GitHub.*")

        with open(REFERENCE_PATH, "w") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"[sot] Error regenerating reference: {e}")


# ── Public API ──────────────────────────────────────────────────

INSIGHTS_PATH = "pages/insights.md"
INSIGHTS_HEADER = "# Insights & Stats\n\n> Rolling list of quotable facts from Ahrefs studies, blog, and supporting external research. Each entry = one specific claim with source + date.\n\n---\n"


def _format_insight_card(card):
    """Render one insight dict as a markdown card."""
    lines = []
    lines.append(f"### {card.get('claim','').strip()}")
    if card.get("number"):
        lines.append(f"- **Number:** {card['number']}")
    if card.get("source_url"):
        src_label = card.get("source_title") or card["source_url"]
        lines.append(f"- **Source:** [{src_label}]({card['source_url']})")
    elif card.get("source_title"):
        lines.append(f"- **Source:** {card['source_title']}")
    if card.get("date"):
        lines.append(f"- **Date:** {card['date']}")
    if card.get("topic_tag"):
        lines.append(f"- **Topic tag:** {card['topic_tag']}")
    if card.get("context"):
        lines.append(f"- **Context:** {card['context']}")
    lines.append("")
    return "\n".join(lines)


def _count_insight_cards(md):
    return md.count("\n### ") + (1 if md.lstrip().startswith("### ") else 0)


def _update_insights_index(count):
    index, _ = _read_index()
    index = [e for e in index if e.get("type") != "insights"]
    index.append({
        "path": INSIGHTS_PATH,
        "title": "Insights & Stats",
        "category": "Insights",
        "type": "insights",
        "count": count,
        "summary": f"{count} quotable facts.",
    })
    index.sort(key=lambda e: (e["type"], e.get("category", ""), e["title"]))
    _write_index(index, f"index: insights count = {count}")


def append_insights(cards):
    """Append insight cards to the single insights.md page. Returns (path, count_added).
    cards: list of dicts with keys claim, number, source_url, source_title, date, topic_tag, context.
    Runs in a background thread (matches the rest of the public API)."""
    if not cards:
        return INSIGHTS_PATH, 0

    def _do():
        with _lock:
            current, _ = _read_file(INSIGHTS_PATH)
            if not current:
                current = INSIGHTS_HEADER
            new_md = current.rstrip() + "\n\n" + "\n".join(_format_insight_card(c) for c in cards)
            ok, err = _write_file(INSIGHTS_PATH, new_md, f"[insights] +{len(cards)} card{'s' if len(cards)!=1 else ''}")
            if err:
                print(f"[sot] Error appending insights: {err}")
                return
            total = _count_insight_cards(new_md)
            _update_insights_index(total)
            print(f"[sot] Appended {len(cards)} insight(s); total {total}")

    threading.Thread(target=_do, daemon=True).start()
    return INSIGHTS_PATH, len(cards)


def read_insights_raw():
    """Return raw markdown of insights.md (or empty header)."""
    content, _ = _read_file(INSIGHTS_PATH)
    return content or INSIGHTS_HEADER


# ── Concepts & Mechanisms (third bucket) ─────────────────────────
CONCEPTS_PATH = "pages/concepts.md"
CONCEPTS_HEADER = "# Concepts & Mechanisms\n\n> Rolling list of 'how X works' explainers relevant to Ahrefs use cases (AI search, ranking signals, crawling, indexing, etc.). Each entry = one concept with a plain-English summary + key mechanism/implication bullets.\n\n---\n"


def _format_concept_card(card):
    """Render one concept dict as a markdown card."""
    lines = []
    lines.append(f"### {card.get('concept','').strip()}")
    if card.get("summary"):
        lines.append(f"- **Summary:** {card['summary']}")
    key_points = card.get("key_points") or []
    if key_points:
        lines.append("- **Key points:**")
        for kp in key_points:
            lines.append(f"  - {kp}")
    if card.get("source_url"):
        src_label = card.get("source_title") or card["source_url"]
        lines.append(f"- **Source:** [{src_label}]({card['source_url']})")
    elif card.get("source_title"):
        lines.append(f"- **Source:** {card['source_title']}")
    if card.get("date"):
        lines.append(f"- **Date:** {card['date']}")
    if card.get("topic_tag"):
        lines.append(f"- **Topic tag:** {card['topic_tag']}")
    lines.append("")
    return "\n".join(lines)


def _count_concept_cards(md):
    return md.count("\n### ") + (1 if md.lstrip().startswith("### ") else 0)


def _update_concepts_index(count):
    index, _ = _read_index()
    index = [e for e in index if e.get("type") != "concepts"]
    index.append({
        "path": CONCEPTS_PATH,
        "title": "Concepts & Mechanisms",
        "category": "Concepts",
        "type": "concepts",
        "count": count,
        "summary": f"{count} explainers.",
    })
    index.sort(key=lambda e: (e["type"], e.get("category", ""), e["title"]))
    _write_index(index, f"index: concepts count = {count}")


def append_concepts(cards):
    """Append concept cards to the single concepts.md page. Returns (path, count_added)."""
    if not cards:
        return CONCEPTS_PATH, 0

    def _do():
        with _lock:
            current, _ = _read_file(CONCEPTS_PATH)
            if not current:
                current = CONCEPTS_HEADER
            new_md = current.rstrip() + "\n\n" + "\n".join(_format_concept_card(c) for c in cards)
            ok, err = _write_file(CONCEPTS_PATH, new_md, f"[concepts] +{len(cards)} card{'s' if len(cards)!=1 else ''}")
            if err:
                print(f"[sot] Error appending concepts: {err}")
                return
            total = _count_concept_cards(new_md)
            _update_concepts_index(total)
            print(f"[sot] Appended {len(cards)} concept(s); total {total}")

    threading.Thread(target=_do, daemon=True).start()
    return CONCEPTS_PATH, len(cards)


def read_concepts_raw():
    """Return raw markdown of concepts.md (or empty header)."""
    content, _ = _read_file(CONCEPTS_PATH)
    return content or CONCEPTS_HEADER


# ── Product Info (fourth bucket) ──────────────────────────────────────
PRODUCTS_PATH = "pages/products.md"
PRODUCTS_HEADER = "# Product Info\n\n> Rolling list of facts about Ahrefs products (Site Explorer, Keywords Explorer, Site Audit, Rank Tracker, Brand Radar, AI Content Helper, Content Explorer, Web Analytics, Agent A, etc.). Each entry = one specific product fact: a capability, limit, pricing detail, integration, or behaviour.\n\n---\n"


def _format_product_card(card):
    """Render one product dict as a markdown card."""
    lines = []
    lines.append(f"### {card.get('claim','').strip()}")
    if card.get("product"):
        lines.append(f"- **Product:** {card['product']}")
    if card.get("fact_type"):
        lines.append(f"- **Type:** {card['fact_type']}")
    if card.get("detail"):
        lines.append(f"- **Detail:** {card['detail']}")
    if card.get("source_url"):
        src_label = card.get("source_title") or card["source_url"]
        lines.append(f"- **Source:** [{src_label}]({card['source_url']})")
    elif card.get("source_title"):
        lines.append(f"- **Source:** {card['source_title']}")
    if card.get("date"):
        lines.append(f"- **Date:** {card['date']}")
    if card.get("topic_tag"):
        lines.append(f"- **Topic tag:** {card['topic_tag']}")
    lines.append("")
    return "\n".join(lines)


def _count_product_cards(md):
    return md.count("\n### ") + (1 if md.lstrip().startswith("### ") else 0)


def _update_products_index(count):
    index, _ = _read_index()
    index = [e for e in index if e.get("type") != "products"]
    index.append({
        "path": PRODUCTS_PATH,
        "title": "Product Info",
        "category": "Product Info",
        "type": "products",
        "count": count,
        "summary": f"{count} product facts.",
    })
    index.sort(key=lambda e: (e["type"], e.get("category", ""), e["title"]))
    _write_index(index, f"index: products count = {count}")


def append_products(cards):
    """Append product-info cards to pages/products.md. Returns (path, count_added).
    cards: list of dicts with keys claim, product, fact_type, detail,
           source_url, source_title, date, topic_tag."""
    if not cards:
        return PRODUCTS_PATH, 0

    def _do():
        with _lock:
            current, _ = _read_file(PRODUCTS_PATH)
            if not current:
                current = PRODUCTS_HEADER
            new_md = current.rstrip() + "\n\n" + "\n".join(_format_product_card(c) for c in cards)
            ok, err = _write_file(PRODUCTS_PATH, new_md, f"[products] +{len(cards)} card{'s' if len(cards)!=1 else ''}")
            if err:
                print(f"[sot] Error appending products: {err}")
                return
            total = _count_product_cards(new_md)
            _update_products_index(total)
            print(f"[sot] Appended {len(cards)} product fact(s); total {total}")

    threading.Thread(target=_do, daemon=True).start()
    return PRODUCTS_PATH, len(cards)


def read_products_raw():
    """Return raw markdown of products.md (or empty header)."""
    content, _ = _read_file(PRODUCTS_PATH)
    return content or PRODUCTS_HEADER


def save_howto(title, description, prerequisites, steps, tags):
    """Save a how-to guide to GitHub and update index."""
    slug = _slugify(title)
    path = f"howtos/{slug}.md"

    md = f"# {title}\n\n"
    if description:
        md += f"{description}\n\n"
    if prerequisites:
        md += f"## Prerequisites\n\n{prerequisites}\n\n"
    if tags:
        md += f"**Tags:** {', '.join(tags)}\n\n"
    md += "## Steps\n\n"
    for i, step in enumerate(steps, 1):
        if isinstance(step, dict):
            md += f"{i}. **{step.get('title', '')}**"
            if step.get('description'):
                md += f"\n   {step['description']}"
            md += "\n"
        else:
            md += f"{i}. {step}\n"

    def _do():
        with _lock:
            ok, err = _write_file(path, md, f"[howto] {title}")
            if err:
                print(f"[sot] Error saving howto: {err}")
                return
            _update_index(path, title, "How-Tos", "howto", description or "")
            print(f"[sot] Saved howto: {path}")

    threading.Thread(target=_do, daemon=True).start()
    return path, slug


def delete_howto_file(title):
    slug = _slugify(title)
    path = f"howtos/{slug}.md"

    def _do():
        with _lock:
            _delete_file(path, f"[howto] Delete: {title}")
            _remove_from_index(path)
            print(f"[sot] Deleted howto: {path}")

    threading.Thread(target=_do, daemon=True).start()


def read_page(path):
    """Read a file from GitHub. Returns content string or None."""
    content, _ = _read_file(path)
    return content


def list_pages():
    """Get the full index."""
    index, _ = _read_index()
    return index


def get_tree():
    """Get full repo file tree."""
    pat = _get_pat()
    if not pat:
        return []
    url = f"https://api.github.com/repos/{REPO}/git/trees/main?recursive=1"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "SourceOfTruth-Agent",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            return [f["path"] for f in data.get("tree", []) if f["type"] == "blob"]
    except Exception:
        return []


def is_configured():
    return _get_pat() is not None


def rebuild_index_from_repo():
    """Scan all files on GitHub and rebuild index.json from scratch.
    Two buckets: insights (single rolling page) + howtos."""
    files = get_tree()
    index = []
    for f in files:
        if f == INSIGHTS_PATH:
            content = read_page(f) or ""
            count = _count_insight_cards(content)
            index.append({
                "path": INSIGHTS_PATH,
                "title": "Insights & Stats",
                "category": "Insights",
                "type": "insights",
                "count": count,
                "summary": f"{count} quotable facts.",
            })

        elif f.startswith("howtos/") and f.endswith(".md"):
            content = read_page(f)
            if not content:
                continue
            title = f.split("/")[-1].replace(".md", "").replace("-", " ").title()
            desc = ""
            for line in content.split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()
                elif line and not line.startswith("#") and not line.startswith("**") and not line.startswith(">"):
                    desc = line.strip()[:200]
                    break
            index.append(_build_index_entry(f, title, "How-Tos", "howto", desc))

    index.sort(key=lambda e: (e["type"], e.get("category", ""), e["title"]))
    _write_index(index, "index: full rebuild from repo")
    return index
