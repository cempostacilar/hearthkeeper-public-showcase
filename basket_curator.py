"""
Basket Curator — Smart batch item curation with cross-vendor price comparison.

Takes a list of raw item names (e.g., from the user's wife), matches each to
products across all stores, compares prices, respects preferences, and
optimizes the vendor split considering minimum order values.
"""

import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "autostock.db"


# ── Word normalization & synonym expansion ──

# Common grocery synonyms: user term → alternative search terms
GROCERY_SYNONYMS = {
    "minced meat": ["beef mince", "mince"],
    "mince meat": ["beef mince", "minced meat"],
    "ground beef": ["beef mince"],
    "olive kalamata": ["kalamata olives"],
    "kalamata olive": ["kalamata olives"],
    "capsicum": ["bell pepper", "pepper"],
    "bell pepper": ["capsicum"],
    "coriander": ["cilantro"],
    "cilantro": ["coriander"],
    "courgette": ["zucchini"],
    "zucchini": ["courgette"],
    "aubergine": ["eggplant"],
    "eggplant": ["aubergine"],
    "prawns": ["shrimp"],
    "shrimp": ["prawns"],
    "rocket": ["arugula"],
    "arugula": ["rocket"],
}

# Broad category terms → specific items they could refer to.
# These are used to detect generic queries and suggest specific alternatives
# instead of substring-matching to wrong products (e.g., "meat" → "Mussels Meat").
CATEGORY_SYNONYMS = {
    "meat": ["beef mince", "beef", "chicken", "lamb", "striploin", "steak"],
    "fish": ["salmon", "seabream", "cod", "sea bass", "tuna"],
    "seafood": ["salmon", "shrimp", "prawns", "mussels", "seabream"],
    "greens": ["spinach", "rocket", "lettuce", "kale"],
    "fruit": ["apple", "banana", "mango", "strawberry", "avocado", "grape"],
    "dairy": ["milk", "yogurt", "cheese", "butter", "cream", "kefir"],
    "vegetables": ["potato", "carrot", "tomato", "onion", "cucumber", "pepper"],
    "veggies": ["potato", "carrot", "tomato", "onion", "cucumber", "pepper"],
    "bread": ["bread"],  # not generic — actual item
    "pasta": ["pasta"],  # not generic — actual item
}

# Irregular stems that simple suffix stripping can't handle
_STEM_OVERRIDES = {
    "minced": "mince", "potatoes": "potato", "tomatoes": "tomato",
    "mangoes": "mango", "avocadoes": "avocado", "leaves": "leaf",
}


def _stem_word(word):
    """Basic stemming for grocery matching: strip plural/past tense suffixes."""
    w = word.lower().strip()
    if w in _STEM_OVERRIDES:
        return _STEM_OVERRIDES[w]
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"  # berries→berry
    if w.endswith("es") and len(w) > 4 and w[-3] in "shxz":
        return w[:-2]  # peaches→peach
    if w.endswith("s") and len(w) > 3 and not w.endswith("ss"):
        return w[:-1]  # apples→apple, carrots→carrot
    return w


def _normalize_words(text):
    """Split text into stemmed word set for fuzzy matching."""
    return set(_stem_word(w) for w in text.lower().split() if len(w) >= 2)


def _expand_search_terms(raw_name, conn=None):
    """
    Expand a raw item name into multiple search terms using:
    1. Manual GROCERY_SYNONYMS (fast, always available)
    2. Catalog Intelligence knowledge base (DB-backed, richer)
    Returns a list where the original term is first, followed by alternatives.
    """
    terms = [raw_name.strip()]
    key = raw_name.lower().strip()
    seen = {key}

    # Manual synonyms (fast path)
    if key in GROCERY_SYNONYMS:
        for syn in GROCERY_SYNONYMS[key]:
            if syn.lower() not in seen:
                terms.append(syn)
                seen.add(syn.lower())

    # Catalog Intelligence knowledge base (richer, DB-backed)
    try:
        from catalog_intelligence import expand_search
        ci_terms = expand_search(key, conn=conn)
        for t in ci_terms:
            if t.lower() not in seen:
                # Skip noisy long product names (>4 words) — they're specific
                # products, not synonyms. e.g. "pro-v milky damage repair shampoo"
                # could falsely match "milk" in the items table.
                if len(t.split()) <= 4:
                    terms.append(t)
                    seen.add(t.lower())
    except Exception:
        pass  # Knowledge base not built yet — use manual synonyms only

    return terms


def _score_product_relevance(query, product_name):
    """
    Score how relevant a catalog product is to the search query.
    Key insight: if the product name contains another FOOD word that isn't
    the query, it's probably a different product that just contains our query
    as a flavor/modifier (e.g., "Focaccia Onion" is bread, not onion).

    Returns 0-100 relevance score.
    """
    query_lower = query.lower().strip()
    name_lower = product_name.lower().strip()

    # Clean product name: remove size/weight suffixes, punctuation, brand prefixes
    name_clean = re.sub(r'\s*[-–]\s*\d+\s*(g|kg|ml|l|pcs|pieces?|pack)\b.*$', '', name_lower)
    name_clean = re.sub(r'\s*\d+x\d+\w*$', '', name_clean)  # "2x300g" patterns
    name_clean = re.sub(r'\s*\d+\s*(g|kg|ml|l)\b', '', name_clean)  # inline sizes
    name_clean = re.sub(r'[,;:\'\"()\[\]]', '', name_clean)  # strip punctuation
    name_words = [w for w in name_clean.split() if len(w) >= 2 and w != "&"]

    query_words = [w for w in query_lower.split() if len(w) >= 2]
    query_stems = set(_stem_word(w) for w in query_words)
    name_stems = [_stem_word(w) for w in name_words]

    if not name_stems or not query_stems:
        return 0

    # How many query words appear in the product name?
    matching = query_stems & set(name_stems)

    if not matching:
        # ZERO query words found in product — almost certainly wrong product
        return 0

    match_ratio = len(matching) / len(query_stems)

    if matching == query_stems:
        score = 70  # all query words found — strong base
    elif match_ratio > 0.5:
        score = 45  # more than half found — decent partial
    elif len(query_stems) <= 3 and match_ratio <= 0.5:
        # Short multi-word query with only half or fewer matches is almost
        # certainly wrong: "toilet paper" matching "toilet brush" (only
        # "toilet" matches) is NOT toilet paper. Hard-reject.
        return 0
    else:
        score = 25  # less than half — weak, likely false positive

    # Word-order check for multi-word queries: "dish soap" ≠ "soap dish"
    # If query has 2+ words, check if they appear in the same order in the name.
    if len(query_words) >= 2 and matching == query_stems:
        # Check if the query phrase appears as a substring (preserves word order)
        if query_lower in name_lower:
            score += 10  # exact phrase match — strong signal
        else:
            # Words present but reversed — very likely a different product type
            # "dish soap" (cleaning) vs "soap dish" (bathroom accessory)
            score -= 40

    # Conflicting food words: another food item in the product name?
    # If so, the query term is likely a flavor/modifier, not the core product.
    # EXCEPTION: if the query phrase appears literally in the name, the product
    # IS the query item — extra words are just descriptors (e.g., "roll" in
    # "toilet paper roll" is a format, not a food roll).
    exact_phrase = query_lower in name_lower
    conflicting = _find_conflicting_food_words(name_stems, query_stems)
    if conflicting and not exact_phrase:
        score -= 35  # strong penalty: this is a different product
    else:
        score += 15  # no conflicting foods, or exact phrase overrides

    # Bonus: query word is the last meaningful word (core product position)
    last_stem = name_stems[-1] if name_stems else ""
    if last_stem in query_stems:
        score += 10

    return max(0, min(100, score))


