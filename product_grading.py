"""
AutoRestock - Product Grading Engine

Category-aware quality grading for grocery products.
Separates OBJECTIVE quality from SUBJECTIVE household preferences.

Two layers:
  1. Extract structured attributes from raw product data
  2. Assign quality grade (A-F) based on category-specific rules

The existing product_score.py handles the SUBJECTIVE layer (household preferences,
brand loyalty, purchase history). This module feeds into that.

Grades:
  A -- Premium quality (organic, clean ingredients, premium origin, fresh)
  B -- Good quality (fresh, good origin, minimal processing)
  C -- Standard quality (average, nothing special, nothing bad)
  D -- Low quality (heavily processed, many additives, avoided origins)
  F -- Wrong product / bulk wholesale / irrelevant search result

Usage:
  from product_grading import grade_product, grade_products

  result = grade_product(scraped_product_dict)
  # -> {"grade": "A", "points": 88, "category": "Meat", "reasons": [...], "attributes": {...}}
"""

import re


# ============================================================
# ORIGIN QUALITY TIERS (per category)
# ============================================================
# What country a food comes from matters differently per category.
# Australian beef is premium; Australian cheese is just OK.

ORIGIN_TIERS = {
    "Meat": {
        "premium": ["australia", "new zealand", "ireland", "scotland", "uk", "united kingdom"],
        "good": ["usa", "united states", "argentina", "uruguay", "france", "spain", "italy"],
        "standard": ["uae", "saudi arabia", "pakistan", "south africa"],
        "avoid": ["brazil", "india"],
    },
    "Poultry": {
        "premium": ["new zealand", "ireland", "uk", "united kingdom", "france"],
        "good": ["australia", "usa", "united states"],
        "standard": ["uae", "saudi arabia", "brazil"],
        "avoid": [],
    },
    "Seafood": {
        "premium": ["norway", "scotland", "alaska", "japan", "new zealand", "iceland"],
        "good": ["uk", "united kingdom", "france", "spain", "canada", "australia"],
        "standard": ["vietnam", "india", "egypt", "oman", "gcc", "thailand", "indonesia"],
        "avoid": [],
    },
    "Dairy": {
        "premium": ["ireland", "denmark", "netherlands", "france", "new zealand", "switzerland"],
        "good": ["uk", "united kingdom", "australia", "spain", "italy", "greece"],
        "standard": ["uae", "saudi arabia", "turkey", "egypt"],
        "avoid": [],
    },
    "Produce": {
        "premium": ["italy", "spain", "france", "south africa", "new zealand", "usa"],
        "good": ["australia", "egypt", "kenya", "netherlands", "chile"],
        "standard": ["uae", "oman", "india", "pakistan", "jordan"],
        "avoid": [],
    },
    "Pantry": {
        "premium": ["italy", "spain", "greece", "france"],
        "good": ["uk", "australia", "usa", "turkey"],
        "standard": ["uae", "saudi arabia", "india", "egypt"],
        "avoid": [],
    },
}


# ============================================================
# CATEGORY QUALITY DIMENSIONS
# ============================================================
# Every category has specific quality signals that products differentiate on.
# Derived from analyzing 26,394 products in the catalog.
#
# Structure per category:
#   dimensions: ordered list of quality axes (most important first)
#   Each dimension has:
#     - key: attribute name used in extract_attributes() output
#     - label: human-readable name
#     - type: "boolean" | "enum" | "tier" | "numeric"
#     - values: possible values (for enum/tier types)
#     - weight: default scoring weight (0-10, higher = more important)
#     - prevalence: % of products in this category that have this signal
#
# This is the OBJECTIVE product quality schema. Household SUBJECTIVE
# preferences (which values are good/bad) live in household_profile.py.

