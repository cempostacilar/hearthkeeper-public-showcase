"""Price intelligence: baselines + what drives price.

The cross-vendor comparison taught us that a raw "cheaper" number is meaningless
without a sense of what a thing SHOULD cost: Driscoll's strawberries at AED 6/kg
isn't a deal, it's bad data; a grass-fed sausage costing more isn't overpriced,
the attribute drives it. This module builds that sense:

  - item_price_baseline(item_id)  — the normalised unit-price band (per kg / L /
    piece) for an item, from its clean catalog alternatives.
  - category_price_baseline(cat)  — the same at category level, for items with
    thin data.
  - price_plausibility(...)        — is a given unit price normal / too cheap /
    too expensive vs the baseline? (the bad-data + fake-deal guard).
  - attribute_price_effect(cat)    — how much organic / origin country / brand /
    pack size move the unit price: the drivers.

All read-only. Unit prices come from product_score._calculate_unit_price; we only
ever compare within the SAME base unit. Stats are plain-Python percentiles (no
numpy) over the cleaned item_alternatives + product_catalog.
"""
import re
import sqlite3

from db_setup import DB_PATH


def _conn(conn):
    if conn is not None:
        return conn, False
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c, True


def _unit_price(price, size):
    try:
        from product_score import _calculate_unit_price
        return _calculate_unit_price(price, size)
    except Exception:
        return None, None


# Origin country is free-text and messy ("UAE", "Uae", "United Arab Emirates").
# Normalise to a canonical lower-case key so per-origin medians aren't fragmented.
_ORIGIN_CANON = {
    "uae": "uae", "u.a.e": "uae", "united arab emirates": "uae",
    "ksa": "saudi arabia", "saudi": "saudi arabia",
    "usa": "usa", "us": "usa", "u.s.a": "usa", "united states": "usa",
    "uk": "uk", "u.k": "uk", "united kingdom": "uk", "great britain": "uk",
    "holland": "netherlands", "the netherlands": "netherlands",
}


def _norm_origin(s):
    if not s:
        return None
    k = re.sub(r"[^a-z ]", "", str(s).strip().lower()).strip()
    return _ORIGIN_CANON.get(k, k) or None


def _name_organic(name):
    return "organic" in (name or "").lower()


# "Type" = the household's revealed quality/variety choice, read from the bought
# product's name (order_items.raw_name carries it: "Beef Mince (Grass Fed)",
# "Organic Free Range Eggs", "Greek Yoghurt (Natural)"). Prices vary a LOT by
# type, so a baseline that ignores type is meaningless. Ordered most-to-least
# specific; the first match per family wins where they're exclusive.
_TYPE_TAGS = [
    ("organic",     ("organic",)),
    ("grass-fed",   ("grass fed", "grass-fed", "grass finished")),
    ("free-range",  ("free range", "free-range", "pasture", "pastured")),
    ("wagyu",       ("wagyu",)),
    ("wild-caught", ("wild caught", "wild-caught", "wild ")),
    ("a2",          ("a2 ", "a2milk", "a2 milk")),
    ("greek",       ("greek",)),
    ("low-fat",     ("low fat", "low-fat", "skim", "skimmed", "light", "lite")),
    ("full-fat",    ("full fat", "full-fat", "whole milk", "full cream")),
]


def product_type(name):
    """Sorted tuple of type tags for a product name, or ('standard',) if none.
    The household's revealed type choice — what they actually buy."""
    n = " " + (name or "").lower() + " "
    tags = [tag for tag, kws in _TYPE_TAGS if any(k in n for k in kws)]
    return tuple(sorted(tags)) if tags else ("standard",)


