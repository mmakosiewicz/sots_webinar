"""Duplicate / look-alike detection for Source of Truth.

Two-stage:
  1. Jaccard pre-filter on normalized tokens (free, fast) — shortlists candidates.
  2. Claude Haiku judge per shortlisted pair — returns same / different / partial.

Caches judgments in Postgres so we don't re-pay for the same comparison.
"""

import hashlib
import json
import os
import re
import urllib.request
import urllib.error
import psycopg2
import psycopg2.extras

import os

import _sot_github as gh

# ── Config ─────────────────────────────────────────────────────────
# OpenAI-compatible endpoint. Defaults to OpenAI proper; override to point at
# an OpenRouter/local proxy (e.g. http://127.0.0.1:18080/api/v1).
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_URL = f"{LLM_BASE_URL}/chat/completions"
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL = os.environ.get("SOT_JUDGE_MODEL", "anthropic/claude-haiku-4.5")
# We use the *overlap coefficient* (|A∩B| / min(|A|,|B|)) rather than Jaccard,
# because existing cards have much larger token sets (heading + body) than
# new proposed items (heading + summary only), which deflates Jaccard.
OVERLAP_THRESHOLD = 0.40   # shortlist anything at/above this
TOP_K_PER_ITEM = 3         # at most N candidates passed to the judge
SAME_CONFIDENCE_MIN = 0.55 # below this, treat as "different" even if model says same

_STOPWORDS = set("""
a an and as at be by for from how in is it its of on or that the their this to with
not no into out about which what who when where why use using used you your we our
""".split())


# ── DB helpers ─────────────────────────────────────────────────────

def _get_db():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set. See .env.example.")
    return psycopg2.connect(dsn)


def ensure_schema():
    """Idempotent table for cached judgments."""
    with _get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sot_dup_judgments (
                pair_hash       TEXT PRIMARY KEY,
                bucket          TEXT NOT NULL,
                verdict         TEXT NOT NULL,
                confidence      REAL NOT NULL,
                rationale       TEXT,
                left_summary    TEXT,
                right_summary   TEXT,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)


# ── Tokenization & Jaccard ─────────────────────────────────────────

def _tokens(text):
    if not text:
        return set()
    text = text.lower()
    text = re.sub(r"[^a-z0-9%\s]", " ", text)
    toks = [t for t in text.split() if len(t) > 2 and t not in _STOPWORDS]
    return set(toks)


def _overlap(a, b):
    """Overlap coefficient: |A∩B| / min(|A|, |B|). Robust to size mismatch."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / min(len(a), len(b))


def _signature(item, bucket):
    """Build a token set representing the item's claim/concept/title PLUS key fields."""
    if bucket == "insights":
        parts = [item.get("claim", ""), str(item.get("number", "")), item.get("context", "")]
    elif bucket == "concepts":
        parts = [item.get("concept", ""), item.get("summary", "")]
        for kp in (item.get("key_points") or [])[:3]:
            parts.append(kp)
    elif bucket == "products":
        parts = [item.get("product", ""), item.get("claim", ""), item.get("detail", "")]
    else:  # howtos
        parts = [item.get("title", "")]
        for s in (item.get("steps") or [])[:2]:
            if isinstance(s, str):
                parts.append(s)
            else:
                parts.append(s.get("text") or s.get("title") or "")
    return _tokens(" ".join(parts))


# ── Existing-card extraction ───────────────────────────────────────

def _card_id_from_heading(heading_text):
    """Stable short id derived from heading text (lowercase, no punctuation)."""
    norm = re.sub(r"[^a-z0-9]+", "-", heading_text.lower()).strip("-")
    return norm[:60] or "card"