CATEGORY_QUALITY_DIMENSIONS = {
    "Meat": {
        "label": "Meat (Beef, Lamb, Veal)",
        "dimensions": [
            {
                "key": "origin_tier",
                "label": "Origin Country",
                "type": "tier",
                "values": ["premium", "good", "standard", "avoid"],
                "weight": 9,
                "prevalence": 85,  # 85% of beef has origin data
                "note": "Australia (55%), South Africa, NZ, Argentina in catalog",
            },
            {
                "key": "is_grass_fed",
                "label": "Grass Fed",
                "type": "boolean",
                "weight": 8,
                "prevalence": 19,  # 18.9% of beef
            },
            {
                "key": "breed",
                "label": "Breed / Quality Tier",
                "type": "enum",
                "values": ["wagyu", "angus", "premium", "standard"],
                "weight": 7,
                "prevalence": 24,  # 12% angus + 12% wagyu
            },
            {
                "key": "is_organic",
                "label": "Organic",
                "type": "boolean",
                "weight": 6,
                "prevalence": 2,  # only 2.3% of beef is organic
            },
            {
                "key": "cut_type",
                "label": "Cut Type",
                "type": "enum",
                "values": ["fillet", "striploin", "ribeye", "sirloin",
                           "steak", "mince", "bones", "whole"],
                "weight": 5,
            },
            {
                "key": "processing_level",
                "label": "Processing Level",
                "type": "tier",
                "values": ["raw", "minimal", "marinated", "breaded",
                           "processed", "ready_meal"],
                "weight": 7,
            },
            {
                "key": "is_free_range",
                "label": "Free Range",
                "type": "boolean",
                "weight": 4,
                "prevalence": 0,  # rare for beef
            },
        ],
    },

    "Poultry": {
        "label": "Chicken, Turkey, Duck",
        "dimensions": [
            {
                "key": "is_organic",
                "label": "Organic",
                "type": "boolean",
                "weight": 9,
                "prevalence": 21,  # 21.4% - highest organic rate in meat
                "note": "Bostock NZ is primary organic chicken brand",
            },
            {
                "key": "is_free_range",
                "label": "Free Range",
                "type": "boolean",
                "weight": 8,
                "prevalence": 5,
            },
            {
                "key": "origin_tier",
                "label": "Origin Country",
                "type": "tier",
                "values": ["premium", "good", "standard", "avoid"],
                "weight": 7,
                "note": "NZ/Ireland = premium, UAE = standard, Brazil = avoid",
            },
            {
                "key": "cut_type",
                "label": "Cut Type",
                "type": "enum",
                "values": ["breast", "fillet", "thigh", "drumstick",
                           "wing", "whole"],
                "weight": 5,
                "prevalence": 75,  # most chicken has clear cut type
            },
            {
                "key": "processing_level",
                "label": "Processing Level",
                "type": "tier",
                "values": ["raw", "minimal", "marinated", "breaded",
                           "processed", "ready_meal"],
                "weight": 7,
            },
            {
                "key": "antibiotic_free",
                "label": "Antibiotic Free",
                "type": "boolean",
                "weight": 4,
                "prevalence": 3,
            },
        ],
    },

    "Seafood": {
        "label": "Fish, Prawns, Shellfish",
        "dimensions": [
            {
                "key": "cut_type",
                "label": "Cut / Form",
                "type": "enum",
                "values": ["fillet", "steak", "whole", "cleaned_whole",
                           "head", "portion"],
                "weight": 9,
                "prevalence": 59,  # 58.5% of fish is fillet
                "note": "Fillet is the dominant quality signal for fish",
            },
            {
                "key": "origin_tier",
                "label": "Origin Country",
                "type": "tier",
                "values": ["premium", "good", "standard", "avoid"],
                "weight": 9,
                "note": "Norway dominates (27%), then UK, Vietnam",
            },
            {
                "key": "is_wild_caught",
                "label": "Wild Caught",
                "type": "boolean",
                "weight": 8,
                "prevalence": 5,  # 5.1% in canned, less in fresh
                "note": "Wild-caught is the seafood equivalent of grass-fed",
            },
            {
                "key": "freshness",
                "label": "Freshness",
                "type": "enum",
                "values": ["fresh", "frozen", "ambient"],
                "weight": 7,
            },
            {
                "key": "processing_level",
                "label": "Processing Level",
                "type": "tier",
                "values": ["raw", "minimal", "smoked", "marinated",
                           "breaded", "processed"],
                "weight": 6,
                "note": "Smoked is valued for certain fish (salmon)",
            },
        ],
    },

    "Dairy": {
        "label": "Milk, Yoghurt, Cheese, Butter, Eggs, Kefir",
        "dimensions": [
            {
                "key": "is_organic",
                "label": "Organic",
                "type": "boolean",
                "weight": 9,
                "prevalence": 20,  # 20-32% depending on sub-type
                "note": "Eggs 32%, yoghurt 20%, milk 8%",
            },
            {
                "key": "fat_content",
                "label": "Fat Content",
                "type": "enum",
                "values": ["full", "low", "zero"],
                "weight": 7,
                "prevalence": 60,  # most dairy declares fat content
                "note": "Full fat 12%, low fat 8% in yoghurt",
            },
            {
                "key": "dairy_subtype",
                "label": "Product Type / Style",
                "type": "enum",
                "values": {
                    "yoghurt": ["plain", "natural", "greek", "flavored",
                                "probiotic", "drinking"],
                    "milk": ["fresh", "uht", "raw", "plant_based"],
                    "cheese": ["natural", "aged", "processed"],
                    "eggs": ["free_range", "cage_free", "conventional",
                             "pasteurized"],
                    "butter": ["unsalted", "salted", "cultured",
                               "margarine"],
                    "kefir": ["plain", "flavored"],
                },
                "weight": 8,
                "note": "Greek is 50% of yoghurt, plain 9%, free_range 16% of eggs",
            },
            {
                "key": "is_free_range",
                "label": "Free Range",
                "type": "boolean",
                "weight": 8,
                "prevalence": 16,  # 15.9% of eggs
                "note": "Primarily relevant for eggs",
            },
            {
                "key": "origin_tier",
                "label": "Origin Country",
                "type": "tier",
                "values": ["premium", "good", "standard", "avoid"],
                "weight": 5,
                "note": "Ireland/Denmark/France = premium dairy",
            },
            {
                "key": "freshness",
                "label": "Freshness Type",
                "type": "enum",
                "values": ["fresh", "uht", "frozen"],
                "weight": 6,
                "note": "Fresh vs UHT is key for milk",
            },
            {
                "key": "ingredients",
                "label": "Ingredient Quality",
                "type": "tier",
                "values": ["clean_label", "standard", "has_additives"],
                "weight": 5,
            },
        ],
    },

    "Produce": {
        "label": "Fruits & Vegetables",
        "dimensions": [
            {
                "key": "is_organic",
                "label": "Organic",
                "type": "boolean",
                "weight": 10,  # highest weight - organic is THE signal for produce
                "prevalence": 15,  # varies wildly by item
                "note": "Dirty Dozen items MUST be organic, Clean Fifteen can skip",
            },
            {
                "key": "freshness",
                "label": "Freshness",
                "type": "enum",
                "values": ["fresh", "frozen"],
                "weight": 8,
            },
            {
                "key": "origin_tier",
                "label": "Origin Country",
                "type": "tier",
                "values": ["premium", "good", "standard", "avoid"],
                "weight": 3,
                "note": "Origin matters less for produce if organic",
            },
            {
                "key": "size_variant",
                "label": "Size Variant",
                "type": "enum",
                "values": ["baby", "mini", "standard", "large", "jumbo"],
                "weight": 2,
            },
        ],
    },

    "Pantry": {
        "label": "Pasta, Oil, Rice, Grains, Sauces",
        "dimensions": [
            {
                "key": "pantry_subtype",
                "label": "Product Quality Grade",
                "type": "enum",
                "values": {
                    "pasta": ["legume", "veggie", "whole_wheat",
                              "refined_wheat"],
                    "oil": ["extra_virgin", "virgin", "pomace",
                            "light", "regular"],
                    "rice": ["basmati", "whole_grain", "white"],
                },
                "weight": 9,
                "note": "Extra virgin 39% of oil; legume pasta is niche but premium",
            },
            {
                "key": "origin_tier",
                "label": "Origin Country",
                "type": "tier",
                "values": ["premium", "good", "standard", "avoid"],
                "weight": 7,
                "note": "Italy dominates pasta (36%) and olive oil, Spain/Greece too",
            },
            {
                "key": "is_organic",
                "label": "Organic",
                "type": "boolean",
                "weight": 5,
                "prevalence": 6,  # 6% organic in pasta
                "note": "Less important than origin/grade for pantry",
            },
            {
                "key": "ingredients",
                "label": "Ingredient Quality",
                "type": "tier",
                "values": ["clean_label", "standard", "has_additives"],
                "weight": 6,
            },
        ],
    },

    "Bread": {
        "label": "Bread, Bagels, Rolls",
        "dimensions": [
            {
                "key": "bread_type",
                "label": "Bread Type",
                "type": "enum",
                "values": ["sourdough", "rye", "wholemeal", "white"],
                "weight": 8,
                "prevalence": 33,  # 33% sourdough in bagels
                "note": "Sourdough is the premium signal for bread",
            },
            {
                "key": "ingredients",
                "label": "Ingredient Quality",
                "type": "tier",
                "values": ["clean_label", "standard", "has_additives"],
                "weight": 9,
                "note": "Bread is where additives/preservatives matter most",
            },
            {
                "key": "has_seeds",
                "label": "Seeds / Grains",
                "type": "boolean",
                "weight": 5,
            },
            {
                "key": "is_organic",
                "label": "Organic",
                "type": "boolean",
                "weight": 4,
                "prevalence": 3,  # rare for bread
            },
        ],
    },
}



def get_dimension_weight(category, dimension_key):
    """
    Get the scoring weight for a specific quality dimension in a category.

    Returns weight (0-10) or 0 if not found.
    """
    cat_dims = CATEGORY_QUALITY_DIMENSIONS.get(category, {})
    for dim in cat_dims.get("dimensions", []):
        if dim["key"] == dimension_key:
            return dim["weight"]
    return 0



# ============================================================
# PROCESSING LEVEL KEYWORDS
# ============================================================
# Ordered from most processed to least. First match wins.

PROCESSING_LEVELS = [
    ("ready_meal", ["ready to eat", "ready meal", "kiev", "nugget", "burger patty",
                    "meatball", "spring roll"]),
    ("breaded", ["breaded", "crumbed", "coated", "battered", "crispy", "southern fried"]),
    ("processed", ["mortadella", "pastrami", "pepperoni", "salami", "bacon",
                   "hot dog", "frankfurter", "luncheon"]),
    ("marinated", ["marinated", "seasoned", "shawarma", "tikka", "tandoori", "spiced",
                   "teriyaki", "bbq"]),
    ("frozen", ["frozen", "iqf"]),
    ("minimal", ["skinless", "boneless", "fillet", "cleaned", "trimmed",
                 "sliced", "diced", "minced"]),
]


