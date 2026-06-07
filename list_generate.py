"""
AutoRestock - Shopping List Generator (Slice 7)

The brain that decides WHAT to buy and WHEN.

Logic:
  1. Look at restock_rules: which items have next_restock_date <= target_date?
  2. For each due item, pull quantity from avg_quantity
  3. Assign to the right store via preferred_store_id
  4. Create a shopping_list + list_items in the DB
  5. Return a grouped-by-store summary for the check-in

This is the core of the prediction engine. Everything else
(scraping, cart building, WhatsApp) flows from this list.

Usage:
  python3 list_generate.py                     # generate for next order date (Tue or Fri)
  python3 list_generate.py --date 2026-02-24   # generate for specific date
  python3 list_generate.py --dry-run            # preview without saving to DB
  python3 list_generate.py --show               # show the most recent list
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from db_setup import DB_PATH, create_database
from order_scraper import sync_restock_dates_from_deliveries


# ────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _next_order_date():
    """
    Next Tuesday or Friday from today.
    Our household orders twice a week: Tue + Fri.
    Each order covers ~3.5 days of consumption.
    """
    today = datetime.now().date()
    for d in range(0, 8):  # include today if it's an order day
        candidate = today + timedelta(days=d)
        if candidate.weekday() in (1, 4):  # Tue=1, Fri=4
            return candidate.strftime("%Y-%m-%d")
    # Fallback: 3 days from now
    return (today + timedelta(days=3)).strftime("%Y-%m-%d")


def _day_name(date_str):
    """'2026-02-24' -> 'Tuesday'"""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
    except (ValueError, TypeError):
        return ""


def _inventory_suggest_quantity(
    avg_quantity,
    avg_days_between,
    last_ordered_date,
    today_dt,
    target_dt,
    coverage_window_days,
    last_order_qty=None,
):
    """The inventory model: how many units (if any) does the household
    need to order to cover the gap from `target_dt` to the next order
    after it?

    Returns (qty_to_order, reason_string).
      qty_to_order == 0  -> skip the item; reason explains why (e.g.
                            "stocked:Xd" meaning the projected on-hand
                            stock lasts X days past target)
      qty_to_order >= 1  -> include; reason == "DUE"

    Model (see docs/suggestion_model_2026_05_27.md for the full design):

      daily_consumption = avg_quantity / avg_days_between
      inventory_today   = max(0, avg_quantity
                              - daily_consumption * days_since_last_order)
      inventory_at_target = max(0, inventory_today
                              - daily_consumption * days_until_target)
      days_inv_lasts_past_target = inventory_at_target / daily_consumption

      if days_inv_lasts_past_target >= coverage_window_days:
          # Stock lasts the whole window past the target order. Skip.
          return (0, f"stocked:{N}d")
      short_days = coverage_window_days - days_inv_lasts_past_target
      qty_to_order = max(1, ceil(short_days * daily_consumption))

    The "days the stock lasts" framing avoids a unit-threshold trap: for
    a low-cadence item (e.g. yogurt, 0.29 units/day) a 0.2-unit shortage
    is still half a day of going without -- worth ordering one more.

    Args:
        avg_quantity: avg units per order (restock_rules.avg_quantity).
            Used to compute the daily consumption rate.
        avg_days_between: cadence (restock_rules.avg_days_between).
        last_ordered_date: 'YYYY-MM-DD' string from restock_rules.
        today_dt: date.today(), passed in for test-determinism.
        target_dt: the order's target_date as a date object.
        coverage_window_days: days from target_dt to the next order after
            it (typically 3 or 4 for Tue/Fri cadence).
        last_order_qty: optional. The actual quantity bought on the most
            recent order. When passed, used as the inventory baseline
            (since "you bought 6 cucumbers" is a better stock estimate
            than "you typically buy 2"). Falls back to avg_quantity when
            None. Order qty is in restock-rule units, same scale as
            avg_quantity.
    """
    import math
    try:
        cadence = float(avg_days_between or 0)
    except (TypeError, ValueError):
        return (0, "bad_cadence")
    if cadence <= 0:
        return (0, "bad_cadence")
    try:
        avg_qty = float(avg_quantity or 0)
    except (TypeError, ValueError):
        avg_qty = 1.0
    if avg_qty <= 0:
        avg_qty = 1.0
    daily_consumption = avg_qty / cadence
    if daily_consumption <= 0:
        return (0, "no_consumption")

    try:
        last_dt = datetime.strptime(last_ordered_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return (0, "no_last_ordered")
    days_since_last = max(0, (today_dt - last_dt).days)

    # Inventory baseline: prefer the actual last-order quantity (truth),
    # fall back to the long-run avg when the join couldn't find an
    # order_items row. Defensive max(0) in case data is corrupt.
    try:
        baseline_qty = (
            float(last_order_qty)
            if last_order_qty is not None
            else avg_qty
        )
    except (TypeError, ValueError):
        baseline_qty = avg_qty
    if baseline_qty <= 0:
        baseline_qty = avg_qty

    inventory_today = max(0.0, baseline_qty - daily_consumption * days_since_last)
    days_until_target = max(0, (target_dt - today_dt).days)
    inventory_at_target = max(
        0.0, inventory_today - daily_consumption * days_until_target
    )
    # Days the stock at target lasts past the target order.
    days_inv_lasts_past_target = inventory_at_target / daily_consumption

    # If stock at target carries us all the way to the next order (or
    # past it), no need to top up. Use a tiny epsilon so floating-point
    # noise doesn't matter -- exact ties land on "skip" too.
    EPS = 1e-6
    if days_inv_lasts_past_target >= coverage_window_days - EPS:
        return (0, f"stocked:{int(round(days_inv_lasts_past_target))}d")

    short_days = coverage_window_days - days_inv_lasts_past_target
    coverage_qty = max(1, math.ceil(short_days * daily_consumption))

    # Quantity policy (2026-06-04): suggest the household's TYPICAL per-order
    # purchase, not a full coverage top-up. avg_quantity already encodes how
    # much they actually buy each shop, so topping up to cover the whole
    # window over-ordered fast staples -- milk/bread/bananas they buy 1 of
    # were showing 2-3. So:
    #   * never suggest MORE than the usual purchase (cap at typical_qty),
    #   * suggest LESS when fresh stock means less is needed (coverage_qty),
    #   * exceed the usual amount only by ONE, and only on a severe shortfall
    #     (projected need >= 2x the usual purchase over the window -> a clear
    #     run-out), so we still protect against running out without inflating
    #     every fast item.
    # min(coverage, typical) preserves bulk items (a 6-cucumber buyer still
    # gets up to ~6 when genuinely short, but only 1 right after a big buy).
    typical_qty = max(1, int(round(avg_qty)))
    if coverage_qty <= typical_qty:
        qty = coverage_qty
    elif coverage_qty >= 2 * typical_qty + 1:
        qty = typical_qty + 1
    else:
        qty = typical_qty
    return (qty, "DUE")


def _coverage_cutoff(target_date):
    """
    Calculate the date this order needs to cover until.

    Order days are Tue and Fri. A Tuesday order covers until the next Friday,
    a Friday order covers until the next Tuesday. Include items due anytime
    before the next order so nothing runs out between deliveries.

    Example: ordering Tue Feb 24 → cover until Fri Feb 27
             ordering Fri Feb 27 → cover until Tue Mar 3
    """
    try:
        dt = datetime.strptime(target_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        # Fallback: target_date + 4 days
        return target_date

    # Find the next order day AFTER this one
    for d in range(1, 8):
        candidate = dt + timedelta(days=d)
        if candidate.weekday() in (1, 4):  # Tue=1, Fri=4
            return candidate.strftime("%Y-%m-%d")

    # Fallback
    return (dt + timedelta(days=4)).strftime("%Y-%m-%d")


# ────────────────────────────────────────────
# Core: list_generate
# ────────────────────────────────────────────

def _consolidate_lone_vendors(list_id, conn):
    """Don't strand a single item at a vendor — you can't place a one-item order.

    If a store ends up with exactly ONE item and that item is also available at
    the store holding the MOST items, move it there so each vendor's basket is
    worth ordering. Items with no in-stock product at the main store stay put
    (they genuinely need that vendor). Switches the store only; the product
    re-resolves at the new store on render/build. Best-effort. Returns # moved.
    """
    from collections import Counter
    rows = conn.execute(
        """SELECT li.id AS li_id, li.item_id, li.store_id, s.name AS store_name
           FROM list_items li LEFT JOIN stores s ON li.store_id = s.id
           WHERE li.list_id = ?
             AND COALESCE(li.status,'') != 'skipped'
             AND COALESCE(li.user_action,'') != 'skipped'
             AND li.store_id IS NOT NULL""",
        (list_id,),
    ).fetchall()
    counts = Counter(r["store_id"] for r in rows)
    if len(counts) < 2:
        return 0  # already a single vendor — nothing to consolidate
    main_store_id = counts.most_common(1)[0][0]
    moved = 0
    for r in rows:
        sid = r["store_id"]
        if sid == main_store_id or counts.get(sid, 0) != 1 or not r["item_id"]:
            continue
        # Pick from the curated (noun-matched, non-avoid) alternatives at the
        # main store, but DON'T trust item_alternatives.rank for the price
        # decision -- that table is ranked by a separate scorer
        # (product_knowledge.score_and_rank_alternatives) that can rank a
        # wildly-overpriced pack #1 (a 15-egg Tregor @ 72.70 outranked sane
        # ~30 AED eggs). Prefer organic, then the household's typical price
        # (closest to the item's real avg unit_price), then cheapest -- so the
        # consolidated line lands on a sane product, not an outlier.
        avg_paid = conn.execute(
            """SELECT AVG(oi.unit_price) FROM order_items oi
               JOIN orders o ON o.id = oi.order_id
               WHERE oi.item_id = ? AND oi.unit_price > 0
                 AND o.status IN ('delivered','completed')""",
            (r["item_id"],),
        ).fetchone()[0]
        best = conn.execute(
            """SELECT ia.catalog_id
               FROM item_alternatives ia
               JOIN product_catalog pc ON ia.catalog_id = pc.id
               WHERE ia.item_id = ? AND ia.store_id = ?
                 AND COALESCE(ia.tier, '') != 'avoid'
                 AND pc.is_available = 1 AND COALESCE(pc.price, 0) > 0
               ORDER BY pc.is_organic DESC,
                        ABS(pc.price - COALESCE(?, pc.price)) ASC,
                        pc.price ASC
               LIMIT 1""",
            (r["item_id"], main_store_id, avg_paid),
        ).fetchone()
        if not best:
            continue  # not available at the main store — leave it where it is
        # Pin the best-ranked in-stock product AT THE MAIN STORE instead of
        # NULLing catalog_id. A NULL pin made build_frontend_list free-text
        # re-match the moved item at RENDER time -- with no quality/price
        # scoring -- which landed on a wrong/overpriced product (eggs moved
        # Carrefour->Kibsons rendered a 15-pack Tregor @ 72.70 instead of a
        # sane Kibsons egg the validator would have chosen). Carry a real pin.
        conn.execute(
            "UPDATE list_items SET store_id = ?, catalog_id = ? WHERE id = ?",
            (main_store_id, best["catalog_id"], r["li_id"]),
        )
        counts[sid] -= 1
        counts[main_store_id] = counts.get(main_store_id, 0) + 1
        moved += 1
        try:
            from app_logging import log_event
            log_event("vendor_consolidated", source="list_generate",
                      detail=f"item={r['item_id']} from {r['store_name']} -> main {main_store_id}")
        except Exception:
            pass
    if moved:
        conn.commit()
    return moved


def list_generate(target_date=None, dry_run=False, conn=None):
    """
    Generate a shopping list for a target date.

    Steps:
      1. Find all items where next_restock_date <= target_date
      2. Pull avg_quantity for each
      3. Group by store
      4. Save to shopping_lists + list_items tables (unless dry_run)
      5. Return structured result

    Returns dict:
      {
        "list_id": int or None (if dry_run),
        "target_date": str,
        "day_name": str,
        "items": [
          {
            "item_id": int,
            "item_name": str,
            "store_name": str,
            "store_id": int,
            "quantity": float,
            "estimated_price": float or None,
            "category": str,
            "notes": str,
            "next_restock_date": str,
            "confidence": float,
          }
        ],
        "by_store": {
          "Kibsons": [items...],
          "Carrefour UAE": [items...]
        },
        "summary": {
          "total_items": int,
          "total_estimated": float,
          "stores": { "Kibsons": {"count": N, "estimated": X}, ... }
        }
      }
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    if target_date is None:
        target_date = _next_order_date()

    # Sync restock dates from any recent deliveries before generating the list.
    # This ensures items that were just delivered don't appear prematurely.
    try:
        sync_restock_dates_from_deliveries(conn)
    except Exception as e:
        print(f"  [list_generate] Restock sync warning: {e}")

    in_flight_items = set()

    # Refresh observed cadences from actual order history. Without this,
    # restock_rules.avg_days_between stays pinned to the hardcoded HABITS
    # values (3.5 days for nearly everything), which is 3-10x more
    # aggressive than what the household actually buys -- making the
    # predictor over-eager and the resulting lists either weirdly short
    # (when restore-on-cart bypasses regen) or weirdly bloated.
    try:
        from cadence_calibration import refresh_observed_cadences
        cad_stats = refresh_observed_cadences(conn=conn)
        print(f"  [list_generate] Cadence refresh: updated={cad_stats['updated']} "
              f"clamped={cad_stats['clamped']} kept_hardcoded={cad_stats['kept_hardcoded']}")
    except Exception as e:
        print(f"  [list_generate] Cadence refresh failed (non-fatal): {e}")

    # ── Forward-only last_ordered correctors (run AFTER cadence refresh) ──
    # refresh recomputes last_ordered from captured order history, which is
    # incomplete (Carrefour line-items often don't scrape). These two
    # passes correct it from more-reliable signals, and MUST run after the
    # refresh so it can't clobber them back to a stale captured date:
    #   1. delivery sequence: anchor on the actual delivery date of recent
    #      orders + collect in-flight (incoming) items to suppress.
    #   2. finalized-list backfill: advance last_ordered to the most recent
    #      finalized list that contained the item (we built those lists, so
    #      they're a reliable record of what was ordered when).
    try:
        in_flight_items = _apply_delivery_sequence(
            conn, datetime.now().strftime("%Y-%m-%d")
        )
    except Exception as e:
        print(f"  [list_generate] delivery-sequence warning: {e}")
    try:
        _backfilled = backfill_last_ordered_from_lists(conn)
        if _backfilled:
            print(f"  [list_generate] corrected last_ordered for "
                  f"{len(_backfilled)} item(s) from finalized-list history")
    except Exception as e:
        print(f"  [list_generate] finalized-list backfill warning: {e}")

    # Calculate coverage window: this order should cover until the NEXT order.
    # E.g., Tuesday order covers until Friday (3 days), Friday covers until Tuesday (4 days).
    # Include items due anytime before the next order after this one.
    coverage_cutoff = _coverage_cutoff(target_date)

    cur = conn.cursor()

    # ── Step 1: candidates (patterns we still buy) ──
    #
    # Hard pre-filters before the inventory math runs:
    #   - times_ordered >= 3: three purchases is the floor for "this is a
    #     pattern, not a one-shot." Items below this stay invisible until
    #     they earn pattern status.
    #   - last_ordered within MAX(4x avg_days_between, 90 days): not lapsed.
    #     90-day floor protects weekly items from ingestion droughts
    #     (see 2026-05-27 commit + docs/suggestion_model_2026_05_27.md).
    #   - stop_suggesting preference honored (Tier-1/Tier-2 drop detector).
    #
    # Inventory model (replaces the old next_restock_date <= coverage_cutoff
    # gate). See docs/suggestion_model_2026_05_27.md for the full design.
    # In one sentence: project inventory forward from the last purchase
    # using avg_quantity / avg_days_between as the daily consumption rate,
    # ask if the projected inventory covers the gap from this order to the
    # next, suggest only what's needed to close that gap.
    # Correlated subquery on last_order_qty -- the actual quantity bought on
    # the most recent order containing this item. The inventory model uses
    # this as the starting stock so an anomalously-large purchase (e.g.
    # 6 cucumbers when you normally buy 2) correctly tracks lots of stock.
    # Falls back to avg_quantity if there's no order_items row (shouldn't
    # happen for items that pass the times_ordered >= 3 gate, but defensive).
    cur.execute("""
        SELECT
            i.id AS item_id,
            i.name AS item_name,
            i.category,
            i.notes,
            i.avg_price,
            i.search_terms,
            i.preferred_store_id AS store_id,
            s.name AS store_name,
            r.avg_quantity,
            r.avg_days_between,
            r.next_restock_date,
            r.last_ordered_date,
            r.confidence,
            r.times_ordered,
            (SELECT oi.quantity
               FROM order_items oi
               JOIN orders o ON o.id = oi.order_id
              WHERE oi.item_id = i.id
              ORDER BY o.order_date DESC, o.id DESC
              LIMIT 1) AS last_order_qty
        FROM restock_rules r
        JOIN items i ON r.item_id = i.id
        JOIN stores s ON i.preferred_store_id = s.id
        WHERE r.avg_days_between IS NOT NULL
          AND r.avg_days_between > 0
          AND r.last_ordered_date IS NOT NULL
          AND r.confidence >= 0.3
          AND COALESCE(r.times_ordered, 0) >= 3
          AND julianday(date('now')) - julianday(r.last_ordered_date)
              <= MAX(r.avg_days_between * 4, 90)
          AND NOT EXISTS (
              SELECT 1 FROM user_item_prefs uip
              WHERE uip.item_id = i.id
                AND uip.pref_type = 'stop_suggesting'
                AND uip.is_active = 1
          )
        ORDER BY s.name, r.confidence DESC, i.name
    """)

    raw_candidates = cur.fetchall()

    # ── Step 1a: inventory-based filter ──
    # For each candidate compute the qty_to_order via the inventory model.
    # Anything that returns qty_to_order == 0 ("stocked") drops out.
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        target_dt = datetime.now().date()
    try:
        cutoff_dt = datetime.strptime(coverage_cutoff, "%Y-%m-%d").date()
        coverage_window_days = max(1, (cutoff_dt - target_dt).days)
    except (ValueError, TypeError):
        coverage_window_days = 4  # safe default: a Friday → next Tuesday span
    today_dt = datetime.now().date()

    due_items = []
    stocked_count = 0
    for row in raw_candidates:
        qty_to_order, reason = _inventory_suggest_quantity(
            avg_quantity=row["avg_quantity"],
            avg_days_between=row["avg_days_between"],
            last_ordered_date=row["last_ordered_date"],
            today_dt=today_dt,
            target_dt=target_dt,
            coverage_window_days=coverage_window_days,
            last_order_qty=row["last_order_qty"],
        )
        if qty_to_order > 0:
            # Carry qty_to_order forward as a synthetic column so Step 2 can
            # use the inventory math result instead of avg_quantity. sqlite3.Row
            # is read-only, so wrap as a dict.
            row_d = dict(row)
            row_d["_inventory_qty"] = qty_to_order
            row_d["_inventory_reason"] = reason
            due_items.append(row_d)
        else:
            stocked_count += 1
    if stocked_count:
        print(f"  [inventory_model] {stocked_count} candidate items had enough stock; "
              f"skipped. {len(due_items)} items still need restocking.")

    # ── Step 1b: Delivery cooldown filter ──
    # Don't include items that were delivered very recently (within 48h).
    # Even if they're "due" within the coverage window, re-ordering items
    # that just arrived feels wrong to the user. They'll appear on the NEXT order.
    recent_delivery_items = set()
    try:
        cooldown_cutoff = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
        recent = cur.execute("""
            SELECT DISTINCT oi.item_id
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
            WHERE o.delivery_date IS NOT NULL
              AND o.delivery_date >= ?
              AND oi.item_id IS NOT NULL
        """, (cooldown_cutoff,)).fetchall()
        recent_delivery_items = {r["item_id"] for r in recent}
        if recent_delivery_items:
            print(f"  [cooldown] {len(recent_delivery_items)} items delivered within 48h of {target_date}, excluding from list")
    except Exception as e:
        print(f"  [cooldown] Warning: {e}")

    # ── In-flight suppression (delivery-anchored model) ──
    # Items on an order that is placed but NOT yet delivered are "incoming"
    # -- don't re-suggest them. _apply_delivery_sequence (run at the top of
    # list_generate) returns that set. The consumption clock for delivered
    # items is anchored on the DELIVERY date there; here we just exclude the
    # in-flight ones from this cycle.
    if in_flight_items:
        before = len(recent_delivery_items)
        recent_delivery_items |= in_flight_items
        added = len(recent_delivery_items) - before
        if added:
            print(f"  [in-flight] suppressing {added} item(s) on orders "
                  f"pending delivery")

    # ── Step 2: Build item list with quantities ──
    items = []
    by_store = {}
    total_estimated = 0.0

    # prefer_variety categories (from delivered-order feedback, e.g. "buy
    # varied pasta"). v1: cap a single product to qty 1 in these categories
    # so you don't get 2x the same -- variety then comes from the distinct
    # items already on the list. (Richer "split N across variants" is a
    # follow-up.)
    try:
        from household_rules import get_variety_categories, category_prefers_variety
        _variety_cats = get_variety_categories(conn)
    except Exception:
        _variety_cats = []
        category_prefers_variety = lambda *a, **k: False  # noqa: E731

    for row in due_items:
        # Skip items in delivery cooldown period
        if row["item_id"] in recent_delivery_items:
            continue

        # Prefer the inventory model's qty_to_order; fall back to avg_quantity
        # for safety if the inventory math didn't run (shouldn't happen --
        # every row in due_items has _inventory_qty -- but defensive).
        quantity = (
            int(row.get("_inventory_qty", 0))
            if isinstance(row, dict)
            else 0
        )
        if quantity <= 0:
            quantity = max(1, round(row["avg_quantity"] or 1))

        # prefer_variety: don't suggest N× the same product in a category
        # the household wants varied (e.g. "2 lentil pasta" -> 1). Variety
        # across the category comes from the other distinct items on the list.
        if quantity > 1 and _variety_cats and category_prefers_variety(
            row["category"] or "", row["item_name"] or "", _variety_cats
        ):
            quantity = 1

        price_each = row["avg_price"] or 0
        estimated_total = round(quantity * price_each, 2)
        total_estimated += estimated_total

        # Parse search_terms JSON and get the terms for THIS store
        all_search_terms = {}
        if row["search_terms"]:
            try:
                all_search_terms = json.loads(row["search_terms"])
            except (json.JSONDecodeError, TypeError):
                pass
        store_search_terms = all_search_terms.get(row["store_name"], [])

        item = {
            "item_id": row["item_id"],
            "item_name": row["item_name"],
            "store_name": row["store_name"],
            "store_id": row["store_id"],
            "quantity": quantity,
            "estimated_price": estimated_total if price_each > 0 else None,
            "price_each": price_each if price_each > 0 else None,
            "category": row["category"] or "Other",
            "notes": row["notes"] or "",
            "search_terms": store_search_terms,  # what to type into the store search bar
            "all_search_terms": all_search_terms,  # all stores (for fallback/comparison)
            "next_restock_date": row["next_restock_date"],
            "last_ordered_date": row["last_ordered_date"],
            "confidence": row["confidence"],
            "avg_days_between": row["avg_days_between"],
        }
        items.append(item)

        # Group by store
        store = row["store_name"]
        if store not in by_store:
            by_store[store] = []
        by_store[store].append(item)

    # ── Step 3: Build summary ──
    store_summary = {}
    for store, store_items in by_store.items():
        store_total = sum(i["estimated_price"] or 0 for i in store_items)
        store_summary[store] = {
            "count": len(store_items),
            "estimated": round(store_total, 2),
        }

    summary = {
        "total_items": len(items),
        "total_estimated": round(total_estimated, 2),
        "stores": store_summary,
    }

    # ── Step 3b: Log ranking reasons for each item's selected product ──
    for item in items:
        try:
            best = cur.execute("""
                SELECT ia.score, ia.tier, ia.reasons, pc.product_name
                FROM item_alternatives ia
                JOIN product_catalog pc ON ia.catalog_id = pc.id
                WHERE ia.item_id = ? AND ia.store_id = ?
                  AND ia.tier != 'avoid'
                ORDER BY ia.rank ASC LIMIT 1
            """, (item["item_id"], item["store_id"])).fetchone()
            if best:
                reasons = []
                try:
                    reasons = json.loads(best["reasons"]) if best["reasons"] else []
                except (json.JSONDecodeError, TypeError):
                    pass
                top_reasons = [r for r in reasons if "+" in r][:3]
                reason_str = "; ".join(top_reasons) if top_reasons else "no specific reasons"
                print(f"  [{best['tier'].upper():10s} {best['score']:3.0f}] {item['item_name']:20s} → {best['product_name'][:45]:45s} | {reason_str}")
        except Exception:
            pass

    # ── Step 4: Save to DB (unless dry run) ──
    # Check if a draft list already exists for this target date (merge logic).
    # This allows user-created lists and AI-generated lists to coexist:
    # if the user already started a list, we add AI items to it rather than
    # creating a duplicate.
    list_id = None
    if not dry_run:
        # ── Supersede stale scheduled drafts ──
        # There should be exactly ONE active scheduled list: the next
        # order's. Any other OPEN suggestion draft (a previous order's
        # leftover, a debug far-future target, a pre-rebuild dump) is
        # stale -- it lingers and the "active list" query keeps surfacing
        # it as a bloated "current" list (this is how a 53-item draft for
        # a month-out date kept showing up after an order). Mark every
        # open suggestion draft for a DIFFERENT target_date as done +
        # dismissed. Scoped to suggestion-type (list_type 'suggestion' or
        # NULL) so in-progress carts (list_type 'cart') are never touched.
        superseded = cur.execute(
            """UPDATE shopping_lists
                  SET status = 'done',
                      dismissed_at = datetime('now'),
                      notes = TRIM(COALESCE(notes,'')
                              || ' [superseded by regen for ' || ? || ']')
                WHERE status IN ('draft', 'suggested', 'in_review')
                  AND COALESCE(dismissed_at, '') = ''
                  AND COALESCE(target_date, '') != ?
                  AND (list_type = 'suggestion' OR list_type IS NULL)""",
            (target_date, target_date),
        ).rowcount
        if superseded:
            conn.commit()
            print(f"  Superseded {superseded} stale scheduled draft(s) for other dates")

    if not dry_run and items:
        # Find existing drafts — keep only the LATEST, mark rest as done
        all_drafts = cur.execute("""
            SELECT id FROM shopping_lists
            WHERE target_date = ? AND status = 'draft'
            ORDER BY created_at DESC
        """, (target_date,)).fetchall()

        existing_draft = all_drafts[0] if all_drafts else None

        # Deduplicate: mark old drafts as done
        if len(all_drafts) > 1:
            for old in all_drafts[1:]:
                cur.execute(
                    "UPDATE shopping_lists SET status = 'done', notes = 'Deduplicated by list_generate' WHERE id = ?",
                    (old["id"],)
                )
            conn.commit()
            print(f"  Deduplicated: kept list {existing_draft['id']}, marked {len(all_drafts)-1} old drafts as done")

        if existing_draft:
            # ── Merge into existing draft ──
            list_id = existing_draft["id"]

            # ── Prune stale system suggestions ──
            # Remove items that were auto-suggested on a PRIOR run but are no
            # longer due (e.g. now covered by a just-placed order). Without
            # this, regenerating only ADDED new due items and never removed
            # old ones, so the list kept growing and showed things we just
            # bought. Only prunes system-added, still-pending, user-untouched
            # rows -- anything the user added, edited, or skipped is kept.
            due_ids = [it["item_id"] for it in items if it.get("item_id")]
            if due_ids:
                placeholders = ",".join("?" * len(due_ids))
                pruned = cur.execute(
                    f"""DELETE FROM list_items
                        WHERE list_id = ?
                          AND item_id IS NOT NULL
                          AND item_id NOT IN ({placeholders})
                          AND COALESCE(added_by, 'system') = 'system'
                          AND COALESCE(status, '') = 'pending'
                          AND COALESCE(user_action, '') = ''""",
                    [list_id] + due_ids,
                ).rowcount
                if pruned:
                    conn.commit()
                    print(f"  Pruned {pruned} stale system suggestion(s) "
                          f"no longer due from list {list_id}")

            # Find which item_ids are already on the list
            existing_item_ids = set(
                row["item_id"] for row in cur.execute(
                    "SELECT item_id FROM list_items WHERE list_id = ? AND item_id IS NOT NULL",
                    (list_id,)
                ).fetchall()
            )

            added_count = 0
            refreshed_count = 0
            for item in items:
                if item["item_id"] in existing_item_ids:
                    # Already on the draft. Refresh the quantity/price to the
                    # CURRENT model output -- but only for a system-added,
                    # user-untouched row whose quantity still equals its
                    # original suggestion. Previously this just `continue`d,
                    # which froze stale quantities on the draft so a model
                    # change (e.g. the typical-purchase quantity policy) never
                    # reached an existing list. User edits (added_by='user', a
                    # user_action, a non-pending status, or a hand-changed qty)
                    # are never overwritten.
                    refreshed_count += cur.execute("""
                        UPDATE list_items
                           SET quantity = ?, original_qty = ?, estimated_price = ?
                         WHERE list_id = ? AND item_id = ?
                           AND COALESCE(added_by, 'system') = 'system'
                           AND COALESCE(user_action, '') = ''
                           AND COALESCE(status, 'pending') = 'pending'
                           AND quantity = COALESCE(original_qty, quantity)
                    """, (
                        item["quantity"], item["quantity"], item["estimated_price"],
                        list_id, item["item_id"],
                    )).rowcount
                    continue

                cur.execute("""
                    INSERT INTO list_items
                        (list_id, item_id, store_id, quantity, estimated_price,
                         original_qty, status, added_by)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', 'system')
                """, (
                    list_id,
                    item["item_id"],
                    item["store_id"],
                    item["quantity"],
                    item["estimated_price"],
                    item["quantity"],
                ))

                # Also add suggestion snapshot for the new item
                best = cur.execute("""
                    SELECT ia.catalog_id, pc.product_name, pc.price
                    FROM item_alternatives ia
                    JOIN product_catalog pc ON ia.catalog_id = pc.id
                    WHERE ia.item_id = ? AND ia.store_id = ?
                      AND ia.tier != 'avoid' AND pc.is_available = 1
                    ORDER BY ia.rank ASC LIMIT 1
                """, (item["item_id"], item["store_id"])).fetchone()

                cur.execute("""
                    INSERT INTO suggestion_snapshots
                        (list_id, item_id, store_id, original_qty, original_price,
                         original_catalog_id, original_product_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    list_id,
                    item["item_id"],
                    item["store_id"],
                    item["quantity"],
                    item["estimated_price"],
                    best["catalog_id"] if best else None,
                    best["product_name"] if best else item["item_name"],
                ))
                added_count += 1

            # Update the list notes to reflect the merge
            cur.execute("""
                UPDATE shopping_lists SET notes = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (
                f"Merged: +{added_count} new, {refreshed_count} qty-refreshed "
                f"into existing draft ({len(items)} due)",
                list_id,
            ))
            conn.commit()
            print(f"  Merged {added_count} new + refreshed {refreshed_count} "
                  f"existing system qty into draft list {list_id}")

        else:
            # ── Create new list ──
            day = _day_name(target_date)
            list_name = f"{day} {target_date} order"

            cur.execute("""
                INSERT INTO shopping_lists (name, target_date, status, notes)
                VALUES (?, ?, 'draft', ?)
            """, (
                list_name,
                target_date,
                f"Auto-generated: {len(items)} items across {len(by_store)} store(s)",
            ))
            list_id = cur.lastrowid

            for item in items:
                cur.execute("""
                    INSERT INTO list_items
                        (list_id, item_id, store_id, quantity, estimated_price,
                         original_qty, status, added_by)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', 'system')
                """, (
                    list_id,
                    item["item_id"],
                    item["store_id"],
                    item["quantity"],
                    item["estimated_price"],
                    item["quantity"],  # original_qty = same as initial quantity
                ))

            # Save frozen suggestion snapshot (never edited — used for diff + reset)
            for item in items:
                # Look up the best-known product for this item+store
                best = cur.execute("""
                    SELECT ia.catalog_id, pc.product_name, pc.price
                    FROM item_alternatives ia
                    JOIN product_catalog pc ON ia.catalog_id = pc.id
                    WHERE ia.item_id = ? AND ia.store_id = ?
                      AND ia.tier != 'avoid' AND pc.is_available = 1
                    ORDER BY ia.rank ASC
                    LIMIT 1
                """, (item["item_id"], item["store_id"])).fetchone()

                cur.execute("""
                    INSERT INTO suggestion_snapshots
                        (list_id, item_id, store_id, original_qty, original_price,
                         original_catalog_id, original_product_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    list_id,
                    item["item_id"],
                    item["store_id"],
                    item["quantity"],
                    item["estimated_price"],
                    best["catalog_id"] if best else None,
                    best["product_name"] if best else item["item_name"],
                ))

            conn.commit()

    # ── Step 4: Quality validator (Phase-1 agent pass) ──
    # Evaluate each just-saved list_item against the household's
    # quality bar. Repair (switch store) when a better in-stock match
    # exists. Persist every decision to item_resolution_attempts so the
    # next cycle starts smarter. See list_quality_validator.py for the
    # full contract.
    validator_report = None
    if list_id and not dry_run:
        try:
            from list_quality_validator import validate_list_quality
            validator_report = validate_list_quality(list_id, conn=conn)
            print(
                f"  [validator] list={list_id} validated={validator_report['validated']} "
                f"matched={validator_report['matched']} "
                f"switched={validator_report['switched']} "
                f"flagged={validator_report['flagged']}"
            )
        except Exception as e:
            # Validation is additive -- a failure must NOT block the
            # list from being generated. Log and move on.
            print(f"  [validator] failed (non-fatal): {e}")
            try:
                from app_logging import log_event
                log_event(
                    "validator_pass_error",
                    source="list_generate",
                    detail=str(e)[:200],
                    level="error",
                )
            except Exception:
                pass

        # Don't strand a single item at a vendor (you can't place a 1-item
        # order). Move a lone item onto the main vendor when it's available there.
        try:
            n_moved = _consolidate_lone_vendors(list_id, conn)
            if n_moved:
                print(f"  [consolidate] moved {n_moved} lone-vendor item(s) to the main store")
        except Exception as _ce:
            print(f"  [consolidate] failed (non-fatal): {_ce}")

        # CRITICAL: persist the validator's catalog_id pins + consolidation
        # moves. validate_list_quality / _consolidate_lone_vendors write
        # list_items.catalog_id on the shared conn but don't commit; without
        # this commit, close_conn rolls them back, leaving catalog_id NULL ->
        # build_frontend_list free-text re-matches at render and can land on a
        # wrong/overpriced product (eggs showed a 15-pack Tregor @ 72.70 even
        # though the validator had correctly picked Organic Larder @ 30.79).
        conn.commit()

    if close_conn:
        conn.close()

    return {
        "list_id": list_id,
        "target_date": target_date,
        "day_name": _day_name(target_date),
        "items": items,
        "by_store": by_store,
        "summary": summary,
        "validator": validator_report,
    }


# ────────────────────────────────────────────
# list_get — fetch an existing list
# ────────────────────────────────────────────

def list_get(list_id=None, conn=None):
    """
    Fetch a shopping list by ID, or the most recent one if no ID given.

    Returns same structure as list_generate, or None if not found.
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    if list_id is None:
        cur.execute("SELECT id FROM shopping_lists ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            if close_conn:
                conn.close()
            return None
        list_id = row["id"]

    # Get list header
    cur.execute("""
        SELECT id, name, target_date, status, notes, created_at
        FROM shopping_lists WHERE id = ?
    """, (list_id,))
    list_row = cur.fetchone()
    if not list_row:
        if close_conn:
            conn.close()
        return None

    # Get list items (LEFT JOIN items so catalog-only entries with item_id=NULL still show)
    cur.execute("""
        SELECT
            li.id AS list_item_id,
            li.item_id,
            li.catalog_id,
            li.quantity,
            li.estimated_price,
            li.status,
            li.user_action,
            li.user_note,
            li.added_by,
            li.substitution_note,
            li.failure_context,
            li.category_override,
            i.origin_signal_llm,
            COALESCE(i.name, li.item_name_text) AS item_name,
            COALESCE(li.category_override, i.category, pc.category, 'Other') AS category,
            i.notes AS item_notes,
            COALESCE(i.avg_price, 0) AS avg_price,
            s.name AS store_name,
            s.id AS store_id
        FROM list_items li
        LEFT JOIN items i ON li.item_id = i.id
        LEFT JOIN product_catalog pc ON li.catalog_id = pc.id
        JOIN stores s ON li.store_id = s.id
        WHERE li.list_id = ?
        ORDER BY s.name, COALESCE(li.category_override, i.category, pc.category, 'Other'),
                 COALESCE(i.name, li.item_name_text)
    """, (list_id,))

    items = []
    by_store = {}
    total_estimated = 0.0

    for row in cur.fetchall():
        item = {
            "list_item_id": row["list_item_id"],
            "item_id": row["item_id"],
            "catalog_id": row["catalog_id"],
            "item_name": row["item_name"],
            "store_name": row["store_name"],
            "store_id": row["store_id"],
            "quantity": row["quantity"],
            "estimated_price": row["estimated_price"],
            "price_each": row["avg_price"],
            "category": row["category"] or "Other",
            "notes": row["item_notes"] or "",
            "status": row["status"],
            "user_action": row["user_action"],
            "user_note": row["user_note"],
            "added_by": row["added_by"],
            "substitution_note": row["substitution_note"],
            "failure_context": row["failure_context"],
            "category_override": row["category_override"],
        }
        items.append(item)
        total_estimated += (row["estimated_price"] or 0)

        store = row["store_name"]
        if store not in by_store:
            by_store[store] = []
        by_store[store].append(item)

    store_summary = {}
    for store, store_items in by_store.items():
        store_total = sum(i["estimated_price"] or 0 for i in store_items)
        store_summary[store] = {
            "count": len(store_items),
            "estimated": round(store_total, 2),
        }

    if close_conn:
        conn.close()

    return {
        "list_id": list_id,
        "list_name": list_row["name"],
        "target_date": list_row["target_date"],
        "day_name": _day_name(list_row["target_date"]),
        "status": list_row["status"],
        "created_at": list_row["created_at"],
        "items": items,
        "by_store": by_store,
        "summary": {
            "total_items": len(items),
            "total_estimated": round(total_estimated, 2),
            "stores": store_summary,
        },
    }


# ────────────────────────────────────────────
# _backfill_snapshots — create snapshots for pre-Phase-1 lists
# ────────────────────────────────────────────

def _backfill_snapshots(list_id, items=None, conn=None):
    """
    Create suggestion_snapshots for a list that was generated before
    the snapshot system existed. Uses current list_items quantities
    as the "original" values (best we can do retroactively).

    Skips silently if snapshots already exist for this list.
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Already has snapshots? Skip.
    cur.execute("SELECT COUNT(*) FROM suggestion_snapshots WHERE list_id = ?", (list_id,))
    if cur.fetchone()[0] > 0:
        if close_conn:
            conn.close()
        return

    # Pull current list_items as the baseline
    rows = cur.execute("""
        SELECT li.item_id, li.store_id, li.quantity, li.estimated_price,
               COALESCE(li.original_qty, li.quantity) AS orig_qty
        FROM list_items li
        WHERE li.list_id = ? AND li.added_by != 'user'
    """, (list_id,)).fetchall()

    if not rows:
        if close_conn:
            conn.close()
        return

    for row in rows:
        # Look up best product for this item+store
        best = cur.execute("""
            SELECT ia.catalog_id, pc.product_name, pc.price
            FROM item_alternatives ia
            JOIN product_catalog pc ON ia.catalog_id = pc.id
            WHERE ia.item_id = ? AND ia.store_id = ?
              AND ia.tier != 'avoid' AND pc.is_available = 1
            ORDER BY ia.rank ASC
            LIMIT 1
        """, (row["item_id"], row["store_id"])).fetchone()

        cur.execute("""
            INSERT INTO suggestion_snapshots
                (list_id, item_id, store_id, original_qty, original_price,
                 original_catalog_id, original_product_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            list_id,
            row["item_id"],
            row["store_id"],
            row["orig_qty"],
            row["estimated_price"],
            best["catalog_id"] if best else None,
            best["product_name"] if best else None,
        ))

        # Also set original_qty on the list_item if it was NULL
        cur.execute("""
            UPDATE list_items SET original_qty = ?
            WHERE list_id = ? AND item_id = ? AND original_qty IS NULL
        """, (row["orig_qty"], list_id, row["item_id"]))

    conn.commit()
    print(f"  Backfilled {len(rows)} suggestion snapshots for list {list_id}")

    if close_conn:
        conn.close()


# ────────────────────────────────────────────
# list_get_diff — compare current list vs original suggestion
# ────────────────────────────────────────────

def get_forgotten_items(list_id=None, limit=8, conn=None):
    """A2 — items the household normally buys but ISN'T on the active
    list yet, sorted by how-overdue.

    Useful as a "did we forget X?" surface for the curator. Excludes
    items that are stop_suggesting, items already on the active list,
    and items with low cadence confidence (< 0.3) so we don't surface
    one-off purchases as habits.

    Args:
        list_id: the active list to compare against. If None, pulls
            the most recent draft / suggestion list automatically.
        limit: cap how many to return (UI nudge, not a hard rule).

    Returns:
        list of dicts:
          {"item_id", "item_name", "category", "store_id", "store_name",
           "last_ordered_date", "next_restock_date", "avg_days_between",
           "confidence", "times_ordered", "days_overdue"}
        sorted by days_overdue DESC (most overdue first).
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Resolve list_id if caller didn't supply -- prefer the most
    # recent suggestion-typed draft, fall back to the most recent
    # list of any type. Mirrors how the front-end picks the active list.
    if list_id is None:
        latest = cur.execute(
            """SELECT id FROM shopping_lists
               WHERE status IN ('draft', 'suggested', 'in_review')
                 AND COALESCE(dismissed_at, '') = ''
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if not latest:
            if close_conn:
                conn.close()
            return []
        list_id = latest[0] if not isinstance(latest, sqlite3.Row) else latest["id"]

    # Same gating as list_generate.generate_list_for_date (Step 1):
    #   - times_ordered >= 3 (real pattern)
    #   - last_ordered within MAX(4x cadence, 90 days) (still in rotation)
    #   - stop_suggesting honored
    # Plus the inventory model -- only items projected to run out before
    # the next order. The next_restock_date constraint is dropped here
    # because the inventory model is the authority on "overdue" now.
    rows = cur.execute(
        """
        SELECT
            i.id AS item_id,
            i.name AS item_name,
            i.category,
            i.preferred_store_id AS store_id,
            s.name AS store_name,
            r.last_ordered_date,
            r.next_restock_date,
            r.avg_days_between,
            r.avg_quantity,
            r.confidence,
            r.times_ordered,
            CAST(julianday('now') - julianday(r.next_restock_date) AS INTEGER)
                AS days_overdue
        FROM items i
        JOIN restock_rules r ON r.item_id = i.id
        LEFT JOIN stores s ON s.id = i.preferred_store_id
        WHERE r.confidence >= 0.3
          AND COALESCE(r.times_ordered, 0) >= 3
          AND r.last_ordered_date IS NOT NULL
          AND r.avg_days_between IS NOT NULL
          AND r.avg_days_between > 0
          AND julianday(date('now')) - julianday(r.last_ordered_date)
              <= MAX(r.avg_days_between * 4, 90)
          AND NOT EXISTS (
              SELECT 1 FROM list_items li
              WHERE li.list_id = ?
                AND li.item_id = i.id
                AND COALESCE(li.status, '') != 'skipped'
          )
          AND NOT EXISTS (
              SELECT 1 FROM user_item_prefs uip
              WHERE uip.item_id = i.id
                AND uip.pref_type = 'stop_suggesting'
                AND uip.is_active = 1
          )
        ORDER BY r.confidence DESC, i.name
        """,
        (list_id,),
    ).fetchall()

    today_dt = datetime.now().date()
    # For forgotten-items the "target" is effectively today -- this surface
    # is "what's overdue right now?", not "what will be due on next order?".
    # Coverage window of 1 day means "what's needed for the next day if we
    # ordered now"; the inventory math handles the rest.
    out = []
    for r in rows:
        row = dict(r) if isinstance(r, sqlite3.Row) else r
        qty, reason = _inventory_suggest_quantity(
            avg_quantity=row.get("avg_quantity"),
            avg_days_between=row.get("avg_days_between"),
            last_ordered_date=row.get("last_ordered_date"),
            today_dt=today_dt,
            target_dt=today_dt,
            coverage_window_days=1,
        )
        if qty > 0:
            row["suggested_qty"] = qty
            out.append(row)
        if len(out) >= limit:
            break

    if close_conn:
        conn.close()
    return out


def list_get_diff(list_id, conn=None):
    """
    Compare current list_items against the original suggestion_snapshots.

    Returns:
        {
            "list_id": int,
            "has_changes": bool,
            "changes": [
                {
                    "item_name": str,
                    "change_type": "qty_changed"|"removed"|"added"|"unchanged",
                    "original_qty": float or None,
                    "current_qty": float or None,
                }
            ],
            "summary": {"unchanged": N, "qty_changed": N, "removed": N, "added": N}
        }
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Check if snapshots exist for this list
    cur.execute("SELECT COUNT(*) FROM suggestion_snapshots WHERE list_id = ?", (list_id,))
    if cur.fetchone()[0] == 0:
        if close_conn:
            conn.close()
        return {"list_id": list_id, "has_changes": False, "changes": [], "summary": {
            "unchanged": 0, "qty_changed": 0, "removed": 0, "added": 0
        }}

    # Get original suggestions
    originals = {}
    for row in cur.execute("""
        SELECT ss.item_id, ss.original_qty, ss.original_price, ss.original_product_name,
               i.name AS item_name
        FROM suggestion_snapshots ss
        JOIN items i ON ss.item_id = i.id
        WHERE ss.list_id = ?
    """, (list_id,)):
        originals[row["item_id"]] = {
            "item_name": row["item_name"],
            "original_qty": row["original_qty"],
            "original_price": row["original_price"],
            "original_product": row["original_product_name"],
        }

    # Get current list items
    current = {}
    for row in cur.execute("""
        SELECT li.item_id, li.quantity, li.status, li.user_action, li.added_by,
               i.name AS item_name
        FROM list_items li
        JOIN items i ON li.item_id = i.id
        WHERE li.list_id = ?
    """, (list_id,)):
        current[row["item_id"]] = {
            "item_name": row["item_name"],
            "quantity": row["quantity"],
            "status": row["status"],
            "user_action": row["user_action"],
            "added_by": row["added_by"],
        }

    changes = []
    counts = {"unchanged": 0, "qty_changed": 0, "removed": 0, "added": 0}

    # Compare originals against current
    for item_id, orig in originals.items():
        cur_item = current.get(item_id)
        if not cur_item or cur_item["status"] == "skipped":
            changes.append({
                "item_name": orig["item_name"],
                "change_type": "removed",
                "original_qty": orig["original_qty"],
                "current_qty": 0,
            })
            counts["removed"] += 1
        elif cur_item["quantity"] != orig["original_qty"]:
            changes.append({
                "item_name": orig["item_name"],
                "change_type": "qty_changed",
                "original_qty": orig["original_qty"],
                "current_qty": cur_item["quantity"],
            })
            counts["qty_changed"] += 1
        else:
            counts["unchanged"] += 1

    # Check for user-added items (in current but not in originals)
    for item_id, cur_item in current.items():
        if item_id not in originals and cur_item["status"] != "skipped":
            changes.append({
                "item_name": cur_item["item_name"],
                "change_type": "added",
                "original_qty": None,
                "current_qty": cur_item["quantity"],
            })
            counts["added"] += 1

    if close_conn:
        conn.close()

    has_changes = counts["qty_changed"] > 0 or counts["removed"] > 0 or counts["added"] > 0

    return {
        "list_id": list_id,
        "has_changes": has_changes,
        "changes": changes,
        "summary": counts,
    }


# ────────────────────────────────────────────
# list_reset_to_original — restore AI suggestion
# ────────────────────────────────────────────

def list_reset_to_original(list_id, conn=None):
    """
    Reset all items on a list back to the original AI suggestion.
    - Restores quantities from suggestion_snapshots
    - Un-skips items that were removed
    - Removes user-added items
    - Resets user_action and status to 'pending'

    Returns dict with counts of what was restored.
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Check if snapshots exist
    cur.execute("SELECT COUNT(*) FROM suggestion_snapshots WHERE list_id = ?", (list_id,))
    if cur.fetchone()[0] == 0:
        if close_conn:
            conn.close()
        return {"error": "No original suggestion found for this list"}

    # Step 1: Delete user-added items (not in the original suggestion)
    cur.execute("""
        DELETE FROM list_items
        WHERE list_id = ? AND added_by = 'user'
    """, (list_id,))
    removed_user_adds = cur.rowcount

    # Step 2: Restore original quantities and reset status
    originals = cur.execute("""
        SELECT item_id, store_id, original_qty, original_price
        FROM suggestion_snapshots WHERE list_id = ?
    """, (list_id,)).fetchall()

    restored = 0
    for orig in originals:
        cur.execute("""
            UPDATE list_items
            SET quantity = ?,
                estimated_price = ?,
                status = 'pending',
                user_action = NULL,
                user_note = NULL
            WHERE list_id = ? AND item_id = ?
        """, (
            orig["original_qty"],
            orig["original_price"],
            list_id,
            orig["item_id"],
        ))
        if cur.rowcount > 0:
            restored += 1

    # Step 3: Re-insert any original items that were fully deleted (edge case)
    for orig in originals:
        exists = cur.execute(
            "SELECT id FROM list_items WHERE list_id = ? AND item_id = ?",
            (list_id, orig["item_id"])
        ).fetchone()
        if not exists:
            cur.execute("""
                INSERT INTO list_items
                    (list_id, item_id, store_id, quantity, estimated_price,
                     original_qty, status, added_by)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 'system')
            """, (
                list_id, orig["item_id"], orig["store_id"],
                orig["original_qty"], orig["original_price"], orig["original_qty"],
            ))
            restored += 1

    conn.commit()
    if close_conn:
        conn.close()

    return {
        "restored": restored,
        "removed_user_adds": removed_user_adds,
    }


# ────────────────────────────────────────────
# list_update_item — user approves/skips/modifies items
# ────────────────────────────────────────────

def list_update_item(list_item_id, action, quantity=None, note=None, conn=None):
    """
    Update a single item on a shopping list.

    Actions:
      'approve'  — user confirms this item (ready for cart)
      'skip'     — user doesn't want this item this time
      'modify'   — user changed the quantity
      'add_note' — user added a note (e.g., "get the big pack")

    Returns True if updated, False if not found.
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Verify item exists
    cur.execute("SELECT id, status FROM list_items WHERE id = ?", (list_item_id,))
    row = cur.fetchone()
    if not row:
        if close_conn:
            conn.close()
        return False

    # E6 — reverse path for the Tier-1/Tier-2 ladder.
    # When an item ends up status='approved' (via direct approve or via
    # modify), the user is telling us "yes, I do still want this." If
    # the pattern detector had previously pushed this item's cadence
    # out (cadence_adjustment > 1.0) or auto-stopped it after 5 drops,
    # we reset both back to neutral so the suggestion ladder starts
    # fresh. Only AUTO-inserted stop_suggesting rows (value LIKE 'auto:%')
    # are deactivated; a user-driven explicit stop stays untouched.
    def _reset_tier_ladder_for_list_item(_cur, _list_item_id):
        try:
            row = _cur.execute(
                "SELECT item_id FROM list_items WHERE id = ?",
                (_list_item_id,),
            ).fetchone()
            if not row or row["item_id"] is None:
                return
            item_id_for_reset = row["item_id"]
            _cur.execute(
                """UPDATE restock_rules
                   SET cadence_adjustment = 1.0,
                       updated_at = datetime('now')
                   WHERE item_id = ? AND cadence_adjustment > 1.0""",
                (item_id_for_reset,),
            )
            _cur.execute(
                """UPDATE user_item_prefs
                   SET is_active = 0
                   WHERE item_id = ?
                     AND pref_type = 'stop_suggesting'
                     AND is_active = 1
                     AND value LIKE 'auto:%'""",
                (item_id_for_reset,),
            )
        except Exception:
            # Reset is best-effort -- the primary status update still
            # succeeds even if the reverse path has a transient hiccup.
            pass

    if action in ("approve", "approved"):
        if quantity is not None:
            # Approve with quantity update
            cur.execute("""
                UPDATE list_items SET status = 'approved', user_action = 'modified', quantity = ?
                WHERE id = ?
            """, (quantity, list_item_id))
        else:
            cur.execute("""
                UPDATE list_items SET status = 'approved', user_action = 'approved'
                WHERE id = ?
            """, (list_item_id,))
        _reset_tier_ladder_for_list_item(cur, list_item_id)

    elif action in ("skip", "skipped"):
        cur.execute("""
            UPDATE list_items SET status = 'skipped', user_action = 'skipped'
            WHERE id = ?
        """, (list_item_id,))

    elif action == "modify" and quantity is not None:
        cur.execute("""
            UPDATE list_items SET quantity = ?, user_action = 'modified', status = 'approved'
            WHERE id = ?
        """, (quantity, list_item_id))
        _reset_tier_ladder_for_list_item(cur, list_item_id)

    elif action == "add_note" and note is not None:
        cur.execute("""
            UPDATE list_items SET user_note = ?, user_action = COALESCE(user_action, 'noted')
            WHERE id = ?
        """, (note, list_item_id))

    else:
        if close_conn:
            conn.close()
        return False

    conn.commit()
    if close_conn:
        conn.close()
    return True


# ────────────────────────────────────────────
# list_add_item — user requests an extra item
# ────────────────────────────────────────────

def list_add_item(list_id, item_name, store_name=None, store_id=None, item_id=None,
                  quantity=1, estimated_price=None, note=None, catalog_id=None,
                  failure_context=None, category_override=None, status="approved",
                  conn=None):
    """
    Add an item to an existing list (e.g., user says "also get olive oil").

    Resolution priority:
      - item_id: if provided, uses it directly (skips name matching)
      - item_name: tries exact name match in items table
      - store_id > store_name > item's preferred store
      - estimated_price: if provided, uses it directly (from curator)

    Returns the new list_item_id, or None if list not found.
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Verify list exists
    cur.execute("SELECT id FROM shopping_lists WHERE id = ?", (list_id,))
    if not cur.fetchone():
        if close_conn:
            conn.close()
        return None

    # If item_id was provided directly (from curator), use it
    if item_id is not None:
        # Verify item exists and get its preferred store
        cur.execute("""
            SELECT i.id AS item_id, i.preferred_store_id, i.avg_price, s.name AS store_name
            FROM items i
            JOIN stores s ON i.preferred_store_id = s.id
            WHERE i.id = ?
        """, (item_id,))
        item_row = cur.fetchone()
        if item_row:
            if not store_id:
                store_id = item_row["preferred_store_id"]
            if estimated_price is None:
                estimated_price = round((item_row["avg_price"] or 0) * quantity, 2) or None
        # If item_id was given but not found, still proceed with item_id
        # (it may be from a different context)
    else:
        # Try to find matching item in items table (case-insensitive)
        cur.execute("""
            SELECT i.id AS item_id, i.preferred_store_id, i.avg_price, s.name AS store_name
            FROM items i
            JOIN stores s ON i.preferred_store_id = s.id
            WHERE LOWER(i.name) = LOWER(?)
        """, (item_name,))
        item_row = cur.fetchone()

        if item_row:
            item_id = item_row["item_id"]
            if not store_id:
                store_id = item_row["preferred_store_id"]
            if estimated_price is None:
                estimated_price = round((item_row["avg_price"] or 0) * quantity, 2) or None

    # Override store by name if no store_id was resolved yet
    if not store_id and store_name:
        cur.execute("SELECT id FROM stores WHERE LOWER(name) = LOWER(?)", (store_name,))
        store_row = cur.fetchone()
        if store_row:
            store_id = store_row["id"]

    # Default to first active store if still none
    if store_id is None:
        cur.execute("SELECT id FROM stores WHERE is_active = 1 LIMIT 1")
        store_row = cur.fetchone()
        store_id = store_row["id"] if store_row else 1

    # Dedup: if a row for this (list_id, item_id) already exists, don't
    # add a new one. Choose action by existing row state:
    #   - status='skipped' OR user_action='skipped' -> un-skip it
    #     (the user is re-adding something they previously dropped).
    #     Restore status to the new `status` arg and merge note/price.
    #   - already active (status != 'skipped') -> bump quantity by the
    #     new `quantity` arg. Don't overwrite price/store/catalog --
    #     the existing row's pick is authoritative.
    # Without this guard, each "add eggs" / "add milk" via WhatsApp /
    # in-app chat / curator UI piles a fresh row onto the same list
    # and the visible item count balloons. Found on the Ally where a
    # long-lived draft accumulated 70+ rows.
    if item_id is not None:
        existing = cur.execute(
            """SELECT id, status, user_action, quantity FROM list_items
               WHERE list_id = ? AND item_id = ?
               ORDER BY (CASE WHEN COALESCE(status,'') = 'skipped'
                              OR COALESCE(user_action,'') = 'skipped'
                          THEN 1 ELSE 0 END) ASC,
                        id DESC
               LIMIT 1""",
            (list_id, item_id),
        ).fetchone()
        if existing:
            ex_skipped = (
                (existing["status"] or "") == "skipped"
                or (existing["user_action"] or "") == "skipped"
            )
            if ex_skipped:
                # Un-skip + restore the requested status
                cur.execute(
                    """UPDATE list_items
                       SET status = ?, user_action = 'added',
                           quantity = ?,
                           estimated_price = COALESCE(?, estimated_price),
                           user_note = COALESCE(?, user_note)
                       WHERE id = ?""",
                    (status, quantity, estimated_price, note, existing["id"]),
                )
            else:
                # Already active -- bump quantity rather than duplicate.
                new_qty = (existing["quantity"] or 0) + (quantity or 0)
                cur.execute(
                    "UPDATE list_items SET quantity = ?, "
                    "user_note = COALESCE(?, user_note) WHERE id = ?",
                    (new_qty, note, existing["id"]),
                )
            conn.commit()
            if close_conn:
                conn.close()
            return existing["id"]

    cur.execute("""
        INSERT INTO list_items
            (list_id, item_id, store_id, quantity, estimated_price, status, user_action,
             user_note, added_by, item_name_text, catalog_id, failure_context, category_override)
        VALUES (?, ?, ?, ?, ?, ?, 'added', ?, 'user', ?, ?, ?, ?)
    """, (list_id, item_id, store_id, quantity, estimated_price, status, note,
          item_name, catalog_id, failure_context, category_override))

    list_item_id = cur.lastrowid
    conn.commit()

    if close_conn:
        conn.close()
    return list_item_id


def _order_item_ids(conn, order_row, list_match_days=3):
    """Item_ids an order contains.

    Prefer the scraped order_items; if empty (Carrefour invoice capture is
    flaky), fall back to the finalized shopping list that produced the
    order -- matched by the SAME store and a target_date within a few days
    of the order date. The list we built is the reliable record of what was
    ordered when the vendor's line-items didn't scrape.
    """
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT DISTINCT item_id FROM order_items "
        "WHERE order_id = ? AND item_id IS NOT NULL",
        (order_row["id"],),
    ).fetchall()
    ids = {r["item_id"] for r in rows}
    if ids:
        return ids
    # Prefer delivery_date for the list-match window: Carrefour's scraped
    # order_date is sometimes a status string ("Arrives Today May 29 2026"),
    # which is unparseable. delivery_date is a clean timestamp.
    od = (order_row["delivery_date"] or order_row["order_date"] or "")[:10]
    if not od or len(od) != 10 or not od[:4].isdigit():
        return set()
    try:
        lo = (datetime.strptime(od, "%Y-%m-%d") - timedelta(days=list_match_days)).strftime("%Y-%m-%d")
        hi = (datetime.strptime(od, "%Y-%m-%d") + timedelta(days=list_match_days)).strftime("%Y-%m-%d")
    except ValueError:
        return set()
    rows = cur.execute(
        """SELECT DISTINCT li.item_id
           FROM list_items li
           JOIN shopping_lists sl ON sl.id = li.list_id
           WHERE li.store_id = ?
             AND li.item_id IS NOT NULL
             AND COALESCE(li.status,'') != 'skipped'
             AND COALESCE(li.user_action,'') != 'skipped'
             AND COALESCE(sl.dismissed_at,'') = ''
             AND sl.status IN ('confirmed','ordered','placed','done')
             AND COALESCE(sl.target_date,'') >= ?
             AND COALESCE(sl.target_date,'') <= ?""",
        (order_row["store_id"], lo, hi),
    ).fetchall()
    return {r["item_id"] for r in rows}