def _mode(vals):
    """Most common value (the household's typical choice), or None."""
    counts = {}
    for v in vals:
        if v is not None:
            counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _pctile(sorted_vals, p):
    """Linear-interpolated percentile of a pre-sorted list (p in 0..100)."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (p / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return round(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac, 2)


def _stats(vals):
    if not vals:
        return None
    s = sorted(vals)
    return {
        "n": len(s),
        "min": round(s[0], 2),
        "p25": _pctile(s, 25),
        "median": _pctile(s, 50),
        "p75": _pctile(s, 75),
        "max": round(s[-1], 2),
    }


# ── Unit-price collection ──

def _item_unit_rows(conn, item_id):
    """Clean per-product (unit_price, base_unit, is_organic, origin, brand,
    price, size, name) for an item's non-avoid, in-stock, sized alternatives."""
    rows = conn.execute(
        """SELECT pc.price, pc.size, pc.brand, pc.origin_country,
                  pc.product_name, pc.is_organic
           FROM item_alternatives ia
           JOIN product_catalog pc ON pc.id = ia.catalog_id
           WHERE ia.item_id = ? AND COALESCE(ia.tier,'') != 'avoid'
             AND pc.is_available = 1 AND COALESCE(pc.price,0) > 0
             AND pc.size IS NOT NULL AND TRIM(pc.size) != ''""",
        (item_id,),
    ).fetchall()
    out = []
    for r in rows:
        up, bu = _unit_price(float(r["price"]), r["size"])
        if up and bu:
            out.append({
                "unit_price": up, "base_unit": bu, "price": float(r["price"]),
                "size": r["size"], "brand": r["brand"],
                "origin": _norm_origin(r["origin_country"]),
                "name": r["product_name"],
                "organic": _name_organic(r["product_name"]),
            })
    return out


def _dominant_base_unit(rows):
    counts = {}
    for r in rows:
        counts[r["base_unit"]] = counts.get(r["base_unit"], 0) + 1
    return max(counts, key=counts.get) if counts else None


# ── Baselines ──

def item_price_baseline(item_id, conn=None):
    """Normalised unit-price band for an item (the dominant base unit), from its
    clean catalog alternatives, plus the household's typical paid pack price."""
    c, own = _conn(conn)
    try:
        rows = _item_unit_rows(c, item_id)
        if not rows:
            return {"item_id": item_id, "base_unit": None, "stats": None,
                    "household_pack_price": None}
        bu = _dominant_base_unit(rows)
        stats = _stats([r["unit_price"] for r in rows if r["base_unit"] == bu])
        hh = c.execute(
            """SELECT AVG(oi.unit_price) FROM order_items oi
               JOIN orders o ON o.id = oi.order_id
               WHERE oi.item_id = ? AND oi.unit_price > 0
                 AND o.status IN ('delivered','completed')""",
            (item_id,),
        ).fetchone()
        return {
            "item_id": item_id, "base_unit": bu, "stats": stats,
            "household_pack_price": round(hh[0], 2) if hh and hh[0] else None,
        }
    finally:
        if own:
            c.close()


def _category_product_rows(conn, category, cols="pc.price, pc.size"):
    """Distinct clean products that are alternatives for items in a HOUSEHOLD
    category (items.category — product_catalog.category is the store's taxonomy,
    not Dairy/Meat/Produce/Pantry, so we must join through item_alternatives)."""
    return conn.execute(
        f"""SELECT DISTINCT pc.id AS cid, {cols}
            FROM item_alternatives ia
            JOIN items i ON i.id = ia.item_id
            JOIN product_catalog pc ON pc.id = ia.catalog_id
            WHERE i.category = ? AND COALESCE(ia.tier,'') != 'avoid'
              AND pc.is_available = 1 AND COALESCE(pc.price,0) > 0
              AND pc.size IS NOT NULL AND TRIM(pc.size) != ''""",
        (category,),
    ).fetchall()


