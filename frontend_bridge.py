"""
Hearthkeeper - Frontend Bridge

Transforms backend database data into the exact JSON shape
the React frontend expects (vendors > categories > items).

Used by app.py to send structured list data over WebSocket.
"""

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime

from db_setup import DB_PATH
from list_generate import list_get
from product_grading import grade_product, get_product_tags


# ============================================================
# CATEGORY & STORE NAME HELPERS
# ============================================================

def _simplify_category(raw_category: str) -> str:
    """
    Simplify catalog breadcrumb paths into human-friendly category names.

    e.g. "Bio & Organic Food > Organic Fruits & Vegetables > Organic Fruits" → "Produce"
         "Meat > Beef Veal" → "Meat"
         "Pantry > Pasta Noodles" → "Pantry"
    """
    if not raw_category:
        return "Other"

    parts = [p.strip() for p in raw_category.split(">")]
    top = parts[0]

    # "Bio & Organic Food > ..." — Carrefour's organic section.
    # Look at the 2nd level to infer the real category.
    if top == "Bio & Organic Food":
        if len(parts) >= 2:
            sub = parts[1].lower()
            if "fruit" in sub or "vegetable" in sub or "produce" in sub:
                return "Produce"
            if "dairy" in sub or "milk" in sub:
                return "Dairy"
            if "meat" in sub or "poultry" in sub:
                return "Meat"
            if "bakery" in sub or "bread" in sub:
                return "Bakery"
            if "pantry" in sub or "pasta" in sub:
                return "Pantry"
        return "Produce"  # sensible default for Carrefour organic section

    # "Fruits & Vegetables > ..." → Produce (also Kibsons standalone "Fruits" / "Vegetables")
    if top in ("Fruits & Vegetables", "Fruits", "Vegetables", "Produce"):
        return "Produce"

    # "Fresh Food > ..." — Carrefour's umbrella for chilled/fresh goods.
    # Look at the 2nd level to infer the real category.
    if top == "Fresh Food":
        if len(parts) >= 2:
            sub = parts[1].lower()
            if "dairy" in sub or "egg" in sub:
                return "Dairy"
            if "meat" in sub or "poultry" in sub:
                return "Meat"
            if "fish" in sub or "seafood" in sub:
                return "Seafood"
            if "fruit" in sub or "vegetable" in sub:
                return "Produce"
            if "bakery" in sub or "bread" in sub:
                return "Bakery"
            if "chilled" in sub or "deli" in sub:
                return "Deli"
        return "Fresh"  # generic fallback for unrecognized sub-categories

    # For multi-level paths, just use the top level.
    # "Meat > Beef Veal" → "Meat"
    # "Bakery > Bread Loaves" → "Bakery"
    # "Pantry > Pasta Noodles" → "Pantry"
    return top


_STORE_DISPLAY_NAMES = {
    "Carrefour UAE": "Carrefour",
}


def _clean_store_name(raw_name: str) -> str:
    """Return a clean display name for a store."""
    return _STORE_DISPLAY_NAMES.get(raw_name, raw_name)


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def _strip_brand_and_size(product_name, brand, size):
    """Return product_name with brand prefix + trailing size removed.

    Helps surface the descriptive middle ("Organic Unsalted",
    "Classic", "Red Lentil Fusilli") when the catalog product_name
    redundantly includes brand and size that we already have as
    separate fields.

    Conservative: only strips when the brand is at the start (case-
    insensitive) and only strips a tail that matches a size unit.
    Returns the original string if no clean strip is possible.
    """
    import re as _re
    if not product_name:
        return ""
    s = product_name.strip()
    if brand:
        low = s.lower()
        b = brand.lower().strip()
        if b and low.startswith(b):
            s = s[len(brand):].lstrip(" ,-.")
    # Strip trailing size token if it matches a known unit pattern
    s = _re.sub(
        r"[\s,-]+\d+(?:\.\d+)?\s*(?:g|kg|ml|l|pc|pcs|piece|pieces|litre|liter|x|×)s?\s*$",
        "",
        s,
        flags=_re.IGNORECASE,
    )
    return s.strip(" ,-.")


def _get_price_baselines(conn, item_ids):
    """Return {item_id: (avg_price, purchase_count)} for the given items.

    Uses delivered + completed order_items as the source of truth.
    Skips items with <2 historical purchases -- not enough data to
    say if "today's price" is high or low.
    """
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    try:
        rows = conn.execute(
            f"""SELECT oi.item_id,
                       AVG(oi.unit_price) AS avg_price,
                       COUNT(*) AS purchase_count
                FROM order_items oi
                JOIN orders o ON oi.order_id = o.id
                WHERE oi.item_id IN ({placeholders})
                  AND oi.unit_price > 0
                  AND o.status IN ('delivered', 'completed')
                GROUP BY oi.item_id
                HAVING COUNT(*) >= 2""",
            list(item_ids),
        ).fetchall()
        return {
            r["item_id"]: (float(r["avg_price"]), int(r["purchase_count"]))
            for r in rows
        }
    except Exception:
        return {}


