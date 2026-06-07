"""Delivery-fee-aware cart analysis.

The per-vendor delivery cost model lives in the existing `stores` columns
(`min_order_value` = free-delivery threshold, `delivery_fee` = fee charged when
the free condition isn't met) plus a `household_preferences` subscription flag.

Two vendor shapes (confirmed with the user 2026-06-07, all editable in Settings):
  - Carrefour — classic value threshold: free over AED 150 scheduled grocery,
    else ~AED 12.
  - Kibsons   — FLAT AED 8 per order regardless of cart size (min_order_value = 0
    => no value threshold), free only with their subscription.

Slice 1 (this module): load/edit the model + compute the per-vendor fee picture
for a list (subtotal, effective fee, shortfall-to-free). Padding (pull-forward to
clear a threshold) and cross-vendor consolidation build on top of analyze().
"""
import json
import sqlite3

from db_setup import DB_PATH

# Confirmed with the user 2026-06-07. Used only to SEED the stores table once
# (idempotent, guarded by a flag) and as a fallback for unknown vendors.
DEFAULT_MODEL = {
    "1": {"min_order_value": 0.0,   "delivery_fee": 8.0},   # Kibsons (flat fee)
    "2": {"min_order_value": 150.0, "delivery_fee": 12.0},  # Carrefour UAE
}
SUBSCRIPTION_PREF_KEY = "delivery_subscriptions"   # JSON {store_id: bool}
SEEDED_FLAG_KEY = "delivery_model_seeded_v1"


def _conn(conn):
    if conn is not None:
        return conn, False
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c, True


# ── Subscription flags (household_preferences JSON) ──

def get_subscriptions(conn=None):
    c, own = _conn(conn)
    try:
        row = c.execute(
            "SELECT pref_value FROM household_preferences WHERE pref_key=?",
            (SUBSCRIPTION_PREF_KEY,),
        ).fetchone()
        if not row:
            return {}
        try:
            return {str(k): bool(v) for k, v in json.loads(row[0]).items()}
        except Exception:
            return {}
    finally:
        if own:
            c.close()


def set_subscription(store_id, has_sub, conn=None):
    """Toggle a vendor's delivery subscription (e.g. Kibsons membership =>
    delivery becomes free in the math). Returns the updated map."""
    c, own = _conn(conn)
    try:
        subs = get_subscriptions(c)
        subs[str(store_id)] = bool(has_sub)
        c.execute(
            "INSERT INTO household_preferences (pref_key, pref_value) VALUES (?, ?) "
            "ON CONFLICT(pref_key) DO UPDATE SET pref_value=excluded.pref_value, "
            "updated_at=datetime('now')",
            (SUBSCRIPTION_PREF_KEY, json.dumps(subs)),
        )
        c.commit()
        return subs
    finally:
        if own:
            c.close()


# ── Delivery cost model (stores columns + subscription) ──

def ensure_delivery_model_seeded(conn=None):
    """Once-only: write the user-confirmed thresholds/fees into the stores table.
    Guarded by a household_preferences flag so it never clobbers later edits the
    user makes in Settings. Safe to call on every startup."""
    c, own = _conn(conn)
    try:
        seen = c.execute(
            "SELECT 1 FROM household_preferences WHERE pref_key=?", (SEEDED_FLAG_KEY,)
        ).fetchone()
        if seen:
            return False
        for sid, cfg in DEFAULT_MODEL.items():
            c.execute(
                "UPDATE stores SET min_order_value=?, delivery_fee=? WHERE id=?",
                (cfg["min_order_value"], cfg["delivery_fee"], int(sid)),
            )
        c.execute(
            "INSERT INTO household_preferences (pref_key, pref_value) VALUES (?, '1')",
            (SEEDED_FLAG_KEY,),
        )
        c.commit()
        return True
    finally:
        if own:
            c.close()