def category_price_baseline(category, conn=None, base_unit=None):
    """Unit-price band across the clean, sized products that match items in a
    household category. Fallback baseline for items with too few of their own."""
    c, own = _conn(conn)
    try:
        rows = _category_product_rows(c, category)
        by_unit = {}
        for r in rows:
            up, bu = _unit_price(float(r["price"]), r["size"])
            if up and bu:
                by_unit.setdefault(bu, []).append(up)
        if not by_unit:
            return {"category": category, "base_unit": None, "stats": None}
        bu = base_unit or max(by_unit, key=lambda k: len(by_unit[k]))
        return {"category": category, "base_unit": bu, "stats": _stats(by_unit.get(bu, []))}
    finally:
        if own:
            c.close()


# ── Historical baseline (learned from what the household actually buys) ──

def _history_rows(conn, item_id):
    """Priced purchase history for an item, each row carrying the bought
    product's TYPE (from raw_name), SIZE, pack price and normalised unit price."""
    rows = conn.execute(
        """SELECT oi.raw_name, oi.size, oi.unit_price, oi.quantity, oi.brand
           FROM order_items oi
           JOIN orders o ON o.id = oi.order_id
           WHERE oi.item_id = ? AND oi.unit_price > 0
             AND o.status IN ('delivered','completed')""",
        (item_id,),
    ).fetchall()
    out = []
    for r in rows:
        up, bu = _unit_price(float(r["unit_price"]), r["size"])
        out.append({
            "type": product_type(r["raw_name"]),
            "size": (r["size"] or "").strip() or None,
            "pack_price": float(r["unit_price"]),
            "unit_price": up, "base_unit": bu,
            "brand": r["brand"], "name": r["raw_name"],
        })
    return out


def _summarize_history(recs):
    """Roll a list of history rows into a type+size aware baseline block."""
    if not recs:
        return None
    by_type = {}
    for r in recs:
        by_type.setdefault(r["type"], []).append(r)
    type_blocks = {}
    for t, rs in by_type.items():
        ups = [r["unit_price"] for r in rs if r["unit_price"] is not None]
        bu = _mode([r["base_unit"] for r in rs if r["base_unit"]])
        ups = [r["unit_price"] for r in rs if r["base_unit"] == bu and r["unit_price"]]
        type_blocks["+".join(t)] = {
            "n": len(rs),
            "typical_size": _mode([r["size"] for r in rs]),
            "pack_price": _stats([r["pack_price"] for r in rs]),
            "unit_price": _stats(ups),
            "base_unit": bu,
            "typical_brand": _mode([r["brand"] for r in rs]),
        }
    type_blocks = dict(sorted(type_blocks.items(),
                              key=lambda kv: kv[1]["n"], reverse=True))
    dominant_type = next(iter(type_blocks)) if type_blocks else None
    return {
        "n_purchases": len(recs),
        "dominant_type": dominant_type,
        "dominant_size": _mode([r["size"] for r in recs]),
        "by_type": type_blocks,
    }


def historical_baseline(item_id, conn=None):
    """Per-item pricing baseline learned from PURCHASE HISTORY, segmented by the
    TYPE the household actually buys (organic / grass-fed / greek / full-fat ...)
    and by SIZE. Reveals the dominant type + typical size + the price band per
    type — so a baseline isn't a market average but 'what WE buy and pay'.
    Returns None when there's no priced history for the item."""
    c, own = _conn(conn)
    try:
        recs = _history_rows(c, item_id)
        if not recs:
            return {"item_id": item_id, "n_purchases": 0, "by_type": {}}
        out = _summarize_history(recs)
        out["item_id"] = item_id
        return out
    finally:
        if own:
            c.close()


def _category_history_rows(conn, category):
    rows = conn.execute(
        """SELECT oi.raw_name, oi.size, oi.unit_price, oi.quantity, oi.brand
           FROM order_items oi
           JOIN orders o ON o.id = oi.order_id
           JOIN items i ON i.id = oi.item_id
           WHERE i.category = ? AND oi.unit_price > 0
             AND o.status IN ('delivered','completed')""",
        (category,),
    ).fetchall()
    out = []
    for r in rows:
        up, bu = _unit_price(float(r["unit_price"]), r["size"])
        out.append({
            "type": product_type(r["raw_name"]),
            "size": (r["size"] or "").strip() or None,
            "pack_price": float(r["unit_price"]),
            "unit_price": up, "base_unit": bu,
            "brand": r["brand"], "name": r["raw_name"],
        })
    return out


