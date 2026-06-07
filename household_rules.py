"""Household shopping rules captured from delivered-order feedback.

Turns a reviewer's free-text note on a delivered order ("no more Angus
beef", "buy variety, not 2 of the same pasta") into durable, structured,
CATEGORY-scoped rules that list generation honors going forward.

Rule types (stored in the household_rules table):
  - avoid_brand_in_category : {brand, category}  -> the quality validator
        hard-rejects any product of `brand` in `category` (whole-category
        brand block, e.g. no "Angus" on any meat item).
  - prefer_variety          : {category}          -> list_generate caps a
        single product's quantity in that category so you get variety
        instead of N× the same (e.g. don't suggest 2× lentil pasta).

`category` is the user's own word and is matched case-insensitively
against BOTH an item's category path and its name (so "pasta" matches
"Pantry > Pasta Noodles" and a product literally named "... Pasta").

The note is parsed by a cheap one-shot Haiku call (same model/budget
plumbing as item_origin_evaluator). If the LLM is unavailable or returns
nothing structured, we store nothing and the order rating still succeeds.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

import anthropic

from config import ANTHROPIC_API_KEY
from db_setup import DB_PATH

_LLM_MODEL = "claude-haiku-4-5"

RULE_AVOID_BRAND = "avoid_brand_in_category"
RULE_PREFER_BRAND = "prefer_brand_in_category"
RULE_PREFER_VARIETY = "prefer_variety"
# Positive quality/origin/grade preference scoped to a category. The `value`
# column holds the attribute text (comma-separated alternatives allowed),
# e.g. category="meat", value="wagyu, new zealand, australia".
RULE_PREFER_ATTRIBUTE = "prefer_attribute_in_category"
# "When options are comparable, pick the cheaper one" within a category.
RULE_PREFER_CHEAPER = "prefer_cheaper_in_category"
# An actionable request that is NOT a general preference (e.g. "verify the
# tomato was organic and tag it"). Captured + surfaced so it is never silently
# dropped; does not affect scoring.
RULE_CHECK_FLAG = "check_flag"


def add_household_rule(rule_type, category, brand=None, source="manual",
                       note=None, conn=None):
    """Insert one household rule directly (deterministic, no LLM), de-duped
    against active rules of the same shape. Returns True if a row was added.
    """
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        cat = (category or "").strip().lower()
        br = (brand or "").strip() or None
        exists = conn.execute(
            """SELECT 1 FROM household_rules
               WHERE is_active = 1 AND rule_type = ?
                 AND COALESCE(category,'') = COALESCE(?, '')
                 AND COALESCE(LOWER(brand),'') = COALESCE(LOWER(?), '')""",
            (rule_type, cat, br),
        ).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT INTO household_rules (rule_type, category, brand, source, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (rule_type, cat, br, source, (note or "")[:300] or None),
        )
        conn.commit()
        return True
    finally:
        if own:
            conn.close()


def swap_differentiators(chosen, original):
    """PURE: what makes the chosen product X better than the replaced Y, across
    the small fixed set of grocery-choice variables. Returns [(kind, value)]:
    ("brand", "Chiquita") / ("organic", None) / ("cheaper", saved_aed). No side
    effects, so it's unit-testable; learn_from_swap turns these into durable
    preferences. Shared by every surface that records a swap (in-app
    alternatives sheet + WhatsApp swap pick) so X-vs-Y learning is identical
    everywhere."""
    diffs = []
    bx = (chosen.get("brand") or "").strip()
    by = (original.get("brand") or "").strip()
    if bx and bx.lower() != by.lower():
        diffs.append(("brand", bx))
    if chosen.get("is_organic") and not original.get("is_organic"):
        diffs.append(("organic", None))
    try:
        px = float(chosen.get("price") or 0)
        py = float(original.get("price") or 0)
        if px > 0 and py > 0 and px <= py * 0.85:   # >=15% cheaper
            diffs.append(("cheaper", round(py - px, 2)))
    except (TypeError, ValueError):
        pass
    return diffs


def learn_from_swap(item_id, chosen, original, category, signal_source="app_ui"):
    """Compare a chosen alternative X against the product it replaced Y and learn
    WHY X was preferred — the cleanest preference signal there is. The pin on the
    SKU is handled by the caller; this GENERALIZES the choice into DURABLE
    preferences BOTH surfaces honor: a household_rule for the scheduled-list
    validator, plus a preference_state signal for the chat curator. signal_source
    tags the origin (app_ui / whatsapp). Returns a short human summary list of
    what was learned."""
    cat = (category or "").strip().lower()
    learned = []
    for kind, val in swap_differentiators(chosen, original):
        if kind == "brand":
            try:
                from preference_engine import emit_signal
                emit_signal(item_id=item_id, signal_type="brand_pref_stated",
                            signal_source=signal_source, dimension="brand",
                            new_value={"brand": val}, context={"source": "swap_compare"})
            except Exception:
                pass
            if cat:
                try:
                    add_household_rule(RULE_PREFER_BRAND, cat, brand=val, source="swap_compare")
                except Exception:
                    pass
            learned.append(f"prefer {val}")
        elif kind == "organic":
            try:
                from preference_engine import emit_signal
                emit_signal(item_id=item_id, signal_type="attribute_pref_stated",
                            signal_source=signal_source, dimension="attribute",
                            new_value={"keyword": "organic", "mode": "prefer"},
                            context={"source": "swap_compare"})
            except Exception:
                pass
            learned.append("prefer organic")
        elif kind == "cheaper":
            if cat:
                try:
                    add_household_rule(RULE_PREFER_CHEAPER, cat, source="swap_compare")
                except Exception:
                    pass
            learned.append("prefer cheaper")
    return learned


def get_prefer_brand_rules(conn=None):
    """Active [{brand, category}] preferred-brand rules (e.g. prefer
    Coca-Cola in soft drinks). The quality validator boosts these."""
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        rows = conn.execute(
            "SELECT brand, category FROM household_rules "
            "WHERE is_active = 1 AND rule_type = ? AND COALESCE(brand,'') != ''",
            (RULE_PREFER_BRAND,),
        ).fetchall()
        return [{"brand": r["brand"], "category": (r["category"] or "").lower()}
                for r in rows]
    finally:
        if own:
            conn.close()

_PROMPT = """You convert a shopper's free-text feedback about a grocery \
order into structured, durable shopping rules. The shopper is reviewing a \
just-delivered order.

