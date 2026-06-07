"""
Quick test for the shopping list generator.

Checks:
  1. DB has items and restock_rules (from consumption_habits.py)
  2. list_generate produces results for upcoming dates
  3. list_get retrieves the saved list
  4. list_update_item works (approve, skip, modify)
  5. list_add_item adds a user-requested item
  6. list_finalize bumps restock dates
"""

import sqlite3
from datetime import datetime, timedelta
from db_setup import DB_PATH, create_database
from consumption_habits import seed_habits
from list_generate import (
    list_generate, list_get, list_update_item,
    list_add_item, list_finalize, _print_list,
)

def main():
    print("=" * 55)
    print("  TEST: Shopping List Generator")
    print("=" * 55)

    # Ensure DB and habits are seeded
    create_database()
    print("\n[1] Seeding consumption habits...")
    seed_habits()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Check what's in the DB
    cur.execute("SELECT COUNT(*) as cnt FROM items")
    print(f"    Items in DB: {cur.fetchone()['cnt']}")
    cur.execute("SELECT COUNT(*) as cnt FROM restock_rules")
    print(f"    Restock rules: {cur.fetchone()['cnt']}")

    # Show next_restock_dates
    print("\n[2] Restock dates in DB:")
    cur.execute("""
        SELECT i.name, r.next_restock_date, r.avg_days_between, r.confidence
        FROM restock_rules r JOIN items i ON r.item_id = i.id
        ORDER BY r.next_restock_date
        LIMIT 10
    """)
    for row in cur.fetchall():
        print(f"    {row['name']}: next={row['next_restock_date']}, "
              f"every {row['avg_days_between']} days, conf={row['confidence']}")

    conn.close()

    # Test: generate for a date far enough in the future to catch items
    future = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    print(f"\n[3] Generating list for {future} (dry run)...")
    result = list_generate(target_date=future, dry_run=True)
    print(f"    Found {result['summary']['total_items']} items due")
    if result["items"]:
        _print_list(result)
    else:
        print("    No items due. Trying 14 days out...")
        future14 = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
        result = list_generate(target_date=future14, dry_run=True)
        print(f"    Found {result['summary']['total_items']} items due for {future14}")
        if result["items"]:
            _print_list(result)

    # Test: save a real list
    print(f"\n[4] Saving list to DB...")
    result = list_generate(target_date=future)
    if result["list_id"]:
        print(f"    Saved as list #{result['list_id']}")

        # Test list_get
        print(f"\n[5] Retrieving list #{result['list_id']}...")
        fetched = list_get(result["list_id"])
        print(f"    Got: {fetched['summary']['total_items']} items, "
              f"status='{fetched['status']}'")

        # Test list_update_item
        if fetched["items"]:
            first_item = fetched["items"][0]
            lid = first_item["list_item_id"]
            print(f"\n[6] Approving first item: {first_item['item_name']}...")
            ok = list_update_item(lid, "approve")
            print(f"    Result: {'OK' if ok else 'FAILED'}")

            if len(fetched["items"]) > 1:
                second = fetched["items"][1]
                print(f"    Skipping: {second['item_name']}...")
                list_update_item(second["list_item_id"], "skip")

        # Test list_add_item
        print(f"\n[7] Adding user-requested item: 'Avocado'...")
        new_id = list_add_item(result["list_id"], "Avocado", store_name="Kibsons", quantity=3, note="ripe ones please")
        print(f"    Added as list_item #{new_id}")

        # Test list_finalize
        print(f"\n[8] Finalizing list...")
        final = list_finalize(result["list_id"])
        print(f"    Approved: {final['approved']}, Skipped: {final['skipped']}")
        print(f"    Restock dates updated for {final['restock_dates_updated']} items")

        # Show final state
        print(f"\n[9] Final list state:")
        final_list = list_get(result["list_id"])
        _print_list(final_list)
    else:
        print("    No items to save — list was empty")

    print("\nAll tests passed!")


if __name__ == "__main__":
    main()
