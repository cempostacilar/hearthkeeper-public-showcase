"""
Hearthkeeper - WhatsApp Feedback Agent

Processes household member replies on WhatsApp.
3-tier hybrid approach:
  Tier 1: Pattern matching (free, handles ~85% of messages)
  Tier 2: Catalog search (free, resolves item names to products)
  Tier 3: Claude LLM fallback (for ambiguous messages only)
"""

import os
import re
import sqlite3
from typing import Optional

from config import ANTHROPIC_API_KEY, LLM_MODEL
from db_setup import DB_PATH
from product_knowledge import search_catalog
from app_logging import get_logger

logger = get_logger("whatsapp_agent")

# ── Tier 1: Pattern Matching ──

# Intent patterns — order matters (first match wins)
_ADD_PATTERNS = [
    # "add 3 eggs", "get 2x milk" — verb + quantity + item (must be before plain verb pattern)
    re.compile(r"^(?:add|get|need|buy|also|include|want|grab)\s+(\d+)\s*x?\s+(.+)", re.I),
    # "add X", "get X", "need X", "buy X", "also X"
    re.compile(r"^(?:add|get|need|buy|also|include|want|grab)\s+(.+)", re.I),
    # "X please", "some X"
    re.compile(r"^(?:some|a few|a couple|couple)\s+(.+)", re.I),
    re.compile(r"^(.+?)\s+please$", re.I),
    # "{qty} X" or "{qty}x X"
    re.compile(r"^(\d+)\s*x?\s+(.+)", re.I),
]

_REMOVE_PATTERNS = [
    # "remove X", "remove {number}", "skip X", "no X", "take out X"
    re.compile(r"^(?:remove|skip|drop|cancel|no more|take out|don't need|dont need)\s+(.+)", re.I),
    re.compile(r"^no\s+(.+)", re.I),
]

# v2 verb-then-numbers shortcut commands.
# "skip 3", "skip 1 3 5", "have 2", "have 1, 3", "stop 4"
# Verbs canonicalised: have / skip / stop.
# Aliases parsed -> same canonical action. Each number becomes a
# separate action so the dispatcher can run them in order.
#
# Trailing punctuation (. , ! ?) is tolerated so users typing on a
# phone keyboard can finish with a period or exclamation without
# breaking the regex match. Found in QA: "skip 1." was being
# misparsed as a generic remove with item="1."
_VERB_NUM_RE = re.compile(
    r"^\s*(skip|remove|drop|have|had|got|stop|never|swap|change|switch|replace)\s+([\d\s,]+)[\s.,!?]*$",
    re.IGNORECASE,
)

# Verbs that signal a NEGATIVE intent: remove, skip, stop, etc. If
# any of these appear ANYWHERE in a message that didn't match the
# verb-with-numbers regex, refuse to fuzzy-add the rest of the
# message. Otherwise "could you maybe skip the milk please"
# becomes "ADD milk" -- a confidently wrong outcome.
_NEGATIVE_VERB_RE = re.compile(
    r"\b(skip|remove|stop|never|drop|cancel|swap|don['’]?t|dont|"
    r"take\s+out|no\s+more|not?\s+(this|the))\b",
    re.IGNORECASE,
)

# Verbs that the user typed at the START of the message but without
# a number (e.g. "skip the milk please"). The dispatcher renders a
# "I need a number" hint instead of fuzzy-matching the rest.
_VERB_NO_NUMBER_RE = re.compile(
    r"^\s*(skip|remove|drop|have|had|got|stop|never|swap|change|switch|replace)\b",
    re.IGNORECASE,
)
_VERB_TO_ACTION = {
    "skip":     "skip_tier0",
    "remove":   "skip_tier0",
    "drop":     "skip_tier0",
    "have":     "have_it",
    "had":      "have_it",
    "got":      "have_it",
    "stop":     "stop_suggesting",
    "never":    "stop_suggesting",
    # swap variants -- ask the alternatives engine for 3 candidates
    "swap":     "swap_request",
    "change":   "swap_request",
    "switch":   "swap_request",
    "replace":  "swap_request",
}


# Verbs short enough that ±1 edit distance routinely collides with
# real English. "had" is 3 chars and "have" is 4; fuzzy-matching
# either to "head" / "hat" / "haze" would be wrong. Exclude them
# from the fuzzy path; users can still type the exact word.
_NO_FUZZY_VERBS = {"had", "got"}