def category_historical_baseline(category, conn=None):
    """Category-level historical baseline: rolls every item's priced history in
    the category into the same type+size view. The fallback baseline for items
    the household hasn't bought enough of on their own."""
    c, own = _conn(conn)
    try:
        out = _summarize_history(_category_history_rows(c, category)) or {
            "n_purchases": 0, "by_type": {}}
        out["category"] = category
        return out
    finally:
        if own:
            c.close()


# ── Accepted-price band + preference-aware recommendation reference ──
#
# The historical baseline is our ACCEPTED price band (with a tolerance range).
# It's the reference for recommendations, cross-referenced with PREFERENCE:
#   * A cheap option of a non-preferred TYPE is no benefit (we buy organic eggs;
#     a cheap non-organic egg doesn't help us).
#   * WITHIN the preferred type, a price far above what we've accepted is
#     "outrageous for our purchase power" (2 vs 9 AED/egg), even if it's organic.
# Organic preference is LEARNED PER ITEM from history (we buy organic eggs but
# standard beef mince), falling back to the household-wide flag.

def household_prefers_organic(conn):
    row = conn.execute(
        "SELECT pref_value FROM household_preferences WHERE pref_key='prefer_organic'"
    ).fetchone()
    return bool(row) and str(row[0]).strip().lower() == "true"


def item_organic_preference(item_id, conn=None):
    """Learned per-item: do WE buy organic for THIS item? Majority of priced
    purchases organic -> True; minority -> False; no history -> None (caller
    falls back to the household-wide prefer_organic flag)."""
    c, own = _conn(conn)
    try:
        recs = _history_rows(c, item_id)
        if not recs:
            return None
        org = sum(1 for r in recs if "organic" in r["type"])
        return org >= max(1, len(recs) * 0.5)
    finally:
        if own:
            c.close()


def _band_from_rows(rows, organic_required):
    if organic_required is True:
        rows = [r for r in rows if "organic" in r["type"]]
    elif organic_required is False:
        rows = [r for r in rows if "organic" not in r["type"]]
    pairs = [(r["unit_price"], r["base_unit"]) for r in rows if r["unit_price"]]
    if not pairs:
        return None, None
    bu = _mode([p[1] for p in pairs])
    return _stats([p[0] for p in pairs if p[1] == bu]), bu


def accepted_band(item_id, conn=None, organic_required=None, category=None, min_n=3):
    """The unit-price band we've HISTORICALLY ACCEPTED for an item (optionally
    restricted to organic / non-organic), falling back to category history when
    the item has < min_n of its own. Returns {base_unit, stats, source} or None."""
    c, own = _conn(conn)
    try:
        st, bu = _band_from_rows(_history_rows(c, item_id), organic_required)
        src = "item"
        if (not st or st["n"] < min_n) and category:
            cst, cbu = _band_from_rows(_category_history_rows(c, category), organic_required)
            if cst and (not st or cst["n"] > st["n"]):
                st, bu, src = cst, cbu, "category"
        return {"base_unit": bu, "stats": st, "source": src} if st else None
    finally:
        if own:
            c.close()


