"""
AutoRestock - Household Profile

Centralized household knowledge that every scoring decision references.
This is the "shopper brain" — organic tiers, portion sizing, product-type
gates, and origin sensitivity.

Why this exists:
  The product_score.py PREFERENCE_PROFILES describe per-item preferences.
  This module describes household-level INTELLIGENCE that applies across
  all items: "spinach must be organic because it's Dirty Dozen",
  "500g meat per meal for 3 people", "never wheat pasta".

  Scoring (product_score.py) imports from here. It doesn't duplicate
  this knowledge — it calls get_organic_tier(), check_size_fit(), etc.

Household: 2 adults + 1 child (age 4), Dubai UAE, upper-middle class.
Values: Quality-first, health-conscious, budget-aware on organic
  (spend where it matters, don't waste on Clean Fifteen).
"""

import re
from typing import Optional


# ============================================================
# HOUSEHOLD BASICS
# ============================================================

HOUSEHOLD = {
    "adults": 2,
    "children": 1,
    "child_age": 4,
    "total_people": 3,
    "location": "Dubai, UAE",
    "orders_per_week": 2,       # Tue + Fri
    "days_per_order": 3.5,      # each order covers ~3.5 days
}


# ============================================================
# ORGANIC INTELLIGENCE
# ============================================================
# Dirty Dozen / Clean Fifteen adapted for this household.
#
# "always_organic" = Dirty Dozen + household values. The directive is:
#   pick organic, and if there is NO organic option, explicitly flag it
#   and fall back to the best conventional option.
#
# "prefer_organic" = good to have but don't overpay. Pick organic if
#   the price premium is < 40% over conventional.
#
# "dont_bother" = Clean Fifteen or items where organic doesn't matter.
#   Don't pay the organic premium — conventional is fine.

ORGANIC_TIERS = {
    "always_organic": {
        "items": [
            # Dirty Dozen produce
            "spinach", "rocket", "apple", "strawberry", "grape",
            "pepper", "capsicum", "cherry tomato", "peach", "pear",
            "nectarine", "celery",
            # Household values — dairy & protein quality matters
            "eggs", "milk", "yogurt", "kefir", "cream", "butter",
            "chicken", "chicken breast",
            # Meat — organic/grass-fed is always the right call for this household
            "beef", "beef mince", "striploin", "lamb",
        ],
        "directive": "Must be organic. If no organic option, flag and fallback to best conventional.",
        "penalty_if_missing": 40,  # score penalty when organic is required but missing
    },
    "prefer_organic": {
        "items": [
            # Proteins where organic helps but isn't a hard rule
            "salmon",
            # Produce where organic helps but isn't critical
            "potato", "carrot", "tomato", "cucumber", "lettuce",
            "zucchini", "broccoli",
            # Banana — household prefers organic even though it's Clean Fifteen
            "banana",
            # Dairy
            "cheese",
            # Bread if available
            "bread",
        ],
        "directive": "Pick organic if premium < 40% over conventional. Otherwise pick best conventional and note.",
        "max_premium_pct": 40,  # don't pay more than 40% extra for organic
    },
    "dont_bother": {
        "items": [
            # Clean Fifteen — thick skin or low pesticide
            "avocado", "mango", "pineapple", "onion",
            "sweet corn", "asparagus", "kiwi", "cabbage", "mushroom",
            "honeydew", "cantaloupe", "watermelon", "papaya",
            # Processed/packaged items — organic label doesn't matter
            "pasta", "rice", "oil", "olive oil", "vinegar",
            "spices", "nuts", "cereal", "juice",
        ],
        "directive": "Conventional is fine. Don't pay organic premium.",
    },
}

# Build reverse lookup: item_name ->tier
_ORGANIC_LOOKUP = {}
for _tier_name, _tier_data in ORGANIC_TIERS.items():
    for _item in _tier_data["items"]:
        _ORGANIC_LOOKUP[_item.lower()] = _tier_name