# Food category words — if these appear in a product name but AREN'T the search
# query, the product is likely something else (e.g., "Focaccia Onion" is bread).
_FOOD_CATEGORY_WORDS = {
    # Baked goods
    "focaccia", "baguette", "bread", "loaf", "croissant", "cake", "muffin",
    "brioche", "ciabatta", "pita", "naan", "tortilla", "wrap", "roll",
    # Meat & seafood (core product words only — cuts/forms like fillet, mince are in _IGNORE)
    "meat", "steak", "tuna", "salmon", "chicken", "beef", "lamb", "prawn", "shrimp",
    "sausage", "burger", "fish", "seafood",
    # Dairy
    "cheese", "yogurt", "yoghurt", "cream", "butter", "labneh", "milk",
    # Drinks
    "juice", "smoothie", "water", "tea", "coffee", "latte",
    # Prepared foods
    "pizza", "pasta", "noodle", "soup", "stew", "curry", "sauce",
    "spread", "dip", "hummus", "salad", "sandwich", "meal",
    # Snacks & confectionery
    "chips", "crisp", "cracker", "biscuit", "cookie", "chocolate",
    "candy", "gum", "lollipop", "toffee", "caramel", "sweet",
    "dessert", "pudding", "custard", "jelly", "fudge",
    # Condiments & oils
    "oil", "vinegar", "ketchup", "mustard", "mayo", "mayonnaise",
    # Produce that can be flavors
    "onion", "garlic", "lemon", "lime", "ginger", "chili", "chilli",
    "tomato", "potato", "carrot", "apple", "banana", "mango",
    "olive", "pepper", "spinach", "mushroom", "corn", "pumpkin",
    "coconut", "almond", "walnut", "pistachio", "date",
    "strawberry", "blueberry", "raspberry", "cherry", "grape",
    "orange", "pineapple", "peach", "pear", "fig",
}

# Pre-compute stemmed versions so conflict detection works with stemmed input.
# Without this, "chips" stems to "chip" but _FOOD_CATEGORY_WORDS has "chips" → miss.
_FOOD_CATEGORY_STEMS = set(_stem_word(w) for w in _FOOD_CATEGORY_WORDS)

# Food category GROUPS — words in the same group are related types, not conflicts.
# e.g., "Focaccia Bread" → focaccia and bread are both baked goods → NOT a conflict.
# But "Onion Bread" → onion (produce) and bread (baked goods) → IS a conflict.
_FOOD_CATEGORY_GROUPS = [
    {"focaccia", "baguette", "bread", "loaf", "croissant", "cake", "muffin",
     "brioche", "ciabatta", "pita", "naan", "tortilla", "wrap", "roll"},
    {"meat", "steak", "chicken", "beef", "lamb", "sausage", "burger"},
    {"tuna", "salmon", "prawn", "shrimp", "fish", "seafood"},
    {"cheese", "yogurt", "yoghurt", "cream", "butter", "labneh", "milk"},
    {"juice", "smoothie", "water", "tea", "coffee", "latte"},
    {"chips", "crisp", "cracker", "biscuit", "cookie", "chocolate",
     "candy", "gum", "dessert", "pudding", "fudge"},
    {"pizza", "pasta", "noodle", "soup", "stew", "curry", "sandwich", "meal"},
]

# Build a stem → group_index lookup for fast same-group checks
_STEM_TO_GROUP = {}
for _gi, _group in enumerate(_FOOD_CATEGORY_GROUPS):
    for _word in _group:
        _STEM_TO_GROUP[_stem_word(_word)] = _gi


