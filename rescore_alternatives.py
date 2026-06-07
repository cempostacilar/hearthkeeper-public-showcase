"""
Rescore item_alternatives against the current product catalog.

Two entry points:
  rescore_store(store_id, conn)  — rescore one store (called by scrapers post-scrape)
  rescore_all()                  — rescore all active stores (manual / CLI)

After a Gatherer (scraper) run, old products may be marked is_available=0.
This module re-indexes the Book Keeper's item_alternatives table so the
Curator always sees fresh, available products.

Contract: "Gatherer never finishes without the Book Keeper's index being refreshed."
"""

import json
import sqlite3
from db_setup import DB_PATH, create_database
from product_knowledge import search_catalog, score_and_rank_alternatives


def rescore_store(store_id, conn=None, verbose=True):
    """
    Re-index item_alternatives for a single store against its current catalog.

    Called automatically by scrapers after each inventory run (post-scrape hook),
    or manually via rescore_all().

    Args:
        store_id:  which store to rescore (1=Kibsons, 2=Carrefour)
        conn:      existing SQLite connection (reuses scraper's connection).
                   If None, opens a new one.
        verbose:   print progress lines (True for CLI, False for quiet mode)

    Returns:
        dict with stats: {"items": N, "alternatives_scored": N, "preferred": N}
    """
    own_conn = False
    if conn is None:
        create_database()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own_conn = True

    # Get store name for logging
    store_row = conn.execute("SELECT name FROM stores WHERE id = ?", (store_id,)).fetchone()
    store_name = store_row["name"] if store_row else f"Store {store_id}"

    # Get all tracked items
    items = conn.execute("""
        SELECT i.id, i.name, i.search_terms
        FROM items i ORDER BY i.name
    """).fetchall()

    if verbose:
        print(f"\n[Rescore] {store_name}: re-indexing {len(items)} items against fresh catalog...")

    total_scored = 0
    total_preferred = 0

    for item in items:
        item_id = item["id"]
        item_name = item["name"]

        # Step 1: Count and delete old alternatives for this item/store
        old_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM item_alternatives WHERE item_id = ? AND store_id = ?",
            (item_id, store_id)
        ).fetchone()["cnt"]

        conn.execute(
            "DELETE FROM item_alternatives WHERE item_id = ? AND store_id = ?",
            (item_id, store_id)
        )
        conn.commit()

        # Step 2: Build search terms
        terms = [item_name]

        # For multi-word items, also search individual significant words
        words = item_name.split()
        if len(words) > 1:
            for w in words:
                if len(w) >= 3:
                    terms.append(w)

        # Also include store-specific search terms from the items table
        search_terms_json = item["search_terms"]
        if search_terms_json:
            try:
                st = json.loads(search_terms_json)
                for store_key, store_terms in st.items():
                    if isinstance(store_terms, list):
                        terms.extend(store_terms[:3])
            except (json.JSONDecodeError, AttributeError):
                pass

        # Step 3: Search the catalog for each term, collect unique catalog IDs
        catalog_ids = set()
        for term in terms:
            results = search_catalog(term, store_id=store_id, limit=25, conn=conn)
            for r in results:
                catalog_ids.add(r["catalog_id"])

        if not catalog_ids:
            if verbose and old_count > 0:
                print(f"    {item_name}: 0 catalog matches (was {old_count})")
            continue

        # Step 4: Score and rank
        ranked = score_and_rank_alternatives(
            item_id, item_name, store_id, list(catalog_ids), conn=conn
        )

        good = [r for r in ranked if r["tier"] in ("preferred", "acceptable")]

        if verbose:
            print(f"    {item_name}: {len(catalog_ids)} candidates -> "
                  f"{len(ranked)} scored ({len(good)} preferred/acceptable) "
                  f"[was {old_count}]")
            if ranked:
                best = ranked[0]
                price = best['product'].get('price', '?')
                name = best['product']['name'][:55]
                print(f"      -> {name} | AED {price} [{best['tier']}, {best['score']}/100]")

        total_scored += len(ranked)
        total_preferred += len(good)

    if verbose:
        # Post-rescore diagnostic for this store
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pc.is_available = 1 THEN 1 ELSE 0 END) as available
            FROM item_alternatives ia
            JOIN product_catalog pc ON ia.catalog_id = pc.id
            WHERE ia.store_id = ?
        """, (store_id,)).fetchone()
        print(f"[Rescore] {store_name} done: {row['available'] or 0} available alternatives "
              f"({total_preferred} preferred/acceptable)")

    if own_conn:
        conn.close()

    return {
        "items": len(items),
        "alternatives_scored": total_scored,
        "preferred": total_preferred,
    }


def rescore_all():
    """
    Rescore item_alternatives for ALL active stores.

    CLI entry point — prints full diagnostics before and after.
    """
    create_database()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # ── Pre-fix diagnostic ──
    print("=" * 60)
    print("  PRE-FIX DIAGNOSTIC")
    print("=" * 60)

    for store_id, store_label in [(1, "Kibsons"), (2, "Carrefour")]:
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pc.is_available = 1 THEN 1 ELSE 0 END) as available,
                   SUM(CASE WHEN pc.is_available = 0 THEN 1 ELSE 0 END) as unavailable
            FROM item_alternatives ia
            JOIN product_catalog pc ON ia.catalog_id = pc.id
            WHERE ia.store_id = ?
        """, (store_id,)).fetchone()
        print(f"  {store_label} alternatives: {row['available'] or 0} available, "
              f"{row['unavailable'] or 0} unavailable (of {row['total'] or 0} total)")

    for store_id, store_label in [(1, "Kibsons"), (2, "Carrefour")]:
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN is_available = 1 THEN 1 ELSE 0 END) as available
            FROM product_catalog WHERE store_id = ?
        """, (store_id,)).fetchone()
        print(f"  {store_label} catalog: {row['available'] or 0} available of {row['total'] or 0} total")

    # ── Get active stores ──
    stores = conn.execute("""
        SELECT id, name FROM stores WHERE is_active = 1
    """).fetchall()

    print(f"\n  Stores: {', '.join(s['name'] for s in stores)}")

    # ── Rescore each store ──
    print(f"\n{'=' * 60}")
    print(f"  RESCORING ALTERNATIVES")
    print(f"{'=' * 60}")

    total_scored = 0
    for store in stores:
        result = rescore_store(store["id"], conn=conn, verbose=True)
        total_scored += result["alternatives_scored"]

    # ── Post-fix diagnostic ──
    print(f"\n{'=' * 60}")
    print(f"  POST-FIX DIAGNOSTIC")
    print(f"{'=' * 60}")

    for store_id, store_label in [(1, "Kibsons"), (2, "Carrefour")]:
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pc.is_available = 1 THEN 1 ELSE 0 END) as available,
                   SUM(CASE WHEN pc.is_available = 0 THEN 1 ELSE 0 END) as unavailable
            FROM item_alternatives ia
            JOIN product_catalog pc ON ia.catalog_id = pc.id
            WHERE ia.store_id = ?
        """, (store_id,)).fetchone()
        print(f"  {store_label} alternatives: {row['available'] or 0} available, "
              f"{row['unavailable'] or 0} unavailable (of {row['total'] or 0} total)")

    conn.close()
    print(f"\n  Done! {total_scored} alternatives scored across all stores.")


if __name__ == "__main__":
    rescore_all()