def get_organic_tier(item_name: str) -> str:
    """
    Returns the organic tier for an item.

    Returns: 'always_organic' | 'prefer_organic' | 'dont_bother' | 'unknown'

    Matching strategy:
      1. Exact match on item name (lowercased)
      2. Check if any tier item is a substring of item_name (e.g., "beef" matches "beef mince")
      3. Check if item_name is a substring of a tier item
    """
    name_lower = item_name.lower().strip()

    # Exact match
    if name_lower in _ORGANIC_LOOKUP:
        return _ORGANIC_LOOKUP[name_lower]

    # Substring match: tier item contained in the query
    # e.g., "chicken" matches "organic chicken breast fillet"
    for tier_item, tier_name in _ORGANIC_LOOKUP.items():
        if tier_item in name_lower or name_lower in tier_item:
            return tier_name

    return "unknown"


# ============================================================
# PORTION SIZING RULES
# ============================================================
# Right-sized shopping for a household of 3 (2 adults + 1 child).
# Dubai heat = shorter shelf life for perishables.
#
# These define TARGET sizes per purchase, not per meal.
# check_size_fit() uses these to flag oversized/undersized products.

PORTION_RULES = {
    # Meat & protein — per meal portion ~500g for the household (3 people).
    # allow_multi_qty: True means "it's fine to order 2x250g instead of 1x500g".
    # This is how meat works — striploin steaks come as 250g each, order 2.
    # Mince comes in 500g packs, order 1. Both hit the ~500g target.
    "meat": {
        "target_g": 500,
        "min_g": 150,       # Single unit can be small (e.g., 250g steak)
        "max_g": 750,
        "unit": "g",
        "allow_multi_qty": True,
        "max_qty": 4,       # Don't order more than 4 units
        "note": "~170g/adult + child portion. Can be 2x250g or 1x500g.",
    },
    "striploin": {
        "target_g": 500,
        "min_g": 150,       # Individual steaks are 250-300g
        "max_g": 700,
        "unit": "g",
        "allow_multi_qty": True,
        "max_qty": 3,       # 3 steaks max (one per person)
        "note": "One steak per person. Order 2-3x individual steaks to hit ~500g.",
    },
    "beef mince": {
        "target_g": 500,
        "min_g": 250,
        "max_g": 750,
        "unit": "g",
        "allow_multi_qty": True,
        "max_qty": 3,
        "note": "One meal batch. Usually comes in 500g packs.",
    },
    "chicken breast": {
        "target_g": 500,
        "min_g": 150,
        "max_g": 750,
        "unit": "g",
        "allow_multi_qty": True,
        "max_qty": 3,
        "note": "One meal's worth. Can be multiple smaller packs.",
    },
    "salmon": {
        "target_g": 500,
        "min_g": 150,
        "max_g": 700,
        "unit": "g",
        "allow_multi_qty": True,
        "max_qty": 3,
        "note": "2-3 fillets for one meal. Individual fillets ~200g each.",
    },
    "beef sausages": {
        "target_g": 500,
        "min_g": 250,
        "max_g": 750,
        "unit": "g",
        "allow_multi_qty": True,
        "max_qty": 3,
        "note": "3 breakfast mornings worth (~6 sausages).",
    },

    # Leafy greens — spoil fast in Dubai, buy small and fresh
    "leafy_greens": {
        "target_g": 150,
        "max_g": 300,
        "unit": "g",
        "note": "Spoils in 3 days. Small pack, buy fresh each order.",
    },
    "spinach": {
        "target_g": 150,
        "max_g": 300,
        "unit": "g",
        "note": "Baby spinach. Small pack — spoils in 3 days in Dubai.",
    },
    "rocket": {
        "target_g": 100,
        "max_g": 200,
        "unit": "g",
        "note": "Wild rocket. Very perishable.",
    },

    # Dairy
    "milk": {
        "target_ml": 1000,
        "max_ml": 2000,
        "unit": "ml",
        "note": "For 2 lattes daily. 1-2L per 3.5 day order.",
    },
    "kefir": {
        "target_ml": 500,
        "max_ml": 500,
        "unit": "ml",
        "note": "Slowly consumed. 500ml max — don't buy 1L, it'll expire.",
    },
    "yogurt": {
        "target_g": 950,
        "min_g": 400,
        "max_g": 1200,
        "unit": "g",
        "note": "Plain only. ~1kg tub for family. 2kg is too much, goes off.",
    },
    "eggs": {
        "target_pcs": 12,
        "max_pcs": 15,
        "unit": "pcs",
        "note": "Family of 3, every 3.5 days. 12-15 pack. 30-pack is too many.",
    },
    "cheese": {
        "target_g": 200,
        "max_g": 500,
        "unit": "g",
        "note": "200-400g block. Cheddar varieties.",
    },
    "butter": {
        "target_g": 200,
        "max_g": 500,
        "unit": "g",
        "note": "200g Lurpak. Lasts ~1 week.",
    },

    # Produce
    "apple": {
        "target_pcs": 6,
        "max_pcs": 10,
        "unit": "pcs",
        "note": "For the child. 5-7 apples per order.",
    },
    "banana": {
        "target_g": 1000,
        "max_g": 1500,
        "unit": "g",
        "note": "~1kg bunch. For the child.",
    },
    "cucumber": {
        "target_g": 500,
        "max_g": 1000,
        "unit": "g",
        "note": "500g pack, organic.",
    },
    "pepper": {
        "target_g": 500,
        "max_g": 1000,
        "unit": "g",
        "note": "2 peppers. Different colors for variety.",
    },

    # Pantry
    "pasta": {
        "target_g": 500,
        "max_g": 1000,
        "unit": "g",
        "note": "500g pack. Plant-based only.",
    },
    "bread": {
        "target_g": 400,
        "max_g": 800,
        "unit": "g",
        "note": "1 loaf. Brown/rye/flaxseed.",
    },
    "olive oil": {
        "target_ml": 1000,
        "max_ml": 2000,
        "unit": "ml",
        "note": "1L bottle. Lasts 2-3 weeks.",
    },
}