def price_verdict(item_id, unit_price, base_unit, conn=None, organic_required=None,
                  category=None, accept_mult=1.4, outrage_mult=2.2):
    """Where a unit price sits vs our accepted band for that item+organic-ness:
    'below_usual' / 'accepted' / 'premium' (above usual, preference may justify) /
    'outrageous' (far beyond what we've ever paid). Tolerance: accepted up to
    ~1.4x median or 1.1x our historical max; outrageous past ~2.2x median or
    1.3x max."""
    c, own = _conn(conn)
    try:
        band = accepted_band(item_id, conn=c, organic_required=organic_required,
                             category=category)
        if not band or band["base_unit"] != base_unit or not unit_price:
            return {"verdict": "unknown", "ratio": None, "median": None, "source": None}
        st = band["stats"]
        med, mx, mn = st["median"], st["max"], st["min"]
        if not med or med <= 0:
            return {"verdict": "unknown", "ratio": None, "median": None,
                    "source": band["source"]}
        accepted_ceiling = max(med * accept_mult, mx * 1.1)
        outrage_floor = max(med * outrage_mult, mx * 1.3)
        if unit_price < mn * 0.6:
            v = "below_usual"
        elif unit_price <= accepted_ceiling:
            v = "accepted"
        elif unit_price >= outrage_floor:
            v = "outrageous"
        else:
            v = "premium"
        return {"verdict": v, "ratio": round(unit_price / med, 2), "median": med,
                "max": mx, "accepted_ceiling": round(accepted_ceiling, 2),
                "source": band["source"]}
    finally:
        if own:
            c.close()


def evaluate_option(item_id, product_name, price, size, conn=None, category=None):
    """THE recommendation reference. Cross-references our historical accepted
    price band with our learned type preference:
      1. preference gate — if we buy organic for this item, a non-organic option
         is NOT a benefit regardless of price;
      2. within the preferred type, judge the price vs our accepted band
         (2 vs 9 AED/egg -> outrageous).
    Returns {recommend, preferred_type, organic, unit_price, base_unit,
    price_verdict, median, accepted_ceiling, reason}."""
    c, own = _conn(conn)
    try:
        cand_organic = _name_organic(product_name)
        pref = item_organic_preference(item_id, c)
        if pref is None:
            pref = household_prefers_organic(c)
        up, bu = _unit_price(price, size)
        if pref and not cand_organic:
            return {"recommend": False, "preferred_type": False,
                    "organic": False, "unit_price": up, "base_unit": bu,
                    "price_verdict": "n/a",
                    "reason": "we buy organic for this item — a non-organic "
                              "option isn't a benefit regardless of price"}
        pv = price_verdict(item_id, up, bu, conn=c,
                           organic_required=cand_organic, category=category)
        recommend = pv["verdict"] in ("accepted", "below_usual")
        reason = {
            "below_usual": "cheaper than we usually pay — a genuine deal",
            "accepted": "within our usual accepted range",
            "premium": "above our usual; preference/availability may justify it",
            "outrageous": "far above what we've ever paid — not worth it for us",
            "unknown": "no historical price to compare against",
        }.get(pv["verdict"], "")
        return {"recommend": recommend, "preferred_type": True,
                "organic": cand_organic, "unit_price": up, "base_unit": bu,
                "price_verdict": pv["verdict"], "median": pv.get("median"),
                "accepted_ceiling": pv.get("accepted_ceiling"),
                "source": pv.get("source"), "reason": reason}
    finally:
        if own:
            c.close()


def _slim(scored):
    """A candidate trimmed to the fields a recommendation surface needs."""
    ev = scored.get("eval", {})
    return {
        "name": scored.get("name") or scored.get("product_name"),
        "price": scored.get("price"), "size": scored.get("size"),
        "store_id": scored.get("store_id"), "catalog_id": scored.get("catalog_id"),
        "unit_price": ev.get("unit_price"), "base_unit": ev.get("base_unit"),
        "price_verdict": ev.get("price_verdict"),
        "preferred_type": ev.get("preferred_type"),
        "median": ev.get("median"),
    }


