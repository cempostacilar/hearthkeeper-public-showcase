"""
AutoRestock - Kibsons RSC Scraper (v2)

Scrapes the FULL Kibsons grocery inventory via React Server Component payloads.
Instead of visiting individual product pages (3 sec each), we fetch structured
data from RSC endpoints — 100 products per request with ~108 fields each.

Architecture:
  1. Launch Edge with HearthKeeper profile (Cloudflare requires real browser)
  2. Navigate to kibsons.com to establish session
  3. For each category/family: fetch RSC payload, parse products, paginate
  4. Map RSC fields to our product_catalog schema
  5. Batch upsert via catalog_store_products()

Performance:
  Old (DOM scraper): ~1,200 products in 60 minutes
  New (RSC):         ~10,000 products in 5-8 minutes

Usage:
  python3 scrape_kibsons_rsc.py                    # full inventory
  python3 scrape_kibsons_rsc.py --test              # test: dairy/eggs only
  python3 scrape_kibsons_rsc.py --category fruits   # one category
  python3 scrape_kibsons_rsc.py --family dairy/eggs  # one family
"""

import asyncio
import json
import re
import sys
import time
import argparse

from browser_common import launch_edge, check_browser_health, relaunch_browser, safe_close, ensure_logged_in
from product_knowledge import catalog_store_products
from db_setup import DB_PATH

import sqlite3

KIBSONS_STORE_ID = 1

# ────────────────────────────────────────────
# Full Kibsons Grocery Category Tree
# ────────────────────────────────────────────
# Mapped from the live site. Each category has families (sub-categories).
# URL pattern: /en/{category}/{family}

GROCERY_CATEGORIES = {
    "fruits": [
        "apples-pears", "avocados", "bananas", "berries", "citrus",
        "coconuts", "dates", "exotics", "grapes", "mangoes", "melons",
        "papayas", "pineapples", "pomegranates", "stone-fruits",
    ],
    "vegetables": [
        "artichokes", "beans-peas-corn", "broccoli-cauliflower", "cabbage",
        "cucumbers", "eggplants-marrows", "garlic-ginger", "herbs-sprouts",
        "indian-vegetables", "leafy-greens", "mixed-vegetables", "mushrooms",
        "onions", "peppers", "potatoes", "pumpkin-squash", "root-vegetables",
        "stalks", "tomatoes",
    ],
    "meat": [
        "beef-veal", "chicken-poultry", "deli-meat", "dhabayeh-lamb-goat",
        "freshly-prepared", "jerky-biltong", "lamb-goat", "marinated-meat",
        "meat-packs", "ostrich-venison", "sous-vide",
    ],
    "seafood": [
        "breaded-seafood", "canned-seafood", "caviar", "fish",
        "fish-patties", "fresh-catch", "prawns-shellfish",
        "smoked-seafood", "squid-octopus",
    ],
    "dairy": [
        "butter-ghee", "cheese", "cream", "eggs", "fresh-milk",
        "ice-cream-frozen-desserts", "laban-probiotic-drinks",
        "long-life-milk", "plant-based", "yoghurt",
    ],
    "bakery": [
        "arabic-bread", "bagels-pretzels", "bread-loaves",
        "breakfast-bakery", "buns-rolls", "cakes-cupcakes",
        "cookies-doughnuts", "crackers-biscuits", "energy-snacks",
        "indian-breads", "wraps",
    ],
    "pantry": [
        "breakfast-cereals", "cans-jars-packets", "chocolates-sweets",
        "condiments-dressings", "crackers-biscuits", "crisps-snacks",
        "deli-dips", "energy-snacks", "flours-mixes", "honey-jams-spreads",
        "instant-fix", "nuts-seeds-dried-fruits", "oil-vinegar",
        "pasta-noodles", "pulses-seeds", "rice-grains",
        "sauces-ingredients", "spices-seasonings", "sugar-baking",
        "superfoods", "world-foods",
    ],
    "drinks": [
        "carbonated-drinks", "chocolate-malt-drinks", "coconut-drinks",
        "coffee", "cordials", "health-drinks", "juice",
        "non-alcoholic-drinks", "sports-energy-drinks", "tea", "water",
    ],
    "frozen": [
        "frozen-appetizers", "frozen-bakery", "frozen-chicken-meat",
        "frozen-chips-potatoes-rice", "frozen-dairy", "frozen-fruit",
        "frozen-pasta", "frozen-pizza", "frozen-ready-meals",
        "frozen-seafood", "frozen-vegetables", "ice-cream-frozen-desserts",
        "vegetarian-vegan-food",
    ],
}