def _find_conflicting_food_words(name_stems, query_stems):
    """
    Check if the product name contains food words that AREN'T part of the query.
    These indicate the product is something else.

    Same-group words are NOT conflicts: "Focaccia Bread" when searching "bread"
    is fine because both are baked goods. But "Onion Bread" when searching "onion"
    IS a conflict because onion (produce) ≠ bread (baked goods).

    Returns set of conflicting food words found.
    """
    # Ignore adjectives and brand-like words
    _IGNORE = {
        "fresh", "organic", "baby", "red", "green", "white", "yellow", "black",
        "imported", "local", "farm", "gate", "premium", "natural", "raw",
        "wild", "free", "range", "pure", "whole", "mini", "giant", "large",
        "small", "extra", "double", "full", "low", "light", "classic",
        "royal", "golden", "sweet", "hot", "mild", "plain", "original",
        "australian", "brazilian", "norwegian", "scottish", "american",
        "mama", "mata", "kibsons", "kitchen", "fgc", "bakemart",
        "al", "ain", "rawabi", "almarai",
        # Packaging/format words that aren't food identifiers
        "gnocchi", "granule", "flake", "powder", "stick", "strip",
        # Meat/seafood cut/preparation forms — describe how it's cut, not what it is
        # "Chicken Breast Fillet" IS chicken, "fillet" is the cut form
        "fillet", "breast", "thigh", "drumstick", "wing", "leg", "loin",
        "rib", "chop", "cutlet", "escalope", "tenderloin", "shank",
        "mince", "minced", "ground", "sliced", "diced", "cubed",
        "boneless", "skinless", "bone",
    }

    # Pre-compute query group IDs (which food groups the query words belong to)
    query_groups = set()
    for qs in query_stems:
        gi = _STEM_TO_GROUP.get(qs)
        if gi is not None:
            query_groups.add(gi)

    conflicts = set()
    for stem in name_stems:
        if stem in _IGNORE:
            continue
        if stem in query_stems:
            continue
        if stem in _FOOD_CATEGORY_STEMS:
            # Check if this stem is in the same food group as any query stem
            stem_group = _STEM_TO_GROUP.get(stem)
            if stem_group is not None and stem_group in query_groups:
                continue  # same group → not a conflict (e.g., focaccia + bread)
            conflicts.add(stem)

    return conflicts


def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def curate_basket(item_names, list_id=None):
    """
    Take raw item names and return an optimized, vendor-split basket.

    Args:
        item_names: list of strings, e.g. ["onion", "potato", "minced meat"]
        list_id: optional shopping list ID (for context, not required)

    Returns:
        {
            "items": [...],         # per-item curation results
            "vendor_split": {...},  # grouped by store with subtotals
            "total": float,
            "summary": str,         # human-readable summary
            "unmatched": [str],     # items we couldn't find
        }
    """
    conn = _get_conn()
    try:
        stores = _get_stores(conn)
        store_ids = [s["id"] for s in stores]

        curated_items = []
        for raw_name in item_names:
            raw_name = raw_name.strip()
            if not raw_name:
                continue
            result = _curate_single_item(raw_name, store_ids, conn)
            curated_items.append(result)

        # Optimize vendor split (considering MOV)
        _optimize_vendor_split(curated_items, stores)

        # Build vendor summary
        vendor_split = {}
        total = 0.0
        for item in curated_items:
            if item["status"] == "unmatched":
                continue
            store_name = item.get("recommended_store") or "Unknown"
            if store_name not in vendor_split:
                vendor_split[store_name] = {"items": [], "subtotal": 0.0}
            vendor_split[store_name]["items"].append(item)
            vendor_split[store_name]["subtotal"] += item.get("recommended_price", 0) or 0
            total += item.get("recommended_price", 0) or 0

        # Check MOV
        for store in stores:
            name = store["name"]
            mov = store.get("min_order_value", 0) or 0
            if name in vendor_split:
                vendor_split[name]["meets_mov"] = vendor_split[name]["subtotal"] >= mov
                vendor_split[name]["mov"] = mov

        matched = [i for i in curated_items if i["status"] not in ("unmatched", "needs_input")]
        unmatched = [i["input_name"] for i in curated_items if i["status"] in ("unmatched", "needs_input")]

        # Build summary
        summary_parts = [f"Found {len(matched)} of {len(curated_items)} items."]
        for store_name, data in vendor_split.items():
            count = len(data["items"])
            sub = data["subtotal"]
            mov_warn = ""
            if not data.get("meets_mov", True):
                mov_warn = f" [!] below AED {data['mov']:.0f} minimum"
            summary_parts.append(f"{store_name}: {count} items (AED {sub:.0f}){mov_warn}")

        # Mention preference-driven picks (e.g., "organic" choices)
        pref_picks = []
        compromise_picks = []
        for i in matched:
            sr = i.get("selection_reason")
            if not sr:
                continue
            # selection_reason is now a dict with "summary", "directive_met", "compromises"
            summary_text = sr.get("summary", "") if isinstance(sr, dict) else (sr or "")
            if any(kw in summary_text.lower() for kw in ("preference", "organic", "free range", "grass fed", "premium origin")):
                pref_picks.append(i)
            if isinstance(sr, dict) and not sr.get("directive_met", True):
                compromise_picks.append(i)

        if pref_picks:
            pref_names = [f"{i['input_name']} -> {i.get('recommended_product', '?')}"
                          for i in pref_picks[:3]]
            summary_parts.append(f"Preference picks: {'; '.join(pref_names)}")

        if compromise_picks:
            comp_names = []
            for i in compromise_picks[:3]:
                sr = i.get("selection_reason", {})
                comps = sr.get("compromises", []) if isinstance(sr, dict) else []
                first = comps[0] if comps else "compromise needed"
                comp_names.append(f"{i['input_name']}: {first}")
            summary_parts.append(f"Compromises: {'; '.join(comp_names)}")

        if unmatched:
            # Include failure reasons instead of just names
            unmatched_details = []
            for i in curated_items:
                if i["status"] == "unmatched":
                    fc = i.get("failure_context", {})
                    reason = fc.get("reason_human", i["input_name"]) if fc else i["input_name"]
                    unmatched_details.append(reason)
            summary_parts.append(f"Issues: {'; '.join(unmatched_details)}")

        return {
            "items": curated_items,
            "vendor_split": vendor_split,
            "total": round(total, 2),
            "summary": "\n".join(summary_parts),
            "unmatched": unmatched,
        }
    finally:
        conn.close()


def _get_stores(conn):
    """Get all active stores with MOV info."""
    return [dict(r) for r in conn.execute("""
        SELECT id, name, min_order_value, delivery_fee
        FROM stores WHERE is_active = 1
    """).fetchall()]


