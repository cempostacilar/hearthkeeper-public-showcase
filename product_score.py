"""
AutoRestock - Product Intelligence & Scoring (Slice 7b)

When the scraper searches a store, it gets back a list of products.
This module decides WHICH product to pick — not just by name matching,
but by understanding what makes a product "good" for this household.

Three modes:
  1. EXACT MATCH  — preferred product found → instant, no scoring needed
  2. LOCAL SCORE   — score results using preference rules (fast, no API call)
  3. CLAUDE SCORE  — send to Claude API for complex evaluation (smart, ~$0.001)

Flow:
  scrape results → try exact match → if no match → score locally → if unsure → ask Claude

The scoring considers:
  - Required attributes (organic, unsalted) — must have, instant reject if missing
  - Preferred brands/varieties — big bonus
  - Quality signals (grass fed, free range, imported) — moderate bonus
  - Avoid signals (conventional, generic, certain origins) — penalty
  - Price proximity — is it in the expected range?
  - Size match — right pack size?

Usage:
  from product_score import score_products, PREFERENCE_PROFILES

  # scrape_results = [{"name": "...", "price": 34.49, "size": "15 PCS", ...}, ...]
  ranked = score_products("Eggs", "Carrefour UAE", scrape_results)
  # Returns: [{"product": {...}, "score": 85, "reasons": [...], "tier": "preferred"}, ...]
"""

import re
import sqlite3
from db_setup import DB_PATH


# ────────────────────────────────────────────
# HOUSEHOLD PREFERENCES
# ────────────────────────────────────────────
# Global preferences that apply to ALL items, not just those in the items table.
# Used as a fallback when item_id is None (catalog-only items).

_household_profile_cache = None


def get_household_profile():
    """
    Build a scoring profile from household-level preferences.

    Used as fallback when item_id is None (catalog-only items) AND item_name
    doesn't match any static PREFERENCE_PROFILES. This ensures that global
    preferences like "prefer organic" apply to unknown items too.

    Returns a dict in the same format as PREFERENCE_PROFILES entries.
    """
    global _household_profile_cache
    if _household_profile_cache is not None:
        return _household_profile_cache

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        prefs = {
            row["pref_key"]: row["pref_value"]
            for row in conn.execute("SELECT pref_key, pref_value FROM household_preferences")
        }
        conn.close()
    except Exception:
        return {}

    profile = {}

    # Build "prefer" keywords list
    prefer_kw = prefs.get("prefer_keywords", "")
    if prefer_kw:
        profile["prefer"] = [k.strip() for k in prefer_kw.split(",") if k.strip()]

    # Build "avoid" keywords list
    avoid_kw = prefs.get("avoid_keywords", "")
    if avoid_kw:
        profile["avoid"] = [k.strip() for k in avoid_kw.split(",") if k.strip()]

    _household_profile_cache = profile
    return profile



# ────────────────────────────────────────────
# SIBLING-NOUN QUALIFIERS (Phase 4)
# ────────────────────────────────────────────
# Words that, when prepended to a noun in a product name, turn it into
# a DIFFERENT food rather than a variant of the same food. e.g.:
#   "Sweet Potatoes"  is not  "Potatoes"   (sibling vegetable)
#   "Pearl Onions"    is not  "Onions"     (sibling cultivar)
#   "Spring Onions"   is not  "Onions"     (totally different plant)
#   "Kids Yoghurt"    is not  "Yoghurt"    (kids product, smaller pack, sugary)
#
# If the product name contains "<qualifier> <item_noun>" AND the item
# name itself does NOT contain that qualifier, apply a penalty so the
# bare-noun product outranks the qualifier-prefixed one. Soft penalty
# (not a hard gate) so the qualifier-prefixed product can still win if
# nothing better is available.
#
# Conservative starter list: words that historically caused wrong picks
# in Hearthkeeper. Extend as new regressions surface.
_SIBLING_NOUN_QUALIFIERS = {
    "sweet",     # Sweet Potatoes ≠ Potatoes
    "pearl",     # Pearl Onions ≠ Onions
    "spring",    # Spring Onions ≠ Onions
    "kids",      # Kids Yoghurt ≠ family Yoghurt
    "infant",    # Infant Formula ≠ Milk
    "toddler",   # Toddler Milk ≠ regular Milk
    "candied",   # Candied Almonds ≠ raw Almonds
    "smoked",    # Smoked Salmon ≠ fresh Salmon (sibling form)
    "shredded",  # Shredded Cheese ≠ block Cheese (different format)
    "powdered",  # Powdered Milk ≠ fresh Milk
}


def _sibling_noun_penalty(item_name, product_name):
    """Return (penalty_points, reasons_list) for products whose name
    prepends a sibling-noun qualifier to an item noun.

    Item "Potatoes" + product "Del Monte Sweet Potatoes - 400g" finds
    "sweet" prefixed to "potatoes" in the product name. "sweet" is in
    the qualifier set and is NOT in the item name -> -15 penalty.

    Returns (0, []) when no sibling-noun qualifier is found, or when
    the item name itself already includes the qualifier (e.g. item
    name IS "Sweet Potatoes" and product is also "Sweet Potatoes").
    """
    if not item_name or not product_name:
        return 0, []
    item_lower = item_name.lower()
    name_lower = product_name.lower()
    item_words = set(re.findall(r"\b[a-z]+\b", item_lower))
    # Extract item nouns the same way product_knowledge does, so we
    # check the same set of "core" words.
    item_noun_words = [
        w for w in re.findall(r"\b[a-z]+\b", item_lower)
        if len(w) > 2 and w not in {
            "organic", "fresh", "mixed", "premium", "baby", "brown",
            "red", "green", "yellow", "white", "orange", "purple", "black",
            "ripe", "wild", "regular", "extra", "raw", "whole",
            "the", "a", "an", "of", "and", "or",
        }
    ]
    if not item_noun_words:
        return 0, []

    # Build noun variants (singular/plural + regional spellings) so
    # "Potatoes" finds "Potato" AND "Yogurt" finds "Yoghurt".
    # Mirrors product_knowledge._noun_variants; duplicated here to
    # avoid a circular import (product_knowledge -> product_score).
    _spelling_variants = {
        "yogurt": {"yoghurt"}, "yoghurt": {"yogurt"},
        "flavor": {"flavour"}, "flavour": {"flavor"},
        "color": {"colour"},   "colour": {"color"},
        "fiber": {"fibre"},    "fibre": {"fiber"},
        "donut": {"doughnut"}, "doughnut": {"donut"},
        "eggplant": {"aubergine"}, "aubergine": {"eggplant"},
        "zucchini": {"courgette"}, "courgette": {"zucchini"},
        "garbanzo": {"chickpea"},  "chickpea": {"garbanzo"},
    }

    def _variants(n):
        out = {n}
        if n.endswith("s") and len(n) > 3:
            out.add(n[:-1])
        else:
            out.add(n + "s")
        if n.endswith("ies") and len(n) > 4:
            out.add(n[:-3] + "y")
        elif n.endswith("y") and len(n) > 3:
            out.add(n[:-1] + "ies")
        # Regional / spelling variants -- and their plurals
        extras = set()
        for v in list(out):
            for alt in _spelling_variants.get(v, set()):
                extras.add(alt)
                extras.add(alt + "s")
        out.update(extras)
        return out

    penalty = 0
    reasons = []
    seen_qualifiers = set()
    for noun in item_noun_words:
        for variant in _variants(noun):
            # Look for "<word> <variant>" anywhere in the product name
            for match in re.finditer(
                rf"\b([a-z]+)\s+{re.escape(variant)}\b", name_lower
            ):
                preceding = match.group(1)
                if (
                    preceding in _SIBLING_NOUN_QUALIFIERS
                    and preceding not in item_words
                    and preceding not in seen_qualifiers
                ):
                    seen_qualifiers.add(preceding)
                    penalty -= 15
                    reasons.append(
                        f"-sibling_noun: '{preceding} {variant}' is not "
                        f"the same as '{item_name}' (-15)"
                    )
    return penalty, reasons


# ────────────────────────────────────────────
# PREFERENCE PROFILES
# ────────────────────────────────────────────
# One per item. Structured version of the free-text "notes".
# This is what makes the system understand QUALITY, not just names.
#
# Fields:
#   require:     list of keywords that MUST appear in the product (deal-breaker if missing)
#   prefer:      list of keywords that earn bonus points (nice to have)
#   avoid:       list of keywords that get penalized (not wanted)
#   brands:      dict with "preferred", "acceptable", "avoid" brand lists
#   varieties:   acceptable product varieties (for produce mainly)
#   size_target: preferred pack size string to match
#   price_range: {"target": expected price, "max": won't pay more than this}
#   flexibility: "strict" = don't substitute, "moderate" = similar quality OK, "flexible" = any decent option