# Category-level fallbacks for items not in PORTION_RULES
CATEGORY_PORTION_DEFAULTS = {
    "Meat": {"target_g": 500, "max_g": 1000, "unit": "g"},
    "Poultry": {"target_g": 500, "max_g": 1000, "unit": "g"},
    "Seafood": {"target_g": 500, "max_g": 700, "unit": "g"},
    "Dairy": {"target_g": 500, "max_g": 1500, "unit": "g"},
    "Produce": {"target_g": 500, "max_g": 1500, "unit": "g"},
    "Pantry": {"target_g": 500, "max_g": 1500, "unit": "g"},
}


def get_portion_rule(item_name: str) -> Optional[dict]:
    """
    Returns the portion sizing rule for this item, or None if no rule defined.

    Tries exact item match first, then category-level default.
    """
    name_lower = item_name.lower().strip()

    # Exact match
    if name_lower in PORTION_RULES:
        return PORTION_RULES[name_lower]

    # Substring match: "beef mince" ->"meat" via category defaults
    for rule_name, rule in PORTION_RULES.items():
        if rule_name in name_lower or name_lower in rule_name:
            return rule

    # Check leafy greens keywords
    leafy_keywords = ["spinach", "rocket", "arugula", "lettuce", "kale", "chard"]
    if any(kw in name_lower for kw in leafy_keywords):
        return PORTION_RULES.get("leafy_greens")

    return None


def _parse_product_size(size_str: str):
    """
    Parse a product size string into (quantity, unit).

    Returns (float, str) or (None, None).

    Examples:
        "500g"     ->(500.0, "g")
        "1kg"      ->(1000.0, "g")     # normalized to grams
        "2L"       ->(2000.0, "ml")    # normalized to ml
        "500ml"    ->(500.0, "ml")
        "15 PCS"   ->(15.0, "pcs")
        "2.2kg"    ->(2200.0, "g")
    """
    if not size_str:
        return None, None

    text = size_str.strip().lower()

    # Multiplier: "2x15 pcs"
    m = re.search(r'(\d+)\s*x\s*(\d+)\s*(?:pcs|pieces?|pack)', text)
    if m:
        return float(int(m.group(1)) * int(m.group(2))), "pcs"

    # kg ->convert to grams for unified comparison
    m = re.search(r'([\d.]+)\s*kg', text)
    if m:
        return float(m.group(1)) * 1000, "g"

    # grams
    m = re.search(r'([\d.]+)\s*g\b', text)
    if m:
        return float(m.group(1)), "g"

    # liters ->convert to ml
    m = re.search(r'([\d.]+)\s*(?:l|ltr|litre|liter)\b', text)
    if m:
        return float(m.group(1)) * 1000, "ml"

    # ml
    m = re.search(r'([\d.]+)\s*ml\b', text)
    if m:
        return float(m.group(1)), "ml"

    # pieces
    m = re.search(r'(\d+)\s*(?:pcs|pieces?|eggs?|pack|count|tray)\b', text)
    if m:
        return float(m.group(1)), "pcs"

    # Bare number (assume context-dependent)
    m = re.search(r'(\d+)', text)
    if m:
        return float(m.group(1)), "pcs"

    return None, None


