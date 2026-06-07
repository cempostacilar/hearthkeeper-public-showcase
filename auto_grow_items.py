"""
Auto-grow the `items` table from order history.

The household orders 156 distinct item names over 180 days but only
21 are in the tracked-items list. The other ~135 (about 102 after
brand de-duplication) get bought regularly yet the predictor has no
concept of them, so they never get suggested.

This module finds high-frequency `order_items.raw_name` rows that
aren't linked to `items.id`, clusters near-duplicates by normalized
name, and -- for any cluster with at least `min_purchases` distinct
orders -- creates:

  * An `items` row (name, category inferred from keywords,
    preferred_store = most common store for that name).
  * A `restock_rules` row with cadence computed from observed
    order dates.
  * Links the original `order_items` rows back via `item_id`.

Conservative by design:
  * Default threshold of 3 distinct orders before promotion.
  * Caps `max_new` per run (default 25) so a runaway scrape can't
    flood the items table.
  * Skips clusters whose normalized name already exists in `items`.
  * Tags each new item with `notes='Auto-grown from order history'`
    so an operator can audit / revert if needed.

Hooks:
  * `POST /api/diag/auto-grow-items` (HMAC-authed) -- manual trigger.
  * Could be added to the nightly scheduler in a future commit.
"""

import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from db_setup import DB_PATH


# Defaults
MIN_PURCHASES = 3        # ≥ this many distinct orders to promote
MAX_NEW_PER_RUN = 25     # safety cap
DAYS_WINDOW = 180        # look-back window
DEFAULT_CATEGORY = "Pantry"


# Heuristic name -> category map. First-pass crude but useful;
# can be promoted to an LLM call later or hand-curated as the
# tracked set grows.
CATEGORY_KEYWORDS = {
    "Produce": [
        "potato", "tomato", "onion", "carrot", "cucumber",
        "pepper", "capsicum", "lettuce", "spinach", "kale",
        "broccoli", "cauliflower", "cabbage", "garlic",
        "ginger", "mushroom", "chili", "chilli", "eggplant",
        "zucchini", "courgette", "marrow", "okra", "celery",
        "leek", "scallion", "shallot", "rocket", "arugula",
        # fruits
        "apple", "banana", "berry", "berries", "strawberr",
        "blueberr", "raspberr", "blackberr", "grape", "kiwi",
        "mango", "orange", "lemon", "lime", "pear", "peach",
        "plum", "cherry", "watermelon", "melon", "pineapple",
        "pomegranate", "fig", "avocado",
        # herbs
        "dill", "parsley", "coriander", "mint", "basil",
        "thyme", "rosemary", "sage",
    ],
    "Dairy": [
        "milk", "yog", "yoghurt", "cheese", "butter", "cream", "kefir",
        "labneh", "feta", "mozzarella", "halloumi",
    ],
    "Meat": [
        "chicken", "beef", "lamb", "mince", "steak", "sausage",
        "bacon", "ham", "turkey", "duck", "veal", "ribeye",
        "striploin", "wagyu",
    ],
    "Seafood": [
        "salmon", "tuna", "shrimp", "prawn", "fish",
        "lobster", "crab", "scallop", "calamari", "squid",
    ],
    "Bakery": [
        "bread", "loaf", "bun", "bagel", "croissant",
        "muffin", "cake", "pastry", "danish", "pita",
        "tortilla", "flatbread", "naan", "wrap",
    ],
    "Beverages": [
        "juice", "soda", "drink", "cola", "tea",
        "coffee", "water", "sparkling", "smoothie",
    ],
    "Pantry": [
        "pasta", "rice", "flour", "sugar", "salt", "oil",
        "vinegar", "sauce", "ketchup", "mayo", "honey",
        "jam", "spread", "spice", "cereal", "oats", "granola",
        "nut", "almond", "peanut", "lentil", "bean", "chickpea",
        "noodle", "couscous",
    ],
    "Snacks": [
        "chip", "cracker", "biscuit", "cookie", "chocolate",
        "candy", "gum", "popcorn", "pretzel",
    ],
    "Frozen": [
        "frozen", "ice cream", "sorbet",
    ],
}

# Names we never want to promote -- non-grocery, fees, etc.
SKIP_NAME_PATTERNS = [
    "delivery", "service", "fee", "discount", "promo",
    "bag charge", "voucher", "tip",
]