# Known filler ingredients (signal of lower quality in processed foods)
FILLERS = [
    "corn starch", "soya protein", "soy protein", "maltodextrin",
    "modified starch", "modified corn starch", "glucose syrup", "dextrose",
    "vegetable protein", "hydrolyzed vegetable protein",
    "milk powder", "whey powder", "bread crumb",
]


# ============================================================
# CATEGORY DETECTION
# ============================================================

def detect_category(product_name, category_hint=""):
    """
    Detect broad food category from product name.

    Returns one of: Meat, Poultry, Seafood, Dairy, Produce, Pantry, Bread, Unknown
    """
    name = product_name.lower()
    hint = category_hint.lower() if category_hint else ""

    # Seafood (check before meat -- "fish" could appear in meat contexts)
    if any(kw in name for kw in [
        "salmon", "fish", "shrimp", "prawn", "tuna", "sardine", "mackerel",
        "tilapia", "mussel", "squid", "crab", "lobster", "cod", "sea bass",
        "seabass", "trout", "halibut",
    ]) or "seafood" in hint:
        return "Seafood"

    # Poultry
    if any(kw in name for kw in ["chicken", "turkey", "duck", "poultry"]) or "poultry" in hint:
        return "Poultry"

    # Meat
    if any(kw in name for kw in [
        "beef", "lamb", "veal", "striploin", "ribeye", "sirloin",
        "mince", "steak", "sausage", "bacon", "mortadella", "pastrami",
        "pepperoni", "salami",
    ]) or "meat" in hint or "deli" in hint:
        return "Meat"

    # Dairy
    if any(kw in name for kw in [
        "milk", "yogurt", "yoghurt", "cheese", "butter", "cream", "kefir",
        "egg", "dairy", "labneh", "ghee",
    ]) or "dairy" in hint:
        return "Dairy"

    # Bread
    if any(kw in name for kw in [
        "bread", "loaf", "baguette", "croissant", "pita", "naan", "tortilla",
    ]) or "bakery" in hint:
        return "Bread"

    # Produce
    if any(kw in name for kw in [
        "apple", "banana", "orange", "mango", "grape", "strawberry", "blueberry",
        "tomato", "cucumber", "pepper", "capsicum", "spinach", "rocket", "lettuce",
        "onion", "potato", "carrot", "broccoli", "avocado", "lemon", "lime",
        "pear", "pineapple", "watermelon", "melon", "kiwi", "berry",
    ]) or "fruit" in hint or "vegetable" in hint:
        return "Produce"

    # Pantry
    if any(kw in name for kw in [
        "pasta", "spaghetti", "penne", "fusilli", "orzo", "lasagne", "macaroni",
        "olive oil", "oil", "vinegar", "sauce", "rice", "flour",
        "sugar", "honey", "jam", "cereal", "oats",
    ]) or "pantry" in hint:
        return "Pantry"

    return "Unknown"


# ============================================================
# ATTRIBUTE EXTRACTION HELPERS
# ============================================================

def _get_origin_tier(origin_country, category):
    """Map origin country to quality tier for this category."""
    if not origin_country:
        return "unknown"

    origin = origin_country.lower().strip()
    tiers = ORIGIN_TIERS.get(category, ORIGIN_TIERS.get("Pantry", {}))

    for tier_name in ["premium", "good", "standard", "avoid"]:
        countries = tiers.get(tier_name, [])
        if any(c in origin or origin in c for c in countries):
            return tier_name

    return "unknown"


def _get_processing_level(name, description=""):
    """Detect how processed a product is. Returns level string."""
    text = f"{name} {description}".lower()

    for level, keywords in PROCESSING_LEVELS:
        if any(kw in text for kw in keywords):
            return level

    return "raw"


def _get_freshness(name, description="", storage_info=""):
    """Detect freshness: fresh, frozen, ambient, unknown."""
    text = f"{name} {description} {storage_info}".lower()

    if "frozen" in text or "iqf" in text:
        return "frozen"
    if any(kw in text for kw in ["fresh", "chilled", "keep refrigerat", "keep chilled"]):
        return "fresh"
    if any(kw in text for kw in ["ambient", "cool and dry", "cool dry", "room temperature"]):
        return "ambient"

    return "unknown"


def _analyze_ingredients(ingredients_text):
    """
    Analyze ingredient list for quality signals.

    Returns dict with:
        ingredient_count, additive_count, filler_count,
        main_ingredient_pct, first_ingredient, has_clean_label,
        additives (list), fillers (list)
    """
    empty = {
        "ingredient_count": 0, "additive_count": 0, "filler_count": 0,
        "main_ingredient_pct": None, "first_ingredient": "",
        "has_clean_label": None, "additives": [], "fillers": [],
    }

    if not ingredients_text or len(ingredients_text.strip()) < 3:
        return empty

    text = ingredients_text.strip()
    text_lower = text.lower()

    # Count E-numbers (E100 through E1999)
    additives = re.findall(r'E-?\d{3,4}', text, re.I)
    additive_count = len(set(additives))  # unique

    # Count known fillers
    found_fillers = [f for f in FILLERS if f in text_lower]

    # Main ingredient percentage: "Chicken (84%)", "60% min. Cow's Milk", etc.
    pct_match = re.search(r'(\d{1,3})\s*%', text)
    main_pct = float(pct_match.group(1)) if pct_match else None

    # Count ingredients (rough: split by comma/semicolons, ignore sub-lists in brackets)
    # Remove bracketed content first to avoid counting sub-ingredients
    clean = re.sub(r'\([^)]*\)', '', text)
    clean = re.sub(r'\[[^\]]*\]', '', clean)
    parts = re.split(r'[,;]', clean)
    ingredient_count = len([p for p in parts if len(p.strip()) > 1])

    # First ingredient
    first = parts[0].strip() if parts else ""
    first = re.sub(r'\d+%.*', '', first).strip()[:60]

    # Clean label: few ingredients, no additives, no fillers
    has_clean = (ingredient_count <= 5 and additive_count == 0 and len(found_fillers) == 0)

    return {
        "ingredient_count": ingredient_count,
        "additive_count": additive_count,
        "filler_count": len(found_fillers),
        "main_ingredient_pct": main_pct,
        "first_ingredient": first,
        "has_clean_label": has_clean,
        "additives": list(set(additives)),
        "fillers": found_fillers,
    }


def _is_bulk(product_name, size_str=""):
    """Detect if this is a bulk/wholesale pack (not suitable for household)."""
    text = f"{product_name} {size_str}".lower()

    # Weight-based: 5kg+ is bulk
    m = re.search(r'(\d+(?:\.\d+)?)\s*kg', text)
    if m and float(m.group(1)) >= 5:
        return True

    # "carton" or "case" usually means wholesale
    if "carton" in text or "case of" in text:
        return True

    # "approx Xkg" pattern from Kibsons wholesale
    if re.search(r'approx\s+\d+\s*kg', text):
        return True

    return False


# ============================================================
# MAIN ATTRIBUTE EXTRACTION
# ============================================================