# ────────────────────────────────────────────
# RSC Payload Parsing
# ────────────────────────────────────────────

def parse_rsc_products(rsc_text: str) -> list[dict]:
    """
    Extract product objects from a Next.js RSC streaming response.

    RSC format is line-based: each line starts with an index, colon, then data.
    Product data appears as JSON objects with 'stockCode' and 'stockDesc' fields
    embedded in the RSC component tree.

    Strategy: regex-find all stockCode occurrences, then extract the enclosing
    JSON object via balanced brace matching.
    """
    products = []
    seen_skus = set()

    for m in re.finditer(r'"stockCode":"([^"]+)"', rsc_text):
        sku = m.group(1)
        if sku in seen_skus:
            continue

        # Walk backward from match to find the opening brace of this object
        idx = m.start()
        depth = 0
        start = idx
        for i in range(idx, -1, -1):
            if rsc_text[i] == '}':
                depth += 1
            elif rsc_text[i] == '{':
                if depth == 0:
                    start = i
                    break
                depth -= 1

        # Walk forward to find the matching closing brace
        depth = 0
        end = start
        for i in range(start, min(start + 15000, len(rsc_text))):
            if rsc_text[i] == '{':
                depth += 1
            elif rsc_text[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        try:
            obj = json.loads(rsc_text[start:end])
            if 'stockCode' in obj and 'stockDesc' in obj:
                seen_skus.add(sku)
                products.append(obj)
        except (json.JSONDecodeError, TypeError):
            continue

    return products


def map_rsc_to_product(rsc: dict, category: str, family: str) -> dict:
    """
    Map a Kibsons RSC product object to our product_catalog dict format.

    The dict keys match what catalog_store_products() expects.
    """
    name = rsc.get("stockDesc", "").strip()
    brand = rsc.get("brandDesc", "").strip()
    sku = rsc.get("stockCode", "")
    size = rsc.get("stockShortDetail", "").strip()
    origin = rsc.get("originDesc", "").strip()
    unit = rsc.get("stockUnits", "").strip()

    # Price: prefer promotion rate if > 0, else regular
    promo = rsc.get("stockPromotionRate") or 0
    regular = rsc.get("stockRate") or 0
    price = promo if promo > 0 else regular

    # Organic: from classification array (structured, no heuristics)
    classifications = rsc.get("classification") or []
    is_organic = any(
        c.get("desc", "").lower() == "organic"
        for c in classifications
        if isinstance(c, dict)
    )

    # Featured tags: Organic, Eco-Friendly, Ramadan, etc.
    featured = rsc.get("featured") or []
    featured_tags = [f.get("desc") for f in featured if isinstance(f, dict) and f.get("desc")]

    # Badges from classification
    badge_names = [c.get("desc") for c in classifications if isinstance(c, dict) and c.get("desc")]

    # Category hierarchy
    cat_label = category.replace("-", " ").title()
    fam_label = family.replace("-", " ").replace("&", "&").title()
    cat_hierarchy = f"{cat_label} > {fam_label}"

    # Product URL — reconstruct from category/family/slug
    # Kibsons URL pattern: /en/product/{category}/{family}/{stockDesc-slug}-{stockCode}
    slug = _make_slug(name, sku)
    product_url = f"https://www.kibsons.com/en/product/{category}/{family}/{slug}"

    # Images
    img = rsc.get("url") or rsc.get("stockImgLink") or ""

    # Build a rich product name: Brand + Name + Size (avoid duplicates)
    product_name = _build_product_name(name, brand, size)

    # Keywords (plain string on Kibsons)
    keywords = rsc.get("keywords", "")

    return {
        "name": product_name,
        "product_url": product_url,
        "brand": brand,
        "price": float(price),
        "size": size,
        "category": cat_hierarchy,
        "description": "",  # RSC doesn't include detail text
        "ingredients": "",
        "nutrition_info": "",
        "origin_country": origin,
        "image_url": img,
        "is_organic": is_organic,
        # New RSC fields
        "sku": sku,
        "unit": unit,
        "regular_price": float(regular),
        "sale_price": float(promo),
        "transport_route": rsc.get("transportRoute", ""),
        "popularity_score": float(rsc.get("pl_sku_popularity_score") or 0),
        "is_fresh": bool(rsc.get("pl_is_fresh")),
        "badges": json.dumps(badge_names) if badge_names else None,
        "featured_tags": json.dumps(featured_tags) if featured_tags else None,
        "search_keywords": keywords if keywords else None,
        "external_id": str(rsc.get("id", "")),
        "is_available": True,  # Kibsons RSC doesn't expose OOS; live_verify corrects this
    }


def _make_slug(name: str, sku: str) -> str:
    """Build a URL slug from product name + SKU, matching Kibsons pattern."""
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return f"{slug}-{sku.lower()}" if sku else slug


def _build_product_name(name: str, brand: str, size: str) -> str:
    """
    Build display name from components, avoiding duplicate words.
    e.g., name="Eggs", brand="Organicasa", size="6 Pack" → "Organicasa Eggs - 6 Pack"
    """
    parts = []

    # Add brand if not already in name
    if brand and brand.lower() not in name.lower():
        parts.append(brand)

    parts.append(name)

    # Add size if not already in name
    if size and size.lower() not in name.lower():
        parts.append(f"- {size}")

    return " ".join(parts)


# ────────────────────────────────────────────
# RSC Fetching via Browser
# ────────────────────────────────────────────

async def fetch_rsc_page(page, category: str, family: str, page_num: int = 1) -> str:
    """
    Fetch an RSC payload from within the browser context.

    Uses page.evaluate(fetch(...)) so the request inherits Cloudflare cookies.
    The _rsc query param triggers Next.js to return the RSC streaming format
    instead of full HTML. No state tree header needed.

    Returns raw RSC text, or empty string on failure.
    """
    base_path = f"/en/{category}/{family}"
    # Build query string: _rsc is always needed, page only for page 2+
    qs_parts = []
    if page_num > 1:
        qs_parts.append(f"page={page_num}")
    qs_parts.append("_rsc=1")
    fetch_url = f"{base_path}?{'&'.join(qs_parts)}"

    try:
        raw = await page.evaluate(f"""
            async () => {{
                try {{
                    const resp = await fetch('{fetch_url}', {{
                        headers: {{
                            'RSC': '1',
                            'Next-Url': '{base_path}'
                        }}
                    }});
                    if (!resp.ok) return '';
                    return await resp.text();
                }} catch (e) {{
                    return '';
                }}
            }}
        """)
        return raw or ""
    except Exception as e:
        print(f"    [ERROR] fetch failed for {fetch_url}: {e}")
        return ""


async def fetch_family_products(page, category: str, family: str) -> list[dict]:
    """
    Fetch ALL products for a category/family, handling pagination.

    Returns list of mapped product dicts ready for catalog_store_products().
    """
    all_products = []
    page_num = 1

    while True:
        rsc_text = await fetch_rsc_page(page, category, family, page_num)
        if not rsc_text:
            break

        rsc_products = parse_rsc_products(rsc_text)
        if not rsc_products:
            break

        mapped = [map_rsc_to_product(rsc, category, family) for rsc in rsc_products]
        all_products.extend(mapped)

        # If we got fewer than 100, that's the last page
        if len(rsc_products) < 100:
            break

        page_num += 1
        await asyncio.sleep(0.5)  # Polite delay

    return all_products


# ────────────────────────────────────────────
# Main Scraper
# ────────────────────────────────────────────

async def scrape_inventory(
    categories: dict[str, list[str]] | None = None,
    progress_callback=None,
):
    """
    Scrape Kibsons inventory via RSC.

    Args:
        categories: dict of category → family list. Defaults to full GROCERY_CATEGORIES.
        progress_callback: optional async fn(message) for progress updates.

    Returns:
        dict with stats: total, new, updated, families, errors, by_category
    """
    cats = categories or GROCERY_CATEGORIES

    stats = {
        "total": 0,
        "new": 0,
        "updated": 0,
        "families_scraped": 0,
        "families_empty": 0,
        "errors": [],
        "by_category": {},
        "duration_sec": 0,
    }

    start_time = time.time()

    async def log(msg):
        print(msg)
        if progress_callback:
            try:
                await progress_callback(msg)
            except Exception:
                pass

    await log("[RSC] Launching Edge browser...")
    p, context, page = await launch_edge()

    try:
        # Establish Cloudflare session
        await log("[RSC] Establishing Cloudflare session...")
        await page.goto("https://www.kibsons.com/en/", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Check we're on the site
        healthy = await check_browser_health(page)
        if not healthy:
            await log("[RSC] Browser unhealthy after launch, retrying...")
            p, context, page = await relaunch_browser(p, context)
            await page.goto("https://www.kibsons.com/en/", wait_until="domcontentloaded")
            await asyncio.sleep(3)

        # Auto-login if session expired
        logged_in = await ensure_logged_in(page, "Kibsons")
        if not logged_in:
            await log("[RSC] WARNING: Could not log into Kibsons. Continuing anyway (RSC may still work).")
        else:
            # Re-navigate if login changed the URL
            if "kibsons.com/en" not in page.url or "login" in page.url.lower():
                await page.goto("https://www.kibsons.com/en/", wait_until="domcontentloaded")
                await asyncio.sleep(3)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Track existing product URLs to detect removed products later
        existing_urls = set()
        for row in conn.execute(
            "SELECT product_url FROM product_catalog WHERE store_id = ? AND product_url IS NOT NULL",
            (KIBSONS_STORE_ID,)
        ):
            existing_urls.add(row["product_url"])

        seen_urls = set()
        total_families = sum(len(fams) for fams in cats.values())
        family_idx = 0

        for category, families in cats.items():
            cat_count = 0

            for family in families:
                family_idx += 1
                label = f"[{family_idx}/{total_families}] {category}/{family}"

                try:
                    products = await fetch_family_products(page, category, family)
                except Exception as e:
                    err = f"{label}: {e}"
                    stats["errors"].append(err)
                    await log(f"  {label}: ERROR - {e}")
                    continue

                if not products:
                    stats["families_empty"] += 1
                    await log(f"  {label}: 0 products (empty/404)")
                    continue

                # Track URLs we've seen
                for prod in products:
                    if prod.get("product_url"):
                        seen_urls.add(prod["product_url"])

                # Count new vs updated
                before_count = conn.execute(
                    "SELECT COUNT(*) FROM product_catalog WHERE store_id = ?",
                    (KIBSONS_STORE_ID,)
                ).fetchone()[0]

                catalog_store_products(KIBSONS_STORE_ID, products, conn=conn)

                after_count = conn.execute(
                    "SELECT COUNT(*) FROM product_catalog WHERE store_id = ?",
                    (KIBSONS_STORE_ID,)
                ).fetchone()[0]

                new_this_family = after_count - before_count
                updated_this_family = len(products) - new_this_family

                stats["total"] += len(products)
                stats["new"] += new_this_family
                stats["updated"] += updated_this_family
                stats["families_scraped"] += 1
                cat_count += len(products)

                await log(f"  {label}: {len(products)} products ({new_this_family} new, {updated_this_family} updated)")

                # Brief pause between families
                await asyncio.sleep(0.8)

            stats["by_category"][category] = cat_count

        # Mark products not seen in this scrape as potentially unavailable
        # Only do this for FULL inventory scrapes (all categories), not partial.
        is_full_scrape = (categories is None)  # None means "use default = full"
        if is_full_scrape:
            # GUARD: never mark "not seen" products unavailable when the scrape
            # was clearly incomplete (blocked / timed out / errored partway).
            # Otherwise a single bad run wholesale-zeros the store (this is how
            # Carrefour hit 0 available). Only sweep when we re-saw enough of the
            # existing catalog to trust its absences.
            from product_knowledge import availability_sweep_safe
            seen_count = len(seen_urls)
            existing_count = len(existing_urls)
            if not availability_sweep_safe(seen_count, existing_count):
                await log(f"\n[RSC] Availability sweep SKIPPED — saw only "
                          f"{seen_count}/{existing_count} products; scrape likely "
                          f"incomplete, NOT zeroing the store")
                try:
                    from app_logging import log_event
                    log_event("scrape_availability_sweep_skipped", source="scraper",
                              detail=f"store={KIBSONS_STORE_ID} seen={seen_count} "
                                     f"existing={existing_count}", level="warning")
                except Exception:
                    pass
            else:
                disappeared = list(existing_urls - seen_urls)
                if disappeared:
                    # Batch under SQLite's 999-variable limit -- a single IN() over
                    # every unseen URL threw "too many SQL variables" and aborted the
                    # nightly refresh, silently staling the catalog.
                    BATCH = 900
                    for i in range(0, len(disappeared), BATCH):
                        chunk = disappeared[i:i + BATCH]
                        placeholders = ",".join(["?"] * len(chunk))
                        conn.execute(
                            f"UPDATE product_catalog SET is_available = 0 WHERE store_id = ? AND product_url IN ({placeholders})",
                            [KIBSONS_STORE_ID] + chunk,
                        )
                    conn.commit()
                    await log(f"\n[RSC] Marked {len(disappeared)} products as unavailable")
        else:
            await log(f"\n[RSC] Partial scrape — skipping availability check")

        # Post-scrape hook: tell Book Keeper to re-index alternatives
        # Contract: Gatherer never finishes without the index being refreshed.
        await log(f"\n[RSC] Re-indexing item alternatives against fresh catalog...")
        from rescore_alternatives import rescore_store
        rescore_result = rescore_store(store_id=KIBSONS_STORE_ID, conn=conn, verbose=False)
        await log(f"[RSC] Rescore done: {rescore_result['alternatives_scored']} alternatives "
                  f"({rescore_result['preferred']} preferred/acceptable)")

        conn.close()

    finally:
        await safe_close(p, context)

    stats["duration_sec"] = round(time.time() - start_time, 1)
    return stats


def print_stats(stats: dict):
    """Pretty-print scrape results."""
    print("\n" + "=" * 60)
    print("  KIBSONS RSC SCRAPE COMPLETE")
    print("=" * 60)
    print(f"  Duration:     {stats['duration_sec']}s")
    print(f"  Total:        {stats['total']} products")
    print(f"  New:          {stats['new']}")
    print(f"  Updated:      {stats['updated']}")
    print(f"  Families:     {stats['families_scraped']} scraped, {stats['families_empty']} empty")
    print()
    print("  By category:")
    for cat, count in sorted(stats["by_category"].items(), key=lambda x: -x[1]):
        print(f"    {cat:20s} {count:>5}")
    if stats["errors"]:
        print(f"\n  Errors ({len(stats['errors'])}):")
        for err in stats["errors"][:10]:
            print(f"    - {err}")
    print("=" * 60)


# ────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Kibsons RSC Scraper")
    parser.add_argument("--test", action="store_true", help="Test mode: scrape dairy/eggs only")
    parser.add_argument("--category", type=str, help="Scrape one category (e.g., 'fruits')")
    parser.add_argument("--family", type=str, help="Scrape one family (e.g., 'dairy/eggs')")
    args = parser.parse_args()

    if args.test:
        cats = {"dairy": ["eggs"]}
    elif args.family:
        parts = args.family.split("/")
        if len(parts) != 2:
            print("--family must be category/family (e.g., dairy/eggs)")
            sys.exit(1)
        cats = {parts[0]: [parts[1]]}
    elif args.category:
        if args.category not in GROCERY_CATEGORIES:
            print(f"Unknown category: {args.category}")
            print(f"Available: {', '.join(GROCERY_CATEGORIES.keys())}")
            sys.exit(1)
        cats = {args.category: GROCERY_CATEGORIES[args.category]}
    else:
        cats = None  # Full inventory

    stats = await scrape_inventory(categories=cats)
    print_stats(stats)


if __name__ == "__main__":
    asyncio.run(main())
