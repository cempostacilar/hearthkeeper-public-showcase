"""
Catalog Intelligence — Product Knowledge Builder

Analyzes the full product_catalog to build:
1. Product type index: canonical product types per category
2. Synonym groups: maps consumer terms → catalog search terms
3. Category clusters: which products live together

This module is the "Cataloger" — it reads the raw catalog that the
Gatherer (scrapers) collected and builds structured knowledge that the
Concierge (Store Manager agent) uses to find products.

Usage:
    python3 catalog_intelligence.py          # full rebuild
    python3 catalog_intelligence.py --stats  # show current knowledge stats
"""

import sqlite3
import re
import os
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "autostock.db")

log = logging.getLogger("catalog_intelligence")


# ────────────────────────────────────────────
# DB Setup
# ────────────────────────────────────────────

def ensure_tables(conn):
    """Create the knowledge tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS product_synonyms (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_term  TEXT NOT NULL,
            synonym         TEXT NOT NULL,
            category        TEXT,
            source          TEXT DEFAULT 'catalog_analysis',
            confidence      REAL DEFAULT 1.0,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(canonical_term, synonym)
        );

        CREATE TABLE IF NOT EXISTS product_type_index (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            product_type    TEXT NOT NULL,
            category        TEXT,
            subcategory     TEXT,
            sample_names    TEXT,
            store_ids       TEXT,
            product_count   INTEGER DEFAULT 0,
            search_terms    TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(product_type, category)
        );

        CREATE INDEX IF NOT EXISTS idx_synonym_lookup
            ON product_synonyms(synonym);
        CREATE INDEX IF NOT EXISTS idx_synonym_canonical
            ON product_synonyms(canonical_term);
        CREATE INDEX IF NOT EXISTS idx_product_type
            ON product_type_index(product_type);
    """)


# ────────────────────────────────────────────
# Text Normalization
# ────────────────────────────────────────────

# Common brand names to strip from product names for type extraction
_BRAND_STRIP = {
    "kibsons", "carrefour", "al ain", "al rawabi", "al marai", "almarai",
    "nadec", "nido", "arla", "lurpak", "president", "anchor", "elle & vire",
    "sainsbury's", "m&s", "marks & spencer", "tesco", "waitrose",
    "heinz", "kellogg's", "nestle", "maggi", "knorr", "lipton",
    "al fakher", "goody", "americana", "sadia", "al islami", "al kabeer",
}

# Size/quantity patterns to strip
_SIZE_PATTERN = re.compile(
    r'\s*[-–]?\s*\d+[\d.,]*\s*'
    r'(g|kg|mg|ml|l|cl|oz|fl\.?\s*oz|pcs|pieces?|pack|ct|mm|cm|m|lb|lbs'
    r'|sheets?|rolls?|capsules?|tablets?|sachets?)\b.*$',
    re.IGNORECASE
)
_MULTI_PATTERN = re.compile(r'\s*\d+\s*x\s*\d+.*$', re.IGNORECASE)
_PACK_PATTERN = re.compile(r'\s*,?\s*pack of \d+.*$', re.IGNORECASE)


def normalize_product_name(name, brand=None):
    """Strip brand, size, quantity from product name → core product type."""
    n = name.lower().strip()

    # Strip known brand prefixes
    if brand:
        bl = brand.lower().strip()
        if n.startswith(bl):
            n = n[len(bl):].strip()
            if n.startswith("- ") or n.startswith("– "):
                n = n[2:].strip()

    for b in _BRAND_STRIP:
        if n.startswith(b + " "):
            n = n[len(b):].strip()
            if n.startswith("- ") or n.startswith("– "):
                n = n[2:].strip()
            break

    # Strip size/quantity suffixes
    n = _SIZE_PATTERN.sub('', n)
    n = _MULTI_PATTERN.sub('', n)
    n = _PACK_PATTERN.sub('', n)

    # Clean trailing punctuation and whitespace
    n = re.sub(r'[,;:\-–]+$', '', n).strip()

    return n if len(n) > 1 else name.lower().strip()


