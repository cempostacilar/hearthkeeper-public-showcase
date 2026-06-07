"""Per-item cross-vendor price selection (safe / unit-price based).

The user's "right vendor by price" lever. Each list item currently lands on its
fixed preferred_store_id; the only cross-vendor move is list_generate's
COUNT-based consolidation. This compares the item's chosen product against the
PREFERRED product at every other vendor and flags a cheaper one.

Two hard lessons baked in (a naive raw-price compare buys 150g yogurt and
baby-food "bananas"):
  1. Compare the PREFERRED (validator-vetted) product per store, not a re-rank of
     all alternatives — re-ranking lets pollution / odd packs win.
  2. Compare by UNIT price (AED per kg / L / piece) with the SAME base unit, and
     never downgrade organic -> conventional. A different base unit or an
     unparseable size means "not comparable" -> no suggestion (silence beats a
     wrong switch).

Read-only: returns suggestions, mutates nothing. Applying a switch — with the
delivery-fee guardrail from delivery_optimizer (don't move an item off Carrefour
if dropping below its free threshold costs more than the item saving) — is the
next slice, gated on the user seeing these numbers first.
"""
import sqlite3

from db_setup import DB_PATH


def _conn(conn):
    if conn is not None:
        return conn, False
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c, True


def _unit_price(price, size):
    """(unit_price, base_unit) per product_score, or (None, None) if unparseable."""
    try:
        from product_score import _calculate_unit_price
        return _calculate_unit_price(price, size)
    except Exception:
        return None, None


def _name_organic(d):
    """Reliably organic: the product NAME says so. Used for the CANDIDATE side of
    the downgrade guard, because the is_organic flag is unreliable in BOTH
    directions (Isigny Organic flagged 0; Lurpak — not organic — flagged 1)."""
    return "organic" in (d.get("product_name") or "").lower()


def _maybe_organic(d):
    """Possibly organic: flag OR name. Used for the CURRENT side so we PROTECT an
    item that might be organic from being swapped to a conventional one."""
    return bool(d.get("is_organic")) or _name_organic(d)


# Quality attributes that justify a higher price — never silently downgrade one
# away. The live check kept suggesting grass-fed chorizo -> a generic sausage as
# a "saving". Organic is handled separately (its flag is unreliable); these are
# name-only signals.
_QUALITY_ATTRS = (
    "grass fed", "grass-fed", "grass finished", "free range", "free-range",
    "pasture", "wagyu", "wild caught", "wild-caught", "a2",
)


def _quality_set(name):
    n = (name or "").lower()
    return {a for a in _QUALITY_ATTRS if a in n}