def extract_attributes(product, category_hint=""):
    """
    Extract structured quality attributes from a raw scraped product.

    Args:
        product: dict with name, price, origin_country, is_organic, ingredients, etc.
        category_hint: optional category context

    Returns:
        dict of all extracted attributes
    """
    name = product.get("name", "")
    desc = product.get("description", "")
    ingredients = product.get("ingredients", "")
    storage = product.get("storage_info", "")
    origin = product.get("origin_country", "")
    size = product.get("size", "")

    category = detect_category(name, category_hint)
    full_text = f"{name} {desc}".lower()

    # Organic detection: avoid matching "non-organic", "not organic", "inorganic"
    organic_in_text = bool(
        re.search(r'\borganic\b', full_text)
        and not re.search(r'\b(?:non|not|in)[\s-]?organic\b', full_text)
    )

    attrs = {
        "category": category,
        "is_organic": bool(product.get("is_organic")) or organic_in_text,
        "is_free_range": "free range" in full_text or "free-range" in full_text,
        "is_grass_fed": "grass fed" in full_text or "grass-fed" in full_text,
        "is_wild_caught": "wild" in full_text and category == "Seafood",
        "origin_country": origin,
        "origin_tier": _get_origin_tier(origin, category),
        "processing_level": _get_processing_level(name, desc),
        "freshness": _get_freshness(name, desc, storage),
        "is_bulk": _is_bulk(name, size),
        "ingredients": _analyze_ingredients(ingredients),
    }

    # -- Meat / Poultry specifics --
    if category in ("Meat", "Poultry"):
        attrs["antibiotic_free"] = any(kw in full_text for kw in [
            "antibiotic free", "no antibiotic", "antibiotic-free"])
        attrs["hormone_free"] = any(kw in full_text for kw in [
            "hormone free", "no hormone", "hormone-free"])

        cuts = {
            "breast": r"\bbreast", "fillet": r"\bfillet", "mince": r"\bmince",
            "striploin": r"\bstriploin", "ribeye": r"\bribeye", "sirloin": r"\bsirloin",
            "steak": r"\bsteak", "drumstick": r"\bdrumstick", "thigh": r"\bthigh",
            "wing": r"\bwing", "bones": r"\bbones?\b", "whole": r"\bwhole\b",
        }
        attrs["cut_type"] = "unknown"
        for cut, pattern in cuts.items():
            if re.search(pattern, name.lower()):
                attrs["cut_type"] = cut
                break

        # Breed detection (beef-specific quality signal)
        if category == "Meat":
            if "wagyu" in full_text:
                attrs["breed"] = "wagyu"
            elif "angus" in full_text:
                attrs["breed"] = "angus"
            elif any(kw in full_text for kw in ["premium", "select", "choice"]):
                attrs["breed"] = "premium"
            else:
                attrs["breed"] = "standard"

    # -- Seafood specifics --
    elif category == "Seafood":
        for cut_name, kw in [("fillet", "fillet"), ("steak", "steak"),
                             ("whole", "whole"), ("head", "head"),
                             ("cleaned_whole", "cleaned")]:
            if kw in name.lower():
                attrs["cut_type"] = cut_name
                break
        else:
            attrs["cut_type"] = "unknown"

    # -- Dairy specifics --
    elif category == "Dairy":
        nl = name.lower()

        if "egg" in nl:
            attrs["dairy_type"] = "eggs"

        elif "cheese" in nl:
            attrs["dairy_type"] = "cheese"
            # Cheese type
            attrs["cheese_type"] = "other"
            for ct in ["cheddar", "gouda", "brie", "camembert", "mozzarella",
                       "parmesan", "feta", "halloumi", "cream cheese", "cottage", "ricotta"]:
                if ct in full_text:
                    attrs["cheese_type"] = ct
                    break
            # Milk source
            if "sheep" in full_text:
                attrs["milk_source"] = "sheep"
            elif "goat" in full_text:
                attrs["milk_source"] = "goat"
            else:
                attrs["milk_source"] = "cow"
            # Processing
            if any(kw in full_text for kw in ["processed", "spread", "slice singles",
                                              "cheese string", "cheese slice"]):
                attrs["cheese_processing"] = "processed"
            elif any(kw in full_text for kw in ["aged", "mature", "semicurado", "curado", "old"]):
                attrs["cheese_processing"] = "aged"
            else:
                attrs["cheese_processing"] = "natural"

        elif any(kw in nl for kw in ["yogurt", "yoghurt", "dairy snack"]):
            attrs["dairy_type"] = "yogurt"
            attrs["is_greek"] = "greek" in full_text
            attrs["is_plain"] = not any(f in full_text for f in [
                "vanilla", "strawberry", "chocolate", "mango", "blueberry",
                "fruit", "flavour", "flavor", "honey"])
            if any(f in full_text for f in ["full fat", "whole milk"]):
                attrs["fat_content"] = "full"
            elif any(f in full_text for f in ["low fat", "0%", "diet", "light", "lite"]):
                attrs["fat_content"] = "low"
            else:
                attrs["fat_content"] = "unknown"

        elif any(kw in nl for kw in ["milk", "oat drink"]):
            attrs["dairy_type"] = "milk"
            if "double cream" in full_text:
                attrs["milk_type"] = "double_cream"
            elif any(f in full_text for f in ["full fat", "whole"]):
                attrs["milk_type"] = "full_fat"
            elif any(f in full_text for f in ["skim", "low fat", "lite"]):
                attrs["milk_type"] = "low_fat"
            elif any(f in full_text for f in ["oat", "almond", "soy", "coconut"]):
                attrs["milk_type"] = "plant_based"
            else:
                attrs["milk_type"] = "unknown"
            attrs["is_uht"] = any(kw in full_text for kw in ["uht", "long life"])

        elif "butter" in nl:
            attrs["dairy_type"] = "butter"
            attrs["is_unsalted"] = "unsalted" in full_text
            attrs["is_margarine"] = any(kw in nl for kw in ["margarine", "spread"])

        elif "kefir" in nl:
            attrs["dairy_type"] = "kefir"
            attrs["is_plain"] = not any(f in full_text for f in [
                "vanilla", "strawberry", "chocolate", "mango", "fruit", "flavour"])

        else:
            attrs["dairy_type"] = "unknown"

    # -- Pantry specifics --
    elif category == "Pantry":
        nl = name.lower()

        if any(kw in nl for kw in ["pasta", "spaghetti", "penne", "fusilli",
                                   "orzo", "lasagne", "macaroni", "rigatoni"]):
            attrs["pantry_type"] = "pasta"
            if any(kw in full_text for kw in ["lentil", "chickpea", "bean", "legume"]):
                attrs["pasta_base"] = "legume"
            elif any(kw in full_text for kw in ["tricolour", "vegetable", "veggie",
                                                "spinach pasta", "beetroot"]):
                attrs["pasta_base"] = "veggie"
            elif any(kw in full_text for kw in ["whole wheat", "wholemeal", "whole grain"]):
                attrs["pasta_base"] = "whole_wheat"
            else:
                attrs["pasta_base"] = "refined_wheat"

        elif "oil" in nl:
            attrs["pantry_type"] = "oil"
            if "extra virgin" in full_text:
                attrs["oil_grade"] = "extra_virgin"
            elif "virgin" in full_text:
                attrs["oil_grade"] = "virgin"
            elif "pomace" in full_text:
                attrs["oil_grade"] = "pomace"
            elif "light" in full_text:
                attrs["oil_grade"] = "light"
            else:
                attrs["oil_grade"] = "regular"
        else:
            attrs["pantry_type"] = "other"

    # -- Bread specifics --
    elif category == "Bread":
        if "sourdough" in full_text:
            attrs["bread_type"] = "sourdough"
        elif "rye" in full_text:
            attrs["bread_type"] = "rye"
        elif any(kw in full_text for kw in ["whole", "wholemeal", "brown"]):
            attrs["bread_type"] = "wholemeal"
        elif "white" in full_text:
            attrs["bread_type"] = "white"
        else:
            attrs["bread_type"] = "unknown"
        attrs["has_seeds"] = any(kw in full_text for kw in [
            "seed", "flax", "chia", "sesame", "multi-grain", "multigrain"])

    return attrs