def extract_leaf_category(category_str):
    """Extract the most specific (leaf) category from a hierarchy string."""
    if not category_str:
        return None, None
    # Handle both " > " (Kibsons) and "/" (Carrefour) separators
    parts = re.split(r'\s*[>/]\s*', category_str)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[-1]  # (top, leaf)
    return parts[0] if parts else None, parts[0] if parts else None


# ────────────────────────────────────────────
# Phase 1: Extract Product Types from Catalog
# ────────────────────────────────────────────

def extract_product_types(conn):
    """
    Analyze all products in catalog and extract canonical product types.
    Groups products by their normalized name + category.

    Returns dict: {(product_type, top_category): {
        "count": N, "subcategory": str, "sample_names": [str],
        "store_ids": set, "search_terms": [str]
    }}
    """
    rows = conn.execute("""
        SELECT product_name, category, brand, store_id
        FROM product_catalog
        WHERE is_available = 1
    """).fetchall()

    type_map = defaultdict(lambda: {
        "count": 0, "subcategory": None, "sample_names": [],
        "store_ids": set(), "search_terms": set()
    })

    for name, cat, brand, store_id in rows:
        norm = normalize_product_name(name, brand)
        top_cat, leaf_cat = extract_leaf_category(cat)

        key = (norm, top_cat or "Uncategorized")
        entry = type_map[key]
        entry["count"] += 1
        entry["subcategory"] = leaf_cat
        if len(entry["sample_names"]) < 5:
            entry["sample_names"].append(name)
        entry["store_ids"].add(store_id)

        # Build search terms from the normalized name
        words = [w for w in norm.split() if len(w) >= 2]
        for w in words:
            entry["search_terms"].add(w)

    log.info(f"Extracted {len(type_map)} product types from {len(rows)} products")
    return type_map