def _curate_single_item(raw_name, store_ids, conn):
    """
    Match a single item name to the best product across all stores.

    Strategy:
    1. Match to items table (known items with scored alternatives)
    2. Fall back to catalog search (unknown items)
    3. If nothing found, build failure context with "did you mean?" suggestions

    Every result includes either:
    - A recommended product with selection_reason (why THIS product)
    - A failure_context with reason, human message, and suggestions
    """
    # Get store names for failure context
    store_names = []
    for sid in store_ids:
        row = conn.execute("SELECT name FROM stores WHERE id = ?", (sid,)).fetchone()
        if row:
            store_names.append(row["name"])

    result = {
        "input_name": raw_name,
        "matched_item_id": None,
        "matched_name": raw_name,
        "options": [],
        "recommended_store_id": None,
        "recommended_store": None,
        "recommended_price": None,
        "recommended_product": None,
        "recommended_qty": 1,        # usually 1; >1 for items like steaks (2x250g)
        "status": "unmatched",
        "selection_reason": None,    # why this product was picked
        "failure_context": None,     # why we failed (if we did)
    }

    # Step 1: Try items table match (fuzzy)
    item_match = _match_to_items_table(raw_name, conn)

    # Handle generic category terms (e.g., "meat" → did you mean beef, chicken, lamb?)
    if item_match and item_match.get("is_generic"):
        suggestions = item_match["did_you_mean"]
        result["status"] = "needs_input"
        result["failure_context"] = {
            "reason": "generic_term",
            "user_message": (
                f"'{raw_name}' is broad — did you mean one of these? "
                + ", ".join(s["name"] for s in suggestions)
            ),
            "did_you_mean": suggestions,
        }
        return result

    # Fetch purchase history once for this item (used across all stores)
    purchase_history = None
    if item_match:
        try:
            from product_score import get_purchase_history
            purchase_history = get_purchase_history(item_match["name"], conn)
        except Exception:
            pass

    if item_match:
        result["matched_item_id"] = item_match["item_id"]
        result["matched_name"] = item_match["name"]
        # Pass preferred_store_id through so _pick_best_option can use it as tiebreaker
        result["preferred_store_id"] = item_match.get("store_id")

        # Get best alternative from each store
        # Pass raw_name so scoring can boost products matching user's qualifiers
        # (e.g., "double cream milk" boosts "Double Cream" over "Strawberry")
        for store_id in store_ids:
            option = _get_best_alternative(
                item_match["item_id"], store_id, conn,
                raw_query=raw_name, purchase_history=purchase_history)
            if option:
                result["options"].append(option)

        if result["options"]:
            result["status"] = "matched"
            _pick_best_option(result)
            result["selection_reason"] = _build_selection_reason(result)
            return result

    # Step 2: Search catalog across all stores
    # Pass item context if we matched the items table (even if no alternatives found)
    catalog_item_name = item_match["name"] if item_match else None
    catalog_item_id = item_match["item_id"] if item_match else None
    catalog_options = _search_catalog_all_stores(
        raw_name, store_ids, conn,
        item_name=catalog_item_name, item_id=catalog_item_id,
        purchase_history=purchase_history,
    )
    if catalog_options:
        result["options"] = catalog_options
        result["status"] = "catalog_only"
        _pick_best_option(result)
        result["selection_reason"] = _build_selection_reason(result)
        return result

    # Step 3: Nothing found — build failure context
    result["failure_context"] = _build_failure_context(
        raw_name, store_ids, store_names, item_match, conn
    )
    return result


def _build_selection_reason(result):
    """
    Build a structured selection reason explaining why this product was picked
    and whether all directives were met.

    Returns a dict:
        {
            "summary": "Organic (your preference) + Preferred brand",
            "directive_met": True,
            "compromises": [],  # or ["Not organic -- should be organic"]
        }
    """
    best_option = None
    for opt in result.get("options", []):
        if opt.get("store_id") == result.get("recommended_store_id"):
            best_option = opt
            break

    if not best_option:
        return {
            "summary": "Best available match",
            "directive_met": True,
            "compromises": [],
        }

    reasons = best_option.get("reasons", [])
    if isinstance(reasons, str):
        try:
            import json as _json
            reasons = _json.loads(reasons)
        except Exception:
            reasons = []

    # Extract the directive_met and compromises from the scored result
    directive_met = best_option.get("directive_met", True)
    compromises = best_option.get("compromises", [])

    explanations = []
    for r in reasons:
        r_lower = r.lower() if isinstance(r, str) else ""
        if "organic_match" in r_lower or ("organic" in r_lower and "+" in r and "missing" not in r_lower):
            explanations.append("Organic (your preference)")
        elif "free range" in r_lower and "+" in r:
            explanations.append("Free range (your preference)")
        elif "grass fed" in r_lower and "+" in r:
            explanations.append("Grass fed (your preference)")
        elif "origin_premium" in r_lower:
            explanations.append("Premium origin")
        elif "brand" in r_lower and "preferred" in r_lower and "+" in r:
            explanations.append("Preferred brand")
        elif "frequently ordered" in r_lower or "previously ordered" in r_lower:
            explanations.append("You've bought this before")
        elif "exact match" in r_lower or "name match" in r_lower:
            explanations.append("Exact match")

    if not explanations:
        score = best_option.get("score", 0)
        if score >= 80:
            explanations.append("Top-scored match")
        elif score >= 60:
            explanations.append("Good match")
        else:
            explanations.append("Best available match")

    # Deduplicate and limit to 3
    seen = set()
    unique = []
    for e in explanations:
        if e not in seen:
            seen.add(e)
            unique.append(e)

    summary = " + ".join(unique[:3])

    # Add quantity note if multi-qty
    qty = result.get("recommended_qty", 1)
    if qty > 1:
        size = best_option.get("size", "")
        summary += f" (Order {qty}x {size})" if size else f" (Order qty={qty})"

    # If compromises exist, append a brief note
    if compromises and not directive_met:
        first_compromise = compromises[0]
        # Keep it short for the summary
        if len(first_compromise) > 60:
            first_compromise = first_compromise[:57] + "..."
        summary += f" (Compromise: {first_compromise})"

    return {
        "summary": summary,
        "directive_met": directive_met,
        "compromises": compromises,
    }