def build_frontend_list(list_id, conn=None):
    """
    Build the structured vendor/category/item tree the React frontend expects.

    Returns:
        {
            "vendors": [{name, categories: [{name, items: [{id, name, vendor, ...}]}]}],
            "conversation": {title, description},
            "highlights": [{type, text}],
        }
    """
    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own_conn = True

    try:
        shopping_list = list_get(list_id, conn=conn)
        if not shopping_list:
            return {"vendors": [], "conversation": _default_conversation(), "highlights": []}

        # Build enriched items grouped by store > category
        store_categories = defaultdict(lambda: defaultdict(list))
        highlights = []

        for item in shopping_list["items"]:
            # Skip removed items
            if item.get("user_action") == "skipped" or item.get("status") == "skipped":
                continue

            # 5-tier product lookup:
            #   Tier 0: curator's explicit catalog_id pick (set by curate_list)
            #   Tier 1: item_alternatives (pre-scored knowledge base)
            #   Tier 2: catalog_id fallback (catalog-only items)
            #   Tier 3: catalog search by name
            #   Tier 4: placeholder
            product = None

            # Tier 0: If the curator set a catalog_id, prefer it — it uses the
            # full scoring pipeline (organic tiers, type gates, history, etc.)
            curator_catalog_id = item.get("catalog_id")
            curator_context = None
            if curator_catalog_id:
                product = _get_product_from_catalog(curator_catalog_id, conn)
                if product:
                    # Enrich with curator's scoring data from failure_context
                    raw_fc = item.get("failure_context")
                    if raw_fc:
                        try:
                            curator_context = json.loads(raw_fc) if isinstance(raw_fc, str) else raw_fc
                        except (json.JSONDecodeError, TypeError):
                            curator_context = None
                    if curator_context and curator_context.get("curator_pick"):
                        product["score"] = curator_context.get("score")
                        product["tier"] = curator_context.get("tier", "acceptable")
                        product["reasons"] = json.dumps(curator_context.get("reasons", []))

            # Tier 1: item_alternatives (pre-scored)
            if not product:
                product = _get_product_info(item["item_id"], item["store_id"], conn)

            # Tier 2: catalog_id fallback
            if not product and curator_catalog_id:
                product = _get_product_from_catalog(curator_catalog_id, conn)

            if not product:
                product = _try_catalog_search_enrichment(item, conn)
            # Tier 4 (placeholder) handled below when building display fields

            # ── OOS auto-swap: if the enriched product is out of stock, try to
            # replace it with an available alternative so the user sees a real
            # product they can actually buy, not a dead listing.
            oos_swapped = False
            if product and not product.get("is_available", True):
                oos_name = product.get("product_name", "?")
                alt = _find_available_alternative(
                    item, product, conn,
                    exclude_catalog_id=product.get("catalog_id"),
                )
                if alt:
                    product = alt
                    oos_swapped = True

            # Parse failure context if present
            failure = None
            raw_fc = item.get("failure_context")
            if raw_fc:
                try:
                    failure = json.loads(raw_fc) if isinstance(raw_fc, str) else raw_fc
                except (json.JSONDecodeError, TypeError):
                    failure = None

            # Determine frontend status
            # Priority: oos_swap > substitution > failure_context > status > scoring tier
            sub_note = item.get("substitution_note")
            if oos_swapped:
                # We already replaced product with an available alternative above.
                # Surface a note so the swap isn't silent ("auto-swap with a note").
                frontend_status = "substituted"
                new_name = product.get("product_name") or "an available option"
                notes = f"{oos_name} was out of stock — switched to {new_name}"
            elif sub_note:
                frontend_status = "substituted"
                notes = sub_note
            elif failure and failure.get("reason") == "substituted":
                # Live verification found OOS, auto-swapped to alternative
                frontend_status = "substituted"
                notes = failure.get("reason_human", "Auto-substituted due to availability")
            elif failure and failure.get("reason") == "out_of_stock":
                # Live verification found OOS, no substitute available
                frontend_status = "unresolved"
                notes = failure.get("reason_human", "Out of stock at all stores")
            elif item.get("status") == "needs_input":
                # Unmatched item — use failure context for clear messaging
                frontend_status = "needs_input"
                notes = (failure.get("reason_human", "Couldn't find this item")
                         if failure else "Couldn't find this item")
            elif item.get("status") == "unresolved":
                # Marked unresolved by verification or other process
                frontend_status = "unresolved"
                notes = (failure.get("reason_human", "Needs your attention")
                         if failure else "Needs your attention")
            else:
                frontend_status, notes = _determine_frontend_status(item, product, conn)

            # Build display fields — NEVER bare text
            if product:
                display_name = product["product_name"]
                identifiers = _build_identifiers(
                    product.get("brand"),
                    product.get("size"),
                    product.get("is_organic"),
                    product.get("origin_country"),
                )
                price = product.get("price") or item.get("estimated_price") or 0
                price_per_unit = _calc_price_per_unit(price, product.get("size", ""))
                image = product.get("image_url")
            else:
                # Tier 4: placeholder — at minimum, give the user a clear name
                display_name = item["item_name"]
                identifiers = ""
                if failure and failure.get("reason"):
                    identifiers = failure.get("reason_human", "")[:60]
                price = item.get("estimated_price") or 0
                price_per_unit = None
                image = None

            # Generate quality tags from grading engine
            # "Less is more" — only show tags that are truly differentiating.
            # Tags like "Clean Label" and "From X" are noise (too common or
            # already visible in identifiers). Keep only high-signal tags.
            # "organic" excluded — already shown in the identifiers line below the title.
            HIGH_SIGNAL_TAGS = {
                "free_range", "grass_fed", "wild_caught",
                "extra_virgin", "whole_grain", "sourdough",
                "processed", "has_additives", "has_fillers", "frozen",
                "bulk_pack",
            }
            tags = []
            if product:
                try:
                    grading_input = {
                        "name": product["product_name"],
                        "price": product.get("price"),
                        "origin_country": product.get("origin_country"),
                        "is_organic": product.get("is_organic"),
                        "description": product.get("description", ""),
                        "ingredients": product.get("ingredients", ""),
                        "storage_info": product.get("storage_info", ""),
                        "size": product.get("size", ""),
                        "brand": product.get("brand", ""),
                    }
                    graded = grade_product(grading_input, item.get("category", ""))
                    raw_tags = get_product_tags(graded)
                    # Only keep high-signal, differentiating tags
                    tags = [
                        {"id": t["id"], "label": t["label"], "sentiment": t["sentiment"]}
                        for t in raw_tags
                        if t["id"] in HIGH_SIGNAL_TAGS and t.get("label")
                    ]
                except Exception:
                    # Grading failed for this item — skip tags, don't crash the list
                    pass

            # Check if product is pinned
            is_pinned = False
            catalog_id = None
            brand = None
            if product:
                catalog_id = product.get("catalog_id")
                brand = product.get("brand")
                cur_prefs = conn.cursor()
                cur_prefs.execute(
                    "SELECT 1 FROM user_item_prefs WHERE item_id = ? AND pref_type = 'pin_product' AND is_active = 1 LIMIT 1",
                    (item["item_id"],))
                is_pinned = cur_prefs.fetchone() is not None

            # Parse scoring reasons for transparency
            scoring_reasons = []
            score_value = None
            score_tier = None
            if product:
                score_value = product.get("score")
                score_tier = product.get("tier")
                raw_reasons = product.get("reasons")
                if raw_reasons:
                    try:
                        scoring_reasons = json.loads(raw_reasons) if isinstance(raw_reasons, str) else raw_reasons
                    except (json.JSONDecodeError, TypeError):
                        pass

            # Use curator's selection reason if available (richer than scoring-only)
            if curator_context and curator_context.get("curator_pick"):
                selection_reason = curator_context.get("summary", "")
                compromises = curator_context.get("compromises", [])
                directive_met = curator_context.get("directive_met", True)
                # Still build scoring_reasons from curator data
                if not scoring_reasons:
                    scoring_reasons = curator_context.get("reasons", [])
            else:
                # Fallback: build from scoring reasons
                selection_reason = _build_selection_reason_from_scores(scoring_reasons, score_tier)
                compromises = _extract_compromises_from_scores(scoring_reasons)
                directive_met = len(compromises) == 0

            # Richer details for the ImageBottomSheet -- user feedback was
            # "I can't tell if it's a good bread or not from this touch
            # point." Expose what the catalog actually has (size, origin,
            # organic, description, ingredients, product URL) so the
            # bottom sheet can give the user enough to actually assess.
            product_size = (product.get("size") if product else "") or ""
            product_origin = (product.get("origin_country") if product else "") or ""
            product_is_organic = bool(product.get("is_organic")) if product else False
            product_description = (product.get("description") if product else "") or ""
            product_ingredients = (product.get("ingredients") if product else "") or ""
            product_url = (product.get("product_url") if product else "") or ""

            # WhatsApp v2 friendly fields:
            #  - genericName: the household-level concept ("Butter",
            #    "Eggs"). Bold leader on the WhatsApp line.
            #  - varietyDescriptor: product_name with brand prefix +
            #    size suffix stripped ("Organic Unsalted",
            #    "Classic", "Free Range Brown"). Subtitle on the
            #    WhatsApp line.
            generic_name = item.get("item_name") or display_name
            variety_descriptor = _strip_brand_and_size(
                display_name, brand or "", product_size
            ) if product else ""

            # Cost-efficiency call-out: per-natural-unit price over the
            # household ceiling (e.g. > 3 AED/egg) -> a "Pricey" badge so an
            # expensive small pack is flagged even when it had to be picked.
            is_pricey = False
            priced_per_unit = ""
            if product:
                try:
                    from list_quality_validator import per_unit_price
                    _pp = per_unit_price(
                        generic_name, item.get("category"),
                        {"product_name": display_name, "size": product_size,
                         "price": price},
                    )
                    if _pp and _pp[3]:
                        is_pricey = True
                        priced_per_unit = f"{_pp[0]:g} {_pp[2]}"
                except Exception:
                    pass

            frontend_item = {
                "id": item["list_item_id"],
                "itemId": item["item_id"],
                "storeId": item["store_id"],
                "catalogId": catalog_id,
                "name": display_name,
                "brand": brand or "",
                "vendor": item.get("store_name", ""),
                "identifiers": identifiers or "",
                "price": round(price, 2),
                "pricePerUnit": price_per_unit or "",
                "quantity": max(1, round(item.get("quantity", 1))),
                "image": image,
                "tags": tags,
                "status": frontend_status,
                "notes": notes,
                "isPinned": is_pinned,
                "score": score_value,
                "scoreTier": score_tier,
                "scoringReasons": scoring_reasons,
                "selectionReason": selection_reason,
                "directiveMet": directive_met,
                "compromises": compromises,
                "addedBy": item.get("added_by", "system"),
                # Richer product detail (for ImageBottomSheet)
                "size": product_size,
                "originCountry": product_origin,
                "isOrganic": product_is_organic,
                "isPricey": is_pricey,
                "pricedPerUnit": priced_per_unit,
                "description": product_description,
                "ingredients": product_ingredients,
                "productUrl": product_url,
                # WhatsApp v2 fields (also useful in-app later):
                #   genericName  -> "Butter"
                #   variety      -> "Organic Unsalted"
                #   priceBaseline / priceRelativePct populated below
                "genericName": generic_name,
                "variety": variety_descriptor,
                # Per-item LLM verdict on whether origin helps the shopper.
                # NULL means "not yet evaluated"; the formatter falls back
                # to the static rule tables in that case. Set by
                # item_origin_evaluator.evaluate_items().
                "originSignalLlm": item.get("origin_signal_llm"),
                "priceBaseline": None,         # backfilled in pass 2
                "priceRelativePct": None,      # backfilled in pass 2
                "purchaseCount": 0,            # how many past purchases inform the baseline
                # Failure context for unmatched/problem items
                "failureReason": failure.get("reason") if failure else None,
                "failureHuman": failure.get("reason_human") if failure else None,
                "didYouMean": failure.get("did_you_mean", []) if failure else [],
            }

            category = _simplify_category(item.get("category", "Other"))
            store_name = _clean_store_name(item.get("store_name") or "Needs Attention")
            store_categories[store_name][category].append(frontend_item)

            # Build highlights
            if frontend_status == "price_change":
                highlights.append({"type": "warning", "text": notes})
            elif frontend_status in ("unresolved", "needs_input"):
                highlights.append({"type": "error", "text": f"{item['item_name']}: {notes}"})
            elif frontend_status == "substituted":
                highlights.append({"type": "info", "text": f"{item['item_name']}: {notes}"})

        # Pass 2: backfill historical price baselines + relative %
        # for each item in one batch query. Computes:
        #   priceBaseline   = AVG(unit_price) over delivered/completed orders
        #   priceRelativePct = (current_price - baseline) / baseline * 100
        # Only items with >=2 purchases get a baseline; the others
        # show plain price (no relative indicator on the WhatsApp line).
        all_item_ids = set()
        all_items_flat = []
        for store_name in store_categories:
            for cat_name, items in store_categories[store_name].items():
                for fi in items:
                    all_items_flat.append(fi)
                    if fi.get("itemId"):
                        all_item_ids.add(fi["itemId"])

        baselines = _get_price_baselines(conn, all_item_ids)
        for fi in all_items_flat:
            iid = fi.get("itemId")
            if not iid:
                continue
            if iid in baselines:
                baseline_price, purchase_count = baselines[iid]
                fi["priceBaseline"] = round(baseline_price, 2)
                fi["purchaseCount"] = purchase_count
                try:
                    current = float(fi.get("price") or 0)
                    if current > 0 and baseline_price > 0:
                        rel = (current - baseline_price) / baseline_price * 100.0
                        # Only surface significant deltas; ignore noise.
                        if abs(rel) >= 2.0:
                            fi["priceRelativePct"] = round(rel, 1)
                except (TypeError, ValueError):
                    pass

            # Accepted-price-band verdict: judge this pick vs what WE'VE
            # historically accepted for this item's type. Surface a call-out only
            # for the notable cases (above our usual / far beyond it) so the
            # family sees "priced above your usual range" rather than silently
            # paying outrageous. Best-effort; never breaks the list render.
            try:
                from price_intel import evaluate_option
                ev = evaluate_option(iid, fi.get("name") or "", fi.get("price"),
                                     fi.get("size"), conn=conn)
                verdict = ev.get("price_verdict")
                if verdict in ("premium", "outrageous"):
                    fi["priceVerdict"] = verdict
                    fi["priceCallout"] = ev.get("reason")
                    fi["acceptedMedian"] = ev.get("median")
                    fi["acceptedUnit"] = ev.get("base_unit")
            except Exception:
                pass

        # Build vendors array
        vendors = []
        for store_name in store_categories:
            categories = []
            for cat_name, items in store_categories[store_name].items():
                categories.append({"name": cat_name, "items": items})
            # Sort categories alphabetically
            categories.sort(key=lambda c: c["name"])
            vendors.append({"name": store_name, "categories": categories})

        # Sort vendors: Carrefour first, then Kibsons
        store_order = {"Carrefour": 0, "Kibsons": 1}
        vendors.sort(key=lambda v: store_order.get(v["name"], 99))

        # Build conversation header — use actual rendered item count (excludes skipped)
        rendered_count = sum(len(c["items"]) for v in vendors for c in v["categories"])
        conversation = _build_conversation(shopping_list, highlights, rendered_count=rendered_count)

        # Get suggestion diff (original vs user-modified)
        diff = None
        try:
            from list_generate import list_get_diff
            diff = list_get_diff(list_id, conn=conn)
        except Exception:
            pass  # Snapshots may not exist for older lists

        # "Freshness" timestamp for the header caption. We want
        # MAX(list_row.created_at, latest list_items.created_at) -- the
        # bare list-row created_at is wrong for long-lived containers
        # like Cart, which was created 87 days ago but has items added
        # today. Using the most recent activity matches what the user
        # reads when they see "Cart updated 2 hours ago".
        try:
            last_item_at_row = conn.execute(
                "SELECT MAX(created_at) AS last_at FROM list_items WHERE list_id = ?",
                (list_id,),
            ).fetchone()
            last_item_at = last_item_at_row["last_at"] if last_item_at_row else None
        except Exception:
            last_item_at = None
        list_created_at = shopping_list.get("created_at")
        # ISO strings (and SQLite's "YYYY-MM-DD HH:MM:SS") sort
        # lexicographically, so a plain max() over the two does the
        # right thing.
        candidates = [t for t in (list_created_at, last_item_at) if t]
        updated_at = max(candidates) if candidates else None

        result = {
            "vendors": vendors,
            "conversation": conversation,
            "highlights": highlights if highlights else _default_highlights(shopping_list),
            # Most recent activity on this list -- when the agent
            # generated it OR when the last item was added/edited.
            # Frontend uses this for the "Suggested/Cart updated N
            # days ago" caption.
            "createdAt": updated_at or list_created_at,
            # Original row created_at, exposed separately in case the
            # UI ever wants to show "Created 87 days ago, last updated
            # today" -- not used today.
            "listCreatedAt": list_created_at,
            "targetDate": shopping_list.get("target_date"),
        }
        if diff:
            result["diff"] = diff

        return result

    finally:
        if own_conn:
            conn.close()