def save_product_types(type_map, conn):
    """Save extracted product types to product_type_index table."""
    conn.execute("DELETE FROM product_type_index")

    batch = []
    for (ptype, cat), info in type_map.items():
        if info["count"] < 2:
            continue  # Skip ultra-rare types (likely parsing noise)
        batch.append((
            ptype,
            cat,
            info["subcategory"],
            json.dumps(info["sample_names"][:5]),
            json.dumps(sorted(info["store_ids"])),
            info["count"],
            json.dumps(sorted(info["search_terms"])),
        ))

    conn.executemany("""
        INSERT OR REPLACE INTO product_type_index
            (product_type, category, subcategory, sample_names,
             store_ids, product_count, search_terms)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, batch)
    conn.commit()
    log.info(f"Saved {len(batch)} product types to index")
    return len(batch)


# ────────────────────────────────────────────
# Phase 2: Build Synonym Groups
# ────────────────────────────────────────────

# Known consumer-term → catalog-term mappings
# These are pairs where users commonly use one term but the catalog
# uses a different term for the same product.
KNOWN_SYNONYMS = {
    # Personal care
    "cotton swabs": ["cotton pads", "cotton buds", "cotton rounds"],
    "cotton buds": ["cotton pads", "cotton swabs"],
    "cotton pads": ["cotton swabs", "cotton buds", "cotton rounds"],
    "q-tips": ["cotton buds", "cotton swabs", "cotton pads"],
    "tissues": ["facial tissues", "tissue paper", "tissue box"],
    "toilet paper": ["toilet rolls", "bathroom tissue"],
    "kitchen roll": ["kitchen towel", "paper towel"],
    "paper towel": ["kitchen roll", "kitchen towel"],
    "hand soap": ["liquid soap", "hand wash"],
    "hand wash": ["liquid soap", "hand soap"],
    "body wash": ["shower gel"],
    "shower gel": ["body wash"],
    "shampoo": ["hair shampoo"],
    "toothpaste": ["dental cream"],
    "mouthwash": ["mouth rinse", "oral rinse"],
    "deodorant": ["antiperspirant", "body spray"],
    "sunscreen": ["sunblock", "sun cream", "sun lotion"],
    "lip balm": ["lip care", "chapstick"],
    "moisturizer": ["moisturiser", "body lotion", "face cream"],
    "face wash": ["facial cleanser", "face cleanser"],
    "makeup remover": ["micellar water", "cleansing water"],
    "sanitary pads": ["feminine pads", "sanitary napkins", "menstrual pads"],
    "tampons": ["sanitary tampons"],
    "wet wipes": ["baby wipes", "cleansing wipes"],
    "baby wipes": ["wet wipes"],
    "nappies": ["diapers", "baby diapers"],
    "diapers": ["nappies", "baby diapers"],

    # Cleaning
    "dish soap": ["dishwashing liquid", "dish detergent"],
    "dishwashing liquid": ["dish soap", "dish detergent"],
    "laundry detergent": ["washing powder", "laundry powder", "fabric wash"],
    "washing powder": ["laundry detergent", "laundry powder"],
    "fabric softener": ["fabric conditioner"],
    "bleach": ["chlorine bleach", "household bleach"],
    "trash bags": ["garbage bags", "bin bags", "bin liners"],
    "garbage bags": ["trash bags", "bin bags", "bin liners"],
    "bin bags": ["trash bags", "garbage bags", "bin liners"],
    "sponge": ["cleaning sponge", "scrub sponge", "dish sponge"],
    "cling wrap": ["cling film", "plastic wrap", "food wrap"],
    "plastic wrap": ["cling film", "cling wrap", "food wrap"],
    "aluminum foil": ["aluminium foil", "tin foil", "foil wrap"],
    "tin foil": ["aluminum foil", "aluminium foil", "foil wrap"],
    "zip bags": ["ziplock bags", "resealable bags", "storage bags"],
    "ziplock bags": ["zip bags", "resealable bags", "storage bags"],

    # Grocery (extending existing GROCERY_SYNONYMS)
    "minced meat": ["beef mince", "mince", "ground beef"],
    "ground beef": ["beef mince", "minced meat", "mince meat"],
    "capsicum": ["bell pepper", "pepper"],
    "bell pepper": ["capsicum"],
    "coriander": ["cilantro", "fresh coriander"],
    "cilantro": ["coriander", "fresh coriander"],
    "courgette": ["zucchini"],
    "zucchini": ["courgette"],
    "aubergine": ["eggplant"],
    "eggplant": ["aubergine"],
    "prawns": ["shrimp"],
    "shrimp": ["prawns"],
    "rocket": ["arugula", "rucola"],
    "arugula": ["rocket", "rucola"],
    "spring onion": ["green onion", "scallion"],
    "scallion": ["spring onion", "green onion"],
    "sweetcorn": ["sweet corn", "corn on the cob"],
    "sweet corn": ["sweetcorn", "corn on the cob"],
    "lady finger": ["okra", "bhindi"],
    "okra": ["lady finger", "bhindi"],
    "chickpeas": ["garbanzo beans", "chana"],
    "lentils": ["daal", "dal", "masoor"],
    "greek yogurt": ["greek yoghurt", "strained yogurt"],
    "yoghurt": ["yogurt"],
    "yogurt": ["yoghurt"],
    "porridge": ["oatmeal", "porridge oats", "rolled oats"],
    "oatmeal": ["porridge", "porridge oats"],
    "jam": ["preserve", "fruit spread", "marmalade"],
    "ketchup": ["tomato ketchup", "tomato sauce"],
    "mayo": ["mayonnaise"],
    "mayonnaise": ["mayo"],
    "soy sauce": ["soya sauce"],
    "corn flour": ["cornstarch", "corn starch"],
    "cornstarch": ["corn flour", "corn starch"],
    "baking soda": ["bicarbonate of soda", "bicarb"],
    "sparkling water": ["soda water", "carbonated water", "fizzy water"],
    "soda water": ["sparkling water", "carbonated water"],

    # Generic category terms → specific items
    "meat": ["beef mince", "beef", "chicken", "lamb", "steak", "striploin"],
    "fish": ["salmon", "seabream", "cod", "sea bass", "tuna", "tilapia"],
    "seafood": ["salmon", "shrimp", "prawns", "mussels", "seabream", "fish"],
    "greens": ["spinach", "rocket", "lettuce", "kale", "arugula"],
    "fruit": ["apple", "banana", "mango", "strawberry", "avocado", "grape"],
    "fruits": ["apple", "banana", "mango", "strawberry", "avocado", "grape"],
    "dairy": ["milk", "yogurt", "cheese", "butter", "cream", "kefir"],
    "vegetables": ["potato", "carrot", "tomato", "onion", "cucumber", "pepper"],
    "veggies": ["potato", "carrot", "tomato", "onion", "cucumber", "pepper"],
    "snacks": ["chips", "crisps", "crackers", "nuts", "biscuits"],
    "drinks": ["juice", "water", "soda", "tea", "coffee"],
    "cleaning supplies": ["dish soap", "laundry detergent", "bleach", "sponge"],
}


def build_synonym_index(conn):
    """
    Build the product_synonyms table from:
    1. KNOWN_SYNONYMS (curated mappings)
    2. Catalog category analysis (products in same leaf category)
    """
    ensure_tables(conn)
    conn.execute("DELETE FROM product_synonyms")

    # Phase 2a: Insert known synonyms
    known_count = 0
    for term, synonyms in KNOWN_SYNONYMS.items():
        for syn in synonyms:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO product_synonyms
                        (canonical_term, synonym, source, confidence)
                    VALUES (?, ?, 'curated', 1.0)
                """, (term, syn))
                known_count += 1
            except sqlite3.IntegrityError:
                pass

    # Phase 2b: Category-based synonyms
    # Products in the same leaf category with different names are related.
    # E.g., "cotton pads" and "cotton rounds" in "Cotton & Hygiene" category.
    cat_count = _build_category_synonyms(conn)

    conn.commit()
    total = known_count + cat_count
    log.info(f"Built synonym index: {known_count} curated + {cat_count} category-derived = {total} total")
    return total