def _normalize_name(raw_name):
    """Collapse case + punctuation + pack-size suffixes for clustering.

    "Organic Tomatoes 500g" + "Organic Tomatoes 1kg" -> "organic tomatoes"
    so the same canonical item doesn't fragment across pack sizes.
    """
    if not raw_name:
        return ""
    name = raw_name.lower().strip()
    # Strip "- 500g" / "500 g" / "1L" / "x 250g" / "(6 PCS)" suffixes
    name = re.sub(
        r'[\s\-,]*\(?\s*(?:x\s*)?\d+(?:\.\d+)?\s*(?:g|kg|ml|l|pcs?|pc|pack|litre|liter|oz|lb)\)?\.?',
        '',
        name,
    )
    name = re.sub(r'\s*\([^)]*\)\s*', ' ', name)
    name = re.sub(r'\s+', ' ', name)
    name = name.strip(' -,.')
    return name


def _canonical_name(raw_names):
    """Pick a single human-readable name from a cluster of variants.
    Most-common wins; ties broken by shortest (least clutter).
    """
    counts = Counter(raw_names)
    best = counts.most_common()
    if not best:
        return ""
    top_count = best[0][1]
    tied = [name for name, c in best if c == top_count]
    tied.sort(key=len)
    return tied[0].strip()


def _infer_category(name):
    """Heuristic name -> category. Falls back to DEFAULT_CATEGORY."""
    lname = name.lower()
    # Order matters: more specific categories first when keywords overlap.
    # We check in dict insertion order (Python 3.7+).
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw in lname:
                return cat
    return DEFAULT_CATEGORY


def _should_skip(name):
    """Return True if this name shouldn't be promoted to a tracked item."""
    lname = name.lower()
    return any(pat in lname for pat in SKIP_NAME_PATTERNS)


