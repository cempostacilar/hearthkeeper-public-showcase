"""
AutoRestock - Live Verification Module

Verifies Curator picks by making RSC search queries against store websites.
Only checks the picked products (5-15 items, NOT full inventory) to confirm:
  - Product is still available
  - Current price matches what we have in catalog
  - Cross-store cheaper alternatives exist

Flow:
  1. Launch browser (or reuse existing page)
  2. Establish session on Carrefour → verify Carrefour picks, cross-store search for Kibsons picks
  3. Navigate to Kibsons → verify Kibsons picks, cross-store search for Carrefour picks
  4. Update product_catalog with live data
  5. Return results keyed by catalog_id

Usage:
    from live_verify import verify_picks, extract_picks_from_curation

    picks = extract_picks_from_curation(batch_result)
    results = await verify_picks(picks)
    # results: {catalog_id: VerifyResult, ...}
"""

import asyncio
import json
import logging
import time
import sqlite3
from dataclasses import dataclass, asdict
from typing import Optional

from browser_common import launch_edge, safe_close
from db_setup import DB_PATH

logger = logging.getLogger(__name__)

# ── Store IDs (must match db_setup.py) ──
CARREFOUR_STORE_ID = 2
KIBSONS_STORE_ID = 1

# ── Timing budgets ──
TOTAL_TIMEOUT = 30        # seconds — hard ceiling for entire verification
PER_SEARCH_TIMEOUT = 5    # seconds — per-product RSC search
SESSION_SETTLE_TIME = 2   # seconds — wait after navigation for WAF/CF

# ── Cross-store price thresholds ──
# Flag cheaper alternative only if savings exceed BOTH thresholds
CROSS_STORE_MIN_SAVINGS_AED = 3.0    # at least 3 AED cheaper
CROSS_STORE_MIN_SAVINGS_PCT = 0.15   # at least 15% cheaper

# ── Name matching thresholds ──
EXACT_MATCH_THRESHOLD = 0.6    # for verifying our own pick
CROSS_STORE_MATCH_THRESHOLD = 0.3  # for finding equivalents across stores


# ────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────

@dataclass
class VerifyResult:
    """Result of verifying a single product pick."""
    catalog_id: int
    verified: bool = False        # True = we attempted verification (even if OOS)
    is_available: bool = True     # True = product found in search results
    live_price: Optional[float] = None
    price_changed: bool = False
    old_price: float = 0.0
    price_diff: float = 0.0      # live - old (positive = got more expensive)
    cross_store: Optional[dict] = None  # {store_id, store_name, price, product_name, savings, savings_pct}
    error: Optional[str] = None

    def to_dict(self):
        return asdict(self)


# ────────────────────────────────────────────
# RSC Search JavaScript (one per store)
# ────────────────────────────────────────────
# These run inside page.evaluate() so they inherit WAF/CF cookies.
# Each returns {status, text, error?}.

CARREFOUR_SEARCH_JS = """
async (args) => {
    const keyword = args[0];
    const pageSize = args[1] || 10;

    // Primary: Carrefour v3 search endpoint
    const url = '/mafuae/en/v3/search?keyword=' + encodeURIComponent(keyword)
              + '&pageSize=' + pageSize + '&sortBy=relevance&_rsc=1';
    try {
        const resp = await fetch(url, {
            headers: {
                'RSC': '1',
                'Next-Url': '/mafuae/en/v3/search?keyword=' + encodeURIComponent(keyword)
                           + '&pageSize=' + pageSize + '&sortBy=relevance'
            }
        });
        if (!resp.ok) return { status: resp.status, text: '' };
        const text = await resp.text();
        return { status: resp.status, text: text };
    } catch (e) {
        return { status: 0, text: '', error: e.message };
    }
}
"""

KIBSONS_SEARCH_JS = """
async (args) => {
    const keyword = args[0];
    const url = '/en/search/' + encodeURIComponent(keyword) + '?_rsc=1';
    try {
        const resp = await fetch(url, {
            headers: {
                'RSC': '1',
                'Next-Url': '/en/search/' + encodeURIComponent(keyword)
            }
        });
        if (!resp.ok) return { status: resp.status, text: '' };
        const text = await resp.text();
        return { status: resp.status, text: text };
    } catch (e) {
        return { status: 0, text: '', error: e.message };
    }
}
"""


# ── Store configuration (keeps per-store logic DRY) ──
STORE_CONFIG = {
    CARREFOUR_STORE_ID: {
        "name": "Carrefour UAE",
        "search_js": CARREFOUR_SEARCH_JS,
        "home_url": "https://www.carrefouruae.com/mafuae/en/",
        "domain": "carrefouruae.com",
        "parser_module": "scrape_carrefour_rsc",
        "map_args": ("Search", ""),  # (group_name, cat_name) — cat_name replaced per query
    },
    KIBSONS_STORE_ID: {
        "name": "Kibsons",
        "search_js": KIBSONS_SEARCH_JS,
        "home_url": "https://www.kibsons.com/en/",
        "domain": "kibsons.com",
        "parser_module": "scrape_kibsons_rsc",
        "map_args": ("search", ""),  # (category, family) — family replaced per query
    },
}


# ────────────────────────────────────────────
# Main entry point
# ────────────────────────────────────────────