def _build_category_synonyms(conn):
    """
    Analyze products by leaf category to find related product types.
    Products in the same subcategory are related.
    """
    rows = conn.execute("""
        SELECT product_name, category, brand, store_id
        FROM product_catalog
        WHERE is_available = 1 AND category IS NOT NULL AND category != ''
    """).fetchall()

    # Group normalized names by leaf category
    cat_products = defaultdict(Counter)
    for name, cat, brand, _store in rows:
        norm = normalize_product_name(name, brand)
        _, leaf = extract_leaf_category(cat)
        if leaf and len(norm) > 2:
            cat_products[leaf][norm] += 1

    # For each category, create cross-references between product types
    count = 0
    for leaf_cat, types in cat_products.items():
        # Only process categories with multiple distinct types
        top_types = [t for t, c in types.most_common(10) if c >= 2]
        if len(top_types) < 2:
            continue

        # Each top type in this category gets cross-referenced with others
        for i, t1 in enumerate(top_types):
            for t2 in top_types[i+1:]:
                # Only create synonyms if names share at least one word
                # (avoids "apple" ↔ "banana" just because they're both fruits)
                words1 = set(t1.split())
                words2 = set(t2.split())
                shared = words1 & words2
                if shared and any(len(w) >= 3 for w in shared):
                    try:
                        conn.execute("""
                            INSERT OR IGNORE INTO product_synonyms
                                (canonical_term, synonym, category, source, confidence)
                            VALUES (?, ?, ?, 'category_analysis', 0.7)
                        """, (t1, t2, leaf_cat))
                        conn.execute("""
                            INSERT OR IGNORE INTO product_synonyms
                                (canonical_term, synonym, category, source, confidence)
                            VALUES (?, ?, ?, 'category_analysis', 0.7)
                        """, (t2, t1, leaf_cat))
                        count += 2
                    except sqlite3.IntegrityError:
                        pass

    return count