def recommend_for_item(item_id, candidates, conn=None, category=None):
    """Pick the best option for an item against our ACCEPTED price band, and when
    NOTHING qualifies, make a call-out and offer the best next.

      status='ok'        -> an in-band option of our preferred type exists; `pick`
                            is the cheapest of them.
      status='callout'   -> we HAVE a historical band but nothing met it; `pick`
                            is None, `best_next` is the least-bad option (cheapest
                            of our preferred type, else cheapest overall), and
                            `callout` explains it ('priced above your usual …').
      status='no_baseline' -> no history to judge; pick the best preferred-type,
                            no call-out.
      status='none'      -> no candidates.

    candidates: [{name|product_name, price, size, store_id?, catalog_id?}].
    """
    c, own = _conn(conn)
    try:
        scored = []
        for cand in candidates or []:
            ev = evaluate_option(
                item_id, cand.get("name") or cand.get("product_name") or "",
                cand.get("price"), cand.get("size"), conn=c, category=category)
            scored.append({**cand, "eval": ev})
        base = {"item_id": item_id, "candidate_count": len(scored)}
        if not scored:
            return {**base, "status": "none", "pick": None, "best_next": None,
                    "callout": None, "in_band_count": 0}

        def up(s):
            return s["eval"].get("unit_price") or float("inf")

        in_band = [s for s in scored if s["eval"].get("recommend")]
        have_band = any(s["eval"].get("price_verdict")
                        in ("accepted", "below_usual", "premium", "outrageous")
                        for s in scored)
        if in_band:
            return {**base, "status": "ok", "in_band_count": len(in_band),
                    "pick": _slim(min(in_band, key=up)),
                    "best_next": None, "callout": None}

        preferred = [s for s in scored if s["eval"].get("preferred_type")]
        best_next = min(preferred or scored, key=up)
        bn = best_next["eval"]
        if not have_band:
            return {**base, "status": "no_baseline", "in_band_count": 0,
                    "pick": _slim(best_next), "best_next": None, "callout": None}

        unit = bn.get("unit_price")
        med = bn.get("median")
        verdict = bn.get("price_verdict")
        bu = bn.get("base_unit") or ""
        msg = (f"No option within your usual accepted range. Best next: "
               f"{best_next.get('name') or best_next.get('product_name') or '?'} "
               f"at {unit}/{bu} ({verdict}")
        if med:
            msg += f", vs your usual ~{med}/{bu}"
        msg += ")."
        if not preferred:
            msg = "No option of your preferred type within range. " + msg
        return {**base, "status": "callout", "in_band_count": 0,
                "pick": None, "best_next": _slim(best_next), "callout": msg}
    finally:
        if own:
            c.close()


def list_price_callouts(list_id, conn=None):
    """For each non-skipped item on a list, evaluate its AVAILABLE alternatives
    against our accepted band. Surfaces the items where NOTHING is within range
    (a call-out + the best next) so a pricier-than-usual cycle is flagged for the
    user rather than silently accepted."""
    c, own = _conn(conn)
    try:
        items = c.execute(
            """SELECT DISTINCT li.item_id, i.name AS item_name, i.category AS category
               FROM list_items li JOIN items i ON i.id = li.item_id
               WHERE li.list_id = ?
                 AND COALESCE(li.status,'') != 'skipped'
                 AND COALESCE(li.user_action,'') != 'skipped'
                 AND li.item_id IS NOT NULL""",
            (list_id,),
        ).fetchall()
        callouts, ok, no_baseline = [], 0, 0
        for it in items:
            cands = c.execute(
                """SELECT pc.product_name AS name, pc.price, pc.size,
                          ia.store_id, ia.catalog_id
                   FROM item_alternatives ia
                   JOIN product_catalog pc ON pc.id = ia.catalog_id
                   WHERE ia.item_id = ? AND COALESCE(ia.tier,'') != 'avoid'
                     AND pc.is_available = 1 AND COALESCE(pc.price,0) > 0""",
                (it["item_id"],),
            ).fetchall()
            res = recommend_for_item(it["item_id"], [dict(x) for x in cands],
                                     conn=c, category=it["category"])
            if res["status"] == "callout":
                callouts.append({"item_name": it["item_name"], **res})
            elif res["status"] == "ok":
                ok += 1
            elif res["status"] == "no_baseline":
                no_baseline += 1
        return {"list_id": list_id, "items": len(items), "in_band": ok,
                "no_baseline": no_baseline, "callouts": callouts}
    finally:
        if own:
            c.close()