def _build_failure_context(raw_name, store_ids, store_names, item_match, conn):
    """
    Build a structured failure explanation with suggestions.

    Returns:
        {
            "reason": str,         # machine-readable: no_catalog_match, out_of_stock, etc.
            "reason_human": str,   # plain English for the user
            "did_you_mean": [...], # top 3 fuzzy matches from catalog
            "stores_checked": [...],
        }
    """
    from product_knowledge import search_catalog

    context = {
        "reason": "no_catalog_match",
        "reason_human": f"'{raw_name}' isn't in our product catalog yet. Try a different name?",
        "did_you_mean": [],
        "stores_checked": store_names,
    }

    # Check if the issue is "out of stock" rather than "not found"
    # by searching with is_available filter relaxed
    all_results = []
    for store_id in store_ids:
        results = search_catalog(raw_name, store_id=store_id, limit=3, conn=conn)
        all_results.extend(results)

    if not all_results:
        # Truly nothing in catalog — try a broader search (no store filter)
        broader = search_catalog(raw_name, limit=5, conn=conn)
        if broader:
            context["reason"] = "partial_match"
            context["reason_human"] = f"No exact match for '{raw_name}'. Did you mean one of these?"
            context["did_you_mean"] = [
                {
                    "name": r.get("product_name", ""),
                    "catalog_id": r.get("catalog_id"),
                    "store_id": r.get("store_id"),
                    "price": r.get("price"),
                    "image_url": r.get("image_url"),
                }
                for r in broader[:3]
            ]
        return context

    # We found results but they were filtered out by relevance scoring or preference.
    # Check if they're out of stock
    unavailable_count = 0
    for r in all_results:
        catalog_row = conn.execute(
            "SELECT is_available FROM product_catalog WHERE id = ?",
            (r.get("catalog_id"),)
        ).fetchone()
        if catalog_row and not catalog_row["is_available"]:
            unavailable_count += 1

    if unavailable_count == len(all_results):
        context["reason"] = "out_of_stock"
        context["reason_human"] = f"'{raw_name}' is out of stock at all stores."
        # Still show what was found so user knows it exists
        context["did_you_mean"] = [
            {
                "name": r.get("product_name", ""),
                "catalog_id": r.get("catalog_id"),
                "store_id": r.get("store_id"),
                "price": r.get("price"),
                "image_url": r.get("image_url"),
                "out_of_stock": True,
            }
            for r in all_results[:3]
        ]
    else:
        # Found products but relevance was too low (false-positive filtered)
        context["reason"] = "low_relevance"
        context["reason_human"] = (
            f"Found some products matching '{raw_name}' but none were a good fit. "
            f"Try being more specific?"
        )
        context["did_you_mean"] = [
            {
                "name": r.get("product_name", ""),
                "catalog_id": r.get("catalog_id"),
                "store_id": r.get("store_id"),
                "price": r.get("price"),
                "image_url": r.get("image_url"),
            }
            for r in all_results[:3]
        ]

    return context


def _match_to_items_table(raw_name, conn):
    """
    Fuzzy match a raw name to the items table.
    Uses stemming so "minced meat" matches "Beef Mince", "apples" matches "Apple".
    Also tries synonym expansion.

    Returns:
        {"item_id", "name", "store_id", "store_name"} — for exact/good matches
        {"is_generic": True, "did_you_mean": [...]} — for generic category terms
        None — for no match at all
    """
    query_lower = raw_name.lower().strip()

    # Check if this is a generic category term (e.g., "meat", "fish", "greens")
    # These should NOT substring-match to wrong items (e.g., "meat" → "Mussels Meat")
    if query_lower in CATEGORY_SYNONYMS:
        specific_items = CATEGORY_SYNONYMS[query_lower]
        # Try to find which specific items exist in the items table
        items = conn.execute("""
            SELECT i.id, i.name, i.preferred_store_id, s.name AS store_name
            FROM items i
            LEFT JOIN stores s ON i.preferred_store_id = s.id
        """).fetchall()

        suggestions = []
        for specific in specific_items:
            specific_lower = specific.lower()
            specific_stems = _normalize_words(specific)
            for item in items:
                name_lower = item["name"].lower()
                name_stems = _normalize_words(item["name"])
                # Exact or strong match to the specific item
                if (specific_lower == name_lower
                    or specific_lower in name_lower
                    or (specific_stems & name_stems and
                        len(specific_stems & name_stems) >= len(specific_stems) * 0.5)):
                    suggestions.append({
                        "item_id": item["id"],
                        "name": item["name"],
                        "store_id": item["preferred_store_id"],
                        "store_name": item["store_name"],
                    })
                    break  # one match per specific item is enough

        if suggestions:
            return {
                "is_generic": True,
                "did_you_mean": suggestions[:6],  # cap at 6 suggestions
                "generic_term": raw_name,
            }
        # If no suggestions found, fall through to normal matching

    # Try the raw name first, then synonyms
    search_terms_list = _expand_search_terms(raw_name, conn=conn)

    items = conn.execute("""
        SELECT i.id, i.name, i.search_terms, i.preferred_store_id,
               s.name AS store_name
        FROM items i
        LEFT JOIN stores s ON i.preferred_store_id = s.id
    """).fetchall()

    best = None
    best_score = 0

    for search_text in search_terms_list:
        query_lower = search_text.lower().strip()
        query_words = set(w for w in query_lower.split() if len(w) >= 2)
        query_stems = _normalize_words(search_text)
        if not query_stems:
            query_stems = {_stem_word(query_lower)}

        for item in items:
            name_lower = item["name"].lower()
            name_stems = _normalize_words(item["name"])
            stem_overlap = query_stems & name_stems
            score = 0

            # Exact name match
            if query_lower == name_lower:
                score = 100
            # Query contained in name (or vice versa, with stemming)
            elif query_lower in name_lower:
                # Guard against false positives: "meat" in "mussels meat"
                # Only trust substring match if the query IS the core noun
                # (not just a modifier appearing in a longer name)
                name_words = name_lower.split()
                if len(name_words) <= 2 or query_lower == name_words[-1]:
                    score = 60
                else:
                    # Query appears as a modifier, not the main item — lower score
                    score = 30
            elif name_lower in query_lower:
                score = 50
            else:
                # Stemmed word overlap ("minced"→"mince" matches "Beef Mince")
                if stem_overlap:
                    score = len(stem_overlap) * 25

                # Also check raw word overlap as fallback
                raw_overlap = query_words & set(name_lower.split())
                if raw_overlap and score < len(raw_overlap) * 20:
                    score = len(raw_overlap) * 20

            # Check search_terms JSON (contains store-specific product names)
            search_terms = item["search_terms"] or ""
            if search_terms:
                st_lower = search_terms.lower()
                if query_lower in st_lower:
                    score += 30
                else:
                    # Check if any query stem appears in search terms
                    st_stems = _normalize_words(search_terms)
                    st_overlap = query_stems & st_stems
                    if len(st_overlap) >= len(query_stems) * 0.5:
                        score += 15

            # Threshold: exact/substring matches (score>=50) are always trusted.
            # For stem-overlap-only matches on multi-word queries, require
            # majority of stems to match to avoid "olive kalamata" → "Olive Oil".
            min_threshold = 25
            if score < 50 and len(query_stems) >= 2:
                match_ratio = len(stem_overlap) / len(query_stems)
                if match_ratio <= 0.5:
                    min_threshold = 60  # need a much stronger signal

            if score > best_score and score >= min_threshold:
                best_score = score
                best = {
                    "item_id": item["id"],
                    "name": item["name"],
                    "store_id": item["preferred_store_id"],
                    "store_name": item["store_name"],
                }

    return best