def grow_items_from_history(
    min_purchases=MIN_PURCHASES,
    max_new=MAX_NEW_PER_RUN,
    days_window=DAYS_WINDOW,
    dry_run=False,
    conn=None,
    verbose=False,
):
    """
    Promote frequently-purchased un-tracked raw_names into the items
    table.

    Returns a list of dicts, one per item created (or that WOULD be
    created in dry_run mode):
        {
          "item_id": int (None if dry_run),
          "canonical_name": str,
          "category": str,
          "store_id": int,
          "purchases": int,
          "cadence": float,
          "avg_price": float or None,
          "avg_quantity": float,
          "last_ordered": str,
          "next_restock_date": str,
          "raw_names": [str, ...],
          "linked_order_items": int,
        }
    """
    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own_conn = True

    try:
        rows = conn.execute(
            """
            SELECT oi.id AS oi_id,
                   oi.raw_name,
                   oi.quantity,
                   oi.unit_price,
                   oi.brand,
                   o.id AS order_id,
                   o.order_date,
                   o.store_id
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
            WHERE oi.item_id IS NULL
              AND oi.raw_name IS NOT NULL
              AND TRIM(oi.raw_name) != ''
              AND o.order_date >= date('now', ?)
            ORDER BY oi.raw_name
            """,
            (f"-{days_window} days",),
        ).fetchall()

        existing_names = {
            r["n"]
            for r in conn.execute(
                "SELECT LOWER(name) AS n FROM items"
            ).fetchall()
        }
        existing_normalized = {_normalize_name(n) for n in existing_names}

        # Cluster by normalized name
        clusters = defaultdict(list)
        for r in rows:
            norm = _normalize_name(r["raw_name"])
            if not norm:
                continue
            if norm in existing_normalized or norm in existing_names:
                continue
            if _should_skip(norm):
                continue
            clusters[norm].append(r)

        # Score and filter clusters
        candidates = []
        for norm_name, rs in clusters.items():
            distinct_orders = {r["order_id"] for r in rs}
            if len(distinct_orders) < min_purchases:
                continue

            raw_names = [r["raw_name"] for r in rs]
            canonical = _canonical_name(raw_names)
            category = _infer_category(canonical)

            order_dates = sorted({r["order_date"] for r in rs if r["order_date"]})
            if len(order_dates) >= 2:
                try:
                    first = datetime.strptime(order_dates[0], "%Y-%m-%d")
                    last = datetime.strptime(order_dates[-1], "%Y-%m-%d")
                    span = (last - first).days
                    cadence = round(span / (len(order_dates) - 1), 1)
                    if cadence < 1:
                        cadence = 1.0
                    elif cadence > 60:
                        cadence = 60.0
                except (ValueError, TypeError):
                    cadence = 7.0
            else:
                cadence = 7.0

            store_counts = Counter(r["store_id"] for r in rs)
            preferred_store_id = store_counts.most_common(1)[0][0]

            prices = [r["unit_price"] for r in rs if r["unit_price"]]
            avg_price = round(sum(prices) / len(prices), 2) if prices else None

            quantities = [float(r["quantity"]) for r in rs if r["quantity"]]
            avg_quantity = round(sum(quantities) / len(quantities), 2) if quantities else 1.0

            last_ordered = order_dates[-1] if order_dates else None

            candidates.append({
                "norm_name": norm_name,
                "canonical_name": canonical,
                "category": category,
                "store_id": preferred_store_id,
                "raw_names": sorted(set(raw_names))[:6],
                "purchases": len(distinct_orders),
                "cadence": cadence,
                "avg_price": avg_price,
                "avg_quantity": avg_quantity,
                "last_ordered": last_ordered,
                "_oi_ids": [r["oi_id"] for r in rs],
            })

        candidates.sort(key=lambda c: (-c["purchases"], c["cadence"]))
        selected = candidates[:max_new]

        if dry_run:
            for c in selected:
                c.pop("_oi_ids", None)
            return selected

        # Insert items + restock_rules + link order_items
        created = []
        for c in selected:
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO items (name, category, preferred_store_id, avg_price,
                                       quality_tier, notes)
                    VALUES (?, ?, ?, ?, 'preferred', 'Auto-grown from order history')
                    """,
                    (c["canonical_name"], c["category"], c["store_id"], c["avg_price"]),
                )
                new_item_id = cur.lastrowid

                # next_restock = max(today, last_ordered + cadence) so we never
                # send a "due in the past" sentinel; list_generate handles the
                # "now or earlier" case correctly via the coverage_cutoff filter.
                next_restock = datetime.now().date()
                if c["last_ordered"]:
                    try:
                        last_d = datetime.strptime(c["last_ordered"], "%Y-%m-%d").date()
                        candidate = last_d + timedelta(days=int(round(c["cadence"])))
                        if candidate > next_restock:
                            next_restock = candidate
                    except (ValueError, TypeError):
                        pass
                next_restock_str = next_restock.strftime("%Y-%m-%d")

                confidence = min(0.85, 0.5 + 0.1 * min(c["purchases"], 5))

                cur.execute(
                    """
                    INSERT INTO restock_rules
                        (item_id, avg_days_between, avg_quantity,
                         last_ordered_date, next_restock_date,
                         confidence, times_ordered)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_item_id, c["cadence"], c["avg_quantity"],
                        c["last_ordered"], next_restock_str,
                        confidence, c["purchases"],
                    ),
                )

                # Link original order_items rows back to the new items row
                # so we can compute cadence over them on the next refresh.
                oi_ids = c["_oi_ids"]
                if oi_ids:
                    placeholders = ",".join("?" * len(oi_ids))
                    cur.execute(
                        f"UPDATE order_items SET item_id = ? WHERE id IN ({placeholders})",
                        [new_item_id] + oi_ids,
                    )

                conn.commit()

                created_row = {k: v for k, v in c.items() if not k.startswith("_")}
                created_row["item_id"] = new_item_id
                created_row["next_restock_date"] = next_restock_str
                created_row["confidence"] = confidence
                created_row["linked_order_items"] = len(oi_ids)
                created.append(created_row)

                if verbose:
                    print(
                        f"  [grow] {c['canonical_name']:30s} "
                        f"({c['category']:10s}) "
                        f"{c['purchases']:>2}× "
                        f"cadence={c['cadence']}d "
                        f"item_id={new_item_id} "
                        f"linked={len(oi_ids)}"
                    )
            except Exception as e:
                print(f"  [grow] Failed to create {c['canonical_name']}: {e}")
                conn.rollback()

        return created
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    """CLI: dry-run mode by default, --apply to actually create."""
    import sys
    apply_mode = "--apply" in sys.argv
    result = grow_items_from_history(dry_run=not apply_mode, verbose=True)
    print()
    if apply_mode:
        print(f"Created {len(result)} new tracked items.")
    else:
        print(f"DRY RUN: would create {len(result)} new tracked items.")
        print("Pass --apply to commit.")
