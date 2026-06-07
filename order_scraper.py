"""
AutoRestock - Order History Scraper

Scrapes actual placed orders from Carrefour and Kibsons order history pages.
This is the "what actually happened" data that feeds into gap analysis.

Two stores, two different order history pages:
  - Carrefour UAE: /mafuae/en/myAccount/myOrders
  - Kibsons: /en/account/orders

The scraper:
  1. Opens the order history page
  2. Finds orders newer than the last one we have in the DB
  3. Clicks into each new order to get item details
  4. Saves to orders + order_items tables
  5. Returns list of new order IDs for gap analysis

IMPORTANT: Close Edge before running. Playwright needs exclusive profile access.

Usage:
  python3 order_scraper.py                    # scrape both stores
  python3 order_scraper.py --store carrefour  # just Carrefour
  python3 order_scraper.py --store kibsons    # just Kibsons
  python3 order_scraper.py --dry-run          # show what it finds without saving
"""

import asyncio
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import app_state
from browser_common import launch_edge, safe_close, ensure_logged_in
from db_setup import DB_PATH, create_database
from app_logging import log_event


# ============================================================
# STORE CONFIGS
# ============================================================

CARREFOUR_ORDERS_URL = "https://www.carrefouruae.com/mafuae/en/app/orders"
KIBSONS_ORDERS_URL = "https://www.kibsons.com/en/account/orderhistory"


# ============================================================
# HELPERS
# ============================================================

def _get_latest_order_ref(store_name, conn):
    """Get the most recent order_ref we have for a store."""
    cur = conn.cursor()
    cur.execute("""
        SELECT o.order_ref, o.order_date
        FROM orders o
        JOIN stores s ON o.store_id = s.id
        WHERE s.name = ?
        ORDER BY o.order_date DESC, o.id DESC
        LIMIT 1
    """, (store_name,))
    row = cur.fetchone()
    if row:
        return {"order_ref": row["order_ref"], "order_date": row["order_date"]}
    return None


