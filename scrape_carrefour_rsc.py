"""
AutoRestock - Carrefour UAE RSC Scraper (v2)

Scrapes the FULL Carrefour UAE grocery inventory via React Server Component payloads.
Instead of visiting individual product pages (2s each), we fetch structured data
from RSC endpoints — 100 products per request with ~41 fields each.

Architecture:
  1. Launch Edge with HearthKeeper profile (Cloudflare/Akamai requires real browser)
  2. Navigate to carrefouruae.com to establish session
  3. For each category: fetch RSC payload, parse products via balanced-brace on "ean"
  4. Map RSC fields to our product_catalog schema
  5. Batch upsert via catalog_store_products()

Performance:
  Old (DOM scraper): ~767 products in 60+ minutes (visiting each page)
  New (RSC):         ~14,000 products in 10-15 minutes

Usage:
  python3 scrape_carrefour_rsc.py                    # full food inventory
  python3 scrape_carrefour_rsc.py --test              # test: eggs category only
  python3 scrape_carrefour_rsc.py --category F1600000 # one top-level category
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

CARREFOUR_STORE_ID = 2

# ────────────────────────────────────────────
# Carrefour UAE Category Tree (Full Inventory)
# ────────────────────────────────────────────
# Discovered from live site investigation (Feb 2026).
# URL pattern: /mafuae/en/c/{CATEGORY_CODE}
#
# Structure: Top-level categories have subcategories (Level 2/3).
# We scrape at top-level — products self-report their full category
# hierarchy via productCategoriesHearchi field.
# Dedup by EAN across categories handles any overlap.
#
# EXCLUDED: Electronics (NF4000000), Smartphones (NF1200000),
#   Fashion (NF5000000), Toys (NF1400000), Automotive (NF2300000),
#   Kiosk (NF9000000)

# ── Food categories ──
FOOD_CATEGORIES = {
    "Fresh Food": {
        "F1600000": "Fresh Food",           # ~2,860 products
    },
    "Fruits & Vegetables": {
        "F11600000": "Fruits & Vegetables",  # ~530 products
    },
    "Beverages": {
        "F1500000": "Beverages",             # ~1,915 products
    },
    "Food Cupboard": {
        "F1700000": "Food Cupboard",         # ~7,639 products
    },
    "Frozen": {
        "F6000000": "Frozen Food",           # ~1,095 products
    },
    "Bakery": {
        "F1610000": "Bakery",                # ~604 products
    },
    "Health & Organic": {
        "F1200000": "Vegan & Health",        # ~1,220 products
    },
}

# ── Non-food household categories ──
HOUSEHOLD_CATEGORIES = {
    "Cleaning & Household": {
        "NF3000000": "Cleaning & Household",  # laundry, dish, cleaning supplies
    },
    "Beauty & Personal Care": {
        "NF2000000": "Beauty & Personal Care",  # cotton pads, skincare, toiletries
    },
    "Baby Products": {
        "F1000000": "Baby Products",          # diapers, wipes, baby food
    },
    "Pet Supplies": {
        "F1100000": "Pet Supplies",           # pet food, accessories
    },
    "Health & Fitness": {
        "NF7000000": "Health & Fitness",      # vitamins, supplements, first aid
    },
    "Home & Garden": {
        "NF8000000": "Home & Garden",         # cookware, storage, garden
    },
    "Stationery & School Supplies": {
        "NF1300000": "Stationery & School Supplies",
    },
}

# Combined: all categories to scrape (food + household)
ALL_SCRAPE_CATEGORIES = {**FOOD_CATEGORIES, **HOUSEHOLD_CATEGORIES}

# Flat lookup for --category flag
ALL_CATEGORIES = {}
for group_name, cats in ALL_SCRAPE_CATEGORIES.items():
    for code, name in cats.items():
        ALL_CATEGORIES[code] = f"{group_name} > {name}"


# ────────────────────────────────────────────
# RSC Payload Parsing
# ────────────────────────────────────────────

def parse_rsc_products(rsc_text: str) -> list[dict]:
    """
    Extract product objects from a Carrefour Next.js RSC streaming response.

    Strategy: regex-find all "ean" occurrences (Carrefour's product identifier),
    then extract the enclosing JSON object via balanced brace matching.
    Products must have ean, name, and price fields.
    """
    products = []
    seen_eans = set()

    for m in re.finditer(r'"ean"', rsc_text):
        idx = m.start()

        # Walk backward from match to find the opening brace of this object
        depth = 0
        start = idx
        for i in range(idx, max(0, idx - 5000), -1):
            ch = rsc_text[i]
            if ch in "}]":
                depth += 1
            elif ch in "{[":
                if depth == 0:
                    start = i
                    break
                depth -= 1

        # Walk forward to find the matching closing brace
        depth = 0
        end = start
        for i in range(start, min(start + 15000, len(rsc_text))):
            ch = rsc_text[i]
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        try:
            obj = json.loads(rsc_text[start:end])
            if isinstance(obj, dict) and obj.get("ean") and obj.get("name") and obj.get("price"):
                ean = obj["ean"]
                if ean not in seen_eans:
                    seen_eans.add(ean)
                    products.append(obj)
        except (json.JSONDecodeError, TypeError):
            continue

    return products


def map_rsc_to_product(rsc: dict, group_name: str, cat_name: str) -> dict:
    """
    Map a Carrefour RSC product object to our product_catalog dict format.

    Carrefour RSC products have ~41 fields with nested objects:
      brand: {id, name}
      price: {currency, price, formattedValue}
      category: [{id, level, name, url}, ...]  (3-level hierarchy)
      availability: {isAvailable, max}
      rating: {value, count}
      stock: {stockLevelStatus}
    """
    name = (rsc.get("name") or "").strip()
    ean = rsc.get("ean") or ""
    product_id = rsc.get("id") or ""

    # Brand: nested object {id, name}
    brand_obj = rsc.get("brand") or {}
    brand = (brand_obj.get("name") or "").strip() if isinstance(brand_obj, dict) else ""

    # Price: nested object {currency, price, formattedValue}
    price_obj = rsc.get("price") or {}
    if isinstance(price_obj, dict):
        price = price_obj.get("price") or 0
        regular_price = price_obj.get("originalPrice") or price
    else:
        price = 0
        regular_price = 0

    # Check for sale price (if there are offers/promotions)
    sale_price = 0
    offers = rsc.get("offers") or []
    if offers and isinstance(offers, list):
        sale_price = price  # current price IS the sale price when offers exist
        price = regular_price  # use original as display price

    # Size
    size = (rsc.get("size") or "").strip()

    # Origin country
    origin = (rsc.get("productOrigin") or "").strip()

    # Category hierarchy from product data
    categories = rsc.get("category") or []
    if isinstance(categories, list) and categories:
        cat_names = [c.get("name", "") for c in categories if isinstance(c, dict) and c.get("name")]
        cat_hierarchy = " > ".join(cat_names) if cat_names else f"{group_name} > {cat_name}"
    else:
        cat_hierarchy = f"{group_name} > {cat_name}"

    # Also use productCategoriesHearchi if available (more reliable)
    hearchi = rsc.get("productCategoriesHearchi", "")
    if hearchi:
        cat_hierarchy = hearchi.replace("/", " > ")

    # Product URL: links.productUrl.href
    product_url = ""
    links = rsc.get("links") or {}
    if isinstance(links, dict):
        raw_url = links.get("productUrl") or {}
        if isinstance(raw_url, dict):
            product_url = raw_url.get("href", "")
        elif isinstance(raw_url, str):
            product_url = raw_url
    if product_url and not product_url.startswith("http"):
        product_url = f"https://www.carrefouruae.com{product_url}"

    # Image: prefer links.defaultImages[0] (full-size), fallback to links.images[0].href
    image_url = ""
    if isinstance(links, dict):
        default_images = links.get("defaultImages") or []
        if isinstance(default_images, list) and default_images:
            image_url = default_images[0] if isinstance(default_images[0], str) else ""
        if not image_url:
            images_list = links.get("images") or []
            if isinstance(images_list, list) and images_list:
                first_img = images_list[0]
                if isinstance(first_img, dict):
                    image_url = first_img.get("href") or ""

    # Organic detection: check name, badges, and type labels
    is_organic = False
    if "organic" in name.lower():
        is_organic = True
    promo_badges = rsc.get("promoBadges") or []
    if isinstance(promo_badges, list):
        for badge in promo_badges:
            if isinstance(badge, dict) and "organic" in (badge.get("name") or "").lower():
                is_organic = True

    # Fresh detection
    food_type = rsc.get("foodType", "")
    is_fresh = food_type.upper() == "FRESH" if food_type else False

    # Unit
    unit_obj = rsc.get("unit") or {}
    unit = ""
    if isinstance(unit_obj, dict):
        unit = unit_obj.get("name") or unit_obj.get("unit") or ""
    elif isinstance(unit_obj, str):
        unit = unit_obj

    # Rating
    rating_obj = rsc.get("rating") or {}
    rating_value = 0
    rating_count = 0
    if isinstance(rating_obj, dict):
        rating_value = rating_obj.get("value") or 0
        rating_count = rating_obj.get("count") or 0

    # Badges from promoBadges
    badge_names = []
    if isinstance(promo_badges, list):
        for badge in promo_badges:
            if isinstance(badge, dict) and badge.get("name"):
                badge_names.append(badge["name"])

    # Availability
    avail_obj = rsc.get("availability") or {}
    is_available = True
    if isinstance(avail_obj, dict):
        is_available = avail_obj.get("isAvailable", True)

    # Featured tags (type, foodType, supplier)
    featured_tags = []
    if rsc.get("type"):
        featured_tags.append(rsc["type"])
    if food_type:
        featured_tags.append(food_type)
    supplier = rsc.get("supplier", "")
    if supplier:
        featured_tags.append(f"Supplier: {supplier}")

    # Transport route — Carrefour doesn't have this directly,
    # but we can infer from origin
    transport_route = ""

    # Popularity: Carrefour has purchaseHistory.purchased and rating
    popularity_score = 0
    purchase_history = rsc.get("purchaseHistory") or {}
    if isinstance(purchase_history, dict) and purchase_history.get("purchased"):
        popularity_score += 50
    popularity_score += min(50, rating_count * 5)  # Cap at 50 from reviews

    return {
        "name": name,
        "product_url": product_url,
        "brand": brand,
        "price": float(price),
        "size": size,
        "category": cat_hierarchy,
        "description": "",  # RSC doesn't include detail text
        "ingredients": "",
        "nutrition_info": "",
        "origin_country": origin,
        "image_url": image_url,
        "is_organic": is_organic,
        # RSC-enriched fields
        "sku": ean,  # EAN barcode as SKU for Carrefour
        "unit": unit,
        "regular_price": float(regular_price),
        "sale_price": float(sale_price),
        "transport_route": transport_route,
        "popularity_score": float(popularity_score),
        "is_fresh": is_fresh,
        "badges": json.dumps(badge_names) if badge_names else None,
        "featured_tags": json.dumps(featured_tags) if featured_tags else None,
        "search_keywords": None,  # Carrefour doesn't expose keywords
        "external_id": str(product_id),
        "is_available": is_available,
    }


# ────────────────────────────────────────────
# RSC Fetching via Browser
# ────────────────────────────────────────────

FETCH_RSC_JS = """
async (args) => {
    const catCode = args[0];
    const pageNum = args[1];
    const pageSize = args[2];
    const path = '/mafuae/en/c/' + catCode;
    const url = path + '?currentPage=' + pageNum + '&pageSize=' + pageSize + '&sortBy=relevance&_rsc=1';
    try {
        const resp = await fetch(url, {
            headers: {
                'RSC': '1',
                'Next-Url': path + '?currentPage=' + pageNum + '&pageSize=' + pageSize + '&sortBy=relevance'
            }
        });
        if (!resp.ok) return { status: resp.status, text: '' };
        const text = await resp.text();
        // Extract totalProducts from the response
        const m = text.match(/"totalProducts"\\s*:\\s*(\\d+)/);
        const total = m ? parseInt(m[1]) : -1;
        return { status: resp.status, text: text, totalProducts: total };
    } catch (e) {
        return { status: 0, text: '', error: e.message };
    }
}
"""


async def fetch_category_page(page, cat_code: str, page_num: int = 0,
                               page_size: int = 100) -> tuple[str, int]:
    """
    Fetch one page of RSC data for a category.

    Uses page.evaluate(fetch(...)) so the request inherits WAF cookies.
    Returns (rsc_text, total_products).
    """
    try:
        result = await page.evaluate(FETCH_RSC_JS, [cat_code, page_num, page_size])
        if result.get("error"):
            print(f"    [ERROR] fetch failed for {cat_code} page {page_num}: {result['error']}")
            return "", -1
        if result["status"] != 200:
            return "", -1
        return result.get("text", ""), result.get("totalProducts", -1)
    except Exception as e:
        print(f"    [ERROR] evaluate failed for {cat_code} page {page_num}: {e}")
        return "", -1


async def fetch_category_products(page, cat_code: str, group_name: str,
                                   cat_name: str) -> list[dict]:
    """
    Fetch ALL products for a category, handling pagination.

    Carrefour pagination: currentPage (0-indexed), pageSize (max 100).
    Returns list of mapped product dicts ready for catalog_store_products().
    """
    all_products = []
    seen_eans = set()
    page_num = 0

    while True:
        rsc_text, total = await fetch_category_page(page, cat_code, page_num)
        if not rsc_text:
            break

        rsc_products = parse_rsc_products(rsc_text)
        if not rsc_products:
            break

        # Dedup across pages
        new_products = []
        for rsc in rsc_products:
            ean = rsc.get("ean", "")
            if ean and ean not in seen_eans:
                seen_eans.add(ean)
                new_products.append(map_rsc_to_product(rsc, group_name, cat_name))

        all_products.extend(new_products)

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
    categories: dict[str, dict[str, str]] | None = None,
    progress_callback=None,
):
    """
    Scrape Carrefour UAE inventory via RSC.

    Args:
        categories: dict of group → {code: name}. Defaults to ALL_SCRAPE_CATEGORIES.
        progress_callback: optional async fn(message) for progress updates.

    Returns:
        dict with stats: total, new, updated, categories, errors, by_group
    """
    cats = categories or ALL_SCRAPE_CATEGORIES

    stats = {
        "total": 0,
        "new": 0,
        "updated": 0,
        "categories_scraped": 0,
        "categories_empty": 0,
        "errors": [],
        "by_group": {},
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
        # Establish WAF session
        await log("[RSC] Establishing Carrefour session...")
        await page.goto("https://www.carrefouruae.com/mafuae/en/",
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        # Check we're on the site
        healthy = await check_browser_health(page)
        if not healthy:
            await log("[RSC] Browser unhealthy after launch, retrying...")
            p, context, page = await relaunch_browser(p, context)
            await page.goto("https://www.carrefouruae.com/mafuae/en/",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)

        # Auto-login if session expired
        logged_in = await ensure_logged_in(page, "Carrefour UAE")
        if not logged_in:
            await log("[RSC] WARNING: Could not log into Carrefour. Continuing anyway (RSC may still work).")
        else:
            # Re-navigate if login changed the URL
            if "carrefouruae.com/mafuae/en" not in page.url:
                await page.goto("https://www.carrefouruae.com/mafuae/en/",
                                wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Track existing product URLs to detect removed products later
        existing_urls = set()
        for row in conn.execute(
            "SELECT product_url FROM product_catalog WHERE store_id = ? AND product_url IS NOT NULL",
            (CARREFOUR_STORE_ID,)
        ):
            existing_urls.add(row["product_url"])

        seen_urls = set()
        total_cats = sum(len(subcats) for subcats in cats.values())
        cat_idx = 0

        for group_name, subcats in cats.items():
            group_count = 0

            for cat_code, cat_name in subcats.items():
                cat_idx += 1
                label = f"[{cat_idx}/{total_cats}] {group_name}/{cat_name} ({cat_code})"

                try:
                    products = await fetch_category_products(
                        page, cat_code, group_name, cat_name
                    )
                except Exception as e:
                    err = f"{label}: {e}"
                    stats["errors"].append(err)
                    await log(f"  {label}: ERROR - {e}")
                    continue

                if not products:
                    stats["categories_empty"] += 1
                    await log(f"  {label}: 0 products (empty/404)")
                    continue

                # Track URLs we've seen
                for prod in products:
                    if prod.get("product_url"):
                        seen_urls.add(prod["product_url"])

                # Count new vs updated
                before_count = conn.execute(
                    "SELECT COUNT(*) FROM product_catalog WHERE store_id = ?",
                    (CARREFOUR_STORE_ID,)
                ).fetchone()[0]

                catalog_store_products(CARREFOUR_STORE_ID, products, conn=conn)

                after_count = conn.execute(
                    "SELECT COUNT(*) FROM product_catalog WHERE store_id = ?",
                    (CARREFOUR_STORE_ID,)
                ).fetchone()[0]

                new_this_cat = after_count - before_count
                updated_this_cat = len(products) - new_this_cat

                stats["total"] += len(products)
                stats["new"] += new_this_cat
                stats["updated"] += updated_this_cat
                stats["categories_scraped"] += 1
                group_count += len(products)

                await log(f"  {label}: {len(products)} products ({new_this_cat} new, {updated_this_cat} updated)")

                # Brief pause between categories
                await asyncio.sleep(0.8)

            stats["by_group"][group_name] = group_count

        # Mark products not seen in this scrape as potentially unavailable
        # Only do this for FULL inventory scrapes, not partial.
        is_full_scrape = (categories is None)
        if is_full_scrape:
            # GUARD: never mark "not seen" products unavailable when the scrape
            # was clearly incomplete (blocked / timed out / errored partway).
            # Otherwise a single bad run wholesale-zeros the store — that is how
            # Carrefour reached 0 available of 60,714. Only sweep when we re-saw
            # enough of the existing catalog to trust its absences.
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
                              detail=f"store={CARREFOUR_STORE_ID} seen={seen_count} "
                                     f"existing={existing_count}", level="warning")
                except Exception:
                    pass
            else:
                disappeared = list(existing_urls - seen_urls)
                if disappeared:
                    # Batch under SQLite's parameter limit (999): a full Carrefour
                    # scrape can leave tens of thousands of URLs unseen, and a single
                    # `IN (?, ?, ...)` over all of them threw "too many SQL variables"
                    # -- which aborted the whole nightly refresh, silently staling the
                    # catalog. 900 per batch leaves room for the store_id bind.
                    BATCH = 900
                    for i in range(0, len(disappeared), BATCH):
                        chunk = disappeared[i:i + BATCH]
                        placeholders = ",".join(["?"] * len(chunk))
                        conn.execute(
                            f"UPDATE product_catalog SET is_available = 0 WHERE store_id = ? AND product_url IN ({placeholders})",
                            [CARREFOUR_STORE_ID] + chunk,
                        )
                    conn.commit()
                    await log(f"\n[RSC] Marked {len(disappeared)} products as unavailable")
        else:
            await log(f"\n[RSC] Partial scrape — skipping availability check")

        # Post-scrape hook: tell Book Keeper to re-index alternatives
        # Contract: Gatherer never finishes without the index being refreshed.
        await log(f"\n[RSC] Re-indexing item alternatives against fresh catalog...")
        from rescore_alternatives import rescore_store
        rescore_result = rescore_store(store_id=CARREFOUR_STORE_ID, conn=conn, verbose=False)
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
    print("  CARREFOUR UAE RSC SCRAPE COMPLETE")
    print("=" * 60)
    print(f"  Duration:     {stats['duration_sec']}s")
    print(f"  Total:        {stats['total']} products")
    print(f"  New:          {stats['new']}")
    print(f"  Updated:      {stats['updated']}")
    print(f"  Categories:   {stats['categories_scraped']} scraped, {stats['categories_empty']} empty")
    print()
    print("  By group:")
    for group, count in sorted(stats["by_group"].items(), key=lambda x: -x[1]):
        print(f"    {group:25s} {count:>5}")
    if stats["errors"]:
        print(f"\n  Errors ({len(stats['errors'])}):")
        for err in stats["errors"][:10]:
            print(f"    - {err}")
    print("=" * 60)


# ────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Carrefour UAE RSC Scraper")
    parser.add_argument("--test", action="store_true",
                        help="Test mode: scrape eggs category only")
    parser.add_argument("--category", type=str,
                        help="Scrape one category code (e.g., F1600000)")
    parser.add_argument("--group", type=str,
                        help="Scrape one group (e.g., 'Fresh Food')")
    parser.add_argument("--food-only", action="store_true",
                        help="Scrape food categories only (skip household/personal care)")
    args = parser.parse_args()

    if args.test:
        cats = {"Fresh Food": {"F21630400": "Eggs"}}
    elif args.category:
        code = args.category
        if code in ALL_CATEGORIES:
            label = ALL_CATEGORIES[code]
            group = label.split(" > ")[0]
            name = label.split(" > ")[1] if " > " in label else code
            cats = {group: {code: name}}
        else:
            # Try as a top-level category — scrape all its subcategories
            found = False
            for group_name, subcats in ALL_SCRAPE_CATEGORIES.items():
                if code in subcats:
                    cats = {group_name: {code: subcats[code]}}
                    found = True
                    break
            if not found:
                print(f"Unknown category code: {code}")
                print(f"Available top-level groups: {list(ALL_SCRAPE_CATEGORIES.keys())}")
                sys.exit(1)
    elif args.group:
        group_match = None
        for group_name in ALL_SCRAPE_CATEGORIES:
            if args.group.lower() in group_name.lower():
                group_match = group_name
                break
        if not group_match:
            print(f"Unknown group: {args.group}")
            print(f"Available: {list(ALL_SCRAPE_CATEGORIES.keys())}")
            sys.exit(1)
        cats = {group_match: ALL_SCRAPE_CATEGORIES[group_match]}
    elif args.food_only:
        cats = FOOD_CATEGORIES
    else:
        cats = None  # Full inventory (food + household)

    stats = await scrape_inventory(categories=cats)
    print_stats(stats)


if __name__ == "__main__":
    asyncio.run(main())
