"""
External purchases -- the "I already bought it elsewhere" channel.

The agent only has visibility into Carrefour + Kibsons online orders.
In reality the household buys groceries via Talabat, picks things up
at the mall, gets stuff from family, etc. When they tell the agent
"just bought milk", we record it as a phantom purchase so the
predictor stops suggesting milk for the natural cadence.

Public API:
    parse_external_purchase_text(text) -> dict | None
        Regex parser. Returns {raw_names: [str, ...], date: 'YYYY-MM-DD'}
        if the text matches a "just bought X" / "got X" / "I have X"
        pattern. None if the text isn't recognised.

    record_external_purchase(raw_name, purchase_date, source, ...)
        Insert one row. Resolves raw_name -> items.id via fuzzy
        match where possible. Returns the new id + resolved item_id
        + name.

    record_many_from_text(text, source, member_id, conn)
        Convenience: parse + record multi-item utterances like
        "got milk, bread, and eggs". Returns a list of records.

The cadence calibrator (cadence_calibration.refresh_observed_cadences)
unions order_items + external_purchases so the cadence math reflects
these. See that module for the read side.
"""

import re
import sqlite3
from datetime import datetime
from db_setup import DB_PATH


# ============================================================
# REGEX PARSER
# ============================================================

# Action verbs that mean "this is something we already have / bought".
# Tense matters: "bought" is past, "buy" is future (a request).
# We match past-tense or stative ("I have X") only.
_PAST_VERBS = r"(?:bought|got|grabbed|picked\s+up|purchased)"
_STATIVE = r"(?:have(?:\s+got)?|got)"  # "we have milk", "got milk"

# Patterns that signal "external purchase" -- order matters,
# more specific patterns first.
_PATTERNS = [
    # "i just bought / we just got / picked up": clear past purchases
    re.compile(rf"^\s*(?:i|we)?\s*(?:just\s+)?{_PAST_VERBS}\s+(.+?)(?:\s+(?:at|from|today|yesterday)\s+.+)?\s*$", re.IGNORECASE),
    # "got X" without subject (terse)
    re.compile(rf"^\s*{_PAST_VERBS}\s+(.+?)(?:\s+(?:at|from)\s+.+)?\s*$", re.IGNORECASE),
    # "I have X" / "we have X" / "we have plenty of X"
    re.compile(r"^\s*(?:i|we)\s+(?:already\s+)?(?:have|got)\s+(?:plenty\s+of\s+|enough\s+|some\s+)?(.+?)(?:\s+(?:already|left|in\s+(?:the\s+)?fridge|in\s+(?:the\s+)?pantry|at\s+home))?\s*$", re.IGNORECASE),
    # "X is in the fridge" / "X already at home"
    re.compile(r"^\s*(.+?)\s+(?:is|are)\s+(?:already\s+)?(?:in\s+(?:the\s+)?(?:fridge|pantry|cupboard)|at\s+home|in\s+stock)\s*$", re.IGNORECASE),
]

# Phrases that look like external purchases but are actually different
# intents. Used to filter false positives.
_REJECT_PATTERNS = [
    re.compile(r"^\s*(?:i\s+)?(?:want|need|would\s+like)\s+to\s+(?:buy|get)\b", re.I),  # "I want to buy" = add request
    re.compile(r"^\s*can\s+(?:you|i)\b", re.I),  # questions
    re.compile(r"\bnext\s+time\b", re.I),
    re.compile(r"\b(?:add|please\s+add)\b.*\bto\s+(?:the\s+)?(?:list|cart)\b", re.I),
]