# ============================================================
# GRADING LOGIC (per category)
# ============================================================

def _points_to_grade(points):
    """Convert 0-100 points to letter grade."""
    if points >= 80:
        return "A"
    elif points >= 65:
        return "B"
    elif points >= 50:
        return "C"
    elif points >= 35:
        return "D"
    else:
        return "F"


def _grade_meat(attrs):
    """Grade meat and poultry. Returns (grade_letter, points, reasons)."""
    pts = 50
    reasons = []

    if attrs.get("is_bulk"):
        return "F", 10, ["Bulk pack - not household sized"]

    # -- Processing level (biggest differentiator) --
    proc = attrs.get("processing_level", "raw")
    proc_scores = {
        "raw": (15, "Fresh/raw (+15)"),
        "minimal": (12, "Minimally processed (+12)"),
        "marinated": (-5, "Marinated/seasoned (-5)"),
        "breaded": (-15, "Breaded/coated (-15)"),
        "processed": (-25, "Deli/processed meat (-25)"),
        "ready_meal": (-25, "Ready meal (-25)"),
        "frozen": (-10, "Frozen (-10)"),
    }
    delta, reason = proc_scores.get(proc, (0, ""))
    if delta:
        pts += delta
        reasons.append(reason)

    # -- Origin --
    tier = attrs.get("origin_tier", "unknown")
    origin_name = attrs.get("origin_country", "?")
    if tier == "premium":
        pts += 15
        reasons.append(f"Premium origin: {origin_name} (+15)")
    elif tier == "good":
        pts += 8
        reasons.append(f"Good origin: {origin_name} (+8)")
    elif tier == "avoid":
        pts -= 15
        reasons.append(f"Avoided origin: {origin_name} (-15)")

    # -- Breed / quality tier (beef-specific) --
    breed = attrs.get("breed", "standard")
    breed_scores = {
        "wagyu": (12, "Wagyu breed (+12)"),
        "angus": (8, "Angus breed (+8)"),
        "premium": (5, "Premium grade (+5)"),
        "standard": (0, ""),
    }
    delta, reason = breed_scores.get(breed, (0, ""))
    if delta:
        pts += delta
        reasons.append(reason)

    # -- Quality flags --
    if attrs.get("is_organic"):
        pts += 10
        reasons.append("Organic (+10)")
    if attrs.get("is_free_range"):
        pts += 8
        reasons.append("Free range (+8)")
    if attrs.get("is_grass_fed"):
        pts += 8
        reasons.append("Grass fed (+8)")
    if attrs.get("antibiotic_free"):
        pts += 5
        reasons.append("Antibiotic free (+5)")
    if attrs.get("hormone_free"):
        pts += 3
        reasons.append("Hormone free (+3)")

    # -- Ingredients quality (for processed items) --
    ing = attrs.get("ingredients", {})
    if ing.get("additive_count", 0) > 0:
        penalty = min(ing["additive_count"] * 3, 15)
        pts -= penalty
        reasons.append(f"{ing['additive_count']} additives (-{penalty})")
    if ing.get("filler_count", 0) > 0:
        penalty = min(ing["filler_count"] * 4, 12)
        pts -= penalty
        reasons.append(f"Fillers: {', '.join(ing['fillers'][:3])} (-{penalty})")
    if ing.get("main_ingredient_pct") is not None:
        pct = ing["main_ingredient_pct"]
        if pct >= 90:
            pts += 5
            reasons.append(f"Main ingredient {pct:.0f}% (+5)")
        elif pct < 70:
            pts -= 5
            reasons.append(f"Main ingredient only {pct:.0f}% (-5)")
    if ing.get("has_clean_label"):
        pts += 5
        reasons.append("Clean label (+5)")

    # -- Cut type --
    cut = attrs.get("cut_type", "unknown")
    if cut in ("breast", "fillet", "striploin", "ribeye"):
        pts += 3
        reasons.append(f"Premium cut: {cut} (+3)")
    elif cut in ("bones", "wing"):
        pts -= 5
        reasons.append(f"Low-value cut: {cut} (-5)")

    pts = max(0, min(100, pts))
    return _points_to_grade(pts), pts, reasons


def _grade_seafood(attrs):
    """Grade seafood products."""
    pts = 50
    reasons = []

    if attrs.get("is_bulk"):
        return "F", 10, ["Bulk pack"]

    # Origin
    tier = attrs.get("origin_tier", "unknown")
    origin_name = attrs.get("origin_country", "?")
    if tier == "premium":
        pts += 15
        reasons.append(f"Premium origin: {origin_name} (+15)")
    elif tier == "good":
        pts += 8
        reasons.append(f"Good origin: {origin_name} (+8)")

    # Wild caught
    if attrs.get("is_wild_caught"):
        pts += 10
        reasons.append("Wild caught (+10)")

    # Cut
    cut = attrs.get("cut_type", "unknown")
    cut_scores = {"fillet": 10, "steak": 8, "cleaned_whole": 3, "whole": 3, "head": -10}
    if cut in cut_scores:
        pts += cut_scores[cut]
        reasons.append(f"Cut: {cut} ({'+' if cut_scores[cut] > 0 else ''}{cut_scores[cut]})")

    # Freshness
    fresh = attrs.get("freshness", "unknown")
    if fresh == "fresh":
        pts += 5
        reasons.append("Fresh (+5)")
    elif fresh == "frozen":
        pts -= 8
        reasons.append("Frozen (-8)")

    # Organic
    if attrs.get("is_organic"):
        pts += 8
        reasons.append("Organic (+8)")

    # Processing
    proc = attrs.get("processing_level", "raw")
    if proc in ("breaded", "ready_meal", "processed"):
        pts -= 15
        reasons.append(f"Processed: {proc} (-15)")
    elif proc == "marinated":
        pts -= 5
        reasons.append("Marinated (-5)")

    pts = max(0, min(100, pts))
    return _points_to_grade(pts), pts, reasons