def _get_best_alternative(item_id, store_id, conn, raw_query=None, purchase_history=None):
    """
    Get the best scored alternative for an item at a specific store.
    Re-scores the top candidates with current preference state so that
    learned preferences (feedback, ratings) always apply.

    raw_query: the user's original search text (e.g., "double cream milk").
               Used to boost products matching user's qualifiers that aren't
               captured in the items table name (e.g., "Milk").
    purchase_history: pre-fetched purchase history from get_purchase_history().
               Enables history-based scoring (brand, size, store, quality boosts).
    Returns option dict or None.
    """
    from product_score import score_single_product

    # Pull top candidates to re-score with latest prefs.
    # Start with standard pool of 5; supplementary catalog search below
    # handles qualifiers that aren't in the alternatives table.
    rows = conn.execute("""
        SELECT ia.score AS original_score, ia.tier, ia.rank, ia.reasons,
               pc.id AS catalog_id, pc.product_name, pc.price, pc.brand,
               pc.size, pc.image_url, pc.is_organic, pc.description,
               pc.category AS product_category, pc.sku, pc.product_url,
               pc.origin_country, pc.ingredients, pc.is_available,
               s.name AS store_name,
               i.name AS item_name
        FROM item_alternatives ia
        JOIN product_catalog pc ON ia.catalog_id = pc.id
        JOIN stores s ON ia.store_id = s.id
        JOIN items i ON ia.item_id = i.id
        WHERE ia.item_id = ? AND ia.store_id = ?
          AND ia.tier != 'avoid'
          AND pc.price > 0
          AND pc.is_available = 1
        ORDER BY ia.rank ASC
        LIMIT 15
    """, (item_id, store_id)).fetchall()

    if not rows:
        return None

    # Extract qualifier words from raw_query that aren't in the items table name
    # e.g., raw_query="double cream milk", item_name="Milk" → qualifiers={"double","cream"}
    # Only include meaningful descriptive modifiers — exclude generic food words
    # (e.g., "meat" in "minced meat" shouldn't boost chicken mince over beef mince)
    query_qualifiers = set()
    if raw_query:
        item_name_lower = rows[0]["item_name"].lower() if rows else ""
        item_stems = {_stem_word(iw) for iw in item_name_lower.split()}
        for w in raw_query.lower().split():
            if len(w) < 3:
                continue
            stem = _stem_word(w)
            if stem in item_stems:
                continue
            # Skip generic food words — they don't narrow product type
            if stem in _FOOD_CATEGORY_STEMS:
                continue
            query_qualifiers.add(stem)

    # If user has qualifiers but none of the alternatives match, supplement with
    # a direct catalog search for the full raw_query at this store.
    # This catches products not in item_alternatives (e.g., "Double Cream Milk" at Carrefour).
    if query_qualifiers:
        has_qualifier_match = False
        for r in rows:
            p_stems = {_stem_word(w) for w in r["product_name"].lower().split()}
            b_stems = {_stem_word(w) for w in (r["brand"] or "").lower().split() if len(w) >= 2}
            if query_qualifiers & (p_stems | b_stems):
                has_qualifier_match = True
                break
        if not has_qualifier_match:
            from product_knowledge import search_catalog
            catalog_hits = search_catalog(raw_query, store_id=store_id, limit=3, conn=conn)
            if catalog_hits:
                store_row = conn.execute(
                    "SELECT name FROM stores WHERE id = ?", (store_id,)).fetchone()
                s_name = store_row["name"] if store_row else rows[0]["store_name"]
                i_name = rows[0]["item_name"]
                # Convert catalog hits to the same dict format as rows
                extra_rows = []
                for ch in catalog_hits:
                    extra_rows.append({
                        "original_score": 0, "tier": "catalog_supplement", "rank": 999,
                        "reasons": "", "catalog_id": ch["catalog_id"],
                        "product_name": ch["product_name"], "price": ch.get("price", 0),
                        "brand": ch.get("brand", ""), "size": ch.get("size", ""),
                        "image_url": ch.get("image_url"), "is_organic": ch.get("is_organic", 0),
                        "description": ch.get("description", ""),
                        "product_category": ch.get("category", ""),
                        "sku": ch.get("sku", ""), "product_url": ch.get("product_url", ""),
                        "origin_country": ch.get("origin_country", ""),
                        "ingredients": ch.get("ingredients", ""),
                        "is_available": ch.get("is_available", 1),
                        "store_name": s_name, "item_name": i_name,
                    })
                rows = list(rows) + extra_rows

    # Re-score each candidate with current preferences (includes learned signals)
    best = None
    best_score = -1
    for row in rows:
        product = {
            "name": row["product_name"],
            "price": row["price"],
            "brand": row["brand"] or "",
            "size": row["size"] or "",
            "description": row["description"] or "",
            "origin_country": row["origin_country"] or "",
            "ingredients": row["ingredients"] or "",
            "is_organic": bool(row["is_organic"]),
            "store_id": store_id,
        }
        scored = score_single_product(
            row["item_name"], row["store_name"], product,
            item_id=item_id,
            purchase_history=purchase_history,
        )
        live_score = scored["score"]
        live_tier = scored["tier"]

        # Check qualifier matches BEFORE disqualification — user explicitly asking
        # for a brand (e.g., "balade yogurt") should override auto-disqualification
        qualifier_match = False
        brand_match = False
        if query_qualifiers:
            product_stems = {_stem_word(w) for w in row["product_name"].lower().split()}
            brand_stems = {_stem_word(w) for w in (row["brand"] or "").lower().split() if len(w) >= 2}
            all_stems = product_stems | brand_stems
            matching_quals = query_qualifiers & all_stems
            if matching_quals:
                qualifier_match = True
                brand_match = bool(query_qualifiers & brand_stems)

        if scored["disqualified"]:
            # Override disqualification when user explicitly requested this brand/qualifier
            if not qualifier_match:
                continue
            # User asked for this — un-disqualify but keep base score as-is

        # Boost based on user's qualifier words
        # "double cream milk" → "Al Ain Double Cream Milk" gets a boost
        # Brand matches get a stronger boost — user explicitly naming a brand is a strong signal
        if qualifier_match:
            live_score += 15 * len(matching_quals)  # bonus per matching qualifier
            if brand_match:
                live_score += 25  # extra boost for explicit brand match

        if live_score > best_score:
            best_score = live_score
            best = {
                "store_id": store_id,
                "store_name": row["store_name"],
                "catalog_id": row["catalog_id"],
                "product_name": row["product_name"],
                "price": row["price"],
                "brand": row["brand"],
                "size": row["size"],
                "image_url": row["image_url"],
                "is_organic": bool(row["is_organic"]),
                "category": row["product_category"] or "",
                "sku": row["sku"] or "",
                "product_url": row["product_url"] or "",
                "score": live_score,
                "tier": live_tier,
                "reasons": scored.get("reasons", []),
                "directive_met": scored.get("directive_met", True),
                "compromises": scored.get("compromises", []),
                "suggested_qty": product.get("_suggested_qty", 1),
                "qualifier_match": qualifier_match,
                "brand_match": brand_match,
            }

    # If best option has no category (Kibsons), try catalog intelligence
    if best and not best.get("category"):
        try:
            from catalog_intelligence import get_category_for_term
            item_label = best.get("item_name") or ""
            cat, _sub = get_category_for_term(item_label, conn=conn)
            if cat:
                best["category"] = cat
        except Exception:
            pass

    return best


