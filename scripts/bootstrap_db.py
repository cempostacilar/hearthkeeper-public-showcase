#!/usr/bin/env python3
"""Initialize SQLite database and run Slice-1 smoke test."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "sql" / "schema.sql"


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    with SCHEMA_PATH.open("r", encoding="utf-8") as f:
        conn.executescript(f.read())
    seed_stores(conn)
    conn.commit()
    return conn


def seed_stores(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO stores(name, base_url) VALUES (?, ?)",
        [
            ("Carrefour UAE", "https://www.carrefouruae.com"),
            ("Kibsons", "https://kibsons.com"),
        ],
    )


def run_smoke_test(conn: sqlite3.Connection) -> None:
    store_id = conn.execute(
        "SELECT id FROM stores WHERE name = ?", ("Carrefour UAE",)
    ).fetchone()[0]

    conn.execute(
        """
        INSERT INTO products(canonical_name, brand, size_text, quality_tier, origin_country, preferred_store_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_name, brand, size_text) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
        """,
        ("Eggs", "Organic Valley", "6 pcs", "organic", "UAE", store_id),
    )

    product_id = conn.execute(
        "SELECT id FROM products WHERE canonical_name = ? AND brand = ? AND size_text = ?",
        ("Eggs", "Organic Valley", "6 pcs"),
    ).fetchone()[0]

    conn.execute(
        """
        INSERT OR IGNORE INTO orders(store_id, external_order_id, ordered_at, subtotal, delivery_fee, total)
        VALUES (?, ?, datetime('now'), ?, ?, ?)
        """,
        (store_id, "dummy-order-001", 21.0, 5.0, 26.0),
    )

    order_id = conn.execute(
        "SELECT id FROM orders WHERE store_id = ? AND external_order_id = ?",
        (store_id, "dummy-order-001"),
    ).fetchone()[0]

    conn.execute(
        """
        INSERT OR IGNORE INTO order_items(order_id, product_id, raw_product_name, quantity, unit_price, line_total, product_url, category, source_brand, source_size_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            product_id,
            "Organic Valley Eggs 6 pcs",
            1,
            21.0,
            21.0,
            "https://www.carrefouruae.com/example-eggs",
            "Dairy & Eggs",
            "Organic Valley",
            "6 pcs",
        ),
    )

    conn.commit()

    rows = conn.execute(
        """
        SELECT o.external_order_id, s.name AS store, p.canonical_name, oi.quantity, oi.unit_price, o.total
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE o.external_order_id = 'dummy-order-001'
        """
    ).fetchall()

    print("Smoke test rows:")
    for row in rows:
        print(row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize SQLite DB for grocery copilot MVP")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "grocery_agent.db")
    parser.add_argument("--smoke-test", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    conn = init_db(args.db)
    if args.smoke_test:
        run_smoke_test(conn)
    conn.close()
    print(f"Database initialized at: {args.db}")


if __name__ == "__main__":
    main()