def _get_store_id(store_name, conn):
    """Get or create store ID."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM stores WHERE name = ?", (store_name,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute("INSERT INTO stores (name) VALUES (?)", (store_name,))
    conn.commit()
    return cur.lastrowid


def _order_exists(order_ref, store_id, conn):
    """Check if we already have this order."""
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM orders WHERE order_ref = ? AND store_id = ?",
        (order_ref, store_id)
    )
    return cur.fetchone() is not None


def _parse_price(text):
    """Extract a float price from text like '34.49 AED' or 'AED 34.49'."""
    if not text:
        return 0
    match = re.search(r'([\d,]+\.?\d*)', str(text).replace(',', ''))
    return float(match.group(1)) if match else 0


def _parse_date(text):
    """Try to parse various date formats into YYYY-MM-DD."""
    if not text:
        return ""
    text = text.strip()
    # Strip time parts: "24-Feb-2026 | 11:23pm" → "24-Feb-2026"
    # Also handles "25-Feb-2026 | 9 AM - 1 PM" → "25-Feb-2026"
    if '|' in text:
        text = text.split('|')[0].strip()
    # Already in ISO format
    if re.match(r'\d{4}-\d{2}-\d{2}', text):
        return text[:10]
    # Common formats
    for fmt in [
        "%d-%b-%Y",      # 24-Feb-2026 (Kibsons format)
        "%d %b %Y",      # 15 Jan 2026
        "%d %B %Y",      # 15 January 2026
        "%b %d, %Y",     # Jan 15, 2026
        "%B %d, %Y",     # January 15, 2026
        "%d/%m/%Y",      # 15/01/2026
        "%m/%d/%Y",      # 01/15/2026
        "%d-%m-%Y",      # 15-01-2026
    ]:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


# ============================================================
# CARREFOUR ORDER SCRAPER
# ============================================================

# JavaScript executed inside Chrome to extract Carrefour order cards
# from /mafuae/en/app/orders. Shared by both the (now-disabled)
# Playwright path and the live UC Chrome path so the parsing logic
# stays in one place.
_CARREFOUR_EXTRACT_JS_IIFE = """
(function() {
    const orders = [];
    const allLinks = document.querySelectorAll('a[href*="/app/orders/"]');
    for (const link of allLinks) {
        const href = link.getAttribute('href') || '';
        const match = href.match(/\\/app\\/orders\\/(\\d{10,20})\\//);
        if (!match) continue;
        const orderRef = match[1];
        const texts = [];
        const walker = document.createTreeWalker(link, NodeFilter.SHOW_ELEMENT);
        let node;
        while (node = walker.nextNode()) {
            if (node.childNodes.length === 1 && node.childNodes[0].nodeType === 3) {
                const t = node.textContent.trim();
                if (t) texts.push(t);
            }
        }
        let status = '', dateText = '', totalText = '', itemsText = '';
        let deliveryType = '';   // 'now' | 'scheduled' | ''
        for (const t of texts) {
            if (/your order is/i.test(t)) {
                if (/delivered/i.test(t)) status = 'delivered';
                else if (/on the way|on its way/i.test(t)) status = 'dispatched';
                else if (/being prepared|preparing/i.test(t)) status = 'processing';
                else if (/confirmed/i.test(t)) status = 'confirmed';
                else if (/cancel/i.test(t)) status = 'cancelled';
                else status = t;
            }
            if (/\\w{3},\\s+\\w{3}\\s+\\d{1,2}/.test(t) && /\\d{4}/.test(t)) {
                dateText = t;
            }
            if (/^AED\\s+[\\d,.]+$/.test(t)) {
                totalText = t.replace(/^AED\\s+/, '');
            }
            if (/^\\d+\\s+items?$/i.test(t)) {
                itemsText = t;
            }
            // Carrefour labels each order card with its delivery channel.
            // "NOW Delivery" = Carrefour NOW express (~30-90 min).
            // "Scheduled Delivery" = regular slotted delivery (next-day-ish).
            // Some cards omit it entirely on older orders.
            if (/^\\s*now\\s+delivery\\s*$/i.test(t)) {
                deliveryType = 'now';
            } else if (/^\\s*scheduled\\s+delivery\\s*$/i.test(t)) {
                deliveryType = 'scheduled';
            }
        }
        let headerDate = '';
        let prev = link.previousElementSibling;
        if (prev) {
            const pt = (prev.textContent || '').trim();
            if (/^\\w{3}\\s+\\d{1,2},\\s+\\d{4}$/.test(pt)) {
                headerDate = pt;
            }
        }
        orders.push({
            order_ref: orderRef,
            status: status || 'unknown',
            date_text: dateText || headerDate,
            total_text: totalText,
            items_text: itemsText,
            detail_url: href,
            delivery_type: deliveryType,
        });
    }
    return orders;
})()
"""


async def _extract_carrefour_order_cards(page):
    """Extract Carrefour order cards via Playwright (legacy path).

    Disabled in the live flow: Akamai fingerprints Playwright Chromium
    and 403s the auth API, so the live Carrefour pipeline uses UC Chrome
    instead (see process_carrefour_orders_uc). Kept around as a
    one-liner in case Akamai's stance ever changes.
    """
    return await page.evaluate(_CARREFOUR_EXTRACT_JS_IIFE)


# JS that extracts line items from the Carrefour order page ("Consignment
# Details", /mafuae/en/app/orders/{ref}/{consignmentId}). That page is the
# ONLY working URL -- the bare /app/orders/{ref} is an empty shell and the
# Kibsons-style /en/orderinvoice/RSO/{ref}/order 404s for Carrefour.
#
# Strategy (text-receipt parse): items render as plain text, NOT as
# product cards, so we read body.innerText line-by-line between the
# "<N> Items" header and "Order Summary". Each block is
# qty / name / [size] / price-int / .price-dec / AED. This mirrors the
# proven Kibsons detail parser and is robust to lazy-loaded images and
# tailwind class churn. Product PDP URLs (/mafuae/en/{sku}?sid=...) are
# zipped on by document order when their count matches the item count.
#
# Iterate via /api/diag/scrape-carrefour-detail {background:true,order_id:N}
# + GET /api/diag/carrefour-backfill-result; the debug block reports
# itemsParsed / sectionStart / firstItems (and dumps page text only when
# the "<N> Items" section can't be found -- i.e. Carrefour changed layout).
_CARREFOUR_ORDER_DETAIL_JS = """
(function() {
    // 2026-06-04 rewrite: the Carrefour order page ("Consignment Details",
    // /mafuae/en/app/orders/{ref}/{consignmentId}) renders line items as a
    // TEXT RECEIPT, not as PDP-link cards. The previous link-anchored
    // extractor returned 0 items because (a) it searched for '/p/' PDP
    // links but Carrefour PDPs are '/mafuae/en/{sku}?sid=...', and (b) the
    // item rows carry no anchor at all -- name/qty/price are plain text.
    //
    // Each item block in body.innerText looks like:
    //     1                          <- quantity (integer) OR weight "0.47Kg"
    //     Jenan ... Eggs ... 15 PCS  <- product name
    //     15 PCS                     <- size/variant (sometimes absent)
    //     33                         <- price integer part
    //     .49                        <- price decimal part (separate node)
    //     AED                        <- currency
    //     [AED 9.99]                 <- optional original/struck price
    // The split int+.dec+AED is the actual paid LINE total; unit = total/qty.
    // Items live between the "<N> Items" header and "Order Summary".
    //
    // Product PDP links (/mafuae/en/{sku}?sid=...) appear 1:1 with items in
    // document order; we zip them on by index when the counts match so each
    // row still carries a product_url.
    //
    // Returns {items: [...], debug: {...}}.

    const bodyText = (document.body && document.body.innerText) || '';
    const lines = bodyText.split(/\\r?\\n/).map(s => s.trim()).filter(Boolean);

    // Product PDP links in document order. Carrefour PDPs look like
    // /mafuae/en/2214047?sid=EXPRESS&... -- numeric sku, no '/p/'.
    const productUrls = [];
    const seenSku = new Set();
    for (const a of Array.from(document.querySelectorAll('a[href]'))) {
        const href = a.getAttribute('href') || '';
        const m = href.match(/^\\/mafuae\\/[a-z]{2}\\/(\\d{3,})\\?/);
        if (m && href.indexOf('sid=') !== -1 && !seenSku.has(m[1])) {
            seenSku.add(m[1]);
            productUrls.push('https://www.carrefouruae.com' + href);
        }
    }

    const isQty = (s) => /^\\d{1,3}$/.test(s);
    const isWeight = (s) => /^\\d*\\.?\\d+\\s*kg$/i.test(s);
    const isPriceInt = (s) => /^\\d{1,5}$/.test(s);
    const isPriceDec = (s) => /^\\.\\d{2}$/.test(s);
    const isCurrency = (s) => /^AED$/i.test(s);
    const isFullPrice = (s) => /^AED\\s+[\\d,.]+$/i.test(s);

    // Item-section bounds: from the "<N> Items" header to "Order Summary".
    let start = -1, end = lines.length;
    for (let i = 0; i < lines.length; i++) {
        if (/^\\d+\\s+items?$/i.test(lines[i])) { start = i + 1; break; }
    }
    for (let i = (start < 0 ? 0 : start); i < lines.length; i++) {
        if (/^order summary/i.test(lines[i])) { end = i; break; }
    }

    const items = [];
    if (start >= 0) {
        let i = start;
        while (i < end) {
            const line = lines[i];
            let qty = 0, weight = false;
            if (isWeight(line)) { qty = 1; weight = true; }
            else if (isQty(line)) { qty = parseInt(line, 10); }
            else { i++; continue; }

            // Name is the very next line; reject if it looks like a
            // number / currency / price fragment (means we mis-read qty).
            const name = lines[i + 1] || '';
            if (!name || name.length < 3 || isQty(name) || isWeight(name)
                    || isCurrency(name) || isFullPrice(name) || isPriceDec(name)) {
                i++; continue;
            }

            // Scan forward for the paid-price triple: INT, .DEC, AED.
            let total = 0, consumedTo = i + 1;
            for (let j = i + 2; j < end && j <= i + 7; j++) {
                if (isPriceInt(lines[j]) && isPriceDec(lines[j + 1] || '')
                        && isCurrency(lines[j + 2] || '')) {
                    total = parseFloat(lines[j] + (lines[j + 1] || ''));
                    consumedTo = j + 2;
                    if (isFullPrice(lines[j + 3] || '')) consumedTo = j + 3;
                    break;
                }
                // Stop if we ran into the next item (qty followed by a name).
                if ((isQty(lines[j]) || isWeight(lines[j]))
                        && lines[j + 1] && lines[j + 1].length >= 3
                        && !isQty(lines[j + 1]) && !isCurrency(lines[j + 1])) {
                    break;
                }
            }
            if (total <= 0) { i++; continue; }

            const unit = qty > 1 ? +(total / qty).toFixed(2) : total;
            items.push({
                name: name,
                quantity: qty,
                price: unit,
                total_price: total,
                brand: '',
                url: '',
                image_url: '',
                weight_based: weight,
            });
            i = consumedTo + 1;
        }
    }

    // Attach product URLs by index when the counts line up (defensive --
    // a recommendations carousel would break the 1:1, so only zip on match).
    if (productUrls.length === items.length) {
        for (let k = 0; k < items.length; k++) items[k].url = productUrls[k];
    }

    const debug = {
        url: location.href,
        title: document.title,
        itemsParsed: items.length,
        productUrlCount: productUrls.length,
        sectionStart: start,
        sectionEnd: end,
        bodyTextLength: bodyText.length,
        firstItems: items.slice(0, 3),
        // Only dump the page text when we found NO section -- that's the
        // signal Carrefour changed the layout and we need to look again.
        textSample: (start < 0 ? bodyText.slice(0, 1500) : ''),
    };
    return {items: items, debug: debug};
})()
""".strip()


def _scrape_carrefour_order_detail_uc(driver, detail_url, return_debug=False):
    """Visit a Carrefour order-invoice page in UC Chrome and extract items.

    Args:
        driver: a UC Chrome WebDriver (the same one process_carrefour_orders_uc uses).
        detail_url: relative or absolute. Comes from the order summary
                    extraction; format /en/orderinvoice/RSO/{ref}/order.
        return_debug: if True, returns (items, debug) tuple rather
                    than just items. Used by the backfill path to
                    capture diagnostic info when extraction is empty.

    Returns:
        list of dicts with keys: name, quantity, price, total_price,
        brand, url, image_url. Empty list if the page didn't yield any
        recognisable rows. (Carrefour DOM drift surfaces that way;
        the debug snapshot will tell you what was on the page.)
    """
    base = "https://www.carrefouruae.com"
    if not detail_url:
        return ([], {"error": "no detail_url"}) if return_debug else []
    full_url = detail_url if detail_url.startswith("http") else (base + detail_url)

    try:
        driver.get(full_url)
        # Carrefour invoice pages hydrate progressively. 8 s gives the
        # React tree time to render line items even when the network
        # is slow; cheaper than the surprise of empty extractions.
        time.sleep(8)
    except Exception as e:
        msg = f"      ERROR loading Carrefour detail page {full_url}: {e}"
        print(msg)
        return ([], {"error": str(e)[:200]}) if return_debug else []

    try:
        raw = driver.execute_script("return " + _CARREFOUR_ORDER_DETAIL_JS.lstrip())
        if raw is None:
            return ([], {"error": "js returned null"}) if return_debug else []
        # New JS contract: {items: [...], debug: {...}}
        # Old JS contract: [...] (kept for compatibility during rollout)
        if isinstance(raw, dict):
            items_raw = raw.get("items") or []
            debug = raw.get("debug") or {}
        elif isinstance(raw, list):
            items_raw = raw
            debug = {"legacy_extractor": True}
        else:
            return ([], {"error": f"unexpected js return type {type(raw).__name__}"}) if return_debug else []

        items_clean = [
            it for it in items_raw
            if isinstance(it, dict)
            and it.get("name")
            and it.get("price", 0) > 0
        ]
        return (items_clean, debug) if return_debug else items_clean
    except Exception as e:
        msg = f"      ERROR extracting items from {full_url}: {e}"
        print(msg)
        return ([], {"error": str(e)[:200]}) if return_debug else []


def backfill_carrefour_order_items(order_id=None, dry_run=False):
    """Re-scrape detail pages for Carrefour orders that have 0 items.

    Args:
        order_id: int -- if given, only this order. Otherwise scrapes
                  every delivered Carrefour order with no order_items
                  rows.
        dry_run: if True, prints what would be inserted without writing.

    Uses the same UC Chrome session pattern as
    process_carrefour_orders_uc (acquires the browser lock, etc.).
    Designed to be called from a diag endpoint or as a one-off CLI.

    Returns dict: {"orders_processed", "items_inserted", "errors",
                   "debug_per_order"} where debug_per_order is a small
                   diagnostic snapshot for each order (especially
                   useful when items_inserted=0 to see what was on
                   the page).
    """
    # Lazy import to avoid pulling in UC + selenium at startup.
    from cart_builder import _launch_uc_chrome, _uc_ensure_carrefour_login

    result = {
        "orders_processed": 0,
        "items_inserted": 0,
        "errors": [],
        "debug_per_order": [],
    }

    if app_state.cart_build_active:
        result["errors"].append("cart_build_active blocks scrape")
        return result

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        store_id = _get_store_id("Carrefour UAE", conn)
        if not store_id:
            result["errors"].append("Carrefour store_id not found")
            return result

        if order_id:
            rows = conn.execute(
                """SELECT id, order_ref, detail_url FROM orders
                   WHERE id = ? AND store_id = ?""",
                (order_id, store_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT o.id, o.order_ref, o.detail_url FROM orders o
                   WHERE o.store_id = ?
                     AND o.status IN ('delivered', 'completed')
                     AND NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.id)
                   ORDER BY o.order_date DESC""",
                (store_id,),
            ).fetchall()

        if not rows:
            return result

        app_state.cart_build_active = True
        driver = None
        try:
            driver = _launch_uc_chrome()
            if not _uc_ensure_carrefour_login(driver):
                result["errors"].append("Carrefour login failed")
                return result

            # Phase 1: visit the orders LIST page once and refresh
            # detail_url for any orders we're about to backfill that
            # don't already have one. We trust the orders list URLs
            # because those are exactly what Carrefour links to from
            # their own UI -- no guessing path formats.
            try:
                driver.get("https://www.carrefouruae.com/mafuae/en/app/orders")
                time.sleep(8)
                live_orders = _extract_carrefour_orders_uc(driver) or []
                live_by_ref = {
                    o.get("order_ref"): o.get("detail_url")
                    for o in live_orders
                    if o.get("order_ref") and o.get("detail_url")
                }
                # Diagnostic: what the live orders-list page actually
                # handed us (the real href Carrefour links to), so we can
                # tell whether the detail nav fails because the URL is
                # wrong vs. the page itself won't direct-load.
                result["live_list"] = {
                    "count": len(live_orders),
                    "sample": [
                        {"ref": o.get("order_ref"),
                         "detail_url": o.get("detail_url"),
                         "status": o.get("status"),
                         "delivery_type": o.get("delivery_type")}
                        for o in live_orders[:6]
                    ],
                }
                if live_by_ref:
                    refreshed = 0
                    for r in rows:
                        ref_ = r["order_ref"]
                        cur_url = r["detail_url"] if "detail_url" in r.keys() else None
                        if not cur_url and ref_ in live_by_ref:
                            conn.execute(
                                "UPDATE orders SET detail_url = ? WHERE id = ?",
                                (live_by_ref[ref_], r["id"]),
                            )
                            refreshed += 1
                    if refreshed:
                        conn.commit()
                        print(f"  [carrefour-backfill] refreshed detail_url for {refreshed} order(s) from live list")
                        # Re-read so the loop below uses the updated values
                        ids = [r["id"] for r in rows]
                        rows = conn.execute(
                            "SELECT id, order_ref, detail_url FROM orders "
                            f"WHERE id IN ({','.join('?' for _ in ids)})",
                            ids,
                        ).fetchall()
            except Exception as e:
                print(f"  [carrefour-backfill] live-list refresh failed: {e}")
                result["errors"].append(f"live_list_refresh: {str(e)[:120]}")

            for row in rows:
                order_id_ = row["id"]
                order_ref = row["order_ref"]
                stored_url = row["detail_url"] if "detail_url" in row.keys() else None

                # URL candidate order:
                #   1. The exact href we stored when we first saw the
                #      order (most accurate -- whatever Carrefour
                #      handed us at scrape time).
                #   2. /mafuae/en/app/orders/{ref}  -- Carrefour's
                #      canonical app path.
                #   3. /en/orderinvoice/RSO/{ref}/order  -- the
                #      legacy URL the old code used (Kibsons-style).
                #      Kept as last resort because the old data we're
                #      backfilling may have nothing else to try.
                url_candidates = []
                if stored_url:
                    url_candidates.append(stored_url)
                url_candidates.append(f"/mafuae/en/app/orders/{order_ref}")
                url_candidates.append(f"/en/orderinvoice/RSO/{order_ref}/order")

                print(f"  [carrefour-backfill] order_id={order_id_} ref={order_ref}")
                items, debug = [], {}
                cand_debugs = []
                loaded_debug = None  # full debug of the first candidate that loaded a real page
                for cand in url_candidates:
                    print(f"    trying {cand}")
                    items, debug = _scrape_carrefour_order_detail_uc(
                        driver, cand, return_debug=True,
                    )
                    final_url = debug.get("url") or ""
                    body_len = debug.get("bodyTextLength") or 0
                    # A "real" detail page: navigation succeeded (not a Chrome
                    # error page) and there's substantial content. The empty
                    # /app/orders/{ref} shell (no consignment id) is ~318 chars;
                    # the real "Consignment Details" page is ~2400+.
                    page_loaded = (not final_url.startswith("chrome-error")) and body_len > 800
                    cand_debugs.append({
                        "candidate": cand,
                        "final_url": final_url,
                        "title": debug.get("title"),
                        "bodyTextLength": body_len,
                        "allLinkCount": debug.get("allLinkCount"),
                        "productLinkCount": debug.get("productLinkCount"),
                        "imgCount": debug.get("imgCount"),
                        "items": len(items),
                    })
                    if items:
                        debug["used_url"] = cand
                        loaded_debug = debug
                        # Backfill the detail_url for future runs so
                        # we don't keep retrying broken candidates.
                        if not stored_url:
                            try:
                                conn.execute(
                                    "UPDATE orders SET detail_url = ? WHERE id = ?",
                                    (cand, order_id_),
                                )
                                conn.commit()
                            except Exception:
                                pass
                        break
                    if page_loaded and loaded_debug is None:
                        # The real detail page loaded but the extractor found
                        # no items. Keep its full debug (textSample / img alts
                        # for redesign) and STOP -- the remaining candidates are
                        # the empty shell and a hard 404, so falling through to
                        # them only masks the page we actually want.
                        debug["used_url"] = cand
                        loaded_debug = debug
                        # Persist this working URL so we don't re-probe.
                        if not stored_url:
                            try:
                                conn.execute(
                                    "UPDATE orders SET detail_url = ? WHERE id = ?",
                                    (cand, order_id_),
                                )
                                conn.commit()
                            except Exception:
                                pass
                        break
                if loaded_debug is not None:
                    debug = loaded_debug
                print(f"    extracted {len(items)} items")
                # Capture per-order diagnostic snapshot so callers
                # can see what was on the page even on zero-yield
                # extractions. `candidates` shows each URL's fate;
                # `debug` is the LAST candidate's full snapshot (incl.
                # linkHrefSample / textSample for extractor redesign).
                result["debug_per_order"].append({
                    "order_id": order_id_,
                    "order_ref": order_ref,
                    "stored_detail_url": stored_url,
                    "items_extracted": len(items),
                    "candidates": cand_debugs,
                    "debug": debug,
                })

                if dry_run:
                    for it in items[:5]:
                        print(f"      [DRY] {it.get('name')} qty={it.get('quantity')} price={it.get('price')}")
                else:
                    cur = conn.cursor()
                    for it in items:
                        qty = it.get("quantity", 1) or 1
                        price = it.get("price", 0) or 0
                        cur.execute(
                            """INSERT INTO order_items
                                   (order_id, raw_name, quantity, unit_price,
                                    total_price, brand, product_url)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (
                                order_id_,
                                it.get("name", ""),
                                qty,
                                price,
                                it.get("total_price", price * qty),
                                it.get("brand", ""),
                                it.get("url", ""),
                            ),
                        )
                        result["items_inserted"] += 1
                    conn.commit()

                    # Enrichment (links to items table) so cadence calc picks it up.
                    try:
                        from enrich_order_history import enrich_new_order_items
                        enrich_new_order_items(order_id_, conn)
                    except Exception as e:
                        print(f"    enrich warning: {e}")

                result["orders_processed"] += 1
                # Polite delay between detail pages.
                time.sleep(2)
        finally:
            app_state.cart_build_active = False
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
    finally:
        conn.close()

    return result


def _extract_carrefour_orders_uc(driver):
    """Selenium counterpart of _extract_carrefour_order_cards.

    Runs the same JS payload (a module-level constant) inside UC Chrome
    via driver.execute_script. Returns a Python list of dicts -- shape
    is identical to the Playwright path.

    The .lstrip() defends against JavaScript ASI: the constant starts
    with a newline (triple-quoted string), and a LineTerminator
    immediately after `return` causes the engine to parse the script as
    `return; (function(){...})();` -- a bare `return` plus a discarded
    IIFE -- and execute_script then gets undefined / None.
    """
    return driver.execute_script("return " + _CARREFOUR_EXTRACT_JS_IIFE.lstrip())


def _normalize_carrefour_status(raw):
    """Map Carrefour status text to our internal status.

    Order in priority order -- 'out for delivery' must beat 'delivered'
    substring checks etc. A SCRAPED card always represents a real placed
    order, so the fallback is 'confirmed' (placed), NOT 'expected':
    'expected' is reserved for the pre-scrape placeholder. Returning
    'expected' here was why freshly-scraped orders ("Arrives Today",
    "Scheduled") got stuck and never advanced.
    """
    raw = (raw or '').lower().strip()
    if 'cancel' in raw:
        return 'cancelled'
    if 'delivered' in raw or 'completed' in raw:
        return 'delivered'
    # On-the-way variants (check before generic 'arriv'/'today').
    if ('out for delivery' in raw or 'out for' in raw or 'dispatch' in raw
            or 'on the way' in raw or 'on its way' in raw or 'shipping' in raw):
        return 'dispatched'
    if 'process' in raw or 'prepar' in raw or 'packing' in raw:
        return 'processing'
    # Scheduled / arriving / confirmed / received -> placed & confirmed.
    if ('confirm' in raw or 'arriv' in raw or 'scheduled' in raw
            or 'today' in raw or 'tomorrow' in raw or 'received' in raw
            or 'placed' in raw):
        return 'confirmed'
    return 'confirmed'  # scraped order is real -> at least placed/confirmed


def _parse_carrefour_date(text):
    """Parse Carrefour date formats.

    Handles:
      - "Mon, Feb 23rd 2026, 9:05 PM" (status date)
      - "Feb 23, 2026" (header date)
    """
    if not text:
        return ""
    text = text.strip()
    # Remove day-of-week prefix: "Mon, " -> ""
    text = re.sub(r'^\w{3},\s*', '', text)
    # Remove ordinal suffixes: "23rd" -> "23"
    text = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', text)
    # Remove time suffix: ", 9:05 PM" -> ""
    text = re.sub(r',\s*\d{1,2}:\d{2}\s*(AM|PM).*$', '', text, flags=re.IGNORECASE)
    # Now we should have "Feb 23 2026" or "Feb 23, 2026"
    text = text.replace(',', '').strip()
    for fmt in ["%b %d %Y", "%B %d %Y", "%d %b %Y"]:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Fallback to generic parser
    return _parse_date(text)


def _is_delivery_text(text):
    """True if a card's date text describes a DELIVERY/arrival, not the
    placement date (e.g. 'Arrives Today', 'Arriving Tomorrow', 'Delivery ...')."""
    return bool(re.search(r"(?i)arriv|deliver|today|tomorrow", text or ""))


def _parse_delivery_date(text):
    """Extract an arrival/delivery date from card text like
    'Arrives Today May 29 2026', 'Arriving Tomorrow', 'Delivery May 30 2026'.
    Returns ISO yyyy-mm-dd, or '' if none found.
    """
    if not text:
        return ""
    t = text.strip().lower()
    # Prefer an explicit date if present (strip the arrival/relative words).
    cleaned = re.sub(
        r"(?i)\b(arrives?|arriving|delivery|delivered|expected|scheduled|by|on|today|tomorrow)\b",
        " ", text,
    )
    iso = _parse_carrefour_date(cleaned.strip())
    if iso:
        return iso
    today = datetime.now().date()
    if "today" in t:
        return today.strftime("%Y-%m-%d")
    if "tomorrow" in t:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    return ""


def process_carrefour_orders_uc(dry_run=False, pending_dict=None):
    """Scrape Carrefour order history + update pending statuses, via UC Chrome.

    Carrefour's Akamai Bot Manager fingerprints Playwright Chromium and
    403s its calls to the auth API, even from a residential IP. UC Chrome
    (cart_builder's launcher) is not fingerprinted the same way and gets
    through fine, so the Carrefour orders pipeline runs there.

    Combines new-order scraping AND pending-order status checks into one
    UC session so we don't pay the browser-launch cost twice.

    SQLite connections are thread-bound, so this function opens its own
    connection -- the caller's connection may live on a different thread
    (we run under asyncio.to_thread).

    Args:
        dry_run: if True, log but do not write to the DB.
        pending_dict: optional {order_ref: dict-like} of existing pending
            orders whose statuses should be updated. Plain dicts only --
            the caller should convert sqlite3.Row to dict before passing
            for thread safety. Pass None to skip status updates.

    Returns:
        {"new_orders": [order_dict, ...],
         "status_changes": [{"order_id", "old", "new"}, ...]}
    """
    # Lazy import: cart_builder pulls in selenium + UC. Only load when we
    # actually need to scrape Carrefour.
    from cart_builder import (
        _launch_uc_chrome,
        _uc_ensure_carrefour_login,
        _uc_carrefour_email_login,
    )

    store_name = "Carrefour UAE"
    result = {"new_orders": [], "status_changes": []}

    # Browser-profile lock: ~/.hearthkeeper-carrefour/ is shared with
    # cart_builder. Bail rather than fight if a cart build is mid-flight;
    # the next scheduler tick will retry.
    if app_state.cart_build_active:
        print("  Skipping: another browser job is in progress (cart_build_active=True)")
        log_event("order_poller_skip", source="order_scraper",
                  detail="Carrefour UC blocked by cart_build_active")
        return result

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        store_id = _get_store_id(store_name, conn)
        latest = _get_latest_order_ref(store_name, conn)

        print(f"\n  {'='*50}")
        print(f"  CARREFOUR ORDER HISTORY (UC Chrome)")
        print(f"  {'='*50}")
        if latest:
            print(f"  Last known order: {latest['order_ref']} ({latest['order_date']})")
        else:
            print(f"  No previous orders in DB for Carrefour")

        app_state.cart_build_active = True
        driver = None
        try:
            driver = _launch_uc_chrome()

            # Ensure logged in (uses cached UC profile session if available,
            # otherwise drives the email/password flow).
            if not _uc_ensure_carrefour_login(driver):
                print(f"  ERROR: Could not log into Carrefour. Check credentials in .env")
                log_event("order_poller_error", source="order_scraper",
                          detail="Carrefour UC login failed", level="error")
                return result

            # Navigate to the orders page
            print(f"  Navigating to order history...")
            try:
                driver.get(CARREFOUR_ORDERS_URL)
                time.sleep(5)
            except Exception as e:
                print(f"  ERROR loading order history page: {e}")
                return result

            # Carrefour step-up auths /app/* routes: the homepage cookies
            # show us "logged in" (so _uc_ensure_carrefour_login returns
            # True), but hitting /app/orders bounces us to /login/phone
            # demanding a fresh credential exchange. Detect that, drop
            # the partial session (cookies AND web storage -- UC's
            # persistent profile keeps localStorage/IndexedDB across
            # delete_all_cookies(), which is exactly what makes the
            # homepage check lie to us), then drive the email login form
            # directly via _uc_carrefour_email_login (bypassing the
            # misleading homepage check), and re-navigate.
            if "/login" in driver.current_url.lower():
                print(f"  Orders page redirected to {driver.current_url} -- forcing re-login...")
                # Land on the Carrefour origin so localStorage/sessionStorage
                # belong to it (clear() is origin-scoped).
                try:
                    driver.get("https://www.carrefouruae.com/mafuae/en/")
                    time.sleep(2)
                except Exception as e:
                    print(f"  WARN: homepage navigation before re-auth failed: {e}")
                try:
                    driver.execute_script(
                        "try { localStorage.clear(); sessionStorage.clear(); } catch(e) {}"
                    )
                except Exception as e:
                    print(f"  WARN: web-storage clear failed: {e}")
                try:
                    driver.delete_all_cookies()
                except Exception as e:
                    print(f"  WARN: cookie clear failed: {e}")
                # Bypass the homepage "logged-in" shortcut -- it can still
                # report True after the clear above on some sessions. Drive
                # the email form unconditionally.
                if not _uc_carrefour_email_login(driver):
                    print(f"  ERROR: Re-login failed.")
                    log_event("order_poller_error", source="order_scraper",
                              detail="Carrefour UC re-login failed", level="error")
                    return result
                try:
                    driver.get(CARREFOUR_ORDERS_URL)
                    time.sleep(5)
                except Exception as e:
                    print(f"  ERROR re-navigating to orders page: {e}")
                    return result
                if "/login" in driver.current_url.lower():
                    print(f"  Still on {driver.current_url} after re-auth -- aborting Carrefour.")
                    log_event("order_poller_error", source="order_scraper",
                              detail="Carrefour orders page persistently redirects to login",
                              level="error")
                    return result

            # Extract via shared JS
            try:
                orders_data = _extract_carrefour_orders_uc(driver)
            except Exception as e:
                print(f"  ERROR extracting orders: {e}")
                log_event("order_poller_error", source="order_scraper",
                          detail=f"Carrefour extract failed: {e}", level="error")
                return result

            print(f"  Found {len(orders_data)} orders on the page")

            # Per-scrape delivery-type summary -- so we can see the now /
            # scheduled / unlabeled mix without a DB schema change. Visible
            # via /api/diag/recent-errors. Once we're happy with the
            # detection, we can promote this to a real column.
            dt_counts = {"now": 0, "scheduled": 0, "unlabeled": 0}
            now_refs = []
            for od in orders_data:
                t = (od.get("delivery_type") or "").lower()
                if t == "now":
                    dt_counts["now"] += 1
                    now_refs.append(od.get("order_ref", ""))
                elif t == "scheduled":
                    dt_counts["scheduled"] += 1
                else:
                    dt_counts["unlabeled"] += 1
            log_event(
                "carrefour_delivery_mix",
                source="order_scraper",
                detail=(
                    f"now={dt_counts['now']} scheduled={dt_counts['scheduled']} "
                    f"unlabeled={dt_counts['unlabeled']} "
                    f"now_refs={','.join(now_refs[:10])}"
                )[:380],
            )

            if not orders_data:
                return result

            # Walk the extracted cards: new orders get saved; existing orders
            # whose ref is in pending_dict get their status updated.
            for od in orders_data:
                ref = od.get("order_ref", "")
                if not ref:
                    continue

                raw_status = od.get("status", "unknown")
                new_status = _normalize_carrefour_status(raw_status)

                # Best-effort backfill of delivery_type on existing rows --
                # the column was added later, and pre-existing Carrefour
                # orders have it NULL. Only write if currently NULL, so we
                # never clobber a value already set.
                # Best-effort backfill of delivery_date from the card's
                # arrival text ("Arrives Today …") on existing rows that
                # don't have one yet -- so already-reconciled orders pick up
                # their delivery date on the next scrape.
                _dt_text = od.get("date_text", "")
                if _is_delivery_text(_dt_text):
                    _dd = _parse_delivery_date(_dt_text)
                    if _dd:
                        try:
                            conn.execute(
                                """UPDATE orders SET delivery_date = ?
                                   WHERE store_id = ? AND order_ref = ?
                                     AND COALESCE(delivery_date,'') = ''""",
                                (_dd, store_id, ref),
                            )
                            conn.commit()
                        except Exception as e:
                            print(f"  WARN: delivery_date backfill failed for {ref}: {e}")

                scraped_dt = (od.get("delivery_type") or "").lower()
                if scraped_dt in ("now", "scheduled"):
                    try:
                        conn.execute(
                            """UPDATE orders SET delivery_type = ?
                               WHERE store_id = ? AND order_ref = ?
                                 AND (delivery_type IS NULL OR delivery_type = '')""",
                            (scraped_dt, store_id, ref),
                        )
                        conn.commit()
                    except Exception as e:
                        print(f"  WARN: delivery_type backfill failed for {ref}: {e}")

                if _order_exists(ref, store_id, conn):
                    if pending_dict and ref in pending_dict:
                        db_order = pending_dict[ref]
                        old_status = db_order["status"]
                        if new_status and new_status != old_status:
                            change = _update_order_status(
                                db_order["id"], old_status, new_status, conn
                            )
                            result["status_changes"].append(change)
                            print(f"    Status #{ref}: {old_status} -> {new_status}")
                        else:
                            # Mark we checked even if nothing changed
                            conn.execute(
                                "UPDATE orders SET last_status_check = ? WHERE id = ?",
                                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), db_order["id"]),
                            )
                            conn.commit()
                    continue

                # New order -- visit the detail page to extract items.
                # Falls back to items=[] if the detail extractor returns
                # nothing (Carrefour DOM drift); the order header still
                # saves so we don't lose the row. The backfill function
                # below can re-scrape items later for orders with 0 rows.
                detail_url = od.get("detail_url", "")
                items_for_order = []
                if detail_url:
                    try:
                        items_for_order = _scrape_carrefour_order_detail_uc(
                            driver, detail_url
                        )
                        print(f"    [detail] {ref}: extracted {len(items_for_order)} items")
                    except Exception as e:
                        print(f"    [detail] {ref}: extraction failed: {e}")

                # Carrefour's card "date" is often the ARRIVAL/delivery date
                # ("Arrives Today May 29 2026"), not the placement date. Route
                # it to delivery_date and use today as the placement proxy for
                # a just-placed order; otherwise treat it as the order date.
                _date_text = od.get("date_text", "")
                if _is_delivery_text(_date_text):
                    _delivery_date = _parse_delivery_date(_date_text)
                    _order_date = datetime.now().strftime("%Y-%m-%d")
                else:
                    _delivery_date = ""
                    _order_date = _parse_carrefour_date(_date_text)
                order_data = {
                    "store_name": store_name,
                    "store_id": store_id,
                    "order_ref": ref,
                    "order_date": _order_date,
                    "delivery_date": _delivery_date or None,
                    "total_amount": _parse_price(od.get("total_text", "")),
                    "status": new_status,
                    "items": items_for_order,
                    "source": "browser_scrape",
                    "delivery_type": (od.get("delivery_type") or "").lower() or None,
                    # Persist the exact href Carrefour exposes so the
                    # backfill scraper can revisit this URL later
                    # without guessing the right path format.
                    "detail_url": detail_url,
                }
                if dry_run:
                    print(f"    [DRY RUN] Would save: {ref} | {order_data['order_date']} | "
                          f"{new_status} | {order_data['total_amount']} AED")
                else:
                    _save_order(order_data, conn)
                    print(f"    Saved: {ref} | {order_data['order_date']} | "
                          f"{new_status} | {order_data['total_amount']} AED")
                result["new_orders"].append(order_data)

            print(f"  New orders saved: {len(result['new_orders'])}")
            if pending_dict:
                print(f"  Status changes:   {len(result['status_changes'])}")
            return result

        finally:
            app_state.cart_build_active = False
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# KIBSONS ORDER SCRAPER
# ============================================================

async def _extract_kibsons_order_cards(page):
    """Extract order data from Kibsons order history page (whichever tab is active).

    Kibsons uses Tailwind CSS. Order cards are bordered divs containing:
    - Order ID header: div with "Order ID #12345678"
    - Status: span with "Order Dispatched" / "Delivered" etc.
    - Placed on date, Arriving/Delivered on date, Items count, Total price
    - View Order link: a[href*="orderinvoice/RSO/{ref}"]
    """
    return await page.evaluate("""
        () => {
            const orders = [];
            // Find all "View Order" links — each is inside one order card
            const viewLinks = document.querySelectorAll('a[href*="orderinvoice/RSO"]');

            for (const link of viewLinks) {
                // Walk up to the card container (bordered div)
                let card = link;
                for (let i = 0; i < 10; i++) {
                    card = card.parentElement;
                    if (!card) break;
                    const cls = card.className || '';
                    if (cls.includes('tw-border') && cls.includes('tw-rounded')) break;
                }
                if (!card) continue;

                // Extract order ref from link href: /en/orderinvoice/RSO/12166841/order
                const href = link.getAttribute('href') || '';
                const refMatch = href.match(/RSO\\/(\\d+)\\//);
                if (!refMatch) continue;

                // Get all leaf text nodes in the card
                const texts = [];
                const walker = document.createTreeWalker(card, NodeFilter.SHOW_ELEMENT);
                let node;
                while (node = walker.nextNode()) {
                    if (node.childNodes.length === 1 && node.childNodes[0].nodeType === 3) {
                        texts.push(node.textContent.trim());
                    }
                }

                // Parse fields from texts
                let status = '', date_text = '', arriving_text = '', total_text = '', items_text = '';
                for (let i = 0; i < texts.length; i++) {
                    const t = texts[i];
                    // Status: first text that looks like a status
                    if (/delivered|dispatched|confirmed|processing|cancelled|shipped|completed|preparing/i.test(t) && !status) {
                        status = t;
                    }
                    // Date after "Placed on:" is always the placed date
                    if (/placed on/i.test(texts[i-1] || '') || (!date_text && /^\\d{1,2}-\\w{3}-\\d{4}/.test(t))) {
                        date_text = t;
                    }
                    // Arriving/Delivered on date
                    if (/arriving on|delivered on/i.test(texts[i-1] || '')) {
                        arriving_text = t;
                    }
                    // Items count: "Items ( 9 )"
                    if (/^items\\s*\\(/i.test(t)) {
                        items_text = t;
                    }
                    // Total: standalone number (typically after items count)
                    if (/^\\d+\\.\\d{2}$/.test(t)) {
                        total_text = t;
                    }
                }

                orders.push({
                    order_ref: refMatch[1],
                    status: status,
                    date_text: date_text,
                    arriving_text: arriving_text,
                    total_text: total_text,
                    items_text: items_text,
                    detail_url: href,
                });
            }
            return orders;
        }
    """)


async def _scrape_kibsons_order_detail(page, detail_url):
    """Navigate to a Kibsons order detail page and extract line items.

    The detail page URL looks like: /en/orderinvoice/RSO/{ref}/order
    Each item row typically has: product name, quantity, unit price.

    Returns list of dicts: [{name, quantity, price, brand, url}, ...]
    """
    base = "https://www.kibsons.com"
    url = detail_url if detail_url.startswith("http") else base + detail_url

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
    except Exception as e:
        print(f"      ERROR loading order detail page: {e}")
        return []

    # Extract items from the order detail page.
    # Kibsons order detail format (text-based, no product links):
    #   x1          ← quantity
    #   Double Cream Milk  ← product name
    #   UAE         ← origin country
    #   2 Litres    ← size/variant
    #   12.70       ← price
    #   / bottle    ← unit
    #   Buy Again   ← action button
    items = await page.evaluate("""
        () => {
            const items = [];
            const bodyText = document.body.innerText || '';

            // Split into lines and find the "Cart Items" section
            const lines = bodyText.split('\\n').map(l => l.trim()).filter(l => l);
            let inCart = false;
            let i = 0;

            for (; i < lines.length; i++) {
                if (/^Cart Items/i.test(lines[i])) {
                    inCart = true;
                    i++;
                    break;
                }
            }

            if (!inCart) return items;

            // Parse items: look for "x{qty}" pattern followed by name then price
            while (i < lines.length) {
                const line = lines[i];

                // Stop at "Order Summary" section
                if (/^Order Summary/i.test(line)) break;

                // Match quantity pattern: "x1", "x2", etc.
                const qtyMatch = line.match(/^x(\\d+)$/);
                if (qtyMatch) {
                    const qty = parseInt(qtyMatch[1]);

                    // Next non-empty line is the product name
                    i++;
                    if (i >= lines.length) break;
                    const name = lines[i];

                    // Skip origin, size lines — scan forward until we find the price
                    let price = 0;
                    let j = i + 1;
                    while (j < lines.length && j <= i + 5) {
                        const pMatch = lines[j].match(/^(\\d+\\.\\d{2})$/);
                        if (pMatch) {
                            price = parseFloat(pMatch[1]);
                            break;
                        }
                        // Also stop if we hit next item or section
                        if (/^x\\d+$/.test(lines[j]) || /^Order Summary/i.test(lines[j])) break;
                        j++;
                    }

                    // Skip "Buy Again" and similar non-product lines
                    if (name && !/^Buy Again$/i.test(name) && name.length > 1) {
                        items.push({
                            name: name,
                            quantity: qty,
                            price: price,
                            url: '',
                        });
                    }
                }
                i++;
            }

            return items;
        }
    """)

    return items or []


def _normalize_kibsons_status(raw):
    """Map Kibsons status text to our internal status.

    Priority-ordered. A scraped card is a real placed order, so the
    fallback is 'confirmed' (placed), NOT 'expected' -- 'expected' is the
    pre-scrape placeholder state only. Returning 'expected' here was why
    reconciled Kibsons orders (real ref + items) stayed stuck.
    """
    raw = (raw or '').lower().strip()
    if 'cancel' in raw:
        return 'cancelled'
    if 'delivered' in raw or 'complete' in raw:
        return 'delivered'  # Kibsons "Completed" = delivered
    if ('out for delivery' in raw or 'out for' in raw or 'dispatch' in raw
            or 'on the way' in raw or 'on its way' in raw or 'ship' in raw):
        return 'dispatched'
    if 'process' in raw or 'prepar' in raw or 'packing' in raw:
        return 'processing'
    if ('confirm' in raw or 'arriv' in raw or 'scheduled' in raw
            or 'today' in raw or 'tomorrow' in raw or 'received' in raw
            or 'placed' in raw):
        return 'confirmed'
    return 'confirmed'  # scraped order is real -> at least placed/confirmed


async def scrape_kibsons_orders(page, conn, dry_run=False):
    """
    Scrape order history from Kibsons.

    Kibsons order history page has two tabs: Upcoming Orders and Past Orders.
    Each tab shows order cards with ID, status, dates, total, and a View Order link.

    Returns list of new order dicts.
    """
    store_name = "Kibsons"
    store_id = _get_store_id(store_name, conn)
    latest = _get_latest_order_ref(store_name, conn)

    print(f"\n  {'='*50}")
    print(f"  SCRAPING KIBSONS ORDER HISTORY")
    print(f"  {'='*50}")
    if latest:
        print(f"  Last known order: {latest['order_ref']} ({latest['order_date']})")
    else:
        print(f"  No previous orders in DB for Kibsons")

    # Lazy import: app_logging pulls in the DB layer; we don't want every
    # scrape call to pay that cost at top-of-module import time.
    from app_logging import log_event

    # Navigate to order history
    print(f"  Navigating to order history...")
    try:
        await page.goto(KIBSONS_ORDERS_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)
    except Exception as e:
        print(f"  ERROR loading order history page: {e}")
        log_event("kibsons_order_page_load_failed", source="order_scraper",
                  detail=str(e)[:300], level="error")
        return []

    # Check login — auto-login if session expired. Surface this as a
    # structured event so a silent login break (the 2026-04 / 05 gap on
    # the Ally between order_id 41 and 50) shows up in recent-errors next
    # time rather than disappearing into stdout.
    if not await ensure_logged_in(page, "Kibsons"):
        print(f"  ERROR: Could not log into Kibsons. Check credentials in .env")
        log_event("kibsons_login_failed", source="order_scraper",
                  detail="ensure_logged_in returned False; "
                         "check KIBSONS_EMAIL/KIBSONS_PASSWORD in .env",
                  level="error")
        return []

    # Re-navigate after login (may have been redirected)
    if "orderhistory" not in page.url.lower():
        await page.goto(KIBSONS_ORDERS_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

    # Extract order cards from the Upcoming tab (shown by default)
    orders_data = await _extract_kibsons_order_cards(page)
    print(f"  Found {len(orders_data)} upcoming orders")

    # Also check Past Orders tab for recently delivered orders
    try:
        past_tab = await page.query_selector('text="Past Orders"')
        if past_tab:
            await past_tab.click()
            await asyncio.sleep(3)
            past_orders = await _extract_kibsons_order_cards(page)
            orders_data.extend(past_orders)
            print(f"  Found {len(past_orders)} past orders")
    except Exception as e:
        print(f"  Could not scrape Past Orders tab: {e}")
        log_event("kibsons_past_orders_tab_failed", source="order_scraper",
                  detail=str(e)[:300], level="warning")

    print(f"  Total orders found: {len(orders_data)}")

    if not orders_data:
        print(f"  No orders found on page.")
        snippet = await page.evaluate("() => (document.body.innerText || '').substring(0, 300)")
        print(f"  Page preview: {snippet[:200]}...")
        # Empty-page event: distinguishes between login OK / page reachable
        # but order cards not detected (selector change, account empty, etc.)
        # vs login failure above. Critical for future debugging.
        log_event("kibsons_orders_empty", source="order_scraper",
                  detail=f"url={page.url[:120]} preview={snippet[:150]!r}",
                  level="warning")
        return []

    # Filter to only new orders (not already in DB)
    new_orders = []
    for od in orders_data:
        ref = od.get("order_ref", "")
        if not ref:
            continue
        if _order_exists(ref, store_id, conn):
            continue
        new_orders.append(od)

    print(f"  New orders to save: {len(new_orders)}")

    if not new_orders:
        print(f"  All orders already in DB.")
        # Still run backfill for existing orders missing items
        if not dry_run:
            await _backfill_kibsons_order_items(page, conn, store_id)
        return []

    # Save each new order — scrape detail page for items
    scraped = []
    for i, od in enumerate(new_orders):
        ref = od["order_ref"]
        raw_status = od.get("status", "unknown")
        status = _normalize_kibsons_status(raw_status)

        # Scrape the order detail page for line items
        detail_url = od.get("detail_url", "")
        items = []
        if detail_url and not dry_run:
            print(f"    [{i+1}/{len(new_orders)}] Scraping detail page for {ref}...")
            items = await _scrape_kibsons_order_detail(page, detail_url)
            print(f"      Found {len(items)} items")

        order_data = {
            "store_name": store_name,
            "store_id": store_id,
            "order_ref": ref,
            "order_date": _parse_date(od.get("date_text", "")),
            "total_amount": _parse_price(od.get("total_text", "")),
            "status": status,
            "expected_by": _parse_date(od.get("arriving_text", "")),
            "items": items,
            "source": "browser_scrape",
        }

        if dry_run:
            print(f"    [DRY RUN] Would save: {ref} | {order_data['order_date']} | "
                  f"{status} | {order_data['total_amount']} AED | {len(items)} items")
        else:
            _save_order(order_data, conn)
            print(f"    Saved: {ref} | {order_data['order_date']} | "
                  f"{status} | {order_data['total_amount']} AED | {len(items)} items")

        scraped.append(order_data)

    # Backfill items for existing orders that have 0 items
    if not dry_run:
        await _backfill_kibsons_order_items(page, conn, store_id)

    return scraped


async def _backfill_kibsons_order_items(page, conn, store_id):
    """Re-scrape order detail pages for Kibsons orders missing items."""
    cur = conn.cursor()
    # Find orders with 0 items
    rows = cur.execute("""
        SELECT o.id, o.order_ref
        FROM orders o
        WHERE o.store_id = ?
          AND NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.id)
          AND o.order_ref NOT LIKE 'kibsons_screen_%'
          AND o.order_ref NOT LIKE 'CRF-%'
    """, (store_id,)).fetchall()

    if not rows:
        return

    print(f"\n  Backfilling items for {len(rows)} orders with 0 items...")

    for row in rows:
        order_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        order_ref = row["order_ref"] if isinstance(row, sqlite3.Row) else row[1]
        detail_url = f"/en/orderinvoice/RSO/{order_ref}/order"

        print(f"    Backfill {order_ref}...")
        items = await _scrape_kibsons_order_detail(page, detail_url)

        if not items:
            print(f"      No items found, skipping")
            continue

        # Insert the items
        for item in items:
            qty = item.get("quantity", 1) or 1
            price = item.get("price", 0) or 0
            cur.execute("""
                INSERT INTO order_items (order_id, raw_name, quantity, unit_price, total_price, brand, product_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                order_id,
                item.get("name", ""),
                qty,
                price,
                price * qty,
                item.get("brand", ""),
                item.get("url", ""),
            ))

        conn.commit()
        print(f"      Saved {len(items)} items")

        # Enrich new items
        try:
            from enrich_order_history import enrich_new_order_items
            enrich_new_order_items(order_id, conn)
        except Exception as e:
            print(f"      [WARN] Enrichment failed: {e}")

        await asyncio.sleep(2)  # Polite delay between pages


# ============================================================
# SAVE ORDER TO DB
# ============================================================

def _save_order(order_data, conn):
    """Save a scraped order to the database.

    If an 'expected' placeholder exists for this store, update it instead
    of creating a duplicate.
    """
    cur = conn.cursor()
    store_id = order_data["store_id"]
    order_ref = order_data["order_ref"]

    # A scraped order is a real, placed order -- never let it land as
    # 'expected' (that state is reserved for the pre-scrape placeholder).
    # Covers any store path (e.g. Kibsons) that didn't set a status, which
    # was leaving reconciled orders stuck at 'expected'.
    _st = (order_data.get("status") or "").strip().lower()
    if not _st or _st == "expected":
        order_data["status"] = "confirmed"

    # Skip if already exists
    if _order_exists(order_ref, store_id, conn):
        return None

    total = order_data.get("total_amount", 0)
    if not total and order_data.get("items"):
        total = sum(
            (it.get("price", 0) or 0) * (it.get("quantity", 1) or 1)
            for it in order_data["items"]
        )

    # Check for an 'expected' placeholder to update
    cur.execute("""
        SELECT id FROM orders
        WHERE store_id = ? AND status = 'expected'
        ORDER BY order_date DESC LIMIT 1
    """, (store_id,))
    expected = cur.fetchone()

    delivery_type = order_data.get("delivery_type")
    delivery_date = order_data.get("delivery_date") or None
    detail_url = order_data.get("detail_url") or None

    if expected:
        # Update the placeholder with real data
        order_id = expected["id"]
        cur.execute("""
            UPDATE orders
            SET order_ref = ?, order_date = ?, total_amount = ?,
                status = ?, source = ?, delivery_type = ?,
                delivery_date = COALESCE(?, delivery_date),
                detail_url = COALESCE(?, detail_url),
                scraped_at = datetime('now')
            WHERE id = ?
        """, (
            order_ref,
            order_data.get("order_date", ""),
            total,
            order_data.get("status", "completed"),
            order_data.get("source", "browser_scrape"),
            delivery_type,
            delivery_date,
            detail_url,
            order_id,
        ))
        # Log the status transition
        _update_order_status(order_id, "expected",
                             order_data.get("status", "completed"),
                             conn, source="scraper")
        print(f"    Updated expected order #{order_id} with real data")
    else:
        cur.execute("""
            INSERT INTO orders (store_id, order_date, order_ref, total_amount, currency, status, source, expected_by, delivery_type, delivery_date, detail_url)
            VALUES (?, ?, ?, ?, 'AED', ?, ?, ?, ?, ?, ?)
        """, (
            store_id,
            order_data.get("order_date", ""),
            order_ref,
            total,
            order_data.get("status", "completed"),
            order_data.get("source", "browser_scrape"),
            order_data.get("expected_by"),
            delivery_type,
            delivery_date,
            detail_url,
        ))
        order_id = cur.lastrowid

    # Save items
    for item in order_data.get("items", []):
        qty = item.get("quantity", 1) or 1
        price = item.get("price", 0) or 0
        cur.execute("""
            INSERT INTO order_items (order_id, raw_name, quantity, unit_price, total_price, brand, product_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            order_id,
            item.get("name", ""),
            qty,
            price,
            price * qty,
            item.get("brand", ""),
            item.get("url", ""),
        ))

    conn.commit()

    # Auto-enrich new order items (extract brands, link to items table)
    try:
        from enrich_order_history import enrich_new_order_items
        enrich_new_order_items(order_id, conn)
    except Exception as e:
        print(f"  [WARN] Order enrichment failed (non-critical): {e}")

    return order_id


# ============================================================
# MAIN SCRAPER
# ============================================================

async def scrape_orders(store_filter=None, dry_run=False):
    """
    Scrape order history from all (or specified) stores.

    Args:
        store_filter: "carrefour", "kibsons", or None for both
        dry_run: if True, show what would be saved without writing

    Returns dict:
        {
            "new_orders": [{"store_name", "order_ref", "order_date", "items": [...]}],
            "total_new": int,
            "stores_checked": [str],
        }
    """
    create_database()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    all_new = []
    stores_checked = []

    try:
        # Carrefour via UC Chrome (Playwright gets fingerprinted by Akamai).
        # Selenium is synchronous, so run in a thread to avoid blocking the
        # asyncio loop.
        if not store_filter or "carrefour" in store_filter.lower():
            stores_checked.append("Carrefour UAE")
            try:
                cf_result = await asyncio.to_thread(
                    process_carrefour_orders_uc, dry_run, None
                )
                all_new.extend(cf_result.get("new_orders", []))
            except Exception as e:
                print(f"  ERROR processing Carrefour: {e}")

        # Kibsons via Playwright (no anti-bot issues there)
        if not store_filter or "kibsons" in store_filter.lower():
            stores_checked.append("Kibsons")
            print("\n  Launching Playwright browser for Kibsons...")
            p, context, page = await launch_edge()
            try:
                new = await scrape_kibsons_orders(page, conn, dry_run=dry_run)
                all_new.extend(new)
            except Exception as e:
                print(f"  ERROR scraping Kibsons: {e}")
            finally:
                await safe_close(p, context)

    finally:
        conn.close()

    result = {
        "new_orders": all_new,
        "total_new": len(all_new),
        "stores_checked": stores_checked,
    }

    # Summary
    print(f"\n  {'='*50}")
    print(f"  ORDER SCRAPE COMPLETE")
    print(f"  {'='*50}")
    print(f"  Stores checked: {', '.join(stores_checked)}")
    print(f"  New orders found: {len(all_new)}")
    for order in all_new:
        print(f"    - {order['store_name']}: #{order['order_ref']} "
              f"({order.get('order_date', '?')}) - {len(order.get('items', []))} items")
    print(f"  {'='*50}")

    return result


# ============================================================
# ORDER STATUS CHECKING
# ============================================================

def _update_order_status(order_id, old_status, new_status, conn, source="scraper"):
    """Update an order's status and log the transition."""
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        "UPDATE orders SET status = ?, last_status_check = ? WHERE id = ?",
        (new_status, now, order_id)
    )

    # If delivered, also set delivery_date
    if new_status == "delivered":
        cur.execute(
            "UPDATE orders SET delivery_date = ? WHERE id = ? AND delivery_date IS NULL",
            (now, order_id)
        )

    cur.execute("""
        INSERT INTO order_status_log (order_id, old_status, new_status, source)
        VALUES (?, ?, ?, ?)
    """, (order_id, old_status, new_status, source))

    conn.commit()
    return {"order_id": order_id, "old": old_status, "new": new_status}


def get_pending_orders(conn):
    """Get orders that haven't reached a terminal status yet."""
    cur = conn.cursor()
    cur.execute("""
        SELECT o.id, o.order_ref, o.status, o.order_date, o.store_id, s.name as store_name
        FROM orders o
        JOIN stores s ON o.store_id = s.id
        WHERE o.status NOT IN ('delivered', 'cancelled', 'completed')
        ORDER BY o.order_date DESC
    """)
    return cur.fetchall()


async def check_order_status(page, conn, skip_stores=None):
    """Check current status for pending orders by walking store history pages.

    Args:
        page: Playwright page -- this implementation drives store pages
            via Playwright.
        conn: open sqlite3 connection (row_factory=Row).
        skip_stores: optional iterable of store names to skip. Used when
            another code path has already handled that store via a
            different browser flavor (see process_carrefour_orders_uc()).

    Returns list of status changes:
        [{"order_id": 42, "old": "confirmed", "new": "delivered"}, ...]
    """
    pending = get_pending_orders(conn)
    if not pending:
        print("  No pending orders to check.")
        return []

    print(f"\n  {'='*50}")
    print(f"  CHECKING ORDER STATUS")
    print(f"  {'='*50}")
    print(f"  Pending orders: {len(pending)}")

    changes = []

    # Group pending orders by store
    by_store = {}
    for order in pending:
        store = order["store_name"]
        if store not in by_store:
            by_store[store] = []
        by_store[store].append(order)

    for store_name, orders in by_store.items():
        if skip_stores and store_name in skip_stores:
            continue
        print(f"\n  Checking {store_name} ({len(orders)} pending)...")
        refs_to_check = {o["order_ref"]: o for o in orders}

        if "Carrefour" in store_name:
            url = CARREFOUR_ORDERS_URL
        elif "Kibsons" in store_name:
            url = KIBSONS_ORDERS_URL
        else:
            print(f"  Unknown store: {store_name}, skipping")
            continue

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)
        except Exception as e:
            print(f"  ERROR loading {store_name} order history: {e}")
            continue

        # Check login — auto-login if session expired
        if not await ensure_logged_in(page, store_name):
            print(f"  WARNING: Could not log into {store_name}, skipping status check")
            continue

        # Re-navigate after login if needed
        if url not in page.url:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)

        # Extract order cards using store-specific functions
        if "Carrefour" in store_name:
            page_orders = await _extract_carrefour_order_cards(page)
        elif "Kibsons" in store_name:
            # Check BOTH tabs: Upcoming (default) and Past Orders.
            # Kibsons moves completed orders to "Past Orders" tab,
            # so we'd miss the dispatched→completed transition otherwise.
            page_orders = await _extract_kibsons_order_cards(page)
            try:
                past_tab = await page.query_selector('text="Past Orders"')
                if past_tab:
                    await past_tab.click()
                    await asyncio.sleep(3)
                    past_orders = await _extract_kibsons_order_cards(page)
                    page_orders.extend(past_orders)
            except Exception as e:
                print(f"  Could not check Past Orders tab: {e}")
        else:
            page_orders = []

        print(f"  Found {len(page_orders)} order cards on {store_name} page")
        print(f"  Looking for refs: {list(refs_to_check.keys())}")
        for po in page_orders:
            print(f"    Page card: ref={po.get('order_ref')}, status={po.get('status')!r}")

        # Match page statuses to our pending orders
        for page_order in page_orders:
            ref = page_order.get("order_ref", "")
            raw_status = page_order.get("status", "")
            if "Carrefour" in store_name:
                new_status = _normalize_carrefour_status(raw_status)
            else:
                new_status = _normalize_kibsons_status(raw_status)

            if ref in refs_to_check and new_status:
                db_order = refs_to_check[ref]
                old_status = db_order["status"]

                if new_status != old_status:
                    change = _update_order_status(
                        db_order["id"], old_status, new_status, conn
                    )
                    changes.append(change)
                    print(f"    Order #{ref}: {old_status} -> {new_status}")
                else:
                    print(f"    Order #{ref}: still {old_status}")

                # Update last_status_check even if no change
                conn.execute(
                    "UPDATE orders SET last_status_check = ? WHERE id = ?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), db_order["id"])
                )
                conn.commit()

    print(f"\n  Status changes: {len(changes)}")
    return changes