def _verb_edit_distance(a: str, b: str, cap: int = 2) -> int:
    """Damerau-Levenshtein distance with an early-exit cap.

    Standard Levenshtein counts a transposition (swapping two adjacent
    chars, e.g. "sikp" -> "skip") as TWO ops, which means "skip"
    typoed as "sikp" wouldn't match at our cap=1. Damerau-Levenshtein
    counts it as ONE op, which matches what a human typist actually
    did. That's especially important here because transpositions are
    the most common phone-keyboard error.

    Capped because anything >cap is "definitely not a typo of this
    verb"; returning cap+1 short-circuits the caller's <=cap check.
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    if la == 0:
        return lb
    if lb == 0:
        return la
    # Full DP table for Damerau-Levenshtein (needs i-2/j-2 lookback).
    rows = la + 1
    cols = lb + 1
    d = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        d[i][0] = i
    for j in range(cols):
        d[0][j] = j
    for i in range(1, rows):
        row_min = cap + 1
        for j in range(1, cols):
            ca = a[i - 1]
            cb = b[j - 1]
            sub_cost = 0 if ca == cb else 1
            val = min(
                d[i - 1][j] + 1,         # deletion
                d[i][j - 1] + 1,         # insertion
                d[i - 1][j - 1] + sub_cost,  # substitution
            )
            # Damerau transposition: a[i-1]==b[j-2] and a[i-2]==b[j-1]
            if (
                i >= 2 and j >= 2
                and ca == b[j - 2]
                and a[i - 2] == cb
            ):
                val = min(val, d[i - 2][j - 2] + 1)
            d[i][j] = val
            if val < row_min:
                row_min = val
        if row_min > cap:
            return cap + 1   # entire row exceeds cap -> can only grow
    return d[la][lb]


def _fuzzy_resolve_verb(token: str) -> str | None:
    """Return the canonical verb a token resembles within edit distance
    ≤1, with safety constraints:

      * first letter must match the canonical (eliminates "sap" -> "swap")
      * length within ±1 of the canonical (eliminates "skippity" -> "skip")
      * verbs in _NO_FUZZY_VERBS (very short) require an exact match
      * candidate must be the unique closest verb -- ties resolve to
        None to avoid silent wrong routing

    Returns None for exact matches too if the verb is in
    _NO_FUZZY_VERBS but not the right length (defensive). Exact
    matches that ARE in _VERB_TO_ACTION are returned as-is.
    """
    if not token:
        return None
    t = token.lower().strip().rstrip(".,!?;:")
    if not t:
        return None
    # Exact match short-circuits.
    if t in _VERB_TO_ACTION:
        return t
    # Don't fuzzy-match very short verbs.
    best = None
    best_dist = 99
    second_best_dist = 99
    for canon in _VERB_TO_ACTION:
        if canon in _NO_FUZZY_VERBS:
            continue
        if t[0] != canon[0]:
            continue
        if abs(len(t) - len(canon)) > 1:
            continue
        d = _verb_edit_distance(t, canon, cap=1)
        if d <= 1:
            if d < best_dist:
                second_best_dist = best_dist
                best_dist = d
                best = canon
            elif d == best_dist:
                second_best_dist = d
    if best_dist > 1:
        return None
    if best_dist == second_best_dist:
        # Tie -- don't guess. "stop" vs "swap" with token "ssop"
        # would tie at distance 1; punt.
        return None
    return best


# Loose verb-with-numbers regex. Captures the first token (any
# letters) so we can fuzzy-resolve it. Used by parse_verb_with_numbers
# when the strict _VERB_NUM_RE doesn't match -- catches typoed
# commands like "skipp 3" or "hve 1 2" without forcing the user
# to spell it perfectly.
_FUZZY_VERB_NUM_RE = re.compile(
    r"^\s*([a-zA-Z]+)\s+([\d\s,]+)[\s.,!?]*$",
)


def parse_verb_with_numbers(text):
    """Parse '<verb> <num> [<num> ...]' commands, with typo tolerance.

    Returns a list of action dicts, one per number, or None if the
    text doesn't match the pattern. Each action has the canonical
    type ('skip_tier0' / 'have_it' / 'stop_suggesting' /
    'swap_request') and a ref_number pointing into the numbered list
    message we sent.

    Two passes:
      1. Strict _VERB_NUM_RE for the canonical spelling.
      2. Fuzzy _FUZZY_VERB_NUM_RE with _fuzzy_resolve_verb -- catches
         "skipp 3", "hve 2", "stp 4", "swp 1" etc. without an
         LLM round-trip.
    """
    if not text:
        return None

    nums = None
    verb = None

    # Strict path first -- cheapest and zero-risk.
    m = _VERB_NUM_RE.match(text.strip())
    if m:
        verb = m.group(1).lower()
        nums = [int(n) for n in re.findall(r"\d+", m.group(2))]
    else:
        # Fuzzy path: pull the first alpha token, see if it's a
        # near-miss spelling of a known verb, and use the numbers
        # that follow.
        m2 = _FUZZY_VERB_NUM_RE.match(text.strip())
        if m2:
            candidate = m2.group(1)
            resolved = _fuzzy_resolve_verb(candidate)
            if resolved:
                verb = resolved
                nums = [int(n) for n in re.findall(r"\d+", m2.group(2))]

    if not verb or not nums:
        return None
    action = _VERB_TO_ACTION.get(verb)
    if not action:
        return None
    # Dedupe while preserving order so "skip 3 3 5" -> [3, 5].
    seen = set()
    deduped = []
    for n in nums:
        if n not in seen:
            seen.add(n)
            deduped.append(n)
    return [
        {"type": action, "ref_number": n, "item": None, "qty": 0}
        for n in deduped
    ]

_CONFIRM_PATTERNS = [
    re.compile(r"^(?:ok|okay|looks?\s+good|fine|yes|yep|yeah|approved?|perfect|great|all\s+good|no\s+changes?|nothing|nope|nah|no\s+thanks?|good)(?:\s*!*\.*)$", re.I),
    re.compile(r"^(?:lgtm|sounds?\s+good|that's?\s+it|thats?\s+it|nothing\s+else|all\s+set)(?:\s*!*\.*)$", re.I),
]

_DONE_PATTERNS = [
    re.compile(r"^(?:done|that'?s?\s+(?:all|it)|nothing\s+else|all\s+(?:good|done|set))(?:\s*!*\.*)$", re.I),
]

# Emoji shortcuts
_THUMBS_UP = {"👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿", "✅", "☑️"}
_THUMBS_DOWN = {"👎", "👎🏻", "👎🏼", "👎🏽", "👎🏾", "👎🏿", "❌", "🚫"}


def parse_message_tier1(text: str) -> list[dict]:
    """
    Tier 1: Parse a WhatsApp message using regex patterns.

    Returns a list of parsed actions:
      [{"type": "add"|"remove"|"confirm"|"done"|"unknown", "item": str, "qty": int}]
    """
    text = text.strip()
    if not text:
        return [{"type": "unknown", "item": None, "qty": 0}]

    # Emoji shortcuts
    if text in _THUMBS_UP:
        return [{"type": "confirm", "item": None, "qty": 0}]
    if text in _THUMBS_DOWN:
        return [{"type": "confirm", "item": None, "qty": 0}]

    # v2 verb-with-numbers commands (skip 1 3 5, have 2, stop 4) --
    # checked BEFORE the generic remove/add patterns so terse
    # "skip 3" reaches the multi-number dispatcher.
    verb_actions = parse_verb_with_numbers(text)
    if verb_actions:
        return verb_actions

    # "undo" -- universal recovery within the round's undo window.
    if re.match(r"^\s*undo(?:\s+all)?\s*$", text, re.I):
        return [{"type": "undo", "all": bool(re.search(r"\ball\b", text, re.I))}]

    # Done patterns
    for pattern in _DONE_PATTERNS:
        if pattern.match(text):
            return [{"type": "done", "item": None, "qty": 0}]

    # Confirm patterns
    for pattern in _CONFIRM_PATTERNS:
        if pattern.match(text):
            return [{"type": "confirm", "item": None, "qty": 0}]

    # Verb-without-number short-circuit. If the message STARTS with
    # a command verb (skip / remove / stop / swap / have / etc.) but
    # didn't match the verb-with-numbers regex, the user almost
    # certainly meant a number-based command -- they just forgot the
    # number or referenced the item by name. Don't fall into fuzzy
    # add/remove parsing where "skip the milk" became "remove 'the
    # milk'" (phantom item not on the list).
    m = _VERB_NO_NUMBER_RE.match(text)
    if m:
        verb = m.group(1).lower()
        return [{
            "type": "needs_number",
            "verb": verb,
            "raw": text,
        }]
    # Same short-circuit but for typoed verbs: "skipp the milk" /
    # "hve item 3" should ALSO surface as needs_number, not slip
    # through to the negative-verb fallback or the LLM. We extract
    # the first alpha token and fuzzy-resolve it.
    m_first = re.match(r"^\s*([a-zA-Z]+)\b", text)
    if m_first:
        resolved = _fuzzy_resolve_verb(m_first.group(1))
        if resolved:
            return [{
                "type": "needs_number",
                "verb": resolved,
                "raw": text,
            }]

    # Negative-verb anywhere short-circuit. "could you maybe skip
    # the milk please" doesn't start with a verb but contains one.
    # The X-please add pattern (and other lax patterns) would
    # otherwise resolve "milk" via fuzzy catalog match and ADD it
    # to the list -- the worst possible outcome (confidently wrong).
    #
    # Returns "needs_number" rather than "unknown" so process_message
    # does NOT fall through to the LLM fallback (which would happily
    # parse "skip the milk please" as a remove-by-name action and
    # undo the guard). The dispatcher's needs_number branch renders
    # the verb-specific hint.
    neg_m = _NEGATIVE_VERB_RE.search(text)
    if neg_m:
        verb_token = (neg_m.group(1) or "").lower().strip()
        return [{
            "type": "needs_number",
            "verb": verb_token,
            "raw": text,
            "reason": "negative_verb_no_number",
        }]

    # Try splitting on "and" / commas for multi-item messages
    # e.g., "add greek yogurt and avocados"
    results = []
    segments = _split_items(text)

    for segment in segments:
        parsed = _parse_single_segment(segment.strip())
        if parsed:
            results.extend(parsed)

    return results if results else [{"type": "unknown", "item": text, "qty": 0}]


def _split_items(text: str) -> list[str]:
    """
    Split multi-item messages on 'and', commas, newlines.
    Preserves the action verb for each segment.
    """
    # Check if starts with an action verb
    action_match = re.match(r"^(add|get|need|buy|remove|skip|drop|also)\s+", text, re.I)
    action_prefix = action_match.group(0) if action_match else ""
    remainder = text[len(action_prefix):] if action_prefix else text

    # Split on "and", ",", "+"
    parts = re.split(r"\s*(?:,|\band\b|\+)\s*", remainder)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) <= 1:
        return [text]

    # Re-attach action prefix to each part (unless part already has one)
    result = []
    for i, part in enumerate(parts):
        has_action = re.match(r"^(?:add|get|need|buy|remove|skip|drop|also)\s+", part, re.I)
        if has_action or not action_prefix:
            result.append(part)
        else:
            result.append(action_prefix + part)

    return result


def _parse_single_segment(text: str) -> list[dict] | None:
    """Parse a single segment (one item action)."""
    # Remove patterns
    for pattern in _REMOVE_PATTERNS:
        m = pattern.match(text)
        if m:
            item_text = m.group(1).strip()
            # Check if it's a number reference (e.g., "remove 3")
            num_match = re.match(r"^(\d+)$", item_text)
            if num_match:
                return [{"type": "remove", "item": None, "qty": 0, "ref_number": int(num_match.group(1))}]
            return [{"type": "remove", "item": item_text, "qty": 1}]

    # Add patterns
    for pattern in _ADD_PATTERNS:
        m = pattern.match(text)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                # Pattern with quantity: "{qty} X"
                qty = int(groups[0])
                item_text = groups[1].strip()
            else:
                qty = 1
                item_text = groups[0].strip()
            return [{"type": "add", "item": item_text, "qty": qty}]

    # Number reference alone (e.g., "3" with no context — might mean "remove #3")
    num_match = re.match(r"^#?(\d+)$", text.strip())
    if num_match:
        return [{"type": "number_ref", "item": None, "qty": 0, "ref_number": int(num_match.group(1))}]

    return None


# ── Tier 2: Catalog Resolution ──

def resolve_item(item_text: str, store_id: int = None, limit: int = 5) -> list[dict]:
    """
    Resolve an item name to catalog products.
    Returns top matches from product_catalog.
    """
    if not item_text:
        return []

    results = search_catalog(item_text, store_id=store_id, limit=limit)
    return results


def auto_pick_best(item_text: str, store_id: int = None) -> dict | None:
    """
    Auto-pick the best catalog match for an item using the full curator pipeline.

    Uses curate_basket() — the same matching + scoring engine the web chat uses —
    instead of a bare SQL LIKE search. This gives:
    - Fuzzy items-table matching (handles typos, informal names)
    - Multi-store scoring with preference awareness
    - Full product details (brand, price, image, category)

    Returns a product dict or None if no good match.
    """
    try:
        from basket_curator import curate_basket
        result = curate_basket([item_text])
    except Exception as e:
        logger.warning("curate_basket failed for '%s': %s — falling back to search_catalog", item_text, e)
        # Fallback to simple catalog search if curator fails
        results = resolve_item(item_text, store_id=store_id, limit=3)
        return results[0] if results else None

    items = result.get("items", [])
    unmatched = result.get("unmatched", [])

    # If the item was unmatched or needs_input, no good match
    if unmatched or not items:
        return None

    matched = items[0]
    if matched.get("status") in ("unmatched", "needs_input"):
        return None

    # Extract the recommended option (the actual catalog product)
    rec_opt = None
    for opt in matched.get("options", []):
        if opt.get("store_id") == matched.get("recommended_store_id"):
            rec_opt = opt
            break
    if not rec_opt and matched.get("options"):
        rec_opt = matched["options"][0]

    if not rec_opt:
        return None

    # Return in the format feedback_manager expects
    return {
        "product_name": matched.get("recommended_product") or rec_opt.get("product_name"),
        "catalog_id": rec_opt.get("catalog_id"),
        "store_id": matched.get("recommended_store_id") or rec_opt.get("store_id"),
        "store_name": matched.get("recommended_store") or rec_opt.get("store_name"),
        "price": matched.get("recommended_price") or rec_opt.get("price"),
        "brand": rec_opt.get("brand", ""),
        "size": rec_opt.get("size", ""),
        "image_url": rec_opt.get("image_url", ""),
        "category": rec_opt.get("category", ""),
        "item_id": matched.get("matched_item_id"),  # items table ID for habit tracking
    }


# ── Tier 3: LLM Fallback ──

def build_whatsapp_llm_context(round_id: int = None, phone: str = None) -> str:
    """Snapshot of relevant app state for grounding the LLM fallback.

    Without this, the LLM can't tell that "drop the bread" refers to
    item #1 on the user's list, that "almond milk" is a sensible add
    given they avoid dairy, or that "more cheese" should be skipped
    because they already asked to stop suggesting it. The context is
    folded into the system prompt so the LLM produces ref_number-
    keyed actions the existing dispatcher can route directly.

    Returns a multi-line text block. Empty string if nothing relevant
    is available (so the prompt stays terse).
    """
    parts = []
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # 1) Numbered cart -- same ordering build_frontend_list uses, so
        #    a ref_number we return here matches what `format_cart_message`
        #    showed the user. This is the single most important piece
        #    of context: it turns "drop the bread" into "ref_number=1".
        if round_id:
            row = conn.execute(
                "SELECT list_id FROM feedback_rounds WHERE id = ?",
                (round_id,),
            ).fetchone()
            if row and row["list_id"]:
                try:
                    from frontend_bridge import build_frontend_list
                    data = build_frontend_list(row["list_id"], conn=conn)
                    cart_lines = []
                    n = 0
                    for vendor in data.get("vendors", []):
                        store_name = (
                            vendor.get("displayName")
                            or vendor.get("name") or ""
                        )
                        for cat in vendor.get("categories", []):
                            for item in cat.get("items", []):
                                if item.get("quantity", 1) <= 0:
                                    continue
                                n += 1
                                name = (
                                    item.get("genericName")
                                    or item.get("name") or "?"
                                )
                                price = item.get("price")
                                line = f"  #{n}. {name}"
                                if store_name:
                                    line += f" — {store_name}"
                                if isinstance(price, (int, float)) and price > 0:
                                    line += f" (AED {price:.2f})"
                                cart_lines.append(line)
                    if cart_lines:
                        parts.append("CURRENT NUMBERED CART:")
                        parts.extend(cart_lines)
                    else:
                        parts.append("CURRENT NUMBERED CART: (empty)")
                except Exception as e:
                    logger.debug(
                        "build_frontend_list failed in LLM context: %s", e,
                    )

        # 2) Household-level preferences. Influences "is this a sensible
        #    add request?" decisions. We don't BLOCK adds in the LLM,
        #    but giving it visibility lets it warn / pick variants
        #    (e.g. organic milk when household prefers organic).
        try:
            pref_rows = conn.execute(
                "SELECT pref_key, pref_value FROM household_preferences"
            ).fetchall()
            prefs = {r["pref_key"]: r["pref_value"] for r in pref_rows}
            if prefs.get("prefer_keywords"):
                parts.append(f"HOUSEHOLD PREFERS: {prefs['prefer_keywords']}")
            if prefs.get("avoid_keywords"):
                parts.append(f"HOUSEHOLD AVOIDS: {prefs['avoid_keywords']}")
            if prefs.get("dietary_restrictions"):
                parts.append(f"DIETARY: {prefs['dietary_restrictions']}")
            if prefs.get("household_size"):
                parts.append(f"HOUSEHOLD SIZE: {prefs['household_size']} people")
        except Exception:
            pass

        # 3) Items the user has explicitly told us to stop suggesting.
        #    The LLM should NOT auto-add these even if the user mentions
        #    them casually; better to ask or return unknown so we surface
        #    the conflict.
        try:
            stops = conn.execute(
                "SELECT i.name FROM user_item_prefs uip "
                "JOIN items i ON i.id = uip.item_id "
                "WHERE uip.pref_type = 'stop_suggesting' "
                "AND uip.is_active = 1 "
                "ORDER BY uip.id DESC LIMIT 10"
            ).fetchall()
            if stops:
                parts.append(
                    "ITEMS USER ASKED US TO STOP SUGGESTING: "
                    + ", ".join(r["name"] for r in stops)
                )
        except Exception:
            pass

        # 4) Pinned products -- user-chosen winners for specific items.
        #    Useful so the LLM doesn't try to "swap" an item the user
        #    has already locked their preferred product for.
        try:
            pins = conn.execute(
                "SELECT i.name AS item_name, pc.product_name AS prod_name "
                "FROM user_item_prefs uip "
                "JOIN items i ON i.id = uip.item_id "
                "JOIN product_catalog pc "
                "  ON pc.id = CAST(uip.value AS INTEGER) "
                "WHERE uip.pref_type = 'pin_product' "
                "AND uip.is_active = 1 "
                "ORDER BY uip.id DESC LIMIT 10"
            ).fetchall()
            if pins:
                parts.append("PINNED PRODUCTS (user picks for these items):")
                for r in pins:
                    parts.append(f"  - {r['item_name']} -> {r['prod_name']}")
        except Exception:
            pass

        # 5) Pending dispatcher state -- e.g. a swap_pending where we
        #    already showed A/B/C. The Tier-1 path already short-circuits
        #    bare letter replies, but a free-text reply ("the cheaper
        #    one") needs LLM resolution and the LLM should know the
        #    options exist.
        if phone:
            try:
                pend = conn.execute(
                    "SELECT pending_intent FROM feedback_member_status fms "
                    "JOIN feedback_rounds fr ON fr.id = fms.round_id "
                    "JOIN whatsapp_members wm ON wm.id = fms.member_id "
                    "WHERE wm.phone = ? "
                    "  AND fr.status = 'collecting' "
                    "  AND fms.pending_intent IS NOT NULL "
                    "ORDER BY fr.initiated_at DESC LIMIT 1",
                    (phone,),
                ).fetchone()
                if pend and pend["pending_intent"]:
                    try:
                        import json as _json
                        pi = _json.loads(pend["pending_intent"])
                        if pi.get("kind") == "swap_pending":
                            options = pi.get("options") or []
                            opt_lines = ", ".join(
                                f"{chr(65+i)}={o.get('name','?')[:40]}"
                                for i, o in enumerate(options[:5])
                            )
                            parts.append(
                                f"PENDING STATE: user just asked to swap "
                                f"#{pi.get('ref_number')} "
                                f"({pi.get('generic_name','?')}) and was "
                                f"shown alternatives [{opt_lines}]. If they "
                                f"now pick one in free text, return "
                                f"swap_pick with choice_letter (A/B/C/D/E)."
                            )
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        logger.debug("build_whatsapp_llm_context outer failure: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return "\n".join(parts) if parts else ""


# System prompt for the WhatsApp LLM fallback. Folds in the
# build_whatsapp_llm_context() block so the LLM can resolve item
# references ("drop the bread") to numbered ref values, honor user
# stop-suggesting preferences, and produce the SAME action-type
# strings the existing dispatcher already routes.
_WA_LLM_SYSTEM_PROMPT = """You are Hearthkeeper, a UAE household's grocery restock assistant.
You're parsing a free-text WhatsApp reply that the regex parser couldn't handle.
Use the CONTEXT below to make informed decisions; never invent items, \
prices, or numbers that aren't grounded in it.

{context}

The cart above is NUMBERED. When the user references an item by name
("drop the bread", "I bought the eggs", "swap the milk"), find its
number in the cart and return an action keyed by ref_number. If the
named item is NOT on the cart, you may add it instead.

