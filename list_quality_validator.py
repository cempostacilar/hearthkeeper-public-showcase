"""Phase-1 quality validator agent for the scheduled list.

Runs after list_generate to evaluate each item-to-product match against
the household's actual standard (organic, brand, in-stock). Repairs by
switching stores when a better match exists elsewhere, flags when no
good option anywhere, persists every decision to
`item_resolution_attempts` so the *next* cycle can read this history
and shortcut decisions ("organic was OOS at Carrefour last 3 weeks ->
go straight to Kibsons this week").

The contract:
  - Reads from: items, household_preferences, product_catalog, stores,
                item_resolution_attempts (history)
  - Writes to:  list_items (store_id swap on repair),
                item_resolution_attempts (one row per attempt)
  - Logs:       structured app_events ("validator_repaired",
                "validator_flagged") via app_logging.log_event

Quality bar derivation
──────────────────────
Per-item rules parsed from items.notes + household_preferences:

  * "Only organic" / "always organic" / household.prefer_organic=true
       -> require is_organic=1

  * "Jenan brand only" / "must be Glenstal"
       -> require brand contains the name (case-insensitive)

  * "preferred" quality_tier
       -> tighten: combine organic + household keywords (organic,
                   free range, grass fed, etc.)

  * default
       -> any in-stock match at the preferred store

The bar is a dict so it's auditable when persisted:

  {
    "require_organic": true,
    "require_brand":   "Jenan",
    "prefer_keywords": ["organic", "free range"],
    "min_in_stock":    1,
  }

Repair strategy
───────────────
For each item:

  1. Score the candidate set at items.preferred_store_id against the bar.
  2. If any candidate meets the bar AND is in stock -> matched, done.
  3. Otherwise check the FALLBACK stores listed in the item's
     search_terms (other store keys). Score there.
  4. If a fallback store has a meeting-bar in-stock candidate, switch
     list_items.store_id to that store. Record outcome='switched_store'.
  5. If nothing in stock anywhere meets the bar:
       - if any store has the item in-stock at a lower tier (e.g.
         non-organic when organic was required), DO NOT silently
         downgrade. Record outcome='no_in_stock', notes=summary.
         The renderer will surface this as "needs review".
       - if no store has the item AT ALL, outcome='no_match'.
  6. Always persist to item_resolution_attempts.

History-informed shortcut
─────────────────────────
At the very start of validating each item, read the most recent
`item_resolution_attempts` row for it (within the last 30 days).
If that row outcome=='switched_store' and the alt store was
consistent across the last N attempts, *start the evaluation at that
store* instead of the configured preferred_store. This is the
"persistent learning" loop -- the system gets faster and more accurate
over cycles without anyone editing config.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional, Tuple

from db_setup import DB_PATH


# ─────────────────────────────────────────────
# Skip-marker convention (list_items.user_action)
# ─────────────────────────────────────────────
# A skip is either SYSTEM-derived (we couldn't match it / it was out of stock /
# a transient glitch) or a genuine USER decision. They must be distinguishable,
# because a SYSTEM skip has to recover on the next pass once the cause clears,
# while a USER skip must stick.
#
#   SYSTEM skips  -> user_action starts with SYSTEM_SKIP_PREFIX ("auto_skipped_")
#                    e.g. "auto_skipped_no_match", "auto_skipped_oos".
#                    validate_list_quality un-sticks ALL of these each pass.
#   USER skips    -> "skipped" (chat/app) or "removed" (WhatsApp feedback / drop).
#                    Never reset by the system.
#
# Any NEW automatic skip (e.g. the OOS-on-load availability check) MUST use the
# SYSTEM_SKIP_PREFIX so it auto-recovers; using the bare "skipped" marker would
# make a transiently-OOS staple indistinguishable from a user drop and it would
# linger forever.
SYSTEM_SKIP_PREFIX = "auto_skipped_"
SKIP_NO_MATCH = SYSTEM_SKIP_PREFIX + "no_match"   # validator found no clean match
SKIP_OOS = SYSTEM_SKIP_PREFIX + "oos"             # out of stock / unavailable


# ─────────────────────────────────────────────
# Quality-bar derivation
# ─────────────────────────────────────────────

# HARD brand requirement only -- phrases that mean "it MUST be this
# brand". "preferred" is deliberately excluded: "Balade preferred"
# means Balade is nice-to-have, not mandatory, so a good substitute
# (organic Greek yogurt of another brand) should still qualify. A hard
# require_brand on "preferred" was making Yogurt flag whenever Balade
# was out of stock, dropping a perfectly good substitute.
_BRAND_PATTERN = re.compile(
    r"\b(?:must be|brand only|only from|brand:)\s+([A-Z][A-Za-z]+)",
    re.IGNORECASE,
)
_BRAND_PATTERN_2 = re.compile(
    r"^([A-Z][A-Za-z]+)\s+(?:Organic|Free Range|Brand)\b.*\bonly\b",
    re.IGNORECASE,
)

# Words that look like brand names to a naive regex but are actually
# quality descriptors. The brand extractor must reject these so it
# doesn't end up looking for "Organic" as if it were a manufacturer.
_NON_BRAND_WORDS = {
    "organic", "free", "fresh", "natural", "grass", "fed", "range",
    "raw", "whole", "lean", "lite", "low", "high", "good", "best",
    "premium", "select", "pure",
}

# "Only organic" / "always organic" intent in free-text notes.
_ORGANIC_INTENT = re.compile(
    r"\b(only\s+organic|always\s+organic|must\s+be\s+organic|organic\s+only|"
    r"never\s+non-?organic)\b",
    re.IGNORECASE,
)

# "Organic OPTIONAL" intent: organic preferred but a good non-organic option is
# acceptable ("Organic or good quality", "preferably organic", "organic if
# available"). Used to downgrade the produce hard-organic rule to a soft
# preference so the item isn't skipped when the catalog has no organic option.
_ORGANIC_OPTIONAL = re.compile(
    r"\borganic\s+or\b|\bor\s+good\s+quality\b|\bpreferably\s+organic\b|"
    r"\borganic\s+(?:if|when)\s+(?:available|possible)\b|\bor\s+good\b",
    re.IGNORECASE,
)


def derive_quality_bar(item_row: dict, household_prefs: dict,
                       item_feedback: dict = None) -> dict:
    """Build the quality criteria the validator must enforce for an item.

    Args:
        item_row: a row from `items` (dict-like with name, notes,
            quality_tier, brand, etc.)
        household_prefs: a dict mapping pref_key -> pref_value, e.g.
            {"prefer_organic": "true", "prefer_keywords":
             "organic,free range,grass fed"}
        item_feedback: the user's DIRECT in-app taps for THIS item, from
            user_item_prefs: {"avoid_brands": [...], "blocked_catalog_ids":
            [...], "pinned_catalog_id": int|None}. Highest priority — these
            were previously invisible to the validator, so "wrong item" /
            "never brand" only changed the current list and re-appeared next
            generation. None when the item has no direct feedback.

    Returns: a dict that can be JSON-serialized and persisted.
    """
    bar = {
        "require_organic": False,
        "require_brand": None,
        "prefer_keywords": [],
        "min_in_stock": 1,
        "avoid_brands": [],
        "prefer_attributes": [],
        "prefer_cheaper": False,
        "blocked_catalog_ids": [],
        "pinned_catalog_id": None,
    }

    # Household-appropriate target pack size, in grams. items.size is seeded from
    # consumption_habits.typical_size (e.g. yogurt "950g", cheese "200g"), sized
    # for a 3-person / ~3-day order. score_candidate uses it as a weak tiebreaker
    # so a single-serving pack (150g yogurt) doesn't beat a household-sized one.
    bar["target_grams"] = _size_grams(item_row.get("size") or "")
    if not bar["target_grams"]:
        # Fallback for items whose size column was never seeded: a per-staple
        # household target (same values as the consumption_habits seed). Keeps
        # the size band active so a 200ml milk / 150g yogurt can't win on a
        # low per-pack price when the explicit size is missing.
        _nm = (item_row.get("item_name") or item_row.get("name") or "").lower()
        _words = set(re.findall(r"[a-z]+", _nm))
        for _kw, _g in _FALLBACK_TARGET_GRAMS.items():
            if _kw in _words:
                bar["target_grams"] = _g
                break

    # Typical price the household pays for this item (per its usual pack). Used
    # as a soft price-sanity signal in score_candidate so a wildly-overpriced
    # mismatch is deprioritized.
    try:
        bar["avg_price"] = float(item_row.get("avg_price") or 0) or None
    except (TypeError, ValueError):
        bar["avg_price"] = None

    notes = (item_row.get("notes") or "").strip()
    tier = (item_row.get("quality_tier") or "").lower()
    category = (item_row.get("category") or "").lower().strip()

    # Category-level brand-avoidance (from delivered-order feedback, e.g.
    # "no Angus in meat"). A rule applies to this item when its category
    # word appears in the item's category path OR name.
    _hay = f"{category} {(item_row.get('name') or '').lower()}"
    _avoid_rules = household_prefs.get("_avoid_brand_rules") or []
    if _avoid_rules:
        bar["avoid_brands"] = [
            r["brand"] for r in _avoid_rules
            if r.get("category") and r["category"] in _hay
        ]
    # Preferred brands for this item's category (e.g. prefer Coca-Cola in
    # soft drinks) -- a strong scoring boost, not a hard requirement, so a
    # temporary OOS doesn't leave the item unmatched.
    _prefer_rules = household_prefs.get("_prefer_brand_rules") or []
    if _prefer_rules:
        bar["prefer_brands"] = [
            r["brand"] for r in _prefer_rules
            if r.get("category") and r["category"] in _hay
        ]
    # Positive attribute / origin / grade preferences for this item's category
    # (e.g. prefer wagyu / New Zealand / Australia for meat) -- a scoring boost.
    for _ar in (household_prefs.get("_prefer_attribute_rules") or []):
        if _ar.get("category") and _ar["category"] in _hay:
            for _v in _ar.get("values", []):
                if _v and _v not in bar["prefer_attributes"]:
                    bar["prefer_attributes"].append(_v)
    # "Pick the cheaper comparable option" for this item's category.
    if any(c and c in _hay
           for c in (household_prefs.get("_prefer_cheaper_cats") or [])):
        bar["prefer_cheaper"] = True

    prefers_organic = (
        str(household_prefs.get("prefer_organic", "")).strip().lower() == "true"
    )

    # Household-wide organic preference (soft, applies everywhere)
    if prefers_organic:
        bar["prefer_keywords"].append("organic")

    # Household-wide preference keywords
    kw_raw = household_prefs.get("prefer_keywords") or ""
    for kw in (k.strip() for k in kw_raw.split(",")):
        if kw and kw.lower() not in bar["prefer_keywords"]:
            bar["prefer_keywords"].append(kw.lower())

    # PRODUCE rule: for fresh fruit & veg, organic is a HARD requirement
    # when the household prefers organic. Organic matters most for
    # produce (pesticide load, taste, the household's standing rule),
    # so prefer an organic option even across varieties -- an organic
    # apple of another variety beats a non-organic Royal Gala. Other
    # categories keep organic as a soft preference unless their note
    # says "only organic".
    if prefers_organic and category in (
        "produce", "fruits", "fruit", "vegetables", "vegetable"
    ):
        bar["require_organic"] = True

    # Item-level organic intent ("Only organic") -> HARD require anywhere, and
    # STRICT: skip rather than buy non-organic (eggs). The produce rule above is
    # NON-strict on its own -- organic is strongly preferred, but a good
    # non-organic option is accepted when no organic one is stocked (so a staple
    # like garlic isn't skipped just because the store has no organic bulb).
    if notes and _ORGANIC_INTENT.search(notes):
        bar["require_organic"] = True
        bar["organic_strict"] = True

    # Item-level organic-OPTIONAL intent ("Organic or good quality") -> organic
    # is preferred, not mandatory. Downgrade the produce hard-require to a soft
    # boost so a good non-organic option (e.g. Chiquita bananas, when the catalog
    # stocks no organic banana) still qualifies instead of the item being skipped
    # entirely. "Only organic" items (checked above) are left strict.
    if (notes and not _ORGANIC_INTENT.search(notes)
            and _ORGANIC_OPTIONAL.search(notes)):
        bar["require_organic"] = False
        if "organic" not in bar["prefer_keywords"]:
            bar["prefer_keywords"].append("organic")

    # Item-level brand intent. Extract a candidate brand, but ONLY make
    # it a HARD requirement when the note actually signals mandatory
    # ("only" / "must"). A bare "Brand: Balade preferred" or "Almarai
    # full fat" is a soft preference -- if that brand is OOS, a good
    # type-correct substitute (any plain Greek yoghurt) should still
    # qualify. Hard-requiring on "preferred"/"Brand:" was dropping
    # Yogurt entirely whenever Balade was out of stock.
    if notes:
        m = _BRAND_PATTERN.search(notes) or _BRAND_PATTERN_2.match(notes)
        if m:
            candidate_brand = m.group(1)
            is_mandatory = re.search(r"\b(only|must)\b", notes, re.IGNORECASE)
            if candidate_brand.lower() not in _NON_BRAND_WORDS and is_mandatory:
                bar["require_brand"] = candidate_brand

    # Quality-tier "preferred" used to auto-promote organic from
    # preference -> hard requirement, but that's too aggressive: it
    # forces require_organic on items like Soft Drinks and Garlic that
    # nobody actually shops as organic. Now require_organic comes from
    # ONLY the explicit per-item note ("Only organic"). The household
    # keyword remains as a soft preference (positive score in
    # score_candidate, no hard rejection).

    # ── In-app per-item feedback (direct user taps) — highest priority ──
    # The user's "never {brand}" / "wrong item" / "pin" taps live in
    # user_item_prefs. Fold them into the bar so the SCHEDULED-LIST product
    # picker honors them: a blocked product never returns, a blocked brand is
    # avoided, a pinned product always wins. This is the fix for "I flagged it
    # but it keeps coming back" — the validator now reads what the app captured.
    fb = item_feedback or {}
    fb_brands = [b for b in (fb.get("avoid_brands") or []) if b]
    if fb_brands:
        bar["avoid_brands"] = list(dict.fromkeys((bar.get("avoid_brands") or []) + fb_brands))
    bar["blocked_catalog_ids"] = list(fb.get("blocked_catalog_ids") or [])
    bar["pinned_catalog_id"] = fb.get("pinned_catalog_id")

    return bar


# ─────────────────────────────────────────────
# Candidate scoring
# ─────────────────────────────────────────────

def _size_grams(size_str: str) -> Optional[int]:
    """Extract the gram-equivalent from a size string for pack-size comparison.
    Handles weight AND volume (~1 g/ml): "Approx 250g" -> 250, "1kg" -> 1000,
    "2L" -> 2000, "200ml" -> 200, "1.5 litre" -> 1500. Without volume support,
    liquids (milk "2L", drinking yogurt "150ml") parsed to None and got NO size
    scoring, so tiny packs won. Returns None if nothing parseable.
    """
    if not size_str:
        return None
    import re
    s = size_str.lower()
    # kilograms
    m = re.search(r"(\d+(?:\.\d+)?)\s*kg", s)
    if m:
        return int(float(m.group(1)) * 1000)
    # millilitres (~1 g/ml) — check before the 'l' rule so "200ml" isn't misread
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", s)
    if m:
        return int(float(m.group(1)))
    # litres
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:l|ltr|litre|liter)s?\b", s)
    if m:
        return int(float(m.group(1)) * 1000)
    # grams
    m = re.search(r"(\d+(?:\.\d+)?)\s*g\b", s)
    if m:
        return int(float(m.group(1)))
    return None


# Per-natural-unit price ceilings — call a product "pricey" when its price per
# real unit (egg / kg / litre) exceeds the ceiling. (keyword, max AED/unit, kind,
# label). kind: "each" = price / pack count; "kg"/"l" = price per 1000 g/ml.
# Whole-word matched so "Eggplant" never matches "egg". Tunable / extensible.
PRICEY_RULES = [
    ("egg", 3.0, "each", "AED/egg"),
]

# Fallback household target sizes (grams/ml) by item-name keyword, used ONLY when
# items.size was never seeded. Same magnitudes as the consumption_habits seed so a
# single-serving pack (200ml milk / 150g yogurt) still loses the size band. Whole-
# word matched. Items not listed simply get no size band (graceful no-op).
_FALLBACK_TARGET_GRAMS = {
    "milk": 2000, "yogurt": 950, "yoghurt": 950, "kefir": 500,
    "laban": 1000, "labneh": 400, "juice": 1000, "cream": 250,
}


def _pack_count(size_str):
    """Pieces in a pack: '12 PCS'->12, '6 eggs'->6, '10 pcs'->10. None if not a
    countable pack."""
    if not size_str:
        return None
    import re
    s = str(size_str).lower()
    m = re.search(r"(\d+)\s*(?:pcs|pc|pieces|piece|eggs|egg|ct|count|pk|pack)\b", s)
    if m:
        return int(m.group(1))
    # "Pack of 10", "box of 6" -- count after the connector.
    m = re.search(r"(?:pack|box|tray|carton)\s*of\s*(\d+)", s)
    return int(m.group(1)) if m else None


def per_unit_price(generic_name, category, product):
    """If a PRICEY_RULES rule matches this product, return
    (per_unit, threshold, label, is_pricey); else None. Used both to score
    (penalize pricey) and to surface a "Pricey" tag in the UI."""
    price = product.get("price") or 0
    if price <= 0:
        return None
    import re
    hay = (f"{category or ''} {generic_name or ''} "
           f"{product.get('product_name') or ''}").lower()
    words = set(re.findall(r"[a-z]+", hay))
    size = (product.get("size") or "").strip()
    pname = product.get("product_name") or ""
    for kw, thresh, kind, label in PRICEY_RULES:
        if kw in words or (kw + "s") in words:
            per = None
            if kind == "each":
                # The pack count can live in either the size field or the
                # product name ("...Pack of 10, 10 pc") -- try both.
                n = _pack_count(size) or _pack_count(pname)
                if n:
                    per = price / n
            else:  # "kg" / "l" -> price per 1000 g/ml
                g = _size_grams(size) or _size_grams(pname)
                if g:
                    per = price / (g / 1000.0)
            if per is not None:
                per = round(per, 2)
                return (per, thresh, label, per > thresh)
    return None


# Words that are always "allowed" in a product name without counting as
# a foreign noun -- quality modifiers, size units, and filler. These
# never indicate a different product type.
_ALLOWED_PRODUCT_WORDS = {
    # quality / farming
    "organic", "fresh", "natural", "free", "range", "grass", "fed",
    "raw", "whole", "baby", "wild", "caught", "extra", "virgin",
    "premium", "pitted", "approx", "approximately", "lean",
    # size / pack
    "pack", "packs", "piece", "pieces", "pcs", "pc", "kg", "g", "ml",
    "l", "oz", "lb", "x", "approx",
    # packaging / measurement connectors -- "1kg per piece",
    # "approx 700g each", "net wt". These describe quantity/packaging,
    # never the product itself, so they must not count as foreign nouns.
    # ("per" was wrongly rejecting "Red Cabbages 1kg per piece".)
    "per", "each", "about", "min", "max", "net", "wt", "weight",
    "avg", "average", "gm", "gms", "kgs", "ltr",
    # filler / connectors
    "the", "and", "of", "with", "in", "a", "an", "no", "added",
    # ── food DESCRIPTOR words (UNIVERSAL) ──
    # These describe HOW a product is presented, not WHAT it is, and
    # apply across all categories. A "Greek Style Yoghurt" is still
    # yogurt. Without these the noun-mismatch wrongly rejects legit
    # descriptors as if they signalled a different product.
    "greek", "style", "plain", "smooth", "creamy", "thick", "set",
    "full", "fat", "low", "skimmed", "semi", "double", "single",
    "light", "lite", "mild", "mature", "sharp", "aged", "smoked",
    "salted", "unsalted", "sliced", "grated", "shredded", "diced",
    "chopped", "minced", "crushed", "peeled",
    # NB: "yoghurt"/"yogurt" intentionally NOT allowed here -- product NOUNS, not
    # descriptors. A universal allow let "Banana Yoghurt" pass the noun gate for
    # a "Banana" item. Yogurt's UK/US spelling tolerance is in _SPELLING_EQUIV.
    "colour", "color", "size", "vary", "may", "leaf", "loose",
    "long", "short", "grain", "red", "green", "yellow", "white",
    "black", "brown", "ripe", "seedless",
}

# UK/US (and common) spelling equivalences, applied in score_candidate ONLY
# when one side is already an expected word -- so a "Yogurt" item still matches a
# "Greek Yoghurt" product, without yoghurt being a universal free pass that lets
# "Banana Yoghurt" win for a "Banana" item.
_SPELLING_EQUIV = {
    "yogurt": "yoghurt", "yoghurt": "yogurt",
    "yogurts": "yoghurts", "yoghurts": "yogurts",
}

# Cut / butchery words that are valid DESCRIPTORS only for meat &
# seafood. In produce, "fillet" is foreign -- "Garlic Anchovy Fillets"
# is anchovies, not garlic, so "fillets" must NOT excuse it there.
_MEAT_CUT_WORDS = {
    "fillet", "fillets", "breast", "thigh", "leg", "legs", "mince",
    "boneless", "skinless", "steak", "steaks", "chop", "chops",
    "rib", "ribs", "loin", "shank", "drumstick", "drumsticks", "wing",
    "wings",
}


def _foreign_words(candidate: dict, expected_words: set, category: str = "") -> list:
    """Tokens in the candidate's product_name that aren't explained by
    the item name, the search term, the brand, or the allowed-modifier
    list. Each one suggests the product is a DIFFERENT thing than what
    we're shopping for.

      "Garlic & Rosemary Butter"  vs  expected {garlic}  -> [rosemary, butter]
      "Cocos Coconut Milk Kefir"  vs  expected {kefir,cocos} -> [coconut, milk]
      "Kibsons Royal Gala Apples" vs  expected {apple,royal,gala,kibsons} -> []

    Cut words (fillet, breast, mince...) only count as allowed
    descriptors in meat/seafood -- in produce, "Garlic Anchovy Fillets"
    must keep "fillets" as foreign so the anchovy product is rejected.
    """
    import re
    cat = (category or "").lower()
    cut_words_allowed = cat in ("meat", "seafood", "poultry", "fish")
    name = (candidate.get("product_name") or "").lower()
    tokens = [t for t in re.findall(r"[a-z]+", name) if len(t) > 2]
    foreign = []
    for t in tokens:
        if t in expected_words:
            continue
        if t in _ALLOWED_PRODUCT_WORDS:
            continue
        if t in _MEAT_CUT_WORDS and cut_words_allowed:
            continue
        # singular/plural tolerance against expected
        if t.rstrip("s") in expected_words or (t + "s") in expected_words:
            continue
        foreign.append(t)
    return foreign


def score_candidate(
    candidate: dict,
    bar: dict,
    *,
    category: str = "",
    generic_name: str = "",
    search_term: str = "",
    brand_name: str = "",
) -> Tuple[bool, int, str]:
    """Score a product_catalog row against the quality bar.

    Returns:
        (meets_bar, score, reason)

        meets_bar: True if the hard requirements pass
        score:     int, higher is better (used to rank meeting-bar candidates)
        reason:    one-line human-readable explanation
    """
    score = 0
    reasons = []

    # In-stock is a hard requirement.
    if not candidate.get("is_available"):
        return (False, 0, "out of stock")

    # ── In-app per-item feedback (direct user taps) — authoritative ──
    # A pinned product always wins; a product the user flagged "wrong item"
    # is never picked again. (avoid_brands, which now also carries the user's
    # "never {brand}" taps, is enforced further down.) These run right after
    # the in-stock gate so a pinned OOS product still correctly falls back to
    # an alternative instead of being forced.
    cand_id = candidate.get("id")
    if cand_id is not None:
        if bar.get("pinned_catalog_id") and cand_id == bar["pinned_catalog_id"]:
            return (True, 10000, "pinned by user")
        if cand_id in (bar.get("blocked_catalog_ids") or []):
            return (False, 0, "blocked — you flagged this product wrong")

    # ── Noun-mismatch penalty ──
    # The single biggest source of "wrong product" matches: substring
    # search catches "Garlic & Rosemary Butter" for "Garlic", "Coconut
    # Milk Kefir" for "Kefir". Build the set of words we EXPECT in a
    # correct product name (item name + the search term that found it +
    # the catalog brand + allowed modifiers), then count the leftover
    # "foreign" words. Each foreign noun strongly implies a different
    # product. Two or more -> almost certainly wrong; hard-fail it.
    import re
    expected = set()
    for src in (generic_name, search_term, candidate.get("brand") or "", brand_name):
        if src:
            expected |= {w for w in re.findall(r"[a-z]+", src.lower()) if len(w) > 2}
    # Preferred attributes (e.g. "wagyu", "grass fed") are DESIRED qualifiers
    # from household feedback, not signs of a wrong product. Treat them as
    # expected so they neither trigger the foreign-word penalty nor a
    # wrong-product rejection -- otherwise the -15 penalty would cancel the
    # preference boost and the household's preferred grade would lose.
    for pa in (bar.get("prefer_attributes") or []):
        expected |= {w for w in re.findall(r"[a-z]+", pa.lower()) if len(w) > 2}
    expected |= {w + "s" for w in list(expected)} | {w.rstrip("s") for w in list(expected)}
    # Spelling equivalence, scoped to when the word is actually expected: a
    # "Yogurt" item should still match a "Greek Yoghurt" product (UK/US spelling)
    # WITHOUT yoghurt being universally excused (which let "Banana Yoghurt" win
    # for a "Banana" item). Only expands within the same concept.
    for a, b in _SPELLING_EQUIV.items():
        if a in expected:
            expected.add(b)
    foreign = _foreign_words(candidate, expected, category=category)
    # "Derivative form" words turn the base ingredient into a different
    # product entirely -- garlic -> garlic BREAD, tomato -> tomato SAUCE,
    # apple -> apple JUICE. A single one of these is enough to reject,
    # even though a single ordinary foreign word only gets a soft penalty.
    _DERIVATIVE_FORMS = {
        "baguette", "baguettes", "bread", "butter", "juice", "sauce",
        "paste", "powder", "puree", "jam", "spread", "dip", "crisps",
        "chips", "cake", "pie", "soup", "seasoning", "flavoured",
        "flavored", "marinade", "stock", "cube", "cubes",
        "toastie", "toasties", "toast", "roll", "rolls", "bun", "buns",
        "granules", "salt", "pickled", "naan", "pizza", "ciabatta",
        # drink / crushed / concentrate forms -- "Apple Crush" is a
        # drink, not fresh apples; "Tomato Concentrate" isn't tomatoes.
        "crush", "squash", "cordial", "smoothie", "nectar",
        "concentrate", "syrup", "shake",
        "dried", "candied", "canned", "tinned", "frozen",
    }
    deriv_hit = next((f for f in foreign if f in _DERIVATIVE_FORMS), None)
    if deriv_hit:
        return (False, 0, f"wrong form ({deriv_hit})")
    if len(foreign) >= 2:
        # e.g. "rosemary butter", "coconut milk" -- this is a different
        # product. Reject outright so it can't win even if organic/cheap.
        return (False, 0, f"wrong product (extra: {', '.join(foreign[:3])})")
    if len(foreign) == 1:
        score -= 15
        reasons.append(f"~{foreign[0]}?")

    # Pack-size nudge for fresh produce. A household weekly order is
    # usually a bag (400g-2kg), not a single piece (Approx 250g) or a
    # bulk wholesale pack (>5kg). The bias is small enough that it
    # only acts as a tiebreaker WITHIN a varietally-correct candidate
    # set -- variety priority is enforced upstream by the term-priority
    # candidate loop, so we won't promote Bramley over Royal Gala just
    # to get a bigger size.
    cat_lower = (category or "").lower()
    # Pack-size preference: prefer a household-appropriate size and penalize a
    # single-serving pack (e.g. 150g yogurt for a 3-person / ~3-day order). When
    # the item's typical household size is known (bar.target_grams, from
    # items.size) we score the candidate RELATIVE to it — works for any category,
    # not just produce. Otherwise we fall back to the generic produce bag band.
    # Weak tiebreaker WITHIN the meeting-bar set (variety priority is upstream).
    cand_grams = _size_grams(candidate.get("size") or candidate.get("product_name") or "")
    target_grams = bar.get("target_grams")
    if cand_grams is not None and target_grams:
        ratio = cand_grams / target_grams
        if ratio < 0.45:
            score -= 14          # single-serving pack — strongly avoid: the qty
            reasons.append("too small")   # model would otherwise just buy more of it
        elif ratio <= 1.7:
            score += 8           # ~one order's worth — the sweet spot
            reasons.append("right size")
        elif ratio > 3.0:
            score -= 5
            reasons.append("oversized")
    elif cand_grams is not None and any(
            k in cat_lower for k in ("produce", "fruit", "vegetable")):
        if cand_grams < 400:
            score -= 8
            reasons.append("small pack")
        elif cand_grams > 5000:
            score -= 15
            reasons.append("bulk pack")
        else:
            score += 3
            reasons.append("bag-sized")

    # Organic. require_organic is STRONGLY preferred (+20, so an organic option
    # decisively beats a non-organic one whenever both are in the candidate set).
    # When the requirement is STRICT (explicit "only organic", e.g. eggs) a
    # non-organic candidate is hard-rejected. Otherwise (the produce rule) a good
    # non-organic option is ACCEPTED with a penalty -- it only wins when no
    # organic option is stocked, so a staple isn't skipped over it ("organic or
    # good"). The +20 vs -12 gap keeps any organic option ahead.
    is_organic = bool(candidate.get("is_organic"))
    if bar.get("require_organic"):
        if is_organic:
            score += 20
            reasons.append("organic")
        elif bar.get("organic_strict"):
            return (False, 0, "not organic (required)")
        else:
            score -= 12
            reasons.append("non-organic (no organic in stock)")
    elif is_organic and "organic" in bar.get("prefer_keywords", []):
        score += 10
        reasons.append("organic")

    # Brand-avoidance (category-level, from delivered-order feedback, e.g.
    # "no Angus in meat"). Hard reject -- match brand OR product name so we
    # catch "Angus Beef Mince" even when the brand field is generic.
    avoid_brands = bar.get("avoid_brands") or []
    if avoid_brands:
        cand_blob = (
            (candidate.get("brand") or "") + " " +
            (candidate.get("product_name") or "")
        ).lower()
        for ab in avoid_brands:
            if ab and ab.lower() in cand_blob:
                return (False, 0, f"brand avoided ({ab})")

    # Preferred brand (soft, strong boost) -- e.g. prefer Coca-Cola in soft
    # drinks. Match brand OR product name so "Coca-Cola Zero 330ml" wins.
    prefer_brands = bar.get("prefer_brands") or []
    if prefer_brands:
        pcand = (
            (candidate.get("brand") or "") + " " +
            (candidate.get("product_name") or "")
        ).lower()
        for pb in prefer_brands:
            if pb and pb.lower() in pcand:
                score += 25
                reasons.append(f"preferred {pb}")
                break

    # Purchase-history brand alignment — prefer the brand the household ACTUALLY
    # buys for this item, graduated by how often. A soft boost reflecting their
    # real behavior (distinct from the explicit prefer_brand feedback above), so
    # the scheduled list converges on what gets ordered.
    hist_brands = bar.get("history_brands") or {}
    if hist_brands:
        hblob = ((candidate.get("brand") or "") + " " +
                 (candidate.get("product_name") or "")).lower()
        best_n, best_b = 0, None
        for b, n in hist_brands.items():
            if b and len(b) >= 2 and b.lower() in hblob and n > best_n:
                best_n, best_b = n, b
        if best_b:
            boost = 30 if best_n >= 5 else 20 if best_n >= 3 else 12 if best_n >= 2 else 5
            score += boost
            reasons.append(f"you usually buy {best_b} (x{best_n})")

    # Brand
    req_brand = bar.get("require_brand")
    if req_brand:
        cb = (candidate.get("brand") or "").lower()
        if req_brand.lower() not in cb:
            return (False, 0, f"brand mismatch (want {req_brand})")
        score += 15
        reasons.append(f"brand {req_brand}")

    # Preferred attributes (origin / grade / quality, e.g. wagyu, grass-fed, a
    # country of origin) for this category, from delivered-order feedback --
    # soft but meaningful boost. Match name, brand, description OR origin.
    prefer_attrs = bar.get("prefer_attributes") or []
    if prefer_attrs:
        ablob = " ".join([
            (candidate.get("product_name") or ""),
            (candidate.get("brand") or ""),
            (candidate.get("description") or ""),
            (candidate.get("origin_country") or ""),
        ]).lower()
        for pa in prefer_attrs:
            if pa and pa in ablob:
                score += 12
                reasons.append(f"prefers {pa}")
                break

    # Soft keyword match in product name or description
    name_blob = " ".join([
        (candidate.get("product_name") or ""),
        (candidate.get("brand") or ""),
        (candidate.get("description") or ""),
    ]).lower()
    for kw in bar.get("prefer_keywords", []):
        if kw == "organic":
            continue  # already handled above
        if kw in name_blob:
            score += 3
            reasons.append(kw)

    # Cheaper is better. Normally a small tiebreaker; when the household asked
    # to "pick the cheaper comparable option" for this category, price gets
    # more weight (still capped so it can't override a hard quality gate).
    price = candidate.get("price") or 0
    if price > 0:
        if bar.get("prefer_cheaper"):
            score -= min(int(price / 3), 12)
            if price > 0:
                reasons.append("cheaper-preferred")
        else:
            # Negative coefficient so cheaper -> higher score, capped so
            # price never dominates a quality decision.
            score -= min(int(price / 5), 5)

    # Cost-efficiency: penalize (and flag) a product whose price-per-REAL-unit
    # (per egg / kg / L) exceeds the household ceiling, so a pricey pack doesn't
    # win just because its per-PACK price is low. Graduated by how far over the
    # ceiling: a 1.6x-over egg (4.85/egg vs a 3.0 ceiling) is penalized harder
    # than a borderline one. The old flat -12 didn't differentiate, so a wildly
    # pricey pack could still win on other factors.
    pp = per_unit_price(generic_name, category, candidate)
    if pp and pp[3]:
        over = (pp[0] / pp[1]) if pp[1] else 1.0
        score -= min(30, int(round(12 * max(1.0, over))))
        reasons.append(f"pricey {pp[0]} {pp[2]}")

    # Price-sanity (SIZE-NORMALIZED): a product priced far above what the
    # household pays PER REAL UNIT for this item is usually the wrong product
    # (a premium / novelty pack -- e.g. 15-egg "Tregor" at 72.70 / +161%).
    # Compare price-per-gram to the baseline's price-per-gram
    # (avg_price / target_grams) so a legitimately BIGGER pack is NOT penalized
    # -- only a genuinely overpriced one. Self-calibrating: if the household
    # consistently buys premium, avg_price reflects that and the ratio stays ~1,
    # so only an OUTLIER above their own norm is hit. Graduated and strong
    # enough that a sane-priced in-stock alternative outscores the outlier; when
    # no sane option exists the (penalized) outlier is still allowed, so we
    # never reject the only product. (Was a flat -10 at a 3x PACK threshold,
    # which let a 2-3x overpriced product slip through.)
    avg_price = bar.get("avg_price")
    if avg_price and avg_price > 0 and price > 0:
        target_grams = bar.get("target_grams") or 0
        cand_grams = _size_grams(candidate.get("size") or "")
        ratio = thresh = None
        if target_grams and cand_grams:
            ratio = (price / cand_grams) / (avg_price / target_grams)
            thresh = 1.6   # per-gram: pack size is already accounted for
        elif price > avg_price:
            ratio = price / avg_price
            thresh = 2.5   # raw pack ratio (no size to normalize): looser
        if ratio is not None and ratio > thresh:
            pen = min(35, int(round((ratio - 1.0) * 22)))
            if pen > 0:
                score -= pen
                reasons.append(f"{ratio:.1f}x over usual price")

    return (True, score, "+".join(reasons) if reasons else "in stock")


# ─────────────────────────────────────────────
# Free-text added-item resolution
# ─────────────────────────────────────────────
# Words to strip when finding the "core noun" of a free-text add, so
# "organic red cabbage" -> core noun "cabbage" (searched in catalog),
# while "organic"/"red" become quality/variety hints.
_ADD_MODIFIER_WORDS = {
    "organic", "fresh", "red", "green", "yellow", "white", "baby",
    "large", "small", "ripe", "raw", "whole", "free", "range",
    "grass", "fed", "natural", "a", "an", "the", "some", "of",
}


def resolve_freetext_item(text, conn, prefers_organic=True):
    """Resolve a free-text added item (e.g. "organic red cabbage") to a
    real in-stock catalog product, applying the household's organic
    preference and the noun-mismatch guard.

    Why this exists: free-text adds (item_id=None) used to bypass all
    quality logic -- build_frontend_list searched the WHOLE phrase
    ("organic red cabbage") and matched "Organic Red Chillies" on the
    shared "organic red". We search by the CORE NOUN ("cabbage")
    instead, so chillies never enter the candidate set, then score with
    organic + noun-mismatch.

    Returns the best product dict (with catalog_id, store_id, price,
    size, brand, origin, is_organic) or None if nothing clean in stock.
    """
    import re
    raw = (text or "").strip()
    if not raw:
        return None
    tokens = [w for w in re.findall(r"[a-z]+", raw.lower()) if len(w) > 1]
    core_tokens = [w for w in tokens if w not in _ADD_MODIFIER_WORDS]
    core_noun = core_tokens[-1] if core_tokens else (tokens[-1] if tokens else raw)

    # Guard against degenerate input ("a", "the some of", "organic") where
    # the core noun collapses to a stopword or a 1-2 char fragment. A bare
    # LIKE on that ("%of%", "%a%") matches half the catalog and returns
    # nonsense (baby swaddles for "the some of"). Decline instead so the
    # caller surfaces it for a manual pick.
    if len(core_noun) < 3 or core_noun in _ADD_MODIFIER_WORDS:
        return None

    # Colour / variety descriptors the shopper explicitly asked for
    # ("RED cabbage", "BABY spinach"). These are modifiers we strip from
    # the core noun, minus the quality words handled by the organic bar.
    # Red cabbage and green cabbage are genuinely different vegetables,
    # so a requested descriptor must actively REWARD the matching product
    # -- otherwise a plain "Cabbage" ties a "Red Cabbage" and wins on id
    # order, ignoring what was asked for.
    _quality_tokens = {"organic", "fresh", "natural", "free", "range",
                       "grass", "fed", "raw", "whole"}
    wanted_descriptors = [
        w for w in tokens
        if w in _ADD_MODIFIER_WORDS and w not in _quality_tokens and w != core_noun
    ]

    wants_organic = prefers_organic or "organic" in raw.lower()
    bar = {
        "require_organic": wants_organic,
        "require_brand": None,
        "prefer_keywords": ["organic"] if wants_organic else [],
        "min_in_stock": 1,
    }

    # Search by the core noun across all stores (NOT the full phrase).
    # Order the candidate window toward the products most likely to be
    # the plain item: in-stock first, organic next, then shortest name
    # ("Milk" / "Kibsons Milk 1L" sort ahead of "Belgian Milk Chocolate
    # Truffles"). Without this ORDER BY, a bare LIMIT let derivative
    # products crowd out the real one and the resolver returned NO MATCH.
    rows = conn.execute(
        """SELECT id, product_name, brand, size, price, description,
                  is_organic, is_available, origin_country, category,
                  store_id
           FROM product_catalog
           WHERE LOWER(product_name) LIKE LOWER(?)
           ORDER BY is_available DESC, is_organic DESC,
                    LENGTH(product_name) ASC
           LIMIT 120""",
        (f"%{core_noun}%",),
    ).fetchall()

    def _scan(rows, bar, suffix=""):
        """Score every candidate, applying the requested-descriptor bonus.
        Returns (score, product_dict, reason) for the winner or None."""
        winner = None
        for r in rows:
            c = dict(r)
            meets, sc, why = score_candidate(
                c, bar, category=(c.get("category") or "produce"),
                generic_name=core_noun, search_term=raw,
            )
            if not meets:
                continue
            name_l = (c.get("product_name") or "").lower()
            matched = [d for d in wanted_descriptors if re.search(rf"\b{re.escape(d)}", name_l)]
            if matched:
                sc += 12 * len(matched)
                why = why + "+" + "+".join(matched)
            if winner is None or sc > winner[0]:
                winner = (sc, c, why + suffix)
        return winner

    best = _scan(rows, bar)

    # Fallback: if organic was required but nothing organic is in stock,
    # relax to non-organic so the item still resolves to the RIGHT
    # product (cabbage), just not organic -- better than chillies. The
    # caller can flag the organic downgrade.
    if best is None and wants_organic:
        bar["require_organic"] = False
        best = _scan(rows, bar, suffix=" (non-organic — no organic in stock)")

    if not best:
        return None
    _, prod, why = best
    prod["_match_reason"] = why
    # A clean household-level generic for display: requested colour/variety
    # descriptors + the core noun, title-cased ("red cabbage" -> "Red
    # Cabbage"). Excludes quality words like "organic" (those surface as a
    # separate attribute, matching the list style "Banana · Organic").
    prod["_clean_generic"] = " ".join(wanted_descriptors + [core_noun]).title()
    return prod


# ─────────────────────────────────────────────
# Persistence helpers
# ─────────────────────────────────────────────

def _recent_history(conn: sqlite3.Connection, item_id: int, days: int = 30) -> list[dict]:
    """Most recent resolution attempts for an item, newest first."""
    rows = conn.execute(
        """SELECT * FROM item_resolution_attempts
           WHERE item_id = ?
             AND attempted_at >= datetime('now', ?)
           ORDER BY attempted_at DESC
           LIMIT 5""",
        (item_id, f"-{days} days"),
    ).fetchall()
    return [dict(r) for r in rows]


def _learned_store(history: list[dict]) -> Optional[int]:
    """If the last 2+ attempts for this item resolved to the same
    *non-preferred* store, treat that as the learned preference for
    this cycle. Returns the store_id or None.
    """
    if len(history) < 2:
        return None
    switched = [
        h for h in history
        if h.get("outcome") == "switched_store" and h.get("resolved_store_id")
    ]
    if len(switched) < 2:
        return None
    last_store = switched[0]["resolved_store_id"]
    if all(s["resolved_store_id"] == last_store for s in switched[:3]):
        return last_store
    return None


def _persist(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    list_id: int,
    list_item_id: int,
    quality_bar: dict,
    store_id_tried: int,
    candidates_total: int,
    candidates_meeting_bar: int,
    candidates_in_stock: int,
    outcome: str,
    resolved_catalog_id: Optional[int],
    resolved_store_id: Optional[int],
    notes: str,
) -> None:
    """Single persistence write -- always called per item evaluated."""
    conn.execute(
        """INSERT INTO item_resolution_attempts
           (item_id, list_id, list_item_id, quality_bar,
            store_id_tried, candidates_total, candidates_meeting_bar,
            candidates_in_stock, outcome, resolved_catalog_id,
            resolved_store_id, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id, list_id, list_item_id, json.dumps(quality_bar),
            store_id_tried, candidates_total, candidates_meeting_bar,
            candidates_in_stock, outcome, resolved_catalog_id,
            resolved_store_id, notes,
        ),
    )


