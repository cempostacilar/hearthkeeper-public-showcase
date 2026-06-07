"""
AutoRestock - Product Knowledge Base Manager

Manages the deep product knowledge that powers smart alternatives.

Scraping Strategy (3 phases):
  ┌──────────────────────────────────────────────────────────┐
  │ Phase 1: INITIAL DISCOVERY (run once per item per store) │
  │   Search "eggs" on Carrefour → get 15-20 results        │
  │   For each: scrape product page (description, origin,   │
  │   ingredients, nutrition) → score → store in catalog     │
  │   Result: "For Eggs, we know 15 products, ranked"        │
  │                                                          │
  │ Phase 2: ORDER-TIME CHECK (before each order, fast)      │
  │   For preferred product: is it still in stock? price?    │
  │   If yes → done (0.5 sec)                                │
  │   If no → pull pre-scored alternative from knowledge     │
  │   base (instant, no scraping needed)                     │
  │                                                          │
  │ Phase 3: ON-DEMAND REFRESH (only when needed)            │
  │   Preferred out of stock AND alternatives stale?         │
  │   Re-scrape just that item's alternatives (not the       │
  │   whole store — 10-15 products, not 1000s)               │
  └──────────────────────────────────────────────────────────┘

Numbers that matter:
  20 habit items × 2 stores × 15 results = ~600 products to catalog
  Each product page scrape: ~2 seconds
  Initial full discovery: ~20 minutes (one time)
  Order-time check: ~10 seconds (just availability pings)
  On-demand refresh: ~30 seconds (one item's alternatives)

Usage:
  from product_knowledge import (
      discover_alternatives,    # Phase 1: initial deep scrape + score
      check_availability,       # Phase 2: quick stock check
      refresh_alternatives,     # Phase 3: re-scrape one item
      get_best_alternative,     # Get top pick from knowledge base
      get_ranked_alternatives,  # Get all scored alternatives
  )
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta
from db_setup import DB_PATH, create_database
from product_score import score_products
from app_logging import get_logger

logger = get_logger("product_knowledge")


# ============================================================
# CATEGORY-AWARE MATCHER (Phase 1+2: 2026-05-26)
# ============================================================
#
# Filter catalog candidates BEFORE they enter item_alternatives so
# obviously-wrong matches ("Walnut" -> "Chilli Walnut Dip", "Apple"
# -> "Apple Sauce", "Beef Sausages" -> "Chicken Sausages") never
# become a #1 ranked alternative the cart builder picks.
#
# Two layers, applied in score_and_rank_alternatives:
#   1. Category gate -- the catalog row's category must contain
#      one of the patterns mapped from the item's high-level
#      category. Items have 6 buckets (Produce/Meat/Dairy/Pantry/
#      Bakery/Beverages); catalog uses 1488 deeply-nested
#      Carrefour categories. We do substring matching against the
#      catalog category lowercased.
#
#   2. Form-modifier gate -- when the item is fresh-form (Produce,
#      Meat, Dairy), reject catalog products whose name signals a
#      processed / different form ("Sauce", "Dip", "Snack", "Jerky",
#      "Powder", etc.).
#
# Both gates fail-open: if data is missing (no category on either
# side, no item category set), we let the candidate through rather
# than risk false rejection.

ITEM_CATEGORY_TO_CATALOG_PATTERNS = {
    "Produce": [
        "fresh produce", "fruits", "vegetables", "fruit", "vegetable",
        "salad", "greens", "herbs", "mushrooms",
    ],
    "Meat": [
        "meat", "poultry", "seafood", "fish",
        "beef", "chicken", "lamb", "turkey", "pork",
    ],
    "Dairy": [
        "dairy", "eggs", "cheese", "yogurt", "yoghurt", "milk",
        "butter", "cream", "kefir",
    ],
    "Pantry": [
        "pantry", "food cupboard", "snacks", "pasta", "rice",
        "noodles", "oils", "vinegars", "vinegar", "condiments",
        "sauces", "spices", "world specialities", "cereals",
        "canned", "tinned", "grains", "legumes", "pulses", "baking",
        "nuts", "dried fruit", "biscuits & crackers",
    ],
    "Bakery": [
        "bakery", "bread", "patisserie", "viennoiserie",
    ],
    "Beverages": [
        "beverages", "drinks", "juice", "water", "coffee", "tea",
        "soda", "carbonated", "soft drink",
    ],
}

# When item.category is one of these "raw / whole food" buckets,
# reject products whose name contains a form-modifier word that
# indicates the product is a prepared / processed variant.
FORM_MODIFIERS_BLOCKED = {
    "Produce": [
        "sauce", "paste", "dip", "spread", "pickle", "pickled",
        "snack", "bar", "biscotti", "cookies", "biscuits",
        "crisps", "chips", "jam", "marmalade", "powder", "puree",
        "frozen", "dried", "canned", "candy", "candied", "syrup",
        "juice",  # "apple" should not match apple juice
        "duet",   # "pepper" should not match "Classic Pepper Duet" condiment
        # Prepared liquid/dessert forms — no whole-produce staple uses these,
        # so they're safe to block (e.g. "Apple Banana Crush" baby food,
        # "Sambazon Scoopable Acai" dessert, "Pokka Banana Milk" drink,
        # "... Smoothie/Nectar/Shake").
        "crush", "smoothie", "scoopable", "nectar", "drink", "milk", "shake",
    ],
    "Meat": [
        "jerky", "snack", "powder", "paste", "sauce", "stock",
        "broth", "cube", "bouillon",
    ],
    "Dairy": [
        "powder", "mix", "biscuits", "cookies", "bar",
        "ice cream",  # not "milk" by intent
    ],
    # Pantry items often share noun words with prepared/processed
    # snacks ("Walnut" -> "Walnut Dip", "Walnuts" -> "Biscotti -
    # Coconut & Walnuts", "Dill" -> "Chickpea Snack - Dill Pickle").
    # The form-modifier gate handles those by blocking prepared
    # forms when the item is meant as a raw / pantry-staple noun.
    #
    # Notably we DON'T block "milk" here because legitimate pantry
    # items like coconut milk, almond milk, condensed milk should
    # still match -- the noun-gate (Phase 3) handles cross-noun
    # mismatches (Pistachios vs Milk) on its own.
    "Pantry": [
        "dip", "spread", "pickle", "pickled", "biscotti",
        "cookies", "biscuit",
    ],
}


def _category_allowed(item_cat, catalog_cat):
    """Phase 1 gate. True if the catalog row's category is in the
    allowed bucket for the item's high-level category.

    Fail-open: missing item_cat or missing catalog_cat -> allow.
    """
    if not item_cat or not catalog_cat:
        return True
    patterns = ITEM_CATEGORY_TO_CATALOG_PATTERNS.get(item_cat)
    if not patterns:
        return True  # unknown item bucket -> don't second-guess
    cat_lower = catalog_cat.lower()
    return any(p in cat_lower for p in patterns)


def _form_modifier_blocked(item_cat, product_name):
    """Phase 2 gate. True if the product name contains a form
    modifier that disqualifies it given the item's category.

    Uses word-boundary matching so "sauce" doesn't match "saucer"
    or some product called "Sauceless Tomato Pasta".
    """
    if not product_name:
        return False
    blocked = FORM_MODIFIERS_BLOCKED.get(item_cat or "")
    if not blocked:
        return False
    name_lower = product_name.lower()
    for modifier in blocked:
        if re.search(rf"\b{re.escape(modifier)}\b", name_lower):
            return True
    return False


# Adjectives / descriptors that come BEFORE the actual noun in an
# item name. Stripped before we extract the primary noun(s), so
# "Organic Green Capsicums" -> ["capsicums"] and "Baby Spinach" ->
# ["spinach"]. Order doesn't matter; we just remove these words.
_ITEM_NAME_ADJECTIVES = {
    "organic", "fresh", "mixed", "premium", "baby", "brown",
    "red", "green", "yellow", "white", "orange", "purple", "black",
    "ripe", "wild", "regular", "extra", "raw", "whole",
    "the", "a", "an", "of", "and", "or",
}


def _primary_nouns(item_name):
    """Extract the primary noun(s) from an item name.

      "Organic Green Capsicums"   -> ["capsicums"]
      "Beef Sausages"             -> ["beef", "sausages"]
      "Tilda Pure Basmati Rice"   -> ["tilda", "pure", "basmati", "rice"]
      "Walnut"                    -> ["walnut"]

    Strips common color/quality/size adjectives so the noun gate
    doesn't reject "Organic Eggplants" when the item is "Eggplants".
    """
    if not item_name:
        return []
    words = re.findall(r"\b[a-z]+\b", item_name.lower())
    return [w for w in words if w not in _ITEM_NAME_ADJECTIVES and len(w) > 2]


# Common spelling / regional variants of grocery nouns. When we
# look up an item's noun we ALSO check these alternatives in the
# product name. Found by QA: "Yogurt" (US) was failing against
# every "Yoghurt" (UK) product in the catalog and the matcher
# regressed to a 153 AED kids yogurt because that was the only
# product with the US spelling.
_NOUN_SPELLING_VARIANTS = {
    "yogurt": {"yoghurt"},
    "yoghurt": {"yogurt"},
    "flavor": {"flavour"},
    "flavour": {"flavor"},
    "color": {"colour"},
    "colour": {"color"},
    "fiber": {"fibre"},
    "fibre": {"fiber"},
    "donut": {"doughnut"},
    "doughnut": {"donut"},
    "eggplant": {"aubergine"},
    "aubergine": {"eggplant"},
    "zucchini": {"courgette"},
    "courgette": {"zucchini"},
    "garbanzo": {"chickpea"},
    "chickpea": {"garbanzo"},
}


def _noun_variants(noun):
    """All variants a single noun could appear as in product names:
    plurals, regional spellings, common interchangeables. Used by
    _noun_match for lookup tolerance.
    """
    variants = {noun}
    # singular/plural
    if noun.endswith("s") and len(noun) > 3:
        variants.add(noun[:-1])
    else:
        variants.add(noun + "s")
    if noun.endswith("y") and len(noun) > 3:
        variants.add(noun[:-1] + "ies")
    if noun.endswith("ies") and len(noun) > 4:
        variants.add(noun[:-3] + "y")
    # regional / spelling variants -- and their plurals
    extras = set()
    for v in list(variants):
        for spelling_alt in _NOUN_SPELLING_VARIANTS.get(v, set()):
            extras.add(spelling_alt)
            extras.add(spelling_alt + "s")
    variants.update(extras)
    return variants


def _noun_match(item_nouns, product_name):
    """Phase 3 gate. True if EVERY primary noun from the item name
    appears as a word in the product name.

    Tolerant of:
      * Singular/plural variation ("Capsicums" matches "Capsicum"
        or "Red Capsicums Mix"; "Walnut" matches "Walnut" or
        "Walnuts").
      * Regional spellings via _NOUN_SPELLING_VARIANTS ("Yogurt"
        matches "Yoghurt", "Eggplant" matches "Aubergine").

    Fail-open: if we couldn't extract any nouns from the item name
    (e.g. all words were adjectives), allow the candidate through.
    Better to let a marginal match in than reject everything.
    """
    if not item_nouns or not product_name:
        return True
    name_lower = product_name.lower()
    for noun in item_nouns:
        variants = _noun_variants(noun)
        if not any(
            re.search(rf"\b{re.escape(v)}\b", name_lower) for v in variants
        ):
            return False
    return True


# Phase 4 gate: a produce item should not match a product whose name carries a
# DIFFERENT produce head-noun. _noun_match only checks the item noun is PRESENT,
# so "Banana Shallots" (an onion), "Plantain Banana", "Banana Passionfruit"
# (curuba) and "Banana leaf" all slip through — the item word is there, but as a
# modifier/flavour on another fruit/veg or a non-fruit plant part. Curated for
# precision: only clearly-distinct nouns, so a real "Chiquita Banana" is never
# dropped.
_DISTINCT_PRODUCE_NOUNS = {
    "shallot", "shallots", "plantain", "plantains",
    "passionfruit", "curuba", "acai",
    "leek", "leeks", "scallion", "scallions",
}
# Non-fruit plant parts. Wrong for a fruit/veg item ("Banana leaf", "Banana
# Stem") but RIGHT for leafy/stem crops, so the block is skipped when the item
# itself is one of those.
_PART_NOUNS = {"leaf", "leaves", "stem", "stems", "stalk", "stalks"}
_LEAFY_OR_STEM_ITEMS = {
    "lettuce", "spinach", "kale", "chard", "cabbage", "celery", "rhubarb",
    "mint", "basil", "coriander", "cilantro", "parsley", "arugula", "rocket",
    "leek", "leeks", "chives", "dill", "thyme", "rosemary", "bay", "herb",
    "herbs", "greens", "scallion", "scallions", "lemongrass", "fennel",
}


def _foreign_produce_noun_blocked(item_cat, item_nouns, product_name):
    """Phase 4 gate (Produce only). True if the product name carries a produce
    head-noun DIFFERENT from the item's — i.e. the item word is present but it's
    really a different fruit/veg or a non-fruit plant part. High precision: a
    curated distinct-noun set, and plant-part words are allowed for items that
    ARE leafy/stem crops (so "Lettuce Leaves" survives)."""
    if (item_cat or "") != "Produce" or not product_name:
        return False
    name_lower = product_name.lower()
    # Whole produce is sold by weight or piece, never in millilitres — an `ml`
    # volume means it's a juice/smoothie/drink masquerading as the fruit (e.g.
    # "Barakat Strawberry Banana 330ml", "... Banana Milk 240ml").
    if re.search(r"\b\d+\s*ml\b", name_lower):
        return True
    name_words = set(re.findall(r"\b[a-z]+\b", name_lower))
    item_set = set(item_nouns or [])
    for w in _DISTINCT_PRODUCE_NOUNS:
        if w in name_words and w not in item_set:
            return True
    if not (item_set & _LEAFY_OR_STEM_ITEMS):
        for w in _PART_NOUNS:
            if w in name_words and w not in item_set:
                return True
    return False


# ────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ────────────────────────────────────────────
# WRITE: Store scraped products into knowledge base
# ────────────────────────────────────────────

def catalog_store_products(store_id, products, conn=None):
    """
    Store scraped products into product_catalog.

    Args:
        store_id: which store these came from
        products: list of dicts from scraper, each with:
            name, price, size, brand, product_url, image_url,
            description (optional), ingredients (optional),
            nutrition_info (optional), origin_country (optional),
            is_organic (optional), category (optional)

    Returns: list of catalog_ids for the stored products
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()
    catalog_ids = []

    for p in products:
        product_url = p.get("product_url", "")
        has_detail = bool(p.get("description") or p.get("ingredients"))

        # Upsert by (store_id, product_url) if URL exists, else by (store_id, product_name)
        if product_url:
            cur.execute(
                "SELECT id FROM product_catalog WHERE store_id = ? AND product_url = ?",
                (store_id, product_url)
            )
        else:
            cur.execute(
                "SELECT id FROM product_catalog WHERE store_id = ? AND product_name = ?",
                (store_id, p.get("name", ""))
            )

        existing = cur.fetchone()

        if existing:
            catalog_id = existing["id"]
            cur.execute("""
                UPDATE product_catalog SET
                    product_name = ?, price = ?, size = ?, brand = ?,
                    category = ?, description = ?, ingredients = ?,
                    nutrition_info = ?, origin_country = ?, image_url = ?,
                    is_organic = ?, is_available = COALESCE(?, is_available),
                    scraped_at = datetime('now'),
                    detail_scraped = CASE WHEN ? THEN 1 ELSE detail_scraped END,
                    sku = COALESCE(?, sku),
                    unit = COALESCE(?, unit),
                    regular_price = COALESCE(?, regular_price),
                    sale_price = COALESCE(?, sale_price),
                    transport_route = COALESCE(?, transport_route),
                    popularity_score = COALESCE(?, popularity_score),
                    is_fresh = COALESCE(?, is_fresh),
                    badges = COALESCE(?, badges),
                    featured_tags = COALESCE(?, featured_tags),
                    search_keywords = COALESCE(?, search_keywords),
                    external_id = COALESCE(?, external_id)
                WHERE id = ?
            """, (
                p.get("name"), p.get("price"), p.get("size"), p.get("brand"),
                p.get("category"), p.get("description"), p.get("ingredients"),
                p.get("nutrition_info"), p.get("origin_country"), p.get("image_url"),
                1 if p.get("is_organic") else 0,
                1 if p.get("is_available", True) else 0,
                has_detail,
                p.get("sku"), p.get("unit"),
                p.get("regular_price"), p.get("sale_price"),
                p.get("transport_route"), p.get("popularity_score"),
                1 if p.get("is_fresh") else None,
                p.get("badges"), p.get("featured_tags"),
                p.get("search_keywords"), p.get("external_id"),
                catalog_id,
            ))
        else:
            cur.execute("""
                INSERT INTO product_catalog
                    (store_id, product_name, product_url, brand, price, size,
                     category, description, ingredients, nutrition_info,
                     origin_country, image_url, is_organic, is_available,
                     detail_scraped,
                     sku, unit, regular_price, sale_price, transport_route,
                     popularity_score, is_fresh, badges, featured_tags,
                     search_keywords, external_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                store_id,
                p.get("name"), product_url, p.get("brand"), p.get("price"),
                p.get("size"), p.get("category"), p.get("description"),
                p.get("ingredients"), p.get("nutrition_info"),
                p.get("origin_country"), p.get("image_url"),
                1 if p.get("is_organic") else 0,
                1 if p.get("is_available", True) else 0,
                1 if has_detail else 0,
                p.get("sku"), p.get("unit"),
                p.get("regular_price"), p.get("sale_price"),
                p.get("transport_route"), p.get("popularity_score"),
                1 if p.get("is_fresh") else 0,
                p.get("badges"), p.get("featured_tags"),
                p.get("search_keywords"), p.get("external_id"),
            ))
            catalog_id = cur.lastrowid

        catalog_ids.append(catalog_id)

    conn.commit()
    if close_conn:
        conn.close()

    return catalog_ids


def availability_sweep_safe(seen_count, existing_count,
                            min_coverage=0.5, min_seen=200):
    """Guard for the post-scrape "mark not-seen products unavailable" step.

    A healthy FULL catalog scrape re-sees ~every current product, so the set of
    "disappeared" URLs (existing minus seen) is small — only genuinely delisted
    items. But if a scrape is blocked / times out / errors partway, it sees only
    a fraction, and marking everything-not-seen unavailable wrongly zeros the
    WHOLE store. That is exactly how Carrefour ended up 0 available of 60,714.

    Returns True only when the scrape covered enough of the existing catalog to
    trust its absences, so the caller skips the availability sweep on a clearly
    incomplete run rather than nuking the store.

    Args:
        seen_count:     unique product URLs collected in this scrape
        existing_count: product URLs already in the catalog for this store
        min_coverage:   minimum fraction of the existing catalog that must have
                        been re-seen (default 0.5)
        min_seen:       absolute floor — fewer than this many products seen is
                        always treated as incomplete (default 200)
    """
    if existing_count <= 0:
        return True   # brand-new / empty store — nothing to wrongly zero
    if seen_count < min_seen:
        return False  # saw almost nothing — definitely incomplete
    return (seen_count / existing_count) >= min_coverage


def score_and_rank_alternatives(item_id, item_name, store_id, catalog_ids, conn=None):
    """
    Score catalog products against the preference profile and store rankings.

    This is the bridge between the catalog (raw scraped data) and
    the alternatives table (pre-computed "what to pick").

    Args:
        item_id: which habit item we're finding alternatives for
        item_name: the habit item name (for looking up preference profile)
        store_id: which store
        catalog_ids: list of product_catalog IDs to score

    Returns: list of scored results, best first
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Fetch store name
    cur.execute("SELECT name FROM stores WHERE id = ?", (store_id,))
    store_row = cur.fetchone()
    store_name = store_row["name"] if store_row else "Unknown"

    # Fetch the item's high-level category for the matcher gates.
    # Stays None for items that have no category set (legacy rows);
    # the gates fail-open in that case.
    cur.execute("SELECT category FROM items WHERE id = ?", (item_id,))
    item_row = cur.fetchone()
    item_cat = item_row["category"] if item_row else None

    # Fetch catalog products
    if not catalog_ids:
        if close_conn:
            conn.close()
        return []

    placeholders = ",".join("?" * len(catalog_ids))
    cur.execute(f"""
        SELECT * FROM product_catalog WHERE id IN ({placeholders})
    """, catalog_ids)
    catalog_rows = cur.fetchall()

    # Convert to dicts for the scoring engine, BUT first apply the
    # category + form-modifier + required-noun gates so obviously-
    # wrong matches (Walnut -> Walnut Dip, Apple -> Apple Sauce,
    # Beef Sausages -> Chicken Sausages, Cucumber -> Eggplants)
    # never enter item_alternatives.
    item_nouns = _primary_nouns(item_name)
    products = []
    catalog_map = {}  # map product dict back to catalog_id
    dropped_category = 0
    dropped_form = 0
    dropped_noun = 0
    dropped_foreign = 0
    for row in catalog_rows:
        if not _category_allowed(item_cat, row["category"]):
            dropped_category += 1
            continue
        if _form_modifier_blocked(item_cat, row["product_name"]):
            dropped_form += 1
            continue
        if not _noun_match(item_nouns, row["product_name"]):
            dropped_noun += 1
            continue
        if _foreign_produce_noun_blocked(item_cat, item_nouns, row["product_name"]):
            dropped_foreign += 1
            continue
        p = {
            "name": row["product_name"],
            "price": row["price"],
            "size": row["size"] or "",
            "brand": row["brand"] or "",
            "description": row["description"] or "",
            "ingredients": row["ingredients"] or "",
            "origin_country": row["origin_country"] or "",
            "product_url": row["product_url"] or "",
            "is_organic": bool(row["is_organic"]),
        }
        products.append(p)
        catalog_map[id(p)] = row["id"]

    if dropped_category or dropped_form or dropped_noun:
        logger.info(
            "matcher gates: item_id=%s (%s) dropped %d/%d candidates "
            "(category=%d form=%d noun=%d)",
            item_id, item_cat, dropped_category + dropped_form + dropped_noun,
            len(catalog_rows), dropped_category, dropped_form, dropped_noun,
        )

    # Score them (pass item_id to use learned preferences from preference_state)
    scored = score_products(item_name, store_name, products, item_id=item_id)

    # Store rankings in item_alternatives
    for rank, result in enumerate(scored, 1):
        catalog_id = catalog_map.get(id(result["product"]))
        if catalog_id is None:
            continue

        is_preferred = 1 if rank == 1 and result["tier"] == "preferred" else 0
        reasons_json = json.dumps(result["reasons"])

        cur.execute("""
            INSERT INTO item_alternatives
                (item_id, store_id, catalog_id, rank, score, tier, reasons, is_preferred, last_scored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(item_id, store_id, catalog_id) DO UPDATE SET
                rank = excluded.rank,
                score = excluded.score,
                tier = excluded.tier,
                reasons = excluded.reasons,
                is_preferred = excluded.is_preferred,
                last_scored_at = datetime('now')
        """, (
            item_id, store_id, catalog_id,
            rank, result["score"], result["tier"],
            reasons_json, is_preferred,
        ))

    conn.commit()
    if close_conn:
        conn.close()

    return scored


def purge_gated_alternatives(conn=None):
    """Delete item_alternatives rows whose product now FAILS the matcher gates
    (category / form-modifier / required-noun / foreign-produce-noun). The scorer
    upserts and never deletes, so pollution that entered before the gates were
    tightened lingers until rescored — this clears it immediately. Returns
    (deleted_count, sample) where sample lists a few removed (item, product)
    pairs for the log. Safe + idempotent: only removes rows the gates reject."""
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True
    try:
        rows = conn.execute(
            """SELECT ia.id, i.name AS item_name, i.category AS item_cat,
                      pc.product_name, pc.category AS prod_cat
               FROM item_alternatives ia
               JOIN product_catalog pc ON pc.id = ia.catalog_id
               JOIN items i ON i.id = ia.item_id"""
        ).fetchall()
        to_del, sample = [], []
        for r in rows:
            nouns = _primary_nouns(r["item_name"] or "")
            pname = r["product_name"] or ""
            if (not _category_allowed(r["item_cat"], r["prod_cat"])
                    or _form_modifier_blocked(r["item_cat"], pname)
                    or not _noun_match(nouns, pname)
                    or _foreign_produce_noun_blocked(r["item_cat"], nouns, pname)):
                to_del.append(r["id"])
                if len(sample) < 12:
                    sample.append(f"{r['item_name']} -> {pname}")
        for i in range(0, len(to_del), 400):
            chunk = to_del[i:i + 400]
            conn.execute(
                "DELETE FROM item_alternatives WHERE id IN (%s)"
                % ",".join("?" * len(chunk)),
                chunk,
            )
        conn.commit()
        return len(to_del), sample
    finally:
        if close_conn:
            conn.close()


# ────────────────────────────────────────────
# READ: Query the knowledge base
# ────────────────────────────────────────────

def get_ranked_alternatives(item_id, store_id, limit=5, conn=None):
    """
    Get pre-scored alternatives for an item at a store.
    Returns from the knowledge base — NO scraping, instant.

    Returns list of dicts:
        [{"rank": 1, "product_name": "...", "price": X, "score": 85,
          "tier": "preferred", "reasons": [...], "product_url": "...", ...}, ...]
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()
    cur.execute("""
        SELECT
            ia.rank, ia.score, ia.tier, ia.reasons,
            ia.is_preferred, ia.last_scored_at,
            pc.product_name, pc.price, pc.size, pc.brand,
            pc.origin_country, pc.is_organic, pc.is_available,
            pc.product_url, pc.image_url, pc.description,
            pc.scraped_at
        FROM item_alternatives ia
        JOIN product_catalog pc ON ia.catalog_id = pc.id
        WHERE ia.item_id = ? AND ia.store_id = ?
          AND ia.tier != 'avoid'
        ORDER BY ia.rank ASC
        LIMIT ?
    """, (item_id, store_id, limit))

    results = []
    for row in cur.fetchall():
        reasons = []
        try:
            reasons = json.loads(row["reasons"]) if row["reasons"] else []
        except (json.JSONDecodeError, TypeError):
            pass

        results.append({
            "rank": row["rank"],
            "product_name": row["product_name"],
            "price": row["price"],
            "size": row["size"],
            "brand": row["brand"],
            "origin_country": row["origin_country"],
            "is_organic": bool(row["is_organic"]),
            "is_available": bool(row["is_available"]),
            "product_url": row["product_url"],
            "score": row["score"],
            "tier": row["tier"],
            "reasons": reasons,
            "is_preferred": bool(row["is_preferred"]),
            "scraped_at": row["scraped_at"],
            "last_scored_at": row["last_scored_at"],
        })

    if close_conn:
        conn.close()

    return results


def get_cross_vendor_alternatives(item_id, exclude_store_id, limit=3, conn=None):
    """Get pre-scored alternatives for the SAME item at OTHER vendors.

    Used by the cart-builder OOS fallback: when same-vendor
    alternatives are exhausted, look up the same item at every
    other store we know about. Returns the best-ranked available
    candidate per store.

    Args:
        item_id: the items.id we're trying to fulfil.
        exclude_store_id: skip this store (the one whose adds just
            failed).
        limit: cap how many candidates to return.

    Returns:
        List of dicts, each with the same shape as
        get_ranked_alternatives() plus a "store_id" / "store_name"
        field. Sorted with the LOWEST-rank (i.e. best) per-store
        candidate first, then by score.
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True
    try:
        cur = conn.cursor()
        # Prefer one row per (item, store): the lowest-rank entry
        # that's available and isn't 'avoid' tier. Lateral-join
        # would be nicest but SQLite doesn't have it; we approximate
        # with a window function (SQLite 3.25+ supports ROW_NUMBER).
        cur.execute(
            """
            SELECT * FROM (
                SELECT
                    ia.store_id,
                    s.name AS store_name,
                    ia.rank, ia.score, ia.tier, ia.reasons,
                    ia.is_preferred, ia.last_scored_at,
                    pc.product_name, pc.price, pc.size, pc.brand,
                    pc.origin_country, pc.is_organic, pc.is_available,
                    pc.product_url, pc.image_url, pc.description,
                    pc.scraped_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY ia.store_id
                        ORDER BY ia.rank ASC, ia.score DESC
                    ) AS rn
                FROM item_alternatives ia
                JOIN product_catalog pc ON ia.catalog_id = pc.id
                JOIN stores s ON s.id = ia.store_id
                WHERE ia.item_id = ?
                  AND ia.store_id != ?
                  AND ia.tier != 'avoid'
                  AND pc.is_available = 1
            )
            WHERE rn = 1
            ORDER BY rank ASC, score DESC
            LIMIT ?
            """,
            (item_id, exclude_store_id, limit),
        )

        results = []
        for row in cur.fetchall():
            reasons = []
            try:
                reasons = json.loads(row["reasons"]) if row["reasons"] else []
            except (json.JSONDecodeError, TypeError):
                pass
            results.append({
                "store_id": row["store_id"],
                "store_name": row["store_name"],
                "rank": row["rank"],
                "score": row["score"],
                "tier": row["tier"],
                "reasons": reasons,
                "is_preferred": bool(row["is_preferred"]),
                "last_scored_at": row["last_scored_at"],
                "product_name": row["product_name"],
                "price": row["price"],
                "size": row["size"],
                "brand": row["brand"],
                "origin_country": row["origin_country"],
                "is_organic": bool(row["is_organic"]),
                "is_available": bool(row["is_available"]),
                "product_url": row["product_url"],
                "image_url": row["image_url"],
                "description": row["description"],
                "scraped_at": row["scraped_at"],
            })
        return results
    finally:
        if close_conn:
            conn.close()


def get_best_alternative(item_id, store_id, conn=None):
    """
    Get the single best available product for an item at a store.
    Instant — reads from knowledge base, no scraping.

    Returns (product_dict, score_info) or (None, None) if nothing known.
    """
    ranked = get_ranked_alternatives(item_id, store_id, limit=3, conn=conn)

    for alt in ranked:
        if alt["is_available"]:
            return alt, alt
        # If preferred is unavailable, try next

    # Nothing available
    return None, None


# ────────────────────────────────────────────
# PRE-AVAILABILITY CHECK
# ────────────────────────────────────────────

def pre_check_availability(items, conn=None):
    """
    For each item in a shopping list, check if the preferred product is available.
    If not, find the next-best alternative and return substitution info.

    Args:
        items: list of dicts with at least {item_id, store_id, item_name}

    Returns:
        dict keyed by item_id:
        {
            item_id: {
                "status": "available" | "substituted" | "unavailable",
                "original_product": str or None,
                "substitute_product": str or None,
                "substitute_price": float or None,
                "substitute_catalog_id": int or None,
                "note": str or None,  # human-readable substitution note
            }
        }
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    results = {}

    for item in items:
        item_id = item["item_id"]
        store_id = item["store_id"]
        item_name = item.get("item_name", "Unknown")

        ranked = get_ranked_alternatives(item_id, store_id, limit=5, conn=conn)

        if not ranked:
            # No knowledge at all — can't check availability
            results[item_id] = {
                "status": "available",  # assume available, no data to say otherwise
                "original_product": None,
                "substitute_product": None,
                "substitute_price": None,
                "substitute_catalog_id": None,
                "note": None,
            }
            continue

        # Check if the top-ranked (preferred) product is available
        preferred = ranked[0]

        if preferred["is_available"]:
            results[item_id] = {
                "status": "available",
                "original_product": preferred["product_name"],
                "substitute_product": None,
                "substitute_price": None,
                "substitute_catalog_id": None,
                "note": None,
            }
            continue

        # Preferred is unavailable — look for a substitute
        substitute = None
        for alt in ranked[1:]:
            if alt["is_available"]:
                substitute = alt
                break

        if substitute:
            note = (
                f"Instead of {preferred['product_name']} (out of stock) "
                f"→ {substitute['product_name']}"
            )
            results[item_id] = {
                "status": "substituted",
                "original_product": preferred["product_name"],
                "substitute_product": substitute["product_name"],
                "substitute_price": substitute["price"],
                "substitute_catalog_id": None,  # could add catalog_id if needed
                "note": note,
            }
        else:
            note = f"{preferred['product_name']} is out of stock — no alternatives available"
            results[item_id] = {
                "status": "unavailable",
                "original_product": preferred["product_name"],
                "substitute_product": None,
                "substitute_price": None,
                "substitute_catalog_id": None,
                "note": note,
            }

    if close_conn:
        conn.close()

    return results


# ────────────────────────────────────────────
# FRESHNESS: Is the knowledge stale?
# ────────────────────────────────────────────

def knowledge_status(item_id, store_id, conn=None):
    """
    Check if we have knowledge about an item at a store, and how fresh it is.

    Returns dict:
        {
            "has_knowledge": bool,
            "alternatives_count": int,
            "preferred_available": bool,
            "last_scraped": str or None (datetime),
            "is_stale": bool (True if older than 7 days),
            "needs_discovery": bool (True if never scraped),
            "needs_refresh": bool (True if stale or preferred unavailable),
        }
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Check alternatives
    cur.execute("""
        SELECT COUNT(*) as cnt,
               MIN(ia.last_scored_at) as oldest,
               MAX(ia.last_scored_at) as newest
        FROM item_alternatives ia
        WHERE ia.item_id = ? AND ia.store_id = ?
    """, (item_id, store_id))
    row = cur.fetchone()
    alt_count = row["cnt"] or 0
    last_scored = row["newest"]

    # Check preferred availability
    cur.execute("""
        SELECT pc.is_available
        FROM item_alternatives ia
        JOIN product_catalog pc ON ia.catalog_id = pc.id
        WHERE ia.item_id = ? AND ia.store_id = ? AND ia.is_preferred = 1
    """, (item_id, store_id))
    pref_row = cur.fetchone()
    preferred_available = bool(pref_row["is_available"]) if pref_row else False

    # Check staleness (older than 7 days)
    is_stale = False
    if last_scored:
        try:
            scored_date = datetime.strptime(last_scored[:19], "%Y-%m-%d %H:%M:%S")
            is_stale = (datetime.now() - scored_date) > timedelta(days=7)
        except (ValueError, TypeError):
            is_stale = True
    else:
        is_stale = True

    if close_conn:
        conn.close()

    has_knowledge = alt_count > 0

    return {
        "has_knowledge": has_knowledge,
        "alternatives_count": alt_count,
        "preferred_available": preferred_available,
        "last_scraped": last_scored,
        "is_stale": is_stale,
        "needs_discovery": not has_knowledge,
        "needs_refresh": not has_knowledge or is_stale or not preferred_available,
    }


def get_scrape_plan(conn=None):
    """
    Figure out what needs scraping across all items and stores.
    Returns a prioritized list of (item_id, item_name, store_id, store_name, reason).

    Priority order:
      1. Never scraped (needs_discovery) — must scrape
      2. Preferred unavailable — urgent, need alternative
      3. Stale knowledge (>7 days) — should refresh
      4. Everything else — fine, skip
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Get all active items with their preferred stores
    cur.execute("""
        SELECT i.id AS item_id, i.name AS item_name,
               i.preferred_store_id AS store_id, s.name AS store_name
        FROM items i
        JOIN stores s ON i.preferred_store_id = s.id
        JOIN restock_rules r ON r.item_id = i.id
        WHERE r.confidence >= 0.3
        ORDER BY i.name
    """)
    items = cur.fetchall()

    plan = []
    for item in items:
        status = knowledge_status(item["item_id"], item["store_id"], conn=conn)

        if status["needs_discovery"]:
            reason = "Never scraped — needs initial discovery"
            priority = 1
        elif not status["preferred_available"]:
            reason = "Preferred product unavailable — need alternatives"
            priority = 2
        elif status["is_stale"]:
            reason = f"Knowledge stale (last: {status['last_scraped']})"
            priority = 3
        else:
            continue  # Up to date, skip

        plan.append({
            "item_id": item["item_id"],
            "item_name": item["item_name"],
            "store_id": item["store_id"],
            "store_name": item["store_name"],
            "reason": reason,
            "priority": priority,
            "alternatives_count": status["alternatives_count"],
        })

    plan.sort(key=lambda x: x["priority"])

    if close_conn:
        conn.close()

    return plan


# ────────────────────────────────────────────
# HIGH-LEVEL: Discovery + Scoring pipeline
# ────────────────────────────────────────────

def discover_alternatives(item_id, item_name, store_id, scraped_products, conn=None):
    """
    Phase 1: Full pipeline — store scraped products, score them, rank alternatives.

    This is what you call after the scraper has done a deep search.
    It handles everything: catalog storage, scoring, ranking.

    Args:
        item_id: habit item we're finding alternatives for
        item_name: name (for preference profile lookup)
        store_id: which store
        scraped_products: list of product dicts from the scraper

    Returns: ranked alternatives list (best first)
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    # Step 1: Store in catalog
    catalog_ids = catalog_store_products(store_id, scraped_products, conn=conn)

    # Step 2: Score and rank
    ranked = score_and_rank_alternatives(item_id, item_name, store_id, catalog_ids, conn=conn)

    if close_conn:
        conn.close()

    return ranked


# ────────────────────────────────────────────
# SEARCH: Find products in catalog by keyword
# ────────────────────────────────────────────

def search_catalog(query, store_id=None, limit=10, conn=None):
    """
    Search product_catalog for products matching a text query.
    Uses word-based fuzzy matching similar to match_item_to_db().

    Args:
        query: search text, e.g. "organic banana"
        store_id: optional — restrict to one store
        limit: max results to return

    Returns:
        List of product dicts sorted by match relevance, each with:
        {catalog_id, product_name, brand, price, size, image_url,
         is_organic, store_id, store_name, product_url}
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Build SQL query — fetch all potentially matching products
    query_lower = query.lower().strip()
    words = [w for w in query_lower.split() if len(w) >= 2]

    if not words:
        if close_conn:
            conn.close()
        return []

    # Build LIKE conditions for each word
    like_conditions = []
    params = []
    for word in words:
        like_conditions.append("LOWER(pc.product_name) LIKE ?")
        params.append(f"%{word}%")

    # At least one word must match
    where_clause = " OR ".join(like_conditions)

    if store_id:
        sql = f"""
            SELECT pc.id AS catalog_id, pc.product_name, pc.brand, pc.price,
                   pc.size, pc.image_url, pc.is_organic, pc.store_id,
                   pc.product_url, pc.is_available, pc.description,
                   pc.category, pc.origin_country, pc.ingredients,
                   s.name AS store_name
            FROM product_catalog pc
            JOIN stores s ON pc.store_id = s.id
            WHERE ({where_clause}) AND pc.store_id = ? AND pc.is_available = 1
            ORDER BY LENGTH(pc.product_name)
            LIMIT 200
        """
        params.append(store_id)
    else:
        sql = f"""
            SELECT pc.id AS catalog_id, pc.product_name, pc.brand, pc.price,
                   pc.size, pc.image_url, pc.is_organic, pc.store_id,
                   pc.product_url, pc.is_available, pc.description,
                   pc.category, pc.origin_country, pc.ingredients,
                   s.name AS store_name
            FROM product_catalog pc
            JOIN stores s ON pc.store_id = s.id
            WHERE ({where_clause}) AND pc.is_available = 1
            ORDER BY LENGTH(pc.product_name)
            LIMIT 200
        """

    cur.execute(sql, params)
    rows = cur.fetchall()

    if close_conn:
        conn.close()

    if not rows:
        return []

    # Score each result by relevance
    scored = []
    for row in rows:
        name_lower = (row["product_name"] or "").lower()
        score = 0

        # Exact query match in name
        if query_lower in name_lower:
            score += 100

        # Word overlap scoring
        name_words = set(name_lower.split())
        for word in words:
            if word in name_words:
                score += 25  # Exact word match
            elif any(word in nw for nw in name_words):
                score += 10  # Substring match

        # Bonus: all words match (more specific queries rank higher)
        all_match = all(word in name_lower for word in words)
        if all_match:
            score += 50

        # Bonus: short product name = likely the actual product, not a flavored variant.
        # "Potato" (1 word) >> "Crunchos Salted Natural Potato Chips 40g" (6 words)
        if len(name_words) <= len(words) + 2:
            score += 40  # "Potato", "Organic Potato", "Red Onion 3Kg"
        elif len(name_words) <= len(words) + 4:
            score += 15  # "Ripe Organic Potatoes 1Kg"

        # Bonus: organic flag matches "organic" keyword
        if "organic" in words and row["is_organic"]:
            score += 30

        if score > 0:
            scored.append({
                "catalog_id": row["catalog_id"],
                "product_name": row["product_name"],
                "brand": row["brand"] or "",
                "price": row["price"],
                "size": row["size"] or "",
                "image_url": row["image_url"] or "",
                "is_organic": bool(row["is_organic"]),
                "store_id": row["store_id"],
                "store_name": row["store_name"],
                "product_url": row["product_url"] or "",
                "description": row["description"] or "",
                "category": row["category"] or "",
                "origin_country": row["origin_country"] or "",
                "ingredients": row["ingredients"] or "",
                "is_available": bool(row["is_available"]),
                "_relevance": score,
            })

    # Sort by relevance score descending, then by price ascending
    scored.sort(key=lambda x: (-x["_relevance"], x["price"] or 9999))

    # Remove internal scoring field
    for item in scored:
        del item["_relevance"]

    return scored[:limit]


# ────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────

if __name__ == "__main__":
    create_database()
    conn = _get_conn()

    print("=" * 60)
    print("  Product Knowledge Base Status")
    print("=" * 60)

    plan = get_scrape_plan(conn=conn)

    if not plan:
        print("\n  All items have up-to-date knowledge. Nothing to scrape.")
    else:
        print(f"\n  {len(plan)} items need scraping:\n")
        for p in plan:
            priority_label = {1: "DISCOVERY", 2: "URGENT", 3: "REFRESH"}.get(p["priority"], "?")
            existing = f" ({p['alternatives_count']} alternatives known)" if p["alternatives_count"] else ""
            print(f"  [{priority_label}] {p['item_name']} @ {p['store_name']}")
            print(f"           {p['reason']}{existing}")

    # Demo: simulate discovering alternatives for Eggs
    print(f"\n{'='*60}")
    print("  Demo: Simulated discovery for Eggs on Carrefour")
    print(f"{'='*60}")

    # Get Eggs item_id and Carrefour store_id
    cur = conn.cursor()
    cur.execute("SELECT id FROM items WHERE name = 'Eggs'")
    eggs_row = cur.fetchone()
    cur.execute("SELECT id FROM stores WHERE name = 'Carrefour UAE'")
    carrefour_row = cur.fetchone()

    if eggs_row and carrefour_row:
        # Simulate what a scraper would return
        fake_scraped = [
            {
                "name": "Jenan Organic Free Range Eggs 15 PCS",
                "price": 34.49, "size": "15 PCS", "brand": "Jenan",
                "product_url": "https://carrefouruae.com/eggs/jenan-organic-15",
                "description": "Premium organic free range eggs from Jenan farms. 15 pieces.",
                "origin_country": "UAE",
                "is_organic": True,
            },
            {
                "name": "Al Ain Organic Free Range Brown Eggs 15 PCS",
                "price": 29.99, "size": "15 PCS", "brand": "Al Ain",
                "product_url": "https://carrefouruae.com/eggs/alain-organic-15",
                "description": "Organic free range eggs from Al Ain Farms.",
                "origin_country": "UAE",
                "is_organic": True,
            },
            {
                "name": "Emirates Bio Farm Organic Eggs 15 PCS",
                "price": 38.99, "size": "15 PCS", "brand": "Emirates Bio Farm",
                "product_url": "https://carrefouruae.com/eggs/biofarm-organic-15",
                "description": "Certified organic eggs from Emirates Bio Farm.",
                "origin_country": "UAE",
                "is_organic": True,
            },
            {
                "name": "Al Ain Fresh Table Eggs 15 PCS",
                "price": 12.75, "size": "15 PCS", "brand": "Al Ain",
                "product_url": "https://carrefouruae.com/eggs/alain-table-15",
                "description": "Fresh table eggs. Non-organic.",
                "origin_country": "UAE",
                "is_organic": False,
            },
            {
                "name": "Carrefour Value Eggs 30 PCS",
                "price": 16.99, "size": "30 PCS", "brand": "Carrefour",
                "product_url": "https://carrefouruae.com/eggs/carrefour-value-30",
                "description": "Economy pack cage-free eggs.",
                "origin_country": "Saudi Arabia",
                "is_organic": False,
            },
        ]

        ranked = discover_alternatives(
            eggs_row["id"], "Eggs", carrefour_row["id"],
            fake_scraped, conn=conn
        )

        for i, r in enumerate(ranked, 1):
            p = r["product"]
            mark = "★" if not r["disqualified"] and r["tier"] == "preferred" else " "
            print(f"\n  {mark} #{i} [{r['tier'].upper()}] Score: {r['score']}/100")
            print(f"     {p['name']} — {p['price']} AED")
            for reason in r["reasons"]:
                print(f"       {reason}")

        # Now show knowledge base status
        print(f"\n  --- Knowledge base after discovery ---")
        alts = get_ranked_alternatives(eggs_row["id"], carrefour_row["id"], conn=conn)
        print(f"  {len(alts)} alternatives stored for Eggs @ Carrefour")
        for a in alts:
            avail = "✓" if a["is_available"] else "✗"
            pref = " (PREFERRED)" if a["is_preferred"] else ""
            print(f"    {avail} #{a['rank']} {a['product_name']} — {a['price']} AED [{a['tier']}]{pref}")

    conn.close()
    print(f"\n{'='*60}")
    print("  Done. Run 'python3 product_knowledge.py' to see status.")
    print(f"{'='*60}")
