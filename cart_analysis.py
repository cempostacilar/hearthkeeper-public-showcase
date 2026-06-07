"""
AutoRestock - Cart-to-Order Gap Analysis

Learns from what was PLANNED vs what was ACTUALLY purchased.

The gap between cart creation and the final order tells us:
  1. Items removed from cart -> still had stock, bought elsewhere, or budget trim
  2. Items added manually   -> missing from habits, one-off, or faster consumption
  3. Quantity changes        -> consumption rate needs calibrating
  4. Substitutions           -> preferred product unavailable or better deal found

This module:
  - Records cart snapshots (what the system built)
  - Compares against actual order data (scraped from store order history)
  - Classifies each gap with a likely reason
  - Feeds adjustments back into restock_rules and consumption confidence

DB tables used:
  - cart_snapshots: what we built (1 row per cart-build event)
  - cart_snapshot_items: each item in the built cart
  - orders / order_items: what was actually purchased
  - restock_rules: where we push timing adjustments

Usage:
  from cart_analysis import save_cart_snapshot, analyze_gap, get_gap_history
"""

import sqlite3
from datetime import datetime

from db_setup import DB_PATH


# ============================================================
# DB SCHEMA ADDITIONS
# ============================================================
# These tables sit alongside the existing schema in db_setup.py.
# We create them here so the module is self-contained for now.
# Can be merged into db_setup.py later.

