"""
AutoRestock - Order History Enrichment

Makes order history usable as the strongest scoring signal by:
  1. Extracting brands from raw_name (matching against known catalog brands)
  2. Linking order_items -> items table (fuzzy name matching)
  3. Adding enriched_at timestamp to avoid re-processing

Before enrichment: brand 4.2% populated, item_id 8.9% linked.
Target: brand ~80%+, item_id ~70%+.

Usage:
  python3 enrich_order_history.py           # enrich all un-enriched order items
  python3 enrich_order_history.py --force   # re-enrich everything
  python3 enrich_order_history.py --dry-run # show what would change

Hook into order_scraper.py:
  from enrich_order_history import enrich_new_order_items
  enrich_new_order_items(order_id, conn)
"""

import re
import sqlite3
import sys
from datetime import datetime

from db_setup import DB_PATH


# ============================================================
# SCHEMA MIGRATION
# ============================================================
# Add enriched_at column if it doesn't exist

def _ensure_schema(conn):
    """Add enriched_at column to order_items if missing."""
    cols = conn.execute("PRAGMA table_info(order_items)").fetchall()
    col_names = [c[1] for c in cols]

    if "enriched_at" not in col_names:
        conn.execute("ALTER TABLE order_items ADD COLUMN enriched_at TEXT")
        print("  [SCHEMA] Added 'enriched_at' column to order_items")
        conn.commit()


# ============================================================
# BRAND EXTRACTION
# ============================================================

_brand_cache = None


def _load_known_brands(conn):
    """Load all known brands from product_catalog, sorted by frequency."""
    global _brand_cache
    if _brand_cache is not None:
        return _brand_cache

    rows = conn.execute("""
        SELECT brand, COUNT(*) as cnt
        FROM product_catalog
        WHERE brand IS NOT NULL AND brand != '' AND brand != 'Carrefour'
        GROUP BY brand
        ORDER BY cnt DESC
    """).fetchall()

    # Also include brands from order_items that are already populated
    existing = conn.execute("""
        SELECT DISTINCT brand FROM order_items
        WHERE brand IS NOT NULL AND brand != ''
    """).fetchall()

    # Words that are common descriptors, NOT brands — even if the catalog
    # has products with these as "brand" due to scraping artifacts.
    NOT_BRANDS = {
        # Colors / descriptors
        "red", "green", "blue", "white", "brown", "yellow", "orange", "black",
        "golden", "silver",
        # Quality / type words
        "organic", "natural", "fresh", "frozen", "premium", "original",
        "classic", "mini", "jumbo", "large", "small", "baby", "raw",
        "wild", "free range", "grass fed",
        # Origin words / countries that look like brands
        "australian", "new zealand", "italian", "french", "spanish",
        "australian wagyu", "wagyu", "angus",
        "italy", "ecuador", "kenya", "uk", "uae", "brazil", "norway",
        "ireland", "denmark", "greece", "spain", "turkey", "egypt",
        "south africa", "usa", "germany", "german",
        # Generic category words
        "best", "royal", "mega", "super", "generic",
        "royal gala",  # apple variety, not a brand
        "organics red capsicums",  # scraped artifact
    }

    brands = {}
    for r in rows:
        name = r[0].strip()
        if len(name) >= 2 and name.lower() not in NOT_BRANDS:
            brands[name.lower()] = name  # lowercase -> proper case

    for r in existing:
        name = r[0].strip()
        if len(name) >= 2 and name.lower() not in NOT_BRANDS and name.lower() not in brands:
            brands[name.lower()] = name

    _brand_cache = brands
    return brands