# ============================================================
# PRODUCT INFO LOOKUP
# ============================================================

def _get_product_info(item_id, store_id, conn):
    """Get the best-ranked product details for an item from the knowledge base.

    Prefers available products (is_available=1). Falls back to any product
    only if nothing available exists, so downstream status logic can still
    flag it as OOS.
    """
    cur = conn.cursor()

    _PRODUCT_INFO_SQL = """
        SELECT pc.id as catalog_id,
               pc.product_name, pc.brand, pc.price, pc.size,
               pc.image_url, pc.product_url, pc.is_organic,
               pc.origin_country, pc.is_available,
               pc.description, pc.ingredients, pc.nutrition_info,
               ia.tier, ia.score, ia.reasons, ia.is_preferred
        FROM item_alternatives ia
        JOIN product_catalog pc ON ia.catalog_id = pc.id
        WHERE ia.item_id = ? AND ia.store_id = ?
          {availability_filter}
        ORDER BY ia.score DESC
        LIMIT 1
    """

    # First try: only available products
    cur.execute(
        _PRODUCT_INFO_SQL.format(availability_filter="AND pc.is_available = 1"),
        (item_id, store_id),
    )
    row = cur.fetchone()

    # Fallback: any product (so OOS items still get an enriched display)
    if not row:
        cur.execute(
            _PRODUCT_INFO_SQL.format(availability_filter=""),
            (item_id, store_id),
        )
        row = cur.fetchone()

    if not row:
        return None

    return {
        "catalog_id": row["catalog_id"],
        "product_name": row["product_name"],
        "brand": row["brand"],
        "price": row["price"],
        "size": row["size"],
        "image_url": row["image_url"],
        "product_url": row["product_url"],
        "is_organic": bool(row["is_organic"]),
        "origin_country": row["origin_country"],
        "is_available": bool(row["is_available"]) if row["is_available"] is not None else True,
        "description": row["description"] or "",
        "ingredients": row["ingredients"] or "",
        "nutrition_info": row["nutrition_info"] or "",
        "tier": row["tier"],
        "score": row["score"],
        "reasons": row["reasons"],
        "is_preferred": bool(row["is_preferred"]),
    }