async def check_and_scrape_orders(store_filter=None, dry_run=False):
    """Combined flow: scrape new orders AND check pending-order statuses.

    Carrefour goes through UC Chrome -- process_carrefour_orders_uc()
    handles both new-order scraping and pending-order status updates in
    one UC session. Kibsons goes through Playwright: scrape new orders,
    then a Kibsons-only pass through the legacy check_order_status() for
    pending-order statuses.

    This is the main entry point for the scheduler.
    """
    create_database()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    all_new = []
    status_changes = []
    stores_checked = []

    # Build a per-store pending-orders dict up front -- both legs need it.
    pending_by_store = {}
    try:
        for order in get_pending_orders(conn):
            pending_by_store.setdefault(order["store_name"], {})[order["order_ref"]] = order
    except Exception as e:
        print(f"  WARN: could not load pending orders: {e}")

    try:
        # Carrefour via UC Chrome (combined new-orders + status updates)
        if not store_filter or "carrefour" in store_filter.lower():
            stores_checked.append("Carrefour UAE")
            try:
                cf_pending_rows = pending_by_store.get("Carrefour UAE", {})
                # Convert sqlite3.Row -> dict so the worker thread doesn't
                # touch any sqlite3 object bound to the main thread.
                cf_pending = {ref: dict(row) for ref, row in cf_pending_rows.items()}
                cf_result = await asyncio.to_thread(
                    process_carrefour_orders_uc, dry_run, cf_pending
                )
                all_new.extend(cf_result.get("new_orders", []))
                status_changes.extend(cf_result.get("status_changes", []))
            except Exception as e:
                print(f"  ERROR processing Carrefour: {e}")

        # Kibsons via Playwright (new orders + Kibsons-only status check).
        # The browser launch itself can raise -- e.g. if Playwright Chromium
        # isn't installed on the host (`playwright install chromium` never
        # ran). Treat that as a Kibsons-only failure: log it, skip the
        # Kibsons leg, and let any Carrefour results above still make it
        # out, so order_poller_done can land for the rest of the run.
        if not store_filter or "kibsons" in store_filter.lower():
            stores_checked.append("Kibsons")
            print("\n  Launching Playwright browser for Kibsons...")
            p = context = page = None
            try:
                p, context, page = await launch_edge()
            except Exception as e:
                err = str(e)[:300]
                print(f"  ERROR launching Playwright for Kibsons: {err}")
                from app_logging import log_event as _log_event
                _log_event(
                    "kibsons_launch_failed",
                    source="order_scraper",
                    detail=err,
                    level="error",
                )
            if page:
                try:
                    try:
                        new = await scrape_kibsons_orders(page, conn, dry_run=dry_run)
                        all_new.extend(new)
                    except Exception as e:
                        print(f"  ERROR scraping Kibsons new orders: {e}")
                    try:
                        kb_changes = await check_order_status(
                            page, conn, skip_stores={"Carrefour UAE"}
                        )
                        status_changes.extend(kb_changes)
                    except Exception as e:
                        print(f"  ERROR checking Kibsons status: {e}")
                finally:
                    await safe_close(p, context)

        # Safety net: any order that's been reconciled with a real store
        # ref is no longer a placeholder, so it must not stay 'expected'
        # (that state is for pre-scrape placeholders only). Promote to
        # 'confirmed' so it enters the normal placed -> on-the-way ->
        # delivered lifecycle. Fixes orders reconciled under older code
        # that left status='expected' with a ref attached.
        try:
            from app_logging import log_event as _log_evt
        except Exception:
            _log_evt = lambda *a, **k: None
        try:
            cur_sweep = conn.execute(
                """UPDATE orders SET status = 'confirmed'
                   WHERE TRIM(COALESCE(status, '')) = 'expected'
                     AND TRIM(COALESCE(order_ref, '')) != ''"""
            )
            promoted = cur_sweep.rowcount
            conn.commit()
            _log_evt("order_expected_promotion", source="order_scraper",
                     detail=f"promoted={promoted}")
            if promoted:
                print(f"  Promoted {promoted} reconciled order(s) expected -> confirmed")
        except Exception as e:
            _log_evt("order_expected_promotion_err", source="order_scraper",
                     detail=str(e)[:150], level="warning")
            print(f"  WARN: expected->confirmed promotion failed: {e}")

        # Household order-stage notifications (placed / on-the-way /
        # delivered). Idempotent (orders.notif_*_at), and gated to the
        # curator until ORDER_NOTIFS_TEST_ONLY is flipped, so this is safe
        # to run on every poll. Best-effort -- never block the scrape.
        try:
            from order_notifications import process_household_notifications
            summary = await process_household_notifications()
            if summary and any(summary.values()):
                print(f"  Household order notifications: {summary}")
        except Exception as e:
            print(f"  WARN: household order notifications failed: {e}")

    finally:
        conn.close()

    return {
        "new_orders": all_new,
        "total_new": len(all_new),
        "status_changes": status_changes,
        "stores_checked": stores_checked,
    }