def check_size_fit(item_name: str, product_size_str: str) -> tuple:
    """
    Check if a product size fits the household's portion rules.

    Returns: (verdict, reason, suggested_qty)
        verdict: 'good' | 'good_multi' | 'too_large' | 'too_small' | 'unknown'
        reason: human-readable explanation
        suggested_qty: int — how many units to order (1 for most items,
                       2-3 for items like individual steaks where allow_multi_qty=True)

    'good_multi' means: the individual product is smaller than the meal target,
    but ordering suggested_qty units hits the target. E.g., 2x250g striploin = 500g.

    Examples:
        check_size_fit("kefir", "1L")        -> ("too_large", "...", 1)
        check_size_fit("striploin", "2.2kg")  -> ("too_large", "...", 1)
        check_size_fit("striploin", "250g")   -> ("good_multi", "Order 2x 250g = 500g", 2)
        check_size_fit("striploin", "500g")   -> ("good", "...", 1)
        check_size_fit("eggs", "15 PCS")      -> ("good", "...", 1)
    """
    rule = get_portion_rule(item_name)
    if not rule:
        return "unknown", "No portion rule defined", 1

    product_qty, product_unit = _parse_product_size(product_size_str)
    if product_qty is None:
        return "unknown", f"Cannot parse size '{product_size_str}'", 1

    # Determine which rule fields to use based on product unit
    if product_unit == "g" and "target_g" in rule:
        target = rule["target_g"]
        maximum = rule["max_g"]
        unit_label = "g"
    elif product_unit == "ml" and "target_ml" in rule:
        target = rule["target_ml"]
        maximum = rule["max_ml"]
        unit_label = "ml"
    elif product_unit == "pcs" and "target_pcs" in rule:
        target = rule["target_pcs"]
        maximum = rule["max_pcs"]
        unit_label = "pcs"
    elif product_unit == "g" and "target_ml" in rule:
        # Sometimes dairy is measured in g but rule is in ml (close enough)
        target = rule["target_ml"]
        maximum = rule["max_ml"]
        unit_label = "ml/g"
    elif product_unit == "ml" and "target_g" in rule:
        target = rule["target_g"]
        maximum = rule["max_g"]
        unit_label = "g/ml"
    else:
        return "unknown", f"Unit mismatch: product is {product_unit}, rule has {list(k for k in rule if k.startswith('target_'))}", 1

    name_lower = item_name.lower().strip()
    allow_multi = rule.get("allow_multi_qty", False)
    max_qty = rule.get("max_qty", 1)

    # Too large check (even at qty=1 it exceeds max)
    if product_qty > maximum * 1.1:  # 10% tolerance
        return "too_large", f"{product_qty:.0f}{unit_label} exceeds max {maximum}{unit_label} for {name_lower}. {rule.get('note', '')}", 1

    # Check explicit minimum
    min_key = f"min_{rule['unit']}"
    explicit_min = rule.get(min_key)

    # Product is below the target — can we order multiples?
    if allow_multi and product_qty < target * 0.9:  # more than 10% below target
        import math
        qty_needed = math.ceil(target / product_qty)
        # Clamp to max_qty
        qty_needed = min(qty_needed, max_qty)
        total_qty = product_qty * qty_needed

        if total_qty >= target * 0.7 and total_qty <= maximum * 1.1 * qty_needed:
            # Multi-qty gets us into an acceptable range
            return (
                "good_multi",
                f"Order {qty_needed}x {product_qty:.0f}{unit_label} = {total_qty:.0f}{unit_label} for {name_lower}",
                qty_needed,
            )

    # Standard min check for non-multi-qty items
    if explicit_min and product_qty < explicit_min and not allow_multi:
        return "too_small", f"{product_qty:.0f}{unit_label} is too small (min {explicit_min}{unit_label}) for {name_lower}. {rule.get('note', '')}", 1

    # Fallback: if no explicit min and no multi-qty, flag at 30% of target
    if not explicit_min and not allow_multi and product_qty < target * 0.3:
        return "too_small", f"{product_qty:.0f}{unit_label} is too small (target {target}{unit_label}) for {name_lower}", 1

    return "good", f"{product_qty:.0f}{unit_label} within range for {name_lower}", 1