# ─────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────

def validate_list_quality(list_id: int, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Post-list_generate quality pass on a shopping list.

    Returns a structured report:
      {
        "list_id": int,
        "validated": int,
        "matched":   int,
        "switched":  int,
        "flagged":   int,
        "details":   [ {list_item_id, item_name, outcome, notes}, ... ]
      }

    Idempotent on outcome: re-running on the same list does no harm
    (logs additional attempts, may switch back if state changed).
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    cur = conn.cursor()

    # Un-stick any items the SYSTEM auto-skipped on a prior run (no clean match,
    # out of stock, or a transient glitch -- anything marked with the
    # SYSTEM_SKIP_PREFIX). Auto-skip is a DERIVED state, not a user decision, so
    # it must be re-evaluated fresh each pass -- otherwise an item we skipped once
    # (Yogurt before the descriptor-allowlist fix; a staple that was briefly OOS)
    # could never recover even after the cause clears. A real user drop
    # (user_action 'skipped'/'removed') is deliberately left untouched.
    cur.execute(
        "UPDATE list_items SET status = 'pending', user_action = '' "
        "WHERE list_id = ? AND user_action LIKE ?",
        (list_id, SYSTEM_SKIP_PREFIX + "%"),
    )

    # Household-wide preferences as a dict
    prefs_rows = cur.execute(
        "SELECT pref_key, pref_value FROM household_preferences"
    ).fetchall()
    household_prefs = {r["pref_key"]: r["pref_value"] for r in prefs_rows}

    # Category-level brand-avoidance rules captured from delivered-order
    # feedback (e.g. "no Angus in meat"). Stashed under a private key so
    # derive_quality_bar can compute which brands apply to each item.
    try:
        from household_rules import (
            get_avoid_brand_rules, get_prefer_brand_rules,
            get_prefer_attribute_rules, get_prefer_cheaper_categories,
        )
        household_prefs["_avoid_brand_rules"] = get_avoid_brand_rules(conn)
        household_prefs["_prefer_brand_rules"] = get_prefer_brand_rules(conn)
        household_prefs["_prefer_attribute_rules"] = get_prefer_attribute_rules(conn)
        household_prefs["_prefer_cheaper_cats"] = get_prefer_cheaper_categories(conn)
    except Exception:
        household_prefs["_avoid_brand_rules"] = []
        household_prefs["_prefer_brand_rules"] = []
        household_prefs["_prefer_attribute_rules"] = []
        household_prefs["_prefer_cheaper_cats"] = []

    # In-app per-item feedback (the user's direct taps): "never {brand}",
    # "wrong item" (blocks that exact product), and "pin". Previously the
    # validator never read these, so a flagged product/brand re-appeared on the
    # next generation. Load once, keyed by item_id, and feed each item's bar.
    item_feedback: dict = {}
    try:
        for r in cur.execute(
            "SELECT item_id, pref_type, value FROM user_item_prefs "
            "WHERE COALESCE(is_active, 1) = 1 "
            "AND pref_type IN ('block_brand', 'wrong_item', 'pin_product')"
        ).fetchall():
            fb = item_feedback.setdefault(r["item_id"], {
                "avoid_brands": [], "blocked_catalog_ids": [],
                "pinned_catalog_id": None})
            pt, val = r["pref_type"], r["value"]
            if pt == "block_brand" and val:
                fb["avoid_brands"].append(str(val))
            elif pt in ("wrong_item", "pin_product") and val is not None:
                try:
                    cid = int(val)
                except (TypeError, ValueError):
                    continue
                if pt == "wrong_item":
                    fb["blocked_catalog_ids"].append(cid)
                else:
                    fb["pinned_catalog_id"] = cid
    except Exception as e:
        logger.warning("Could not load in-app item feedback: %s", e)

    # Store name <-> id
    stores_rows = cur.execute("SELECT id, name FROM stores").fetchall()
    store_id_by_name = {s["name"]: s["id"] for s in stores_rows}
    store_name_by_id = {s["id"]: s["name"] for s in stores_rows}

    # List items that came from the system (skip user-added items --
    # they don't have quality bars derived from items.notes).
    # avg_price drives the price-sanity penalty in score_candidate. Prefer the
    # REAL per-purchase baseline (avg unit_price actually paid across delivered
    # orders) over items.avg_price, which is a static seed that's often NULL or
    # stale for generic items -- a NULL baseline silently disabled the penalty,
    # letting a 2-3x overpriced product (eggs at +161%) win. Fall back to
    # items.avg_price only when there's no order history.
    items_rows = cur.execute(
        """SELECT li.id AS list_item_id, li.item_id, li.store_id,
                  li.quantity, li.status, li.added_by,
                  i.name AS item_name, i.notes, i.search_terms,
                  i.preferred_store_id, i.quality_tier,
                  i.category, i.size,
                  COALESCE(
                    (SELECT AVG(oi.unit_price) FROM order_items oi
                       JOIN orders o ON o.id = oi.order_id
                      WHERE oi.item_id = i.id AND oi.unit_price > 0
                        AND o.status IN ('delivered', 'completed')),
                    i.avg_price
                  ) AS avg_price
           FROM list_items li
           JOIN items i ON i.id = li.item_id
           WHERE li.list_id = ?
             AND li.item_id IS NOT NULL
             AND COALESCE(li.added_by, '') IN ('system', '')
             AND COALESCE(li.status, '') != 'skipped'
           ORDER BY li.id""",
        (list_id,),
    ).fetchall()

    report = {
        "list_id": list_id,
        "validated": 0,
        "matched": 0,
        "switched": 0,
        "flagged": 0,
        "details": [],
    }

    for row in items_rows:
        item = dict(row)
        bar = derive_quality_bar(item, household_prefs,
                                 item_feedback.get(item["item_id"]))

        # Purchase-history alignment: bias the pick toward the brands the
        # household ACTUALLY buys (from scraped vendor orders + app orders), so
        # the SCHEDULED list matches their real shopping — not just generic
        # quality. The chat curator already does this via get_purchase_history;
        # the scheduled-list picker didn't, so the two surfaces disagreed and
        # the list didn't "learn" what gets ordered.
        try:
            from product_score import get_purchase_history
            _hist = get_purchase_history(item.get("item_name") or "", conn)
            bar["history_brands"] = _hist.get("brands") or {}
        except Exception:
            bar["history_brands"] = {}

        # Parse search_terms (JSON keyed by store name)
        try:
            search_terms = json.loads(item.get("search_terms") or "{}")
        except Exception:
            search_terms = {}

        # History-informed shortcut: did we consistently switch this
        # item to a non-preferred store recently?
        history = _recent_history(conn, item["item_id"], days=30)
        learned_sid = _learned_store(history)

        # Build the ordered list of stores to try:
        # 1. learned_sid (if any) -- the "shortcut"
        # 2. items.preferred_store_id
        # 3. any other store mentioned in search_terms
        store_order: list[int] = []
        if learned_sid:
            store_order.append(learned_sid)
        if item.get("preferred_store_id"):
            sid = item["preferred_store_id"]
            if sid not in store_order:
                store_order.append(sid)
        for sname in search_terms.keys():
            sid = store_id_by_name.get(sname)
            if sid and sid not in store_order:
                store_order.append(sid)

        best_match = None      # (catalog_row_dict, score, reason, store_id)
        any_catalog_seen = False
        any_in_stock_seen = False

        for sid in store_order:
            sname = store_name_by_id.get(sid, "?")
            terms = search_terms.get(sname) or []

            # Pull candidates by search term, in PRIORITY ORDER. The
            # household's search_terms list IS the variety preference:
            # ["Royal Gala Apple", "Gala Apple", "Apple"] means "Royal
            # Gala first, Gala second, anything-apple last". Pooling all
            # term-results together lets foreign varieties (Bramley)
            # bubble up just because they score high on quality bar.
            # Instead: try term 0, if it yields in-stock candidates
            # meeting the bar, score among those. Only fall back to
            # term 1 if zero qualifying candidates at term 0.
            item_cat = (item.get("category") or "").lower().strip()
            cands: list[dict] = []

            def _query(term: str, use_category: bool = True) -> list[dict]:
                # Category-aware first; fall back to broader match if zero
                # category-constrained hits. use_category=False forces a pure
                # name match (used by the term loop when the category pre-filter
                # hid the only good product -- store taxonomy "fruits" vs our
                # household category "Produce").
                cat_filter = ""
                params = [sid, f"%{term}%"]
                if use_category and item_cat and item_cat not in ("other", ""):
                    cat_filter = " AND LOWER(COALESCE(category,'')) LIKE LOWER(?)"
                    params.append(f"%{item_cat}%")
                # ORDER BY is critical: a broad term like "Garlic" matches dozens
                # of rows (garlic oil capsules, garlic spice, garlic cheese...) and
                # LIMIT 20 with no order returned an arbitrary slice that often
                # EXCLUDED the actual fresh "Kibsons Garlic". Put in-stock first
                # (don't waste the budget on OOS flavoured products) then the most
                # concise name (the base produce, not a compound flavour product).
                order = " ORDER BY is_available DESC, LENGTH(product_name) ASC"
                rows = cur.execute(
                    """SELECT id, product_name, brand, size, price,
                              description, is_organic, is_available,
                              origin_country, category
                       FROM product_catalog
                       WHERE store_id = ?
                         AND LOWER(product_name) LIKE LOWER(?)""" +
                    cat_filter + order + " LIMIT 20",
                    params,
                ).fetchall()
                if not rows and cat_filter:
                    rows = cur.execute(
                        """SELECT id, product_name, brand, size, price,
                                  description, is_organic, is_available,
                                  origin_country, category
                           FROM product_catalog
                           WHERE store_id = ?
                             AND LOWER(product_name) LIKE LOWER(?)""" +
                        order + " LIMIT 20",
                        (sid, f"%{term}%"),
                    ).fetchall()
                return [dict(r) for r in rows]

            def _meeting(cand_list, term):
                out = []
                for c in cand_list:
                    meets, _, _ = score_candidate(
                        c, bar, category=item.get("category"),
                        generic_name=item["item_name"], search_term=term,
                    )
                    if meets:
                        out.append(c)
                return out

            matched_term = item["item_name"]
            for term in (terms or [item["item_name"]])[:5]:
                term_cands = _query(term)
                if not term_cands:
                    continue
                matched_term = term
                # Score these candidates and see if any meet the quality
                # bar AND are in stock. If yes, this term is good enough
                # -- stop, don't pull broader terms.
                meeting = _meeting(term_cands, term)
                # The category pre-filter can hide the only correct product when
                # the store's taxonomy doesn't use our household category word
                # (e.g. "Produce" vs the store's "fruits"/"vegetables"). If
                # nothing here meets the bar, re-pull this term WITHOUT the
                # category filter and re-check before giving up on it. The noun
                # gate still rejects wrong products (Banana Milk / baby food).
                if not meeting and item_cat and item_cat not in ("other", ""):
                    seen_ids = {c.get("id") for c in term_cands}
                    extra = [c for c in _query(term, use_category=False)
                             if c.get("id") not in seen_ids]
                    extra_meet = _meeting(extra, term)
                    if extra_meet:
                        term_cands = term_cands + extra
                        meeting = extra_meet
                if meeting:
                    cands = term_cands
                    break
                # No in-stock meeting-bar candidate at this term -- keep
                # the candidates around but try the next, more general,
                # term. If nothing meets later either, the broadest pool
                # gives score_candidate its widest set to choose from.
                cands = term_cands

            if cands:
                any_catalog_seen = True
            for c in cands:
                if c.get("is_available"):
                    any_in_stock_seen = True

            # Score candidates (with the term that retrieved them, so the
            # noun-mismatch penalty knows the expected variety words).
            scored = []
            for c in cands:
                meets, sc, why = score_candidate(
                    c, bar, category=item.get("category"),
                    generic_name=item["item_name"], search_term=matched_term,
                )
                if meets:
                    scored.append((sc, c, why))
            scored.sort(key=lambda t: -t[0])

            if scored:
                top_sc, top_c, top_why = scored[0]
                if best_match is None or top_sc > best_match[1]:
                    best_match = (top_c, top_sc, top_why, sid)
                # First store that returns a match is usually right --
                # don't keep searching unless the learned shortcut was
                # the first store tried and it failed.
                break

        # Decide outcome
        item_id = item["item_id"]
        list_item_id = item["list_item_id"]
        current_store_id = item.get("store_id") or item.get("preferred_store_id")

        if best_match:
            cat, sc, why, found_sid = best_match
            outcome_store_name = store_name_by_id.get(found_sid, "?")
            cur_store_name = store_name_by_id.get(current_store_id, "?")

            # Always pin both store AND specific catalog product. Writing
            # list_items.catalog_id (Tier-0 hint in build_frontend_list)
            # forces the renderer to use the exact product the agent
            # chose, not whatever name-substring match happens to win
            # at render time. This is what closes the loop between
            # "agent decided X" and "user sees X on WhatsApp".
            conn.execute(
                "UPDATE list_items SET store_id = ?, catalog_id = ? WHERE id = ?",
                (found_sid, cat.get("id"), list_item_id),
            )
            if found_sid != current_store_id:
                report["switched"] += 1
                outcome = "switched_store"
                notes = (
                    f"{item['item_name']}: {cur_store_name} -> "
                    f"{outcome_store_name} ({why}). product: "
                    f"{cat.get('product_name', '?')[:60]}"
                )
            else:
                report["matched"] += 1
                outcome = "matched"
                notes = (
                    f"{item['item_name']} @ {outcome_store_name} ({why}). "
                    f"product: {cat.get('product_name', '?')[:60]}"
                )
            _persist(
                conn,
                item_id=item_id, list_id=list_id, list_item_id=list_item_id,
                quality_bar=bar,
                store_id_tried=found_sid,
                candidates_total=len(cands) if cands else 0,
                candidates_meeting_bar=len(scored) if scored else 0,
                candidates_in_stock=sum(1 for c in cands if c.get("is_available")) if cands else 0,
                outcome=outcome,
                resolved_catalog_id=cat.get("id"),
                resolved_store_id=found_sid,
                notes=notes,
            )
            report["details"].append({
                "list_item_id": list_item_id,
                "item_name": item["item_name"],
                "outcome": outcome,
                "notes": notes,
            })
        else:
            # No store had a quality-meeting in-stock match.
            report["flagged"] += 1
            outcome = "no_match" if not any_catalog_seen else (
                "no_in_stock" if not any_in_stock_seen else "flagged"
            )
            notes = (
                f"{item['item_name']}: no in-stock match meets bar "
                f"{json.dumps(bar)} across {len(store_order)} store(s). "
                f"any_catalog_seen={any_catalog_seen} "
                f"any_in_stock_seen={any_in_stock_seen}"
            )
            # When the catalog DID have candidates but every one was a
            # wrong-product / wrong-form reject (garlic -> only garlic
            # bread; the base ingredient simply isn't stocked), don't
            # leave the line for the renderer to free-style a mislabel
            # onto. Skip it -- better to omit than to show garlic bread
            # as "garlic". The item reappears next cycle when stock or
            # catalog data changes.
            #
            # We only skip on this "saw products but all wrong" case
            # (any_catalog_seen and any_in_stock_seen). A pure brand/OOS
            # miss with a real type-correct product available is handled
            # by the relaxed brand bar above, so it won't reach here.
            if any_catalog_seen and any_in_stock_seen:
                conn.execute(
                    "UPDATE list_items SET status = 'skipped', "
                    "user_action = ? WHERE id = ?",
                    (SKIP_NO_MATCH, list_item_id),
                )
                outcome = "skipped_no_clean_match"
            _persist(
                conn,
                item_id=item_id, list_id=list_id, list_item_id=list_item_id,
                quality_bar=bar,
                store_id_tried=current_store_id or 0,
                candidates_total=0,
                candidates_meeting_bar=0,
                candidates_in_stock=0,
                outcome=outcome,
                resolved_catalog_id=None,
                resolved_store_id=None,
                notes=notes,
            )
            report["details"].append({
                "list_item_id": list_item_id,
                "item_name": item["item_name"],
                "outcome": outcome,
                "notes": notes,
            })

        report["validated"] += 1

    conn.commit()

    # Structured app_events log so ops can see what happened on this
    # validation pass without rummaging through item_resolution_attempts.
    try:
        from app_logging import log_event
        log_event(
            "validator_pass_done",
            source="list_quality_validator",
            detail=(
                f"list_id={list_id} validated={report['validated']} "
                f"matched={report['matched']} switched={report['switched']} "
                f"flagged={report['flagged']}"
            ),
            level="info" if report["flagged"] == 0 else "warning",
        )
    except Exception:
        pass  # logging is non-critical here

    if close_conn:
        conn.close()
    return report
