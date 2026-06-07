"""
Cadence calibration -- replace hardcoded HABITS frequency labels with
the OBSERVED cadence from actual order history.

The hardcoded labels in `consumption_habits.HABITS` are wishful: they
say "Cucumber: daily" (3.5 days), but the real Kibsons + Carrefour
purchase data shows the household orders cucumbers every ~27 days.
That bug makes the predictor 3-10x more aggressive than reality,
which then has knock-on effects everywhere (lists generated too
often, items marked due too eagerly, alternatives picked too soon).

This module recomputes `restock_rules.avg_days_between` (and the
derived `next_restock_date`) for every tracked item that has enough
order history to estimate a real cadence. Brand-aware: aggregates
across all `order_items.raw_name` variants that link to the same
`items.id`, so "Chiquita Bananas" + "Organic Bananas" + "Bananas"
all feed into the SAME observed-cadence number.

Designed to be safe and idempotent:
  - Items with <2 order observations keep their hardcoded HABITS
    cadence (no observation = no override).
  - We never extend a cadence past 60 days even if observation
    suggests it -- that path likely indicates brand-fragmented data
    rather than a genuine cooking_staple item.
  - next_restock_date is recomputed from last_ordered + new cadence,
    bounded so we don't push items further than 60 days into the
    future even when the math says so.

Usage:
    from cadence_calibration import refresh_observed_cadences
    refresh_observed_cadences()  # uses default 180d window, opens its own conn

Hooks:
  - Called by list_generate before each list is generated, so the
    cadence reflects the latest available data.
  - Called by the scheduler's nightly task (when wired up).
  - Safe to call ad-hoc from a diag endpoint.
"""

import re
import sqlite3
from datetime import datetime, timedelta
from db_setup import DB_PATH


# Bounds on the computed cadence. Outside these we ignore the
# observation and keep whatever HABITS said -- the data probably
# isn't telling us what it looks like it is.
MIN_OBSERVED_CADENCE = 1.0   # daily items
MAX_OBSERVED_CADENCE = 60.0  # roughly two months

# Per-staple cadence caps in DAYS. Key = a whole word that can appear in an item
# name. These exist ONLY as a safety net so basics don't wrongly fall off the
# list as "stocked": the household orders ~every 3.5 days and buys these
# essentially every order, so their real cadence is ~the order cadence. We cap
# the observed cadence at ~4d (a touch above the 3.5d order cadence) so a staple
# surfaces about every order even if its observed gap is briefly noisy.
#
# 2026-06-04: raised from 2-3d to 4d. The old tight caps were compensation for
# the Carrefour line-item gap (which inflated observed cadence to 13-33d). That
# gap is now FIXED (order_scraper text-receipt parser), so observed cadences are
# trustworthy and the 2d caps were over-shortening cadence -> over-stating daily
# consumption -> inflating suggested quantities (milk/bread/bananas showed 2-3).
# Whole-word matched, so "Eggplant" / "Buttermilk" / "Breadcrumbs" are NOT
# affected. The min() at the apply site means a real shorter observed cadence
# still passes through unchanged; this only bounds runaway-LONG observations.
STAPLE_MAX_CADENCE = {
    "bread": 4.0,
    "milk": 4.0,
    "kefir": 4.0,
    "banana": 4.0, "bananas": 4.0,
    "egg": 4.0, "eggs": 4.0,
    "yogurt": 4.0, "yoghurt": 4.0,
}
STAPLE_WORDS = set(STAPLE_MAX_CADENCE)


def _staple_caps(conn):
    """Map item_id -> max cadence (days). An item matching several staple words
    takes the TIGHTEST (smallest) cap. Whole-word matched, so
    "Eggplant" / "Buttermilk" / "Breadcrumbs" never match."""
    out = {}
    try:
        for row in conn.execute("SELECT id, name FROM items").fetchall():
            iid = row["id"] if isinstance(row, sqlite3.Row) else row[0]
            name = (row["name"] if isinstance(row, sqlite3.Row) else row[1]) or ""
            words = set(re.findall(r"[a-z]+", name.lower())) & STAPLE_WORDS
            if words:
                out[iid] = min(STAPLE_MAX_CADENCE[w] for w in words)
    except sqlite3.Error:
        pass
    return out