def _apply_delivery_sequence(conn, today_str, lookback_days=14, fallback_days=2):
    """Anchor each item's restock clock on its DELIVERY date, and return the
    set of items currently in-flight (ordered, not yet delivered) so they're
    suppressed from new suggestions.

    The household's model (2026-05-29):
      - An order's items start their consumption clock on the DELIVERY date.
      - While an order is placed-but-undelivered, its items are "incoming"
        -- don't re-suggest them.
      - If an order isn't confirmed delivered within `fallback_days` (2),
        assume it arrived: anchor the clock on the order date and stop
        suppressing, so a missed delivery-confirmation can't freeze items
        forever.

    Item membership uses order_items, falling back to the source shopping
    list (see _order_item_ids) when the vendor line-items didn't scrape.
    last_ordered_date only ever moves forward (idempotent).

    Returns: set(item_id) currently in-flight (to exclude this cycle).
    """
    cur = conn.cursor()
    try:
        today = datetime.strptime(today_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return set()
    cutoff = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    orders = cur.execute(
        """SELECT id, store_id, status, order_date, delivery_date
           FROM orders
           WHERE COALESCE(status,'') NOT IN ('cancelled',)
             AND COALESCE(order_date,'') >= ?""",
        (cutoff,),
    ).fetchall()

    in_flight = set()
    anchored = 0
    for o in orders:
        status = (o["status"] or "").lower()
        delivered = status in ("delivered", "completed") and o["delivery_date"]
        anchor = None
        if delivered:
            anchor = (o["delivery_date"] or "")[:10]
        else:
            # Placed but not confirmed delivered. Use order_date age.
            od = (o["order_date"] or "")[:10]
            order_dt = None
            if od and len(od) == 10:
                try:
                    order_dt = datetime.strptime(od, "%Y-%m-%d")
                except ValueError:
                    order_dt = None
            age_days = (today - order_dt).days if order_dt else 999
            if age_days <= fallback_days:
                # In-flight: assume coming, suppress its items this cycle.
                in_flight |= _order_item_ids(conn, o)
                continue
            else:
                # 2-day fallback: assume delivered, anchor on order date.
                anchor = od
        if not anchor or len(anchor) != 10:
            continue
        for item_id in _order_item_ids(conn, o):
            rule = cur.execute(
                "SELECT last_ordered_date, avg_days_between FROM restock_rules WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if not rule:
                continue
            cur_last = (rule["last_ordered_date"] or "")[:10]
            if cur_last and cur_last >= anchor:
                continue  # already anchored at/after this delivery
            days = int(rule["avg_days_between"]) if rule["avg_days_between"] else None
            next_date = None
            if days:
                try:
                    next_date = (datetime.strptime(anchor, "%Y-%m-%d")
                                 + timedelta(days=days)).strftime("%Y-%m-%d")
                except ValueError:
                    next_date = None
            if next_date:
                cur.execute(
                    "UPDATE restock_rules SET last_ordered_date=?, "
                    "next_restock_date=?, updated_at=datetime('now') WHERE item_id=?",
                    (anchor, next_date, item_id),
                )
            else:
                cur.execute(
                    "UPDATE restock_rules SET last_ordered_date=?, "
                    "updated_at=datetime('now') WHERE item_id=?",
                    (anchor, item_id),
                )
            anchored += 1
    if anchored:
        conn.commit()
        print(f"  [delivery-seq] anchored {anchored} item(s) on their delivery date")
    if in_flight:
        print(f"  [delivery-seq] {len(in_flight)} item(s) in-flight (pending delivery)")
    return in_flight


def backfill_last_ordered_from_lists(conn=None):
    """One-time, idempotent backfill of restock_rules.last_ordered_date from
    our own finalized-list history.

    Months of incomplete vendor order-item capture left many items with a
    stale last_ordered_date (e.g. Apple stuck at 2026-03-21), so the
    inventory model thinks they're perpetually overdue and keeps suggesting
    them. We built every scheduled list ourselves, so the most recent
    FINALIZED, non-dismissed list that contained an item is a reliable
    record of when it was last ordered. Advance last_ordered_date to that
    date (forward-only; never backward, never invents a purchase) and
    recompute next_restock_date.

    Returns [{item_id, name, old, new}] for the rows changed.
    """
    own = False
    if conn is None:
        conn = _get_conn()
        own = True
    cur = conn.cursor()
    # Only anchor on lists targeting TODAY-or-earlier. A list targeting a
    # FUTURE order day (next_order_date -- e.g. the upcoming Friday) is the
    # *upcoming* order, NOT a past purchase. The dedup logic marks superseded
    # drafts as 'done', so a superseded draft for the next order day would
    # otherwise feed its future target_date in here as "last ordered" -- which
    # set last_ordered_date in the future, making those items look perpetually
    # fresh (days_since_last clamps to 0) and never come due. Bound the MAX to
    # <= today so last_ordered can never be pushed past today.
    today = datetime.now().strftime("%Y-%m-%d")
    rows = cur.execute("""
        SELECT li.item_id, MAX(sl.target_date) AS last_list_date
        FROM list_items li
        JOIN shopping_lists sl ON sl.id = li.list_id
        WHERE sl.status IN ('confirmed', 'ordered', 'placed', 'done')
          AND COALESCE(sl.dismissed_at, '') = ''
          AND li.item_id IS NOT NULL
          AND COALESCE(li.status, '') != 'skipped'
          AND COALESCE(li.user_action, '') != 'skipped'
          AND COALESCE(sl.target_date, '') != ''
          AND sl.target_date <= ?
        GROUP BY li.item_id
    """, (today,)).fetchall()

    changes = []
    for r in rows:
        item_id = r["item_id"]
        new_date = (r["last_list_date"] or "")[:10]
        if not new_date or len(new_date) != 10 or not new_date[:4].isdigit():
            continue
        rule = cur.execute(
            "SELECT last_ordered_date, avg_days_between FROM restock_rules WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if not rule:
            continue
        cur_last = (rule["last_ordered_date"] or "")[:10]
        if cur_last and cur_last >= new_date:
            continue  # already at/after this date -- nothing to do
        days = int(rule["avg_days_between"]) if rule["avg_days_between"] else None
        next_date = None
        if days:
            try:
                next_date = (datetime.strptime(new_date, "%Y-%m-%d")
                             + timedelta(days=days)).strftime("%Y-%m-%d")
            except ValueError:
                next_date = None
        nm = cur.execute("SELECT name FROM items WHERE id = ?", (item_id,)).fetchone()
        if next_date:
            cur.execute(
                "UPDATE restock_rules SET last_ordered_date=?, next_restock_date=?, "
                "updated_at=datetime('now') WHERE item_id=?",
                (new_date, next_date, item_id),
            )
        else:
            cur.execute(
                "UPDATE restock_rules SET last_ordered_date=?, "
                "updated_at=datetime('now') WHERE item_id=?",
                (new_date, item_id),
            )
        changes.append({
            "item_id": item_id,
            "name": (nm["name"] if nm else "?"),
            "old": cur_last or None,
            "new": new_date,
        })
    conn.commit()
    if own:
        conn.close()
    return changes


# ────────────────────────────────────────────
# list_finalize — mark list as ready for cart building
# ────────────────────────────────────────────

def list_finalize(list_id, conn=None):
    """
    Mark a shopping list as 'confirmed' — ready for cart building.
    Also updates restock_rules: bump next_restock_date for approved items.

    Returns summary dict or None if list not found.
    """
    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Get approved items
    cur.execute("""
        SELECT li.item_id, li.quantity, r.avg_days_between
        FROM list_items li
        LEFT JOIN restock_rules r ON li.item_id = r.item_id
        WHERE li.list_id = ? AND li.status = 'approved'
    """, (list_id,))
    approved = cur.fetchall()

    # Update next_restock_date for each approved item
    today = datetime.now().strftime("%Y-%m-%d")
    for row in approved:
        if row["item_id"] and row["avg_days_between"]:
            days = int(row["avg_days_between"])
            next_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            cur.execute("""
                UPDATE restock_rules
                SET last_ordered_date = ?, next_restock_date = ?, updated_at = datetime('now')
                WHERE item_id = ?
            """, (today, next_date, row["item_id"]))

    # Mark list as confirmed
    cur.execute("""
        UPDATE shopping_lists SET status = 'confirmed', updated_at = datetime('now')
        WHERE id = ?
    """, (list_id,))

    # Auto-approve any still-pending items (user didn't explicitly act on them)
    cur.execute("""
        UPDATE list_items SET status = 'approved', user_action = 'auto-approved'
        WHERE list_id = ? AND status = 'pending'
    """, (list_id,))

    conn.commit()

    # Emit approval signals for each approved item (passive confirmation)
    try:
        from preference_engine import emit_signal
        for row in approved:
            if row["item_id"]:
                emit_signal(
                    item_id=row["item_id"],
                    signal_type="item_approve",
                    signal_source="app_ui",
                    dimension="item_inclusion",
                    new_value={"action": "approved", "qty": row["quantity"]},
                    strength=0.3,
                    context={"list_id": list_id},
                )
    except Exception as e:
        print(f"  [list_finalize] Could not emit approval signals: {e}")

    # Count results
    cur.execute("""
        SELECT status, COUNT(*) as cnt FROM list_items WHERE list_id = ? GROUP BY status
    """, (list_id,))
    status_counts = {row["status"]: row["cnt"] for row in cur.fetchall()}

    if close_conn:
        conn.close()

    return {
        "list_id": list_id,
        "approved": status_counts.get("approved", 0),
        "skipped": status_counts.get("skipped", 0),
        "restock_dates_updated": len(approved),
    }


# ────────────────────────────────────────────
# Pretty printer (for CLI testing)
# ────────────────────────────────────────────

def _print_list(result):
    """Print a shopping list result in a readable format."""
    if not result:
        print("No list found.")
        return

    target = result["target_date"]
    day = result["day_name"]
    summary = result["summary"]

    print(f"\n{'='*55}")
    print(f"  Shopping List: {day} {target}")
    print(f"  {summary['total_items']} items | ~{summary['total_estimated']} AED estimated")
    print(f"{'='*55}")

    for store, store_items in result["by_store"].items():
        store_info = summary["stores"].get(store, {})
        print(f"\n  [{store}] — {store_info.get('count', 0)} items, ~{store_info.get('estimated', 0)} AED")
        print(f"  {'-'*40}")
        for item in store_items:
            qty = item["quantity"]
            name = item["item_name"]
            price = f"~{item['estimated_price']} AED" if item.get("estimated_price") else ""
            status = f" [{item.get('status', '')}]" if item.get("status") else ""
            conf = item.get("confidence")
            conf_str = f" ({int(conf*100)}%)" if conf else ""
            # Show what the scraper will actually search for
            terms = item.get("search_terms", [])
            search_str = f' -> search: "{terms[0]}"' if terms else " -> search: (generic)"
            print(f"    {qty}x {name} {price}{conf_str}{status}{search_str}")

    print(f"\n{'='*55}\n")


# ────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────

if __name__ == "__main__":
    create_database()

    # Parse args
# ────────────────────────────────────────────
# curate_list — run the curator on every item in a list
# ────────────────────────────────────────────

def curate_list(list_id, conn=None):
    """
    Run the basket curator on every item in a shopping list.

    This replaces the old stale item_alternatives lookup with the
    FULL scoring pipeline: organic tiers, type gates, portion sizing,
    origin scoring, order history, multi-qty — everything.

    For each list_item:
      1. Runs the curator to find the best product across ALL stores
      2. Updates list_items: catalog_id, store_id, quantity, estimated_price
      3. Stores selection_reason as JSON in failure_context (reused field)

    Returns dict with curated results for display.
    """
    from basket_curator import curate_basket

    close_conn = False
    if conn is None:
        conn = _get_conn()
        close_conn = True

    cur = conn.cursor()

    # Get all pending/approved items on the list
    rows = cur.execute("""
        SELECT li.id AS list_item_id, li.item_id, i.name AS item_name,
               li.store_id, li.quantity, li.status
        FROM list_items li
        JOIN items i ON li.item_id = i.id
        WHERE li.list_id = ?
          AND li.status NOT IN ('skipped')
        ORDER BY i.name
    """, (list_id,)).fetchall()

    if not rows:
        if close_conn:
            conn.close()
        return {"list_id": list_id, "items": [], "summary": "No items to curate"}

    # Collect item names for the curator
    item_names = [r["item_name"] for r in rows]

    # Run the full curator pipeline
    print(f"\n{'='*60}")
    print(f"  CURATING LIST #{list_id}: {len(item_names)} items")
    print(f"{'='*60}")

    result = curate_basket(item_names, list_id=list_id)

    # Build a lookup from input_name -> curator result
    curator_lookup = {}
    for item in result["items"]:
        curator_lookup[item["input_name"].lower()] = item

    # Update each list_item with the curator's pick
    updated = 0
    compromises = []
    for row in rows:
        curated = curator_lookup.get(row["item_name"].lower())
        if not curated or curated["status"] == "unmatched":
            continue

        # Find the catalog_id from the best option
        best_option = None
        for opt in curated.get("options", []):
            if opt.get("store_id") == curated.get("recommended_store_id"):
                best_option = opt
                break
        if not best_option and curated.get("options"):
            best_option = curated["options"][0]

        catalog_id = best_option.get("catalog_id") if best_option else None
        new_store_id = curated.get("recommended_store_id") or row["store_id"]
        new_price = curated.get("recommended_price") or row["quantity"] * 10
        new_qty = curated.get("recommended_qty", 1)

        # Build selection context as JSON (stored in failure_context for now)
        sel_reason = curated.get("selection_reason", {})
        selection_json = json.dumps({
            "curator_pick": True,
            "product_name": curated.get("recommended_product"),
            "store": curated.get("recommended_store"),
            "score": best_option.get("score") if best_option else None,
            "tier": best_option.get("tier") if best_option else None,
            "reasons": best_option.get("reasons", []) if best_option else [],
            "directive_met": sel_reason.get("directive_met", True),
            "compromises": sel_reason.get("compromises", []),
            "summary": sel_reason.get("summary", ""),
            "qty": new_qty,
        })

        # Get category from product catalog for category_override
        cat_row = cur.execute(
            "SELECT category FROM product_catalog WHERE id = ?", (catalog_id,)
        ).fetchone() if catalog_id else None

        cur.execute("""
            UPDATE list_items
            SET catalog_id = ?,
                store_id = ?,
                quantity = ?,
                estimated_price = ?,
                failure_context = ?,
                category_override = ?
            WHERE id = ?
        """, (
            catalog_id,
            new_store_id,
            new_qty,
            new_price,
            selection_json,
            cat_row["category"] if cat_row else None,
            row["list_item_id"],
        ))
        updated += 1

        if sel_reason and not sel_reason.get("directive_met", True):
            compromises.append({
                "item": row["item_name"],
                "issue": sel_reason.get("compromises", ["Unknown"])[0],
            })

    conn.commit()

    print(f"\n  Updated {updated}/{len(rows)} items with curator picks")
    if compromises:
        print(f"  Compromises: {len(compromises)}")
        for c in compromises:
            print(f"    [!] {c['item']}: {c['issue']}")
    print()

    if close_conn:
        conn.close()

    return {
        "list_id": list_id,
        "items_curated": updated,
        "total_items": len(rows),
        "compromises": compromises,
        "curator_result": result,
    }


    target = None
    dry_run = "--dry-run" in sys.argv
    show_mode = "--show" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--date" and i + 1 < len(sys.argv):
            target = sys.argv[i + 1]

    if show_mode:
        # Show the most recent list
        result = list_get()
        _print_list(result)
    else:
        # Generate a new list
        mode = "DRY RUN" if dry_run else "SAVED"
        result = list_generate(target_date=target, dry_run=dry_run)

        if result["items"]:
            print(f"[{mode}] Generated list for {result['day_name']} {result['target_date']}")
            if result["list_id"]:
                print(f"  List ID: {result['list_id']}")
            _print_list(result)
        else:
            print(f"\nNo items due for restocking by {result['target_date']}.")
            print("This means either:")
            print("  - All items were recently ordered")
            print("  - next_restock_date hasn't been set yet")
            print("\nTip: Run 'python3 consumption_habits.py' first to seed restock rules.")