def extract_brand(raw_name, known_brands):
    """
    Extract brand from a raw order item name.

    Strategy:
      1. Exact match: "Al Ain Fresh Milk" -> check if "Al Ain" is a known brand
      2. Two-word prefix: "Lurpak Unsalted Butter" -> "Lurpak" is a brand
      3. One-word prefix: "Barilla Spaghetti" -> "Barilla" is a brand
      4. Anywhere match: check if any known brand appears anywhere in the name

    Returns: (brand_proper_case, match_type) or (None, None)
    """
    name = raw_name.strip()
    name_lower = name.lower()

    if not name:
        return None, None

    words = name.split()

    # Strategy 1+2: Check 3-word, 2-word, and 1-word prefixes
    for n_words in [3, 2, 1]:
        if len(words) >= n_words + 1:  # need at least one more word after brand
            prefix = " ".join(words[:n_words]).lower()
            # Clean common store/generic prefixes
            if prefix in ("organic", "fresh", "frozen", "raw", "premium",
                          "natural", "free range", "wild", "local", "imported"):
                continue
            if prefix in known_brands:
                return known_brands[prefix], f"prefix_{n_words}"

    # Strategy 3: Anywhere match (longer brands first to avoid partial matches)
    sorted_brands = sorted(known_brands.keys(), key=len, reverse=True)
    for brand_lower in sorted_brands:
        if len(brand_lower) < 3:
            continue  # skip 1-2 char brands
        # Word boundary match to avoid "Al" matching inside "Also"
        if re.search(r'\b' + re.escape(brand_lower) + r'\b', name_lower):
            return known_brands[brand_lower], "anywhere"

    return None, None


# ============================================================
# ITEM LINKING
# ============================================================

_items_cache = None


def _load_items(conn):
    """Load all items from the items table."""
    global _items_cache
    if _items_cache is not None:
        return _items_cache

    rows = conn.execute("SELECT id, name FROM items").fetchall()
    _items_cache = [(r[0], r[1], r[1].lower(), set(w.lower() for w in r[1].split() if len(w) > 2))
                    for r in rows]
    return _items_cache


def _normalize_name(raw_name):
    """
    Normalize a raw order item name for matching.

    Strips brand, size, quantity info to get core product noun.
    "Al Ain Fresh Milk Full Cream 1L" -> "fresh milk full cream"
    "Organic Free Range Eggs 6 Pack" -> "organic free range eggs"
    """
    name = raw_name.lower().strip()

    # Remove size/quantity patterns
    name = re.sub(r'\d+\s*(g|kg|ml|l|pcs|pck|pack|x\d+)\b', '', name, flags=re.I)
    name = re.sub(r'\b\d+\s*$', '', name)  # trailing numbers
    name = re.sub(r'\b(approx|approximately|about)\b', '', name)

    # Remove common non-informative words
    name = re.sub(r'\b(the|a|an|of|with|and|in|for|per|each)\b', '', name)

    # Clean up whitespace
    name = re.sub(r'\s+', ' ', name).strip()

    return name