def _staple_item_ids(conn):
    """item_ids of staples (keys of _staple_caps). Thin wrapper for callers/
    tests that only need membership, not the per-item cap."""
    return set(_staple_caps(conn))


def refresh_observed_cadences(conn=None, days=180, verbose=False):
    """
    Recompute restock_rules.avg_days_between from observed order history.

    For each tracked items.id with >= 2 order_items rows linked to it,
    compute the observed cadence as:
        (last_ordered - first_ordered) / (order_count - 1)
    aggregated across ALL raw_name variants linked to that item.

    Then update:
      restock_rules.avg_days_between = clamped observed cadence
      restock_rules.next_restock_date = max(last_ordered + cadence, today)
      restock_rules.times_ordered = order_count
      restock_rules.last_ordered_date = last_ordered

    Returns dict with stats: {
        "updated": N,
        "kept_hardcoded": N (items with <2 observations),
        "clamped": N (items where observation was outside bounds),
    }
    """
    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own_conn = True

    try:
        # Aggregate per tracked item_id, brand-aware (all raw_name
        # variants linked to same item_id collapse together).
        #
        # Sources unioned:
        #   - order_items (Carrefour + Kibsons scraped orders)
        #   - external_purchases (chat-reported / receipt OCR / pattern)
        # Each "event" is keyed by source+id so a single order with
        # 3 items still counts as ONE event for cadence calculation
        # (matches the original COUNT DISTINCT order_id behavior).
        rows = conn.execute(
            """
            SELECT item_id,
                   COUNT(DISTINCT event_id) AS order_count,
                   MIN(purchase_date) AS first_ordered,
                   MAX(purchase_date) AS last_ordered,
                   AVG(quantity) AS avg_quantity
            FROM (
                SELECT oi.item_id,
                       'order:' || o.id AS event_id,
                       o.order_date AS purchase_date,
                       oi.quantity
                FROM order_items oi
                JOIN orders o ON oi.order_id = o.id
                WHERE oi.item_id IS NOT NULL
                  AND o.order_date IS NOT NULL
                  AND o.order_date >= date('now', ?)
                UNION ALL
                SELECT ep.item_id,
                       'ext:' || ep.id AS event_id,
                       ep.purchase_date,
                       ep.quantity
                FROM external_purchases ep
                WHERE ep.item_id IS NOT NULL
                  AND ep.purchase_date IS NOT NULL
                  AND ep.purchase_date >= date('now', ?)
            )
            GROUP BY item_id
            HAVING COUNT(DISTINCT event_id) >= 2
            """,
            (f"-{days} days", f"-{days} days"),
        ).fetchall()

        staple_caps = _staple_caps(conn)
        updated = 0
        kept = 0
        clamped = 0
        stapled = 0
        today_str = datetime.now().strftime("%Y-%m-%d")

        for r in rows:
            item_id = r["item_id"]
            try:
                first = datetime.strptime(r["first_ordered"], "%Y-%m-%d").date()
                last = datetime.strptime(r["last_ordered"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue

            # Guard against a future last_ordered (e.g. an order whose
            # delivery_date was set to a scheduled future slot, or a tz skew):
            # a future date makes days_since_last clamp to 0 -> the item looks
            # perpetually fresh and never comes due. Never record an order in
            # the future.
            _today = datetime.now().date()
            if last > _today:
                last = _today
            if first > last:
                first = last

            count = r["order_count"]
            if count < 2:
                kept += 1
                continue

            span_days = (last - first).days
            observed_cadence = span_days / (count - 1)

            # Staple floor: basics (bread/milk/kefir/eggs/yogurt/bananas) are
            # bought essentially every order. Gappy capture inflates their
            # observed gap, so cap each at its real consumption interval. Done
            # BEFORE the clamp so an inflated observation (e.g. 21d) lands at the
            # cap instead of being thrown out and reverting to wishful HABITS.
            cap = staple_caps.get(item_id)
            if cap is not None and observed_cadence > cap:
                if verbose:
                    print(f"  [staple] item_id={item_id} observed={observed_cadence:.1f}d -> {cap}d")
                observed_cadence = cap
                stapled += 1

            # Clamp out-of-range observations. Below MIN: usually
            # bulk-buy or restock-burst (multiple orders same day);
            # not a real cadence. Above MAX: usually brand-fragmented
            # data that has spread one real item across many names;
            # the aggregate will look genuine once auto-grow links it.
            if observed_cadence < MIN_OBSERVED_CADENCE or observed_cadence > MAX_OBSERVED_CADENCE:
                clamped += 1
                if verbose:
                    print(f"  [clamp] item_id={item_id} observed={observed_cadence:.1f}d (out of [{MIN_OBSERVED_CADENCE}, {MAX_OBSERVED_CADENCE}])")
                continue

            new_cadence = round(observed_cadence, 2)

            # next_restock_date = last_ordered + cadence, but never
            # pushed further than 60 days out (even for genuine
            # cooking-staples, we want them surfacing eventually).
            try:
                next_restock = last + timedelta(days=int(round(new_cadence)))
                # Never pick a date in the past as "next" -- if we're
                # already past it, the item is due NOW.
                if next_restock < datetime.now().date():
                    next_restock = datetime.now().date()
                next_restock_str = next_restock.strftime("%Y-%m-%d")
            except Exception:
                next_restock_str = today_str

            # Honor any Tier-0 drop push that's still in effect.
            # The user (or reviewer) intentionally pushed this item
            # past the next cycle via skip_tracking; cadence calibration
            # should never regress that. Take the LATER of (computed
            # cadence date, existing pushed date).
            try:
                existing_row = conn.execute(
                    "SELECT next_restock_date FROM restock_rules WHERE item_id = ?",
                    (item_id,),
                ).fetchone()
                if existing_row:
                    existing = (existing_row["next_restock_date"]
                                if isinstance(existing_row, sqlite3.Row)
                                else existing_row[0])
                    if existing and existing > next_restock_str:
                        next_restock_str = existing
            except sqlite3.Error:
                pass

            avg_qty = round(float(r["avg_quantity"] or 1.0), 2)
            confidence = min(0.95, 0.5 + 0.1 * min(count, 5))

            # Upsert -- restock_rules may not have a row for this
            # item if it was just auto-grown.
            conn.execute(
                """
                INSERT INTO restock_rules
                    (item_id, avg_days_between, avg_quantity,
                     last_ordered_date, next_restock_date,
                     confidence, times_ordered)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    avg_days_between = excluded.avg_days_between,
                    avg_quantity = excluded.avg_quantity,
                    -- last_ordered_date is FORWARD-ONLY. Captured order
                    -- history is incomplete (Carrefour line-items often
                    -- don't scrape), so the MAX(order_date) computed here
                    -- can be months stale. Overwriting backward would undo
                    -- a more-recent date set from delivery anchoring or the
                    -- finalized-list backfill, making just-bought items look
                    -- perpetually overdue. Only advance it -- EXCEPT a date
                    -- already in the future (a scheduled-slot delivery or tz
                    -- skew), which we DO pull back so it can't make the item
                    -- look perpetually fresh. excluded.* is clamped to <= today.
                    next_restock_date = CASE
                        WHEN restock_rules.last_ordered_date IS NULL
                          OR excluded.last_ordered_date > restock_rules.last_ordered_date
                          OR restock_rules.last_ordered_date > date('now')
                        THEN excluded.next_restock_date
                        ELSE restock_rules.next_restock_date END,
                    last_ordered_date = CASE
                        WHEN restock_rules.last_ordered_date IS NULL
                          OR excluded.last_ordered_date > restock_rules.last_ordered_date
                          OR restock_rules.last_ordered_date > date('now')
                        THEN excluded.last_ordered_date
                        ELSE restock_rules.last_ordered_date END,
                    confidence = excluded.confidence,
                    times_ordered = excluded.times_ordered,
                    updated_at = datetime('now')
                """,
                (
                    item_id, new_cadence, avg_qty,
                    last.strftime("%Y-%m-%d"), next_restock_str,
                    confidence, count,
                ),
            )
            updated += 1
            if verbose:
                print(f"  [cadence] item_id={item_id} new={new_cadence:.1f}d (was hardcoded) "
                      f"next_restock={next_restock_str} count={count}")

        # ── Correct inflated times_ordered (seed placeholder) ──
        # consumption_habits seeds every staple with times_ordered=99 so it
        # surfaces before any order history exists. But generic staples this
        # household never actually buys (salmon, beef sausages, pepper,
        # striploin...) then keep the 99 forever -- it bypasses
        # list_generate's "times_ordered >= 3" gate, so they show as
        # established staples despite ZERO real purchases. Reset times_ordered
        # DOWN to the real distinct-purchase count (order_items +
        # external_purchases, all-time). Never inflates -- only corrects a
        # placeholder/over-count down to reality. Gated on the household being
        # ESTABLISHED (has real order history) so a fresh install still gets
        # the seed bootstrap that makes staples appear at all.
        corrected = 0
        try:
            total_orders = conn.execute(
                "SELECT COUNT(*) FROM orders "
                "WHERE status IN ('delivered','completed')"
            ).fetchone()[0]
            if total_orders >= 10:
                real_count_sql = (
                    "SELECT COUNT(DISTINCT ev) FROM ("
                    "  SELECT 'order:' || oi.order_id AS ev FROM order_items oi "
                    "   WHERE oi.item_id = restock_rules.item_id"
                    "  UNION ALL"
                    "  SELECT 'ext:' || ep.id AS ev FROM external_purchases ep "
                    "   WHERE ep.item_id = restock_rules.item_id)"
                )
                corrected = conn.execute(
                    f"UPDATE restock_rules SET times_ordered = ({real_count_sql}), "
                    f"updated_at = datetime('now') "
                    f"WHERE times_ordered > ({real_count_sql})"
                ).rowcount
                if corrected and verbose:
                    print(f"  [times-fix] corrected {corrected} inflated "
                          f"times_ordered down to real purchase count")
        except sqlite3.Error as e:
            if verbose:
                print(f"  [times-fix] skipped: {e}")

        # ── Hard floor: no last_ordered_date may be in the FUTURE ──
        # A future last_ordered (created when a superseded draft targeting the
        # next order day fed its future target_date into
        # backfill_last_ordered_from_lists -- now bounded to <= today) makes
        # days_since_last clamp to 0, so the item looks perpetually fresh and
        # never comes due. The cadence UPSERT self-heal above only reaches
        # items that make it into the aggregation (>=2 in-range events);
        # out-of-range (clamped/continue'd, e.g. 60d-cadence produce) or sparse
        # items slip past it. This blanket clamp pulls ANY future date back to
        # today and recomputes next_restock. localtime matches the app's "today".
        future_fixed = 0
        try:
            future_fixed = conn.execute(
                "UPDATE restock_rules "
                "SET last_ordered_date = date('now','localtime'), "
                "    next_restock_date = date('now','localtime', "
                "        '+' || CAST(COALESCE(avg_days_between, 7) AS INT) || ' days'), "
                "    updated_at = datetime('now') "
                "WHERE last_ordered_date > date('now','localtime')"
            ).rowcount
            if future_fixed and verbose:
                print(f"  [future-fix] clamped {future_fixed} future last_ordered_date(s) to today")
        except sqlite3.Error as e:
            if verbose:
                print(f"  [future-fix] skipped: {e}")

        conn.commit()

        return {
            "updated": updated,
            "kept_hardcoded": kept,
            "clamped": clamped,
            "stapled": stapled,
            "times_corrected": corrected,
            "future_corrected": future_fixed,
        }
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    """CLI: refresh cadences with verbose output."""
    result = refresh_observed_cadences(verbose=True)
    print()
    print(f"Updated: {result['updated']}")
    print(f"Clamped (out of bounds): {result['clamped']}")
    print(f"Kept hardcoded (<2 obs):  {result['kept_hardcoded']}")
    print(f"Staple-floored: {result.get('stapled', 0)}  (caps: {STAPLE_MAX_CADENCE})")