async def verify_picks(picks: list[dict], page=None, cross_store: bool = True,
                       timeout: Optional[float] = None) -> dict[int, VerifyResult]:
    """
    RSC-verify a list of Curator picks. For each pick, search the store
    via RSC and confirm the product is available + current price.

    Also performs cross-store price comparison:
    - While on store A, search for store B's picks equivalents
    - Flag alternatives that are significantly cheaper

    Args:
        picks: list of dicts, each with:
            catalog_id, product_name, product_url, store_id,
            expected_price, sku, item_name
        page: optional Playwright page (reuses existing browser session)

    Returns:
        dict keyed by catalog_id → VerifyResult
    """
    if not picks:
        return {}

    results: dict[int, VerifyResult] = {}
    deadline = time.time() + (timeout or TOTAL_TIMEOUT)
    own_browser = page is None
    p = context = None

    try:
        if own_browser:
            logger.info("[LiveVerify] Launching browser...")
            p, context, page = await launch_edge()

        # Group picks by store
        carrefour_picks = [pk for pk in picks if pk["store_id"] == CARREFOUR_STORE_ID]
        kibsons_picks = [pk for pk in picks if pk["store_id"] == KIBSONS_STORE_ID]

        logger.info(f"[LiveVerify] Verifying {len(carrefour_picks)} Carrefour + "
                     f"{len(kibsons_picks)} Kibsons picks")

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        try:
            # ── Phase 1: Carrefour session ──
            # Verify Carrefour picks + cross-store search Kibsons picks on Carrefour
            if (carrefour_picks or kibsons_picks) and time.time() < deadline:
                await _establish_session(page, CARREFOUR_STORE_ID, deadline)

                # Verify our Carrefour picks (critical path)
                for pk in carrefour_picks:
                    if time.time() >= deadline:
                        results[pk["catalog_id"]] = VerifyResult(
                            catalog_id=pk["catalog_id"], error="timeout")
                        continue
                    result = await _verify_single(page, pk, CARREFOUR_STORE_ID, deadline)
                    results[pk["catalog_id"]] = result

                # Cross-store: search Carrefour for cheaper alternatives to Kibsons picks
                if cross_store:
                    for pk in kibsons_picks:
                        if time.time() >= deadline:
                            break
                        cross = await _cross_store_search(
                            page, pk, CARREFOUR_STORE_ID, deadline)
                        if cross:
                            # Stash for later — attach after Kibsons picks verify
                            pk["_cross_store_carrefour"] = cross

            # ── Phase 2: Kibsons session ──
            # Verify Kibsons picks + cross-store search Carrefour picks on Kibsons
            if (kibsons_picks or carrefour_picks) and time.time() < deadline:
                await _establish_session(page, KIBSONS_STORE_ID, deadline)

                # Verify our Kibsons picks (critical path)
                for pk in kibsons_picks:
                    if time.time() >= deadline:
                        results[pk["catalog_id"]] = VerifyResult(
                            catalog_id=pk["catalog_id"], error="timeout")
                        continue
                    result = await _verify_single(page, pk, KIBSONS_STORE_ID, deadline)
                    # Attach cross-store from Phase 1 if we found one
                    cross = pk.get("_cross_store_carrefour")
                    if cross and result.verified and result.is_available:
                        result.cross_store = cross
                    results[pk["catalog_id"]] = result

                # Cross-store: search Kibsons for cheaper alternatives to Carrefour picks
                if cross_store:
                    for pk in carrefour_picks:
                        if time.time() >= deadline:
                            break
                        cat_id = pk["catalog_id"]
                        if cat_id in results and results[cat_id].verified and results[cat_id].is_available:
                            cross = await _cross_store_search(
                                page, pk, KIBSONS_STORE_ID, deadline)
                            if cross:
                                results[cat_id].cross_store = cross

            # ── Update catalog with live data ──
            _update_catalog(results, conn)
            conn.commit()

        finally:
            conn.close()

    except Exception as e:
        logger.error(f"[LiveVerify] Failed: {e}", exc_info=True)
        # Return partial results + unverified for the rest
        for pk in picks:
            if pk["catalog_id"] not in results:
                results[pk["catalog_id"]] = VerifyResult(
                    catalog_id=pk["catalog_id"], error=str(e)[:200])

    finally:
        if own_browser and p:
            await safe_close(p, context)

    verified_count = sum(1 for r in results.values() if r.verified)
    price_changes = sum(1 for r in results.values() if r.price_changed)
    oos_count = sum(1 for r in results.values() if r.verified and not r.is_available)
    cross_count = sum(1 for r in results.values() if r.cross_store)

    logger.info(f"[LiveVerify] Done: {verified_count}/{len(picks)} verified, "
                f"{price_changes} price changes, {oos_count} OOS, "
                f"{cross_count} cross-store alternatives")

    return results


# ────────────────────────────────────────────
# Session management
# ────────────────────────────────────────────