def _get_product_from_catalog(catalog_id, conn):
    """
    Direct catalog lookup — used for catalog-only items (not in items table)
    where we have a catalog_id stored in list_items.
    Returns same shape as _get_product_info() for consistency.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT id as catalog_id,
               product_name, brand, price, size,
               image_url, product_url, is_organic,
               origin_country, is_available,
               description, ingredients, nutrition_info,
               category
        FROM product_catalog
        WHERE id = ?
    """, (catalog_id,))

    row = cur.fetchone()
    if not row:
        return None

    return {
        "catalog_id": row["catalog_id"],
        "product_name": row["product_name"],
        "brand": row["brand"],
        "price": row["price"],
        "size": row["size"],
        "image_url": row["image_url"],
        "product_url": row["product_url"],
        "is_organic": bool(row["is_organic"]),
        "origin_country": row["origin_country"],
        "is_available": bool(row["is_available"]) if row["is_available"] is not None else True,
        "description": row["description"] or "",
        "ingredients": row["ingredients"] or "",
        "nutrition_info": row["nutrition_info"] or "",
        "tier": "acceptable",  # catalog-only items don't have preference scoring
        "score": None,
        "reasons": None,
        "is_preferred": False,
    }


def _try_catalog_search_enrichment(item, conn):
    """
    Tier 3 fallback: search the catalog by item name to at least get
    an image and brand, even if we don't have an exact match.
    Read-only DB query, fast.
    """
    try:
        from product_knowledge import search_catalog
        results = search_catalog(
            item.get("item_name", ""),
            store_id=item.get("store_id"),
            limit=1,
            conn=conn,
        )
        if results:
            r = results[0]
            return {
                "catalog_id": r.get("catalog_id"),
                "product_name": r.get("product_name", item.get("item_name", "")),
                "brand": r.get("brand", ""),
                "price": r.get("price") or item.get("estimated_price") or 0,
                "size": r.get("size", ""),
                "image_url": r.get("image_url"),
                "product_url": r.get("product_url", ""),
                "is_organic": bool(r.get("is_organic")),
                "origin_country": r.get("origin_country", ""),
                "is_available": True,
                "description": "",
                "ingredients": "",
                "nutrition_info": "",
                "tier": "fallback",
                "score": None,
                "reasons": None,
                "is_preferred": False,
                "is_search_fallback": True,  # flag: not an exact preference-scored match
            }
    except Exception:
        pass
    return None