def link_to_item(raw_name, items):
    """
    Match an order_items.raw_name to an item in the items table.

    Strategy:
      1. Multi-word item name found in raw_name (high confidence)
      2. All item words appear in the raw_name
      3. Single-word item name: must be the CORE noun (not part of a compound word)

    Returns: (item_id, match_type, confidence) or (None, None, 0)
    """
    raw_lower = raw_name.lower()
    raw_words = set(w for w in raw_lower.split() if len(w) > 2)

    # Compound phrases where a single item word is misleading
    # "Apple Cider Vinegar" -> NOT apple, "Butter Biscuits" -> NOT butter
    COMPOUND_EXCEPTIONS = {
        "apple": ["vinegar", "cider", "sauce", "juice", "pie", "crumble"],
        "butter": ["biscuit", "cookie", "cake", "croissant", "pastry", "peanut"],
        "bread": ["crumb", "stick", "bite", "chip"],
        "milk": ["chocolate", "shake", "powder"],
        "cream": ["ice cream", "sun cream", "biscuit"],
        "cheese": ["cake", "cracker"],
        "rice": ["paper", "noodle", "cake", "cracker"],
    }

    best_match = None
    best_score = 0

    for item_id, item_name, item_lower, item_words in items:
        # --- Multi-word item names: substring match is reliable ---
        if len(item_words) >= 2:
            if item_lower in raw_lower:
                return item_id, "substring", 0.95

            # All item words appear in the raw_name
            if item_words.issubset(raw_words):
                score = len(item_words) / max(len(raw_words), 1)
                score = min(score + 0.3, 0.95)  # boost multi-word matches
                if score > best_score:
                    best_score = score
                    best_match = (item_id, "all_words", score)

        # --- Single-word item names: need extra caution ---
        elif len(item_words) == 1:
            item_word = list(item_words)[0]

            # Check if it's in a compound exception
            is_compound = False
            exceptions = COMPOUND_EXCEPTIONS.get(item_word, [])
            for exc in exceptions:
                if exc in raw_lower:
                    is_compound = True
                    break

            if is_compound:
                continue  # Skip: "apple cider vinegar" is not "apple"

            # For single-word items, require the word to be the core noun:
            # either it's the main word (> half the raw_name is this word)
            # or it appears as a standalone word (including plural & spelling forms)
            # Spelling variants: "yogurt" matches "yoghurt", etc.
            SPELLING_VARIANTS = {
                "yogurt": ["yoghurt"],
                "yoghurt": ["yogurt"],
            }
            check_words = [item_word] + SPELLING_VARIANTS.get(item_word, [])

            matched_variant = False
            for check_word in check_words:
                if check_word in raw_lower:
                    # Check it's not part of a larger word — allow plural forms
                    # "banana" matches "bananas", "apple" matches "apples",
                    # "yogurt" matches "yoghurt", "cucumber" matches "cucumbers"
                    pattern = r'\b' + re.escape(check_word) + r'(?:e?s)?\b'
                    if re.search(pattern, raw_lower):
                        matched_variant = True
                        break

            if matched_variant:
                # Simple heuristic: if the raw_name is short (1-4 words),
                # the item word is likely the core noun
                if len(raw_words) <= 4:
                    score = 0.8
                else:
                    score = 0.6  # longer names, less confident
                if score > best_score:
                    best_score = score
                    best_match = (item_id, "core_noun", score)

        # Token overlap (for multi-word items)
        if item_words and raw_words and len(item_words) >= 2:
            overlap = item_words & raw_words
            item_coverage = len(overlap) / len(item_words)
            if item_coverage >= 0.5 and len(overlap) >= 2:
                if item_coverage > best_score:
                    best_score = item_coverage
                    best_match = (item_id, "token_overlap", item_coverage)

    if best_match and best_score >= 0.5:
        return best_match

    return None, None, 0


# ============================================================
# MAIN ENRICHMENT
# ============================================================

def enrich_order_items(conn, force=False, dry_run=False):
    """
    Enrich all order_items that haven't been enriched yet.

    Args:
        conn: SQLite connection
        force: re-enrich even previously enriched items
        dry_run: show changes without writing

    Returns dict with stats.
    """
    _ensure_schema(conn)

    known_brands = _load_known_brands(conn)
    items = _load_items(conn)

    print(f"  Loaded {len(known_brands)} known brands, {len(items)} items")

    # Select order items to enrich
    if force:
        rows = conn.execute("""
            SELECT oi.id, oi.raw_name, oi.brand, oi.item_id, oi.order_id,
                   o.store_id
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
            ORDER BY oi.id
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT oi.id, oi.raw_name, oi.brand, oi.item_id, oi.order_id,
                   o.store_id
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
            WHERE oi.enriched_at IS NULL
            ORDER BY oi.id
        """).fetchall()

    if not rows:
        print("  No order items to enrich.")
        return {"total": 0, "brands_added": 0, "items_linked": 0}

    print(f"  Enriching {len(rows)} order items...")

    stats = {
        "total": len(rows),
        "brands_added": 0,
        "items_linked": 0,
        "already_had_brand": 0,
        "already_had_item": 0,
    }

    updates = []
    now = datetime.now().isoformat()

    for row in rows:
        oi_id = row[0]
        raw_name = row[1]
        current_brand = row[2]
        current_item_id = row[3]

        new_brand = current_brand
        new_item_id = current_item_id

        # -- Brand extraction --
        if force or not current_brand or current_brand.strip() == "":
            brand, match_type = extract_brand(raw_name, known_brands)
            if brand:
                new_brand = brand
                if not current_brand or current_brand.strip() == "":
                    stats["brands_added"] += 1
                if dry_run:
                    print(f"    BRAND: '{raw_name[:50]}' -> '{brand}' ({match_type})")
            elif force:
                # Force mode: clear false-positive brands that no longer match
                new_brand = None
        else:
            stats["already_had_brand"] += 1

        # -- Item linking --
        if force or current_item_id is None:
            item_id, match_type, confidence = link_to_item(raw_name, items)
            if item_id is not None:
                new_item_id = item_id
                if current_item_id is None:
                    stats["items_linked"] += 1
                if dry_run:
                    item_name = next((name for iid, name, _, _ in items if iid == item_id), "?")
                    print(f"    ITEM:  '{raw_name[:50]}' -> [{item_id}] {item_name} ({match_type}, {confidence:.0%})")
            elif force:
                new_item_id = None  # clear if no longer matches
        else:
            stats["already_had_item"] += 1

        updates.append((new_brand, new_item_id, now, oi_id))

    if not dry_run:
        conn.executemany("""
            UPDATE order_items
            SET brand = ?, item_id = ?, enriched_at = ?
            WHERE id = ?
        """, updates)
        conn.commit()

    return stats