Order vendor: {vendor}
Items in this order: {items}

Extract EVERY rule that clearly generalizes to FUTURE orders. A single note
often contains several. Supported rules:

1. avoid_brand_in_category — no longer wants a brand within a category.
   category is the shopper's own word ("meat","pasta","dairy"...).
   "no more Angus beef" ->
   {{"rule_type":"avoid_brand_in_category","brand":"Angus","category":"meat"}}

2. prefer_brand_in_category — positively wants a specific brand in a category.
   "always get Coca-Cola for soft drinks" ->
   {{"rule_type":"prefer_brand_in_category","brand":"Coca-Cola","category":"soft drink"}}

3. prefer_attribute_in_category — wants a quality / origin / grade / cut /
   attribute in a category (NOT a brand): e.g. wagyu, grass-fed, free-range,
   organic, or a country of origin. "value" is the attribute text; if several
   are acceptable, comma-separate them.
   "pick wagyu or beef mince from New Zealand or Australia" ->
   {{"rule_type":"prefer_attribute_in_category","category":"meat","value":"wagyu, new zealand, australia"}}

4. prefer_cheaper_in_category — when options are comparable, choose the cheaper
   one in this category.
   "check prices, if comparable pick the cheaper option" ->
   {{"rule_type":"prefer_cheaper_in_category","category":"meat"}}

5. prefer_variety — wants variety instead of multiples of the same product.
   "buy different pasta, not 2 of the same" ->
   {{"rule_type":"prefer_variety","category":"pasta"}}

6. check_flag — an ACTIONABLE request that is NOT a general preference: verify
   or fix a product's data, a one-off check. "note" = what to check; category
   optional. "check if the tomato was organic and tag it properly" ->
   {{"rule_type":"check_flag","category":"produce","note":"verify the tomato's organic status and tag it correctly"}}

Rules of thumb:
  - Emit ALL rules the note implies (a "no X, prefer Y, and pick cheaper" note
    yields avoid + prefer_attribute + prefer_cheaper).
  - Do NOT invent rules. Vague praise with no actionable content -> emit nothing.
  - Lowercase the category. Keep brand/value text as written.

Feedback: "{feedback}"