def _grade_dairy(attrs):
    """Grade dairy products."""
    pts = 50
    reasons = []

    if attrs.get("is_bulk"):
        return "F", 10, ["Bulk pack"]

    dairy_type = attrs.get("dairy_type", "unknown")

    # Organic (universal for dairy)
    if attrs.get("is_organic"):
        pts += 10
        reasons.append("Organic (+10)")
    if attrs.get("is_free_range"):
        pts += 5
        reasons.append("Free range (+5)")

    # Origin
    tier = attrs.get("origin_tier", "unknown")
    origin_name = attrs.get("origin_country", "?")
    if tier == "premium":
        pts += 8
        reasons.append(f"Premium dairy origin: {origin_name} (+8)")
    elif tier == "good":
        pts += 4
        reasons.append(f"Good origin: {origin_name} (+4)")

    # -- Type-specific grading --
    if dairy_type == "eggs":
        if attrs.get("is_free_range"):
            pts += 5  # extra bonus for eggs specifically
            reasons.append("Free range eggs (extra +5)")

    elif dairy_type == "cheese":
        proc = attrs.get("cheese_processing", "natural")
        if proc == "aged":
            pts += 5
            reasons.append("Aged/mature cheese (+5)")
        elif proc == "processed":
            pts -= 15
            reasons.append("Processed cheese (-15)")

    elif dairy_type == "yogurt":
        if attrs.get("is_greek"):
            pts += 5
            reasons.append("Greek style (+5)")
        if attrs.get("is_plain"):
            pts += 5
            reasons.append("Plain/natural (+5)")
        else:
            pts -= 5
            reasons.append("Flavored (-5)")
        fc = attrs.get("fat_content", "unknown")
        if fc == "full":
            pts += 3
            reasons.append("Full fat (+3)")
        elif fc == "low":
            pts -= 5
            reasons.append("Low fat/diet (-5)")

    elif dairy_type == "milk":
        if not attrs.get("is_uht", False):
            pts += 5
            reasons.append("Fresh milk (+5)")
        else:
            pts -= 5
            reasons.append("UHT/long life (-5)")
        mt = attrs.get("milk_type", "unknown")
        if mt == "double_cream":
            pts += 5
            reasons.append("Double cream (+5)")
        elif mt == "full_fat":
            pts += 3
            reasons.append("Full fat (+3)")
        elif mt == "low_fat":
            pts -= 3
            reasons.append("Low fat (-3)")

    elif dairy_type == "butter":
        if attrs.get("is_margarine"):
            pts -= 20
            reasons.append("Margarine/spread (-20)")
        if attrs.get("is_unsalted"):
            pts += 3
            reasons.append("Unsalted (+3)")

    elif dairy_type == "kefir":
        if attrs.get("is_plain"):
            pts += 5
            reasons.append("Plain kefir (+5)")

    # Ingredients
    ing = attrs.get("ingredients", {})
    if ing.get("has_clean_label"):
        pts += 5
        reasons.append("Clean ingredients (+5)")
    elif ing.get("additive_count", 0) > 3:
        pts -= 8
        reasons.append(f"{ing['additive_count']} additives (-8)")

    pts = max(0, min(100, pts))
    return _points_to_grade(pts), pts, reasons


def _grade_produce(attrs):
    """Grade fruits and vegetables."""
    pts = 55  # produce is generally fine, start slightly above neutral
    reasons = []

    if attrs.get("is_bulk"):
        return "F", 10, ["Bulk/wholesale pack - not for household"]

    if attrs.get("is_organic"):
        pts += 15
        reasons.append("Organic (+15)")

    tier = attrs.get("origin_tier", "unknown")
    origin_name = attrs.get("origin_country", "?")
    if tier == "premium":
        pts += 5
        reasons.append(f"Premium origin: {origin_name} (+5)")

    fresh = attrs.get("freshness", "unknown")
    if fresh == "fresh":
        pts += 3
        reasons.append("Fresh (+3)")
    elif fresh == "frozen":
        pts -= 10
        reasons.append("Frozen (-10)")

    pts = max(0, min(100, pts))
    return _points_to_grade(pts), pts, reasons


def _grade_pantry(attrs):
    """Grade pantry items (pasta, oils, etc.)."""
    pts = 50
    reasons = []

    if attrs.get("is_bulk"):
        return "F", 10, ["Bulk pack"]

    if attrs.get("is_organic"):
        pts += 8
        reasons.append("Organic (+8)")

    tier = attrs.get("origin_tier", "unknown")
    origin_name = attrs.get("origin_country", "?")
    if tier == "premium":
        pts += 8
        reasons.append(f"Premium origin: {origin_name} (+8)")
    elif tier == "good":
        pts += 4
        reasons.append(f"Good origin: {origin_name} (+4)")

    pantry_type = attrs.get("pantry_type", "other")

    if pantry_type == "pasta":
        base = attrs.get("pasta_base", "refined_wheat")
        base_scores = {
            "legume": (12, "Legume-based pasta (+12)"),
            "veggie": (10, "Vegetable-based pasta (+10)"),
            "whole_wheat": (6, "Whole wheat pasta (+6)"),
            "refined_wheat": (0, ""),
        }
        delta, reason = base_scores.get(base, (0, ""))
        if delta:
            pts += delta
            reasons.append(reason)

    elif pantry_type == "oil":
        oil_grade = attrs.get("oil_grade", "regular")
        oil_scores = {
            "extra_virgin": (15, "Extra virgin (+15)"),
            "virgin": (5, "Virgin grade (+5)"),
            "pomace": (-15, "Pomace grade (-15)"),
            "light": (-10, "Light/refined (-10)"),
            "regular": (0, ""),
        }
        delta, reason = oil_scores.get(oil_grade, (0, ""))
        if delta:
            pts += delta
            reasons.append(reason)

    # Ingredients
    ing = attrs.get("ingredients", {})
    if ing.get("has_clean_label"):
        pts += 5
        reasons.append("Clean ingredients (+5)")

    pts = max(0, min(100, pts))
    return _points_to_grade(pts), pts, reasons


def _grade_bread(attrs):
    """Grade bread products."""
    pts = 50
    reasons = []

    if attrs.get("is_bulk"):
        return "F", 10, ["Bulk pack"]

    bt = attrs.get("bread_type", "unknown")
    type_scores = {
        "sourdough": (12, "Sourdough (+12)"),
        "rye": (10, "Rye bread (+10)"),
        "wholemeal": (8, "Wholemeal/brown (+8)"),
        "white": (-5, "White bread (-5)"),
        "unknown": (0, ""),
    }
    delta, reason = type_scores.get(bt, (0, ""))
    if delta:
        pts += delta
        reasons.append(reason)

    if attrs.get("has_seeds"):
        pts += 5
        reasons.append("Has seeds/grains (+5)")

    if attrs.get("is_organic"):
        pts += 5
        reasons.append("Organic (+5)")

    ing = attrs.get("ingredients", {})
    if ing.get("has_clean_label"):
        pts += 5
        reasons.append("Clean ingredients (+5)")
    elif ing.get("additive_count", 0) > 3:
        pts -= 8
        reasons.append(f"Many additives (-8)")

    pts = max(0, min(100, pts))
    return _points_to_grade(pts), pts, reasons


# ============================================================
# MAIN GRADING FUNCTION
# ============================================================

def grade_product(product, category_hint=""):
    """
    Grade a single product. Main entry point.

    Args:
        product: scraped product dict (name, price, origin_country, etc.)
        category_hint: optional category context

    Returns:
        {
            "product_name": str,
            "grade": "A"|"B"|"C"|"D"|"F",
            "points": int (0-100),
            "category": str,
            "attributes": dict,
            "reasons": [str],
        }
    """
    attrs = extract_attributes(product, category_hint)
    category = attrs["category"]

    graders = {
        "Meat": _grade_meat,
        "Poultry": _grade_meat,
        "Seafood": _grade_seafood,
        "Dairy": _grade_dairy,
        "Produce": _grade_produce,
        "Pantry": _grade_pantry,
        "Bread": _grade_bread,
    }

    grader = graders.get(category)
    if grader:
        grade, points, reasons = grader(attrs)
    else:
        grade, points, reasons = "C", 50, ["Unknown category -- default grade"]

    return {
        "product_name": product.get("name", "?"),
        "grade": grade,
        "points": points,
        "category": category,
        "attributes": attrs,
        "reasons": reasons,
    }