def enrich_new_order_items(order_id, conn):
    """
    Enrich order items for a specific newly-scraped order.

    Call this right after saving a new order in order_scraper.py.

    Args:
        order_id: the order ID just saved
        conn: SQLite connection
    """
    _ensure_schema(conn)

    known_brands = _load_known_brands(conn)
    items = _load_items(conn)

    rows = conn.execute("""
        SELECT id, raw_name, brand, item_id
        FROM order_items
        WHERE order_id = ?
    """, (order_id,)).fetchall()

    if not rows:
        return

    now = datetime.now().isoformat()
    enriched = 0

    for row in rows:
        oi_id = row[0]
        raw_name = row[1]
        current_brand = row[2]
        current_item_id = row[3]

        new_brand = current_brand
        new_item_id = current_item_id

        if not current_brand or current_brand.strip() == "":
            brand, _ = extract_brand(raw_name, known_brands)
            if brand:
                new_brand = brand

        if current_item_id is None:
            item_id, _, _ = link_to_item(raw_name, items)
            if item_id is not None:
                new_item_id = item_id

        conn.execute("""
            UPDATE order_items
            SET brand = ?, item_id = ?, enriched_at = ?
            WHERE id = ?
        """, (new_brand, new_item_id, now, oi_id))
        enriched += 1

    conn.commit()
    print(f"  [ENRICH] Enriched {enriched} items for order #{order_id}")


# ============================================================
# CLI
# ============================================================

def main():
    force = "--force" in sys.argv
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print("  ORDER HISTORY ENRICHMENT")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    mode = "FORCE" if force else "DRY-RUN" if dry_run else "NORMAL"
    print(f"  Mode: {mode}")
    print(f"  Database: {DB_PATH}")

    stats = enrich_order_items(conn, force=force, dry_run=dry_run)

    print(f"\n  Results:")
    print(f"    Total items processed: {stats['total']}")
    print(f"    Brands extracted:      {stats['brands_added']} new")
    print(f"    Items linked:          {stats['items_linked']} new")
    print(f"    Already had brand:     {stats.get('already_had_brand', 0)}")
    print(f"    Already had item_id:   {stats.get('already_had_item', 0)}")

    if not dry_run and stats["total"] > 0:
        # Show updated stats
        total = conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]
        with_brand = conn.execute(
            "SELECT COUNT(*) FROM order_items WHERE brand IS NOT NULL AND brand != ''"
        ).fetchone()[0]
        with_item = conn.execute(
            "SELECT COUNT(*) FROM order_items WHERE item_id IS NOT NULL"
        ).fetchone()[0]

        print(f"\n  After enrichment:")
        print(f"    Brand populated: {with_brand}/{total} ({with_brand/total*100:.1f}%)")
        print(f"    Item linked:     {with_item}/{total} ({with_item/total*100:.1f}%)")

    conn.close()
    print(f"\n{'=' * 60}")


if __name__ == "__main__":
    main()
