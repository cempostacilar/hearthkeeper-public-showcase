"""
Tier-0 drop tracking: skip_events + next_restock_date push.

When a user drops an item from Scheduled (or via WhatsApp removes from
a feedback round), we want two things to happen:

  1. Log the action in `skip_events` -- so future pattern detection
     can spot repeated drops + so we have an audit trail.

  2. Push `restock_rules.next_restock_date` PAST the next list's
     coverage window. The natural cadence keeps running underneath
     -- the item will reappear at the next-next list, just not the
     very next one.

This module is intentionally narrow. Tier 1 (soft-nudge multipliers
via `cadence_adjustment`) and Tier 2 (one-time "should I stop
suggesting?" ask) live in a separate pattern_detector module, to be
added in the next slice.

Public API:
    record_skip_event(...)         -- low-level INSERT into skip_events
    apply_tier_zero_drop(...)      -- the high-level "user dropped X"
                                      handler used by app + WhatsApp

The push semantics: we set next_restock_date to the day AFTER the
next list's _coverage_cutoff so the upcoming list_generate skips
the item. The list AFTER that has a coverage window extending
further, so the item re-enters at the natural cadence.

Example (Tue/Fri household):
    Today = Tue May 26.
    User drops Potatoes (last bought ~Mar 31, next_restock = today).
    next_order_date() -> Fri May 29.
    _coverage_cutoff('2026-05-29') -> Tue Jun 2.
    push_to = Jun 3.
    -> Fri May 29 list: Potatoes NOT included (next_restock = Jun 3 > Jun 2 cutoff).
    -> Tue Jun 2 list: Potatoes INCLUDED (next_restock = Jun 3 <= Jun 5 next cutoff).
"""

import sqlite3
from datetime import datetime, timedelta
from db_setup import DB_PATH


def _resolve_item_from_list_item(conn, list_item_id):
    """Return (item_id, list_id) for a list_item, or (None, None)."""
    row = conn.execute(
        "SELECT item_id, list_id FROM list_items WHERE id = ?",
        (list_item_id,),
    ).fetchone()
    if not row:
        return None, None
    if isinstance(row, sqlite3.Row):
        return row["item_id"], row["list_id"]
    return row[0], row[1]


def _compute_skip_until(conn=None):
    """Compute the date to push next_restock_date to so that the
    upcoming list skips this item but the one after picks it up.

    Returns 'YYYY-MM-DD' string. Lazy-imports to avoid circular deps.
    """
    # Lazy imports — list_generate imports from a lot of places, and
    # this module wants to stay light to import.
    from agent_brain import next_order_date
    from list_generate import _coverage_cutoff

    try:
        next_target = next_order_date()  # next Tue/Fri (YYYY-MM-DD)
        cutoff = _coverage_cutoff(next_target)  # the Tue/Fri AFTER that
        cutoff_dt = datetime.strptime(cutoff, "%Y-%m-%d").date()
        # Push one day past the next list's coverage window.
        return (cutoff_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        # Fallback: 5 days from today (about one Tue/Fri-cycle gap).
        return (datetime.now().date() + timedelta(days=5)).strftime("%Y-%m-%d")


def record_skip_event(
    item_id,
    list_id,
    list_item_id,
    source,
    reason=None,
    member_id=None,
    conn=None,
):
    """INSERT a row into skip_events. Returns the new id, or None.

    `source` is required. `reason` is optional structured tag
    ('have_it' / 'wrong_pick' / 'this_week_only' / 'stop_suggesting').
    """
    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        own_conn = True
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO skip_events
                   (item_id, list_id, list_item_id, source, reason, member_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item_id, list_id, list_item_id, source, reason, member_id),
        )
        new_id = cur.lastrowid
        conn.commit()
        return new_id
    except sqlite3.Error:
        # Don't crash the drop just because the audit insert failed.
        # The user's drop still goes through; the next pattern run
        # will be slightly less accurate.
        return None
    finally:
        if own_conn:
            conn.close()


def push_next_restock_for_item(item_id, conn=None):
    """Push restock_rules.next_restock_date for this item past the next
    list's coverage window. No-op if there's no restock_rules row, or
    if next_restock_date is already further in the future.

    Returns the new (or unchanged) next_restock_date string, or None.
    """
    if not item_id:
        return None

    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        own_conn = True
    try:
        push_to = _compute_skip_until(conn=conn)
        cur = conn.cursor()
        # Only push FORWARD. If the existing date is already beyond
        # push_to (e.g. multiple drops, or natural cadence is huge),
        # leave it alone.
        cur.execute(
            """UPDATE restock_rules
               SET next_restock_date = ?,
                   updated_at = datetime('now')
               WHERE item_id = ?
                 AND (next_restock_date IS NULL OR next_restock_date < ?)""",
            (push_to, item_id, push_to),
        )
        conn.commit()
        # Read back the current value to report.
        row = cur.execute(
            "SELECT next_restock_date FROM restock_rules WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row:
            return row[0] if not isinstance(row, sqlite3.Row) else row["next_restock_date"]
        return push_to
    except sqlite3.Error:
        return None
    finally:
        if own_conn:
            conn.close()


def apply_tier_zero_drop(
    list_item_id,
    source,
    reason=None,
    member_id=None,
    conn=None,
):
    """High-level "user dropped this item" handler.

    Resolves item_id + list_id from list_item_id, inserts a skip_event,
    and pushes next_restock_date past the upcoming list's coverage
    window so the item doesn't reappear immediately.

    Returns a dict for callers / diag endpoints:
        {
            "list_item_id": int,
            "item_id": int or None,
            "list_id": int or None,
            "skip_event_id": int or None,
            "next_restock_date": str or None,
        }
    """
    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own_conn = True
    try:
        item_id, list_id = _resolve_item_from_list_item(conn, list_item_id)

        skip_id = record_skip_event(
            item_id=item_id,
            list_id=list_id,
            list_item_id=list_item_id,
            source=source,
            reason=reason,
            member_id=member_id,
            conn=conn,
        )

        new_restock_date = None
        if item_id:
            new_restock_date = push_next_restock_for_item(item_id, conn=conn)

        return {
            "list_item_id": list_item_id,
            "item_id": item_id,
            "list_id": list_id,
            "skip_event_id": skip_id,
            "next_restock_date": new_restock_date,
        }
    finally:
        if own_conn:
            conn.close()