# ────────────────────────────────────────────
# Phase 3: Search Enhancement API
# ────────────────────────────────────────────

def expand_search(query, conn=None):
    """
    Expand a search query using the synonym index.

    Returns list of search terms, original first, then synonyms
    sorted by confidence.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    terms = [query.strip()]
    key = query.lower().strip()

    # Check direct synonym matches
    rows = conn.execute("""
        SELECT canonical_term, confidence
        FROM product_synonyms
        WHERE LOWER(synonym) = ?
        ORDER BY confidence DESC
        LIMIT 10
    """, (key,)).fetchall()

    for r in rows:
        term = r["canonical_term"]
        if term.lower() not in [t.lower() for t in terms]:
            terms.append(term)

    # Also check if the query IS a canonical term → get its synonyms
    rows2 = conn.execute("""
        SELECT synonym, confidence
        FROM product_synonyms
        WHERE LOWER(canonical_term) = ?
        ORDER BY confidence DESC
        LIMIT 10
    """, (key,)).fetchall()

    for r in rows2:
        term = r["synonym"]
        if term.lower() not in [t.lower() for t in terms]:
            terms.append(term)

    # Check product_type_index for matching product types
    rows3 = conn.execute("""
        SELECT product_type, category, product_count
        FROM product_type_index
        WHERE LOWER(product_type) LIKE ?
        ORDER BY product_count DESC
        LIMIT 5
    """, (f"%{key}%",)).fetchall()

    for r in rows3:
        term = r["product_type"]
        if term.lower() not in [t.lower() for t in terms]:
            terms.append(term)

    if close_conn:
        conn.close()

    return terms


def get_category_for_term(query, conn=None):
    """
    Look up the category for a search term using the product type index.
    Returns (category, subcategory) or (None, None).
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    key = query.lower().strip()

    # Exact match first
    row = conn.execute("""
        SELECT category, subcategory
        FROM product_type_index
        WHERE LOWER(product_type) = ?
        ORDER BY product_count DESC
        LIMIT 1
    """, (key,)).fetchone()

    if not row:
        # Fuzzy match
        row = conn.execute("""
            SELECT category, subcategory
            FROM product_type_index
            WHERE LOWER(product_type) LIKE ?
            ORDER BY product_count DESC
            LIMIT 1
        """, (f"%{key}%",)).fetchone()

    if not row:
        # Check synonym → canonical → category
        syn_row = conn.execute("""
            SELECT ps.canonical_term, pti.category, pti.subcategory
            FROM product_synonyms ps
            LEFT JOIN product_type_index pti
                ON LOWER(pti.product_type) = LOWER(ps.canonical_term)
            WHERE LOWER(ps.synonym) = ?
            AND pti.category IS NOT NULL
            LIMIT 1
        """, (key,)).fetchone()
        if syn_row:
            row = syn_row

    result = (row["category"], row["subcategory"]) if row else (None, None)

    if close_conn:
        conn.close()

    return result


# ────────────────────────────────────────────
# Full Rebuild Pipeline
# ────────────────────────────────────────────

