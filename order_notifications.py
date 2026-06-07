"""Order-lifecycle WhatsApp notifications: placed / on-the-way / delivered.

Three stages, per the 2026-05-29 product call:
  placed     -> vendors + expected delivery date + item list
  on_the_way -> order info, NO item list
  delivered  -> short delivered note

VERIFY GATE: config.ORDER_NOTIFS_TEST_ONLY (default True) routes every send
to the curator (role='driver') ONLY, so the wording can be approved before
it ever reaches the rest of the household (Sara + Max). Flip
ORDER_NOTIFS_TEST_ONLY=false on the host to go live.

This module is render-first: format_* build the text, render_stage() returns
what WOULD be sent (no side effects), and send_stage() does the gated send.
Live wiring into the poller is intentionally separate so we never risk
double-sending alongside the older B2/B3/B5 hooks until they're reconciled.
"""
import sqlite3

from db_setup import DB_PATH
from app_logging import get_logger, log_event
from config import ORDER_NOTIFS_TEST_ONLY

logger = get_logger("order_notifications")

STAGES = ("placed", "on_the_way", "delivered")


def _fetch_orders(conn, order_ids):
    """Load order rows + their line items for the given ids."""
    orders = []
    for oid in order_ids:
        row = conn.execute(
            """SELECT o.id, o.order_ref, o.order_date, o.delivery_date,
                      o.total_amount, o.status, s.name AS store_name
               FROM orders o JOIN stores s ON s.id = o.store_id
               WHERE o.id = ?""",
            (oid,),
        ).fetchone()
        if not row:
            continue
        d = dict(row)
        d["items"] = [
            dict(r) for r in conn.execute(
                "SELECT raw_name, quantity FROM order_items "
                "WHERE order_id = ? ORDER BY id",
                (oid,),
            ).fetchall()
        ]
        orders.append(d)
    return orders


def _delivery_str(o):
    """Human-friendly delivery date: 'today' / 'tomorrow' / 'Fri 29 May',
    tolerant of a stored date OR full timestamp ('2026-05-29 12:48:04')."""
    dd = (o.get("delivery_date") or "").strip()
    if not dd:
        return "date to be confirmed"
    date_part = dd.split(" ")[0].split("T")[0]  # strip any time component
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(date_part, "%Y-%m-%d").date()
        today = _dt.now().date()
        delta = (d - today).days
        if delta == 0:
            return "today"
        if delta == 1:
            return "tomorrow"
        return d.strftime("%a %d %b")
    except Exception:
        return date_part


def format_placed(orders):
    """Placed: vendors + expected delivery date + item list (per order)."""
    n = len(orders)
    vendors = ", ".join(sorted({o["store_name"] for o in orders}))
    lines = [f"🛒 *{n} order{'s' if n != 1 else ''} placed* — {vendors}"]
    for o in orders:
        lines.append(f"\n*{o['store_name']}* · arriving {_delivery_str(o)}")
        items = o.get("items") or []
        for it in items[:25]:
            qty = it.get("quantity") or 1
            nm = (it.get("raw_name") or "?").strip()
            lines.append(f"  • {nm}" + (f" ×{int(qty)}" if qty and qty > 1 else ""))
        if not items:
            lines.append("  (items will show once the store confirms them)")
    return "\n".join(lines)


def format_on_the_way(order):
    """On the way: order info, NO item list."""
    return (f"🚚 *On the way* — your *{order['store_name']}* order is out for "
            f"delivery, arriving {_delivery_str(order)}.")


def format_delivered(order):
    """Delivered: short note."""
    return (f"✅ *Delivered* — your *{order['store_name']}* order has arrived. "
            f"Enjoy! 🎉")


def render_stage(stage, order_ids, conn=None):
    """Return the exact text that WOULD be sent for this stage. No side effects."""
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        orders = _fetch_orders(conn, order_ids)
        if not orders:
            return None
        if stage == "placed":
            return format_placed(orders)
        if stage == "on_the_way":
            return format_on_the_way(orders[0])
        if stage == "delivered":
            return format_delivered(orders[0])
        return None
    finally:
        if own:
            conn.close()