async def _establish_session(page, store_id: int, deadline: float):
    """
    Navigate to store homepage to establish WAF/Cloudflare session.
    Skips navigation if already on the right domain.
    """
    remaining = deadline - time.time()
    if remaining <= 0:
        return

    config = STORE_CONFIG[store_id]
    current_url = page.url or ""

    # Skip if already on the right domain
    if config["domain"] in current_url:
        logger.debug(f"[LiveVerify] Already on {config['name']}, skipping navigation")
        return

    timeout_ms = min(int(remaining * 1000), 15000)

    try:
        logger.debug(f"[LiveVerify] Navigating to {config['name']}...")
        await page.goto(
            config["home_url"],
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        await asyncio.sleep(SESSION_SETTLE_TIME)
    except Exception as e:
        logger.warning(f"[LiveVerify] Session establishment for {config['name']} failed: {e}")


# ────────────────────────────────────────────
# RSC search (store-agnostic)
# ────────────────────────────────────────────

async def _search_rsc(page, store_id: int, query: str,
                      deadline: float, limit: int = 10) -> list[dict]:
    """
    Execute an RSC search on a store and return mapped product dicts.

    Uses the store's search JS template + parser from the store's scraper.
    Returns product dicts in our catalog schema (same as catalog_store_products input).
    """
    remaining = deadline - time.time()
    if remaining <= 0:
        return []

    config = STORE_CONFIG[store_id]

    try:
        # Execute search in browser context
        timeout = min(remaining, PER_SEARCH_TIMEOUT)
        result = await asyncio.wait_for(
            page.evaluate(config["search_js"], [query, limit]),
            timeout=timeout,
        )

        if not result or result.get("error") or result.get("status") != 200:
            logger.debug(f"[LiveVerify] {config['name']} search '{query[:30]}': "
                         f"status={result.get('status') if result else '?'}, "
                         f"error={result.get('error') if result else '?'}")
            return []

        rsc_text = result.get("text", "")
        if not rsc_text:
            return []

        # Parse using the store's scraper module
        return _parse_search_results(store_id, rsc_text, query)

    except asyncio.TimeoutError:
        logger.debug(f"[LiveVerify] {config['name']} search '{query[:30]}' timed out")
        return []
    except Exception as e:
        logger.debug(f"[LiveVerify] {config['name']} search '{query[:30]}' failed: {e}")
        return []


def _parse_search_results(store_id: int, rsc_text: str, query: str) -> list[dict]:
    """Parse RSC search response using the appropriate store's parser."""

    if store_id == CARREFOUR_STORE_ID:
        from scrape_carrefour_rsc import parse_rsc_products, map_rsc_to_product
        raw = parse_rsc_products(rsc_text)
        return [map_rsc_to_product(p, "Search", query) for p in raw]

    elif store_id == KIBSONS_STORE_ID:
        from scrape_kibsons_rsc import parse_rsc_products, map_rsc_to_product
        raw = parse_rsc_products(rsc_text)
        return [map_rsc_to_product(p, "search", query) for p in raw]

    return []


# ────────────────────────────────────────────
# Single-product verification
# ────────────────────────────────────────────

async def _verify_single(page, pick: dict, store_id: int,
                         deadline: float) -> VerifyResult:
    """
    Verify a single pick by searching the store's RSC endpoint.

    Search strategy:
      1. Search by specific product name (most precise)
      2. If no match, retry with generic item name (broader)
    """
    result = VerifyResult(
        catalog_id=pick["catalog_id"],
        old_price=pick.get("expected_price", 0),
    )

    # First search: specific product name
    search_query = pick.get("product_name") or pick.get("item_name", "")
    if not search_query:
        result.error = "no_search_query"
        return result

    search_results = await _search_rsc(page, store_id, search_query, deadline)
    matched = _match_product(search_results, pick)
    any_results = bool(search_results)

    # Second search: generic item name (if different and first search failed)
    item_name = pick.get("item_name", "")
    if not matched and item_name and item_name.lower() != search_query.lower():
        search_results = await _search_rsc(page, store_id, item_name, deadline)
        matched = _match_product(search_results, pick)
        any_results = any_results or bool(search_results)

    if matched:
        result.verified = True
        result.is_available = True  # Found in search = available
        live_price = matched.get("price", 0)
        result.live_price = live_price

        if result.old_price and live_price:
            diff = round(live_price - result.old_price, 2)
            if abs(diff) >= 0.01:
                result.price_changed = True
                result.price_diff = diff
    elif any_results:
        # Search returned products but ours wasn't among them — genuinely out
        # of stock or delisted. This is a real, trustworthy OOS signal.
        result.verified = True
        result.is_available = False
        result.live_price = None
    else:
        # Search returned NOTHING at all. That's almost always a blocked/empty
        # search or a session failure — NOT a real availability signal. Mark
        # inconclusive (verified=False) so _update_catalog leaves the catalog
        # untouched. Conflating "search failed" with "out of stock" was marking
        # every product unavailable whenever the store blocked our requests.
        result.verified = False
        result.is_available = True  # unknown; never imply OOS off a failed search
        result.error = "search_empty"

    return result


# ────────────────────────────────────────────
# Cross-store price comparison
# ────────────────────────────────────────────

async def _cross_store_search(page, pick: dict, other_store_id: int,
                              deadline: float) -> Optional[dict]:
    """
    Search another store for a cheaper equivalent of a pick.

    Uses the GENERIC item name (e.g., "milk") not the specific product name
    (e.g., "Al Rawabi Full Cream Milk 1L") because cross-store products
    won't be identical. This is the fallback the user asked for.

    Returns cross-store info dict if significantly cheaper, else None.
    """
    item_name = pick.get("item_name", "")
    if not item_name:
        return None

    other_config = STORE_CONFIG.get(other_store_id)
    if not other_config:
        return None

    search_results = await _search_rsc(page, other_store_id, item_name, deadline, limit=5)
    if not search_results:
        return None

    # Find cheapest reasonable match from the other store
    best = _find_cheapest_match(search_results, item_name)
    if not best:
        return None

    our_price = pick.get("expected_price", 0)
    their_price = best.get("price", 0)

    if not our_price or not their_price:
        return None

    savings = round(our_price - their_price, 2)
    if savings <= 0:
        return None  # Not cheaper

    savings_pct = savings / our_price if our_price else 0

    # Only flag if savings exceed BOTH thresholds (significant enough to mention)
    if savings >= CROSS_STORE_MIN_SAVINGS_AED or savings_pct >= CROSS_STORE_MIN_SAVINGS_PCT:
        return {
            "store_id": other_store_id,
            "store_name": other_config["name"],
            "price": their_price,
            "product_name": best.get("name", ""),
            "savings": savings,
            "savings_pct": round(savings_pct * 100, 1),
        }

    return None


# ────────────────────────────────────────────
# Product matching
# ────────────────────────────────────────────

def _match_product(search_results: list[dict], pick: dict) -> Optional[dict]:
    """
    Find our picked product in search results.
    Match priority: product_url > sku/ean > name similarity.

    Returns the matching product dict, or None.
    """
    if not search_results:
        return None

    pick_url = (pick.get("product_url") or "").rstrip("/").lower()
    pick_sku = (pick.get("sku") or "").strip().lower()
    pick_name = (pick.get("product_name") or "").lower()

    # Tier 1: Match by product URL (most reliable)
    if pick_url:
        for p in search_results:
            result_url = (p.get("product_url") or "").rstrip("/").lower()
            if result_url and result_url == pick_url:
                return p

    # Tier 2: Match by SKU/EAN (cross-reference barcode)
    if pick_sku:
        for p in search_results:
            result_sku = (p.get("sku") or "").strip().lower()
            if result_sku and result_sku == pick_sku:
                return p

    # Tier 3: Name similarity (word overlap)
    if pick_name:
        best_match = None
        best_score = 0

        for p in search_results:
            result_name = (p.get("name") or "").lower()
            score = _name_similarity(pick_name, result_name)
            if score > best_score and score >= EXACT_MATCH_THRESHOLD:
                best_score = score
                best_match = p

        if best_match:
            return best_match

    return None


def _find_cheapest_match(search_results: list[dict], item_name: str) -> Optional[dict]:
    """
    From search results, find the cheapest product that reasonably matches
    the generic item name.

    Uses a LOW similarity threshold (0.3) because cross-store products
    won't have matching names. "milk" should match "Al Rawabi Full Cream Milk 1L".
    This is the FALLBACK for the cross-store comparison.
    """
    candidates = []
    item_words = set(item_name.lower().split())
    item_words -= {"the", "a", "an", "of", "and", "for", "with"}

    if not item_words:
        return None

    for p in search_results:
        name = (p.get("name") or "").lower()
        price = p.get("price", 0)

        if not name or not price or price <= 0:
            continue

        name_words = set(name.split())
        name_words -= {"the", "a", "an", "of", "and", "for", "with", "-", "–", "|"}

        overlap = item_words & name_words
        # How many of our search words appear in the product name?
        recall = len(overlap) / len(item_words) if item_words else 0

        if recall >= CROSS_STORE_MATCH_THRESHOLD:
            candidates.append((price, recall, p))

    if not candidates:
        return None

    # Sort by price (cheapest first), break ties by relevance (highest first)
    candidates.sort(key=lambda x: (x[0], -x[1]))
    return candidates[0][2]


def _name_similarity(a: str, b: str) -> float:
    """
    Word-overlap similarity between two product names.
    Returns 0.0–1.0 based on Jaccard-like overlap.
    """
    fillers = {"the", "a", "an", "of", "and", "for", "with", "-", "–", "|", "uae", "pack"}
    words_a = set(a.lower().split()) - fillers
    words_b = set(b.lower().split()) - fillers

    if not words_a or not words_b:
        return 0.0

    overlap = words_a & words_b
    # Use max denominator for stricter matching
    return len(overlap) / max(len(words_a), len(words_b))


# ────────────────────────────────────────────
# Catalog update
# ────────────────────────────────────────────

def _update_catalog(results: dict[int, VerifyResult], conn):
    """
    Update product_catalog with live verification data.

    Only updates products that were successfully verified.
    Sets scraped_at = now so the data is marked as fresh.
    """
    verified = [r for r in results.values() if r.verified and not r.error]
    oos = [r for r in verified if not r.is_available]
    # Safety valve: if (almost) everything verified came back OOS, that is far
    # more likely a verify/session failure than reality. Refuse to mass-mark the
    # catalog unavailable -- apply only the confirmed-available + price updates.
    # Without this, one blocked search session would flag the whole list OOS.
    distrust_oos = len(verified) >= 3 and len(oos) >= 0.8 * len(verified)
    if distrust_oos:
        logger.warning(
            f"[LiveVerify] Distrusting OOS verdicts ({len(oos)}/{len(verified)} "
            f"verified items OOS) -- treating as a verify failure, NOT marking "
            f"anything unavailable")

    updated = 0
    for catalog_id, result in results.items():
        if not result.verified or result.error:
            continue

        if result.live_price is not None and result.is_available:
            # Product confirmed available with live price
            conn.execute("""
                UPDATE product_catalog
                SET price = ?, is_available = 1, scraped_at = datetime('now')
                WHERE id = ?
            """, (result.live_price, catalog_id))
            updated += 1
        elif not result.is_available and not distrust_oos:
            # Product was searchable but ours wasn't there — mark unavailable.
            conn.execute("""
                UPDATE product_catalog
                SET is_available = 0, scraped_at = datetime('now')
                WHERE id = ?
            """, (catalog_id,))
            updated += 1

    logger.debug(f"[LiveVerify] Updated {updated} catalog entries")


# ────────────────────────────────────────────
# Helper: extract picks from curation result
# ────────────────────────────────────────────

def extract_picks_from_list(list_id: int, conn=None) -> list[dict]:
    """
    Extract verifiable picks from an existing shopping list (DB).

    Used for predicted lists (start_checkin) where there's no batch_result.
    Reads list_items + product_catalog to build the picks list.

    Returns list of pick dicts suitable for verify_picks().
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    try:
        rows = conn.execute("""
            SELECT li.id AS list_item_id, li.item_name_text, li.store_id,
                   li.catalog_id, li.estimated_price,
                   pc.product_name, pc.product_url, pc.sku
            FROM list_items li
            LEFT JOIN product_catalog pc ON li.catalog_id = pc.id
            WHERE li.list_id = ?
              AND li.status IN ('approved', 'modified')
              AND li.catalog_id IS NOT NULL
              AND li.store_id IS NOT NULL
        """, (list_id,)).fetchall()

        picks = []
        for row in rows:
            picks.append({
                "catalog_id": row["catalog_id"],
                "product_name": row["product_name"] or row["item_name_text"],
                "product_url": row["product_url"] or "",
                "store_id": row["store_id"],
                "expected_price": row["estimated_price"] or 0,
                "sku": row["sku"] or "",
                "item_name": row["item_name_text"],
            })

        return picks

    finally:
        if close_conn:
            conn.close()


def extract_picks_from_resolved_list(list_id: int, conn=None) -> list[dict]:
    """Build verify picks for EVERY non-skipped item on a list, resolving the
    product the frontend would currently show.

    Unlike extract_picks_from_list (which only sees 'approved'/'modified' items
    that already carry a catalog_id), this also resolves 'pending' suggestion
    items: their displayed product is computed on the fly from item_alternatives,
    the SAME way the frontend does it (frontend_bridge._get_product_info). That
    lets the Scheduled list be availability-checked even though its rows store no
    catalog_id.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True
    try:
        # Lazy import to avoid a circular import at module load.
        try:
            from frontend_bridge import _get_product_info
        except Exception:
            _get_product_info = None

        rows = conn.execute(
            """
            SELECT li.id AS list_item_id, li.item_id,
                   COALESCE(NULLIF(li.item_name_text, ''), i.name) AS item_name_text,
                   li.store_id, li.catalog_id, li.estimated_price
            FROM list_items li
            LEFT JOIN items i ON li.item_id = i.id
            WHERE li.list_id = ?
              AND COALESCE(li.status, '') != 'skipped'
              AND COALESCE(li.user_action, '') != 'skipped'
              AND li.store_id IS NOT NULL
            """,
            (list_id,),
        ).fetchall()

        picks = []
        seen = set()
        for row in rows:
            catalog_id = row["catalog_id"]
            product_name = row["item_name_text"]
            product_url = ""
            sku = ""
            price = row["estimated_price"] or 0

            if catalog_id:
                # Row already pins a product (approved / human pick) — verify it.
                pc = conn.execute(
                    "SELECT product_name, product_url, sku, price "
                    "FROM product_catalog WHERE id = ?",
                    (catalog_id,),
                ).fetchone()
                if pc:
                    product_name = pc["product_name"] or product_name
                    product_url = pc["product_url"] or ""
                    sku = pc["sku"] or ""
                    price = price or pc["price"] or 0
            elif _get_product_info and row["item_id"]:
                # Pending suggestion — resolve the displayed product like the UI.
                prod = _get_product_info(row["item_id"], row["store_id"], conn)
                if prod:
                    catalog_id = prod.get("catalog_id")
                    product_name = prod.get("product_name") or product_name
                    product_url = prod.get("product_url") or ""
                    price = price or prod.get("price") or 0

            if not catalog_id or catalog_id in seen:
                continue
            seen.add(catalog_id)
            picks.append({
                "catalog_id": catalog_id,
                "product_name": product_name,
                "product_url": product_url,
                "store_id": row["store_id"],
                "expected_price": price,
                "sku": sku,
                "item_name": row["item_name_text"],
            })
        return picks
    finally:
        if close_conn:
            conn.close()


async def live_verify_list_availability(list_id: int, conn=None) -> dict:
    """Live-check availability + price for the products a list currently shows,
    and write the truth back to product_catalog (verify_picks -> _update_catalog).

    Deliberately does NOT mutate list_items: 'pending' suggestion rows store no
    catalog_id, so once product_catalog.is_available is fresh the normal render
    path (frontend_bridge OOS auto-swap) and the cart resolver pick the best
    AVAILABLE alternative on their own. Returns stats so the caller can decide
    whether to re-render.

    Best-effort: a browser/verify hiccup returns changed=False, never raises.
    """
    try:
        picks = extract_picks_from_resolved_list(list_id, conn)
    except Exception as e:
        logger.warning(f"[LiveVerify] resolve picks for list {list_id} failed: {e}")
        return {"changed": False, "verified": 0, "oos": 0, "price_changes": 0,
                "checked": 0, "error": str(e)[:120]}
    if not picks:
        return {"changed": False, "verified": 0, "oos": 0, "price_changes": 0,
                "checked": 0}
    # Availability only: skip the per-pick cross-store price comparison (it
    # ~doubles the search count and starved the picks under the 30s budget), and
    # give a roomier deadline so every pick actually gets searched.
    verification = await verify_picks(picks, cross_store=False, timeout=120)
    verified = sum(1 for v in verification.values() if v.verified)
    oos = sum(1 for v in verification.values() if v.verified and not v.is_available)
    price_changes = sum(1 for v in verification.values() if v.price_changed)
    return {
        "changed": bool(oos or price_changes),
        "verified": verified,
        "oos": oos,
        "price_changes": price_changes,
        "checked": len(picks),
    }


async def debug_search(store_id: int, query: str = "milk", limit: int = 5,
                       pre_visit_store: Optional[int] = None,
                       fetch_path: Optional[str] = None,
                       capture_search: bool = False) -> dict:
    """Diagnostic: run ONE live RSC search with a generous timeout and return the
    RAW browser result (status / error / text length + snippet) plus the parsed
    product count. No 30s verify-budget pressure, so it isolates "is the search
    itself failing" from "the verify deadline ran out". If pre_visit_store is
    set, navigate THAT store's home first (on the same page) to reproduce the
    cross-store-contamination path verify_picks uses.
    """
    out = {"store_id": store_id, "query": query, "pre_visit_store": pre_visit_store}
    p = context = page = None
    try:
        config = STORE_CONFIG.get(store_id)
        if not config:
            return {"error": f"unknown store_id {store_id}"}
        p, context, page = await launch_edge()
        if pre_visit_store and pre_visit_store in STORE_CONFIG:
            try:
                await page.goto(STORE_CONFIG[pre_visit_store]["home_url"],
                                wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(SESSION_SETTLE_TIME)
                out["pre_visit_landed_url"] = page.url
            except Exception as e:
                out["pre_visit_error"] = str(e)[:200]
        if capture_search:
            # Navigate to the actual search PAGE and record every response URL,
            # to find the client-side API the page calls for products.
            urls = []
            page.on("response", lambda r: urls.append(
                (r.url, (r.headers or {}).get("content-type", ""))))
            try:
                await page.goto(config["home_url"] + "v3/search?keyword=" + query,
                                wait_until="load", timeout=40000)
                await asyncio.sleep(6)
            except Exception as e:
                out["capture_nav_error"] = str(e)[:150]
            out["total_responses"] = len(urls)
            # JSON responses from carrefour itself (excludes analytics/static) —
            # the product-listing API is the data we want.
            out["json_carrefour"] = sorted(set(
                u for (u, ct) in urls
                if "carrefour" in u.lower() and "json" in ct.lower()))[:40]
            return out
        try:
            await page.goto(config["home_url"], wait_until="domcontentloaded",
                            timeout=30000)
            await asyncio.sleep(SESSION_SETTLE_TIME)
            out["session_landed_url"] = page.url
        except Exception as e:
            out["session_error"] = str(e)[:200]
        out["url_before_search"] = page.url
        if fetch_path:
            # Raw-path probe: fetch an arbitrary path with RSC headers and report
            # the response shape (does it carry products?). Counting is done in JS
            # so we don't transfer ~1MB back. Used to hunt a working Carrefour route.
            _js = r'''async (p) => {
              try {
                const r = await fetch(p, {headers:{'RSC':'1','Next-Url': p.split('&_rsc')[0]}});
                const t = await r.text();
                const cnt = (s) => t.split(s).length - 1;
                return { status:r.status, len:t.length, ean:cnt('"ean"'),
                         next_f:cnt('__next_f'),
                         is_html: /^\s*<(!doctype|html)/i.test(t.slice(0,200)),
                         head:t.slice(0,160) };
              } catch(e) { return {status:0, error:String(e)}; }
            }'''
            try:
                out["fetch_path"] = fetch_path
                out["fetch_path_result"] = await asyncio.wait_for(
                    page.evaluate(_js, fetch_path), timeout=30)
            except Exception as e:
                out["fetch_path_error"] = str(e)[:200]
            return out
        try:
            result = await asyncio.wait_for(
                page.evaluate(config["search_js"], [query, limit]), timeout=30)
            if isinstance(result, dict):
                out["raw_status"] = result.get("status")
                out["raw_error"] = result.get("error")
                text = result.get("text") or ""
                out["text_len"] = len(text)
                out["text_snippet"] = text[:400]
                out["markers"] = {
                    "is_html": text[:200].lstrip().lower().startswith(("<!doctype", "<html")),
                    "next_f": text.count("__next_f"),
                    "ean_plain": text.count('"ean"'),
                    "ean_escaped": text.count('\\"ean\\"'),
                    "stockcode_plain": text.count('"stockCode"'),
                    "stockcode_escaped": text.count('\\"stockCode\\"'),
                }
                try:
                    parsed = _parse_search_results(store_id, text, query)
                    out["parsed_count"] = len(parsed)
                    out["first_parsed_name"] = (
                        parsed[0].get("name") if parsed else None)
                except Exception as e:
                    out["parse_error"] = str(e)[:200]
            else:
                out["raw_result_type"] = str(type(result))
        except asyncio.TimeoutError:
            out["search_timeout"] = True
        except Exception as e:
            out["search_error"] = str(e)[:200]
    except Exception as e:
        out["fatal"] = str(e)[:200]
    finally:
        if p:
            try:
                await safe_close(p, context)
            except Exception:
                pass
    return out


def extract_picks_from_curation(batch_result: dict) -> list[dict]:
    """
    Extract verifiable picks from a Curator batch_result.

    Translates the Curator's output format into what verify_picks() expects.
    Only includes successfully matched items (not unmatched/needs_input).

    Args:
        batch_result: dict from curate_basket(), with "items" list

    Returns:
        list of pick dicts, each with:
        {catalog_id, product_name, product_url, store_id,
         expected_price, sku, item_name}
    """
    picks = []

    for item in batch_result.get("items", []):
        if item.get("status") not in ("matched", "catalog_only"):
            continue

        # Get the best (recommended) option — options are already sorted by score
        options = item.get("options", [])
        if not options:
            continue

        # Find the option matching the recommended store
        rec_store = item.get("recommended_store_id")
        best = None
        for opt in options:
            if opt.get("store_id") == rec_store:
                best = opt
                break
        if not best:
            best = options[0]  # Fallback to top-scored option

        catalog_id = best.get("catalog_id")
        store_id = best.get("store_id")

        if not catalog_id or not store_id:
            continue

        picks.append({
            "catalog_id": catalog_id,
            "product_name": best.get("product_name", ""),
            "product_url": best.get("product_url", ""),
            "store_id": store_id,
            "expected_price": best.get("price", 0),
            "sku": best.get("sku", ""),
            "item_name": item.get("input_name", ""),  # Generic name for cross-store
        })

    return picks


# ────────────────────────────────────────────
# Verification result application
# ────────────────────────────────────────────

def apply_verification(batch_result: dict, verification: dict[int, VerifyResult],
                       conn=None) -> dict:
    """
    Apply verification results back to the batch curation result.

    Updates prices, marks OOS items, and attaches cross-store highlights.
    Returns a summary dict with stats.

    This is called by app.py after verify_picks() returns.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    stats = {
        "verified": 0,
        "price_changed": 0,
        "oos": 0,
        "substituted": 0,
        "cross_store_found": 0,
        "unverified": 0,
    }

    try:
        for item in batch_result.get("items", []):
            if item.get("status") not in ("matched", "catalog_only"):
                continue

            # Find the catalog_id for this item's recommended product
            options = item.get("options", [])
            if not options:
                continue

            rec_store = item.get("recommended_store_id")
            best = None
            for opt in options:
                if opt.get("store_id") == rec_store:
                    best = opt
                    break
            if not best:
                best = options[0]

            catalog_id = best.get("catalog_id")
            if not catalog_id or catalog_id not in verification:
                stats["unverified"] += 1
                item["verification_status"] = "unverified"
                continue

            vr = verification[catalog_id]

            if vr.error:
                stats["unverified"] += 1
                item["verification_status"] = "unverified"
                continue

            if vr.verified and vr.is_available:
                stats["verified"] += 1
                item["verification_status"] = "verified"

                # Update price if changed
                if vr.price_changed and vr.live_price is not None:
                    stats["price_changed"] += 1
                    item["verification_status"] = "price_updated"
                    item["live_price"] = vr.live_price
                    item["price_diff"] = vr.price_diff
                    # Update the recommended_price
                    item["recommended_price"] = vr.live_price
                    # Also update in options
                    best["price"] = vr.live_price

                # Attach cross-store highlight
                if vr.cross_store:
                    stats["cross_store_found"] += 1
                    item["cross_store_highlight"] = vr.cross_store

            elif vr.verified and not vr.is_available:
                stats["oos"] += 1
                item["verification_status"] = "out_of_stock"

                # Try to substitute with next alternative
                substituted = _try_substitute(item, catalog_id, conn)
                if substituted:
                    stats["substituted"] += 1
                    item["verification_status"] = "substituted"

    finally:
        if close_conn:
            conn.close()

    return stats


def _try_substitute(item: dict, oos_catalog_id: int, conn) -> bool:
    """
    When a product is OOS, try to substitute with the next-ranked alternative.

    Walks the item's options list (already ranked by score) and picks the
    first alternative that isn't the OOS product.

    Returns True if a substitution was made.
    """
    options = item.get("options", [])
    if len(options) < 2:
        return False

    for opt in options[1:]:  # Skip the first (OOS) option
        alt_catalog_id = opt.get("catalog_id")
        if not alt_catalog_id or alt_catalog_id == oos_catalog_id:
            continue

        # Check if this alternative is still marked available in catalog
        row = conn.execute(
            "SELECT is_available, price FROM product_catalog WHERE id = ?",
            (alt_catalog_id,)
        ).fetchone()

        if row and row["is_available"]:
            # Substitute!
            item["recommended_store_id"] = opt.get("store_id")
            item["recommended_store"] = opt.get("store_name")
            item["recommended_price"] = opt.get("price", row["price"])
            item["recommended_product"] = opt.get("product_name")
            item["substitution_note"] = (
                f"Original pick was out of stock. "
                f"Switched to {opt.get('product_name', 'alternative')} "
                f"at {opt.get('store_name', 'store')}"
            )
            return True

    return False


# ────────────────────────────────────────────
# Utility: format verification for chat message
# ────────────────────────────────────────────

def format_verification_summary(stats: dict, batch_result: dict) -> str:
    """
    Build a user-facing summary of verification results.
    Used by app.py to show a status message after verification.
    """
    parts = []

    if stats["verified"] > 0:
        parts.append(f"✓ {stats['verified']} items verified")

    if stats["price_changed"] > 0:
        # Find the items with price changes
        price_notes = []
        for item in batch_result.get("items", []):
            if item.get("verification_status") == "price_updated":
                diff = item.get("price_diff", 0)
                direction = "up" if diff > 0 else "down"
                price_notes.append(
                    f"{item.get('recommended_product', item.get('input_name', '?'))}: "
                    f"AED {abs(diff):.2f} {direction}"
                )
        if price_notes:
            parts.append(f"⚡ {stats['price_changed']} price change(s): " + ", ".join(price_notes))

    if stats["oos"] > 0:
        oos_notes = []
        for item in batch_result.get("items", []):
            vs = item.get("verification_status")
            if vs == "out_of_stock":
                oos_notes.append(item.get("input_name", "?") + " (out of stock)")
            elif vs == "substituted":
                note = item.get("substitution_note", "auto-substituted")
                oos_notes.append(note)
        if oos_notes:
            parts.append("⚠ " + "; ".join(oos_notes))

    if stats["cross_store_found"] > 0:
        cross_notes = []
        for item in batch_result.get("items", []):
            cross = item.get("cross_store_highlight")
            if cross:
                cross_notes.append(
                    f"{item.get('input_name', '?')}: "
                    f"AED {cross['savings']:.0f} cheaper at {cross['store_name']}"
                )
        if cross_notes:
            parts.append("💡 " + "; ".join(cross_notes))

    if stats["unverified"] > 0:
        parts.append(f"({stats['unverified']} unverified — using catalog data)")

    return " | ".join(parts) if parts else "All items checked."


# ────────────────────────────────────────────
# Persist verification results to list_items
# ────────────────────────────────────────────

def persist_verification_to_list(list_id: int, batch_result: dict,
                                  verification: dict[int, VerifyResult],
                                  conn=None):
    """
    Update list_items in the database with verification results.

    This runs BEFORE send_list_data() so the frontend sees verified prices.
    Updates:
      - estimated_price when price changed
      - status + failure_context when OOS
      - catalog_id + store_id + estimated_price when substituted

    Args:
        list_id: the shopping list ID
        batch_result: dict from curate_basket(), with "items" list
            (after apply_verification has been called — items have verification_status)
        verification: dict of catalog_id → VerifyResult
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    try:
        for item in batch_result.get("items", []):
            vs = item.get("verification_status")
            if not vs:
                continue

            # Find the catalog_id for this item's recommended product
            options = item.get("options", [])
            if not options:
                continue

            rec_store = item.get("recommended_store_id")
            best = None
            for opt in options:
                if opt.get("store_id") == rec_store:
                    best = opt
                    break
            if not best:
                best = options[0]

            catalog_id = best.get("catalog_id")
            if not catalog_id:
                continue

            # Find the list_item by catalog_id (most recently added)
            row = conn.execute(
                "SELECT id FROM list_items WHERE list_id = ? AND catalog_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (list_id, catalog_id),
            ).fetchone()

            if not row:
                # Try matching by item name if catalog_id doesn't match
                # (happens when substituted — catalog_id changed)
                row = conn.execute(
                    "SELECT id FROM list_items WHERE list_id = ? AND LOWER(item_name_text) = LOWER(?) "
                    "ORDER BY id DESC LIMIT 1",
                    (list_id, item.get("input_name", "")),
                ).fetchone()

            if not row:
                continue

            li_id = row["id"]

            if vs == "price_updated":
                vr = verification.get(catalog_id)
                if vr and vr.live_price is not None:
                    conn.execute(
                        "UPDATE list_items SET estimated_price = ? WHERE id = ?",
                        (vr.live_price, li_id),
                    )

            elif vs == "out_of_stock":
                conn.execute(
                    "UPDATE list_items SET status = 'unresolved', "
                    "failure_context = ? WHERE id = ?",
                    (json.dumps({
                        "reason": "out_of_stock",
                        "reason_human": f"'{item.get('input_name', '?')}' went out of stock since last check",
                    }), li_id),
                )

            elif vs == "substituted":
                # Update to the substitute product
                sub_note = item.get("substitution_note", "")
                new_price = item.get("recommended_price")
                new_store = item.get("recommended_store_id")
                new_product = item.get("recommended_product", "")

                # Find the substitute's catalog_id
                new_catalog_id = None
                for opt in options:
                    if opt.get("product_name") == new_product and opt.get("store_id") == new_store:
                        new_catalog_id = opt.get("catalog_id")
                        break

                updates = ["item_name_text = ?", "store_id = ?", "estimated_price = ?"]
                params = [new_product or item.get("input_name"), new_store, new_price]

                if new_catalog_id:
                    updates.append("catalog_id = ?")
                    params.append(new_catalog_id)

                updates.append("failure_context = ?")
                params.append(json.dumps({
                    "reason": "substituted",
                    "reason_human": sub_note,
                }))

                params.append(li_id)
                conn.execute(
                    f"UPDATE list_items SET {', '.join(updates)} WHERE id = ?",
                    params,
                )

        conn.commit()

    finally:
        if close_conn:
            conn.close()
