"""
AutoRestock - Database Test (Slice 1 verification)

Creates the DB, inserts dummy data, queries it back, prints results.
If this prints without errors, Slice 1 is done.
"""

import sqlite3
import os
from db_setup import create_database, DB_PATH


def test_database():
    # Step 1: Create the database (safe to re-run)
    create_database()

    conn = sqlite3.connect(DB_PATH)
    # This makes query results accessible by column name (easier to read)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Step 2: Insert test stores
    print("\n--- Inserting test stores ---")
    cursor.execute("""
        INSERT OR IGNORE INTO stores (name, website_url, categories)
        VALUES ('Carrefour UAE', 'https://www.carrefouruae.com', 'general groceries, cleaning, household')
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO stores (name, website_url, categories)
        VALUES ('Kibsons', 'https://www.kibsons.com', 'dairy, fresh produce, meats')
    """)
    conn.commit()

    # Query them back
    cursor.execute("SELECT * FROM stores")
    stores = cursor.fetchall()
    for s in stores:
        print(f"  Store #{s['id']}: {s['name']} ({s['website_url']})")

    # Step 3: Insert a test item
    print("\n--- Inserting test item ---")
    cursor.execute("""
        INSERT INTO items (name, brand, size, category, preferred_store_id, avg_price, quality_tier, origin_country)
        VALUES ('Organic Free Range Eggs', 'Al Jazira', '6 pack', 'dairy', 1, 22.50, 'organic', 'UAE')
    """)
    item_id = cursor.lastrowid
    conn.commit()
    print(f"  Inserted item #{item_id}: Organic Free Range Eggs")

    # Step 4: Insert a test order with that item
    print("\n--- Inserting test order ---")
    cursor.execute("""
        INSERT INTO orders (store_id, order_date, order_ref, total_amount)
        VALUES (1, '2026-02-18', 'CRF-2026-001', 195.50)
    """)
    order_id = cursor.lastrowid

    cursor.execute("""
        INSERT INTO order_items (order_id, item_id, raw_name, quantity, unit_price, total_price)
        VALUES (?, ?, 'Organic Free Range Eggs 6 Pack', 2, 22.50, 45.00)
    """, (order_id, item_id))
    conn.commit()
    print(f"  Inserted order #{order_id} with 1 line item")

    # Step 5: Query it all back — this is the real test
    print("\n--- Querying: What did I buy in my last order? ---")
    cursor.execute("""
        SELECT
            o.order_date,
            s.name as store,
            o.total_amount,
            oi.raw_name,
            oi.quantity,
            oi.unit_price,
            oi.total_price
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        JOIN stores s ON o.store_id = s.id
        WHERE o.id = ?
    """, (order_id,))

    items = cursor.fetchall()
    for item in items:
        print(f"  {item['order_date']} | {item['store']} | {item['raw_name']}")
        print(f"    Qty: {item['quantity']} x {item['unit_price']} AED = {item['total_price']} AED")

    # Step 6: Insert and query a restock rule
    print("\n--- Inserting restock rule ---")
    cursor.execute("""
        INSERT OR REPLACE INTO restock_rules (item_id, avg_days_between, avg_quantity, last_ordered_date, next_restock_date, confidence, times_ordered)
        VALUES (?, 5.0, 2.0, '2026-02-18', '2026-02-23', 0.7, 8)
    """, (item_id,))
    conn.commit()

    cursor.execute("""
        SELECT i.name, r.avg_days_between, r.avg_quantity, r.next_restock_date, r.confidence
        FROM restock_rules r
        JOIN items i ON r.item_id = i.id
    """)
    rules = cursor.fetchall()
    for r in rules:
        print(f"  {r['name']}: every {r['avg_days_between']} days, qty {r['avg_quantity']}, next restock: {r['next_restock_date']} (confidence: {r['confidence']:.0%})")

    # Step 7: Test agent log
    print("\n--- Inserting agent log entry ---")
    cursor.execute("""
        INSERT INTO agent_log (action_type, details, outcome)
        VALUES ('test', 'Slice 1 database test run', 'success')
    """)
    conn.commit()

    cursor.execute("SELECT * FROM agent_log ORDER BY id DESC LIMIT 1")
    log = cursor.fetchone()
    print(f"  [{log['created_at']}] {log['action_type']}: {log['details']} -> {log['outcome']}")

    # Step 8: Count all tables to verify they exist
    print("\n--- Table summary ---")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = cursor.fetchall()
    for t in tables:
        cursor.execute(f"SELECT COUNT(*) as cnt FROM {t['name']}")
        count = cursor.fetchone()['cnt']
        print(f"  {t['name']}: {count} rows")

    conn.close()

    # Clean up test data — delete the DB so real scraping starts fresh
    print(f"\n--- All tests passed! ---")           
    print(f"Database file: {DB_PATH}")
    print(f"File size: {os.path.getsize(DB_PATH) / 1024:.1f} KB")
    print(f"\nSlice 1 is DONE. Delete autostock.db before starting Slice 2 (real data).")


if __name__ == "__main__":
    test_database()