# ============================================================
# STATUS MAPPING
# ============================================================

def _find_available_alternative(item, product, conn, exclude_catalog_id=None):
    """Find the next best available alternative from item_alternatives.

    Returns dict with catalog_id/name/price/image_url or None.
    Optionally excludes a specific catalog_id (the OOS product).
    """
    item_id = item.get("item_id")
    # product from _get_product_from_catalog() doesn't include store_id,
    # so always fall back to item's store_id when product's is missing.
    store_id = (product.get("store_id") if product else None) or item.get("store_id")

    # Catalog-only entries (e.g. added via chat) have no item_id.
    # Try two approaches to resolve it so we can still find alternatives.
    if not item_id and exclude_catalog_id:
        try:
            # Approach 1: check if this catalog_id is in item_alternatives
            ia_row = conn.execute(
                "SELECT item_id FROM item_alternatives WHERE catalog_id = ? LIMIT 1",
                (exclude_catalog_id,),
            ).fetchone()
            if ia_row:
                item_id = ia_row["item_id"]
        except Exception:
            pass

    if not item_id and product:
        try:
            # Approach 2: match product name to items table
            # e.g. "Kibsons Bananas - 4 to 6 pieces" contains "Banana"
            prod_name = product.get("product_name") or item.get("item_name") or ""
            if prod_name:
                item_row = conn.execute(
                    "SELECT id FROM items WHERE ? LIKE '%' || name || '%' LIMIT 1",
                    (prod_name,),
                ).fetchone()
                if item_row:
                    item_id = item_row["id"]
        except Exception:
            pass

    if not item_id or not store_id:
        return None
    try:
        cur = conn.cursor()
        if exclude_catalog_id:
            cur.execute("""
                SELECT ia.catalog_id, ia.score,
                       pc.product_name, pc.brand, pc.price, pc.size,
                       pc.image_url, pc.is_organic, pc.origin_country,
                       pc.is_available, pc.product_url,
                       pc.description, pc.ingredients, pc.nutrition_info,
                       ia.tier, ia.reasons, ia.is_preferred
                FROM item_alternatives ia
                JOIN product_catalog pc ON ia.catalog_id = pc.id
                WHERE ia.item_id = ? AND ia.store_id = ?
                  AND pc.is_available = 1 AND pc.price > 0
                  AND ia.catalog_id != ?
                ORDER BY ia.score DESC
                LIMIT 1
            """, (item_id, store_id, exclude_catalog_id))
        else:
            cur.execute("""
                SELECT ia.catalog_id, ia.score,
                       pc.product_name, pc.brand, pc.price, pc.size,
                       pc.image_url, pc.is_organic, pc.origin_country,
                       pc.is_available, pc.product_url,
                       pc.description, pc.ingredients, pc.nutrition_info,
                       ia.tier, ia.reasons, ia.is_preferred
                FROM item_alternatives ia
                JOIN product_catalog pc ON ia.catalog_id = pc.id
                WHERE ia.item_id = ? AND ia.store_id = ?
                  AND pc.is_available = 1 AND pc.price > 0
                ORDER BY ia.score DESC
                LIMIT 1
            """, (item_id, store_id))
        row = cur.fetchone()
        if row:
            return {
                "catalog_id": row["catalog_id"],
                "name": row["product_name"],
                "product_name": row["product_name"],
                "brand": row["brand"],
                "price": row["price"],
                "size": row["size"],
                "image_url": row["image_url"],
                "is_organic": bool(row["is_organic"]),
                "origin_country": row["origin_country"],
                "is_available": True,
                "product_url": row["product_url"] or "",
                "description": row["description"] or "",
                "ingredients": row["ingredients"] or "",
                "nutrition_info": row["nutrition_info"] or "",
                "tier": row["tier"],
                "score": row["score"],
                "reasons": row["reasons"],
                "is_preferred": bool(row["is_preferred"]),
            }
    except Exception:
        pass
    return None