def _split_cards_markdown(raw):
    """Yield (card_id, heading, body_markdown) from a page that uses ### per card."""
    if not raw:
        return
    parts = raw.split("\n### ")
    # First part may be header before any card; drop unless it begins with ###
    body = parts[0]
    if body.lstrip().startswith("### "):
        body = "### " + body.lstrip()[4:]
        head = body.split("\n", 1)[0].strip().lstrip("# ").strip()
        rest = body.split("\n", 1)[1] if "\n" in body else ""
        yield _card_id_from_heading(head), head, rest
    for p in parts[1:]:
        head = p.split("\n", 1)[0].strip()
        rest = p.split("\n", 1)[1] if "\n" in p else ""
        yield _card_id_from_heading(head), head, rest


def load_existing(bucket):
    """Return list of dicts {card_id, heading, body, signature} for one bucket."""
    if bucket == "insights":
        raw = gh.read_insights_raw()
    elif bucket == "concepts":
        raw = gh.read_concepts_raw()
    elif bucket == "products":
        raw = gh.read_products_raw()
    elif bucket == "howtos":
        # Each howto is its own file. Load index + lazy-pull only the headings.
        index = gh.list_pages()
        out = []
        for entry in index:
            if entry.get("type") != "howto":
                continue
            title = entry.get("title", "")
            summary = entry.get("summary", "") or ""
            cid = _card_id_from_heading(title)
            sig = _tokens(title + " " + summary)
            out.append({
                "card_id": cid,
                "heading": title,
                "body": summary,
                "path": entry.get("path"),
                "signature": sig,
            })
        return out
    else:
        return []

    out = []
    for cid, head, body in _split_cards_markdown(raw):
        sig = _tokens(head + " " + body)
        out.append({"card_id": cid, "heading": head, "body": body, "signature": sig})
    return out


# ── LLM judge ──────────────────────────────────────────────────────

def _pair_hash(bucket, left_text, right_text):
    """Stable hash of (bucket, sorted pair) so order doesn't change the key."""
    a, b = sorted([left_text.strip(), right_text.strip()])
    return hashlib.sha256(f"{bucket}\n{a}\n{b}".encode()).hexdigest()[:32]


def _item_to_judge_text(item, bucket):
    if bucket == "insights":
        bits = [f"Claim: {item.get('claim','')}"]
        if item.get("number"):
            bits.append(f"Number: {item['number']}")
        if item.get("context"):
            bits.append(f"Context: {item['context']}")
        return " | ".join(bits)
    if bucket == "concepts":
        bits = [f"Concept: {item.get('concept','')}"]
        if item.get("summary"):
            bits.append(f"Summary: {item['summary']}")
        kps = item.get("key_points") or []
        if kps:
            bits.append("Key points: " + " · ".join(kps[:3]))
        return " | ".join(bits)
    if bucket == "products":
        bits = [f"Product: {item.get('product','')}"]
        if item.get("claim"):
            bits.append(f"Claim: {item['claim']}")
        if item.get("fact_type"):
            bits.append(f"Type: {item['fact_type']}")
        if item.get("detail"):
            bits.append(f"Detail: {item['detail']}")
        return " | ".join(bits)
    # howtos
    bits = [f"Title: {item.get('title','')}"]
    steps = item.get("steps") or []
    if steps:
        s0_raw = steps[0]
        if isinstance(s0_raw, str):
            s0 = s0_raw
        else:
            s0 = s0_raw.get("text") or s0_raw.get("title") or ""
        bits.append(f"Step 1: {s0}")
    return " | ".join(bits)


JUDGE_SYSTEM = """You compare two pieces of content from a knowledge base. Your single job: are they expressing the SAME underlying fact / concept / how-to, just rephrased, or are they DIFFERENT?

Rules:
- SAME = same underlying claim/mechanism/procedure, even if wording differs or numbers are rounded differently (e.g. "12%" vs "around one in ten").
- DIFFERENT = different fact, different mechanism, or same topic but distinct sub-claim (e.g. "RAG reduces hallucinations" vs "RAG enables fresh answers" → different).
- PARTIAL = significant overlap but one says something extra the other doesn't.

Output STRICT JSON:
{
  "verdict": "same" | "different" | "partial",
  "confidence": 0.0-1.0,
  "rationale": "one short sentence"
}
"""