ACTIONS (return a JSON array of these objects, nothing else):
  {{"type": "skip_tier0",      "ref_number": N}}              # drop item from THIS cycle
  {{"type": "have_it",         "ref_number": N}}              # user already bought it (item IS on cart)
  {{"type": "stop_suggesting", "ref_number": N}}              # permanently stop suggesting
  {{"type": "swap_request",    "ref_number": N}}              # show 3 alternatives
  {{"type": "swap_pick",       "choice_letter": "A"}}         # ONLY if PENDING STATE shows a swap is open: pick option A/B/C/D/E
  {{"type": "add",   "item": "<name>", "qty": K}}             # add a NEW item not on cart
  {{"type": "external_purchase", "item": "<name>", "qty": K}} # user bought elsewhere (item NOT on cart)
  {{"type": "remove", "ref_number": N}}                       # same as skip
  {{"type": "confirm"}}                                       # "looks good", "send it"
  {{"type": "done"}}                                          # finish the round
  {{"type": "unknown", "item": "<the original text>"}}        # can't tell what they mean

RULES:
- Currency is AED (د.إ). Do not invent prices.
- If they mention an item ALREADY on the numbered cart, use its
  ref_number rather than "add" or "external_purchase".
- If the user says they already bought / got / picked up something
  that is NOT on the cart ("I picked up bread at Lulu", "we have
  milk at home", "got eggs from the corner shop"), return
  external_purchase. Do NOT use add for past-tense ownership.
- "add" is reserved for items the user wants in the upcoming order
  ("get me almond milk", "we need yogurt"). Future intent, not past.
- If they ask to add an item from the STOPPED SUGGESTING list, still
  add it -- they're explicitly overriding the prior stop. Just add.
- Multiple actions in one message are fine, return them in order.
- ONLY return swap_pick when PENDING STATE shows an open swap and
  the user's reply chooses one of the options (e.g. "the cheaper
  one", "the second one", "Carrefour brand"). Resolve free-text
  preferences against the listed A/B/C/D/E options. If no swap is
  pending, never return swap_pick.
- If the message is just chat / thanks / a question with no action,
  return [{{"type": "unknown", "item": "<text>"}}].

OUTPUT: pure JSON array. No markdown, no explanation, no leading text.

Examples:
  user: "drop the bread please"            -> [{{"type": "skip_tier0", "ref_number": 1}}]
  user: "I bought milk yesterday"  (milk IS on cart as #2)  -> [{{"type": "have_it", "ref_number": 2}}]
  user: "we picked up almond milk at Lulu"  (NOT on cart)  -> [{{"type": "external_purchase", "item": "almond milk", "qty": 1}}]
  user: "add yogurt and remove the cheese" -> [{{"type": "add", "item": "yogurt", "qty": 1}}, {{"type": "skip_tier0", "ref_number": 3}}]
  user: "show me other options for pasta"  -> [{{"type": "swap_request", "ref_number": 4}}]
  user: "the cheaper one"  (with PENDING STATE showing options) -> [{{"type": "swap_pick", "choice_letter": "<the cheapest option's letter>"}}]
  user: "looks great"                      -> [{{"type": "confirm"}}]
"""


async def parse_with_llm(
    text: str, round_id: int = None, phone: str = None,
) -> list[dict]:
    """
    Use Claude to parse an ambiguous WhatsApp message into actions.
    Only called when Tier 1 can't parse the message.

    `round_id` and `phone` are used to build a context block (current
    numbered cart, household prefs, pending swap state) that grounds
    the LLM in the user's actual situation. Both default to None for
    backwards compat with the previous signature; if neither is given
    the LLM still runs but with the old "blind" prompt.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("No API key — LLM fallback unavailable")
        return [{"type": "unknown", "item": text, "qty": 0}]

    # Budget gate -- skip the LLM if we've hit the daily/monthly cap.
    # Emit a budget_skipped action (rather than plain unknown) so the
    # dispatcher can render a user-visible "AI budget reached" message
    # instead of a confusing "I don't understand".
    try:
        from llm_budget import can_spend, estimate_cost
        est = estimate_cost(LLM_MODEL, 400, 256)  # rough estimate
        ok, reason = can_spend(est)
        if not ok:
            logger.warning(
                "LLM budget reached (reason=%s, est $%.4f) — emitting budget_skipped",
                reason, est,
            )
            try:
                from app_logging import log_event
                log_event(
                    "llm_budget_exceeded", source="whatsapp_agent",
                    detail=(
                        f"intent=whatsapp_legacy_parse model={LLM_MODEL} "
                        f"est=${est:.4f} reason={reason}"
                    ),
                    level="warning",
                )
            except Exception:
                pass
            return [{
                "type": "budget_skipped",
                "reason": reason,  # 'daily_cap' | 'monthly_cap' | 'db_error'
                "item": text,
                "qty": 0,
            }]
    except Exception:
        pass

    try:
        import anthropic

        # Build the grounding context block. If neither round_id nor
        # phone is provided we still run the LLM, but the context will
        # be empty and the LLM falls back to "no situational data".
        ctx_block = build_whatsapp_llm_context(
            round_id=round_id, phone=phone,
        ) or "(no situational context available)"

        system_prompt = _WA_LLM_SYSTEM_PROMPT.format(context=ctx_block)

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=512,  # bumped from 256: action list can be long
            system=system_prompt,
            messages=[{"role": "user", "content": text}],
        )

        # Record the spend now that we have actual token counts.
        try:
            from llm_budget import record_call
            record_call(
                provider="anthropic",
                model=LLM_MODEL,
                intent="whatsapp_ctx_parse",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        except Exception:
            pass

        import json
        raw = response.content[0].text.strip()
        # Strip markdown fences if the model wraps in ```json ... ```
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```\s*$", "", raw)
        # Try to extract JSON from the response
        if raw.startswith("["):
            parsed = json.loads(raw)
        else:
            # Try to find JSON array in the text
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
            else:
                return [{"type": "unknown", "item": text, "qty": 0}]

        # Normalize to our dispatcher's action contract. The LLM is
        # instructed to use these exact type names, but we defensively
        # accept "action": "add"/"remove" (the old single-shape) as
        # synonyms so an older-style response doesn't break the flow.
        _DISPATCHER_TYPES = {
            "skip_tier0", "have_it", "stop_suggesting", "swap_request",
            "swap_pick", "add", "external_purchase",
            "remove", "confirm", "done", "unknown",
        }
        results = []
        for entry in parsed:
            t = entry.get("type") or entry.get("action") or ""
            t = str(t).lower().strip()

            # Map "skip" and similar friendly aliases the LLM might emit
            # despite the prompt. Keeping this list short so we don't
            # silently route surprising types.
            if t in ("skip", "drop"):
                t = "skip_tier0"
            elif t in ("have", "had", "got", "bought"):
                t = "have_it"
            elif t in ("stop", "never"):
                t = "stop_suggesting"
            elif t in ("swap", "switch", "change", "replace"):
                t = "swap_request"
            elif t in ("ok", "good", "approve", "approved"):
                t = "confirm"
            elif t in ("finish", "complete"):
                t = "done"
            elif t == "unclear":
                t = "unknown"

            if t not in _DISPATCHER_TYPES:
                # Unknown type from the LLM -- fall back to unknown so
                # the dispatcher renders the generic help reply rather
                # than silently doing nothing.
                t = "unknown"

            action = {"type": t}
            # ref_number-driven actions
            if t in ("skip_tier0", "have_it", "stop_suggesting",
                     "swap_request", "remove"):
                ref = entry.get("ref_number")
                if isinstance(ref, str) and ref.isdigit():
                    ref = int(ref)
                if isinstance(ref, int) and ref > 0:
                    action["ref_number"] = ref
                else:
                    # Verb without a usable ref. Don't guess -- surface
                    # via needs_number so the user sees a clear hint.
                    action = {
                        "type": "needs_number",
                        "verb": (
                            "skip" if t in ("skip_tier0", "remove")
                            else "have" if t == "have_it"
                            else "stop" if t == "stop_suggesting"
                            else "swap"
                        ),
                    }
            elif t == "swap_pick":
                # Free-text resolution of a mid-swap_pending choice.
                # Accept choice_letter (A-E) or choice_index (0-4).
                # Downstream dispatcher looks up the actual option
                # against the pending_intent on the member's status.
                letter = entry.get("choice_letter")
                index = entry.get("choice_index")
                resolved_idx = None
                if isinstance(letter, str) and len(letter) == 1:
                    upper = letter.upper()
                    if upper in "ABCDE":
                        resolved_idx = ord(upper) - ord("A")
                if resolved_idx is None and isinstance(index, int) and 0 <= index <= 4:
                    resolved_idx = index
                if resolved_idx is None and isinstance(index, str) and index.isdigit():
                    n = int(index)
                    if 0 <= n <= 4:
                        resolved_idx = n
                if resolved_idx is None:
                    # LLM emitted swap_pick but gave nothing parseable.
                    # Fall back to unknown so the dispatcher's generic
                    # help text runs.
                    action = {
                        "type": "unknown",
                        "item": text,
                        "qty": 0,
                    }
                else:
                    action["choice_index"] = resolved_idx
            elif t == "add":
                action["item"] = entry.get("item") or entry.get("name") or ""
                qty = entry.get("qty") or entry.get("quantity") or 1
                try:
                    action["qty"] = int(qty)
                except Exception:
                    action["qty"] = 1
                if not action["item"]:
                    action = {"type": "unknown", "item": text, "qty": 0}
            elif t == "external_purchase":
                # User reports they already bought this elsewhere.
                # Routed to record_external_purchase which logs to the
                # external_purchases table + skips the item off any
                # active list if it resolves.
                action["item"] = entry.get("item") or entry.get("name") or ""
                qty = entry.get("qty") or entry.get("quantity") or 1
                try:
                    action["qty"] = int(qty)
                except Exception:
                    action["qty"] = 1
                if not action["item"]:
                    action = {"type": "unknown", "item": text, "qty": 0}
            elif t == "unknown":
                action["item"] = entry.get("item") or text
                action["qty"] = 0

            results.append(action)
        return results if results else [{"type": "unknown", "item": text, "qty": 0}]

    except Exception as e:
        logger.error("LLM parse failed: %s", e)
        return [{"type": "unknown", "item": text, "qty": 0}]


# ── Full Pipeline ──

async def process_message(
    text: str, round_id: int = None, phone: str = None,
) -> list[dict]:
    """
    Full 3-tier pipeline: parse a WhatsApp message into structured actions.

    Returns list of:
      {"type": "add"|"remove"|"confirm"|"done"|"unknown"|"skip_tier0"|...,
       "item": str|None, "qty": int,
       "ref_number": int|None,  # for number references
       "resolved": dict|None}   # catalog product if resolved

    `round_id` and `phone` are threaded into the Tier-3 LLM so it can
    ground its response in the user's current numbered cart and the
    household's known preferences.
    """
    # Tier 1: Pattern matching
    actions = parse_message_tier1(text)

    # If all unknown and has substance, try LLM
    all_unknown = all(a["type"] == "unknown" for a in actions)
    if all_unknown and len(text) > 3:
        logger.info("Tier 1 failed, trying LLM for: %s", text[:80])
        actions = await parse_with_llm(text, round_id=round_id, phone=phone)

    # Tier 2: Resolve items against catalog
    for action in actions:
        if action["type"] in ("add",) and action.get("item"):
            product = auto_pick_best(action["item"])
            action["resolved"] = product
        elif action["type"] == "remove" and action.get("item"):
            # For removes, try to match against catalog too
            product = auto_pick_best(action["item"])
            action["resolved"] = product

    return actions


# ── Message Generation ──

# Dirham symbol used in price lines. د.إ is the official UAE
# currency symbol but mixed Arabic-Latin script triggers bidi
# weirdness in some WhatsApp clients. "Dhs" is the common Latin
# shorthand used in UAE retail and renders cleanly in mixed text.
# Pick by env (WHATSAPP_CURRENCY_SYMBOL) -- defaults to د.إ.
_CURRENCY_SYMBOL = os.environ.get("WHATSAPP_CURRENCY_SYMBOL", "د.إ")


def _normalize_size(size_text):
    """Tighten a catalog size string for display.

    Catalog rows are noisy: we often see "Tarragon- 290g" or "Black -
    155ml" or "Choice Grade-500g" where the variant prefix is already
    part of the product_name on the title side. We want JUST the
    clean numeric + unit token at the end ("290g", "155ml", "500g").

    Steps:
      1. Arabic unit normalization (غرام / جرام / كيلوغرام).
      2. Extract the LAST <number><unit> token in the string. Drops
         all the prefix gunk before it.
      3. Normalize unit names (pcs / pieces -> pc).

    Falls back to a whitespace-collapsed copy of the input if no
    numeric+unit pattern is found (e.g. "1 Bunch" -> "1 Bunch").
    """
    if not size_text:
        return ""
    s = str(size_text).strip()
    s = s.replace("كيلوغرام", "kg").replace("غرام", "g").replace("جرام", "g")
    # Find the LAST number+unit token. Allow optional hyphen between
    # the number and the unit ("Choice Grade-500g" -> "500g").
    m = re.findall(
        r"(\d+(?:\.\d+)?)\s*-?\s*(kg|g|ml|l|oz|lb|pc|pcs|pieces?|count|ct|bunch)\b",
        s, re.I,
    )
    if m:
        num, unit = m[-1]
        unit = unit.lower()
        if unit in ("pcs", "pieces", "piece"):
            unit = "pc"
        if unit == "l":
            unit = "L"          # readability: capital L for liters
        # SI mass/volume: glued (200g, 500ml, 1kg, 1L)
        # Count units:    spaced (10 pc, 1 bunch) so they read naturally
        if unit in ("g", "kg", "ml", "L", "oz", "lb"):
            return f"{num}{unit}"
        return f"{num} {unit}"
    return re.sub(r"\s+", " ", s).strip()


def _strip_redundant_variant_suffix(title, raw_size):
    """When the catalog product_name ends with " - Variant" AND the
    raw size field also contains that variant text, drop the
    suffix from the title. Otherwise we render
    "Kefir - Original, Original - 500ml" which is ugly.

    Examples it cleans:
      title="Kefir - Original", size="Original - 500ml" -> "Kefir"
      title="Beef Mince - Choice Grade", size="Choice Grade-500g" -> "Beef Mince"
      title="Sausages - Tarragon", size="Tarragon- 290g" -> "Sausages"

    Keeps the suffix when the variant carries meaningful info that
    DOESN'T overlap with size:
      title="Eggs - Medium", size="15 PCS" -> "Eggs - Medium"
    """
    if " - " not in title or not raw_size:
        return title
    head, _, tail = title.rpartition(" - ")
    tail_norm = tail.strip().lower()
    if not tail_norm:
        return title
    # If the tail's words show up in the size string, the suffix is
    # redundant -- drop it.
    if tail_norm in raw_size.lower():
        return head
    return title


def _build_item_title(item, vendor_name):
    """Compose a single descriptive title from generic + variety +
    brand + size. Deduplicates word-overlaps so we never get
    "*Butter* / Unsalted Soft Butter" style duplication.

    Rules:
      * Start from variety (catalog product_name) when present --
        it's the most specific. Fall back to generic.
      * Prepend brand only when (a) brand exists, (b) variety/title
        doesn't already contain it, and (c) brand isn't the vendor
        name (e.g. private-label "Carrefour" when vendor is
        "Carrefour UAE" -- the vendor header already tells us that).
      * Append normalized size as ", 200g" suffix.
    """
    generic = (item.get("genericName") or item.get("name") or "").strip()
    variety = (item.get("variety") or "").strip()
    brand = (item.get("brand") or "").strip()
    raw_size = item.get("size") or ""
    size = _normalize_size(raw_size)

    title = variety if variety else generic
    if not title:
        title = "Unknown item"

    # Drop redundant " - Variant" suffix when the variant text is
    # already in the size string (catalog has both). Done BEFORE
    # brand prefix so "Beef Mince - Choice Grade" -> "Beef Mince"
    # then becomes "Jimmy's Beef Mince, 500g".
    #
    # Iterates: some catalog rows have multiple chained variants
    # like "Avocados - 1 pc - 190g" where size also has both
    # "1 pc" and "190g". One pass strips "190g" leaving
    # "Avocados - 1 pc"; second pass strips "1 pc" leaving "Avocados".
    for _ in range(4):
        new_title = _strip_redundant_variant_suffix(title, raw_size)
        if new_title == title:
            break
        title = new_title

    # Trim trailing "- ", trailing " -", and trailing standalone
    # connectors so we don't render "Bananas - 4 to,".
    title = re.sub(r"\s*[-:]\s*$", "", title).strip()
    # Strip any trailing partial-word fragments like "4 to" that
    # got captured as a variant suffix.
    title = re.sub(r"\s*-\s*\d+\s+to\s*$", "", title, flags=re.I).strip()

    # Drop the brand when it duplicates the vendor (private-label
    # rendering). Strip " uae" suffix from vendor for matching since
    # we store as "Carrefour UAE" but brand as "Carrefour".
    vendor_brand_form = re.sub(r"\s+uae$", "", vendor_name or "", flags=re.I).strip().lower()
    if brand and brand.lower() == vendor_brand_form:
        brand = ""

    # Prepend brand if it's not already inside title.
    if brand and brand.lower() not in title.lower():
        title = f"{brand} {title}"

    if size:
        title = f"{title}, {size}"

    return title


def _format_price(price, rel_pct, qty):
    """Build the right-side price string.

    Format: "13.99 د.إ (-48%)"            -- price + currency + relative
            "13.99 د.إ × 2 (-48%)"        -- multi-qty
            "13.99 د.إ"                    -- no relative info available

    The relative percentage is wrapped in parens, immediately after
    the price, per the agreed v2.1 format.
    """
    if not price:
        return ""
    base = f"{price:.2f} {_CURRENCY_SYMBOL}"
    if qty and qty > 1:
        base += f" × {qty}"
    if rel_pct is not None and abs(rel_pct) >= 1:
        sign = "+" if rel_pct > 0 else ""
        base += f" ({sign}{rel_pct:.0f}%)"
    return base


def format_cart_message(list_data: dict, member_name: str, target_day: str = None,
                        deliveries: str = None, include_links: bool = True) -> str:
    """
    Build the numbered item list message to send on WhatsApp (v2).

    Designed to be conversational, not corporate. No FOMO deadline --
    the curator decides when to send to stores. Reply commands are
    introduced via plain-language questions, not abstract syntax.

    Args:
        list_data: output of build_frontend_list() -- vendors → categories → items
        member_name: "Sara" / "Max" / etc.
        target_day: "Tuesday" / "Friday" -- otherwise omitted
        deliveries: optional human-readable delivery summary
                    e.g. "Tuesday 7-11am · Wed 9-11am"
        include_links: when True (default) appends the catalog
                    `productUrl` for each item so the reviewer can
                    tap through to the store page. WhatsApp
                    auto-links bare URLs. Set False to keep the
                    message shorter (e.g. for tests).
    """
    # Count + totals across all visible items so we can lead with the
    # bottom line. People read the headline before scanning items.
    item_count = 0
    grand_total = 0.0
    for v in list_data.get("vendors", []):
        for c in v.get("categories", []):
            for item in c.get("items", []):
                if item.get("quantity", 1) <= 0:
                    continue
                item_count += 1
                price = item.get("price") or 0
                qty = item.get("quantity", 1) or 1
                grand_total += price * qty

    lines = []
    lines.append(f"Hey {member_name} 👋")
    lines.append("")

    if item_count == 0:
        day_str = f"{target_day}'s" if target_day else "Your"
        lines.append(f"{day_str} order is empty right now.")
        return "\n".join(lines)

    if target_day:
        lines.append(f"{target_day}'s order: *{item_count} items · ~{grand_total:.0f} {_CURRENCY_SYMBOL}*")
    else:
        lines.append(f"Your order: *{item_count} items · ~{grand_total:.0f} {_CURRENCY_SYMBOL}*")
    lines.append("")

    item_number = 0
    for vendor_group in list_data.get("vendors", []):
        vendor_name = vendor_group.get("name", "Store")
        vendor_items = []
        vendor_total = 0.0
        for category in vendor_group.get("categories", []):
            for item in category.get("items", []):
                if item.get("quantity", 1) <= 0:
                    continue
                vendor_items.append(item)
                price = item.get("price") or 0
                qty = item.get("quantity", 1) or 1
                vendor_total += price * qty
        if not vendor_items:
            continue

        lines.append(f"📦 *{vendor_name}* ({len(vendor_items)} · ~{vendor_total:.0f} {_CURRENCY_SYMBOL})")
        for item in vendor_items:
            item_number += 1
            # v2.3 (2026-05-27): combine title + price + URL on ONE line.
            # Previously the URL was indented on a follow-up line which
            # doubled the visual line-count. WhatsApp auto-links a bare
            # URL anywhere in the line, so appending at the end keeps
            # everything tappable without burning vertical space.
            #
            # Title is a single deduplicated phrase (brand + variety +
            # size), no "*Generic*" prefix. Vendor name is dropped from
            # the per-item line because the vendor header above already
            # groups by store.
            title = _build_item_title(item, vendor_name)
            price_str = _format_price(
                item.get("price"),
                item.get("priceRelativePct"),
                item.get("quantity", 1),
            )
            url = ""
            if include_links:
                url = (
                    item.get("productUrl")
                    or item.get("product_url")
                    or ""
                ).strip()

            parts = [f"{item_number:>2}. {title}"]
            if price_str:
                parts.append(f" — {price_str}")
            if url:
                # Separator is two spaces so the URL is visibly distinct
                # from the price (em-dash) but still on the same line.
                # WhatsApp's auto-linker is whitespace-tolerant.
                parts.append(f"  {url}")
            lines.append("".join(parts))
        lines.append("")

    # Optional delivery info (no FOMO deadline -- the curator decides
    # when to send to stores; we don't put pressure on the reviewer).
    if deliveries:
        lines.append(f"🚚 _{deliveries}_")
        lines.append("")

    # Quick replies block -- teach the command vocabulary by example.
    # Each line is "human question  → quoted command" so reviewers
    # read it as a friendly menu, not a syntax manual.
    lines.append("*Quick replies:*")
    lines.append('Already have item 3?    → "have 3"')
    lines.append('Skip it this time?      → "skip 3"')
    lines.append('Want a different one?   → "swap 3"')
    lines.append('Add something new?      → "add yogurt"')
    lines.append('All good?               → "good"')
    lines.append("")
    lines.append('Multiple items? Chain the numbers: "skip 3 5 7".')
    # NB: removed the "Reply 'undo' within 5 min" line -- the undo
    # handler is still a placeholder, so promising it here is a UX
    # trust bomb. Bring it back the day we ship real undo.

    return "\n".join(lines)


# ─────────────────────────────────────────────
# Category-grouped variant (v3, 2026-05-27)
# ─────────────────────────────────────────────
# UX research found the vendor-grouped wall-of-22-items was undigestible.
# This variant regroups by *category* (Produce / Dairy / Meat / Pantry /
# Drinks / Other) so the eye lands on familiar sections, then one
# numbered line per item with `name · variety · origin · price`. Item
# numbers run globally 1..N so "skip 17" still works regardless of which
# category the item lives in.
#
# Empty fields collapse cleanly: an item with no variety + no origin
# renders as "1. Apple · 8 AED" instead of "1. Apple ·  ·  · 8 AED".

# Display order for category sections. Anything not listed lands in
# "Other" at the end. Order matches grocery-store layout heuristics
# (perishables -> shelf-stable).
_CATEGORY_ORDER = [
    ("Produce", "🥬"),
    ("Dairy", "🥛"),
    ("Meat", "🥩"),
    ("Seafood", "🐟"),
    ("Bakery", "🥖"),
    ("Pantry", "🍞"),
    ("Drinks", "🥤"),
    ("Snacks", "🍪"),
    ("Frozen", "🧊"),
    ("Household", "🧴"),
    ("Other", "📦"),
]


# Country name -> short form for inline display. Anything not listed
# is passed through with a title-case clean. Keep this small and
# focused -- "USA" / "UAE" save real screen space, but most other
# countries (Holland, Greece, Spain) read fine as-is.
_COUNTRY_SHORT = {
    "united arab emirates": "UAE",
    "uae": "UAE",
    "united states": "USA",
    "united states of america": "USA",
    "usa": "USA",
    "u.s.a.": "USA",
    "united kingdom": "UK",
    "great britain": "UK",
    "u.k.": "UK",
    "saudi arabia": "KSA",
    "republic of korea": "South Korea",
    "korea, republic of": "South Korea",
}


def _short_country(raw: str) -> str:
    if not raw:
        return ""
    key = raw.strip().lower()
    if key in _COUNTRY_SHORT:
        return _COUNTRY_SHORT[key]
    # Title-case anything else so "ecuador" -> "Ecuador"
    return raw.strip().title()


# ─────────────────────────────────────────────
# When does country-of-origin help a shopper decide?
# ─────────────────────────────────────────────
# Principle: show origin when it tells you something about the product
# (provenance, farming practice, freshness, terroir). Hide it when
# the product is processed/packaged and origin is just "wherever the
# nearest plant happens to be" -- noise the reader can't act on.
#
# Watermelon: origin matters (taste, season, freshness vary by country)
# Vinegar:    origin doesn't (the bottler being in UAE tells you nothing)
# Olive Oil:  origin matters (terroir, even for big brands)
# Pepsi:      origin doesn't (global brand, identical recipe globally)
#
# The signal we look for: is this minimally-processed AGRICULTURAL produce
# (fresh, whole, dairy, meat, eggs, seafood) where supply chain reveals
# product character? Or is it a packaged / manufactured / branded SKU
# where the brand IS the identity?
#
# Implementation: a deterministic rule with three layers of judgment,
# applied in order:
#   1. Per-item override (the most specific signal -- olive oil, honey,
#      saffron, vinegar, salt). Wins over everything else.
#   2. Brand-level override (global brands suppress regardless of cat).
#   3. Category default (Produce/Meat/Dairy/Seafood/Eggs -> show;
#      Drinks/Snacks/Condiments -> hide).
#
# When unsure, default to showing -- under-information is worse than
# minor noise. Future improvement: LLM-evaluated per-item flag cached
# in the items table once, so the system can adjudicate edge cases like
# "is balsamic vinegar's origin meaningful?" without a hand-curated list.

# Categories where origin almost always carries signal.
_CATEGORIES_ORIGIN_SHOWS = {
    "produce", "fruits", "fruit", "vegetables", "vegetable",
    "meat", "poultry", "beef", "lamb", "chicken",
    "seafood", "fish", "shellfish",
    "dairy", "milk", "cheese", "yoghurt", "yogurt", "eggs",
    "herbs",
}

# Categories where origin is almost always noise.
_CATEGORIES_ORIGIN_HIDES = {
    "drinks", "drink", "beverages", "beverage", "soft drinks",
    "snacks", "snack", "confectionery", "chocolate", "candy",
    "condiments", "sauces", "spreads",
    "household", "cleaning", "personal care",
    "frozen processed", "ready meals",
}

# Per-item overrides keyed by lowercase canonical (generic) name.
# These resolve the cases where the category default gets it wrong.
# Items not listed fall through to the category rule.
_ITEM_ORIGIN_OVERRIDES = {
    # SHOW (origin carries meaning even in a category that hides by default)
    "olive oil":          True,    # terroir matters even for big labels
    "balsamic vinegar":   True,    # Modena PGI is real signal
    "wine vinegar":       True,
    "honey":              True,    # provenance varies wildly
    "saffron":            True,
    "tea":                True,
    "coffee":             True,
    "wine":               True,
    "rice":               True,    # basmati / jasmine / arborio vary
    "pasta":              True,    # Italian carries weight
    # HIDE (the item is so processed origin is just plant location)
    "vinegar":            False,   # generic white/cider vinegar
    "salt":               False,
    "sugar":              False,
    "flour":              False,
    "baking soda":        False,
    "baking powder":      False,
    "soft drink":         False,
    "carbonated soft drink": False,
}

# Brands where country-of-origin is uninformative regardless of
# category -- global packaged brands. Kept narrow; the category and
# per-item rules handle most cases first.
_GLOBAL_BRANDS_NO_ORIGIN = {
    "pepsi", "coca cola", "coca-cola", "coke", "sprite", "fanta",
    "7up", "7 up", "mountain dew", "dr pepper", "schweppes",
    "red bull", "monster", "gatorade", "powerade",
    "nestle", "nescafe", "kellogg's", "kelloggs",
    "heinz", "knorr", "maggi", "kraft", "mars", "snickers",
    "lay's", "lays", "doritos", "pringles", "oreo",
}


# Variety tokens that ACTUALLY differentiate a global-brand SKU --
# things a shopper might care about. Keep these when the brand is in
# the global set. Lowercase keys, matched token-by-token.
_USEFUL_VARIETY_TOKENS = {
    # Sugar / diet
    "zero", "diet", "light", "lite", "no", "sugar", "free",
    "max",
    # Flavor variants
    "cherry", "vanilla", "lime", "lemon", "mango",
    "wild", "raspberry", "strawberry", "orange", "peach",
    # Size / pack notations that genuinely matter
    "mini", "family", "value",
}

# Variety tokens that are SKU-internal codes a shopper can't act on.
# Pepsi "Black" is the regular variant under a marketing codename;
# Coke "Premium" is meaningless on a soda. Drop these.
_NOISE_VARIETY_TOKENS = {
    "black", "premium", "special", "select", "standard",
    "classic", "original", "regular",   # default = no info
    "edition", "limited",
}


def _filter_variety_for_global_brand(variety: str) -> str:
    """For a global packaged brand (Pepsi/Coke/etc.), drop variety
    tokens that don't help a shopper distinguish products. Keep only
    the tokens that signal something meaningful (Zero, Diet, Cherry,
    Vanilla, etc.).

    Returns the filtered variety string, or '' if nothing survives.
    """
    if not variety:
        return ""
    keep = []
    for tok in variety.split():
        low = tok.lower().strip(",.-")
        if low in _NOISE_VARIETY_TOKENS:
            continue
        keep.append(tok)
    out = " ".join(keep).strip()
    # If the surviving tokens are ALL non-informative (none in the
    # "useful" set), suppress entirely. Otherwise return the kept text.
    if not any(t.lower().strip(",.-") in _USEFUL_VARIETY_TOKENS for t in out.split()):
        return ""
    return out


def _origin_should_show(
    generic_name: str,
    brand: str,
    category: str,
    *,
    llm_verdict: Optional[int] = None,
) -> bool:
    """The judgment call: does showing country-of-origin help a reader
    make a decision about THIS product?

    Layered logic in priority order:
      0. LLM verdict from items.origin_signal_llm (most informed signal,
         when present). 1 = show, 0 = hide, None = no verdict yet.
      1. Per-item override (hand-curated specifics)
      2. Brand-level override (global mass-market brand)
      3. Category default
      4. Conservative fallback: show

    The LLM verdict is the TOP priority because it's evaluated per-item
    with full context (name, category, notes) and reflects current
    judgment, while the static tables only capture broad patterns.
    """
    # Highest-priority signal: per-item LLM verdict
    if llm_verdict is not None:
        return bool(llm_verdict)

    gn = (generic_name or "").lower().strip()
    if gn in _ITEM_ORIGIN_OVERRIDES:
        return _ITEM_ORIGIN_OVERRIDES[gn]

    br = (brand or "").lower().strip()
    if br in _GLOBAL_BRANDS_NO_ORIGIN:
        return False

    cat = (category or "").lower().strip()
    if cat in _CATEGORIES_ORIGIN_HIDES:
        return False
    if cat in _CATEGORIES_ORIGIN_SHOWS:
        return True

    # Unknown / ambiguous category -- default to showing.
    return True


def _clean_size(size: str) -> str:
    """Reduce the size field to JUST the measurement.

    Catalog size strings often carry descriptor prefixes that duplicate
    the variety field once the per-category layout puts them side by
    side: "Full Fat - 280ml", "Grass Fed - 500g", "Pitted -290g",
    "Yellow - 250g", "Black - 155ml". We keep only the measurement
    segment (weight/volume preferred, else piece-count):

      "Full Fat - 280ml"      -> "280ml"
      "Grass Fed - 500g"      -> "500g"
      "1kg - 5 to 6 pieces"   -> "1kg"
      "5 to 7 pieces"         -> "5 to 7 pieces"   (no weight, keep count)
      "Approx 250g"           -> "250g"
    """
    if not size:
        return ""
    import re
    s = re.sub(r"\b(approx(?:imately)?)\b\.?", "", size, flags=re.IGNORECASE)
    # Split on dash separators and keep only segments that contain a
    # number -- those are the measurements. Descriptor-only segments
    # ("Full Fat", "Yellow", "Pitted") get dropped.
    segments = [seg.strip() for seg in re.split(r"\s*-\s*", s) if seg.strip()]
    digit_segs = [seg for seg in segments if re.search(r"\d", seg)]
    if not digit_segs:
        return re.sub(r"\s{2,}", " ", s).strip(" -·,")
    # Prefer a weight/volume segment over a piece-count when both exist.
    weight_segs = [
        seg for seg in digit_segs
        if re.search(r"\d+(?:\.\d+)?\s*(kg|g|ml|l|oz|lb)\b", seg, re.IGNORECASE)
    ]
    out = weight_segs[0] if weight_segs else digit_segs[0]
    return re.sub(r"\s{2,}", " ", out).strip(" -·,")


def _clean_variety(variety: str, generic_name: str) -> str:
    """Strip the generic item name from the variety descriptor and
    collapse whitespace, so "Fresh Cucumbers" (variety) for a Cucumber
    item becomes just "Fresh".

    Also strips known size noise that leaked through _strip_brand_and_size
    (e.g. "5 to 7 Pieces Royal Gala Apples" -> "Royal Gala") and
    catalog noise like 'Approx' tokens.
    """
    if not variety:
        return ""
    import re
    v = variety.strip()

    # 1. Drop 'Approx' tokens.
    v = re.sub(r"\b(approx(?:imately)?)\b\.?", "", v, flags=re.IGNORECASE)

    # 2. Strip the generic item name (and plural) anywhere it appears,
    #    so "Royal Gala Apples" -> "Royal Gala".
    if generic_name:
        pat = re.compile(r"\b" + re.escape(generic_name) + r"s?\b", re.IGNORECASE)
        v = pat.sub("", v)
        # Handle the yogurt/yoghurt spelling split: if the item is
        # yogurt, also strip the British "yoghurt" (and vice versa),
        # so "Greek Yoghurt Plain" -> "Greek Plain".
        gl = generic_name.lower()
        if "yog" in gl:
            v = re.sub(r"\byogh?urts?\b", "", v, flags=re.IGNORECASE)

    # 3. Strip ALL size / quantity / pack tokens, ANYWHERE in the string
    #    (not just start/end). The size is already its own column, so any
    #    number+unit, piece-count, or range that survived into the variety
    #    is pure duplicated noise:
    #      "Organic 500G - 5 To"      -> "Organic"
    #      "Royal Gala - 5 To"        -> "Royal Gala"
    #      "Organic 1Kg - 5 To"       -> "Organic"
    #      "Free Range 15 PCS"        -> "Free Range"
    #    Applied in order: weight/volume units, then piece-counts, then
    #    numeric ranges, then orphaned range/unit words, then bare numbers.
    # weight / volume:  500g  1kg  200ml  1l  18kg  1.5l
    v = re.sub(r"\b\d+(?:\.\d+)?\s*(?:kg|g|ml|l|oz|lb)\b", " ", v, flags=re.IGNORECASE)
    # piece counts:  12 pcs   5 pieces   1 pc
    v = re.sub(r"\b\d+\s*(?:pieces?|pcs?|pc)\b", " ", v, flags=re.IGNORECASE)
    # numeric ranges + dangling "N to":  5 to 7   5-7   5 to
    v = re.sub(r"\b\d+\s*(?:to|-)\s*\d*\b", " ", v, flags=re.IGNORECASE)
    # bare standalone numbers
    v = re.sub(r"\b\d+(?:\.\d+)?\b", " ", v)
    # orphaned unit / range / pack words left behind
    v = re.sub(r"\b(?:to|pieces?|pcs?|pc|pack|packs|x|kg|g|ml|l|oz|lb)\b",
               " ", v, flags=re.IGNORECASE)

    # 4. Collapse whitespace + connector debris. After token removal we
    #    can be left with orphaned separators in the MIDDLE too, e.g.
    #    "Rosemary Butter - - + Butter". Collapse any run of connector
    #    chars (optionally space-separated) into a single space.
    v = re.sub(r"\s+", " ", v).strip()
    v = re.sub(r"(?:\s*[-+·&/|,]\s*)+", " ", v)   # internal connector runs
    v = re.sub(r"\s{2,}", " ", v).strip()
    v = v.strip(" ,-+·.&/|")

    # 5. Title-case ("fresh" -> "Fresh") while preserving short all-caps
    #    acronyms (USA, UAE, KSA, PDO).
    if v:
        out = []
        for t in v.split(" "):
            if t.isupper() and len(t) <= 4:
                out.append(t)
            else:
                out.append(t.title())
        v = " ".join(out)
    return v


# ─────────────────────────────────────────────
# Per-category display schema (the "one rule doesn't fit all" table)
# ─────────────────────────────────────────────
# Each category lists the slots to render, in order, between the item
# name and the price. Slot vocabulary:
#   "variety"  cleaned descriptor (Royal Gala / Greek / Extra Virgin /
#              Grass-Fed) -- the same field reads as type/grade/variant
#              depending on category; we just call it variety.
#   "brand"    manufacturer (Kibsons / Bertolli / Al Safi)
#   "origin"   short country, gated by _origin_should_show
#   "size"     pack/weight, gated by _size_matters
#
# Refined with the household 2026-05-28:
#   Produce  -> variety + origin, size when it varies (fruits yes,
#               leafy greens no). NO brand (store-brand, irrelevant).
#   Dairy    -> brand + variety. NO origin (brand encodes it).
#   Meat     -> variety(grade+cut) + origin + size. NO brand.
#   Seafood  -> same as meat.
#   Pantry   -> dry goods: variety(type) + brand.
#               oils: variety + brand + origin (terroir matters).
#   Drinks   -> brand + variety(variant) + size. NO origin.
#   Bakery   -> variety(type) + size.
_CATEGORY_SLOTS = {
    "produce":   ["variety", "size", "origin"],
    "fruits":    ["variety", "size", "origin"],
    "fruit":     ["variety", "size", "origin"],
    "vegetables":["variety", "size", "origin"],
    "dairy":     ["brand", "variety", "size"],
    "meat":      ["variety", "origin", "size"],
    "poultry":   ["variety", "origin", "size"],
    "seafood":   ["variety", "origin", "size"],
    "fish":      ["variety", "origin", "size"],
    "pantry":    ["variety", "brand", "size"],          # dry goods
    "pantry_oil":["variety", "brand", "origin", "size"],  # oils/vinegars
    "bakery":    ["variety", "size"],
    "drinks":    ["brand", "variety", "size"],
    "beverages": ["brand", "variety", "size"],
    "snacks":    ["brand", "variety", "size"],
    "other":     ["variety", "brand", "size"],
}
_DEFAULT_SLOTS = ["variety", "brand", "size", "origin"]

# Items where pack size is a standard small packet and adds no decision
# value -- leafy greens and fresh herbs. For these the "size" slot is
# suppressed even in produce. Fruits/roots (apple, potato, watermelon)
# vary 200g-2kg, so they keep size.
_SIZE_RARELY_MATTERS = {
    "spinach", "kale", "rocket", "arugula", "lettuce", "watercress",
    "chard", "dill", "parsley", "coriander", "cilantro", "mint",
    "basil", "thyme", "rosemary", "sage", "oregano", "chives",
}


def _size_matters(generic_name: str) -> bool:
    """Whether to show pack size for this item. Leafy greens / herbs
    come in standard small packets -> size adds nothing. Everything
    else (fruits, roots, dairy, meat, drinks) can vary meaningfully."""
    return (generic_name or "").lower().strip() not in _SIZE_RARELY_MATTERS


def _pantry_is_oil(generic_name: str) -> bool:
    g = (generic_name or "").lower()
    return "oil" in g or "vinegar" in g


def _item_line_category(idx, item: dict, currency: str = "AED") -> str:
    """Render one item line for the category-grouped format, following
    the per-category slot schema in _CATEGORY_SLOTS.

    The category decides WHICH attributes are shown and in what order --
    produce leads with variety+origin and hides brand, dairy leads with
    brand and hides origin, etc. Empty/suppressed slots collapse.

    Pass idx=None to omit the "N. " number prefix (used by the add
    confirmation, which shows a single item outside the numbered list).
    """
    # Prefer the canonical genericName ("Cucumber") over the matched
    # product_name ("Carrefour Fresh Cucumbers 500g") which already
    # bundles brand and size.
    generic = (item.get("genericName") or "").strip()
    fallback_name = (item.get("name") or "").strip()
    display_name = generic or fallback_name

    size = _clean_size(item.get("size") or "")
    brand = (item.get("brand") or "").strip()
    raw_variety = (item.get("variety") or "").strip()
    variety = _clean_variety(raw_variety, generic or fallback_name)
    is_organic = bool(item.get("isOrganic"))
    origin_raw = _short_country(
        item.get("originCountry") or item.get("origin_country") or ""
    )
    cat_key = (item.get("_category") or item.get("category") or "").lower().strip()

    # Apply the relevance judgment: is origin signal or noise for this
    # specific product? Prefer the per-item LLM verdict
    # (items.origin_signal_llm) when available; fall back to the static
    # rules. See _origin_should_show for the full layered logic.
    llm_verdict = item.get("originSignalLlm")
    if llm_verdict is None:
        llm_verdict = item.get("origin_signal_llm")
    origin = origin_raw if _origin_should_show(
        generic, brand, cat_key, llm_verdict=llm_verdict
    ) else ""

    # Same idea applied to variety. For global packaged brands, only
    # keep variety tokens that actually distinguish the product
    # (Zero/Diet/Cherry/Vanilla -- meaningful). Drop SKU-internal cruft
    # like "Black" (Pepsi's codename for regular), "Special", "Premium",
    # which look like info but tell the reader nothing.
    brand_key = brand.lower().strip() if brand else ""
    if brand_key in _GLOBAL_BRANDS_NO_ORIGIN and variety:
        variety = _filter_variety_for_global_brand(variety)

    price = item.get("price")
    qty = item.get("quantity", 1) or 1

    # If the catalog flags it organic but "Organic" isn't already in
    # the variety string, prepend it. This surfaces the household's
    # preferred attribute in the right place.
    if is_organic and "organic" not in variety.lower():
        variety = ("Organic " + variety).strip() if variety else "Organic"

    # Drop a variety that echoes the name. A free-text add can set the
    # generic to the full phrase ("Organic Red Cabbage"), and the variety
    # (product-name strip + organic prepend) can reproduce it verbatim ->
    # "Organic Red Cabbage · Organic Red Cabbage". If every variety word
    # is already in the display name, the variety adds nothing.
    if variety:
        import re as _re
        _dn = set(_re.findall(r"[a-z]+", display_name.lower()))
        _vw = set(_re.findall(r"[a-z]+", variety.lower()))
        if _vw and _vw <= _dn:
            variety = ""

    # Resolve the category's slot template. Pantry splits into oil vs
    # dry-goods (oils show origin, dry goods don't).
    slot_cat = cat_key
    if cat_key in ("pantry", "pantry-dry", "dry goods") and _pantry_is_oil(generic):
        slot_cat = "pantry_oil"
    slots = _CATEGORY_SLOTS.get(slot_cat, _DEFAULT_SLOTS)

    # Suppress size for standard-packet greens/herbs.
    if not _size_matters(generic):
        size = ""

    # Build each slot's value, then assemble in the category's order.
    # variety + origin still merge into one "Royal Gala, UAE" chunk when
    # they're adjacent in the slot order; otherwise they stand alone.
    slot_values = {
        "variety": variety,
        "brand": brand,
        "origin": origin,
        "size": size,
    }

    parts = [display_name]
    i = 0
    while i < len(slots):
        slot = slots[i]
        val = slot_values.get(slot, "")
        # Merge an adjacent variety+origin pair into "variety, origin".
        if slot == "variety" and val and i + 1 < len(slots) \
                and slots[i + 1] == "origin" and slot_values.get("origin"):
            parts.append(f"{val}, {slot_values['origin']}")
            i += 2
            continue
        if val:
            parts.append(val)
        i += 1

    if price:
        if qty and qty > 1:
            parts.append(f"{price:.0f} {currency} × {qty}")
        else:
            parts.append(f"{price:.0f} {currency}")
    detail = " · ".join(parts)
    if idx is None:
        return detail
    return f"{idx:>2}. " + detail


def format_cart_message_category(
    list_data: dict,
    member_name: str,
    target_day: str = None,
    deliveries: str = None,
    currency: str = "AED",
) -> str:
    """Category-grouped variant of format_cart_message.

    Same input shape as format_cart_message (vendors -> categories -> items)
    but reorganizes the output so all items roll up by category name,
    ignoring vendor at the top level. Vendor info isn't lost (it's still
    on each item dict if needed) -- it's just not the organizing axis.

    No per-item URLs. Per the 2026-05-27 product call: the URL clutter
    outweighs the convenience for routine reviewers.
    """
    # Flatten: pull every item out with its category attached.
    flat = []
    for v in list_data.get("vendors", []):
        for c in v.get("categories", []):
            cat_name = c.get("name") or "Other"
            for it in c.get("items", []):
                if it.get("quantity", 1) <= 0:
                    continue
                flat.append({**it, "_category": cat_name})

    if not flat:
        day_str = f"{target_day}'s" if target_day else "Your"
        return f"Hey {member_name} 👋\n\n{day_str} order is empty right now."

    # Totals across all items
    grand_total = 0.0
    for it in flat:
        grand_total += (it.get("price") or 0) * (it.get("quantity", 1) or 1)

    # Group by category, preserving the display order above.
    by_cat = {name: [] for name, _ in _CATEGORY_ORDER}
    for it in flat:
        cat = it["_category"] if it["_category"] in by_cat else "Other"
        by_cat[cat].append(it)

    lines = [f"Hey {member_name} 👋", ""]
    headline_count = len(flat)
    if target_day:
        lines.append(f"{target_day}'s order: *{headline_count} items · ~{grand_total:.0f} {currency}*")
    else:
        lines.append(f"Your order: *{headline_count} items · ~{grand_total:.0f} {currency}*")
    lines.append("")

    item_number = 0
    for cat_name, emoji in _CATEGORY_ORDER:
        items = by_cat.get(cat_name) or []
        if not items:
            continue
        cat_total = sum(
            (it.get("price") or 0) * (it.get("quantity", 1) or 1)
            for it in items
        )
        lines.append(
            f"{emoji} *{cat_name}* ({len(items)} · {cat_total:.0f} {currency})"
        )
        for it in items:
            item_number += 1
            lines.append(_item_line_category(item_number, it, currency=currency))
        lines.append("")

    if deliveries:
        lines.append(f"🚚 _{deliveries}_")
        lines.append("")

    # Compact reply hint. Full menu still works via the parser.
    lines.append("Reply *ok* to confirm, or change anything:")
    lines.append('  "skip 7" / "have 12" / "swap 3" / "add yogurt"')

    return "\n".join(lines)


def format_confirm_add(items: list[dict]) -> str:
    """Build a confirmation message for added items.

    Note: no trailing "Anything else?" prompt here -- the dispatcher
    appends a single `format_round_status_footer` to the full reply
    (running total + next-step nudge) so multi-action messages get
    one coherent close instead of repeated "Anything else?" lines.
    """
    if not items:
        return "Nothing to add."
    lines = ["Got it! Adding:"]
    for item in items:
        name = item.get("resolved_name") or item.get("item_text", "Unknown")
        price = item.get("price")
        price_str = f" — {price:.2f} {_CURRENCY_SYMBOL}" if price else ""
        lines.append(f"- {name}{price_str}")
    return "\n".join(lines)


def format_add_result(added: list[dict], not_found: list[str] = None,
                      currency: str = "AED") -> str:
    """Reply to an 'add' command: a STRAIGHT answer per item -- either
    "added it" with the product detail (same per-category slot logic as
    the list line: produce shows variety/origin, dairy shows brand, etc.,
    less-is-more) or "couldn't find a good match". Never re-renders the
    whole list.

    Args:
        added: enriched item dicts (build_frontend_list shape -- carry
            genericName/variety/originCountry/size/isOrganic/price and
            either `_category` or `category`). Each may carry an optional
            `_add_note` (e.g. an organic downgrade) shown in parentheses.
        not_found: raw texts we could not resolve to a clean product.
    """
    not_found = not_found or []
    lines = []

    for it in added:
        # _item_line_category needs the category on `_category`; the
        # frontend item carries it on `category` (already simplified).
        item = dict(it)
        if "_category" not in item:
            item["_category"] = item.get("category") or "Other"
        detail = _item_line_category(None, item, currency=currency)
        note = (it.get("_add_note") or "").strip()
        suffix = f"  _({note})_" if note else ""
        lines.append(f"Added 👍  {detail}{suffix}")

    for nf in not_found:
        lines.append(
            f"Couldn't find a good match for *{nf}* — want me to try a "
            f"different name, or leave it off?"
        )

    if not lines:
        return "Nothing to add."
    return "\n".join(lines)


def format_round_status_footer(
    item_count: int,
    total_amount: float | None = None,
    currency_sym: str | None = None,
) -> str:
    """One-line cart-status footer appended after a successful action.

    Renders as e.g.:

        Cart now: 19 items · 654.50 د.إ
        Anything else? Reply *good* to send.

    Caller decides when to attach this -- the formatter is pure. The
    dispatcher attaches it whenever an action mutated the cart so the
    user always sees the running total + a single explicit prompt
    instead of every formatter trying to remind them what to do next.
    """
    sym = currency_sym or _CURRENCY_SYMBOL
    bits = [f"{item_count} item" + ("s" if item_count != 1 else "")]
    if isinstance(total_amount, (int, float)) and total_amount > 0:
        bits.append(f"{total_amount:.2f} {sym}")
    head = "Cart now: " + " · ".join(bits)
    return (
        f"{head}\n"
        f"Anything else? Reply *good* to send."
    )


def format_skip_confirm(items: list[tuple]) -> str:
    """Affirmation for one or more Tier-0 skips.

    Args:
        items: list of (item_name, ref_number) tuples in display order.
    """
    if not items:
        return "Nothing to skip."
    if len(items) == 1:
        name, num = items[0]
        return (
            f"✓ Skipped *{name}* (#{num}). Back next cycle."
        )
    lines = [f"✓ Skipped {len(items)} items:"]
    for name, num in items:
        lines.append(f"  #{num} *{name}*")
    lines.append(f"\nAll {len(items)} back next cycle.")
    return "\n".join(lines)


def format_have_it_confirm(items: list[tuple]) -> str:
    """Affirmation for one or more 'I have it' upgrades.

    Args:
        items: list of (item_name, ref_number) tuples.
    """
    if not items:
        return "Nothing to mark."
    if len(items) == 1:
        name, num = items[0]
        return (
            f"✓ Marked *{name}* (#{num}) as already bought.\n"
            f"Won't suggest until next natural cycle."
        )
    lines = [f"✓ Marked {len(items)} as already bought:"]
    for name, num in items:
        lines.append(f"  #{num} *{name}*")
    lines.append(f"\nNone will be suggested until their next natural cycle.")
    return "\n".join(lines)


def format_swap_options(generic_name: str, ref_number: int, alternatives: list) -> str:
    """Render up to 3 alternatives as A / B / C choices.

    alternatives: list of dicts from find_alternatives_for_item -- each
    has name, brand, size, price, vendor, isOrganic, scoreTier.
    """
    if not alternatives:
        return (
            f"⚠️ Couldn't find alternatives for *{generic_name}* (#{ref_number}) right now. "
            f"You can reply 'skip {ref_number}' to drop it, or 'add <name>' to describe what you'd prefer."
        )
    letters = ["A", "B", "C", "D", "E"]
    lines = [
        f"Here are alternatives for *{generic_name}* (#{ref_number}):",
        "",
    ]
    for i, alt in enumerate(alternatives[:5]):
        letter = letters[i]
        name = alt.get("name", "Unknown")
        brand = alt.get("brand", "")
        size = alt.get("size", "")
        price = alt.get("price")
        vendor = alt.get("vendor", "")
        organic = alt.get("isOrganic")
        sub_parts = [p for p in [brand, size, vendor] if p]
        sub = " · ".join(sub_parts)
        price_str = f"{price:.2f} {_CURRENCY_SYMBOL}" if price else ""
        tag = " 🌱" if organic else ""
        lines.append(f"*{letter}.* {name}{tag} — {price_str}")
        if sub:
            lines.append(f"    {sub}")
        # Tappable product link so the reviewer can verify before picking.
        url = (alt.get("productUrl") or alt.get("product_url") or "").strip()
        if url:
            lines.append(f"    {url}")
    lines.append("")
    lines.append(f"Reply *{'/'.join(letters[:len(alternatives[:5])])}* to pick, or *skip {ref_number}* to drop the item.")
    return "\n".join(lines)


def format_swap_accepted(generic_name: str, picked_name: str, ref_number: int) -> str:
    return (
        f"✓ Swapped *{generic_name}* (#{ref_number}) to *{picked_name}*. "
        f"I'll remember this for next time."
    )


def parse_alternative_choice(text: str) -> int | None:
    """Parse a reviewer's pick from A/B/C/D/E or 1/2/3/4/5.

    Returns 0-indexed offset, or None if not a single-letter/number choice.
    """
    t = (text or "").strip().upper()
    if not t:
        return None
    # Single letter A-E
    if len(t) == 1 and t in "ABCDE":
        return ord(t) - ord("A")
    # Single number 1-5
    if t.isdigit():
        n = int(t)
        if 1 <= n <= 5:
            return n - 1
    # "option A", "the second one", etc could be added later
    return None


def format_out_of_range(ref_number: int, max_number: int) -> str:
    """Reply when the user references a number that's not on the list."""
    return (
        f"⚠️ I only see {max_number} items on this list — couldn't find #{ref_number}. "
        f"Take another look and try a different number."
    )


def format_needs_number(verb: str) -> str:
    """User typed a command verb without a number. Render a verb-
    specific 'I need a number' hint rather than guessing what they
    meant. Found in QA: 'skip the milk' previously got fuzzy-removed
    'milk' (phantom item not on the list) -- now we ask for clarity.
    """
    verb = (verb or "").lower()
    canon_map = {
        "skip": "skip", "remove": "skip", "drop": "skip",
        "have": "have", "had": "have", "got": "have",
        "stop": "stop", "never": "stop",
        "swap": "swap", "change": "swap", "switch": "swap", "replace": "swap",
    }
    canon = canon_map.get(verb, verb)
    blurb = {
        "skip": "drop item #N this cycle",
        "have": "tell me you already have item #N",
        "stop": "permanently stop suggesting #N",
        "swap": "see alternatives for #N",
    }.get(canon, "act on item #N")
    return (
        f"I think you want to *{canon}* something — I need a number.\n"
        f"  *{canon} 3* — {blurb}\n"
        f"  *{canon} 1 3 5* — multiple items at once\n\n"
        f"_Item numbers come from the list I sent above._"
    )


def format_budget_exceeded(reason: str = "daily_cap") -> str:
    """User-facing message when the LLM was skipped due to the cost cap.

    Crucially distinct from the generic "I don't understand" reply --
    the user (and we) need to know the cap is the cause so we don't
    chase a parser bug that doesn't exist.

    reason: 'daily_cap' | 'monthly_cap' | 'db_error'
    """
    if reason == "monthly_cap":
        when = "this month"
    elif reason == "db_error":
        when = "(internal error reading the budget table)"
    else:
        when = "today"
    return (
        f"⚠️ I couldn't parse that with the AI helper — my budget for "
        f"{when} is used up. I'll be back to free-text understanding "
        f"once the cap resets.\n\n"
        f"For now, the structured commands still work:\n"
        f"  *skip 3*   — drop item #3 this cycle\n"
        f"  *have 3*   — already bought it\n"
        f"  *swap 3*   — show me alternatives\n"
        f"  *stop 3*   — never suggest #3 again\n"
        f"  *good*     — looks fine, send to stores"
    )


def format_tier2_ask(item_name: str, drop_count: int) -> str:
    """One-time Tier-2 ask after the pattern detector flags 3-4 repeated drops.

    Sent outside any feedback round, so the reply state lives on
    household_members.member_pending_intent rather than the round-scoped
    feedback_member_status.pending_intent.

    Reply options are bare keywords (no item-number) because there's no
    list context — feedback_manager.handle_member_reply routes a bare
    "stop"/"keep"/"later" to the tier2 handler when a pending ask is set.
    """
    return (
        f"Hey — I've noticed *{item_name}* has been dropped {drop_count} times "
        f"in the last month.\n\n"
        f"Want me to stop suggesting it?\n\n"
        f"Reply:\n"
        f"  *stop*   — yes, take it off future lists\n"
        f"  *keep*   — no, keep suggesting (I'll reset the counter)\n"
        f"  *later*  — ask me again next time it comes up\n\n"
        f"_If you ignore this, I'll keep nudging the cadence but won't ask again._"
    )


def format_tier2_resolved(item_name: str, decision: str) -> str:
    """Confirmation after a Tier-2 reply lands.

    decision: 'stop' | 'keep' | 'later'
    """
    if decision == "stop":
        return (
            f"✓ Got it — I'll stop suggesting *{item_name}*. "
            f"Approve it on any future list to bring it back."
        )
    if decision == "keep":
        return (
            f"✓ Keeping *{item_name}* on the list. "
            f"I've reset the drop counter."
        )
    if decision == "later":
        return (
            f"✓ Noted — I'll ask again next time *{item_name}* "
            f"keeps getting dropped."
        )
    # Defensive — should not happen given parse_tier2_reply's allowlist.
    return f"✓ Got it about {item_name}."


_TIER2_REPLY_TOKENS = {
    "stop": "stop",
    "yes": "stop",
    "never": "stop",
    "remove": "stop",
    "keep": "keep",
    "no": "keep",
    "wait": "keep",
    "later": "later",
    "remind": "later",
    "ask": "later",
}


def parse_tier2_reply(text: str) -> str | None:
    """Map a Tier-2 reply to a canonical decision: 'stop' | 'keep' | 'later'.

    Only fires when household_members.member_pending_intent indicates a
    tier2_ask is open — otherwise the regular verb dispatcher handles
    "stop 3" (verb + number) as a Tier-0 stop-suggesting action on that
    list item.

    Returns None if the text isn't a recognized Tier-2 keyword.
    """
    if not text:
        return None
    # Accept the first non-empty token, lowercased. "Stop please" -> "stop".
    tok = text.strip().lower().split()[0] if text.strip() else ""
    # Strip trailing punctuation.
    tok = tok.rstrip(".!?,;:")
    return _TIER2_REPLY_TOKENS.get(tok)


# ─────────────────────────────────────────────
# B4: today's-deliveries morning heads-up
# ─────────────────────────────────────────────

def format_today_deliveries(orders_today: list[dict]) -> str | None:
    """B4 — bundled morning summary of all deliveries expected today.

    Returns None when there are no orders to surface (caller should
    skip the send rather than message an empty heads-up).

    Each order dict expected to have:
      store_name, status, expected_by, delivery_type (optional),
      order_ref (optional).
    """
    if not orders_today:
        return None
    n = len(orders_today)
    plural = "y" if n == 1 else "ies"
    lines = [f"🌅 *Today's deliver{plural}* ({n})"]
    lines.append("")
    for o in orders_today:
        store = o.get("store_name") or "Order"
        expected = o.get("expected_by") or ""
        # If expected_by is YYYY-MM-DD HH:MM, render the time only.
        # If just date, show the whole field; the user already knows
        # it's today.
        time_bit = ""
        if expected and " " in expected:
            time_bit = expected.split(" ", 1)[1]
        delivery_type = (o.get("delivery_type") or "").lower()
        type_tag = " · express (NOW)" if delivery_type == "now" else ""
        status = o.get("status") or "expected"
        # If status already says out_for_delivery, surface that
        # specifically -- the user can act on it differently.
        if status.lower() in {"out_for_delivery", "dispatched"}:
            status_tag = " 🚚 out for delivery"
        elif status.lower() in {"processing", "preparing"}:
            status_tag = " 🛠️ being prepared"
        else:
            status_tag = ""
        if time_bit:
            lines.append(f"  📦 *{store}* — {time_bit}{type_tag}{status_tag}")
        else:
            lines.append(f"  📦 *{store}*{type_tag}{status_tag}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# B2: order-placed notification
# ─────────────────────────────────────────────

def format_order_placed(report: dict) -> str:
    """Build a WhatsApp summary for when cart_build completes.

    Shows what the curator ultimately submitted, per store, with
    items added / failed counts and the estimated total.

    `report` is the dict returned by cart_builder.build_cart with
    shape:

        {
          "list_id": int,
          "stores": {
            "Carrefour UAE": {
              "items_added": N, "items_requested": M,
              "items_failed": K, "estimated_total": float,
              "added": [{"item_name", "product_name", "qty", "price"}, ...],
              "failed": [{"item_name", "reason", ...}],
            },
            ...
          },
        }

    The "delta vs reviewed" callout from the punch-list spec is
    deferred -- we'd need a reviewed-state snapshot (separate from
    `suggestion_snapshots` which captures the AI's original
    suggestion) before we can render meaningful diffs. The
    submitted summary is the high-value piece for now.
    """
    stores_data = report.get("stores", {}) or {}
    if not stores_data:
        return "✅ *Order placed*\n\n_(no store summary available)_"

    grand_total = 0.0
    grand_added = 0
    grand_failed = 0
    grand_existing = 0
    grand_rescued = 0
    blocks = []
    for store_name, sr in stores_data.items():
        added_n = sr.get("items_added", 0) or 0
        failed_n = sr.get("items_failed", 0) or 0
        total = sr.get("estimated_total", 0) or 0
        grand_total += total
        grand_added += added_n
        grand_failed += failed_n

        head = f"📦 *{store_name}*"
        sub_bits = []
        if added_n:
            plural = "" if added_n == 1 else "s"
            sub_bits.append(f"{added_n} item{plural} added")
        if isinstance(total, (int, float)) and total > 0:
            sub_bits.append(f"{total:.2f} {_CURRENCY_SYMBOL}")
        sub = " · ".join(sub_bits) if sub_bits else "no items added"
        lines = [head, sub]

        # Cross-vendor rescues that LANDED in this store. Each added
        # item carries a `rescued_from` key when the cart_builder
        # picked it up here after another store couldn't fulfill.
        # Show inline so the curator sees the redirect without
        # leaving WhatsApp.
        rescued = [a for a in (sr.get("added") or [])
                   if a.get("rescued_from")]
        if rescued:
            grand_rescued += len(rescued)
            for r in rescued[:3]:
                source = r.get("rescued_from", "?")
                name = r.get("item_name") or r.get("product_name", "?")
                lines.append(
                    f"  🔁 {name} — moved from *{source}* (out of stock there)"
                )
            if len(rescued) > 3:
                more = len(rescued) - 3
                lines.append(
                    f"  _+ {more} more cross-vendor rescue"
                    f"{'s' if more != 1 else ''} in the app_"
                )

        # Pre-existing cart items: items already there from a
        # previous session (or a manual add via the store website).
        # We didn't clear them; the user will see them at checkout
        # alongside today's adds. Only surface when item_count > 0;
        # absence of the field means we couldn't snapshot the cart
        # for that store (e.g. Carrefour UC Chrome path) and we
        # shouldn't claim anything either way.
        pre = sr.get("pre_existing") or {}
        pre_count = pre.get("item_count", 0) or 0
        if pre_count > 0:
            grand_existing += pre_count
            pre_total = pre.get("estimated_total") or 0
            extra_bits = [
                f"{pre_count} item{'s' if pre_count != 1 else ''} already in cart"
            ]
            if isinstance(pre_total, (int, float)) and pre_total > 0:
                extra_bits.append(f"+{pre_total:.2f} {_CURRENCY_SYMBOL}")
            lines.append(
                f"  🛒 {' · '.join(extra_bits)} from a previous session"
            )

        # Inline up to 3 failed items so the curator sees the gist
        # without us pasting the whole failure list. The in-app cart
        # report carries the full detail for follow-up.
        # When an item failed AND has a cross-vendor alternative
        # available, we surface that as a one-tap rescue hint so the
        # user doesn't think the item is just lost.
        failed_list = sr.get("failed") or []
        if failed_list:
            shown = failed_list[:3]
            for f in shown:
                name = f.get("item_name", "?")
                # Trim reasons to the first 40 chars; the in-app
                # cart_attempts diag has the full message.
                reason = (f.get("reason") or "").split("\n")[0][:40]
                if reason:
                    lines.append(f"  ⚠️ {name} — {reason}")
                else:
                    lines.append(f"  ⚠️ {name}")
                # Cross-vendor fallback hint. alternatives can be
                # either the new dict shape {"same_vendor","other_vendor"}
                # or the legacy plain list -- handle both.
                alts = f.get("alternatives") or {}
                other = []
                if isinstance(alts, dict):
                    other = alts.get("other_vendor") or []
                # Render only the best cross-vendor candidate -- the
                # user has the full list in the in-app cart UI.
                if other:
                    o = other[0]
                    price_bit = (
                        f" ({o['price']:.2f} {_CURRENCY_SYMBOL})"
                        if isinstance(o.get("price"), (int, float))
                        else ""
                    )
                    lines.append(
                        f"     → available at *{o.get('store_name', '?')}*"
                        f"{price_bit} — tap *swap* in the app"
                    )
            if len(failed_list) > 3:
                more = len(failed_list) - 3
                lines.append(
                    f"  _+ {more} more issue{'s' if more != 1 else ''} "
                    f"in the app_"
                )
        blocks.append("\n".join(lines))

    header_bits = []
    if grand_added:
        plural = "" if grand_added == 1 else "s"
        header_bits.append(f"{grand_added} item{plural}")
    if grand_total > 0:
        header_bits.append(f"~{grand_total:.2f} {_CURRENCY_SYMBOL}")
    header_sub = " · ".join(header_bits)
    header = f"✅ *Order placed*"
    if header_sub:
        header += f" — {header_sub}"

    msg = header + "\n\n" + "\n\n".join(blocks)
    if grand_existing > 0:
        msg += (
            f"\n\n_Heads up: {grand_existing} item"
            f"{'' if grand_existing == 1 else 's'} from a previous "
            f"session will ship along with today's order. Open the "
            f"store cart to clear them if you don't want them._"
        )
    if grand_failed > 0:
        if grand_failed == 1:
            msg += "\n\n_1 item needs follow-up — check the app._"
        else:
            msg += f"\n\n_{grand_failed} items need follow-up — check the app._"
    return msg


# ─────────────────────────────────────────────
# B3: out-for-delivery status notification
# ─────────────────────────────────────────────

# Status values, in forward-progress rank order. Only used to detect
# "is this transition going forwards or sideways/backwards?" -- the
# scheduler skips backward transitions so a poller flicker doesn't
# trigger a notification on every sweep.
_STATUS_RANK = {
    "pending":          0,
    "expected":         0,
    "confirmed":        1,
    "preparing":        2,
    "processing":       2,
    "ready":            3,
    "ready_for_pickup": 3,
    "shipped":          3,
    "out_for_delivery": 4,
    "dispatched":       4,   # Carrefour's canonical label
    "delivered":        5,
    "completed":        5,
    "cancelled":        -1,  # terminal but not "forward"
}


def status_is_forward(old_status: str | None, new_status: str | None) -> bool:
    """True when new_status ranks strictly above old_status.

    Used by the dispatcher to skip notifying on flickered or
    backwards transitions (e.g. processing -> confirmed in the
    middle of a Carrefour status check).
    """
    if not new_status:
        return False
    new_rank = _STATUS_RANK.get(new_status.lower(), -2)
    if new_rank < 0:
        return False
    old_rank = _STATUS_RANK.get((old_status or "").lower(), -2)
    return new_rank > old_rank


# Status values we send a one-line WhatsApp for. `delivered` is
# intentionally NOT in this set -- B5's rating prompt is the
# notification for that transition (it opens with `📦 Store
# delivery · …`).
_STATUS_NOTIFY_SET = {"out_for_delivery", "dispatched"}


def format_status_update(
    order: dict, new_status: str,
) -> str:
    """One-line WhatsApp notification when an order moves to a
    noteworthy status. Currently dispatched/out_for_delivery only.

    order: dict-shaped row with store_name, order_ref, delivery_type,
        expected_by (optional). All fields except store_name optional.
    """
    store = order.get("store_name") or "Your order"
    ref = order.get("order_ref")
    expected = order.get("expected_by")
    delivery_type = (order.get("delivery_type") or "").lower()

    if new_status.lower() in {"out_for_delivery", "dispatched"}:
        head = f"🚚 *{store}* — out for delivery"
        bits = []
        if delivery_type == "now":
            bits.append("express (NOW)")
        if expected:
            bits.append(f"expected {expected}")
        sub = " · ".join(bits)
        tail = f"\n{sub}" if sub else ""
        ref_line = f"\n_Ref: {ref}_" if ref else ""
        return f"{head}{tail}{ref_line}"

    # Defensive fallback: generic transition message. Shouldn't be
    # reached given _STATUS_NOTIFY_SET, but logged in case the
    # set is expanded later without updating this function.
    return f"📦 *{store}* — status: {new_status}"


# ─────────────────────────────────────────────
# B5: delivered-order rating prompt + reply
# ─────────────────────────────────────────────

def format_delivered_rating_prompt(
    order: dict, items: list[dict] | None = None,
) -> str:
    """Short rating prompt sent over WhatsApp once an order transitions
    to status='delivered'.

    Keeps it tiny: order summary + chip-friendly reply options. Replies
    are routed via _maybe_handle_rating_reply on household_members.
    member_pending_intent of kind='rating_pending'.

    order: dict-shaped row from `orders` with at least
        {id, store_name, total_amount, currency, order_ref, item_count}.
    items: optional list of item dicts (used for free-text mentions later).
            Currently rendered only as a count.
    """
    store = order.get("store_name") or "your store"
    total = order.get("total_amount")
    currency_sym = _CURRENCY_SYMBOL
    item_count = order.get("item_count")
    if item_count is None and items is not None:
        item_count = len(items)

    head_bits = [f"📦 *{store}* delivery"]
    if item_count:
        head_bits.append(f"{int(item_count)} items")
    if isinstance(total, (int, float)) and total > 0:
        head_bits.append(f"{total:.2f} {currency_sym}")
    header = " · ".join(head_bits)

    return (
        f"{header}\n\n"
        f"How did it go?\n\n"
        f"Reply:\n"
        f"  *good* 👍  — everything was fine\n"
        f"  *bad*  👎  — something wasn't right\n"
        f"  _free text_ — tell me what to fix "
        f"(e.g. 'rotten bananas', 'wrong brand of yogurt')\n\n"
        f"_You can also rate per-item later in the app._"
    )


def format_rating_reminder(order: dict) -> str:
    """B6 — one-shot reminder after 48h of no rating reply.

    Friendlier than the original prompt: acknowledges they may have
    moved on, makes the ask explicitly skippable. The dispatcher
    flips `orders.rating_reminded_at` so this never re-fires for
    the same order.
    """
    store = order.get("store_name") or "your order"
    return (
        f"Hey 👋 just checking in on the *{store}* delivery from a "
        f"couple days back -- how did it go?\n\n"
        f"Reply:\n"
        f"  *good* 👍  — everything was fine\n"
        f"  *bad*  👎  — something was off\n"
        f"  _free text_ — quick note on what to fix\n"
        f"  *skip*       — don't worry about it\n\n"
        f"_Last ask, promise. After this I'll let it go._"
    )


def format_rating_resolved(decision: str) -> str:
    """Confirmation after a rating reply lands.

    decision: 'thumbs_up_all' | 'thumbs_down_all' | 'free_text' | 'dismiss'
    """
    if decision == "thumbs_up_all":
        return (
            "Glad it all went well 🙌  I've noted everything looked good. "
            "I'll keep going with the same picks next time."
        )
    if decision == "thumbs_down_all":
        return (
            "Sorry it wasn't great. I've flagged the order for review. "
            "Tell me what went wrong (e.g. 'salmon wasn't fresh') so I "
            "can adjust next time."
        )
    if decision == "free_text":
        return (
            "Got it — I've logged your feedback and will tune future "
            "picks accordingly."
        )
    if decision == "dismiss":
        return "No worries, skipping the rating for this order."
    return "Thanks for the feedback."


# Universally-positive replies. Word-bounded check below so "goodbye"
# (which has "good" as a substring) does NOT trigger thumbs_up.
_RATING_POS_TOKENS = {
    "good", "great", "perfect", "love", "loved", "loving",
    "amazing", "awesome", "excellent", "fantastic", "happy",
    "fine", "ok", "okay", "yes", "yep", "yeah", "all_good",
    "lgtm", "satisfied",
}

# Universally-negative replies.
_RATING_NEG_TOKENS = {
    "bad", "awful", "terrible", "horrible", "disappointed",
    "unhappy", "no", "nope", "meh", "ugh", "gross",
    "rotten", "stale", "spoiled", "moldy", "wrong",
    "broken", "missing", "damaged",
}

# Replies that mean "don't bother / skip".
_RATING_DISMISS_TOKENS = {"skip", "dismiss", "later", "nah", "ignore", "later"}

# Emoji shorthand. Compared via set membership on the trimmed text.
_RATING_POS_EMOJIS = {"👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿", "🙌", "❤️", "💯"}
_RATING_NEG_EMOJIS = {"👎", "👎🏻", "👎🏼", "👎🏽", "👎🏾", "👎🏿", "💔"}


# Verb-with-numbers shape we should NEVER hijack into a rating
# response, even when the rating_pending is open. "skip 3" / "have 2" /
# "swap 4" / "stop 1" mean cycle actions, not a rating reply. Without
# this guard "skip" would dismiss the rating because it's in
# _RATING_DISMISS_TOKENS.
_VERB_WITH_NUMBER_RE = re.compile(
    r"^\s*(?:skip|remove|drop|have|had|got|stop|never|swap|change|switch|replace)\s+\d",
    re.IGNORECASE,
)


def parse_rating_reply(text: str) -> str | None:
    """Map a free-text WhatsApp reply to a canonical rating decision.

    Returns one of:
      'thumbs_up_all' | 'thumbs_down_all' | 'dismiss' | 'free_text' | None

    None means "the text isn't a rating-style reply at all" (e.g. a
    regular verb command sneaking through). 'free_text' means it's
    rating-context but doesn't match a canonical token — caller should
    pass through to llm_process_rating_feedback for nuance.

    Only fires when household_members.member_pending_intent indicates a
    rating_pending is open. Otherwise the regular verb dispatcher takes
    the message.

    Safety: returns None for verb-with-number commands ("skip 3",
    "have 2", "stop 4", "swap 1") regardless of any rating context.
    Those mean cycle actions on list items, not rating replies; they
    must reach the round-scoped handler.
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None

    # Verb-with-number shortcut: punt to the regular dispatcher.
    if _VERB_WITH_NUMBER_RE.match(raw):
        return None

    # Emoji-only shortcuts. Trim whitespace but keep the emoji codepoint.
    if raw in _RATING_POS_EMOJIS:
        return "thumbs_up_all"
    if raw in _RATING_NEG_EMOJIS:
        return "thumbs_down_all"

    lower = raw.lower()
    # First-token shortcut for terse replies ("good!", "great.", "bad").
    first_tok = lower.split()[0].rstrip(".!?,;:")
    if first_tok in _RATING_POS_TOKENS:
        return "thumbs_up_all"
    if first_tok in _RATING_NEG_TOKENS:
        return "thumbs_down_all"
    # Dismiss tokens fire ONLY when the reply is a single word.
    # "skip" alone -> dismiss the rating ask; "skip 3" already
    # caught by _VERB_WITH_NUMBER_RE above, but a bare "skip" with
    # nothing after still means "don't bother with the rating".
    if first_tok in _RATING_DISMISS_TOKENS and len(lower.split()) == 1:
        return "dismiss"

    # Multi-word: if the reply is short AND contains a negative word,
    # treat as thumbs_down_all so we capture it as feedback. Long
    # detailed messages flow through to free_text where the LLM gets
    # to extract per-item complaints.
    tokens = re.findall(r"\b[a-z]+\b", lower)
    if len(tokens) <= 5:
        for tok in tokens:
            if tok in _RATING_NEG_TOKENS:
                return "thumbs_down_all"
            if tok in _RATING_POS_TOKENS:
                return "thumbs_up_all"

    # Substantive free-text feedback (more than a one-liner). Pass through
    # so the caller can feed it to llm_process_rating_feedback.
    if len(raw) >= 8:
        return "free_text"

    return None


def format_confirm_remove(items: list[dict]) -> str:
    """Build a confirmation message for removed items.

    Same pattern as format_confirm_add -- the dispatcher attaches
    `format_round_status_footer` once after the bundled reply, so
    this formatter doesn't carry its own "Anything else?" line.
    """
    if not items:
        return "Nothing to remove."
    lines = ["Got it! Removing:"]
    for item in items:
        name = item.get("resolved_name") or item.get("item_text", "Unknown")
        lines.append(f"- {name}")
    return "\n".join(lines)


def format_not_found(item_text: str) -> str:
    """Item couldn't be resolved."""
    return (
        f"I couldn't find '{item_text}'. Could you be more specific? "
        f"(e.g., brand name or 'organic yogurt')"
    )



def format_already_in_cart(item_name: str, qty: int) -> str:
    """Item already in the cart."""
    return (
        f"{item_name} is already on the list (qty: {qty}). "
        f"Want me to change the quantity?"
    )


def format_round_done(member_name: str) -> str:
    """Feedback collection complete for this member."""
    return f"Thanks {member_name}! Passed your suggestions to Cem for review."


def format_no_active_round() -> str:
    """Message received but no active feedback round."""
    return (
        "No active grocery list right now. "
        "I'll message you when the next one is ready!"
    )


def format_round_closed() -> str:
    """Message received after round closed."""
    return (
        "This grocery list has already been finalized. "
        "I'll include you in the next one!"
    )


def format_non_text() -> str:
    """Non-text message received."""
    return (
        "I can only process text messages for now. "
        "Could you type what you'd like to add or remove?"
    )