def find_alternatives_for_item(item_id, store_id, current_catalog_id,
                                scope="same_vendor", conn=None, limit=3):
    """
    Find up to `limit` alternative products for a given list item.

    scope:
      "same_vendor"   -> alternatives at the same store (different SKU)
      "other_vendors" -> alternatives at OTHER stores (for cart
                         consolidation: "find this in Kibsons instead")

    Returns a list of dicts with the fields the React
    AlternativesBottomSheet expects. Empty list if we can't resolve
    the item or there are no candidates left after filtering.
    """
    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own_conn = True

    try:
        # Catalog-only entries (no item_id) -- resolve item_id via the
        # current catalog_id so we have something to query against.
        if not item_id and current_catalog_id:
            ia_row = conn.execute(
                "SELECT item_id FROM item_alternatives WHERE catalog_id = ? LIMIT 1",
                (current_catalog_id,),
            ).fetchone()
            if ia_row:
                item_id = ia_row["item_id"]

        if not item_id:
            return []

        # Carry the original product's organic preference across BOTH tabs.
        # If the product being swapped is organic, the household wants organic
        # alternatives too — previously the other-store tab ignored this and
        # returned non-organic picks. Determined from the product being replaced.
        prefer_organic = False
        if current_catalog_id:
            _org = conn.execute(
                "SELECT is_organic FROM product_catalog WHERE id = ?",
                (current_catalog_id,),
            ).fetchone()
            prefer_organic = bool(_org and _org["is_organic"])

        same = (scope == "same_vendor")
        store_clause = "ia.store_id = ?" if same else "(? IS NULL OR ia.store_id != ?)"

        def _query(organic_only):
            where = [
                "ia.item_id = ?",
                store_clause,
                "pc.is_available = 1",
                "pc.price > 0",
                "(? IS NULL OR ia.catalog_id != ?)",
            ]
            if organic_only:
                where.append("pc.is_organic = 1")
            sql = f"""
                SELECT ia.catalog_id, ia.score, ia.tier,
                       pc.product_name, pc.brand, pc.price, pc.size,
                       pc.image_url, pc.product_url, pc.is_organic,
                       pc.origin_country, pc.description,
                       s.name as store_name, s.id as store_id
                FROM item_alternatives ia
                JOIN product_catalog pc ON ia.catalog_id = pc.id
                JOIN stores s ON ia.store_id = s.id
                WHERE {" AND ".join(where)}
                ORDER BY pc.is_organic DESC, ia.score DESC
                LIMIT ?
            """
            if same:
                p = (item_id, store_id, current_catalog_id, current_catalog_id, limit)
            else:
                p = (item_id, store_id, store_id, current_catalog_id, current_catalog_id, limit)
            return conn.execute(sql, p).fetchall()

        rows = _query(organic_only=prefer_organic)
        if prefer_organic and not rows:
            # No organic option in this scope — fall back to the best available
            # so the tab isn't empty; isOrganic=false makes clear it's not organic.
            rows = _query(organic_only=False)
        results = []
        for row in rows:
            results.append({
                "catalogId": row["catalog_id"],
                "storeId": row["store_id"],
                "name": row["product_name"],
                "brand": row["brand"] or "",
                "size": row["size"] or "",
                "price": round(row["price"], 2) if row["price"] else 0,
                "image": row["image_url"] or "",
                "productUrl": row["product_url"] or "",
                "vendor": row["store_name"],
                "score": row["score"],
                "scoreTier": row["tier"],
                "isOrganic": bool(row["is_organic"]),
                "originCountry": row["origin_country"] or "",
                "description": row["description"] or "",
            })
        return results
    except Exception as e:
        # Caller's WS reply is empty list -- UI will show "no alternatives".
        try:
            from app import logger
            logger.warning("find_alternatives_for_item failed: %s", e)
        except Exception:
            pass
        return []
    finally:
        if own_conn:
            conn.close()