def _search_catalog_all_stores(raw_name, store_ids, conn, item_name=None, item_id=None, purchase_history=None):
    """
    Search product_catalog across all stores for a raw item name.
    Uses synonym expansion, relevance scoring to filter false positives,
    then runs top candidates through the preference scoring engine so that
    learned preferences (organic, brand, size) always apply.
    Returns list of best option per store.
    """
    from product_knowledge import search_catalog
    from product_score import score_single_product

    search_terms = _expand_search_terms(raw_name, conn=conn)
    MIN_RELEVANCE = 50  # below this, the product is likely a false positive
    scoring_name = item_name or raw_name  # for preference profile lookup

    options = []
    for store_id in store_ids:
        # Collect all relevant candidates from all search terms
        candidates = []
        seen_ids = set()

        for term in search_terms:
            results = search_catalog(term, store_id=store_id, limit=5, conn=conn)
            for r in results:
                cid = r.get("catalog_id")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)

                # Relevance filter: is this actually the right product?
                relevance = _score_product_relevance(raw_name, r["product_name"])
                if term != raw_name:
                    alt_relevance = _score_product_relevance(term, r["product_name"])
                    # Synonym-expanded terms get a stricter threshold to avoid
                    # false positives like "hair shampoo" matching "Angel Hair pasta"
                    if alt_relevance > relevance:
                        relevance = max(relevance, alt_relevance - 10)

                if relevance >= MIN_RELEVANCE:
                    # Reject pet products for human-oriented searches
                    # "diapers" should match baby diapers, not dog diapers
                    cat = (r.get("category") or "").lower()
                    if "pet" in cat and not any(w in raw_name.lower() for w in ("pet", "dog", "cat ", "fish", "kitten", "puppy")):
                        relevance = 0  # hard reject
                    if relevance >= MIN_RELEVANCE:
                        candidates.append((r, relevance))

        if not candidates:
            continue

        store_row = conn.execute("SELECT name FROM stores WHERE id = ?", (store_id,)).fetchone()
        store_name = store_row["name"] if store_row else "Unknown"

        # Score each relevant candidate through the preference engine
        best = None
        best_score = -1
        for r, relevance in candidates:
            product = {
                "name": r["product_name"],
                "price": r.get("price", 0),
                "brand": r.get("brand", ""),
                "size": r.get("size", ""),
                "description": r.get("description", ""),
                "origin_country": r.get("origin_country", ""),
                "ingredients": r.get("ingredients", ""),
                "is_organic": bool(r.get("is_organic")),
                "store_id": store_id,
            }
            scored = score_single_product(
                scoring_name, store_name, product,
                item_id=item_id,
                purchase_history=purchase_history,
            )
            live_score = scored["score"]
            live_tier = scored["tier"]

            if scored["disqualified"]:
                continue

            # Blend relevance into score: a more relevant product (relevance 95)
            # should beat a less relevant but cheaper one (relevance 60).
            # Scale: relevance 50-100 maps to 0-40 bonus points.
            relevance_bonus = max(0, (relevance - 50)) * 0.8
            live_score += relevance_bonus

            if live_score > best_score:
                best_score = live_score
                best = {
                    "store_id": store_id,
                    "store_name": store_name,
                    "catalog_id": r["catalog_id"],
                    "product_name": r["product_name"],
                    "price": r.get("price", 0),
                    "brand": r.get("brand", ""),
                    "size": r.get("size", ""),
                    "image_url": r.get("image_url"),
                    "is_organic": bool(r.get("is_organic")),
                    "category": r.get("category", ""),
                    "sku": r.get("sku", ""),
                    "product_url": r.get("product_url", ""),
                    "score": live_score,
                    "tier": live_tier,
                    "reasons": scored.get("reasons", []),
                    "directive_met": scored.get("directive_met", True),
                    "compromises": scored.get("compromises", []),
                    "suggested_qty": product.get("_suggested_qty", 1),
                }

        if best:
            # If category is empty (e.g. Kibsons), try catalog intelligence
            if not best.get("category"):
                try:
                    from catalog_intelligence import get_category_for_term
                    cat, _sub = get_category_for_term(raw_name, conn=conn)
                    if cat:
                        best["category"] = cat
                except Exception:
                    pass
            options.append(best)

    return options