Respond with strict JSON only, an array (possibly empty). Include only the keys
each rule needs (rule_type, plus some of: brand, category, value, note):
[{{"rule_type":"...","category":"..."}}]"""


async def _parse_rules(feedback_text: str, vendor: str, items: list[str]) -> list[dict]:
    if not ANTHROPIC_API_KEY or not (feedback_text or "").strip():
        return []
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    prompt = _PROMPT.format(
        vendor=vendor or "?",
        items=", ".join(items[:25]) or "(unknown)",
        feedback=feedback_text.strip()[:500],
    )
    try:
        resp = await client.messages.create(
            model=_LLM_MODEL, max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            from llm_budget import record_call as _record
            _record(provider="anthropic", model=_LLM_MODEL,
                    intent="feedback_rule_parse",
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens)
        except Exception:
            pass
        data = json.loads(text)
        if not isinstance(data, list):
            return []
        clean = []
        for r in data:
            if not isinstance(r, dict):
                continue
            rt = (r.get("rule_type") or "").strip()
            cat = (r.get("category") or "").strip().lower()
            brand = (r.get("brand") or "").strip()
            value = (r.get("value") or "").strip()
            note = (r.get("note") or "").strip()
            if rt == RULE_AVOID_BRAND and brand and cat:
                clean.append({"rule_type": rt, "brand": brand, "category": cat})
            elif rt == RULE_PREFER_BRAND and brand and cat:
                clean.append({"rule_type": rt, "brand": brand, "category": cat})
            elif rt == RULE_PREFER_ATTRIBUTE and value and cat:
                clean.append({"rule_type": rt, "category": cat, "value": value})
            elif rt == RULE_PREFER_CHEAPER and cat:
                clean.append({"rule_type": rt, "category": cat})
            elif rt == RULE_PREFER_VARIETY and cat:
                clean.append({"rule_type": rt, "category": cat})
            elif rt == RULE_CHECK_FLAG and (note or cat):
                clean.append({"rule_type": rt, "category": cat or None,
                              "note": note or None})
        return clean
    except (json.JSONDecodeError, KeyError, IndexError, anthropic.APIError):
        return []


def _save_rules(rules: list[dict], source: str, note: str, conn) -> int:
    """Insert rules, de-duping against active rules of the same shape."""
    saved = 0
    for r in rules:
        rt, cat = r["rule_type"], r.get("category")
        brand = r.get("brand")
        value = r.get("value")
        # check_flag carries its own focused note; everything else keeps the
        # full feedback text for context.
        rule_note = r.get("note") or note
        exists = conn.execute(
            """SELECT 1 FROM household_rules
               WHERE is_active = 1 AND rule_type = ?
                 AND COALESCE(category,'') = COALESCE(?, '')
                 AND COALESCE(LOWER(brand),'') = COALESCE(LOWER(?), '')
                 AND COALESCE(LOWER(value),'') = COALESCE(LOWER(?), '')""",
            (rt, cat, brand, value),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO household_rules
                   (rule_type, category, brand, value, source, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rt, cat, brand, value, source, (rule_note or "")[:300] or None),
        )
        saved += 1
    conn.commit()
    return saved


def _summarize(rules: list[dict]) -> str:
    bits = []
    for r in rules:
        rt = r["rule_type"]
        if rt == RULE_AVOID_BRAND:
            bits.append(f"avoid {r['brand']} in {r['category']}")
        elif rt == RULE_PREFER_BRAND:
            bits.append(f"prefer {r['brand']} in {r['category']}")
        elif rt == RULE_PREFER_ATTRIBUTE:
            bits.append(f"prefer {r['value']} for {r['category']}")
        elif rt == RULE_PREFER_CHEAPER:
            bits.append(f"pick the cheaper {r['category']} when options are comparable")
        elif rt == RULE_PREFER_VARIETY:
            bits.append(f"buy varied {r['category']} (not multiples of the same)")
        elif rt == RULE_CHECK_FLAG:
            bits.append(f"check {r.get('note') or r.get('category') or 'a flagged item'}")
    return "; ".join(bits)


async def capture_feedback(feedback_text: str, order_id: int, conn=None) -> dict:
    """Parse a delivered-order note into rules, store them, return a summary.

    Returns {captured: [rules], summary: str}. Best-effort: returns empty
    on any failure so the rating flow never breaks.
    """
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        vendor, items = "", []
        try:
            row = conn.execute(
                "SELECT s.name AS store FROM orders o "
                "JOIN stores s ON s.id = o.store_id WHERE o.id = ?",
                (order_id,),
            ).fetchone()
            vendor = (row["store"] if row else "") or ""
            items = [
                r["raw_name"] for r in conn.execute(
                    "SELECT raw_name FROM order_items WHERE order_id = ? "
                    "AND COALESCE(raw_name,'') != ''",
                    (order_id,),
                ).fetchall()
            ]
        except Exception:
            pass
        rules = await _parse_rules(feedback_text, vendor, items)
        if not rules:
            return {"captured": [], "summary": ""}
        _save_rules(rules, source="delivery_feedback", note=feedback_text, conn=conn)
        return {"captured": rules, "summary": _summarize(rules)}
    finally:
        if own:
            conn.close()


async def capture_item_feedback(feedback_text: str, item_name: str,
                                conn=None) -> dict:
    """Parse an in-app per-item feedback note into durable household rules,
    using the SAME LLM "understand" step delivered-order feedback uses.

    When the user taps "wrong item" and types e.g. "too expensive" / "we want
    organic" / "not this brand", that text used to be a literal catalog search
    hint only. Now we run it through the rule parser so it becomes a durable,
    category-scoped rule the validator honors (avoid_brand / prefer_attribute /
    prefer_cheaper / ...). Item-scoped: the category is inferred from the one
    item's name. Best-effort -> {captured: [], summary: ""} on any failure.
    """
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        rules = await _parse_rules(feedback_text, "", [item_name or ""])
        if not rules:
            return {"captured": [], "summary": ""}
        _save_rules(rules, source="in_app_item_feedback",
                    note=feedback_text, conn=conn)
        return {"captured": rules, "summary": _summarize(rules)}
    finally:
        if own:
            conn.close()


# ─────────────────────────────────────────────
# Read helpers used by list generation / quality validator
# ─────────────────────────────────────────────

def get_avoid_brand_rules(conn=None) -> list[dict]:
    """Active [{brand, category}] brand-avoidance rules."""
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        rows = conn.execute(
            "SELECT brand, category FROM household_rules "
            "WHERE is_active = 1 AND rule_type = ? AND COALESCE(brand,'') != ''",
            (RULE_AVOID_BRAND,),
        ).fetchall()
        return [{"brand": r["brand"], "category": (r["category"] or "").lower()}
                for r in rows]
    finally:
        if own:
            conn.close()


def get_variety_categories(conn=None) -> list[str]:
    """Active category words flagged prefer_variety (lowercased)."""
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        rows = conn.execute(
            "SELECT DISTINCT LOWER(category) AS c FROM household_rules "
            "WHERE is_active = 1 AND rule_type = ? AND COALESCE(category,'') != ''",
            (RULE_PREFER_VARIETY,),
        ).fetchall()
        return [r["c"] for r in rows if r["c"]]
    finally:
        if own:
            conn.close()


def get_prefer_attribute_rules(conn=None) -> list[dict]:
    """Active [{category, values:[...]}] positive attribute/origin/grade
    preferences (e.g. prefer wagyu / NZ / Australia for meat). `value` may be
    a comma-separated list of acceptable alternatives; split into `values`."""
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        rows = conn.execute(
            "SELECT category, value FROM household_rules "
            "WHERE is_active = 1 AND rule_type = ? AND COALESCE(value,'') != ''",
            (RULE_PREFER_ATTRIBUTE,),
        ).fetchall()
        out = []
        for r in rows:
            vals = [v.strip().lower() for v in (r["value"] or "").split(",") if v.strip()]
            if vals:
                out.append({"category": (r["category"] or "").lower(), "values": vals})
        return out
    finally:
        if own:
            conn.close()


def get_prefer_cheaper_categories(conn=None) -> list[str]:
    """Active category words flagged prefer_cheaper (lowercased)."""
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        rows = conn.execute(
            "SELECT DISTINCT LOWER(category) AS c FROM household_rules "
            "WHERE is_active = 1 AND rule_type = ? AND COALESCE(category,'') != ''",
            (RULE_PREFER_CHEAPER,),
        ).fetchall()
        return [r["c"] for r in rows if r["c"]]
    finally:
        if own:
            conn.close()


def brand_is_avoided(brand: str, item_category: str, item_name: str,
                     avoid_rules: list[dict]) -> bool:
    """True if `brand` is avoided for an item in this category/name.

    Matches the rule's category word against the item's category path OR
    name (so "meat" hits "Meat > Beef", "pasta" hits a pasta product).
    """
    if not brand or not avoid_rules:
        return False
    b = brand.lower().strip()
    hay = f"{(item_category or '')} {(item_name or '')}".lower()
    for r in avoid_rules:
        if r["brand"].lower().strip() in b and r["category"] and r["category"] in hay:
            return True
    return False


def category_prefers_variety(item_category: str, item_name: str,
                             variety_cats: list[str]) -> bool:
    if not variety_cats:
        return False
    hay = f"{(item_category or '')} {(item_name or '')}".lower()
    return any(c and c in hay for c in variety_cats)