def _determine_frontend_status(item, product, conn):
    """
    Map backend data to frontend status string.

    Returns: (status, notes)
    """
    if not product:
        return ("unresolved", "Couldn't match to catalog — tap to add manually")

    tier = product.get("tier", "unknown")

    if not product.get("is_available", True):
        # Try to auto-suggest an available alternative
        alt = _find_available_alternative(item, product, conn)
        if alt:
            alt_name = alt.get("name", "alternative")
            alt_price = alt.get("price", 0)
            return ("substituted", f"Out of stock \u2192 {alt_name} ({alt_price:.2f} AED)")
        return ("unresolved", "Out of stock \u2014 no alternatives available")

    # Check for price change vs last order
    price_note = _check_price_change(item, product, conn)
    if price_note:
        # Price increases need attention; decreases are just good news
        if price_note.startswith("\u2191"):  # ↑
            return ("price_change", price_note)
        else:
            return ("matched", price_note)  # ↓ decrease — matched with info

    if tier == "preferred":
        return ("matched", "")
    elif tier == "acceptable":
        # Curator already picked the best match — this is not a substitution.
        # "Substituted" should only be used for OOS swaps (handled earlier).
        return ("matched", "")
    elif tier == "avoid":
        return ("unresolved", "Only low-quality matches found")
    else:
        return ("matched", "")


def _check_price_change(item, product, conn):
    """Check if the price changed significantly from last order.

    Compares current catalog price against the last ordered price for the
    SAME item at the SAME store — not the avg_price across different sizes/products.
    """
    if not product or not product.get("price"):
        return None

    item_id = item.get("item_id")
    store_id = product.get("store_id") or item.get("store_id")
    if not item_id:
        return None

    cur = conn.cursor()
    # Try to find the last order price for this specific item + store
    cur.execute("""
        SELECT oi.unit_price
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE oi.item_id = ? AND o.store_id = ? AND oi.unit_price > 0
        ORDER BY o.order_date DESC LIMIT 1
    """, (item_id, store_id))
    row = cur.fetchone()

    if not row:
        # No order history for this item+store — skip price comparison
        return None

    last_price = row["unit_price"]
    current_price = product["price"]
    diff = current_price - last_price

    # Flag if price changed by more than 10% or 3 AED (relaxed thresholds)
    if abs(diff) > max(last_price * 0.10, 3.0):
        arrow = "\u2191" if diff > 0 else "\u2193"
        return f"{arrow} {abs(diff):.2f} AED vs last order"

    return None


def _build_selection_reason_from_scores(scoring_reasons: list, tier: str = None) -> str:
    """
    Translate internal scoring reasons into a plain-English explanation
    of why this product was selected.

    e.g., ["+organic_match (always_organic tier) (+15)", "Preferred brand 'Jenan' (+20)"]
      -> "Organic (your preference) + Preferred brand"

    Handles both old-style code-based reasons and new verbose reasons.
    """
    # Map internal scoring codes / prefixes to human-readable phrases
    REASON_MAP = {
        "+organic_match": "Organic (your preference)",
        "+organic_preference": "Organic (your preference)",
        "+brand_pref": "Preferred brand",
        "+brand_preference": "Preferred brand",
        "+purchase_history": "Bought before",
        "+exact_name": "Exact match",
        "+name_match": "Name match",
        "+high_popularity": "Popular choice",
        "+good_rating": "Well rated",
        "+price_value": "Good value",
        "+fresh": "Fresh product",
        "+local_origin": "Local product",
        "+free_range": "Free range",
        "+grass_fed": "Grass fed",
        "+origin_premium": "Premium origin",
        "+origin_good": "Good origin",
    }

    # Pattern-based matching for verbose scoring reasons
    PATTERN_MAP = [
        ("organic_match", "Organic (your preference)"),
        ("origin_premium", "Premium origin"),
        ("origin_good", "Good origin"),
        ("Preferred brand", "Preferred brand"),
        ("Preferred '", None),  # extract the keyword dynamically
        ("grass fed", "Grass fed"),
        ("free range", "Free range"),
        ("Frequently ordered brand", "Bought before"),
        ("Previously ordered brand", "Bought before"),
        ("fillet", None),  # skip — too specific
    ]

    human_parts = []
    for reason in scoring_reasons:
        if not isinstance(reason, str):
            continue

        # Try exact code match first
        matched = False
        for code, phrase in REASON_MAP.items():
            if reason.startswith(code):
                if phrase not in human_parts:
                    human_parts.append(phrase)
                matched = True
                break

        if matched:
            continue

        # Try pattern matching for verbose reasons
        for pattern, phrase in PATTERN_MAP:
            if pattern in reason:
                if phrase and phrase not in human_parts:
                    human_parts.append(phrase)
                break

    if not human_parts:
        # Fallback based on tier
        if tier == "preferred":
            return "Best match"
        elif tier == "acceptable":
            return "Good alternative"
        return ""

    return " + ".join(human_parts[:3])  # Max 3 reasons to keep it concise


def _extract_compromises_from_scores(scoring_reasons: list) -> list:
    """
    Extract compromise messages from scoring reasons.
    These are reasons where a directive wasn't met.

    e.g., "-organic_required (always_organic tier, not organic) (-40)"
      -> ["Not organic -- should be organic (Dirty Dozen/household rule)"]
    """
    compromises = []
    COMPROMISE_PATTERNS = [
        ("-organic_required", "Not organic (should be organic)"),
        ("TYPE GATE FAIL", None),  # extract full message
        ("-oversized", None),      # extract full message
    ]

    for reason in scoring_reasons:
        if not isinstance(reason, str):
            continue
        for pattern, default_msg in COMPROMISE_PATTERNS:
            if pattern in reason:
                if default_msg:
                    if default_msg not in compromises:
                        compromises.append(default_msg)
                else:
                    # Extract the descriptive part
                    msg = reason.split("(-")[0].strip()  # Remove the point deduction
                    if msg and msg not in compromises:
                        compromises.append(msg)
                break

    return compromises


# ============================================================
# DISPLAY HELPERS
# ============================================================

def _build_identifiers(brand, size, is_organic, origin_country):
    """Build the identifiers string: 'Organic | Jenan | 15 PCS'"""
    parts = []
    if is_organic:
        parts.append("Organic")
    if brand:
        parts.append(brand)
    if size:
        parts.append(size)
    if origin_country and origin_country not in ("", "N/A"):
        parts.append(origin_country)
    return " | ".join(parts)


