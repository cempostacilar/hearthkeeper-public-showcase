"""
Import order history from order_history/*.json into autostock.db.
Use this to load baseline data extracted from screenshots (or other sources).
Safe to re-run: skips orders that already exist (same store + order_ref).

Run from project root: python import_order_history.py
"""

import json
import sqlite3
from pathlib import Path
from typing import Tuple

from db_setup import DB_PATH, create_database

ORDER_HISTORY_DIR = Path(__file__).resolve().parent / "order_history"


def ensure_store(cursor, name: str, website_url: str = None) -> int:
    """Get or create store; return store id."""
    cursor.execute("SELECT id FROM stores WHERE name = ?", (name,))
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute(
        "INSERT INTO stores (name, website_url) VALUES (?, ?)",
        (name, website_url or ""),
    )
    return cursor.lastrowid


def order_exists(cursor, store_id: int, order_ref: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM orders WHERE store_id = ? AND order_ref = ?",
        (store_id, order_ref),
    )
    return cursor.fetchone() is not None


def import_order(conn, payload: dict) -> Tuple[bool, str]:
    """
    Import one order. Returns (success, message).
    Skips if order with same store_id + order_ref already exists.
    """
    cursor = conn.cursor()
    store_name = payload.get("store_name") or payload.get("store")
    if not store_name:
        return False, "missing store_name"
    order_ref = str(payload.get("order_ref", ""))
    order_date = payload.get("order_date", "").strip()
    if not order_ref or not order_date:
        return False, "missing order_ref or order_date"
    items = payload.get("items") or []
    if not items:
        return False, "no items"

    store_id = ensure_store(
        cursor,
        store_name,
        payload.get("store_website_url"),
    )

    if order_exists(cursor, store_id, order_ref):
        return True, f"skipped (already exists): {order_ref}"

    total = payload.get("total_amount")
    if total is None:
        total = sum(
            (it.get("total_price") or (it.get("quantity", 0) * (it.get("unit_price") or 0)))
            for it in items
        )

    cursor.execute(
        """
        INSERT INTO orders (store_id, order_date, order_ref, total_amount, currency, status, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            store_id,
            order_date,
            order_ref,
            total,
            payload.get("currency") or "AED",
            payload.get("status") or "completed",
            payload.get("source") or "order_history_screenshot",
        ),
    )
    order_id = cursor.lastrowid

    for it in items:
        qty = float(it.get("quantity", 1))
        unit = it.get("unit_price")
        line_total = it.get("total_price")
        if line_total is None and unit is not None:
            line_total = qty * unit
        cursor.execute(
            """
            INSERT INTO order_items (order_id, raw_name, quantity, unit_price, total_price, size, brand)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                (it.get("raw_name") or it.get("name") or "").strip(),
                qty,
                unit,
                line_total,
                it.get("size"),
                it.get("brand"),
            ),
        )

    return True, f"imported order {order_ref} ({len(items)} items)"


def load_json_files(dir_path: Path):
    """Yield (file_path, payload) for each JSON file that looks like an order."""
    if not dir_path.is_dir():
        return
    for path in sorted(dir_path.glob("*.json")):
        if path.name.startswith("schema"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            yield path, None, str(e)
            continue
        if isinstance(data, list):
            for i, item in enumerate(data):
                yield path, item, None
        elif isinstance(data, dict) and (data.get("order_ref") or data.get("order_date") or data.get("items")):
            yield path, data, None
        else:
            yield path, None, "not an order object (skip)"


def main():
    create_database()
    if not ORDER_HISTORY_DIR.is_dir():
        print(f"Order history dir not found: {ORDER_HISTORY_DIR}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    imported = 0
    skipped = 0
    errors = []

    for path, payload, load_err in load_json_files(ORDER_HISTORY_DIR):
        if load_err:
            if "not an order" not in load_err:
                print(f"  Skip {path.name}: {load_err}")
            continue
        success, msg = import_order(conn, payload)
        if success:
            if "skipped" in msg:
                skipped += 1
            else:
                imported += 1
            print(f"  {path.name}: {msg}")
        else:
            errors.append(f"{path.name}: {msg}")
            print(f"  {path.name}: ERROR {msg}")

    conn.commit()
    conn.close()

    print(f"\nDone. Imported: {imported}, Skipped: {skipped}, Errors: {len(errors)}")
    if errors:
        for e in errors:
            print(f"  - {e}")
    print(f"Database: {DB_PATH}")
    print("Other agents: see order_history/README.md for how to query this data.")


if __name__ == "__main__":
    main()