def _recipients(conn):
    """Who receives order notifications.

    VERIFY GATE: while ORDER_NOTIFS_TEST_ONLY is set, only the curator
    (role='driver') gets them, so wording is approved before it reaches the
    household. Live mode targets every other active member with a phone
    (Sara, Max, ...).
    """
    if ORDER_NOTIFS_TEST_ONLY:
        where = "COALESCE(role,'') = 'driver'"
    else:
        where = "COALESCE(role,'') != 'driver'"
    rows = conn.execute(
        f"""SELECT id, phone, COALESCE(display_name, name) AS nm
            FROM household_members
            WHERE is_active = 1 AND COALESCE(phone,'') != '' AND {where}"""
    ).fetchall()
    return [(r["id"], r["phone"], r["nm"]) for r in rows]


async def send_stage(stage, order_ids, conn=None):
    """Render + send a stage notification to the (gated) recipients.

    Returns {sent, recipients, test_only, text}. Caller is responsible for
    idempotency (don't call twice for the same order+stage).
    """
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        text = render_stage(stage, order_ids, conn=conn)
        if not text:
            return {"sent": 0, "reason": "no orders / unknown stage"}
        recips = _recipients(conn)
    finally:
        if own:
            conn.close()
    if not recips:
        log_event("order_notif_no_recipients", source="order_notifications",
                  detail=f"stage={stage} test_only={ORDER_NOTIFS_TEST_ONLY}")
        return {"sent": 0, "reason": "no recipients", "text": text}
    from whatsapp_client import wa_client
    sent = 0
    for member_id, phone, nm in recips:
        try:
            mid = await wa_client.send_message(phone, text, member_id=member_id)
            if mid:
                sent += 1
        except Exception as e:
            logger.warning("order notif send failed to %s: %s", nm, e)
    log_event("order_notif_sent", source="order_notifications",
              detail=(f"stage={stage} orders={list(order_ids)} sent={sent} "
                      f"test_only={ORDER_NOTIFS_TEST_ONLY}"))
    return {"sent": sent, "recipients": len(recips),
            "test_only": ORDER_NOTIFS_TEST_ONLY, "text": text}


async def process_household_notifications(conn=None):
    """Idempotent per-stage household notifications, safe to call every poll.

    Sends each order's placed / on-the-way / delivered notice exactly once
    (tracked by orders.notif_*_at), then stamps. Only stamps when a send
    actually succeeded, so a transient failure retries next poll. 'placed'
    is limited to orders from the last 3 days so historical orders never
    retro-notify (belt-and-suspenders with the one-time backfill stamp).

    Returns {placed, on_the_way, delivered} counts of messages sent.
    """
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    summary = {"placed": 0, "on_the_way": 0, "delivered": 0}
    try:
        placed_ids = [r["id"] for r in conn.execute(
            """SELECT id FROM orders
               WHERE notif_placed_at IS NULL
                 AND COALESCE(order_ref,'') != ''
                 AND status IN ('confirmed','processing','dispatched','delivered')
                 AND COALESCE(order_date, scraped_at, datetime('now'))
                     >= datetime('now','-3 days')
               ORDER BY id"""
        ).fetchall()]
        otw_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM orders WHERE notif_otw_at IS NULL AND status = 'dispatched'"
        ).fetchall()]
        deliv_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM orders WHERE notif_delivered_at IS NULL AND status = 'delivered'"
        ).fetchall()]

        # PLACED -- one combined message for all newly-placed orders.
        if placed_ids:
            res = await send_stage("placed", placed_ids)
            if res.get("sent"):
                conn.execute(
                    f"UPDATE orders SET notif_placed_at = datetime('now') "
                    f"WHERE id IN ({','.join('?' * len(placed_ids))})",
                    placed_ids,
                )
                conn.commit()
                summary["placed"] = res["sent"]
        # ON THE WAY / DELIVERED -- per order.
        for oid in otw_ids:
            res = await send_stage("on_the_way", [oid])
            if res.get("sent"):
                conn.execute(
                    "UPDATE orders SET notif_otw_at = datetime('now') WHERE id = ?",
                    (oid,))
                conn.commit()
                summary["on_the_way"] += res["sent"]
        for oid in deliv_ids:
            res = await send_stage("delivered", [oid])
            if res.get("sent"):
                conn.execute(
                    "UPDATE orders SET notif_delivered_at = datetime('now') WHERE id = ?",
                    (oid,))
                conn.commit()
                summary["delivered"] += res["sent"]
    finally:
        if own:
            conn.close()
    if any(summary.values()):
        log_event("household_order_notifs", source="order_notifications",
                  detail=str(summary))
    return summary