def _pick_best_option(result):
    """
    From the available options, pick the best vendor.
    Primary: preference score (encodes organic, brand, variety, etc.).
    Secondary: preferred store (from items table — tiebreaker within same score).
    Tertiary: price (only overrides when savings are significant).
    """
    options = result["options"]
    if not options:
        return

    # Get user's preferred store for this item (set by _curate_single_item)
    pref_store = result.get("preferred_store_id")

    # Filter to acceptable quality (score >= 40)
    viable = [o for o in options if o.get("score", 0) >= 40 and o.get("price", 0) > 0]
    if not viable:
        viable = options  # fallback to whatever we have

    # If any option has a brand/qualifier match (user explicitly asked for it),
    # prefer those options — the user's intent overrides generic scoring
    has_brand_match = any(o.get("brand_match") for o in viable)

    # Sort by: brand match first (if any), score desc, preferred store (tiebreaker), price asc
    viable.sort(key=lambda o: (
        0 if has_brand_match and o.get("brand_match") else 1,
        -o.get("score", 0),
        0 if pref_store and o.get("store_id") == pref_store else 1,
        o.get("price", 999),
    ))
    best = viable[0]

    # Price override: if a cheaper option exists and saves >30% while still
    # being close in score (within 10 points), pick the cheaper one
    if len(viable) > 1:
        cheapest = min(viable, key=lambda o: o.get("price", 999))
        if cheapest != best and cheapest["price"] > 0 and best["price"] > 0:
            savings_pct = (best["price"] - cheapest["price"]) / best["price"]
            score_gap = best.get("score", 0) - cheapest.get("score", 0)
            # Only prefer cheaper if: big savings (>30%) AND small score gap (<10)
            if savings_pct > 0.30 and score_gap < 10:
                best = cheapest

    result["recommended_store_id"] = best["store_id"]
    result["recommended_store"] = best["store_name"]
    result["recommended_price"] = best["price"]
    result["recommended_product"] = best["product_name"]
    # Quantity: usually 1, but >1 for items like individual steaks (2x250g)
    suggested_qty = best.get("suggested_qty", 1)
    result["recommended_qty"] = suggested_qty
    # Adjust total price for multi-qty
    if suggested_qty > 1:
        result["recommended_price"] = best["price"] * suggested_qty


def _optimize_vendor_split(items, stores):
    """
    After initial assignment, check MOV constraints.
    If a store is below its MOV, try to consolidate items to the other store.
    """
    store_map = {s["id"]: s for s in stores}

    # Calculate subtotals per store
    store_totals = {}
    for item in items:
        sid = item.get("recommended_store_id")
        if sid and item["status"] != "unmatched":
            store_totals.setdefault(sid, 0.0)
            store_totals[sid] += item.get("recommended_price", 0) or 0

    # Check each store against its MOV
    for store_id, subtotal in store_totals.items():
        store = store_map.get(store_id, {})
        mov = store.get("min_order_value", 0) or 0

        if mov > 0 and subtotal < mov:
            # This store is below MOV — try to move items to another store
            other_store_ids = [sid for sid in store_totals if sid != store_id]
            if not other_store_ids:
                continue

            target_store_id = other_store_ids[0]  # move to the other store

            for item in items:
                if item.get("recommended_store_id") != store_id:
                    continue
                if item["status"] == "unmatched":
                    continue

                # Check if this item has an acceptable option at the target store
                # Never shift if it would break a directive (organic, type gate, etc.)
                # Also verify the alternative is actually the RIGHT PRODUCT
                # (item_alternatives can have false matches like penne for cucumber)
                alt = None
                item_words = set(item.get("matched_name", item["input_name"]).lower().split())
                for opt in item.get("options", []):
                    if (opt["store_id"] == target_store_id
                            and opt.get("score", 0) >= 40
                            and opt.get("directive_met", True)):
                        # Relevance check: product name must share at least one
                        # meaningful word with the item name
                        prod_words = set(opt.get("product_name", "").lower().split())
                        shared = item_words & prod_words
                        if not shared:
                            continue  # "Mr Organic Penne" has no overlap with "Cucumber"
                        alt = opt
                        break

                if alt:
                    # Move item to the other store
                    item["recommended_store_id"] = alt["store_id"]
                    item["recommended_store"] = alt["store_name"]
                    item["recommended_price"] = alt["price"]
                    item["recommended_product"] = alt["product_name"]
                    item["_consolidated"] = True  # flag for reporting


# ── CLI test ──
if __name__ == "__main__":
    test_items = [
        "onion", "potato", "minced meat", "apples",
        "carrot", "olive kalamata", "fresh milk double cream",
    ]
    result = curate_basket(test_items)
    print(result["summary"])
    print()
    for item in result["items"]:
        status = item["status"]
        if status == "unmatched":
            print(f"  [X] {item['input_name']}: no match")
        else:
            store = item["recommended_store"]
            price = item["recommended_price"]
            product = item["recommended_product"] or "?"
            n_opts = len(item["options"])
            consolidated = " [moved]" if item.get("_consolidated") else ""
            qty = item.get("recommended_qty", 1)
            qty_label = f" x{qty}" if qty > 1 else ""
            print(f"  [OK] {item['input_name']} -> {product[:45]}{qty_label} (AED {price:.2f}, {store}, {n_opts} opts){consolidated}")