def parse_external_purchase_text(text):
    """Try to parse `text` as an external-purchase utterance.

    Returns a dict if matched:
        {
          "raw_names": ["milk", "bread"],
          "date": "YYYY-MM-DD"  (today by default),
        }
    Returns None if not recognised.

    Date parsing is intentionally simple -- recognises "today" /
    "yesterday" and otherwise defaults to today. We don't try to
    parse arbitrary date strings here; it's not worth the
    complexity for marginal value.
    """
    if not text:
        return None

    raw = text.strip()
    if len(raw) < 4 or len(raw) > 300:
        return None

    # Reject obvious non-matches first
    for rx in _REJECT_PATTERNS:
        if rx.search(raw):
            return None

    matched = None
    for rx in _PATTERNS:
        m = rx.match(raw)
        if m:
            matched = m.group(1).strip()
            break
    if not matched:
        return None

    # Date inference: "yesterday" backs off one day, otherwise today
    purchase_date = datetime.now().date()
    if re.search(r"\byesterday\b", raw, re.I):
        from datetime import timedelta
        purchase_date = purchase_date - timedelta(days=1)

    # Split on commas / "and" so "milk, bread and eggs" -> 3 items.
    items = re.split(r"\s*(?:,|\band\b|\+)\s*", matched, flags=re.I)
    items = [_clean_item_name(i) for i in items if i.strip()]
    items = [i for i in items if i]  # drop empties from over-split

    if not items:
        return None

    return {
        "raw_names": items,
        "date": purchase_date.strftime("%Y-%m-%d"),
    }


def _clean_item_name(s):
    """Strip leading articles, trailing junk."""
    s = s.strip()
    # Drop leading "some"/"a"/"the"/"my"
    s = re.sub(r"^(?:some|a|the|my|our)\s+", "", s, flags=re.I)
    # Drop trailing prepositional phrases
    s = re.sub(r"\s+(?:from|at|on)\s+.*$", "", s, flags=re.I)
    # Strip trailing punctuation
    s = s.rstrip(".,!?")
    return s.strip()


# ============================================================
# ITEM RESOLVER (raw_name -> items.id)
# ============================================================

def _resolve_item_id(raw_name, conn):
    """Best-effort fuzzy match: raw_name -> items.id.

    Strategy:
      1. Exact LOWER(name) match.
      2. Substring match (raw_name LIKE items.name OR vice versa).
      3. None.

    Returns (item_id, items.name) or (None, None). We always store
    the raw_name even when we can't resolve -- a later enrichment
    pass can link it.
    """
    if not raw_name:
        return None, None
    norm = raw_name.lower().strip()
    cur = conn.cursor()

    # 1. Exact match
    row = cur.execute(
        "SELECT id, name FROM items WHERE LOWER(name) = ? LIMIT 1",
        (norm,),
    ).fetchone()
    if row:
        return (row["id"] if isinstance(row, sqlite3.Row) else row[0],
                row["name"] if isinstance(row, sqlite3.Row) else row[1])

    # 2. Substring match either direction
    row = cur.execute(
        """SELECT id, name FROM items
           WHERE LOWER(name) LIKE ?
              OR ? LIKE LOWER(name) || '%'
              OR LOWER(name) LIKE '%' || ? || '%'
           ORDER BY LENGTH(name) ASC
           LIMIT 1""",
        (f"%{norm}%", norm, norm),
    ).fetchone()
    if row:
        return (row["id"] if isinstance(row, sqlite3.Row) else row[0],
                row["name"] if isinstance(row, sqlite3.Row) else row[1])

    return None, None


# ============================================================
# RECORDING
# ============================================================

def _skip_matching_active_list_items(item_id, source, conn):
    """When an external purchase is recorded, ANY active draft list
    that still has this item pending should be skipped -- the user
    has it, they don't want it on the list anymore.

    Mirrors the Tier-0 drop flow (see skip_tracking.apply_tier_zero_drop):
    marks list_items as skipped, logs a skip_event, pushes
    next_restock_date forward.

    Returns list of list_item_ids that were marked skipped.
    """
    if not item_id:
        return []

    # Find all pending list_items on active draft lists for this item.
    rows = conn.execute(
        """SELECT li.id AS list_item_id, li.list_id
           FROM list_items li
           JOIN shopping_lists sl ON li.list_id = sl.id
           WHERE li.item_id = ?
             AND sl.status IN ('draft', 'confirmed')
             AND sl.dismissed_at IS NULL
             AND (li.user_action IS NULL OR li.user_action = '')
             AND (li.status IS NULL OR li.status = 'pending')""",
        (item_id,),
    ).fetchall()

    if not rows:
        return []

    # Use the same Tier-0 path so skip_events + cadence push stay
    # consistent with the manual-drop flow.
    from skip_tracking import apply_tier_zero_drop

    skipped_ids = []
    for r in rows:
        list_item_id = r["list_item_id"] if isinstance(r, sqlite3.Row) else r[0]
        # Mark the list item itself as skipped
        try:
            from list_generate import list_update_item
            list_update_item(int(list_item_id), "skip", conn=conn)
        except Exception:
            pass
        # Then run the Tier-0 push (uses a separate connection inside,
        # which is fine -- SQLite auto-handles concurrent reads of the
        # row we just updated).
        try:
            apply_tier_zero_drop(
                list_item_id=int(list_item_id),
                source=source,
                reason="have_it",
            )
            skipped_ids.append(int(list_item_id))
        except Exception:
            pass

    return skipped_ids