# ============================================================
# PRODUCT-TYPE GATES (hard constraints)
# ============================================================
# These are non-negotiable product type requirements.
# If a product fails a gate, it's an instant reject regardless of score.
#
# These override soft preferences — "we want lentil pasta" means
# wheat pasta is REJECTED, not just scored lower.

PRODUCT_TYPE_GATES = {
    "pasta": {
        "require_type": ["lentil", "chickpea", "edamame", "pea protein",
                         "red lentil", "green lentil", "bean", "legume",
                         "veggie", "vegetable"],
        "reject_type": ["wheat", "semolina", "durum", "white pasta",
                        "refined", "macaroni"],
        "directive": "Plant-based pasta only. Lentil, chickpea, or bean-based. Never refined wheat.",
        "fallback": "If no plant-based available, pick whole wheat as last resort and flag compromise.",
    },
    "yogurt": {
        "require_type": ["plain", "natural", "set", "stirred"],
        "reject_type": ["greek", "flavored", "flavoured", "fruit", "vanilla",
                        "strawberry", "mango", "honey", "chocolate",
                        "blueberry", "peach", "coconut flavour"],
        "directive": "Plain/natural yogurt ONLY. Never Greek style. Never flavored.",
        "fallback": "If no plain available, skip yogurt entirely — don't buy flavored.",
    },
    "bread": {
        "require_type": [],  # no hard type requirement
        "reject_type": ["white bread", "brioche", "sweet", "cake",
                        "croissant", "pastry"],
        "reject_ingredients": [
            "preservative", "emulsifier", "e471", "e472", "e481",
            "e322", "e300", "palm oil", "margarine", "corn syrup",
            "high fructose", "sodium benzoate", "potassium sorbate",
            "calcium propionate", "artificial color", "artificial colour",
        ],
        "directive": "Brown/rye/flaxseed bread. Review ingredients — reject if additives/preservatives.",
        "fallback": "If no clean-ingredient bread, pick sourdough or simplest ingredients.",
    },
    "olive oil": {
        "require_type": ["extra virgin"],
        "reject_type": ["pomace", "light olive", "refined", "blended",
                        "vegetable oil", "canola"],
        "directive": "Extra virgin olive oil only.",
    },
    "butter": {
        "require_type": ["unsalted"],
        "reject_type": ["salted", "margarine", "spread", "light butter"],
        "directive": "Unsalted butter only. Lurpak preferred.",
    },
    "salmon": {
        "require_type": ["fillet", "fresh", "whole"],
        "reject_type": ["canned", "smoked", "fishcake", "fish cake",
                        "fish finger", "fish stick", "croquette",
                        "breaded", "battered", "frozen processed"],
        "directive": "Fresh salmon fillets only. Never processed fish products.",
    },
}

# Build reverse lookup
_GATE_LOOKUP = {}
for _gate_name, _gate_data in PRODUCT_TYPE_GATES.items():
    _GATE_LOOKUP[_gate_name.lower()] = _gate_data


def get_product_type_gate(item_name: str) -> Optional[dict]:
    """
    Returns the product-type gate for this item, or None if no gate defined.

    A gate defines hard constraints: require_type keywords that MUST appear,
    reject_type keywords that cause instant rejection.
    """
    name_lower = item_name.lower().strip()

    # Exact match
    if name_lower in _GATE_LOOKUP:
        return _GATE_LOOKUP[name_lower]

    # Substring match
    for gate_name, gate_data in _GATE_LOOKUP.items():
        if gate_name in name_lower or name_lower in gate_name:
            return gate_data

    return None