def load_delivery_model(conn=None):
    """Per-store {store_id, name, min_order_value, delivery_fee, has_subscription}
    from the stores table + subscription prefs, falling back to confirmed
    defaults for any unset column."""
    c, own = _conn(conn)
    try:
        subs = get_subscriptions(c)
        model = {}
        for r in c.execute("SELECT id, name, min_order_value, delivery_fee FROM stores"):
            sid = str(r["id"])
            dft = DEFAULT_MODEL.get(sid, {"min_order_value": 0.0, "delivery_fee": 0.0})
            mov = r["min_order_value"]
            fee = r["delivery_fee"]
            model[sid] = {
                "store_id": r["id"],
                "name": r["name"],
                "min_order_value": float(mov) if mov is not None else dft["min_order_value"],
                "delivery_fee": float(fee) if fee is not None else dft["delivery_fee"],
                "has_subscription": subs.get(sid, False),
            }
        return model
    finally:
        if own:
            c.close()


# ── Pure fee math ──

def effective_fee(vendor_cfg, subtotal):
    """PURE. The delivery fee actually charged for this vendor at this subtotal.
    Subscription => free. A positive min_order_value is a value threshold (free
    at/above it). min_order_value <= 0 means no threshold => the flat fee always
    applies (e.g. Kibsons) unless subscribed."""
    if vendor_cfg.get("has_subscription"):
        return 0.0
    fee = float(vendor_cfg.get("delivery_fee") or 0.0)
    mov = float(vendor_cfg.get("min_order_value") or 0.0)
    if mov > 0 and subtotal >= mov:
        return 0.0
    return fee


def shortfall_to_free(vendor_cfg, subtotal):
    """PURE. AED needed to reach free delivery, or None when the vendor has no
    value threshold (flat-fee or subscription vendors can't be padded to free)."""
    if vendor_cfg.get("has_subscription"):
        return None
    mov = float(vendor_cfg.get("min_order_value") or 0.0)
    if mov <= 0:
        return None
    return max(0.0, round(mov - float(subtotal or 0.0), 2))


def is_flat_fee(vendor_cfg):
    """PURE. True when the vendor charges a flat fee with no way to reach free via
    cart size (no threshold, no subscription) — padding it is pointless."""
    return (not vendor_cfg.get("has_subscription")
            and float(vendor_cfg.get("min_order_value") or 0.0) <= 0
            and float(vendor_cfg.get("delivery_fee") or 0.0) > 0)


# ── Per-list analysis ──

def analyze_list_delivery(list_id, conn=None):
    """Per-vendor delivery picture for a list's non-skipped items:
      {list_id, vendors: [{store_id, name, items, subtotal, min_order_value,
        delivery_fee_if_unmet, effective_fee, is_free, shortfall_to_free,
        has_subscription, flat_fee}], total_delivery_fees}
    Subtotal uses the per-item estimated_price, falling back to items.avg_price.
    """
    c, own = _conn(conn)
    try:
        model = load_delivery_model(c)
        rows = c.execute(
            """SELECT li.store_id AS sid,
                      ROUND(SUM(COALESCE(li.estimated_price, i.avg_price, 0)
                                * li.quantity), 2) AS subtotal,
                      COUNT(*) AS n
               FROM list_items li
               LEFT JOIN items i ON i.id = li.item_id
               WHERE li.list_id = ?
                 AND COALESCE(li.status,'') != 'skipped'
                 AND COALESCE(li.user_action,'') != 'skipped'
               GROUP BY li.store_id""",
            (list_id,),
        ).fetchall()
        vendors = []
        total_fees = 0.0
        for r in rows:
            sid = str(r["sid"]) if r["sid"] is not None else None
            cfg = model.get(sid) or {
                "store_id": r["sid"], "name": "?", "min_order_value": 0.0,
                "delivery_fee": 0.0, "has_subscription": False,
            }
            subtotal = float(r["subtotal"] or 0.0)
            fee = effective_fee(cfg, subtotal)
            total_fees += fee
            vendors.append({
                "store_id": cfg["store_id"],
                "name": cfg["name"],
                "items": r["n"],
                "subtotal": round(subtotal, 2),
                "min_order_value": cfg["min_order_value"],
                "delivery_fee_if_unmet": cfg["delivery_fee"],
                "effective_fee": round(fee, 2),
                "is_free": fee <= 0,
                "shortfall_to_free": shortfall_to_free(cfg, subtotal),
                "has_subscription": cfg["has_subscription"],
                "flat_fee": is_flat_fee(cfg),
            })
        vendors.sort(key=lambda v: v["store_id"])
        return {
            "list_id": list_id,
            "vendors": vendors,
            "total_delivery_fees": round(total_fees, 2),
        }
    finally:
        if own:
            c.close()