def ensure_tables():
    """Create cart analysis tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ── Cart Snapshots ──
    # One row per cart-building event.
    # Links back to the shopping list it was built from.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cart_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id     INTEGER NOT NULL REFERENCES shopping_lists(id),
            store_id    INTEGER NOT NULL REFERENCES stores(id),
            store_name  TEXT NOT NULL,
            built_at    TEXT DEFAULT (datetime('now')),
            total_items INTEGER DEFAULT 0,
            estimated_total REAL DEFAULT 0,
            status      TEXT DEFAULT 'built',
            notes       TEXT
        )
    """)

    # ── Cart Snapshot Items ──
    # Each item that was in the cart when it was built.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cart_snapshot_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id     INTEGER NOT NULL REFERENCES cart_snapshots(id),
            item_id         INTEGER REFERENCES items(id),
            item_name       TEXT NOT NULL,
            product_name    TEXT,
            product_url     TEXT,
            quantity        REAL NOT NULL DEFAULT 1,
            price           REAL,
            was_in_list     INTEGER DEFAULT 1,
            source          TEXT DEFAULT 'system'
        )
    """)

    # ── Gap Analysis Results ──
    # Computed comparison between cart snapshot and actual order.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gap_analysis (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id     INTEGER NOT NULL REFERENCES cart_snapshots(id),
            order_id        INTEGER REFERENCES orders(id),
            item_id         INTEGER REFERENCES items(id),
            item_name       TEXT NOT NULL,
            gap_type        TEXT NOT NULL,
            detail          TEXT,
            cart_qty        REAL,
            order_qty       REAL,
            cart_price      REAL,
            order_price     REAL,
            cart_product     TEXT,
            order_product    TEXT,
            likely_reason   TEXT,
            confidence      REAL DEFAULT 0.5,
            user_confirmed  INTEGER DEFAULT 0,
            user_reason     TEXT,
            analyzed_at     TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# GAP TYPES
# ============================================================

GAP_TYPES = {
    "removed":      "Item was in cart but not purchased",
    "added":        "Item was purchased but wasn't in cart",
    "qty_decreased": "Bought less than planned",
    "qty_increased": "Bought more than planned",
    "substituted":  "Different product/brand than planned",
    "matched":      "Purchased exactly as planned",
}

# ============================================================
# LIKELY REASONS (heuristic classification)
# ============================================================

REMOVAL_REASONS = {
    "still_have_stock": {
        "label": "Still Have Stock",
        "description": "Ordered too early — still have enough at home",
        "action": "Increase avg_days_between in restock_rules",
    },
    "bought_elsewhere": {
        "label": "Bought Elsewhere",
        "description": "Got it from a different store, local market, or a friend",
        "action": "No timing change, but note the off-grid purchase",
    },
    "budget_trim": {
        "label": "Budget Trim",
        "description": "Removed to keep order total down",
        "action": "Flag as optional / lower priority for future lists",
    },
    "changed_mind": {
        "label": "Changed Mind",
        "description": "Didn't actually need it or impulse add during list review",
        "action": "Lower confidence for this item in prediction",
    },
    "out_of_stock": {
        "label": "Out of Stock",
        "description": "Product was unavailable at checkout time",
        "action": "No timing change — add to next order",
    },
    "unknown": {
        "label": "Unknown",
        "description": "Can't determine why — ask user next time",
        "action": "Ask user during next check-in",
    },
}

ADDITION_REASONS = {
    "missing_habit": {
        "label": "Missing from Habits",
        "description": "Item isn't tracked yet — should be added to habits",
        "action": "Suggest adding to consumption_habits",
    },
    "faster_consumption": {
        "label": "Running Low Faster",
        "description": "Consuming faster than predicted — needed sooner",
        "action": "Decrease avg_days_between in restock_rules",
    },
    "one_off": {
        "label": "One-Off Purchase",
        "description": "Occasional purchase, not a regular habit",
        "action": "No rule change — log for reference",
    },
    "forgot_to_add": {
        "label": "Forgot to Add",
        "description": "Should have been on the list but was missed",
        "action": "Check restock_rules — might need confidence boost",
    },
    "unknown": {
        "label": "Unknown",
        "description": "Can't determine why — ask user next time",
        "action": "Ask user during next check-in",
    },
}


# ============================================================
# SAVE CART SNAPSHOT
# ============================================================

def save_cart_snapshot(cart_report: dict, conn=None) -> int:
    """
    Save a cart building report as a snapshot for future comparison.

    Args:
        cart_report: output from cart_builder.build_cart()
            Expected shape: {"list_id": int, "stores": {"StoreName": {"added": [...], ...}}}

    Returns:
        snapshot_id (or -1 if nothing saved)
    """
    ensure_tables()
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    cur = conn.cursor()
    list_id = cart_report.get("list_id")
    snapshot_ids = []

    for store_name, store_data in cart_report.get("stores", {}).items():
        added_items = store_data.get("added", [])
        if not added_items:
            continue

        # Look up store_id
        cur.execute("SELECT id FROM stores WHERE name = ?", (store_name,))
        row = cur.fetchone()
        store_id = row["id"] if row else 0

        estimated_total = store_data.get("estimated_total", 0)

        cur.execute("""
            INSERT INTO cart_snapshots (list_id, store_id, store_name, total_items, estimated_total)
            VALUES (?, ?, ?, ?, ?)
        """, (list_id, store_id, store_name, len(added_items), estimated_total))
        snapshot_id = cur.lastrowid
        snapshot_ids.append(snapshot_id)

        # Save each item in the snapshot
        for item in added_items:
            cur.execute("""
                INSERT INTO cart_snapshot_items
                    (snapshot_id, item_id, item_name, product_name, product_url, quantity, price, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'system')
            """, (
                snapshot_id,
                item.get("item_id"),
                item.get("item_name", ""),
                item.get("product_name", ""),
                item.get("product_url", ""),
                item.get("qty", 1),
                item.get("price", 0),
            ))

        # Also save failed items as snapshot items with source='failed'
        for item in store_data.get("failed", []):
            cur.execute("""
                INSERT INTO cart_snapshot_items
                    (snapshot_id, item_id, item_name, product_name, quantity, price, source)
                VALUES (?, ?, ?, ?, ?, ?, 'failed')
            """, (
                snapshot_id,
                item.get("item_id"),
                item.get("item_name", ""),
                "",
                0,
                0,
            ))

    conn.commit()
    if close_conn:
        conn.close()

    return snapshot_ids[0] if snapshot_ids else -1


# ============================================================
# ANALYZE GAP
# ============================================================

def analyze_gap(snapshot_id: int, order_id: int, conn=None) -> dict:
    """
    Compare a cart snapshot against an actual order to find gaps.

    Args:
        snapshot_id: ID from cart_snapshots table
        order_id: ID from orders table (the actual purchase)

    Returns dict:
        {
            "snapshot_id": int,
            "order_id": int,
            "store_name": str,
            "gaps": [
                {
                    "item_name": str,
                    "gap_type": "removed"|"added"|"qty_decreased"|"qty_increased"|"substituted"|"matched",
                    "detail": str,
                    "likely_reason": str,
                    "confidence": float,
                    "cart_qty": float, "order_qty": float,
                    "cart_product": str, "order_product": str,
                }
            ],
            "summary": {
                "matched": int,
                "removed": int,
                "added": int,
                "qty_changed": int,
                "substituted": int,
                "match_rate": float,
            }
        }
    """
    ensure_tables()
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    cur = conn.cursor()

    # ── Load cart snapshot ──
    cur.execute("""
        SELECT cs.store_name, cs.store_id, cs.list_id, cs.built_at
        FROM cart_snapshots cs
        WHERE cs.id = ?
    """, (snapshot_id,))
    snapshot = cur.fetchone()
    if not snapshot:
        if close_conn:
            conn.close()
        return {"error": f"Snapshot {snapshot_id} not found"}

    cur.execute("""
        SELECT item_id, item_name, product_name, product_url, quantity, price, source
        FROM cart_snapshot_items
        WHERE snapshot_id = ? AND source = 'system'
    """, (snapshot_id,))
    cart_items = [dict(row) for row in cur.fetchall()]

    # ── Load actual order ──
    cur.execute("""
        SELECT oi.item_id, oi.raw_name, oi.quantity, oi.unit_price, oi.total_price,
               oi.brand, oi.product_url
        FROM order_items oi
        WHERE oi.order_id = ?
    """, (order_id,))
    order_items = [dict(row) for row in cur.fetchall()]

    # ── Match cart items to order items ──
    gaps = []
    matched_order_indices = set()

    for cart_item in cart_items:
        best_match = _find_best_match(cart_item, order_items, matched_order_indices)

        if best_match is not None:
            oi = order_items[best_match]
            matched_order_indices.add(best_match)

            # Check quantities
            cart_qty = cart_item["quantity"] or 1
            order_qty = oi["quantity"] or 1

            # Check product match (substitution?)
            is_substituted = _is_substituted(cart_item, oi)

            if is_substituted:
                gap = _build_gap(
                    cart_item, oi, "substituted",
                    f"Cart: {cart_item['product_name'][:40]} -> Order: {oi['raw_name'][:40]}",
                )
                gap["likely_reason"] = "out_of_stock"
                gap["confidence"] = 0.5
            elif abs(order_qty - cart_qty) >= 0.5:
                if order_qty < cart_qty:
                    gap = _build_gap(
                        cart_item, oi, "qty_decreased",
                        f"Planned {cart_qty}, bought {order_qty}",
                    )
                    gap["likely_reason"] = "still_have_stock"
                    gap["confidence"] = 0.6
                else:
                    gap = _build_gap(
                        cart_item, oi, "qty_increased",
                        f"Planned {cart_qty}, bought {order_qty}",
                    )
                    gap["likely_reason"] = "faster_consumption"
                    gap["confidence"] = 0.6
            else:
                gap = _build_gap(cart_item, oi, "matched", "Purchased as planned")
                gap["likely_reason"] = None
                gap["confidence"] = 1.0

            gaps.append(gap)
        else:
            # Cart item not found in order -> removed
            gap = _build_gap_removed(cart_item, snapshot, order_id, cur)
            gaps.append(gap)

    # ── Check for items in order but NOT in cart -> added manually ──
    for idx, oi in enumerate(order_items):
        if idx not in matched_order_indices:
            gap = _build_gap_added(oi, snapshot, cur)
            gaps.append(gap)

    # ── Save gaps to DB ──
    for gap in gaps:
        cur.execute("""
            INSERT INTO gap_analysis
                (snapshot_id, order_id, item_id, item_name, gap_type, detail,
                 cart_qty, order_qty, cart_price, order_price,
                 cart_product, order_product, likely_reason, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot_id, order_id,
            gap.get("item_id"),
            gap["item_name"],
            gap["gap_type"],
            gap.get("detail"),
            gap.get("cart_qty"),
            gap.get("order_qty"),
            gap.get("cart_price"),
            gap.get("order_price"),
            gap.get("cart_product"),
            gap.get("order_product"),
            gap.get("likely_reason"),
            gap.get("confidence", 0.5),
        ))

    conn.commit()

    # ── Build summary ──
    type_counts = {}
    for g in gaps:
        t = g["gap_type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    total = len(gaps)
    matched_count = type_counts.get("matched", 0)

    result = {
        "snapshot_id": snapshot_id,
        "order_id": order_id,
        "store_name": snapshot["store_name"],
        "gaps": gaps,
        "summary": {
            "matched": matched_count,
            "removed": type_counts.get("removed", 0),
            "added": type_counts.get("added", 0),
            "qty_changed": type_counts.get("qty_decreased", 0) + type_counts.get("qty_increased", 0),
            "substituted": type_counts.get("substituted", 0),
            "match_rate": round(matched_count / total, 2) if total > 0 else 0,
        },
    }

    if close_conn:
        conn.close()

    return result


# ============================================================
# APPLY LEARNINGS
# ============================================================

def apply_learnings(gap_result: dict, conn=None) -> list:
    """
    Take a gap analysis result and emit preference signals for each gap.

    Instead of directly modifying restock_rules, this now feeds everything
    through the preference engine's signal pipeline. The engine handles
    aggregation, blending, and syncing back to restock_rules.

    Only emits signals for gaps with confidence >= 0.4 or user_confirmed = 1.

    Returns list of signals emitted:
        [{"item_name": str, "signal_type": str, "dimension": str, "reason": str}]
    """
    try:
        from preference_engine import emit_signal, process_pending_signals
    except ImportError:
        print("[cart_analysis] preference_engine not available — falling back to legacy")
        return _apply_learnings_legacy(gap_result, conn)

    signals_emitted = []
    order_id = gap_result.get("order_id")
    snapshot_id = gap_result.get("snapshot_id")
    context_base = {"order_id": order_id, "snapshot_id": snapshot_id,
                    "store": gap_result.get("store_name")}

    for gap in gap_result.get("gaps", []):
        item_id = gap.get("item_id")
        if not item_id:
            continue

        gap_type = gap["gap_type"]
        reason = gap.get("likely_reason")
        confidence = gap.get("confidence", 0)

        # Only emit if we have some confidence or user confirmed
        if confidence < 0.4 and not gap.get("user_confirmed"):
            continue

        ctx = {**context_base, "gap_type": gap_type, "likely_reason": reason}

        if gap_type == "matched":
            # Perfect match → passive confirmation of timing + brand
            emit_signal(item_id, "gap_matched", "gap_analysis", "timing",
                        new_value={"confirmed": True},
                        strength=0.4, context=ctx)
            # Also confirms the brand/product choice was correct
            if gap.get("order_product"):
                emit_signal(item_id, "purchase_observed", "gap_analysis", "brand",
                            new_value={"product": gap["order_product"],
                                       "confirmed": True},
                            strength=0.7, context=ctx)
            signals_emitted.append({
                "item_name": gap["item_name"],
                "signal_type": "gap_matched",
                "dimension": "timing+brand",
                "reason": "Cart matched order exactly",
            })

        elif gap_type == "removed":
            # Item was in cart but not purchased
            strength = 0.5 if reason == "still_have_stock" else 0.3
            emit_signal(item_id, "gap_removed", "gap_analysis", "timing",
                        old_value={"cart_qty": gap.get("cart_qty")},
                        new_value={"action": "removed", "reason": reason},
                        strength=strength, context=ctx)
            # Also a weak item_inclusion signal (user didn't want it this time)
            emit_signal(item_id, "gap_removed", "gap_analysis", "item_inclusion",
                        new_value={"action": "removed", "reason": reason},
                        strength=0.3, context=ctx)
            signals_emitted.append({
                "item_name": gap["item_name"],
                "signal_type": "gap_removed",
                "dimension": "timing+inclusion",
                "reason": f"Removed from cart — {reason}",
            })

        elif gap_type in ("qty_decreased", "qty_increased"):
            # Quantity was different than planned
            emit_signal(item_id, "gap_qty_changed", "gap_analysis", "quantity",
                        old_value={"qty": gap.get("cart_qty")},
                        new_value={"qty": gap.get("order_qty")},
                        strength=0.5, context=ctx)
            direction = "less" if gap_type == "qty_decreased" else "more"
            signals_emitted.append({
                "item_name": gap["item_name"],
                "signal_type": "gap_qty_changed",
                "dimension": "quantity",
                "reason": f"Bought {direction}: {gap.get('cart_qty')} → {gap.get('order_qty')}",
            })

        elif gap_type == "substituted":
            # Different product/brand than planned
            emit_signal(item_id, "gap_substituted", "gap_analysis", "product",
                        old_value={"product": gap.get("cart_product")},
                        new_value={"product": gap.get("order_product")},
                        strength=0.6, context=ctx)
            emit_signal(item_id, "gap_substituted", "gap_analysis", "brand",
                        old_value={"product": gap.get("cart_product")},
                        new_value={"product": gap.get("order_product")},
                        strength=0.6, context=ctx)
            signals_emitted.append({
                "item_name": gap["item_name"],
                "signal_type": "gap_substituted",
                "dimension": "product+brand",
                "reason": f"Substituted: {(gap.get('cart_product') or '?')[:30]} → {(gap.get('order_product') or '?')[:30]}",
            })

        elif gap_type == "added":
            # Item was purchased but wasn't in cart
            if reason == "faster_consumption":
                emit_signal(item_id, "gap_added", "gap_analysis", "timing",
                            new_value={"action": "added", "reason": reason},
                            strength=0.5, context=ctx)
            # Record the purchase for brand preference
            if gap.get("order_product"):
                emit_signal(item_id, "purchase_observed", "gap_analysis", "brand",
                            new_value={"product": gap["order_product"]},
                            strength=0.7, context=ctx)
            signals_emitted.append({
                "item_name": gap["item_name"],
                "signal_type": "gap_added",
                "dimension": "timing+brand",
                "reason": f"Added to order — {reason}",
            })

    # Process all the signals we just emitted
    if signals_emitted:
        try:
            processed = process_pending_signals()
            print(f"  [preference_engine] Processed {processed} gap analysis signals")
        except Exception as e:
            print(f"  [preference_engine] Error processing signals: {e}")

    return signals_emitted


def _apply_learnings_legacy(gap_result: dict, conn=None) -> list:
    """
    Legacy fallback: directly update restock_rules if preference_engine
    is not available. Kept for backward compatibility.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    cur = conn.cursor()
    adjustments = []

    for gap in gap_result.get("gaps", []):
        item_id = gap.get("item_id")
        if not item_id:
            continue

        gap_type = gap["gap_type"]
        reason = gap.get("likely_reason")
        confidence = gap.get("confidence", 0)

        if confidence < 0.6 and not gap.get("user_confirmed"):
            continue

        if gap_type == "matched":
            cur.execute("""
                UPDATE restock_rules
                SET confidence = MIN(confidence + 0.05, 1.0),
                    updated_at = datetime('now')
                WHERE item_id = ?
            """, (item_id,))
            if cur.rowcount:
                adjustments.append({
                    "item_name": gap["item_name"],
                    "field": "confidence",
                    "change": "+0.05",
                    "reason": "Cart matched order exactly",
                })

        elif gap_type == "removed" and reason == "still_have_stock":
            cur.execute("SELECT avg_days_between FROM restock_rules WHERE item_id = ?", (item_id,))
            row = cur.fetchone()
            if row and row["avg_days_between"]:
                old_days = row["avg_days_between"]
                new_days = round(old_days * 1.15, 1)
                cur.execute("""
                    UPDATE restock_rules
                    SET avg_days_between = ?,
                        confidence = MAX(confidence - 0.05, 0.1),
                        updated_at = datetime('now')
                    WHERE item_id = ?
                """, (new_days, item_id))
                adjustments.append({
                    "item_name": gap["item_name"],
                    "field": "avg_days_between",
                    "old": old_days,
                    "new": new_days,
                    "reason": "Still had stock — stretching restock interval by 15%",
                })

        elif gap_type == "qty_decreased" and reason == "still_have_stock":
            cur.execute("SELECT avg_quantity FROM restock_rules WHERE item_id = ?", (item_id,))
            row = cur.fetchone()
            if row and row["avg_quantity"]:
                old_qty = row["avg_quantity"]
                order_qty = gap.get("order_qty", old_qty)
                new_qty = round(old_qty * 0.7 + order_qty * 0.3, 1)
                cur.execute("""
                    UPDATE restock_rules
                    SET avg_quantity = ?,
                        updated_at = datetime('now')
                    WHERE item_id = ?
                """, (new_qty, item_id))
                adjustments.append({
                    "item_name": gap["item_name"],
                    "field": "avg_quantity",
                    "old": old_qty,
                    "new": new_qty,
                    "reason": f"Bought {order_qty} instead of {gap.get('cart_qty')} — blending into average",
                })

        elif gap_type == "qty_increased" and reason == "faster_consumption":
            cur.execute("SELECT avg_quantity FROM restock_rules WHERE item_id = ?", (item_id,))
            row = cur.fetchone()
            if row and row["avg_quantity"]:
                old_qty = row["avg_quantity"]
                order_qty = gap.get("order_qty", old_qty)
                new_qty = round(old_qty * 0.7 + order_qty * 0.3, 1)
                cur.execute("""
                    UPDATE restock_rules
                    SET avg_quantity = ?,
                        updated_at = datetime('now')
                    WHERE item_id = ?
                """, (new_qty, item_id))
                adjustments.append({
                    "item_name": gap["item_name"],
                    "field": "avg_quantity",
                    "old": old_qty,
                    "new": new_qty,
                    "reason": f"Bought {order_qty} instead of {gap.get('cart_qty')} — increasing average",
                })

        elif gap_type == "added" and reason == "faster_consumption":
            cur.execute("SELECT avg_days_between FROM restock_rules WHERE item_id = ?", (item_id,))
            row = cur.fetchone()
            if row and row["avg_days_between"]:
                old_days = row["avg_days_between"]
                new_days = round(old_days * 0.85, 1)
                cur.execute("""
                    UPDATE restock_rules
                    SET avg_days_between = ?,
                        confidence = MAX(confidence - 0.05, 0.1),
                        updated_at = datetime('now')
                    WHERE item_id = ?
                """, (new_days, item_id))
                adjustments.append({
                    "item_name": gap["item_name"],
                    "field": "avg_days_between",
                    "old": old_days,
                    "new": new_days,
                    "reason": "Needed sooner than predicted — shrinking restock interval by 15%",
                })

    conn.commit()
    if close_conn:
        conn.close()

    return adjustments


# ============================================================
# GAP HISTORY
# ============================================================

def get_gap_history(item_id: int = None, limit: int = 20, conn=None) -> list:
    """
    Get historical gap analysis results, optionally filtered by item.

    Returns list of gap records with type/reason/dates for pattern analysis.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    cur = conn.cursor()

    if item_id:
        cur.execute("""
            SELECT ga.*, cs.store_name, cs.built_at,
                   o.order_date, o.total_amount AS order_total
            FROM gap_analysis ga
            JOIN cart_snapshots cs ON ga.snapshot_id = cs.id
            LEFT JOIN orders o ON ga.order_id = o.id
            WHERE ga.item_id = ?
            ORDER BY ga.analyzed_at DESC
            LIMIT ?
        """, (item_id, limit))
    else:
        cur.execute("""
            SELECT ga.*, cs.store_name, cs.built_at,
                   o.order_date, o.total_amount AS order_total
            FROM gap_analysis ga
            JOIN cart_snapshots cs ON ga.snapshot_id = cs.id
            LEFT JOIN orders o ON ga.order_id = o.id
            ORDER BY ga.analyzed_at DESC
            LIMIT ?
        """, (limit,))

    rows = [dict(row) for row in cur.fetchall()]

    if close_conn:
        conn.close()

    return rows



# ============================================================
# INTERNAL HELPERS
# ============================================================

def _find_best_match(cart_item, order_items, already_matched):
    """
    Find the best matching order item for a cart item.
    Uses item_id first, then fuzzy name matching.
    """
    cart_id = cart_item.get("item_id")
    cart_name = (cart_item.get("item_name") or "").lower()
    cart_product = (cart_item.get("product_name") or "").lower()

    best_idx = None
    best_score = 0

    for idx, oi in enumerate(order_items):
        if idx in already_matched:
            continue

        score = 0

        # item_id match is strongest signal
        if cart_id and oi.get("item_id") and cart_id == oi["item_id"]:
            score += 100

        # Name overlap
        oi_name = (oi.get("raw_name") or "").lower()
        cart_words = set(w for w in cart_name.split() if len(w) > 2)
        oi_words = set(w for w in oi_name.split() if len(w) > 2)

        if cart_words and oi_words:
            overlap = len(cart_words & oi_words)
            score += overlap * 10

        # Product name overlap (more specific)
        if cart_product:
            product_words = set(w for w in cart_product.split() if len(w) > 2)
            product_overlap = len(product_words & oi_words)
            score += product_overlap * 5

        # URL match
        cart_url = cart_item.get("product_url", "")
        oi_url = oi.get("product_url", "")
        if cart_url and oi_url and cart_url == oi_url:
            score += 50

        if score > best_score and score >= 10:  # minimum threshold
            best_score = score
            best_idx = idx

    return best_idx


def _is_substituted(cart_item, order_item):
    """Check if the order item is a different product than the cart item."""
    cart_product = (cart_item.get("product_name") or "").lower()
    order_product = (order_item.get("raw_name") or "").lower()

    if not cart_product or not order_product:
        return False

    # If they share the same item_id but different product names -> substitution
    cart_words = set(w for w in cart_product.split() if len(w) > 3)
    order_words = set(w for w in order_product.split() if len(w) > 3)

    if not cart_words or not order_words:
        return False

    overlap = len(cart_words & order_words)
    similarity = overlap / max(len(cart_words), len(order_words))

    # If less than 50% word overlap on the specific product name -> substitution
    return similarity < 0.5


def _build_gap(cart_item, order_item, gap_type, detail):
    """Build a gap dict from a matched cart+order pair."""
    return {
        "item_id": cart_item.get("item_id"),
        "item_name": cart_item.get("item_name", "?"),
        "gap_type": gap_type,
        "detail": detail,
        "cart_qty": cart_item.get("quantity"),
        "order_qty": order_item.get("quantity"),
        "cart_price": cart_item.get("price"),
        "order_price": order_item.get("unit_price") or order_item.get("total_price"),
        "cart_product": cart_item.get("product_name"),
        "order_product": order_item.get("raw_name"),
    }


def _build_gap_removed(cart_item, snapshot, order_id, cur):
    """
    Build a gap for an item that was in cart but not in the order.
    Try to guess why it was removed.
    """
    item_id = cart_item.get("item_id")
    reason = "unknown"
    confidence = 0.4

    if item_id:
        # Check if this item appeared in a different order around the same time
        # (bought elsewhere / different store)
        built_at = snapshot["built_at"] or datetime.now().isoformat()
        cur.execute("""
            SELECT o.id, o.store_id, s.name AS store_name, oi.raw_name
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
            JOIN stores s ON o.store_id = s.id
            WHERE oi.item_id = ?
              AND o.id != ?
              AND o.order_date BETWEEN date(?, '-7 days') AND date(?, '+7 days')
        """, (item_id, order_id, built_at, built_at))

        other_orders = cur.fetchall()
        if other_orders:
            reason = "bought_elsewhere"
            confidence = 0.7

        # Check if the item was also removed in recent gap analyses
        # (pattern of over-ordering)
        cur.execute("""
            SELECT COUNT(*) as cnt
            FROM gap_analysis
            WHERE item_id = ? AND gap_type = 'removed'
            ORDER BY analyzed_at DESC
            LIMIT 5
        """, (item_id,))
        removed_count = cur.fetchone()
        if removed_count and removed_count[0] >= 2:
            reason = "still_have_stock"
            confidence = 0.7

    return {
        "item_id": item_id,
        "item_name": cart_item.get("item_name", "?"),
        "gap_type": "removed",
        "detail": f"Was in cart ({cart_item.get('product_name', '?')[:40]}) but not purchased",
        "cart_qty": cart_item.get("quantity"),
        "order_qty": 0,
        "cart_price": cart_item.get("price"),
        "order_price": None,
        "cart_product": cart_item.get("product_name"),
        "order_product": None,
        "likely_reason": reason,
        "confidence": confidence,
    }


def _build_gap_added(order_item, snapshot, cur):
    """
    Build a gap for an item that appeared in the order but wasn't in the cart.
    Try to guess why.
    """
    item_id = order_item.get("item_id")
    reason = "unknown"
    confidence = 0.4

    if item_id:
        # Check if this item has restock rules (known habit item)
        cur.execute("""
            SELECT r.avg_days_between, r.last_ordered_date, r.next_restock_date
            FROM restock_rules r
            WHERE r.item_id = ?
        """, (item_id,))
        rule = cur.fetchone()

        if rule:
            # Known item with restock rules — it was missed from the list
            if rule["next_restock_date"]:
                next_date = rule["next_restock_date"]
                built_at = snapshot["built_at"] or datetime.now().isoformat()
                # If next restock was in the future, they consumed faster
                if next_date > built_at[:10]:
                    reason = "faster_consumption"
                    confidence = 0.7
                else:
                    reason = "forgot_to_add"
                    confidence = 0.5
        else:
            # Unknown item — might need to add to habits
            reason = "missing_habit"
            confidence = 0.6
    else:
        # No item_id — completely new/untracked
        reason = "one_off"
        confidence = 0.5

    return {
        "item_id": item_id,
        "item_name": order_item.get("raw_name", "?"),
        "gap_type": "added",
        "detail": f"Not in cart but purchased: {order_item.get('raw_name', '?')[:40]}",
        "cart_qty": 0,
        "order_qty": order_item.get("quantity"),
        "cart_price": None,
        "order_price": order_item.get("unit_price") or order_item.get("total_price"),
        "cart_product": None,
        "order_product": order_item.get("raw_name"),
        "likely_reason": reason,
        "confidence": confidence,
    }


def _generate_suggestion(type_counts, reason_counts, avg_drift, total):
    """Generate a plain-english suggestion based on gap patterns."""
    matched = type_counts.get("matched", 0)
    removed = type_counts.get("removed", 0)
    added = type_counts.get("added", 0)

    if total == 0:
        return "No data yet"

    match_rate = matched / total

    if match_rate >= 0.85:
        return "Predictions are very accurate — keep current settings"

    suggestions = []

    if removed >= 3 and reason_counts.get("still_have_stock", 0) >= 2:
        suggestions.append("Items frequently removed — consider stretching restock intervals")

    if removed >= 2 and reason_counts.get("bought_elsewhere", 0) >= 2:
        suggestions.append("Items often bought elsewhere — might need multi-store tracking")

    if added >= 3 and reason_counts.get("faster_consumption", 0) >= 2:
        suggestions.append("Items added manually often — consumption is faster than predicted")

    if added >= 2 and reason_counts.get("missing_habit", 0) >= 2:
        suggestions.append("Untracked items keep appearing — review habits list")

    if abs(avg_drift) > 0.5:
        direction = "more" if avg_drift > 0 else "less"
        suggestions.append(f"Consistently buying {direction} than planned (avg drift: {avg_drift:+.1f})")

    return "; ".join(suggestions) if suggestions else "Some mismatches — review individual items"


# ============================================================
# PRETTY PRINT
# ============================================================


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":

    ensure_tables()
    print("Cart analysis tables created/verified.")

    # Quick demo: show what the module can do
    print(f"\n{'='*60}")
    print("  CART ANALYSIS MODULE")
    print(f"{'='*60}")
    print("""
  This module tracks the gap between what the system plans
  and what you actually buy. Over time, it learns:

  - If items keep getting removed -> stretch restock intervals
  - If items get added manually  -> shorten intervals / add to habits
  - If quantities drift          -> recalibrate consumption rates
  - If products get swapped      -> update preferred alternatives

  How it works:
    1. cart_builder saves a snapshot after building carts
    2. After you place the order, we scrape what was actually purchased
    3. analyze_gap() compares the two and classifies each difference
    4. apply_learnings() pushes adjustments into restock_rules

  Signals:

  Gap types:
""")
    for gap_type, desc in GAP_TYPES.items():
        print(f"    {gap_type:20s}  {desc}")

    print(f"\n  Removal reasons:")
    for reason_id, info in REMOVAL_REASONS.items():
        print(f"    {reason_id:25s}  {info['label']}")

    print(f"\n  Addition reasons:")
    for reason_id, info in ADDITION_REASONS.items():
        print(f"    {reason_id:25s}  {info['label']}")

    # Check if we have any existing snapshots
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as cnt FROM cart_snapshots")
    snapshot_count = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) as cnt FROM gap_analysis")
    gap_count = cur.fetchone()["cnt"]

    print(f"\n  Current data:")
    print(f"    Cart snapshots: {snapshot_count}")
    print(f"    Gap analyses:   {gap_count}")

    conn.close()
    print(f"\n{'='*60}")