def check_product_type_gate(item_name: str, product_name: str,
                            product_description: str = "",
                            product_ingredients: str = "") -> tuple:
    """
    Check if a product passes the type gate for an item.

    Returns: (passes, reason)
        passes: True if product is OK, False if rejected
        reason: explanation string

    Example:
        check_product_type_gate("pasta", "Barilla Semolina Penne 500g")
        ->(False, "Rejected: 'semolina' — Plant-based pasta only.")

        check_product_type_gate("pasta", "Earth Goods Red Lentil Fusilli 250g")
        ->(True, "Passes: 'lentil' type match")
    """
    gate = get_product_type_gate(item_name)
    if not gate:
        return True, "No type gate defined"

    full_text = f"{product_name} {product_description}".lower()
    ingredients_lower = product_ingredients.lower() if product_ingredients else ""

    # Check require_type FIRST — if a required type matches, product passes immediately.
    # This prevents false rejections (e.g., "unsalted" passes before "salted" rejects).
    require_types = gate.get("require_type", [])
    if require_types:
        has_required = False
        for req_kw in require_types:
            if req_kw.lower() in full_text:
                has_required = True
                break
        if has_required:
            return True, f"Passes: '{req_kw}' type match"
        # None of the required types found
        return False, f"Missing required type ({', '.join(require_types[:3])}...) — {gate.get('directive', '')}"

    # Check reject_type (instant fail) — only if no require_type is defined
    # or if we already passed require_type above (unreachable here).
    # Use word boundary matching to avoid "salted" matching "unsalted"
    import re as _re
    for reject_kw in gate.get("reject_type", []):
        reject_lower = reject_kw.lower()
        # Word boundary check: reject "salted" but not "unsalted"
        if _re.search(r'\b' + _re.escape(reject_lower) + r'\b', full_text):
            # Extra check: make sure it's not negated by a prefix like "un", "non-"
            match = _re.search(r'(\w*)' + _re.escape(reject_lower), full_text)
            if match:
                prefix = match.group(1)
                if prefix in ("un", "non"):
                    continue  # "unsalted" or "non-salted" — not a rejection
            return False, f"Rejected: '{reject_kw}' — {gate.get('directive', '')}"

    # Check reject_ingredients (bread, etc.)
    for reject_ing in gate.get("reject_ingredients", []):
        if reject_ing.lower() in ingredients_lower or reject_ing.lower() in full_text:
            return False, f"Rejected ingredient: '{reject_ing}' — {gate.get('directive', '')}"

    return True, "Passes type gate"


# ============================================================
# ORIGIN SENSITIVITY
# ============================================================
# How much this household cares about origin per food category.
# The actual tier rankings (Australia > Brazil for meat, etc.) come
# from product_grading.ORIGIN_TIERS — we don't duplicate that data.
# This just says how much to WEIGHT origin in scoring.

ORIGIN_SENSITIVE_CATEGORIES = {
    "meat": "high",           # Australian/NZ/Max beef >> Brazilian
    "beef": "high",
    "striploin": "high",
    "beef mince": "high",
    "lamb": "high",
    "poultry": "high",        # NZ/Max/French chicken >> mass-produced
    "chicken": "high",
    "chicken breast": "high",
    "seafood": "high",        # Norwegian/Scottish salmon >> farmed Asian
    "salmon": "high",
    "dairy": "medium",        # Max/Danish butter > local
    "milk": "medium",
    "cheese": "medium",
    "yogurt": "low",          # Origin less important for yogurt
    "butter": "medium",
    "produce": "low",         # Origin less important if organic
    "apple": "low",
    "banana": "low",
    "pantry": "medium",       # Italian pasta/olive oil matters
    "pasta": "medium",
    "olive oil": "high",      # Italian/Spanish/Greek EVOO >> others
    "bread": "low",
}

# Score multipliers per sensitivity level
ORIGIN_WEIGHTS = {
    "high": 1.0,      # Full points: +20 for premium, -20 for avoid
    "medium": 0.6,    # Moderate: +12 for premium, -12 for avoid
    "low": 0.3,       # Mild: +6 for premium, -6 for avoid
}

# Points per origin tier (before multiplier)
ORIGIN_SCORES = {
    "premium": 20,
    "good": 10,
    "standard": 0,
    "avoid": -20,
    "unknown": 0,
}


def get_origin_sensitivity(item_name: str) -> str:
    """
    Returns how much this household cares about origin for this item.

    Returns: 'high' | 'medium' | 'low'
    """
    name_lower = item_name.lower().strip()

    # Exact match
    if name_lower in ORIGIN_SENSITIVE_CATEGORIES:
        return ORIGIN_SENSITIVE_CATEGORIES[name_lower]

    # Substring match
    for cat_name, sensitivity in ORIGIN_SENSITIVE_CATEGORIES.items():
        if cat_name in name_lower or name_lower in cat_name:
            return sensitivity

    # Default
    return "low"