def record_external_purchase(
    raw_name,
    purchase_date=None,
    quantity=1,
    source="chat",
    member_id=None,
    conn=None,
):
    """Insert one external purchase row.

    Side effect: if `raw_name` resolves to a tracked items.id AND that
    item is currently on an active draft list, the matching list_item
    gets marked skipped (with reason='have_it') and a skip_event is
    logged. This keeps the user's "I have it" intent consistent
    across surfaces: chat-reported external purchases pull the item
    off the Scheduled tab just like tapping minus would.

    Returns:
        {
          "id": int,
          "item_id": int | None,
          "resolved_name": str | None,  # items.name if matched
          "raw_name": str,
          "purchase_date": str,
          "skipped_list_items": [int, ...],  # which list rows were dropped
        }
    """
    if not raw_name or not raw_name.strip():
        return None

    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own_conn = True

    try:
        if not purchase_date:
            purchase_date = datetime.now().strftime("%Y-%m-%d")

        item_id, resolved_name = _resolve_item_id(raw_name, conn)
        confidence = 0.9 if item_id else 0.4

        cur = conn.cursor()
        cur.execute(
            """INSERT INTO external_purchases
                   (item_id, raw_name, purchase_date, quantity,
                    source, member_id, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item_id, raw_name, purchase_date, quantity,
             source, member_id, confidence),
        )
        new_id = cur.lastrowid
        conn.commit()

        # Pull the item off any active draft list -- "I have it"
        # semantics mirror a Tier-0 drop.
        skip_source = "reviewer_have_it" if source == "whatsapp" else "curator_have_it"
        skipped_list_items = _skip_matching_active_list_items(
            item_id, skip_source, conn
        )

        # Learning signal (only when we resolved a real item). "Have it" is a
        # POSITIVE inclusion signal — the household uses this item, so reaffirm
        # it's wanted and clear any skip streak. Emitted in the item_inclusion
        # dimension ONLY, never timing: having it now must NOT shrink the restock
        # interval (the wrong-direction offline_purchase behavior that kept this
        # deferred). The restock-clock reset is handled by this external_purchases
        # row feeding the cadence math.
        if item_id:
            try:
                from preference_engine import emit_signal
                emit_signal(
                    item_id=item_id,
                    signal_type="purchase_observed",
                    signal_source=source or "external",
                    dimension="item_inclusion",
                    context={"channel": "have_it", "raw_name": raw_name},
                )
            except Exception:
                pass

        return {
            "id": new_id,
            "item_id": item_id,
            "resolved_name": resolved_name,
            "raw_name": raw_name,
            "purchase_date": purchase_date,
            "confidence": confidence,
            "skipped_list_items": skipped_list_items,
        }
    finally:
        if own_conn:
            conn.close()


def record_many_from_text(text, source="chat", member_id=None, conn=None):
    """Parse `text` for external-purchase intent and record matches.

    Returns:
        {
          "parsed": bool,
          "purchase_date": str | None,
          "records": [
              {"item_id", "resolved_name", "raw_name", "confidence"}, ...
          ],
        }
    If `parsed` is False the text didn't match any external-purchase
    pattern (caller should fall through to normal chat handling).
    """
    parsed = parse_external_purchase_text(text)
    if not parsed:
        return {"parsed": False, "purchase_date": None, "records": []}

    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own_conn = True

    try:
        records = []
        for raw in parsed["raw_names"]:
            rec = record_external_purchase(
                raw_name=raw,
                purchase_date=parsed["date"],
                source=source,
                member_id=member_id,
                conn=conn,
            )
            if rec:
                records.append(rec)
        return {
            "parsed": True,
            "purchase_date": parsed["date"],
            "records": records,
        }
    finally:
        if own_conn:
            conn.close()