def has_pending_orders():
    """Quick check: are there any orders not yet delivered/cancelled?"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    pending = get_pending_orders(conn)
    count = len(pending)
    conn.close()
    return count > 0


def get_active_order_for_frontend():
    """
    Get the most recent non-delivered, non-cancelled order for the tracking card.

    Returns dict or None:
        {order_id, store_name, order_ref, status, order_date, expected_by,
         items: [{name, quantity, price}], total_amount}
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT o.id, o.order_ref, o.status, o.order_date, o.total_amount,
                   o.expected_by, o.delivery_date, o.rating_status,
                   s.name as store_name
            FROM orders o
            JOIN stores s ON o.store_id = s.id
            WHERE o.status NOT IN ('delivered', 'cancelled', 'completed')
              -- Skip stale 'expected' placeholders that never reconciled to a
              -- real order (no order_ref) and are >2 days old. Otherwise a
              -- dead placeholder shows as a phantom "order placed" card
              -- forever (e.g. a Kibsons leg that failed to scrape).
              AND NOT (
                    o.status = 'expected'
                AND COALESCE(o.order_ref, '') = ''
                AND COALESCE(o.order_date, '') < date('now', '-2 days')
              )
            ORDER BY o.order_date DESC
            LIMIT 1
        """).fetchone()

        if not row:
            return None

        # Skip orders with no useful data (no order_ref AND no items)
        items = conn.execute("""
            SELECT raw_name as name, quantity, unit_price as price
            FROM order_items
            WHERE order_id = ?
        """, (row["id"],)).fetchall()

        if not row["order_ref"] and not items:
            return None

        # If expected_by is missing, estimate based on store + order date
        expected_by = row["expected_by"]
        if not expected_by and row["order_date"]:
            try:
                order_dt = datetime.strptime(row["order_date"][:10], "%Y-%m-%d")
                store_lower = (row["store_name"] or "").lower()
                # Kibsons: same-day delivery (morning window, order placed day before)
                # Historical data confirms 7/8 orders deliver on order_date itself
                if "kibsons" in store_lower:
                    est_dt = order_dt  # same-day delivery
                else:
                    est_dt = order_dt + timedelta(days=1)  # Carrefour: next-day
                expected_by = est_dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        # Store-specific delivery time windows
        store_lower = (row["store_name"] or "").lower()
        if "kibsons" in store_lower:
            delivery_window = {"start": "07:00", "end": "13:00"}
        elif "carrefour" in store_lower:
            delivery_window = {"start": "09:00", "end": "23:00"}
        else:
            delivery_window = None

        return {
            "order_id": row["id"],
            "store_name": row["store_name"],
            "order_ref": row["order_ref"],
            "status": row["status"],
            "order_date": row["order_date"],
            "expected_by": expected_by,
            "delivery_window": delivery_window,
            "total_amount": row["total_amount"],
            "items": [{"name": i["name"], "quantity": i["quantity"], "price": i["price"]} for i in items],
        }
    except Exception as e:
        print(f"  get_active_order_for_frontend error: {e}")
        return None
    finally:
        conn.close()


def get_delivered_unrated_order():
    """
    Get the most recent delivered order that hasn't been rated or dismissed.

    Returns dict or None:
        {order_id, store_name, order_ref, delivery_date, order_date,
         items: [{order_item_id, name, quantity, price, item_id}], total_amount}
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT o.id, o.order_ref, o.status, o.order_date, o.total_amount,
                   o.delivery_date, o.rating_status,
                   s.name as store_name
            FROM orders o
            JOIN stores s ON o.store_id = s.id
            WHERE o.status IN ('delivered', 'completed')
              AND (o.rating_status IS NULL OR o.rating_status = 'none')
              AND o.delivery_date IS NOT NULL
            ORDER BY o.delivery_date DESC, o.order_date DESC
            LIMIT 1
        """).fetchone()

        if not row:
            return None

        items = _enrich_order_items(conn, row["id"])
        return {
            "order_id": row["id"],
            "store_name": row["store_name"],
            "order_ref": row["order_ref"],
            "delivery_date": row["delivery_date"],
            "order_date": row["order_date"],
            "total_amount": row["total_amount"],
            "item_count": len(items),
            "items": items,
        }
    except Exception as e:
        print(f"  get_delivered_unrated_order error: {e}")
        return None
    finally:
        conn.close()


def _enrich_order_items(conn, order_id):
    """
    Get order items enriched with catalog data (brand, size, image, origin, organic).

    Enrichment chain:
    1. If item_id exists → look up via item_alternatives → product_catalog
    2. Otherwise → fuzzy-match raw_name directly against product_catalog
    Falls back gracefully when catalog data is unavailable.
    """
    # Get the store_id for this order (used to prefer same-store catalog matches)
    order_row = conn.execute("SELECT store_id FROM orders WHERE id = ?", (order_id,)).fetchone()
    order_store_id = order_row["store_id"] if order_row else None

    items_raw = conn.execute("""
        SELECT oi.id as order_item_id, oi.raw_name as name,
               oi.quantity, oi.unit_price as price, oi.item_id,
               oi.brand as order_brand, oi.size as order_size
        FROM order_items oi WHERE oi.order_id = ?
    """, (order_id,)).fetchall()

    enriched = []
    for i in items_raw:
        brand = i["order_brand"] or ""
        size = i["order_size"] or ""
        image_url = ""
        origin_country = ""
        is_organic = False
        found = False

        # Path 1: item_id → item_alternatives → product_catalog
        if i["item_id"]:
            try:
                catalog = conn.execute("""
                    SELECT pc.brand, pc.size, pc.image_url, pc.origin_country, pc.is_organic
                    FROM item_alternatives ia
                    JOIN product_catalog pc ON ia.catalog_id = pc.id
                    WHERE ia.item_id = ?
                    ORDER BY ia.score DESC LIMIT 1
                """, (i["item_id"],)).fetchone()
                if catalog:
                    brand = catalog["brand"] or brand
                    size = catalog["size"] or size
                    image_url = catalog["image_url"] or ""
                    origin_country = catalog["origin_country"] or ""
                    is_organic = bool(catalog["is_organic"]) if catalog["is_organic"] is not None else False
                    found = True
            except Exception:
                pass

        # Path 2: no item_id or no catalog match → search product_catalog directly
        if not found and i["name"]:
            try:
                raw = i["name"].strip()
                words = [w for w in raw.lower().split() if len(w) >= 3]
                if words:
                    like_clauses = []
                    params = []
                    for w in words[:3]:
                        like_clauses.append("LOWER(product_name) LIKE ?")
                        params.append(f"%{w}%")

                    where = ' AND '.join(like_clauses)
                    # Exclude pet products for non-pet searches
                    pet_exclude = ""
                    if not any(w in raw.lower() for w in ("pet", "dog", "cat", "fish", "bird")):
                        pet_exclude = "AND LOWER(category) NOT LIKE '%pet%'"

                    # Prefer same-store, then products with images, then shorter names
                    store_pref = ""
                    if order_store_id:
                        store_pref = f"CASE WHEN store_id = {order_store_id} THEN 0 ELSE 1 END,"

                    catalog = conn.execute(f"""
                        SELECT brand, size, image_url, origin_country, is_organic
                        FROM product_catalog
                        WHERE {where}
                          AND is_available = 1
                          {pet_exclude}
                        ORDER BY
                            {store_pref}
                            CASE WHEN image_url IS NOT NULL AND image_url != '' THEN 0 ELSE 1 END,
                            LENGTH(product_name) ASC
                        LIMIT 1
                    """, params).fetchone()
                    if catalog:
                        brand = catalog["brand"] or brand
                        size = catalog["size"] or size
                        image_url = catalog["image_url"] or ""
                        origin_country = catalog["origin_country"] or ""
                        is_organic = bool(catalog["is_organic"]) if catalog["is_organic"] is not None else False
            except Exception:
                pass

        enriched.append({
            "order_item_id": i["order_item_id"],
            "name": i["name"],
            "quantity": i["quantity"],
            "price": i["price"],
            "item_id": i["item_id"],
            "brand": brand,
            "size": size,
            "image_url": image_url,
            "origin_country": origin_country,
            "is_organic": is_organic,
        })
    return enriched


def sync_restock_dates_from_deliveries(conn=None):
    """
    Sync restock_rules with actual delivery dates.

    When an order is delivered, items in it shouldn't need restocking for
    avg_days_between days from the DELIVERY date (not the order placement date).
    This catches any deliveries that happened outside the normal list_finalize flow.

    Call this before generating a shopping list to ensure dates are current.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    cur = conn.cursor()

    # Find all delivered orders from the last 14 days that have delivery_date set
    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    delivered = cur.execute("""
        SELECT o.id, o.delivery_date, o.store_id
        FROM orders o
        WHERE o.status IN ('delivered', 'completed')
          AND o.delivery_date IS NOT NULL
          AND o.delivery_date >= ?
    """, (cutoff,)).fetchall()

    updated_count = 0
    for order in delivered:
        delivery_date = order["delivery_date"]
        # Parse delivery_date to just the date part (may include time)
        try:
            delivery_dt = datetime.strptime(delivery_date[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        # Never anchor restock to a FUTURE delivery date (scheduled-slot
        # orders, or a tz skew, can carry tomorrow's date). A future
        # last_ordered makes days_since_last clamp to 0, so the item looks
        # perpetually fresh and never comes due.
        _now = datetime.now()
        if delivery_dt.date() > _now.date():
            delivery_dt = datetime(_now.year, _now.month, _now.day)

        # Get order items that are linked to known items
        items = cur.execute("""
            SELECT oi.item_id
            FROM order_items oi
            WHERE oi.order_id = ? AND oi.item_id IS NOT NULL
        """, (order["id"],)).fetchall()

        for item in items:
            item_id = item["item_id"]

            # Get current restock rule
            rule = cur.execute("""
                SELECT last_ordered_date, avg_days_between, next_restock_date
                FROM restock_rules WHERE item_id = ?
            """, (item_id,)).fetchone()

            if not rule or not rule["avg_days_between"]:
                continue

            # Only update if this delivery is more recent than the recorded last_ordered_date
            last_ordered = rule["last_ordered_date"] or ""
            delivery_date_str = delivery_dt.strftime("%Y-%m-%d")

            if delivery_date_str > last_ordered:
                days = int(rule["avg_days_between"])
                new_next = (delivery_dt + timedelta(days=days)).strftime("%Y-%m-%d")

                cur.execute("""
                    UPDATE restock_rules
                    SET last_ordered_date = ?,
                        next_restock_date = ?,
                        updated_at = datetime('now')
                    WHERE item_id = ?
                """, (delivery_date_str, new_next, item_id))
                updated_count += 1

    if updated_count > 0:
        conn.commit()
        print(f"  [sync_restock] Updated {updated_count} restock dates from recent deliveries")

    if close_conn:
        conn.close()

    return updated_count


def get_all_delivered_unrated_orders():
    """
    Get ALL delivered orders that haven't been rated or dismissed.
    Returns list of dicts with enriched item data.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT o.id, o.order_ref, o.status, o.order_date, o.total_amount,
                   o.delivery_date, o.rating_status,
                   s.name as store_name
            FROM orders o
            JOIN stores s ON o.store_id = s.id
            WHERE o.status IN ('delivered', 'completed')
              AND (o.rating_status IS NULL OR o.rating_status = 'none')
              AND o.delivery_date IS NOT NULL
            ORDER BY o.delivery_date DESC, o.order_date DESC
        """).fetchall()

        if not rows:
            return []

        orders = []
        for row in rows:
            items = _enrich_order_items(conn, row["id"])
            orders.append({
                "order_id": row["id"],
                "store_name": row["store_name"],
                "order_ref": row["order_ref"],
                "delivery_date": row["delivery_date"],
                "order_date": row["order_date"],
                "total_amount": row["total_amount"],
                "item_count": len(items),
                "items": items,
            })
        return orders
    except Exception as e:
        print(f"  get_all_delivered_unrated_orders error: {e}")
        return []
    finally:
        conn.close()


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    store_filter = None
    dry_run = False
    check_status = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--store" and i + 1 < len(args):
            store_filter = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        elif args[i] == "--check-status":
            check_status = True
            i += 1
        elif args[i] == "--full":
            # Combined: scrape new + check status
            check_status = True
            i += 1
        else:
            i += 1

    if check_status:
        asyncio.run(check_and_scrape_orders(store_filter=store_filter, dry_run=dry_run))
    else:
        asyncio.run(scrape_orders(store_filter=store_filter, dry_run=dry_run))