def calculate_origin_score(item_name: str, origin_country: str, category: str = "") -> tuple:
    """
    Calculate origin-based score adjustment for a product.

    Uses product_grading.ORIGIN_TIERS for the actual tier lookup,
    and household sensitivity to weight the result.

    Args:
        item_name: e.g., "Beef Mince"
        origin_country: e.g., "Australia"
        category: e.g., "Meat" (product_grading category)

    Returns: (points: int, reason: str or None)
        e.g., (20, "+origin_premium (Australia, Meat)")
        e.g., (-12, "-origin_avoid (Brazil, Meat)")
        e.g., (0, None)
    """
    if not origin_country or origin_country.strip() in ("", "N/A", "Unknown"):
        return 0, None

    # Import origin tier lookup from grading module
    try:
        from product_grading import ORIGIN_TIERS
    except ImportError:
        return 0, None

    # Determine the grading category to look up
    origin_lower = origin_country.lower().strip()
    lookup_category = _map_to_grading_category(item_name, category)

    if lookup_category not in ORIGIN_TIERS:
        return 0, None

    # Find tier
    tier_data = ORIGIN_TIERS[lookup_category]
    origin_tier = "unknown"
    for tier_name in ["premium", "good", "standard", "avoid"]:
        if origin_lower in tier_data.get(tier_name, []):
            origin_tier = tier_name
            break

    if origin_tier == "unknown" or origin_tier == "standard":
        return 0, None

    # Apply sensitivity weight
    sensitivity = get_origin_sensitivity(item_name)
    weight = ORIGIN_WEIGHTS.get(sensitivity, 0.5)
    raw_points = ORIGIN_SCORES.get(origin_tier, 0)
    points = int(raw_points * weight)

    if points == 0:
        return 0, None

    sign = "+" if points > 0 else ""
    reason = f"{sign}origin_{origin_tier} ({origin_country}, {lookup_category})"
    return points, reason


def _map_to_grading_category(item_name: str, category: str = "") -> str:
    """
    Map an item name or category to the product_grading ORIGIN_TIERS category.

    ORIGIN_TIERS uses: Meat, Poultry, Seafood, Dairy, Produce, Pantry
    """
    name_lower = item_name.lower()

    # Direct category mappings
    meat_items = ["beef", "striploin", "lamb", "mince", "sausage", "veal"]
    poultry_items = ["chicken", "turkey", "duck"]
    seafood_items = ["salmon", "fish", "shrimp", "prawn", "tuna", "cod", "sea bass"]
    dairy_items = ["milk", "cheese", "yogurt", "yoghurt", "butter", "cream",
                   "kefir", "eggs"]
    produce_items = ["apple", "banana", "spinach", "rocket", "pepper", "tomato",
                     "cucumber", "potato", "carrot", "onion", "lettuce",
                     "zucchini", "broccoli"]
    pantry_items = ["pasta", "rice", "bread", "olive oil", "oil", "vinegar",
                    "cereal", "flour"]

    for kw in meat_items:
        if kw in name_lower:
            return "Meat"
    for kw in poultry_items:
        if kw in name_lower:
            return "Poultry"
    for kw in seafood_items:
        if kw in name_lower:
            return "Seafood"
    for kw in dairy_items:
        if kw in name_lower:
            return "Dairy"
    for kw in produce_items:
        if kw in name_lower:
            return "Produce"
    for kw in pantry_items:
        if kw in name_lower:
            return "Pantry"

    # Fallback to category string
    cat_map = {
        "meat": "Meat", "poultry": "Poultry", "seafood": "Seafood",
        "dairy": "Dairy", "produce": "Produce", "pantry": "Pantry",
        "fruit": "Produce", "vegetable": "Produce",
    }
    if category:
        return cat_map.get(category.lower(), "Pantry")

    return "Pantry"


# ============================================================
# UTILITY: Get all household intelligence for an item
# ============================================================