PREFERENCE_PROFILES = {
    # ── EGGS ──
    # Organic free-range, 12-15 pack. Jenan preferred brand.
    # Dirty Dozen: always organic.
    "Eggs": {
        "require": ["organic"],
        "prefer": ["free range", "pasture", "farm", "organic"],
        "avoid": ["cage", "conventional", "barn", "6 pcs", "6 pack"],
        "brands": {
            "preferred": ["Jenan"],
            "acceptable": ["Emirates Bio Farm", "Al Ain"],
            "avoid": [],
        },
        "varieties": [],
        "size_target": "15 PCS",
        "price_range": {"target": 34.49, "max": 50.00},
        "flexibility": "moderate",
        "directive": "Organic free-range eggs, 12-15 pack",
        "fallback": "If no organic, pick free-range conventional and flag",
    },

    # ── CHEESE ──
    # Cheddar varieties. Max/aged preferred. Glenstal or Snowdonia.
    "Cheese": {
        "require": [],
        "prefer": ["cheddar", "irish", "smoked", "aged", "mature"],
        "avoid": ["processed", "spread", "slice singles", "string cheese",
                  "cream cheese"],
        "brands": {
            "preferred": ["Glenstal", "Snowdonia"],
            "acceptable": ["Kerrygold", "Dubliner", "Cathedral City"],
            "avoid": ["Kraft", "La Vache Qui Rit"],
        },
        "varieties": ["Cheddar", "Smoked Cheddar", "Irish Cheddar", "Mild Cheddar"],
        "size_target": "200g",
        "price_range": {"target": 20.00, "max": 35.00},
        "flexibility": "moderate",
        "directive": "Quality cheddar, Max or aged. 200-400g block",
        "fallback": "Any decent cheddar is fine",
    },

    # ── YOGURT ──
    # PLAIN ONLY. Never Greek style. Never flavored.
    # This is a hard household rule.
    "Yogurt": {
        "require": [],
        "prefer": ["plain", "natural", "full fat", "set"],
        "avoid": ["greek", "flavored", "flavoured", "fruit", "vanilla",
                  "strawberry", "mango", "honey", "chocolate", "blueberry",
                  "low fat", "0%", "diet", "light", "coconut"],
        "brands": {
            "preferred": ["Balade"],
            "acceptable": ["Almarai", "Al Ain", "Al Rawabi"],
            "avoid": [],
        },
        "varieties": ["Plain Yogurt", "Natural Yogurt", "Set Yogurt"],
        "size_target": "1KG",
        "price_range": {"target": 12.00, "max": 30.00},
        "flexibility": "strict",
        "directive": "Plain/natural yogurt ONLY. Never Greek. Never flavored.",
        "fallback": "If no plain available, skip entirely",
    },

    # ── BEEF MINCE ──
    # Grass-fed preferred, organic tier: prefer_organic.
    # Premium origin (Australia, NZ, Ireland) preferred.
    "Beef Mince": {
        "require": [],
        "prefer": ["grass fed", "grass-fed", "organic", "angus", "free range"],
        "avoid": ["frozen", "processed", "seasoned", "pre-cooked"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": [],
        "size_target": "500g",
        "price_range": {"target": 36.20, "max": 55.00},
        "flexibility": "moderate",
        "directive": "Grass-fed or organic beef mince, 500g, premium origin",
        "fallback": "Best quality fresh mince available",
    },

    # ── APPLE ──
    # Dirty Dozen: must be organic.
    # Royal Gala, Juliet, Fuji varieties.
    "Apple": {
        "require": ["organic"],
        "prefer": ["organic", "gala", "juliet", "fuji", "pink lady"],
        "avoid": ["conventional", "dried", "juice", "sauce"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": ["Royal Gala", "Juliet", "Fuji", "Granny Smith", "Pink Lady"],
        "size_target": "",
        "price_range": {"target": 13.80, "max": 25.00},
        "flexibility": "moderate",
        "directive": "Organic apples (Dirty Dozen)",
        "fallback": "If no organic, pick imported premium and flag",
    },

    # ── BANANA ──
    # Clean Fifteen: organic NOT needed. Don't waste money.
    "Banana": {
        "require": [],
        "prefer": ["fresh", "ripe"],
        "avoid": ["dried", "chips", "plantain"],
        "brands": {
            "preferred": ["Chiquita"],
            "acceptable": ["Dole"],
            "avoid": [],
        },
        "varieties": [],
        "size_target": "1kg",
        "price_range": {"target": 12.25, "max": 20.00},
        "flexibility": "flexible",
        "directive": "Fresh bananas. Organic not needed (Clean Fifteen)",
        "fallback": "Any decent fresh banana",
    },

    # ── CUCUMBER ──
    # Organic required (prefer_organic tier + household preference).
    "Cucumber": {
        "require": ["organic"],
        "prefer": ["organic", "fresh"],
        "avoid": ["conventional", "pickle", "pickling", "mini", "snack"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": [],
        "size_target": "500g",
        "price_range": {"target": 6.99, "max": 15.00},
        "flexibility": "strict",
        "directive": "Organic cucumber only",
        "fallback": "If no organic, flag and pick freshest conventional",
    },

    # ── KEFIR ──
    # 500ml MAX. Slowly consumed, bigger packs expire.
    # Organic preferred.
    "Kefir": {
        "require": [],
        "prefer": ["organic", "natural", "plain", "probiotic"],
        "avoid": ["flavored", "strawberry", "mango", "vanilla"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": ["Kefir Yoghurt", "Original Kefir", "Plain Kefir"],
        "size_target": "500ml",
        "price_range": {"target": 26.55, "max": 40.00},
        "flexibility": "moderate",
        "directive": "Organic kefir, 500ml max (slowly consumed, spoilage risk)",
        "fallback": "Any plain kefir, small size",
    },

    # ── MILK ──
    # Must be organic (always_organic tier).
    # Full fat, fresh. Used for lattes.
    "Milk": {
        "require": ["organic"],
        "prefer": ["organic", "full fat", "fresh", "whole"],
        "avoid": ["skimmed", "skim", "low fat", "uht", "long life", "powder",
                  "flavored", "chocolate"],
        "brands": {
            "preferred": [],
            "acceptable": ["Al Ain", "Almarai", "Oatly"],
            "avoid": [],
        },
        "varieties": ["Organic Milk", "Full Fat Milk", "Whole Milk"],
        "size_target": "1L",
        "price_range": {"target": 15.00, "max": 30.00},
        "flexibility": "moderate",
        "directive": "Organic full-fat milk. 1-2L per order",
        "fallback": "If no organic milk, pick Al Ain Fresh and flag",
    },

    # ── PEPPER ──
    # Bell peppers / capsicums. Organic preferred (Dirty Dozen).
    "Pepper": {
        "require": [],
        "prefer": ["organic", "capsicum", "bell pepper", "mixed"],
        "avoid": ["chili", "hot", "jalapeno", "dried", "cayenne", "paprika"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": ["Green Capsicum", "Red Capsicum", "Yellow Capsicum", "Bell Pepper"],
        "size_target": "500g",
        "price_range": {"target": 5.99, "max": 15.00},
        "flexibility": "flexible",
        "directive": "Organic bell peppers. Pick different colors for variety",
        "fallback": "Any fresh bell peppers",
    },

    # ── PASTA ──
    # PLANT-BASED ONLY. Lentil, chickpea, edamame.
    # Never refined wheat. Earth Goods preferred brand.
    "Pasta": {
        "require": [],  # type gate handles this (in household_profile)
        "prefer": ["lentil", "chickpea", "edamame", "pea protein",
                   "bean", "legume", "red lentil", "green lentil"],
        "avoid": ["white", "instant", "cup", "wheat", "semolina", "durum",
                  "refined"],
        "brands": {
            "preferred": ["Earth Goods"],
            "acceptable": ["Barilla Legume", "Explore Cuisine"],
            "avoid": [],
        },
        "varieties": ["Fusilli", "Penne", "Orzo", "Spaghetti"],
        "size_target": "500g",
        "price_range": {"target": 11.75, "max": 22.00},
        "flexibility": "strict",
        "directive": "Plant-based pasta only (lentil/chickpea). Never wheat.",
        "fallback": "If no plant-based, pick whole wheat as last resort and flag",
    },

    # ── OLIVE OIL ──
    # Extra virgin only. Bertolli preferred. Italian/Spanish/Greek origin.
    "Olive Oil": {
        "require": ["extra virgin"],
        "prefer": ["extra virgin", "cold pressed", "italian", "spanish"],
        "avoid": ["pomace", "light", "blend", "vegetable", "canola", "refined"],
        "brands": {
            "preferred": ["Bertolli"],
            "acceptable": ["Filippo Berio", "Colavita"],
            "avoid": [],
        },
        "varieties": [],
        "size_target": "1L",
        "price_range": {"target": 62.99, "max": 85.00},
        "flexibility": "strict",
        "directive": "Bertolli EVOO only. Italian/Spanish origin",
        "fallback": "Any quality EVOO if Bertolli unavailable",
    },

    # ── BUTTER ──
    # Unsalted only. Lurpak brand. Max/Danish origin.
    "Butter": {
        "require": ["unsalted"],
        "prefer": ["unsalted", "danish", "irish"],
        "avoid": ["salted", "margarine", "spread light", "plant based"],
        "brands": {
            "preferred": ["Lurpak"],
            "acceptable": ["Kerrygold", "President"],
            "avoid": [],
        },
        "varieties": ["Unsalted", "Soft Butter"],
        "size_target": "200g",
        "price_range": {"target": 13.99, "max": 25.00},
        "flexibility": "strict",
        "directive": "Lurpak unsalted butter",
        "fallback": "Kerrygold or President unsalted if no Lurpak",
    },

    # ── SPINACH ──
    # Dirty Dozen: must be organic.
    # Baby spinach, small pack (spoils fast in Dubai).
    "Spinach": {
        "require": ["organic"],
        "prefer": ["baby", "organic", "fresh"],
        "avoid": ["frozen", "canned", "creamed", "chopped frozen"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": ["Baby Spinach", "Organic Baby Spinach"],
        "size_target": "150g",
        "price_range": {"target": 8.00, "max": 18.00},
        "flexibility": "moderate",
        "directive": "Organic baby spinach (Dirty Dozen). Small pack only",
        "fallback": "If no organic, pick freshest conventional and flag",
    },

    # ── ROCKET ──
    # Leafy green: must be organic.
    # Wild rocket preferred. Small pack (spoils fast).
    "Rocket": {
        "require": ["organic"],
        "prefer": ["wild", "organic", "fresh"],
        "avoid": ["frozen", "dried", "mixed salad"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": ["Wild Rocket", "Organic Rocket"],
        "size_target": "100g",
        "price_range": {"target": 5.00, "max": 15.00},
        "flexibility": "moderate",
        "directive": "Organic wild rocket. Small pack only",
        "fallback": "If no organic, pick fresh conventional and flag",
    },

    # ── CHICKEN BREAST ──
    # Organic/free-range preferred (always_organic tier).
    # Fresh, not frozen or processed.
    "Chicken Breast": {
        "require": [],
        "prefer": ["organic", "free range", "fresh", "skinless", "boneless"],
        "avoid": ["frozen", "marinated", "breaded", "nugget", "kiev",
                  "processed", "smoked"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": [],
        "size_target": "500g",
        "price_range": {"target": 72.25, "max": 95.00},
        "flexibility": "moderate",
        "directive": "Fresh organic/free-range chicken breast, 500g",
        "fallback": "Fresh conventional chicken breast if no organic",
    },

    # ── BEEF SAUSAGES ──
    # Quality matters. Check ingredients for fillers.
    "Beef Sausages": {
        "require": [],
        "prefer": ["beef", "breakfast", "quality", "premium", "grass fed"],
        "avoid": ["chicken", "turkey", "vegan", "plant", "frozen",
                  "cocktail", "mini"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": ["Beef Sausages", "Beef Breakfast Sausages"],
        "size_target": "500g",
        "price_range": {"target": 35.00, "max": 55.00},
        "flexibility": "moderate",
        "directive": "Quality beef sausages. Check ingredients for fillers",
        "fallback": "Best quality beef sausages available",
    },

    # ── SALMON ──
    # Fresh fillets only. Never processed fish products.
    # Norwegian/Scottish origin preferred (high sensitivity).
    "Salmon": {
        "require": ["fillet"],
        "prefer": ["organic", "atlantic", "norwegian", "scottish",
                   "fillet", "fresh", "wild"],
        "avoid": ["canned", "smoked", "frozen", "pink salmon", "fishcake",
                  "fish cake", "fish finger", "breaded", "battered"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": ["Organic Salmon Fillet", "Salmon Fillet", "Atlantic Salmon Fillet"],
        "size_target": "500g",
        "price_range": {"target": 55.00, "max": 85.00},
        "flexibility": "moderate",
        "directive": "Fresh salmon fillets, organic preferred. Norwegian/Scottish origin",
        "fallback": "Any fresh salmon fillet, premium origin",
    },

    # ── STRIPLOIN ──
    # ~500g per meal. Quality beef, grass-fed.
    # Australian/NZ/Max origin preferred (high sensitivity).
    "Striploin": {
        "require": [],
        "prefer": ["grass fed", "angus", "aged", "premium", "organic"],
        "avoid": ["frozen", "pre-seasoned", "marinated"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": ["Beef Striploin", "Striploin Steak"],
        "size_target": "500g",
        "price_range": {"target": 60.00, "max": 100.00},
        "flexibility": "moderate",
        "directive": "Grass-fed striploin ~500g. Premium origin. 2.2kg is way too much",
        "fallback": "Best quality fresh striploin, right size",
    },

    # ── BREAD ──
    # Brown/rye/flaxseed. Must review ingredients for additives.
    # Type gate in household_profile checks for preservatives/emulsifiers.
    "Bread": {
        "require": [],
        "prefer": ["brown", "whole", "rye", "flax", "seed", "sourdough",
                   "multigrain", "wholemeal"],
        "avoid": ["white", "brioche", "sweet", "cake", "pastry", "croissant"],
        "brands": {
            "preferred": [],
            "acceptable": [],
            "avoid": [],
        },
        "varieties": ["Brown Bread", "Rye Bread", "Flax Seed Bread",
                      "Wholemeal Bread", "Sourdough"],
        "size_target": "400g",
        "price_range": {"target": 9.20, "max": 18.00},
        "flexibility": "flexible",
        "directive": "Brown/rye/flaxseed bread. Clean ingredients only",
        "fallback": "Simplest ingredient bread available",
    },
}


# ────────────────────────────────────────────
# Order history analysis
# ────────────────────────────────────────────
# "They bought Jenan eggs 8 times" is a stronger signal than any preference rule.
# This queries actual order history to boost products from brands/products
# the household actually purchases repeatedly.

def get_purchase_history(item_name, conn=None):
    """
    Query enriched order history to find purchase patterns for an item.

    After enrichment (enrich_order_history.py), order_items has:
      - brand (extracted from raw_name)
      - item_id (linked to items table)
      - size (from scraping)
      - unit_price, quantity
      - order -> store_id, source, order_date

    Returns:
        {
            "brands": {"Jenan": 8, "Al Ain": 3},    # brand -> times ordered
            "products": {"Jenan Organic...": 5},      # raw_name -> times ordered
            "sizes": {"500g": 8, "1kg": 1},           # size -> times ordered
            "stores": {1: 8, 2: 1},                   # store_id -> times ordered
            "prices": [36.2, 36.2, 42.0, ...],        # all unit_prices paid
            "avg_price": 38.5,                         # average price paid
            "origins": {"grass fed": 7},               # origin/quality keywords from raw_name
            "source_weights": {"order_history_screenshot": 8, "browser_extension": 1},
            "total_orders": 9,
        }
    """
    close_conn = False
    if conn is None:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            close_conn = True
        except Exception:
            return _empty_history()

    try:
        cur = conn.cursor()

        # Get the item_id for this habit item
        cur.execute("SELECT id FROM items WHERE name = ?", (item_name,))
        item_row = cur.fetchone()
        if not item_row:
            return _empty_history()

        item_id = item_row["id"]

        # Pull ALL enriched order data for this item in one query
        cur.execute("""
            SELECT oi.raw_name, oi.brand, oi.size, oi.unit_price, oi.quantity,
                   o.store_id, o.source, o.order_date
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
            WHERE oi.item_id = ?
            ORDER BY o.order_date DESC
        """, (item_id,))
        rows = cur.fetchall()

        if not rows:
            return _empty_history()

        brands = {}
        products = {}
        sizes = {}
        stores = {}
        prices = []
        source_weights = {}

        for row in rows:
            raw_name = row["raw_name"]
            brand = row["brand"]
            size = row["size"]
            price = row["unit_price"]
            store_id = row["store_id"]
            source = row["source"]

            # Brand frequency
            if brand and brand.strip():
                brands[brand] = brands.get(brand, 0) + 1

            # Product name frequency
            products[raw_name] = products.get(raw_name, 0) + 1

            # Size frequency (normalize common variants)
            if size and size.strip():
                size_key = _normalize_size_key(size)
                sizes[size_key] = sizes.get(size_key, 0) + 1

            # Store frequency
            stores[store_id] = stores.get(store_id, 0) + 1

            # Prices
            if price and price > 0:
                prices.append(price)

            # Source weights (manual orders are stronger signals)
            source_weights[source] = source_weights.get(source, 0) + 1

        total = len(rows)
        avg_price = round(sum(prices) / len(prices), 2) if prices else 0

        # Extract quality keywords from raw_names (grass fed, organic, free range, etc.)
        origins = _extract_quality_keywords_from_history(products)

        return {
            "brands": brands,
            "products": products,
            "sizes": sizes,
            "stores": stores,
            "prices": prices,
            "avg_price": avg_price,
            "origins": origins,
            "source_weights": source_weights,
            "total_orders": total,
        }

    except Exception:
        return _empty_history()
    finally:
        if close_conn:
            conn.close()


def _empty_history():
    """Return an empty purchase history dict."""
    return {
        "brands": {}, "products": {}, "sizes": {}, "stores": {},
        "prices": [], "avg_price": 0, "origins": {},
        "source_weights": {}, "total_orders": 0,
    }


def _normalize_size_key(size_str):
    """
    Normalize size strings for comparison.
    '500g - Fresh' -> '500g', 'Medium Brown - 15 pcs' -> '15 pcs',
    '90g to 100g' -> '100g'
    """
    if not size_str:
        return size_str
    s = size_str.strip().lower()

    # Extract the core size pattern
    # "500g - Fresh" -> "500g"
    # "White - 200g" -> "200g"
    # "Natural - 350g" -> "350g"
    # "Medium Brown - 15 pcs" -> "15 pcs"
    import re

    # Try to find a standard size pattern anywhere in the string
    # kg first
    m = re.search(r'([\d.]+)\s*kg', s)
    if m:
        return f"{m.group(1)}kg"

    # grams
    m = re.search(r'([\d.]+)\s*g\b', s)
    if m:
        return f"{m.group(1)}g"

    # liters
    m = re.search(r'([\d.]+)\s*l\b', s)
    if m:
        return f"{m.group(1)}l"

    # milliliters
    m = re.search(r'([\d.]+)\s*ml', s)
    if m:
        return f"{m.group(1)}ml"

    # pieces / pcs
    m = re.search(r'(\d+)\s*(pcs|pieces?|eggs?|pack)', s)
    if m:
        return f"{m.group(1)} pcs"

    # Bare number with context
    m = re.search(r'(\d+)', s)
    if m:
        return s  # keep original if we can't normalize

    return s


def _extract_quality_keywords_from_history(products):
    """
    Scan raw product names from history for quality keywords.
    "Beef Mince (Grass Fed)" -> {"grass fed": 7}
    "Organic Free Range Eggs" -> {"organic": 5, "free range": 5}
    """
    QUALITY_KEYWORDS = [
        "grass fed", "grass-fed", "free range", "free-range",
        "organic", "wild caught", "wild-caught", "wagyu", "angus",
        "extra virgin", "cold pressed", "cold-pressed",
        "sourdough", "wholegrain", "whole grain", "rye", "flaxseed",
        "pasture raised", "pasture-raised",
    ]

    counts = {}
    for raw_name, qty in products.items():
        name_lower = raw_name.lower()
        for kw in QUALITY_KEYWORDS:
            if kw in name_lower:
                # Normalize: "grass-fed" -> "grass fed"
                kw_normalized = kw.replace("-", " ")
                counts[kw_normalized] = counts.get(kw_normalized, 0) + qty

    return counts


def _history_brand_boost(brand_text, purchase_history):
    """
    Calculate bonus points based on how often we've bought from this brand.

    Order history is the STRONGEST signal — higher weights than any preference rule.

    Scoring (boosted from Phase 6 plan):
        5+ times ordered  → +30 ("This is THEIR brand — they keep choosing it")
        3-4 times ordered → +20 ("Regular purchase — strong preference")
        2 times ordered   → +12 ("Repeat purchase — they tried it and came back")
        1 time ordered    → +5  ("Tried once — mild positive signal")
        Never ordered     → 0   (neutral, NOT a penalty)

    Manual/off-app orders (order_history_screenshot) get extra weight because
    those are conscious decisions — the person specifically chose this brand.
    """
    if not purchase_history or not purchase_history.get("brands"):
        return 0, None

    brand_lower = brand_text.lower()
    for brand_name, count in purchase_history["brands"].items():
        if brand_name.lower() in brand_lower or brand_lower in brand_name.lower():
            # Check if purchases were mostly from manual orders (stronger signal)
            manual_sources = purchase_history.get("source_weights", {})
            manual_count = sum(v for k, v in manual_sources.items()
                             if k in ("order_history_screenshot", "manual"))
            has_manual_signal = manual_count > purchase_history["total_orders"] * 0.5

            if count >= 5:
                pts = 30
                label = f"Household brand '{brand_name}' ({count}x)"
            elif count >= 3:
                pts = 20
                label = f"Regular brand '{brand_name}' ({count}x)"
            elif count >= 2:
                pts = 12
                label = f"Repeat brand '{brand_name}' ({count}x)"
            else:
                pts = 5
                label = f"Tried brand '{brand_name}' before"

            # Manual order bonus: conscious choice signal
            if has_manual_signal and count >= 2:
                pts += 5
                label += " [manual orders]"

            return pts, f"{label} (+{pts})"

    return 0, None


def _history_product_boost(product_name, purchase_history):
    """
    Bonus if this exact product (or very similar name) appears in order history.
    A stronger signal than brand alone — means they picked THIS specific item.

    5+ times → +15 ("They keep buying this exact product — this IS their product")
    3-4 times → +10 ("Regular choice — strong signal")
    2 times  → +6  ("Repeat purchase")
    """
    if not purchase_history or not purchase_history.get("products"):
        return 0, None

    pname_lower = product_name.lower()
    for raw_name, count in purchase_history["products"].items():
        # Check if substantial overlap (not just single word like "Eggs")
        raw_lower = raw_name.lower()
        # Both names must share a significant chunk (at least 2 meaningful words)
        pname_words = set(w for w in pname_lower.split() if len(w) > 2)
        raw_words = set(w for w in raw_lower.split() if len(w) > 2)
        overlap = pname_words & raw_words
        if len(overlap) >= 2:
            if count >= 5:
                return 15, f"Household product '{raw_name[:40]}' ({count}x) (+15)"
            elif count >= 3:
                return 10, f"Regular product '{raw_name[:40]}' ({count}x) (+10)"
            elif count >= 2:
                return 6, f"Repeat product '{raw_name[:40]}' ({count}x) (+6)"

    return 0, None


def _history_size_boost(product_size, purchase_history):
    """
    Bonus if the product size matches what the household typically buys.

    e.g., They always buy 500g beef mince → 500g gets +10, 1kg gets 0.
    e.g., They always buy 15-pack eggs → 15 pcs gets +10, 6 pcs gets 0.
    """
    if not purchase_history or not purchase_history.get("sizes"):
        return 0, None

    if not product_size:
        return 0, None

    product_size_key = _normalize_size_key(product_size)
    if not product_size_key:
        return 0, None

    # Find the most-purchased size
    top_size = max(purchase_history["sizes"].items(), key=lambda x: x[1])
    top_size_key, top_count = top_size

    # Does this product match the preferred size?
    if _sizes_match(product_size_key, top_size_key):
        if top_count >= 3:
            return 10, f"Usual size '{top_size_key}' ({top_count}x) (+10)"
        elif top_count >= 2:
            return 6, f"Previously bought size '{top_size_key}' ({top_count}x) (+6)"

    return 0, None


def _sizes_match(size_a, size_b):
    """
    Check if two normalized size strings represent the same size.
    '500g' matches '0.5kg', '15 pcs' matches '15 pcs', etc.
    """
    if not size_a or not size_b:
        return False

    a, b = size_a.lower().strip(), size_b.lower().strip()

    # Exact match
    if a == b:
        return True

    # Parse both to (quantity, unit) and compare
    qty_a, unit_a = _parse_size(a)
    qty_b, unit_b = _parse_size(b)
    if qty_a is None or qty_b is None:
        return False

    # Normalize to base units
    norm_a, base_a = _normalize_to_base_unit(qty_a, unit_a)
    norm_b, base_b = _normalize_to_base_unit(qty_b, unit_b)

    if base_a != base_b:
        return False

    # Within 10% tolerance (e.g., 350g vs 350g, or 0.5kg vs 500g)
    if norm_a > 0 and norm_b > 0:
        ratio = max(norm_a, norm_b) / min(norm_a, norm_b)
        return ratio <= 1.1

    return False


def _history_store_boost(store_id, purchase_history):
    """
    Soft preference for the store the household usually buys this item from.

    This is a SOFT signal — doesn't override quality, but if two products
    are close in score, the one from the preferred store wins.

    e.g., Beef Mince → 8/9 from Kibsons → Kibsons product gets +8.
    """
    if not purchase_history or not purchase_history.get("stores"):
        return 0, None

    if store_id not in purchase_history["stores"]:
        return 0, None

    store_count = purchase_history["stores"][store_id]
    total = purchase_history["total_orders"]

    if total == 0:
        return 0, None

    ratio = store_count / total

    if ratio >= 0.7 and store_count >= 3:
        return 8, f"Preferred store for this item ({store_count}/{total} orders) (+8)"
    elif ratio >= 0.5 and store_count >= 2:
        return 4, f"Frequently bought here ({store_count}/{total} orders) (+4)"

    return 0, None


def _history_quality_boost(product_name, purchase_history):
    """
    Bonus if the product matches quality keywords from purchase history.

    e.g., History shows "grass fed" 7 times for Beef Mince →
          a grass-fed product gets +12 (they consistently choose grass-fed).
    """
    if not purchase_history or not purchase_history.get("origins"):
        return 0, None

    pname_lower = product_name.lower()
    total_pts = 0
    matched = []

    for keyword, count in purchase_history["origins"].items():
        if keyword.replace("-", " ") in pname_lower or keyword in pname_lower:
            if count >= 3:
                pts = 12
            elif count >= 2:
                pts = 8
            else:
                pts = 3
            total_pts += pts
            matched.append(f"'{keyword}' ({count}x)")

    if total_pts > 0:
        # Cap at 15 to avoid runaway stacking
        total_pts = min(total_pts, 15)
        reason = f"History quality match: {', '.join(matched)} (+{total_pts})"
        return total_pts, reason

    return 0, None


# ────────────────────────────────────────────
# Scoring engine
# ────────────────────────────────────────────

def _text_contains(text, keyword):
    """Case-insensitive keyword check in product text."""
    return keyword.lower() in text.lower()



# ────────────────────────────────────────────
# Size parsing & unit price normalization
# ────────────────────────────────────────────
# Products come in different sizes. To compare fairly:
#   "500g at 10 AED" vs "1kg at 18 AED" → 20 AED/kg vs 18 AED/kg
# The 1kg pack is better value even though the raw price is higher.

def _parse_size(size_str):
    """
    Extract (quantity, unit) from a size string.

    Examples:
        "15 PCS"                → (15.0, "pcs")
        "500g"                  → (500.0, "g")
        "1kg"                   → (1.0, "kg")
        "2L"                    → (2.0, "l")
        "500ml"                 → (500.0, "ml")
        "Medium Brown - 30 pcs" → (30.0, "pcs")
        "200g"                  → (200.0, "g")
        "950g"                  → (950.0, "g")
        "6 Eggs"                → (6.0, "pcs")
        "2x15 pcs"              → (30.0, "pcs")
        "2x6 pcs"               → (12.0, "pcs")

    Returns (quantity, unit) or (None, None) if unparseable.
    Unit is normalized: g, kg, ml, l, pcs
    """
    if not size_str:
        return None, None

    text = size_str.strip().lower()

    # ── Multiplier pattern: "2x15 pcs", "2x6 pcs", "2x10 pcs" ──
    m = re.search(r'(\d+)\s*x\s*(\d+)\s*(pcs|pieces?|eggs?|pack|tray)', text)
    if m:
        return float(int(m.group(1)) * int(m.group(2))), "pcs"

    # ── Weight: kg first (before g, since "kg" contains "g") ──
    m = re.search(r'([\d.]+)\s*kg', text)
    if m:
        return float(m.group(1)), "kg"

    m = re.search(r'([\d.]+)\s*g\b', text)
    if m:
        return float(m.group(1)), "g"

    # ── Volume ──
    m = re.search(r'([\d.]+)\s*(?:ltr|litre|liter)\b', text)
    if m:
        return float(m.group(1)), "l"

    m = re.search(r'([\d.]+)\s*l\b', text)
    if m:
        return float(m.group(1)), "l"

    m = re.search(r'([\d.]+)\s*ml\b', text)
    if m:
        return float(m.group(1)), "ml"

    # ── Pieces/count (eggs, pcs, pack, pieces) ──
    m = re.search(r'(\d+)\s*(?:pcs|pieces?|eggs?|pack|count)\b', text)
    if m:
        return float(m.group(1)), "pcs"

    # ── Bare number + context (e.g., "15" in size_target "15 PCS") ──
    m = re.search(r'(\d+)', text)
    if m:
        return float(m.group(1)), "pcs"  # assume pieces for bare numbers

    return None, None


def _normalize_to_base_unit(quantity, unit):
    """
    Convert to base unit for comparison.
    g → kg, ml → l, pcs stays as pcs.

    Returns (normalized_quantity, base_unit).
    """
    if unit == "g":
        return quantity / 1000.0, "kg"
    elif unit == "kg":
        return quantity, "kg"
    elif unit == "ml":
        return quantity / 1000.0, "l"
    elif unit == "l":
        return quantity, "l"
    elif unit == "pcs":
        return quantity, "pcs"
    else:
        return quantity, unit


def _calculate_unit_price(price, size_str):
    """
    Calculate price per standard unit (per kg, per liter, or per piece).

    Returns (unit_price, base_unit) or (None, None) if can't calculate.

    Examples:
        (10.0, "500g")   → (20.0, "kg")    — 20 AED per kg
        (18.0, "1kg")    → (18.0, "kg")    — 18 AED per kg
        (34.49, "15 PCS") → (2.30, "pcs")  — 2.30 AED per egg
        (13.25, "500ml")  → (26.50, "l")   — 26.50 AED per liter
    """
    if not price or price <= 0:
        return None, None

    qty, unit = _parse_size(size_str)
    if qty is None or qty <= 0:
        return None, None

    norm_qty, base_unit = _normalize_to_base_unit(qty, unit)
    if norm_qty <= 0:
        return None, None

    return round(price / norm_qty, 2), base_unit


def score_single_product(item_name, store_name, product, profile=None, purchase_history=None, item_id=None, rules_view=None):
    """
    Score a single scraped product against the preference profile, purchase history,
    AND household intelligence (organic tiers, type gates, size fitness, origin quality).

    Args:
        item_name: the habit item name (e.g., "Eggs")
        store_name: store being searched (e.g., "Carrefour UAE")
        product: dict with at least {"name": str}, optionally {"price", "size", "brand",
                 "description", "ingredients", "origin_country", "is_organic"}
        profile: optional override for preference profile
        purchase_history: optional dict from get_purchase_history() -- if provided,
            products from brands/items the household actually buys get bonus points.
        item_id: optional item ID -- if provided, loads learned preferences from
            preference_state instead of static PREFERENCE_PROFILES.

    Returns dict:
        {
            "product": the original product dict,
            "score": 0-100 (higher = better match),
            "tier": "preferred" | "acceptable" | "fallback" | "avoid",
            "reasons": ["Preferred brand: Jenan (+20)", "Organic match (+10)", ...],
            "disqualified": bool (True if a "require" rule failed),
            "directive_met": bool (True if primary directive was satisfied),
            "compromises": list of strings describing directive compromises,
        }
    """
    if profile is None:
        if item_id is not None:
            try:
                from preference_engine import get_scoring_profile
                profile = get_scoring_profile(item_id)
            except Exception:
                profile = PREFERENCE_PROFILES.get(item_name, {})
        else:
            # Try static per-item profile first, then fall back to global household preferences.
            # This ensures catalog-only items (not in items table) still get
            # household-level preferences like "prefer organic" applied.
            profile = PREFERENCE_PROFILES.get(item_name, None)
            if profile is None:
                profile = get_household_profile()

    if not profile:
        # No profile defined -- score purely on name similarity.
        # Still apply the sibling-noun penalty (Phase 4) here so that
        # "Sweet Potatoes" doesn't tie with "Potatoes" for an item
        # named "Potatoes" -- the penalty is a correctness signal, not
        # a preference one, and should apply regardless of profile.
        pname_no_profile = product.get("name", "")
        sib_penalty_np, sib_reasons_np = _sibling_noun_penalty(
            item_name, pname_no_profile,
        )
        base_score = 50 + sib_penalty_np
        base_reasons = ["No preference profile defined"]
        if sib_reasons_np:
            base_reasons.extend(sib_reasons_np)
        return {
            "product": product,
            "score": base_score,
            "tier": "fallback",
            "reasons": base_reasons,
            "disqualified": False,
            "directive_met": True,
            "compromises": [],
        }

    # ── Pinned / Blocked product shortcuts ──
    # These are set by user via "Always this" / "Wrong item" buttons in the UI.
    # Pinned = auto-winner (score 100), Blocked = disqualified immediately.
    product_catalog_id = product.get("id")

    pinned = profile.get("_pinned")
    if pinned and product_catalog_id:
        pinned_id = pinned.get("catalog_id")
        if pinned_id and product_catalog_id == pinned_id:
            return {
                "product": product,
                "score": 100,
                "tier": "preferred",
                "reasons": ["Pinned product — always this"],
                "disqualified": False,
                "directive_met": True,
                "compromises": [],
            }

    blocked = profile.get("_blocked")
    if blocked and product_catalog_id:
        blocked_ids = blocked.get("catalog_ids", []) if isinstance(blocked, dict) else []
        if product_catalog_id in blocked_ids:
            return {
                "product": product,
                "score": -50,
                "tier": "avoid",
                "reasons": ["Blocked product — wrong item"],
                "disqualified": True,
                "directive_met": False,
                "compromises": [],
            }

    score = 50  # Start at neutral
    reasons = []
    compromises = []
    disqualified = False
    directive_met = True

    pname = product.get("name", "")
    pdesc = product.get("description", "")
    pingredients = product.get("ingredients", "")
    full_text = f"{pname} {pdesc}"
    pprice = product.get("price")
    psize = product.get("size", "")
    pbrand = product.get("brand", "")
    porigin = product.get("origin_country", "")

    # ══════════════════════════════════════════════
    # HOUSEHOLD INTELLIGENCE (from household_profile.py)
    # These checks run BEFORE traditional scoring.
    # ══════════════════════════════════════════════
    try:
        from household_profile import (
            check_product_type_gate, get_organic_tier,
            check_size_fit, calculate_origin_score,
        )
        has_household = True
    except ImportError:
        has_household = False

    if has_household:
        # ── PRODUCT TYPE GATE (instant reject) ──
        # e.g., wheat pasta -> rejected, Greek yogurt -> rejected
        gate_passes, gate_reason = check_product_type_gate(
            item_name, pname, pdesc, pingredients
        )
        if not gate_passes:
            score -= 50
            disqualified = True
            directive_met = False
            reasons.append(f"TYPE GATE FAIL: {gate_reason} (-50, disqualified)")
            compromises.append(gate_reason)

        # ── ORGANIC TIER ──
        organic_tier = get_organic_tier(item_name)
        is_organic = (
            product.get("is_organic")
            or _text_contains(full_text, "organic")
            or _text_contains(pbrand, "organic")
        )

        if organic_tier == "always_organic":
            if is_organic:
                score += 15
                reasons.append(f"+organic_match (always_organic tier) (+15)")
            else:
                score -= 40
                directive_met = False
                reasons.append(f"-organic_required (always_organic tier, not organic) (-40)")
                compromises.append(f"Not organic -- {item_name} should be organic (Dirty Dozen/household rule)")

        elif organic_tier == "prefer_organic":
            if is_organic:
                score += 10
                reasons.append(f"+organic_preferred (prefer_organic tier) (+10)")
            # No penalty for non-organic in this tier

        elif organic_tier == "dont_bother":
            # Don't score organic at all -- don't waste money
            # But also don't penalize if it happens to be organic
            pass

        # ── SIZE FITNESS ──
        size_verdict, size_reason, suggested_qty = check_size_fit(item_name, psize)
        if size_verdict == "too_large":
            score -= 25
            reasons.append(f"-oversized: {size_reason} (-25)")
            compromises.append(f"Oversized: {size_reason}")
        elif size_verdict == "too_small":
            score -= 10
            reasons.append(f"-undersized: {size_reason} (-10)")
        elif size_verdict == "good_multi":
            # Product is individually small but ordering multiples hits the target.
            # No penalty — this is how steaks, fillets etc. work.
            # Small bonus for being a clean multi-qty fit.
            score += 3
            reasons.append(f"+multi_qty: {size_reason} (+3)")
        # Store suggested_qty for the curator to use
        product["_suggested_qty"] = suggested_qty

        # ── ORIGIN COUNTRY ──
        origin_points, origin_reason = calculate_origin_score(
            item_name, porigin, product.get("category", "")
        )
        if origin_points != 0:
            score += origin_points
            reasons.append(f"{origin_reason} ({origin_points:+d})")

    # ══════════════════════════════════════════════
    # CATEGORY QUALITY SCORING (from product_grading.py)
    # Uses extract_attributes() + CATEGORY_QUALITY_DIMENSIONS
    # to score quality signals that are specific to each category.
    # e.g., breed for beef, wild-caught for seafood, fat_content for dairy
    # ══════════════════════════════════════════════
    try:
        from product_grading import (
            extract_attributes, detect_category, grade_product as _grade_func,
            get_dimension_weight, CATEGORY_QUALITY_DIMENSIONS,
        )
        has_grading = True
    except ImportError:
        has_grading = False

    if has_grading:
        # Build a product dict for extract_attributes()
        _grade_product = {
            "name": pname,
            "description": pdesc,
            "ingredients": pingredients,
            "origin_country": porigin,
            "is_organic": product.get("is_organic"),
            "size": psize,
            "storage_info": product.get("storage_info", ""),
        }
        category_hint = product.get("category", "")
        attrs = extract_attributes(_grade_product, category_hint)
        category = attrs.get("category", "Unknown")

        # Store attrs on product for downstream use (tags, frontend)
        product["_quality_attrs"] = attrs

        # -- Breed scoring (Meat) --
        breed = attrs.get("breed", "")
        if breed and breed != "standard" and category == "Meat":
            breed_weight = get_dimension_weight("Meat", "breed")
            breed_pts = {"wagyu": 12, "angus": 8, "premium": 5}.get(breed, 0)
            if breed_pts:
                score += breed_pts
                reasons.append(f"+breed_{breed} (+{breed_pts})")

        # -- Wild caught scoring (Seafood) --
        if attrs.get("is_wild_caught") and category == "Seafood":
            score += 10
            reasons.append("+wild_caught (+10)")

        # -- Fat content scoring (Dairy) --
        # Only apply if not already covered by prefer/avoid keywords in profile
        fat = attrs.get("fat_content", "")
        profile_prefs = [p.lower() for p in profile.get("prefer", [])]
        profile_avoids = [a.lower() for a in profile.get("avoid", [])]
        if fat and category == "Dairy":
            if fat == "full" and "full fat" not in profile_prefs:
                score += 5
                reasons.append("+full_fat (+5)")
            elif fat == "low" and "low fat" not in profile_avoids:
                score -= 8
                reasons.append("-low_fat (-8)")
            elif fat == "zero":
                score -= 10
                reasons.append("-zero_fat (-10)")

        # -- Freshness scoring --
        freshness = attrs.get("freshness", "")
        if freshness == "frozen" and category in ("Meat", "Poultry", "Seafood", "Produce"):
            score -= 8
            reasons.append("-frozen (-8)")
        elif freshness == "fresh" and category in ("Dairy", "Meat"):
            score += 3
            reasons.append("+fresh (+3)")

        # -- UHT penalty for milk --
        if attrs.get("is_uht") and attrs.get("dairy_type") == "milk":
            score -= 8
            reasons.append("-uht_milk (-8)")

        # -- Processing level scoring --
        proc = attrs.get("processing_level", "raw")
        if proc in ("breaded", "ready_meal") and category in ("Meat", "Poultry", "Seafood"):
            score -= 12
            reasons.append(f"-heavily_processed ({proc}) (-12)")
        elif proc == "processed" and category in ("Meat", "Poultry"):
            score -= 10
            reasons.append("-processed_meat (-10)")

        # -- Ingredient quality scoring --
        ing = attrs.get("ingredients", {})
        if ing.get("has_clean_label"):
            score += 5
            reasons.append("+clean_label (+5)")
        elif ing.get("additive_count", 0) >= 4:
            score -= 8
            reasons.append(f"-additives ({ing['additive_count']} E-numbers) (-8)")
        elif ing.get("filler_count", 0) > 0:
            score -= 6
            reasons.append(f"-fillers ({', '.join(ing.get('fillers', [])[:2])}) (-6)")

        # -- Margarine penalty (butter) --
        if attrs.get("is_margarine"):
            score -= 15
            reasons.append("-margarine (-15)")

        # -- Processed cheese penalty --
        if attrs.get("cheese_processing") == "processed":
            score -= 12
            reasons.append("-processed_cheese (-12)")

        # -- Overall grade as quality floor --
        # Grade F products are rejected outright (too many quality issues).
        # Grade D gets a penalty. Grade A/B get a small bonus.
        try:
            grade_result = _grade_func(_grade_product, category_hint)
            product_grade = grade_result.get("grade", "C")
            product["_grade"] = product_grade
            product["_grade_points"] = grade_result.get("points", 50)

            if product_grade == "F":
                score -= 20
                disqualified = True
                reasons.append(f"Grade F product (-20, disqualified)")
            elif product_grade == "D":
                score -= 8
                reasons.append(f"Grade D quality (-8)")
            elif product_grade == "A":
                score += 8
                reasons.append(f"Grade A quality (+8)")
            elif product_grade == "B":
                score += 3
                reasons.append(f"Grade B quality (+3)")
        except Exception:
            pass  # Grading is a bonus, not critical

    # ══════════════════════════════════════════════
    # TRADITIONAL SCORING (profile-based)
    # ══════════════════════════════════════════════

    # ── NAME RELEVANCE CHECK ──
    # The product should actually be the right type of food.
    # "Apple" should match apple products, not dates or lemons.
    # "Beef Mince" should match mince, not just any beef product.
    item_words = [w.lower() for w in item_name.split() if len(w) > 2]
    pname_lower = pname.lower()

    # Count how many item words appear in the product name
    words_matched = sum(1 for w in item_words if w in pname_lower)

    # Also check search_terms/varieties from the profile
    variety_matched = False
    for term in profile.get("varieties", []):
        term_words = [w.lower() for w in term.split() if len(w) > 2]
        if any(w in pname_lower for w in term_words):
            variety_matched = True
            break

    # Identify core product type — the last word is almost always the
    # food category: "Beef Sausages" → sausages, "Fresh Milk" → milk
    core_noun = item_words[-1] if item_words else ""
    # Basic singularize for matching (sausages→sausage, berries→berry)
    core_singular = core_noun
    if core_noun.endswith('ies'):
        core_singular = core_noun[:-3] + 'y'
    elif core_noun.endswith('es') and len(core_noun) > 3:
        core_singular = core_noun[:-2]
    elif core_noun.endswith('s') and not core_noun.endswith('ss') and len(core_noun) > 3:
        core_singular = core_noun[:-1]
    core_in_product = (core_noun in pname_lower or core_singular in pname_lower)

    if len(item_words) > 0 and words_matched == len(item_words):
        # All words match (e.g., "Beef" AND "Mince" both in product name)
        score += 15
        reasons.append(f"Product name matches '{item_name}' (+15)")
    elif words_matched > 0 and core_in_product:
        # Right product type, partial modifier match
        # e.g., "Chicken Sausages" for "Beef Sausages" — correct category
        score += 5
        reasons.append(f"Partial name match '{item_name}' (+5)")
    elif words_matched > 0 and not core_in_product:
        # CATEGORY MISMATCH: modifier matches but wrong product type
        # e.g., "Beef Tomatoes" for "Beef Sausages" — tomatoes ≠ sausages
        score -= 35
        disqualified = True
        reasons.append(f"Category mismatch: '{core_noun}' not in product (-35, disqualified)")
    elif variety_matched:
        # No direct name words matched but a known variety/search term did
        score += 5
        reasons.append(f"Variety match for '{item_name}' (+5)")
    else:
        # No match at all (e.g., "Dates" for "Apple")
        score -= 20
        reasons.append(f"Product name doesn't match '{item_name}' (-20)")

    # ── SIBLING-NOUN PENALTY (Phase 4) ──
    # Penalize products whose name prepends a sibling-noun qualifier to
    # an item noun, turning it into a different food. e.g. item
    # "Potatoes" should outrank "Sweet Potatoes" because "sweet
    # potatoes" is a sibling vegetable, not a variant.
    sib_penalty, sib_reasons = _sibling_noun_penalty(item_name, pname)
    if sib_penalty:
        score += sib_penalty
        reasons.extend(sib_reasons)

    # ── REQUIRED attributes (deal-breakers) ──
    for req in profile.get("require", []):
        req_met = _text_contains(full_text, req)
        # Honor the catalog's structured is_organic FLAG for the organic
        # requirement, not just the literal word in the product name. Stores
        # like Kibsons sell produce as organic by default and DON'T put
        # "organic" in the title ("Kibsons Cucumbers"), so a pure text search
        # wrongly disqualified genuinely-organic products — e.g. it dropped
        # every real cucumber to 'avoid' and the curator returned eggplant /
        # baby food instead. (The 'prefer' path above already uses the flag.)
        if not req_met and req == "organic" and product.get("is_organic"):
            req_met = True
        if req_met:
            score += 10
            reasons.append(f"Required '{req}' found (+10)")
        else:
            # Don't double-penalize organic if household_profile already handled it
            if has_household and req == "organic" and get_organic_tier(item_name) == "always_organic":
                continue  # already penalized above with -40
            score -= 40
            disqualified = True
            reasons.append(f"MISSING required '{req}' (-40, disqualified)")

    # ── PREFERRED keywords ──
    for pref in profile.get("prefer", []):
        # Skip organic prefer if household_profile already scored it
        if has_household and pref == "organic":
            continue
        if _text_contains(full_text, pref):
            score += 8
            reasons.append(f"Preferred '{pref}' (+8)")

    # ── AVOID keywords ──
    for avoid in profile.get("avoid", []):
        if _text_contains(full_text, avoid):
            score -= 15
            reasons.append(f"Avoid '{avoid}' (-15)")

    # ── BRAND matching ──
    brands = profile.get("brands", {})
    brand_text = f"{pname} {pbrand}"

    brand_matched = False
    for b in brands.get("preferred", []):
        if _text_contains(brand_text, b):
            score += 20
            reasons.append(f"Preferred brand '{b}' (+20)")
            brand_matched = True
            break

    if not brand_matched:
        for b in brands.get("acceptable", []):
            if _text_contains(brand_text, b):
                score += 5
                reasons.append(f"Acceptable brand '{b}' (+5)")
                brand_matched = True
                break

    if not brand_matched:
        for b in brands.get("avoid", []):
            if _text_contains(brand_text, b):
                score -= 20
                reasons.append(f"Avoid brand '{b}' (-20)")
                break

    # ── VARIETY matching (for produce) ──
    varieties = profile.get("varieties", [])
    if varieties:
        for v in varieties:
            if _text_contains(pname, v):
                score += 12
                reasons.append(f"Variety match '{v}' (+12)")
                break

    # ── PRICE & SIZE -- unit price comparison ──
    # Instead of comparing raw prices (unfair across sizes), we normalize
    # to price-per-unit (per kg, per liter, or per piece) so that:
    #   500g at 10 AED (20/kg) vs 1kg at 18 AED (18/kg) -> 1kg wins on value
    #   15 eggs at 34 AED (2.27/egg) vs 30 eggs at 50 AED (1.67/egg) -> 30-pack is better value
    price_range = profile.get("price_range", {})
    size_target = profile.get("size_target", "")

    target_price = price_range.get("target", 0)
    max_price = price_range.get("max", float("inf"))

    # Calculate unit prices for both target and product
    target_unit_price, target_base_unit = _calculate_unit_price(target_price, size_target)
    product_unit_price, product_base_unit = _calculate_unit_price(pprice, psize)

    # Store unit price on the product dict for later use (knowledge base, display)
    if product_unit_price is not None:
        product["unit_price"] = product_unit_price
        product["unit_price_unit"] = product_base_unit

    if (product_unit_price is not None and target_unit_price is not None
            and product_base_unit == target_base_unit):
        # ── Compare by unit price (fair comparison across sizes) ──
        deviation = abs(product_unit_price - target_unit_price) / target_unit_price

        if deviation < 0.15:
            score += 5
            reasons.append(f"Unit price on target ~{product_unit_price:.2f} AED/{product_base_unit} (+5)")
        elif product_unit_price < target_unit_price * 0.85:
            # Better value than target
            saving_pct = int((1 - product_unit_price / target_unit_price) * 100)
            score += 8
            reasons.append(f"Better value: {product_unit_price:.2f} vs {target_unit_price:.2f} AED/{product_base_unit} ({saving_pct}% cheaper) (+8)")
        elif product_unit_price > target_unit_price * 1.5:
            # Way more expensive per unit
            score -= 10
            reasons.append(f"Expensive: {product_unit_price:.2f} vs {target_unit_price:.2f} AED/{product_base_unit} (-10)")
        elif product_unit_price > target_unit_price * 1.2:
            score -= 5
            reasons.append(f"Pricier: {product_unit_price:.2f} vs {target_unit_price:.2f} AED/{product_base_unit} (-5)")

        # Size preference: bonus for matching the target size, mild penalty for very small
        prod_qty, prod_unit = _parse_size(psize)
        target_qty, target_unit = _parse_size(size_target)
        if prod_qty and target_qty and prod_unit == target_unit:
            if prod_qty == target_qty:
                score += 5
                reasons.append(f"Exact size match '{psize}' (+5)")
            elif prod_qty < target_qty * 0.4:
                # Much smaller than what we usually buy (e.g., 6-pack vs 15-pack target)
                score -= 5
                reasons.append(f"Small size '{psize}' vs target '{size_target}' (-5)")

    elif pprice is not None and price_range:
        # ── Fallback: raw price comparison (when sizes can't be parsed) ──
        if pprice > max_price:
            score -= 10
            reasons.append(f"Over max price {pprice} > {max_price} (-10)")
        elif target_price > 0:
            deviation = abs(pprice - target_price) / target_price
            if deviation < 0.15:
                score += 5
                reasons.append(f"Price on target ~{pprice} AED (+5)")
            elif pprice < target_price * 0.5:
                score -= 8
                reasons.append(f"Suspiciously cheap {pprice} AED vs target {target_price} (-8)")

        # Simple size string match as fallback
        if size_target and psize:
            if _text_contains(psize, size_target) or _text_contains(size_target, psize):
                score += 5
                reasons.append(f"Size match '{psize}' (+5)")

    # ══════════════════════════════════════════════
    # ORDER HISTORY (strongest signal)
    # "They actually buy this brand/size/from this store" is the most
    # reliable signal of all — it reflects conscious purchasing decisions.
    # ══════════════════════════════════════════════
    if purchase_history and purchase_history.get("total_orders", 0) > 0:
        brand_text = f"{pname} {pbrand}"
        brand_bonus, brand_reason = _history_brand_boost(brand_text, purchase_history)
        if brand_bonus:
            score += brand_bonus
            reasons.append(brand_reason)

        product_bonus, product_reason = _history_product_boost(pname, purchase_history)
        if product_bonus:
            score += product_bonus
            reasons.append(product_reason)

        size_bonus, size_reason = _history_size_boost(psize, purchase_history)
        if size_bonus:
            score += size_bonus
            reasons.append(size_reason)

        store_id = product.get("store_id")
        if store_id:
            store_bonus, store_reason = _history_store_boost(store_id, purchase_history)
            if store_bonus:
                score += store_bonus
                reasons.append(store_reason)

        quality_bonus, quality_reason = _history_quality_boost(pname, purchase_history)
        if quality_bonus:
            score += quality_bonus
            reasons.append(quality_reason)

    # ══════════════════════════════════════════════
    # HOUSEHOLD RULES (durable, category-level prefer/avoid-brand learned from
    # delivered-order feedback + accepted swaps). The scheduled-list validator
    # already honors these; reading them HERE unifies the chat curator with the
    # validator, so a brand learned in one place moves suggestions everywhere
    # the same way. rules_view is resolved once per item by score_products (no
    # per-product DB hits); same category-match logic as the validator
    # (rule.category appears in the item's category path OR name).
    # ══════════════════════════════════════════════
    if rules_view:
        brand_blob = f"{pbrand} {pname}".lower()
        avoided = False
        for ab in rules_view.get("avoid_brands", []):
            if ab and ab.lower() in brand_blob:
                score -= 40
                reasons.append(f"Avoided brand '{ab}' (household rule, -40)")
                avoided = True
                break
        if not avoided:
            for pb in rules_view.get("prefer_brands", []):
                if pb and pb.lower() in brand_blob:
                    score += 25
                    reasons.append(f"Preferred brand '{pb}' (household rule, +25)")
                    break

    # ── Determine tier ──
    score = max(0, min(100, score))
    if disqualified:
        tier = "avoid"
    elif score >= 75:
        tier = "preferred"
    elif score >= 55:
        tier = "acceptable"
    elif score >= 35:
        tier = "fallback"
    else:
        tier = "avoid"

    return {
        "product": product,
        "score": score,
        "tier": tier,
        "reasons": reasons,
        "disqualified": disqualified,
        "directive_met": directive_met,
        "compromises": compromises,
    }


def _resolve_rules_view(item_name, item_id, conn):
    """Resolve the durable household_rules (category-level prefer/avoid brand)
    that apply to this item, ONCE per item. Mirrors the scheduled-list
    validator's category matching — a rule applies when its category word
    appears in the item's category path OR name — so the chat curator and the
    validator honor the SAME learned brand preferences. Returns
    {"avoid_brands": [...], "prefer_brands": [...]}. Best-effort: any failure
    (or no DB) yields an empty view and scoring proceeds without it."""
    view = {"avoid_brands": [], "prefer_brands": []}
    try:
        from household_rules import get_avoid_brand_rules, get_prefer_brand_rules
        category = ""
        if conn is not None:
            row = None
            try:
                if item_id is not None:
                    row = conn.execute(
                        "SELECT category FROM items WHERE id = ?", (item_id,)
                    ).fetchone()
                if row is None and item_name:
                    row = conn.execute(
                        "SELECT category FROM items WHERE LOWER(name) = LOWER(?) LIMIT 1",
                        (item_name,),
                    ).fetchone()
            except Exception:
                row = None
            if row:
                category = (row[0] or "")
        hay = f"{category} {item_name or ''}".lower()
        view["avoid_brands"] = [
            r["brand"] for r in (get_avoid_brand_rules(conn) or [])
            if r.get("brand") and r.get("category") and r["category"] in hay
        ]
        view["prefer_brands"] = [
            r["brand"] for r in (get_prefer_brand_rules(conn) or [])
            if r.get("brand") and r.get("category") and r["category"] in hay
        ]
    except Exception:
        pass
    return view


def score_products(item_name, store_name, products, conn=None, item_id=None):
    """
    Score and rank a list of scraped products against a preference profile.
    Automatically enriches scoring with purchase history from order data.

    Args:
        item_name: habit item name (e.g., "Eggs")
        store_name: store being searched
        products: list of product dicts from scraper
        conn: optional DB connection (to query purchase history)
        item_id: optional item ID — if provided, loads learned preferences
            from preference_state instead of static PREFERENCE_PROFILES.

    Returns:
        List of scored results, sorted best-first.
        Each entry: {"product": {...}, "score": N, "tier": str, "reasons": [...]}
    """
    if item_id is not None:
        try:
            from preference_engine import get_scoring_profile
            profile = get_scoring_profile(item_id)
        except Exception:
            profile = PREFERENCE_PROFILES.get(item_name, {})
    else:
        profile = PREFERENCE_PROFILES.get(item_name, {})

    # Fetch purchase history once, reuse for all products
    purchase_history = get_purchase_history(item_name, conn=conn)
    if purchase_history.get("total_orders", 0) > 0:
        top_brand = max(purchase_history["brands"].items(), key=lambda x: x[1]) if purchase_history["brands"] else ("?", 0)
        top_size = max(purchase_history["sizes"].items(), key=lambda x: x[1]) if purchase_history.get("sizes") else ("?", 0)
        top_store = max(purchase_history["stores"].items(), key=lambda x: x[1]) if purchase_history.get("stores") else ("?", 0)
        quality_kws = list(purchase_history.get("origins", {}).keys())[:3]
        print(f"    [HISTORY] {item_name}: {purchase_history['total_orders']} orders | "
              f"brand: {top_brand[0]} ({top_brand[1]}x) | "
              f"size: {top_size[0]} ({top_size[1]}x) | "
              f"store: #{top_store[0]} ({top_store[1]}x) | "
              f"quality: {quality_kws or 'none'} | "
              f"avg price: {purchase_history['avg_price']} AED")

    # Durable household rules (prefer/avoid brand) resolved once per item, so
    # the chat curator honors the SAME learned brand preferences as the
    # scheduled-list validator — a brand learned via swap or delivered feedback
    # now moves chat suggestions the same way it moves the scheduled list.
    rules_view = _resolve_rules_view(item_name, item_id, conn)

    results = []
    for product in products:
        scored = score_single_product(
            item_name, store_name, product, profile, purchase_history,
            item_id=item_id, rules_view=rules_view,
        )
        results.append(scored)

    # Sort: non-disqualified first, then by score descending
    results.sort(key=lambda x: (not x["disqualified"], x["score"]), reverse=True)
    return results


def pick_best(item_name, store_name, products, conn=None):
    """
    Pick the single best product from scraped results.

    Returns:
        (best_product_dict, score_result) or (None, None) if nothing acceptable.
    """
    ranked = score_products(item_name, store_name, products, conn=conn)
    if not ranked:
        return None, None

    best = ranked[0]
    if best["disqualified"] or best["tier"] == "avoid":
        return None, best  # nothing acceptable — caller should flag for check-in

    return best["product"], best


def needs_claude_review(score_result):
    """
    Should we escalate this to Claude API for a smarter evaluation?

    Returns True when the local scoring is uncertain — e.g., the best
    option is borderline, or multiple options are very close.
    """
    if score_result is None:
        return True  # no results at all

    # If best result is just "fallback" tier, Claude might find something better
    if score_result["tier"] == "fallback":
        return True

    # If score is in the uncertain zone (45-65), get a second opinion
    if 45 <= score_result["score"] <= 65:
        return True

    return False


# ────────────────────────────────────────────
# Claude API scoring (placeholder — wired up later)
# ────────────────────────────────────────────


# ────────────────────────────────────────────
# CLI demo
# ────────────────────────────────────────────

if __name__ == "__main__":
    # Demo: scoring with purchase history integration
    print("=" * 60)
    print("  Product Scoring Demo (with Order History)")
    print("=" * 60)

    # Connect to DB for real purchase history
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        has_db = True
    except Exception:
        conn = None
        has_db = False

    # Show purchase history for Eggs
    if has_db:
        history = get_purchase_history("Eggs", conn=conn)
        print(f"\n  Purchase history for Eggs:")
        print(f"    Total orders: {history['total_orders']}")
        for brand, count in history.get("brands", {}).items():
            print(f"    Brand '{brand}': ordered {count}x")

    # Simulate Carrefour search results for "Eggs"
    fake_egg_results = [
        {"name": "Jenan Organic Free Range Eggs", "price": 34.49, "size": "15 PCS", "brand": "Jenan"},
        {"name": "Al Ain Fresh Eggs", "price": 15.99, "size": "15 PCS", "brand": "Al Ain"},
        {"name": "Emirates Bio Farm Organic Eggs", "price": 38.99, "size": "15 PCS", "brand": "Emirates Bio Farm"},
        {"name": "Farm Fresh Cage Eggs", "price": 9.99, "size": "30 PCS", "brand": "Farm Fresh"},
        {"name": "Al Ain Organic Free Range Eggs", "price": 29.99, "size": "15 PCS", "brand": "Al Ain"},
    ]

    print("\n--- Scoring Eggs on Carrefour UAE (with purchase history) ---")
    ranked = score_products("Eggs", "Carrefour UAE", fake_egg_results, conn=conn)
    for i, r in enumerate(ranked, 1):
        p = r["product"]
        print(f"\n  #{i} [{r['tier'].upper()}] Score: {r['score']}/100")
        print(f"     {p['name']} — {p['price']} AED ({p['size']})")
        for reason in r["reasons"]:
            print(f"       {reason}")

    # Simulate search results for "Apple" on Kibsons
    fake_apple_results = [
        {"name": "Royal Gala Apple", "price": 13.50, "size": "1kg"},
        {"name": "Organic Fuji Apple", "price": 18.00, "size": "1kg"},
        {"name": "Green Apple (Local)", "price": 5.99, "size": "1kg"},
        {"name": "Pink Lady Apple Premium Import", "price": 22.00, "size": "1kg"},
    ]

    print("\n\n--- Scoring Apple on Kibsons (with purchase history) ---")
    ranked = score_products("Apple", "Kibsons", fake_apple_results, conn=conn)
    for i, r in enumerate(ranked, 1):
        p = r["product"]
        price = p.get('price', '?')
        print(f"\n  #{i} [{r['tier'].upper()}] Score: {r['score']}/100")
        print(f"     {p['name']} — {price} AED")
        for reason in r["reasons"]:
            print(f"       {reason}")

    # Show pick_best
    best, result = pick_best("Eggs", "Carrefour UAE", fake_egg_results, conn=conn)
    print(f"\n\n--- Best pick for Eggs: ---")
    if best:
        print(f"  {best['name']} ({result['tier']}, score {result['score']})")
    else:
        print(f"  No acceptable option found!")

    # Show when Claude review is needed
    _, borderline = pick_best("Beef Mince", "Carrefour UAE", [
        {"name": "Beef Mince", "price": 25.00, "size": "500g"},
    ], conn=conn)
    if borderline and needs_claude_review(borderline):
        print(f"\n  Beef Mince result is borderline — would escalate to Claude API")

    if conn:
        conn.close()