def _calc_price_per_unit(price, size_str):
    """
    Parse size string and return formatted price per unit.
    Examples: "2.75/ea", "40.00/kg", "70.29/L"
    """
    if not size_str or not price:
        return None

    size_str = str(size_str).strip()

    # Try to parse: "15 PCS", "200g", "1L", "500ml", "1 Kg", "6 x 200ml"
    patterns = [
        # "6 x 200ml" or "200ml x 6"
        (r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(ml|l|g|kg)", "multi"),
        (r"(\d+(?:\.\d+)?)\s*(ml|l|g|kg)\s*x\s*(\d+)", "multi_rev"),
        # "15 PCS" or "15 Pieces" or "15 pcs"
        (r"(\d+(?:\.\d+)?)\s*(?:PCS|pcs|Pieces?|pieces?|Pack|pack|Packs|packs|Eggs?|eggs?)", "count"),
        # "200g" or "200 g"
        (r"(\d+(?:\.\d+)?)\s*(?:g|gm|grams?)\b", "grams"),
        # "1.5kg" or "1.5 Kg"
        (r"(\d+(?:\.\d+)?)\s*(?:kg|Kg|KG|kilograms?)\b", "kg"),
        # "500ml" or "500 ml"
        (r"(\d+(?:\.\d+)?)\s*(?:ml|ML|mL|milliliters?)\b", "ml"),
        # "1L" or "1 L" or "1.5L"
        (r"(\d+(?:\.\d+)?)\s*(?:l|L|litres?|liters?)\b", "liters"),
    ]

    for pattern, unit_type in patterns:
        m = re.search(pattern, size_str, re.IGNORECASE)
        if not m:
            continue

        if unit_type == "multi":
            count = float(m.group(1))
            amount = float(m.group(2))
            unit = m.group(3).lower()
            total = count * amount
            if unit in ("ml",):
                total_l = total / 1000
                return f"{price / total_l:.2f}/L"
            elif unit in ("l",):
                return f"{price / (count * amount):.2f}/L"
            elif unit in ("g",):
                total_kg = total / 1000
                return f"{price / total_kg:.2f}/kg"
            elif unit in ("kg",):
                return f"{price / (count * amount):.2f}/kg"

        elif unit_type == "multi_rev":
            amount = float(m.group(1))
            unit = m.group(2).lower()
            count = float(m.group(3))
            total = count * amount
            if unit in ("ml",):
                return f"{price / (total / 1000):.2f}/L"
            elif unit in ("l",):
                return f"{price / total:.2f}/L"
            elif unit in ("g",):
                return f"{price / (total / 1000):.2f}/kg"
            elif unit in ("kg",):
                return f"{price / total:.2f}/kg"

        elif unit_type == "count":
            count = float(m.group(1))
            if count > 0:
                return f"{price / count:.2f}/ea"

        elif unit_type == "grams":
            grams = float(m.group(1))
            if grams > 0:
                return f"{price / (grams / 1000):.2f}/kg"

        elif unit_type == "kg":
            kg = float(m.group(1))
            if kg > 0:
                return f"{price / kg:.2f}/kg"

        elif unit_type == "ml":
            ml = float(m.group(1))
            if ml > 0:
                return f"{price / (ml / 1000):.2f}/L"

        elif unit_type == "liters":
            liters = float(m.group(1))
            if liters > 0:
                return f"{price / liters:.2f}/L"

    return None


# ============================================================
# CONVERSATION & HIGHLIGHTS
# ============================================================

def _build_conversation(shopping_list, highlights, rendered_count=None):
    """Build conversation header metadata."""
    target = shopping_list.get("target_date", "")
    day_name = shopping_list.get("day_name", "")
    summary = shopping_list.get("summary", {})

    # Format date: "Feb 25"
    try:
        dt = datetime.strptime(target, "%Y-%m-%d")
        date_str = dt.strftime("%b %d")
    except (ValueError, TypeError):
        date_str = target

    title = "Your Grocery List"

    # Use rendered_count (excludes skipped items) if available
    total_items = rendered_count if rendered_count is not None else summary.get("total_items", 0)
    store_count = len(summary.get("stores", {}))
    issues = len(highlights)

    items_word = "item" if total_items == 1 else "items"
    stores_word = "store" if store_count == 1 else "stores"
    if issues > 0:
        desc = f"{total_items} {items_word} across {store_count} {stores_word}. {issues} item{'s' if issues != 1 else ''} need{'s' if issues == 1 else ''} your attention."
    else:
        desc = f"{total_items} {items_word} across {store_count} {stores_word}. Everything looks good."

    return {"title": title, "description": desc, "state": "has_list"}


def _default_conversation():
    """Fallback conversation when no list exists."""
    return {
        "title": "All Caught Up!",
        "description": "Everything's stocked. I'll let you know when it's time to reorder.",
        "state": "all_caught_up",
    }


def _default_highlights(shopping_list):
    """Generate default highlights when there are no issues."""
    # No highlights when everything is fine — the list speaks for itself.
    # Vendor totals are already shown in the VendorSection headers.
    return []


# ============================================================
# TESTING
# ============================================================

if __name__ == "__main__":
    """Quick test: build frontend data from the latest list."""
    import json
    from db_setup import create_database

    create_database()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Find the latest list
    cur = conn.cursor()
    cur.execute("SELECT id FROM shopping_lists ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()

    if row:
        result = build_frontend_list(row["id"], conn=conn)
        print(json.dumps(result, indent=2, default=str))
        print(f"\n  Vendors: {len(result['vendors'])}")
        for v in result["vendors"]:
            item_count = sum(len(c["items"]) for c in v["categories"])
            print(f"    {v['name']}: {len(v['categories'])} categories, {item_count} items")
    else:
        print("  No shopping lists found. Run the agent first.")

    conn.close()