def _judge_call(bucket, left_text, right_text):
    """Make one judge LLM call; returns dict or None on error."""
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": f"BUCKET: {bucket}\n\nA: {left_text}\n\nB: {right_text}\n\nRespond with the JSON only."},
        ],
        "temperature": 0,
        "max_tokens": 200,
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    req = urllib.request.Request(
        LLM_URL,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        msg = data["choices"][0]["message"]["content"]
        # Strip code fences if any
        msg = re.sub(r"^```(?:json)?\s*", "", msg.strip())
        msg = re.sub(r"\s*```$", "", msg)
        parsed = json.loads(msg)
        return {
            "verdict": parsed.get("verdict", "different"),
            "confidence": float(parsed.get("confidence", 0.5)),
            "rationale": parsed.get("rationale", "")[:300],
        }
    except Exception as e:
        print(f"[sot dedup] Judge call failed: {e}")
        return None


def judge_pair(bucket, left_text, right_text):
    """Cached single-pair judgment."""
    ph = _pair_hash(bucket, left_text, right_text)
    with _get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT verdict, confidence, rationale FROM sot_dup_judgments WHERE pair_hash = %s", (ph,))
        row = cur.fetchone()
        if row:
            return dict(row)

        result = _judge_call(bucket, left_text, right_text)
        if not result:
            return None

        cur.execute("""
            INSERT INTO sot_dup_judgments
              (pair_hash, bucket, verdict, confidence, rationale, left_summary, right_summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (pair_hash) DO NOTHING
        """, (ph, bucket, result["verdict"], result["confidence"], result["rationale"],
              left_text[:500], right_text[:500]))
        return result


# ── Main entry ─────────────────────────────────────────────────────

def find_lookalikes(bucket, proposed_items, existing=None):
    """For each proposed item, return list of look-alike judgments.

    Returns list aligned by index with proposed_items.
    Each entry is either None (fully new) or:
      {
        "candidates": [{card_id, heading, verdict, confidence, rationale, path?}, ...],
        "default_action": "accept_new" | "skip" | "merge:<card_id>",
        "best_match": {card_id, heading, ...},
      }
    """
    ensure_schema()
    if existing is None:
        existing = load_existing(bucket)
    if not existing:
        return [None] * len(proposed_items)

    out = []
    for item in proposed_items:
        sig = _signature(item, bucket)
        if not sig:
            out.append(None)
            continue

        # Overlap-coefficient shortlist
        scored = []
        for ex in existing:
            j = _overlap(sig, ex["signature"])
            if j >= OVERLAP_THRESHOLD:
                scored.append((j, ex))
        scored.sort(key=lambda x: x[0], reverse=True)
        shortlist = scored[:TOP_K_PER_ITEM]

        if not shortlist:
            out.append(None)
            continue

        # Judge each
        cand_text = _item_to_judge_text(item, bucket)
        candidates = []
        for jscore, ex in shortlist:
            ex_text = f"{ex['heading']}" + (f"\n{ex['body'][:400]}" if ex.get("body") else "")
            verdict = judge_pair(bucket, cand_text, ex_text)
            if not verdict:
                continue
            candidates.append({
                "card_id": ex["card_id"],
                "heading": ex["heading"],
                "path": ex.get("path"),
                "overlap": round(jscore, 3),
                "verdict": verdict["verdict"],
                "confidence": verdict["confidence"],
                "rationale": verdict["rationale"],
            })

        if not candidates:
            out.append(None)
            continue

        # Best match = highest confidence among "same" verdicts; else None
        same = [c for c in candidates if c["verdict"] == "same" and c["confidence"] >= SAME_CONFIDENCE_MIN]
        partial = [c for c in candidates if c["verdict"] == "partial" and c["confidence"] >= SAME_CONFIDENCE_MIN]

        best = None
        if same:
            best = max(same, key=lambda c: c["confidence"])
        elif partial:
            best = max(partial, key=lambda c: c["confidence"])

        if not best:
            out.append({"candidates": candidates, "default_action": "accept_new", "best_match": None})
            continue

        # Decide default per bucket
        if bucket in ("insights", "products") and best["verdict"] == "same":
            # For products we treat them like insights — same fact, different source = merge.
            default = f"merge:{best['card_id']}"
        elif best["verdict"] == "same":
            default = "skip"
        else:
            # partial — accept by default but flag
            default = "accept_new"

        out.append({
            "candidates": candidates,
            "default_action": default,
            "best_match": best,
        })

    return out


# ── Merge helper for insights ──────────────────────────────────────

def merge_source_into_insight(target_card_id, new_source_url, new_source_title=None):
    """Append a new source URL to an existing insight card's 'Also cited:' line.
    Returns (ok, error)."""
    raw = gh.read_insights_raw()
    cards = list(_split_cards_markdown(raw))
    target = None
    for i, (cid, head, body) in enumerate(cards):
        if cid == target_card_id:
            target = i
            break
    if target is None:
        return False, "Target card not found"

    cid, head, body = cards[target]

    # Build new body: append a line under 'Also cited:' bullet, or create one
    line = f"  - [{new_source_title or new_source_url}]({new_source_url})" if new_source_url.startswith("http") else f"  - {new_source_url}"
    if "**Also cited:**" in body:
        # insert under the Also cited bullet
        new_body = re.sub(
            r"(- \*\*Also cited:\*\*[^\n]*\n(?:  - [^\n]*\n)*)",
            lambda m: m.group(1) + line + "\n",
            body,
            count=1,
        )
    else:
        # Insert before trailing blank lines
        new_body = body.rstrip() + f"\n- **Also cited:**\n{line}\n"

    cards[target] = (cid, head, new_body)

    # Reassemble
    header = gh.INSIGHTS_HEADER.rstrip() + "\n"
    out = header
    for _, h, b in cards:
        out += f"\n### {h}\n{b.rstrip()}\n"

    # Write back via low-level _write_file (atomic per file, _lock-protected by caller chain)
    from _sot_github import _write_file, _lock
    with _lock:
        ok, err = _write_file(gh.INSIGHTS_PATH, out, f"[insights] merge source into {target_card_id}")
    return (ok is not None), err


# ── Merge helper for products ──────────────────────────────────────

def merge_source_into_product(target_card_id, new_source_url, new_source_title=None):
    """Append a new source URL to an existing product card's 'Also cited:' line.
    Returns (ok, error)."""
    raw = gh.read_products_raw()
    cards = list(_split_cards_markdown(raw))
    target = None
    for i, (cid, head, body) in enumerate(cards):
        if cid == target_card_id:
            target = i
            break
    if target is None:
        return False, "Target card not found"

    cid, head, body = cards[target]
    line = (f"  - [{new_source_title or new_source_url}]({new_source_url})"
            if new_source_url.startswith("http")
            else f"  - {new_source_url}")
    if "**Also cited:**" in body:
        new_body = re.sub(
            r"(- \*\*Also cited:\*\*[^\n]*\n(?:  - [^\n]*\n)*)",
            lambda m: m.group(1) + line + "\n",
            body,
            count=1,
        )
    else:
        new_body = body.rstrip() + f"\n- **Also cited:**\n{line}\n"

    cards[target] = (cid, head, new_body)

    header = gh.PRODUCTS_HEADER.rstrip() + "\n"
    out = header
    for _, h, b in cards:
        out += f"\n### {h}\n{b.rstrip()}\n"

    from _sot_github import _write_file, _lock
    with _lock:
        ok, err = _write_file(gh.PRODUCTS_PATH, out, f"[products] merge source into {target_card_id}")
    return (ok is not None), err