def get_household_intelligence(item_name: str) -> dict:
    """
    One-call summary of all household knowledge for an item.

    Returns:
        {
            "organic_tier": "always_organic" | "prefer_organic" | "dont_bother" | "unknown",
            "organic_directive": str,
            "portion_rule": dict | None,
            "type_gate": dict | None,
            "origin_sensitivity": "high" | "medium" | "low",
        }
    """
    organic_tier = get_organic_tier(item_name)
    organic_data = ORGANIC_TIERS.get(organic_tier, {})

    return {
        "organic_tier": organic_tier,
        "organic_directive": organic_data.get("directive", ""),
        "portion_rule": get_portion_rule(item_name),
        "type_gate": get_product_type_gate(item_name),
        "origin_sensitivity": get_origin_sensitivity(item_name),
    }


# ============================================================
# Self-test
# ============================================================

if __name__ == "__main__":
    print("=== Household Profile Self-Test ===\n")

    # Organic tiers
    test_items = [
        ("Spinach", "always_organic"),
        ("Eggs", "always_organic"),
        ("Banana", "prefer_organic"),
        ("Pasta", "dont_bother"),
        ("Beef Mince", "always_organic"),
        ("Salmon", "prefer_organic"),
        ("Milk", "always_organic"),
        ("Apple", "always_organic"),
    ]
    print("Organic Tiers:")
    for item, expected in test_items:
        actual = get_organic_tier(item)
        status = "OK" if actual == expected else f"FAIL (got {actual})"
        print(f"  {item:20s} -> {actual:20s} {status}")

    # Size checks
    print("\nSize Checks:")
    size_tests = [
        ("kefir", "1L", "too_large", 1),
        ("kefir", "500ml", "good", 1),
        ("striploin", "2.2kg", "too_large", 1),
        ("striploin", "500g", "good", 1),
        ("striploin", "250g", "good_multi", 2),     # 2x250g = 500g
        ("striploin", "300g", "good_multi", 2),     # 2x300g = 600g
        ("beef mince", "500g", "good", 1),          # 1x500g = perfect
        ("beef mince", "250g", "good_multi", 2),    # 2x250g = 500g
        ("salmon", "200g", "good_multi", 3),        # 3x200g = 600g (3 fillets)
        ("eggs", "30 PCS", "too_large", 1),
        ("eggs", "15 PCS", "good", 1),
        ("eggs", "12 PCS", "good", 1),
    ]
    for item, size, expected_verdict, expected_qty in size_tests:
        verdict, reason, qty = check_size_fit(item, size)
        v_ok = verdict == expected_verdict
        q_ok = qty == expected_qty
        status = "OK" if (v_ok and q_ok) else f"FAIL (got {verdict}, qty={qty})"
        print(f"  {item:15s} {size:10s} -> {verdict:12s} qty={qty}  {status}")
        if not (v_ok and q_ok):
            print(f"    Reason: {reason}")

    # Type gates
    print("\nType Gates:")
    gate_tests = [
        ("pasta", "Barilla Semolina Penne 500g", False),
        ("pasta", "Earth Goods Red Lentil Fusilli 250g", True),
        ("yogurt", "Balade Greek Style Yoghurt 950g", False),
        ("yogurt", "Al Ain Plain Yogurt 500g", True),
        ("salmon", "Fish Cake Salmon Breaded 400g", False),
        ("salmon", "Norwegian Salmon Fillet 500g", True),
    ]
    for item, product, expected_pass in gate_tests:
        passes, reason = check_product_type_gate(item, product)
        status = "OK" if passes == expected_pass else f"FAIL (got {passes})"
        print(f"  {item:10s} | {product:45s} ->{'PASS' if passes else 'REJECT':6s} {status}")
        if passes != expected_pass:
            print(f"    Reason: {reason}")

    # Origin
    print("\nOrigin Sensitivity:")
    origin_tests = [
        ("Beef Mince", "Australia", "Meat"),
        ("Beef Mince", "Brazil", "Meat"),
        ("Salmon", "Norway", "Seafood"),
        ("Banana", "Ecuador", "Produce"),
        ("Olive Oil", "Italy", "Pantry"),
    ]
    for item, origin, cat in origin_tests:
        points, reason = calculate_origin_score(item, origin, cat)
        print(f"  {item:15s} from {origin:12s} ->{points:+d} pts  ({reason})")