def price_plausibility(item_id, unit_price, base_unit, conn=None,
                       category=None, low=0.45, high=2.2):
    """Is `unit_price` (in `base_unit`) normal for this item? Compares against the
    item baseline (falling back to the category band when the item has < 4 of its
    own products). Returns {verdict, ratio, median, source, plausible}. verdict:
    'too_cheap' (likely bad data / a smaller-or-different product), 'too_expensive'
    or 'normal'. Default band: < 0.45x median is too cheap, > 2.2x too expensive."""
    c, own = _conn(conn)
    try:
        base = item_price_baseline(item_id, conn=c)
        stats, src, bu = base["stats"], "item", base["base_unit"]
        if (not stats or stats["n"] < 4 or bu != base_unit) and category:
            cb = category_price_baseline(category, conn=c, base_unit=base_unit)
            if cb["stats"]:
                stats, src, bu = cb["stats"], "category", cb["base_unit"]
        if not stats or bu != base_unit or not unit_price:
            return {"verdict": "unknown", "ratio": None, "median": None,
                    "source": None, "plausible": True}
        med = stats["median"] or 0
        if med <= 0:
            return {"verdict": "unknown", "ratio": None, "median": None,
                    "source": src, "plausible": True}
        ratio = round(unit_price / med, 2)
        verdict = ("too_cheap" if ratio < low
                   else "too_expensive" if ratio > high else "normal")
        return {"verdict": verdict, "ratio": ratio, "median": med,
                "source": src, "plausible": verdict == "normal"}
    finally:
        if own:
            c.close()


# ── What drives price ──

def attribute_price_effect(category, conn=None):
    """How much each attribute moves the unit price within a category — the
    drivers. Returns the organic premium %, per-origin medians, and top brand
    medians, all within the dominant base unit (pack size is already normalised
    away by the unit price)."""
    c, own = _conn(conn)
    try:
        rows = _category_product_rows(
            c, category, cols="pc.price, pc.size, pc.brand, "
                              "pc.origin_country, pc.product_name")
        recs = []
        for r in rows:
            up, bu = _unit_price(float(r["price"]), r["size"])
            if up and bu:
                recs.append({"up": up, "bu": bu, "brand": r["brand"],
                             "origin": _norm_origin(r["origin_country"]),
                             "organic": _name_organic(r["product_name"])})
        if not recs:
            return {"category": category, "base_unit": None}
        bu = max({r["bu"] for r in recs},
                 key=lambda u: sum(1 for r in recs if r["bu"] == u))
        recs = [r for r in recs if r["bu"] == bu]

        def med(vals):
            s = sorted(vals)
            return _pctile(s, 50) if s else None

        org = med([r["up"] for r in recs if r["organic"]])
        con = med([r["up"] for r in recs if not r["organic"]])
        organic_premium = (round(100 * (org - con) / con, 0)
                           if org and con and con > 0 else None)

        def group_med(key, min_n=3):
            g = {}
            for r in recs:
                k = r[key]
                if k:
                    g.setdefault(k, []).append(r["up"])
            out = {k: med(v) for k, v in g.items() if len(v) >= min_n}
            return dict(sorted(out.items(), key=lambda kv: kv[1] or 0, reverse=True))

        return {
            "category": category, "base_unit": bu, "n": len(recs),
            "organic_median": org, "conventional_median": con,
            "organic_premium_pct": organic_premium,
            "by_origin": group_med("origin"),
            "by_brand": dict(list(group_med("brand").items())[:8]),
        }
    finally:
        if own:
            c.close()