def grade_products(products, category_hint=""):
    """Grade a list of products. Returns sorted best-first."""
    results = [grade_product(p, category_hint) for p in products]
    grade_order = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}
    results.sort(key=lambda r: (grade_order.get(r["grade"], 5), -r["points"]))
    return results


# ============================================================
# PRODUCT TAGS / BADGES
# ============================================================
# Quick visual labels for conversations and UI.
# Each tag has: id, icon, label, sentiment, and category applicability.
#
# Sentiments:
#   positive  -- green, good thing (organic, clean, your usual)
#   negative  -- red, warning (processed, additives, bulk)
#   info      -- blue/gray, neutral info (new find, price drop)
#
# Icons use simple ASCII for terminal compatibility.
# UI layer can map these to proper emoji/icons later.

TAG_REGISTRY = {
    # ── Quality signals ──
    "organic": {
        "icon": "[ORG]",
        "label": "Organic",
        "sentiment": "positive",
        "categories": "all",
        "priority": 10,
    },
    "free_range": {
        "icon": "[FR]",
        "label": "Free Range",
        "sentiment": "positive",
        "categories": ["Meat", "Poultry", "Dairy"],
        "priority": 11,
    },
    "grass_fed": {
        "icon": "[GF]",
        "label": "Grass Fed",
        "sentiment": "positive",
        "categories": ["Meat"],
        "priority": 12,
    },
    "wild_caught": {
        "icon": "[WILD]",
        "label": "Wild Caught",
        "sentiment": "positive",
        "categories": ["Seafood"],
        "priority": 12,
    },
    "clean_label": {
        "icon": "[CLN]",
        "label": "Clean Label",
        "sentiment": "positive",
        "categories": "all",
        "priority": 20,
    },
    "premium_origin": {
        "icon": "[>>]",
        "label": "",  # filled dynamically with country name
        "sentiment": "positive",
        "categories": ["Meat", "Poultry", "Seafood", "Dairy"],
        "priority": 15,
    },
    "extra_virgin": {
        "icon": "[EV]",
        "label": "Extra Virgin",
        "sentiment": "positive",
        "categories": ["Pantry"],
        "priority": 10,
    },
    "whole_grain": {
        "icon": "[WG]",
        "label": "",  # filled dynamically: "Legume Pasta", "Whole Wheat", etc.
        "sentiment": "positive",
        "categories": ["Pantry", "Bread"],
        "priority": 13,
    },
    "sourdough": {
        "icon": "[SD]",
        "label": "Sourdough",
        "sentiment": "positive",
        "categories": ["Bread"],
        "priority": 10,
    },
    "grade_a": {
        "icon": "[A]",
        "label": "Top Rated",
        "sentiment": "positive",
        "categories": "all",
        "priority": 1,
    },

    # ── Familiarity signals ──
    "your_usual": {
        "icon": "[*]",
        "label": "Your Usual",
        "sentiment": "positive",
        "categories": "all",
        "priority": 0,  # highest priority -- show first
    },
    "prev_ordered": {
        "icon": "[<]",
        "label": "Ordered Before",
        "sentiment": "positive",
        "categories": "all",
        "priority": 2,
    },
    "new_find": {
        "icon": "[NEW]",
        "label": "New Find",
        "sentiment": "info",
        "categories": "all",
        "priority": 5,
    },

    # ── Value signals ──
    "best_value": {
        "icon": "[$]",
        "label": "Best Value",
        "sentiment": "info",
        "categories": "all",
        "priority": 8,
    },
    "price_drop": {
        "icon": "[v$]",
        "label": "Price Drop",
        "sentiment": "positive",
        "categories": "all",
        "priority": 7,
    },

    # ── Warning signals ──
    "processed": {
        "icon": "[!!]",
        "label": "Processed",
        "sentiment": "negative",
        "categories": ["Meat", "Poultry", "Seafood", "Dairy"],
        "priority": 3,
    },
    "has_additives": {
        "icon": "[E#]",
        "label": "",  # filled dynamically: "5 Additives"
        "sentiment": "negative",
        "categories": "all",
        "priority": 4,
    },
    "has_fillers": {
        "icon": "[FL]",
        "label": "Contains Fillers",
        "sentiment": "negative",
        "categories": ["Meat", "Poultry", "Dairy"],
        "priority": 4,
    },
    "bulk_pack": {
        "icon": "[BLK]",
        "label": "Wholesale",
        "sentiment": "negative",
        "categories": "all",
        "priority": 1,
    },
    "frozen": {
        "icon": "[FRZ]",
        "label": "Frozen",
        "sentiment": "negative",
        "categories": ["Meat", "Poultry", "Seafood", "Produce"],
        "priority": 6,
    },
    "low_fat": {
        "icon": "[LF]",
        "label": "Low Fat",
        "sentiment": "negative",
        "categories": ["Dairy"],
        "priority": 9,
    },
    "flavored": {
        "icon": "[FLV]",
        "label": "Flavored",
        "sentiment": "negative",
        "categories": ["Dairy"],
        "priority": 9,
    },
    "different_brand": {
        "icon": "[~]",
        "label": "",  # filled dynamically: "Not Lurpak"
        "sentiment": "info",
        "categories": "all",
        "priority": 6,
    },
}