def rebuild_knowledge(conn=None):
    """
    Full knowledge rebuild pipeline:
    1. Ensure tables exist
    2. Extract product types from catalog
    3. Save product type index
    4. Build synonym groups
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    log.info("Starting catalog intelligence rebuild...")
    start = datetime.now()

    ensure_tables(conn)

    # Phase 1: Extract product types
    log.info("Phase 1: Extracting product types...")
    type_map = extract_product_types(conn)
    type_count = save_product_types(type_map, conn)

    # Phase 2: Build synonyms
    log.info("Phase 2: Building synonym index...")
    syn_count = build_synonym_index(conn)

    elapsed = (datetime.now() - start).total_seconds()
    log.info(f"Knowledge rebuild complete in {elapsed:.1f}s: "
             f"{type_count} product types, {syn_count} synonyms")

    if close_conn:
        conn.close()

    return {
        "product_types": type_count,
        "synonyms": syn_count,
        "elapsed_seconds": elapsed,
    }


def knowledge_stats(conn=None):
    """Get current knowledge base statistics."""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    ensure_tables(conn)

    stats = {}
    stats["product_types"] = conn.execute(
        "SELECT COUNT(*) as c FROM product_type_index").fetchone()["c"]
    stats["synonyms"] = conn.execute(
        "SELECT COUNT(*) as c FROM product_synonyms").fetchone()["c"]
    stats["curated_synonyms"] = conn.execute(
        "SELECT COUNT(*) as c FROM product_synonyms WHERE source = 'curated'").fetchone()["c"]
    stats["category_synonyms"] = conn.execute(
        "SELECT COUNT(*) as c FROM product_synonyms WHERE source = 'category_analysis'").fetchone()["c"]
    stats["catalog_products"] = conn.execute(
        "SELECT COUNT(*) as c FROM product_catalog WHERE is_available = 1").fetchone()["c"]

    # Top categories
    cats = conn.execute("""
        SELECT category, SUM(product_count) as total
        FROM product_type_index
        GROUP BY category
        ORDER BY total DESC
        LIMIT 15
    """).fetchall()
    stats["top_categories"] = [(r["category"], r["total"]) for r in cats]

    if close_conn:
        conn.close()

    return stats


# ────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Catalog Intelligence Builder")
    parser.add_argument("--stats", action="store_true",
                        help="Show current knowledge stats")
    parser.add_argument("--test", type=str,
                        help="Test synonym expansion for a term")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.stats:
        stats = knowledge_stats(conn)
        print("=" * 50)
        print("  CATALOG INTELLIGENCE STATUS")
        print("=" * 50)
        print(f"  Catalog products:    {stats['catalog_products']:,}")
        print(f"  Product types:       {stats['product_types']:,}")
        print(f"  Synonyms total:      {stats['synonyms']:,}")
        print(f"    Curated:           {stats['curated_synonyms']:,}")
        print(f"    Category-derived:  {stats['category_synonyms']:,}")
        print()
        print("  Top categories:")
        for cat, count in stats["top_categories"]:
            print(f"    {cat:30s} {count:>5} types")
        print("=" * 50)

    elif args.test:
        ensure_tables(conn)
        terms = expand_search(args.test, conn)
        cat, subcat = get_category_for_term(args.test, conn)
        print(f"Query: '{args.test}'")
        print(f"Category: {cat} > {subcat}" if cat else "Category: unknown")
        print(f"Expanded terms ({len(terms)}):")
        for t in terms:
            marker = " ←" if t.lower() == args.test.lower() else ""
            print(f"  - {t}{marker}")

    else:
        result = rebuild_knowledge(conn)
        print("=" * 50)
        print("  CATALOG INTELLIGENCE REBUILD COMPLETE")
        print("=" * 50)
        print(f"  Product types indexed: {result['product_types']:,}")
        print(f"  Synonyms built:       {result['synonyms']:,}")
        print(f"  Time:                 {result['elapsed_seconds']:.1f}s")
        print("=" * 50)

        # Quick validation
        print("\n  Validation tests:")
        for test_term in ["cotton swabs", "meat", "greens", "shrimp",
                          "dish soap", "diapers", "milk", "rocket"]:
            terms = expand_search(test_term, conn)
            cat, _ = get_category_for_term(test_term, conn)
            cat_label = f" [{cat}]" if cat else ""
            print(f"    '{test_term}' -> {len(terms)} terms{cat_label}: "
                  f"{', '.join(terms[:4])}{'...' if len(terms) > 4 else ''}")

    conn.close()