def best_preferred_at_store(conn, item_id, store_id):
    """The validator-vetted best product for this item at this store: the
    PREFERRED row first, then top-scored acceptable, in-stock, priced. Returns a
    dict with unit_price/base_unit attached, or None."""
    row = conn.execute(
        """SELECT ia.catalog_id, ia.is_preferred, ia.score, ia.tier,
                  pc.price, pc.size, pc.is_organic, pc.product_name, pc.brand
           FROM item_alternatives ia
           JOIN product_catalog pc ON pc.id = ia.catalog_id
           WHERE ia.item_id = ? AND ia.store_id = ?
             AND COALESCE(ia.tier,'') != 'avoid'
             AND pc.is_available = 1 AND COALESCE(pc.price,0) > 0
           ORDER BY ia.is_preferred DESC,
                    CASE ia.tier WHEN 'preferred' THEN 0
                                 WHEN 'acceptable' THEN 1 ELSE 2 END,
                    ia.score DESC, pc.price ASC
           LIMIT 1""",
        (item_id, store_id),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["price"] = float(d["price"] or 0)
    up, bu = _unit_price(d["price"], d["size"])
    d["unit_price"] = up
    d["base_unit"] = bu
    d["store_id"] = store_id
    return d


def _current_product(conn, list_item):
    """The product the list currently has for an item, with its unit price. List
    items usually carry no catalog_id (the product is resolved at render time),
    so when it's absent we fall back to the validator-vetted PREFERRED product at
    the item's assigned store — the right like-for-like baseline anyway."""
    cat_id = list_item["catalog_id"]
    if cat_id:
        row = conn.execute(
            "SELECT id AS catalog_id, price, size, is_organic, product_name, brand "
            "FROM product_catalog WHERE id = ?",
            (cat_id,),
        ).fetchone()
        if row:
            d = dict(row)
            price = float(list_item["estimated_price"] or d["price"] or 0)
            d["price"] = price
            up, bu = _unit_price(price, d["size"])
            d["unit_price"] = up
            d["base_unit"] = bu
            return d
    return best_preferred_at_store(conn, list_item["item_id"], list_item["store_id"])


def _pinned_item_ids(conn, list_id):
    rows = conn.execute(
        """SELECT DISTINCT li.item_id FROM list_items li
           JOIN user_item_prefs uip ON uip.item_id = li.item_id
           WHERE li.list_id = ? AND uip.pref_type = 'pin_product'
             AND COALESCE(uip.is_active, 1) = 1""",
        (list_id,),
    ).fetchall()
    return {r[0] for r in rows}


def analyze_list_vendor_savings(list_id, conn=None, min_pct=0.10):
    """For each non-skipped, non-pinned list item with a parseable current pick,
    compare its UNIT price against the preferred product at every OTHER active
    store. Flag a switch only when the candidate is >= min_pct cheaper per unit,
    in the SAME base unit, with no organic downgrade. READ-ONLY.

    AED `saving_estimate` is given only when the two packs are within 25% size of
    each other (so it's a like-for-like number); otherwise it's null and we lean
    on the unit-price delta. Returns
    {list_id, considered, comparable, suggestions, total_saving_estimate}.
    """
    c, own = _conn(conn)
    try:
        store_names = {r[0]: r[1] for r in c.execute("SELECT id, name FROM stores")}
        active = [r[0] for r in c.execute(
            "SELECT id FROM stores WHERE COALESCE(is_active,1)=1")]
        pinned = _pinned_item_ids(c, list_id)
        rows = c.execute(
            """SELECT li.id AS li_id, li.item_id, li.store_id, li.quantity,
                      li.estimated_price, li.catalog_id, li.item_name_text,
                      i.name AS item_name, i.category AS category
               FROM list_items li
               LEFT JOIN items i ON i.id = li.item_id
               WHERE li.list_id = ?
                 AND COALESCE(li.status,'') != 'skipped'
                 AND COALESCE(li.user_action,'') != 'skipped'""",
            (list_id,),
        ).fetchall()

        suggestions = []
        considered = comparable = 0
        total = 0.0
        for r in rows:
            item_id = r["item_id"]
            if not item_id or item_id in pinned:
                continue
            considered += 1
            cur = _current_product(c, r)
            if not cur or cur["unit_price"] is None:
                continue  # can't compare without a parseable current unit price
            comparable += 1
            best = None
            for sid in active:
                if sid == r["store_id"]:
                    continue
                cand = best_preferred_at_store(c, item_id, sid)
                if not cand or cand["unit_price"] is None:
                    continue
                if cand["base_unit"] != cur["base_unit"]:
                    continue  # different unit -> not a fair comparison
                if _maybe_organic(cur) and not _name_organic(cand):
                    continue  # never downgrade a maybe-organic item to conventional
                if _quality_set(cur["product_name"]) - _quality_set(cand["product_name"]):
                    continue  # candidate drops a quality attr (grass-fed, etc.)
                # Bad data / not-really-comparable: a price far below the item's
                # own baseline isn't a deal, it's a bad scrape or a smaller/
                # different product (Driscoll's strawberries @ AED 6/kg).
                try:
                    from price_intel import price_plausibility
                    if price_plausibility(item_id, cand["unit_price"],
                                          cand["base_unit"], conn=c,
                                          category=r["category"]).get("verdict") == "too_cheap":
                        continue
                except Exception:
                    pass
                # Historical accepted-band + LEARNED preference reference (our
                # "accepted price points"): never suggest a non-preferred TYPE
                # (a cheap non-organic isn't a benefit if we buy organic for this
                # item) or an off-the-charts price. Annotate with the verdict.
                try:
                    from price_intel import evaluate_option
                    ev = evaluate_option(item_id, cand["product_name"],
                                         cand["price"], cand["size"], conn=c,
                                         category=r["category"])
                    if ev.get("preferred_type") is False \
                            or ev.get("price_verdict") == "outrageous":
                        continue
                    cand["_verdict"] = ev.get("price_verdict")
                except Exception:
                    cand["_verdict"] = None
                if cand["unit_price"] < cur["unit_price"] * (1 - min_pct):
                    if best is None or cand["unit_price"] < best["unit_price"]:
                        best = cand
            if not best:
                continue
            pct = round(100 * (cur["unit_price"] - best["unit_price"]) / cur["unit_price"], 1)
            # AED estimate only when packs are like-for-like (within 25% size)
            saving_estimate = None
            try:
                from product_score import _parse_size, _normalize_to_base_unit
                cq, cu = _parse_size(cur["size"])
                bq, bu = _parse_size(best["size"])
                cqn, _ = _normalize_to_base_unit(cq, cu)
                bqn, _ = _normalize_to_base_unit(bq, bu)
                if cqn and bqn and min(cqn, bqn) / max(cqn, bqn) >= 0.75:
                    qty = float(r["quantity"] or 1)
                    saving_estimate = round((cur["price"] - best["price"]) * qty, 2)
                    if saving_estimate and saving_estimate > 0:
                        total += saving_estimate
            except Exception:
                pass
            suggestions.append({
                "list_item_id": r["li_id"],
                "item_id": item_id,
                "item_name": r["item_name"] or r["item_name_text"],
                "from_store": store_names.get(r["store_id"], "?"),
                "from_store_id": r["store_id"],
                "from_product": cur["product_name"],
                "from_unit_price": cur["unit_price"],
                "to_store": store_names.get(best["store_id"], "?"),
                "to_store_id": best["store_id"],
                "to_catalog_id": best["catalog_id"],
                "to_product": best["product_name"],
                "to_unit_price": best["unit_price"],
                "base_unit": cur["base_unit"],
                "pct_cheaper": pct,
                "saving_estimate": saving_estimate,
                "accepted_verdict": best.get("_verdict"),
            })
        suggestions.sort(key=lambda s: s["pct_cheaper"], reverse=True)
        return {
            "list_id": list_id,
            "considered": considered,
            "comparable": comparable,
            "suggestions": suggestions,
            "total_saving_estimate": round(total, 2),
        }
    finally:
        if own:
            c.close()