def get_product_tags(graded_result, purchase_history=None, preferred_brand=None,
                     best_unit_price=None, previous_price=None):
    """
    Generate applicable tags for a graded product.

    Args:
        graded_result: output from grade_product()
        purchase_history: optional dict from product_score.get_purchase_history()
            {"brands": {"Jenan": 8}, "products": {"Jenan Organic...": 5}, "total_orders": 11}
        preferred_brand: optional string -- the household's preferred brand for this item
        best_unit_price: optional float -- lowest unit price among alternatives (for best_value tag)
        previous_price: optional float -- last known price (for price_drop tag)

    Returns:
        list of tag dicts, sorted by priority (most important first):
        [{"id": "organic", "icon": "[ORG]", "label": "Organic", "sentiment": "positive"}, ...]
    """
    attrs = graded_result.get("attributes", {})
    category = attrs.get("category", "Unknown")
    ing = attrs.get("ingredients", {})
    product_name = graded_result.get("product_name", "")
    grade = graded_result.get("grade", "C")
    tags = []

    def _add(tag_id, label_override=None):
        defn = TAG_REGISTRY.get(tag_id)
        if not defn:
            return
        cats = defn["categories"]
        if cats != "all" and category not in cats:
            return
        tags.append({
            "id": tag_id,
            "icon": defn["icon"],
            "label": label_override or defn["label"],
            "sentiment": defn["sentiment"],
            "priority": defn["priority"],
        })

    # ── Bulk (always check first -- trumps everything) ──
    if attrs.get("is_bulk"):
        _add("bulk_pack")
        return tags  # nothing else matters for bulk

    # ── Familiarity (requires purchase history) ──
    if purchase_history and purchase_history.get("total_orders", 0) > 0:
        # Check if this exact product was ordered before
        pname_lower = product_name.lower()
        pname_words = set(w for w in pname_lower.split() if len(w) > 2)

        is_usual = False
        is_prev = False
        for raw_name, count in purchase_history.get("products", {}).items():
            raw_words = set(w for w in raw_name.lower().split() if len(w) > 2)
            overlap = pname_words & raw_words
            if len(overlap) >= 2:
                if count >= 3:
                    is_usual = True
                elif count >= 1:
                    is_prev = True
                break

        if is_usual:
            _add("your_usual")
        elif is_prev:
            _add("prev_ordered")
        else:
            _add("new_find")
    elif purchase_history is not None:
        # History exists but product not found in it
        _add("new_find")

    # ── Grade A ──
    if grade == "A":
        _add("grade_a")

    # ── Quality positives ──
    if attrs.get("is_organic"):
        _add("organic")
    if attrs.get("is_free_range"):
        _add("free_range")
    if attrs.get("is_grass_fed"):
        _add("grass_fed")
    if attrs.get("is_wild_caught"):
        _add("wild_caught")
    if ing.get("has_clean_label"):
        _add("clean_label")

    # Premium origin (only for categories where origin matters)
    if attrs.get("origin_tier") == "premium" and category in ("Meat", "Poultry", "Seafood", "Dairy"):
        _add("premium_origin", f"From {attrs.get('origin_country', '?')}")

    # Pantry specifics
    if category == "Pantry":
        if attrs.get("oil_grade") == "extra_virgin":
            _add("extra_virgin")
        base = attrs.get("pasta_base", "")
        if base == "legume":
            _add("whole_grain", "Legume Pasta")
        elif base == "veggie":
            _add("whole_grain", "Veggie Pasta")
        elif base == "whole_wheat":
            _add("whole_grain", "Whole Wheat")

    # Bread specifics
    if category == "Bread":
        if attrs.get("bread_type") == "sourdough":
            _add("sourdough")
        if attrs.get("has_seeds"):
            _add("whole_grain", "Seeded")

    # ── Value signals ──
    product_price = None  # we'd get this from the product dict
    if best_unit_price is not None:
        product_up = graded_result.get("attributes", {}).get("unit_price")
        if product_up and product_up <= best_unit_price * 1.05:
            _add("best_value")

    if previous_price is not None and product_price is not None:
        if product_price < previous_price * 0.9:
            drop_pct = int((1 - product_price / previous_price) * 100)
            _add("price_drop", f"{drop_pct}% Off")

    # ── Warning signals ──
    proc = attrs.get("processing_level", "raw")
    if proc in ("processed", "breaded", "ready_meal"):
        _add("processed")

    if ing.get("additive_count", 0) >= 3:
        _add("has_additives", f"{ing['additive_count']} Additives")
    if ing.get("filler_count", 0) > 0:
        _add("has_fillers")

    if attrs.get("freshness") == "frozen":
        _add("frozen")

    # Dairy warnings
    if category == "Dairy":
        if attrs.get("fat_content") == "low":
            _add("low_fat")
        if attrs.get("dairy_type") == "yogurt" and not attrs.get("is_plain", True):
            _add("flavored")

    # Brand mismatch
    if preferred_brand:
        product_brand = attrs.get("brand", "") or ""
        # Check product name too since brand field is sometimes empty
        brand_text = f"{product_name} {product_brand}".lower()
        if preferred_brand.lower() not in brand_text:
            _add("different_brand", f"Not {preferred_brand}")

    # Sort by priority (lowest number = most important)
    tags.sort(key=lambda t: t["priority"])

    return tags


def format_tags_inline(tags, max_tags=4):
    """
    Format tags as a compact inline string for terminal or chat display.

    Examples:
        "[*] Your Usual  [ORG] Organic  [GF] Grass Fed"
        "[!!] Processed  [E#] 7 Additives  [FL] Contains Fillers"
    """
    selected = tags[:max_tags]
    parts = []
    for t in selected:
        parts.append(f"{t['icon']} {t['label']}")
    return "  ".join(parts)


# ============================================================
# CLI DEMO
# ============================================================

if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    project_dir = Path(__file__).parent
    json_files = sorted(project_dir.glob("scrape_output_*.json"))

    if not json_files:
        print("No scrape_output_*.json files found. Run scrapers first.")
        sys.exit(1)

    # Filter by argument if given
    if len(sys.argv) > 1:
        target = sys.argv[1].lower()
        json_files = [f for f in json_files if target in f.name.lower()]

    print("=" * 70)
    print("  PRODUCT GRADING ENGINE")
    print("=" * 70)

    total_graded = 0
    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}

    for jf in json_files:
        try:
            products = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"\n  ERROR loading {jf.name}: {e}")
            continue

        if not products:
            continue

        graded = grade_products(products)
        total_graded += len(graded)

        # Count grades
        for r in graded:
            grade_counts[r["grade"]] = grade_counts.get(r["grade"], 0) + 1

        print(f"\n{'=' * 70}")
        store = "Kibsons" if "kibsons" in jf.name else "Carrefour"
        item = jf.stem.replace("scrape_output_", "").replace("kibsons_", "").replace("carrefour_", "")
        print(f"  {store} / {item}  ({len(graded)} products)")
        print(f"{'=' * 70}")

        # Show top 3 (best)
        shown = set()
        best = [r for r in graded if r["grade"] in ("A", "B")][:3]
        if best:
            print(f"\n  -- BEST --")
        for r in best:
            shown.add(r["product_name"])
            orig = next((p for p in products if p.get("name") == r["product_name"]), {})
            price = orig.get("price", "?")
            origin = orig.get("origin_country", "?")
            tags = get_product_tags(r)
            tag_str = format_tags_inline(tags) if tags else ""
            print(f"\n  [{r['grade']}] {r['points']}pts  {r['product_name'][:58]}")
            print(f"      {price} AED | {origin}")
            if tag_str:
                print(f"      Tags: {tag_str}")
            for reason in r["reasons"]:
                print(f"      . {reason}")

        # Show bottom 3 (worst)
        worst = [r for r in reversed(graded) if r["grade"] in ("D", "F")
                 and r["product_name"] not in shown][:3]
        if worst:
            print(f"\n  -- WORST --")
        for r in worst:
            shown.add(r["product_name"])
            orig = next((p for p in products if p.get("name") == r["product_name"]), {})
            price = orig.get("price", "?")
            origin = orig.get("origin_country", "?")
            tags = get_product_tags(r)
            tag_str = format_tags_inline(tags) if tags else ""
            print(f"\n  [{r['grade']}] {r['points']}pts  {r['product_name'][:58]}")
            print(f"      {price} AED | {origin}")
            if tag_str:
                print(f"      Tags: {tag_str}")
            for reason in r["reasons"]:
                print(f"      . {reason}")

        # Grade distribution for this file
        dist = {}
        for r in graded:
            dist[r["grade"]] = dist.get(r["grade"], 0) + 1
        dist_str = "  ".join(f"{g}:{dist.get(g, 0)}" for g in ["A", "B", "C", "D", "F"])
        print(f"\n  Distribution: {dist_str}")

    # Overall summary
    print(f"\n{'=' * 70}")
    print(f"  OVERALL: {total_graded} products graded across {len(json_files)} files")
    dist_str = "  ".join(f"{g}: {grade_counts.get(g, 0)}" for g in ["A", "B", "C", "D", "F"])
    print(f"  {dist_str}")
    print(f"{'=' * 70}")
