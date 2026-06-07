"""
AutoRestock - Kibsons Scraper

Real browser scraper using Playwright + your Edge profile.
Same two-mode approach as Carrefour:
  1. CATEGORY BROWSING (preferred) --Navigate to a category page, click "Load More"
     to get the full product list, then scrape each product page for details.
  2. KEYWORD SEARCH (fallback) --Search by keyword, scrape results.

IMPORTANT: Close Edge before running. Playwright can't share the profile.

Usage:
  python3 scrape_kibsons.py                          # scrape eggs via category (default)
  python3 scrape_kibsons.py --search "organic eggs"  # keyword search
  python3 scrape_kibsons.py --category URL            # browse a specific category URL
  python3 scrape_kibsons.py --item Eggs               # scrape by habit item name

Outputs scraped products as JSON and optionally feeds into knowledge base.
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from browser_common import launch_edge, check_browser_health, relaunch_browser

# Kibsons base
BASE_URL = "https://www.kibsons.com/en/"
SEARCH_URL = "https://www.kibsons.com/en/search/{query}"

# ────────────────────────────────────────────
# Category URLs --unbiased browsing
# ────────────────────────────────────────────
# Kibsons uses /en/{category}/{subcategory} pattern.
# Product URLs look like: /en/{category}/{subcategory}/{product-slug-SKU}
#
# Kibsons is the preferred store for 11 of our 20 items.
CATEGORY_URLS = {
    # ── Dairy ──
    "Eggs": [
        "https://www.kibsons.com/en/dairy/eggs",
    ],
    "Cheese": [
        "https://www.kibsons.com/en/dairy/cheese",
    ],
    "Yogurt": [
        "https://www.kibsons.com/en/dairy/yoghurt",  # British spelling on Kibsons
    ],
    "Butter": [
        "https://www.kibsons.com/en/dairy/butter",
    ],
    "Milk": [
        "https://www.kibsons.com/en/dairy/fresh-milk",
        "https://www.kibsons.com/en/dairy/local",
    ],
    "Kefir": [
        # /en/dairy/kefirs category is empty --keyword search will be used as fallback
    ],

    # ── Produce ──
    "Apple": [
        "https://www.kibsons.com/en/fruits",
    ],
    "Banana": [
        "https://www.kibsons.com/en/fruits",
    ],
    "Cucumber": [
        "https://www.kibsons.com/en/vegetables",
    ],
    "Pepper": [
        "https://www.kibsons.com/en/vegetables",
    ],
    "Spinach": [
        "https://www.kibsons.com/en/vegetables/leafy-greens",
        "https://www.kibsons.com/en/vegetables",
    ],
    "Rocket": [
        "https://www.kibsons.com/en/vegetables/leafy-greens",
        "https://www.kibsons.com/en/vegetables",
    ],

    # ── Meat & Fish ──
    "Beef Mince": [
        "https://www.kibsons.com/en/meat",
    ],
    "Beef Sausages": [
        "https://www.kibsons.com/en/meat",
    ],
    "Striploin": [
        "https://www.kibsons.com/en/meat",
    ],
    "Chicken Breast": [
        "https://www.kibsons.com/en/meat/chicken-poultry",
        "https://www.kibsons.com/en/meat",
    ],
    "Salmon": [
        "https://www.kibsons.com/en/seafood",
        "https://www.kibsons.com/en/meat",
    ],

    # ── Bakery & Pantry ──
    "Bread": [
        "https://www.kibsons.com/en/bakery/bread-loaves",
        "https://www.kibsons.com/en/bakery",
    ],
    "Pasta": [
        "https://www.kibsons.com/en/pantry/pasta-noodles",
    ],
    "Olive Oil": [
        # /en/pantry/oils-vinegar category was empty, try broader pantry
        "https://www.kibsons.com/en/pantry",
    ],
}



async def search_kibsons(page, query, max_results=20):
    """
    Search Kibsons and scrape products.

    Falls back to this when category browsing doesn't work for an item
    (e.g., Pasta, Olive Oil --no clear category page).

    Returns list of product dicts.
    """
    print(f"\n  Searching Kibsons for: '{query}'")
    search_url = SEARCH_URL.format(query=query.replace(' ', '%20'))

    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    print(f"  Page loaded. Waiting for products to render...")
    await asyncio.sleep(5)

    # Extract product URLs from search results
    product_urls = await _extract_product_urls(page, max_results)

    print(f"  Found {len(product_urls)} unique product URLs")

    if not product_urls:
        print("  WARNING: No product URLs found.")
        await _debug_dump_page(page)
        return []

    # Visit each product page for real data
    products = await _scrape_product_urls(page, product_urls, max_results)
    return products


async def browse_category(page, category_url, max_load_more=10):
    """
    Browse a Kibsons category page and get ALL products by clicking "Load More".

    Same approach as Carrefour: unbiased, complete inventory.

    Returns list of product dicts.
    """
    print(f"\n  [CAT] Browsing category: {category_url.split('/en/')[-1]}")

    try:
        await page.goto(category_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  [FAIL] Failed to load category page: {e}")
        return []

    print(f"  Page loaded. Waiting for products to render...")
    await asyncio.sleep(5)

    # ── Click "Load More" to get the full product list ──
    load_more_clicks = 0
    for attempt in range(max_load_more):
        count_before = await _count_product_urls(page)

        # Look for "Load More" / pagination buttons
        load_more_btn = None
        load_more_selectors = [
            'button:has-text("Load More")',
            'button:has-text("load more")',
            'button:has-text("Show More")',
            'button:has-text("show more")',
            'button:has-text("View More")',
            'button:has-text("See More")',
            'a:has-text("Load More")',
            'a:has-text("Show More")',
            '[data-testid*="load-more"]',
            '[data-testid*="loadMore"]',
            '[class*="loadMore"]',
            '[class*="load-more"]',
            '[class*="showMore"]',
            '[class*="show-more"]',
            '[class*="viewMore"]',
        ]
        for sel in load_more_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    load_more_btn = btn
                    break
            except Exception:
                continue

        if not load_more_btn:
            print(f"  [OK] All products loaded (no more 'Load More' button)")
            break

        try:
            await load_more_btn.click()
            load_more_clicks += 1
            await asyncio.sleep(3)

            count_after = await _count_product_urls(page)
            new_products = count_after - count_before
            print(f"    Load More #{load_more_clicks}: {count_before} -> {count_after} products (+{new_products})")

            if new_products == 0:
                print(f"  [OK] No new products loaded -- reached end of category")
                break

        except Exception as e:
            print(f"    Load More click failed: {e}")
            break

    # Extract all product URLs
    product_urls = await _extract_product_urls(page)

    total_urls = len(product_urls)
    print(f"  Found {total_urls} unique product URLs (after {load_more_clicks} Load More clicks)")

    if not product_urls:
        print("  WARNING: No product URLs found in category.")
        await _debug_dump_page(page)
        return []

    # Visit each product page for real data
    products = await _scrape_product_urls(page, product_urls)
    return products


async def _count_product_urls(page):
    """Count unique product URLs on the current page."""
    return await page.evaluate("""
        () => {
            const seen = new Set();
            const links = document.querySelectorAll('a[href]');
            for (const link of links) {
                const href = (link.getAttribute('href') || '').split('?')[0];
                // Kibsons product URLs: /en/category/subcategory/product-slug
                const parts = href.replace(/^\\/en\\//, '').split('/').filter(Boolean);
                if (parts.length >= 3 || (parts.length >= 2 && parts[parts.length-1].length > 20)) {
                    if (href.startsWith('/en/') && !href.includes('/shopping/') && !href.includes('/aboutus')) {
                        seen.add(href);
                    }
                }
            }
            return seen.size;
        }
    """)


async def _extract_product_urls(page, max_results=100):
    """Extract unique product URLs from the current page."""
    return await page.evaluate("""
        (maxResults) => {
            const urls = [];
            const seen = new Set();
            const links = document.querySelectorAll('a[href]');

            // Skip these path segments --they're navigation, not products
            const skipPaths = ['shopping', 'aboutus', 'cart', 'checkout', 'account',
                               'login', 'register', 'contact', 'faq', 'terms',
                               'privacy', 'origin', 'home'];

            for (const link of links) {
                if (urls.length >= maxResults) break;
                let href = link.getAttribute('href') || '';
                if (!href.startsWith('/en/')) continue;

                const cleanHref = href.split('?')[0].split('#')[0];

                // Skip navigation/utility pages
                const firstSegment = cleanHref.replace('/en/', '').split('/')[0];
                if (skipPaths.includes(firstSegment)) continue;

                // Count path segments after /en/
                const parts = cleanHref.replace('/en/', '').split('/').filter(Boolean);

                // Product pages have 3+ segments OR 2 segments with long slug
                const isProduct = parts.length >= 3 ||
                    (parts.length >= 2 && parts[parts.length-1].length > 20);

                if (!isProduct) continue;

                // Dedup
                if (seen.has(cleanHref)) continue;
                seen.add(cleanHref);

                // Make absolute
                if (href.startsWith('/')) {
                    href = 'https://www.kibsons.com' + href;
                }
                urls.push(href);
            }
            return urls;
        }
    """, max_results)


async def _scrape_product_urls(page, product_urls, max_results=None):
    """Visit each product page and scrape data."""
    if max_results:
        product_urls = product_urls[:max_results]

    products = []
    detail_page = await page.context.new_page()
    total = len(product_urls)

    try:
        for i, url in enumerate(product_urls):
            slug = url.split('/')[-1][:40] if '/' in url else url[-40:]
            print(f"    [{i+1}/{total}] Scraping: {slug}...")
            try:
                product = await scrape_product_page(detail_page, url)
                if product and product.get("name"):
                    products.append(product)
                    price_str = f"{product['price']} AED" if product.get('price') else "!! no price"
                    print(f"           > {product['name'][:50]} | {price_str} | {product.get('size', '')}")
            except Exception as e:
                print(f"           > Error: {e}")

        # Price extraction summary
        priced = sum(1 for p in products if p.get("price"))
        print(f"\n  Price extraction: {priced}/{len(products)} products have prices")
    finally:
        try:
            await detail_page.close()
        except Exception:
            pass

    print(f"  Extracted {len(products)} products with full details")
    return products


async def scrape_product_page(page, product_url, timeout=15000):
    """
    Scrape a single Kibsons product page for ALL available data.

    Kibsons PDP structure:
      - H1: generic product name ("Yogurt", "Eggs")
      - Brand in colored text (ALL CAPS like "MOVENPICK")
      - Size variant ("Strawberry - 100g")
      - Price ("2.85/pack")
      - Tabs: Description | Ingredients | Storage | Nutrition (Tailwind clickable divs)
      - "More in {Category}" section with related products

    Returns dict with: name, price, size, brand, description, ingredients,
    nutrition_info, origin_country, is_organic, category, product_url, image_url
    """
    product = {"product_url": product_url}

    try:
        await page.goto(product_url, wait_until="domcontentloaded", timeout=timeout)
        await asyncio.sleep(3)  # Let page render

        # ── Step 1: Extract basic info (name, brand, price, size, image) ──
        data = await page.evaluate("""
            () => {
                const result = {};

                // Name: H1
                const h1 = document.querySelector('h1');
                if (h1) result.name = h1.innerText?.trim() || '';

                // Image — prefer product-specific images, skip the generic site logo
                // Real product images: /products/detail/PRODUCT_ID.jpg
                // Generic logo/placeholder: /kibsons-ent/name*_delivery.png
                const productImg = document.querySelector(
                    'img[src*="products/detail"]'
                );
                if (productImg) {
                    result.image_url = productImg.src || '';
                }
                if (!result.image_url) {
                    // Fallback: any kibsecomimgstore img that's NOT the generic logo
                    const allImgs = document.querySelectorAll('img[src*="kibsecomimgstore"]');
                    for (const img of allImgs) {
                        if (!img.src.includes('/kibsons-ent/name')) {
                            result.image_url = img.src;
                            break;
                        }
                    }
                }
                if (!result.image_url) {
                    const anyImg = document.querySelector('[class*="product"] img, [class*="gallery"] img');
                    if (anyImg) result.image_url = anyImg.src || '';
                }

                // Breadcrumb
                const bc = document.querySelector('[class*="breadcrumb"], nav[aria-label*="bread"]');
                if (bc) result.category = bc.innerText?.trim()?.substring(0, 200) || '';

                // ── Product info section (above the tabs) ──
                // On Kibsons, the H1 parent section has:
                //   H1 (product type), Brand text, Size, Price
                // We capture the entire text block for post-processing
                const h1Parent = h1?.closest('[class*="tw-"]')?.parentElement;
                if (h1Parent) {
                    // Get text ONLY from the product info section (not "More in" or tabs)
                    const infoText = h1Parent.innerText?.trim() || '';
                    // Cut at "More in" if present
                    const moreIdx = infoText.indexOf('More in ');
                    const kibsonsRegIdx = infoText.indexOf('KIBSONS REGULAR');
                    const cutIdx = Math.min(
                        moreIdx > 0 ? moreIdx : 99999,
                        kibsonsRegIdx > 0 ? kibsonsRegIdx : 99999,
                        2000
                    );
                    result.product_info_raw = infoText.substring(0, cutIdx).trim();
                }

                // ── Badge detection (organic, etc.) ──
                // Kibsons marks organic with an image badge (organic96.png),
                // not with text. Detect by checking badge image URLs.
                const badgeImgs = document.querySelectorAll('img[alt="small-icon"], img[class*="tw-w-8"], img[class*="tw-w-10"]');
                const badges = [];
                badgeImgs.forEach(img => {
                    const src = img.src || '';
                    if (src.includes('ecomsiteimages/assets/images')) {
                        const m = src.match(/\/([^/]+?)[\d]*\.png$/);
                        if (m) badges.push(m[1].toLowerCase());
                    }
                });
                if (badges.length) result.badges = badges;

                return result;
            }
        """)

        for key, val in data.items():
            if val is not None and val != '':
                product[key] = val

        # ── Step 2: Click each tab to get description, ingredients, storage, nutrition ──
        # IMPORTANT: Read actual tab labels instead of assuming fixed order.
        # Some products have tabs in different order or missing tabs.
        tab_contents = await page.evaluate("""
            async () => {
                const result = {};
                const tabBar = document.querySelector('.tw-flex.tw-border-b.tw-mb-4');
                if (!tabBar) return result;

                const tabParent = tabBar.parentElement;
                if (!tabParent || tabParent.children.length < 2) return result;

                const tabs = tabBar.querySelectorAll(':scope > div');

                for (let i = 0; i < tabs.length; i++) {
                    try {
                        // Read the actual tab label to map to correct field
                        const label = (tabs[i].innerText?.trim() || '').toLowerCase();
                        let key = null;
                        if (label.includes('description')) key = 'description';
                        else if (label.includes('ingredient')) key = 'ingredients';
                        else if (label.includes('storage')) key = 'storage';
                        else if (label.includes('nutrition')) key = 'nutrition';
                        else continue;  // skip unknown tabs

                        tabs[i].click();
                        await new Promise(r => setTimeout(r, 300));
                        const panel = tabParent.children[1];
                        if (panel) {
                            const text = panel.innerText?.trim() || '';
                            if (text.length > 3) {
                                result[key] = text.substring(0, 2000);
                            }
                        }
                    } catch(e) {}
                }

                // Click back to first tab
                if (tabs[0]) tabs[0].click();

                return result;
            }
        """)

        # Store tab content
        if tab_contents.get("description"):
            product["description"] = tab_contents["description"]
        if tab_contents.get("ingredients"):
            product["ingredients"] = tab_contents["ingredients"]
        if tab_contents.get("storage"):
            product["storage_info"] = tab_contents["storage"]
        if tab_contents.get("nutrition"):
            product["nutrition_info"] = tab_contents["nutrition"]

        # ── Step 3: Kibsons-specific post-processing ──
        _post_process_kibsons(product)

    except Exception as e:
        product["scrape_error"] = str(e)

    return product


def _post_process_kibsons(product):
    """
    Parse Kibsons product_info_raw to extract price, brand, size, and
    build a proper full product name (the H1 is just a generic category name).

    product_info_raw layout (lines):
      [optional: "Sponsored ad" / "Fresh" / "By Air" / "May Contain Seed Traces"]
      Product Type Name (e.g., "Aged Sheep Cheese", "Chicken Breasts")
      BRAND or COUNTRY (ALL CAPS: "EL PASTOR", "MOVENPICK", "UAE", "NEW ZEALAND")
      Size variant (e.g., "Rosemary - 175g", "Skinless- 300g")
      [optional: discounted price e.g. "37.50"]
      Price/unit (e.g., "64.40/tray", "25.00/pack", "49.95/pack")
      "0" (quantity counter)
      [Description/Ingredients/Storage/Nutrition tab names --now extracted via clicks]
      [Description content --now extracted via clicks]
      ...
    """
    raw = product.get("product_info_raw", "")
    if not raw:
        return

    lines = [l.strip() for l in raw.split('\n') if l.strip()]

    # ── Strip "More in" section and everything after ──
    clean_lines = []
    for line in lines:
        if line.startswith("More in "):
            break
        clean_lines.append(line)
    lines = clean_lines

    # ── Extract price: prefer discounted price, fall back to XX.XX/unit ──
    if not product.get("price"):
        sale_price = None
        regular_price = None
        regular_unit = None

        for line in lines:
            # XX.XX/unit pattern (regular or bundle price)
            m = re.match(r'^(\d+[\.,]\d{2})\s*/\s*(\w+)', line)
            if m:
                regular_price = float(m.group(1).replace(',', '.'))
                regular_unit = m.group(2)
                continue
            # Standalone number before the /unit line = discounted/sale price
            m2 = re.match(r'^(\d+[\.,]\d{2})$', line)
            if m2 and not sale_price:
                sale_price = float(m2.group(1).replace(',', '.'))

        # Use sale price if available and lower, otherwise regular
        if sale_price and regular_price and sale_price < regular_price:
            product["price"] = sale_price
            product["price_regular"] = regular_price
        elif regular_price:
            product["price"] = regular_price
        elif sale_price:
            product["price"] = sale_price

        if regular_unit:
            product["price_unit"] = regular_unit
        if product.get("price"):
            product["_price_source"] = "info_raw_parse"

    # ── Extract brand and origin from ALL CAPS line (MOST RELIABLE) ──
    skip_tags = {"sponsored ad", "fresh", "by air", "new", "sale", "bestseller",
                 "best seller", "description", "ingredients", "storage", "nutrition",
                 "kibsons regular", "kibsons express", "ready to eat",
                 "may contain seed traces", "grain-fed", "omega-3",
                 "high in protein", "immunity support", "previously purchased",
                 "traditional", "cooks in 4 minutes", "cooks in 5 minutes",
                 "cooks in 6 minutes", "contains animal rennet"}

    countries = {"UAE", "FRANCE", "TURKEY", "USA", "UK", "ITALY", "GERMANY",
                 "NETHERLANDS", "SPAIN", "BRAZIL", "AUSTRALIA", "NEW ZEALAND",
                 "SOUTH AFRICA", "SAUDI ARABIA", "OMAN", "BAHRAIN", "KUWAIT",
                 "IRELAND", "GREECE", "NAMIBIA", "IRAN", "TUNISIA", "EGYPT",
                 "UKRAINE", "DENMARK", "UNITED STATES", "UNITED KINGDOM",
                 "NORWAY", "SCOTLAND", "ARGENTINA", "CANADA", "INDIA",
                 "THAILAND", "JAPAN", "CHINA", "SOUTH KOREA", "CHILE",
                 "PERU", "COLOMBIA", "KENYA", "ETHIOPIA", "MOROCCO",
                 "LEBANON", "JORDAN", "PAKISTAN", "SRI LANKA", "MEXICO",
                 # Added: countries found as brand misidentification in scrape data
                 "VIETNAM", "ECUADOR", "YEMEN", "MAURITIUS", "UGANDA",
                 "POLAND", "SWITZERLAND", "COSTA RICA", "PHILIPPINES",
                 "INDONESIA", "MALAYSIA", "PORTUGAL", "BELGIUM", "AUSTRIA",
                 "SWEDEN", "FINLAND", "CZECH REPUBLIC", "HUNGARY", "ROMANIA",
                 "BULGARIA", "CROATIA", "SERBIA", "TANZANIA", "MADAGASCAR",
                 "MYANMAR", "CAMBODIA", "HONDURAS", "GUATEMALA", "PANAMA",
                 "DOMINICAN REPUBLIC", "JAMAICA", "TRINIDAD", "SYRIA", "IRAQ",
                 "QATAR", "SOMALIA", "SUDAN", "LIBYA", "ALGERIA",
                 "GCC", "EU", "EUROPEAN UNION"}

    for line in lines[:8]:  # Brand/origin appears in first few lines
        low = line.lower()
        if low in skip_tags:
            continue
        # Skip if it's the product name (matches H1)
        if low == product.get("name", "").lower():
            continue
        # Skip if it looks like a size (has digits + units)
        if re.search(r'\d+\s*(pcs|eggs?|pack|pieces?|g|kg|ml|l|ltr|x\d)\b', low):
            continue
        # Skip if it's a price line
        if re.search(r'^\d+[\.,]\d{2}', low):
            continue
        # Skip "0" (quantity counter)
        if line == "0":
            continue

        # ALL CAPS short text → brand or origin country
        if line.isupper() and 2 < len(line) < 40:
            if line in countries:
                product.setdefault("origin_country", line.title())
            else:
                product.setdefault("brand", line.title())
            continue

    # ── Fallback: extract brand from URL slug (if ALL CAPS didn't find it) ──
    url = product.get("product_url", "")
    slug = url.rstrip('/').split('/')[-1] if '/' in url else ""

    if not product.get("brand") and slug:
        brand_from_slug = _extract_brand_from_slug(slug)
        if brand_from_slug:
            product["brand"] = brand_from_slug

    # ── Extract size from lines (line after brand, before price) ──
    if not product.get("size"):
        for line in lines[:8]:
            low = line.lower()
            if low in skip_tags:
                continue
            if line.isupper():
                continue  # Skip brand/country lines
            if re.search(r'^\d+[\.,]\d{2}', low):
                continue  # Skip price lines
            if line == "0":
                continue
            if low == product.get("name", "").lower():
                continue
            # Look for lines with quantity info
            if re.search(r'\d+\s*(pcs|eggs?|pack|pieces?|g|kg|ml|l|ltr)\b', low, re.IGNORECASE):
                product["size"] = line
                break
            # Also catch size-like lines: "Skinless- 300g", "2x 500g", "Rosemary - 175g"
            if re.search(r'\d+\s*g\b|\d+\s*kg\b|\d+\s*ml\b|\d+\s*l\b', low):
                product["size"] = line
                break

    # ── Build proper full name ──
    h1_name = product.get("name", "")
    brand = product.get("brand", "")
    size = product.get("size", "")

    parts = []
    if brand and brand.lower() not in h1_name.lower():
        parts.append(brand)
    parts.append(h1_name)
    if size and size.lower() not in h1_name.lower():
        parts.append(size)
    full_name = " ".join(parts).strip()

    # Deduplicate consecutive repeated words:
    # "Jimmys Jimmys Fusion Burger" → "Jimmys Fusion Burger"
    # "Kibsons Passionfruit Passionfruit 2 Kg" → "Kibsons Passionfruit 2 Kg"
    words = full_name.split()
    deduped = [words[0]] if words else []
    for w in words[1:]:
        if w.lower() != deduped[-1].lower():
            deduped.append(w)
    full_name = " ".join(deduped)

    if full_name and len(full_name) > len(h1_name):
        product["name"] = full_name

    # ── Extract origin from description text if not found ──
    if not product.get("origin_country"):
        desc = product.get("description", "")
        m = re.search(r'Product origin:\s*(.+)', desc, re.IGNORECASE)
        if m:
            product["origin_country"] = m.group(1).strip()
        else:
            # Check raw lines
            for line in lines:
                m = re.match(r'Product origin:\s*(.+)', line, re.IGNORECASE)
                if m:
                    product["origin_country"] = m.group(1).strip()
                    break

    # ── Clean description: strip "Product origin:" line ──
    if product.get("description"):
        desc_lines = product["description"].split('\n')
        clean_desc = []
        for dl in desc_lines:
            if not dl.strip().lower().startswith("product origin"):
                clean_desc.append(dl)
        product["description"] = "\n".join(clean_desc).strip()
        if not product["description"]:
            del product["description"]

    # ── Organic detection (check text + badge images) ──
    all_text = " ".join([
        product.get("name", ""),
        product.get("description", ""),
        product.get("ingredients", ""),
        raw,
        product.get("product_url", ""),
    ])
    has_organic_text = bool(
        re.search(r'\borganic\b', all_text, re.IGNORECASE)
        and not re.search(r'non.organic', all_text, re.IGNORECASE)
    )
    # Kibsons marks organic products with a badge image (organic96.png)
    # that has no corresponding text — detect from scraped badge list
    has_organic_badge = "organic" in product.get("badges", [])
    product["is_organic"] = has_organic_text or has_organic_badge

    # ── Ready meal / prepared food detection ──
    ready_meal_signals = ["ready to eat", "ready meal", "meal kit", "heat and eat",
                          "quesadilla", "salad bowl", "wrap", "burrito",
                          "kibsons kitchen"]  # Kibsons Kitchen = prepared food/recipe kits
    product_text = (product.get("name", "") + " " + raw + " " + product.get("brand", "")).lower()
    product["is_ready_meal"] = any(sig in product_text for sig in ready_meal_signals)


def _extract_brand_from_slug(slug):
    """
    Extract brand name from Kibsons URL slug.

    Slug format: brand-product-SKUCODE
    e.g., "organicasa-eggs-eggoetror30pao" → "Organicasa"
          "jenan-brown-eggs-eggmbaejn30pl1" → "Jenan"
          "tregor-omega-3-eggs-eggomfrtr15pa1" → "Tregor"
          "al-jazira-white-eggs-eggvgaeaj15xl1" → "Al Jazira"
    """
    if not slug:
        return None

    # Split slug into words
    parts = slug.split('-')
    if len(parts) < 2:
        return None

    # The SKU is usually the last part (mix of letters+digits, 10+ chars)
    # Remove it first. SKU patterns include:
    #   eggoetror30pao, eggmbaejn30pl1 (standard)
    #   macfoaecf1.6lc, hammeaecf2.5lc (with dots for weight)
    #   banxxphji13ks1, mgoxxyexxxxxs1 (mixed)
    if parts and re.match(r'^[a-z]{3}[a-z0-9.]{6,}$', parts[-1], re.IGNORECASE):
        parts = parts[:-1]

    # Known product type words --everything before these is the brand.
    # This set is large because Kibsons slugs are like:
    #   "el-pastor-aged-sheep-cheese-SKUCODE" → we want "El Pastor", not "El Pastor Aged Sheep"
    #   "date-crown-khalas-dates-SKUCODE" → we want "Date Crown", not "Date Crown Khalas Dates"
    product_words = {
        # ── Eggs ──
        'eggs', 'egg', 'white', 'brown', 'free', 'range',
        'organic', 'omega', 'lutein', 'quail', 'liquid',
        'dha', 'whites', 'medium', 'large', 'xl', 'xxl',
        # ── Cheese ──
        'cheese', 'aged', 'sheep', 'goat', 'cow', 'cheddar', 'mozzarella',
        'parmesan', 'brie', 'camembert', 'feta', 'halloumi', 'haloumi',
        'gruyere', 'emmental', 'cream', 'cottage', 'ricotta', 'mascarpone',
        'burrata', 'stilton', 'blue', 'smoked', 'sliced', 'shredded',
        'grated', 'block', 'spread', 'flavoured', 'rosemary', 'truffle',
        'pepper', 'chilli', 'herb', 'herbs', 'garlic', 'labneh',
        'manchego', 'pecorino', 'provolone', 'string',
        # ── Dairy ──
        'yogurt', 'yoghurt', 'butter', 'milk', 'kefir', 'laban',
        'unsalted', 'salted', 'ghee', 'cultured', 'clotted',
        'strawberry', 'vanilla', 'peach', 'mango', 'blueberry',
        'raspberry', 'cherry', 'mixed', 'berries', 'plain',
        'greek', 'natural', 'skimmed', 'semi', 'whole', 'full', 'fat', 'low',
        'drinking', 'probiotic',
        # ── Meat ──
        'chicken', 'beef', 'lamb', 'turkey', 'duck', 'veal',
        'breast', 'breasts', 'thigh', 'thighs', 'drumstick', 'drumsticks',
        'wing', 'wings', 'tender', 'tenders', 'tenderloin',
        'mince', 'minced', 'ground', 'sausage', 'sausages',
        'fillet', 'fillets', 'steak', 'steaks', 'striploin', 'ribeye',
        'sirloin', 'rump', 'topside', 'brisket', 'chuck', 'shank',
        'cutlet', 'cutlets', 'chop', 'chops', 'rack', 'ribs',
        'skinless', 'boneless', 'bone', 'in', 'skin', 'on',
        'ready', 'to', 'eat', 'cook', 'marinated', 'seasoned',
        'grain', 'fed', 'grass', 'wagyu', 'angus', 'black',
        'burger', 'burgers', 'patty', 'patties', 'fusion', 'special',
        'juicy', 'lucy', 'chorizo', 'makanek', 'chakalaka',
        'kebab', 'kebabs', 'kofta', 'koftas', 'shawarma',
        'bolognese', 'soup', 'wrap', 'salad',
        # ── Fish / Seafood ──
        'fish', 'seafood', 'king', 'grey', 'mullet', 'grouper',
        'salmon', 'tuna', 'cod', 'sea', 'bass', 'bream',
        'prawns', 'shrimp', 'lobster', 'crab', 'squid', 'calamari',
        'trout', 'sole', 'haddock', 'mackerel', 'sardine', 'sardines',
        'smoked', 'fresh', 'frozen', 'wild', 'caught', 'farmed',
        'portion', 'portions', 'supreme', 'loin', 'darne',
        'fillet', 'whole', 'cleaned', 'brush', 'clean', 'normal',
        'mangrove', 'snapper', 'hammour', 'hamour',
        'guinea', 'fowl',  # poultry: guinea fowl
        # ── Fruits ──
        'apple', 'apples', 'banana', 'bananas', 'orange', 'oranges',
        'grape', 'grapes', 'watermelon', 'melon', 'pineapple',
        'kiwi', 'avocado', 'avocados', 'pomegranate', 'fig', 'figs',
        'dates', 'date', 'khalas', 'majdool', 'medjool', 'sagai', 'sukkari',
        'lemon', 'lemons', 'lime', 'limes', 'grapefruit',
        'mango', 'mangoes', 'papaya', 'passion', 'fruit', 'fruits',
        'berry', 'strawberries', 'blueberries', 'raspberries',
        'blackberries', 'blackberry', 'cranberry', 'cranberries',
        'nectarine', 'nectarines', 'peach', 'peaches', 'plum', 'plums',
        'coconut', 'coconuts', 'dragon', 'granadilla', 'granadillas',
        'passionfruit', 'lychee', 'lychees', 'guava', 'persimmon',
        'apricot', 'apricots', 'clementine', 'mandarin', 'tangerine',
        'taimoor', 'palmer',  # mango varieties on Kibsons
        'red', 'green', 'yellow', 'golden', 'royal', 'gala',
        'fuji', 'granny', 'smith', 'pink', 'lady',
        # ── Vegetables ──
        'cucumber', 'cucumbers', 'tomato', 'tomatoes', 'cherry',
        'pepper', 'peppers', 'bell', 'capsicum', 'chili', 'jalapeno',
        'spinach', 'rocket', 'arugula', 'lettuce', 'kale', 'chard',
        'broccoli', 'cauliflower', 'cabbage', 'zucchini', 'courgette',
        'eggplant', 'aubergine', 'potato', 'potatoes', 'sweet',
        'onion', 'onions', 'carrot', 'carrots', 'beetroot', 'radish',
        'celery', 'corn', 'peas', 'beans', 'baby', 'mini', 'vine',
        'roma', 'cluster', 'bunch', 'pack', 'bag',
        # ── Bakery ──
        'bread', 'loaf', 'loaves', 'sliced', 'sourdough', 'rye', 'soda',
        'wholemeal', 'wholegrain', 'multigrain', 'white', 'brown',
        'brioche', 'baguette', 'ciabatta', 'focaccia', 'pita', 'naan',
        'rolls', 'buns', 'bagel', 'bagels', 'toast', 'wraps', 'tortilla',
        'croissant', 'croissants', 'danish', 'pastry',
        # ── Pantry (Pasta, Oil) ──
        'pasta', 'penne', 'spaghetti', 'fusilli', 'rigatoni', 'farfalle',
        'linguine', 'tagliatelle', 'fettuccine', 'orzo', 'macaroni',
        'lasagne', 'lasagna', 'noodles', 'noodle', 'vermicelli',
        'olive', 'oil', 'extra', 'virgin', 'coconut', 'sunflower',
        'canola', 'vegetable', 'sesame', 'avocado',
        'vinegar', 'balsamic', 'apple', 'cider',
        # ── General/Shared ──
        'premium', 'classic', 'original', 'traditional', 'special',
        'selection', 'range', 'value', 'family', 'single', 'twin',
        'triple', 'mini', 'jumbo', 'super', 'mega', 'lite', 'light',
        'new', 'with', 'and', 'from', 'the', 'for', 'of', 'no', 'non',
    }

    brand_parts = []
    for part in parts:
        if part.lower() in product_words:
            break
        # Stop if we hit a number (like "3" in "omega-3")
        if part.isdigit():
            break
        brand_parts.append(part)

    if brand_parts:
        return " ".join(w.capitalize() for w in brand_parts)

    return None


async def _debug_dump_page(page):
    """Dump page structure to help find the right selectors."""
    print("\n  === DEBUG: Kibsons Page Structure ===")

    testids = await page.evaluate("""
        () => {
            const els = document.querySelectorAll('[data-testid]');
            return [...new Set([...els].map(e => e.getAttribute('data-testid')))].slice(0, 30);
        }
    """)
    print(f"  data-testid values: {testids}")

    all_links = await page.evaluate("""
        () => {
            const links = document.querySelectorAll('a[href^="/en/"]');
            const counts = {};
            for (const link of links) {
                const href = link.getAttribute('href').split('?')[0];
                const depth = href.replace('/en/', '').split('/').filter(Boolean).length;
                counts[depth] = (counts[depth] || 0) + 1;
            }
            return counts;
        }
    """)
    print(f"  Links by depth (1=category, 2=subcategory, 3+=product): {all_links}")

    sample_links = await page.evaluate("""
        () => {
            const links = document.querySelectorAll('a[href^="/en/"]');
            return [...links].slice(0, 10).map(e => ({
                href: e.getAttribute('href')?.substring(0, 80),
                text: e.innerText?.trim()?.substring(0, 50),
            }));
        }
    """)
    for link in sample_links:
        print(f"    {link['text'][:40]} > {link['href']}")

    prices = await page.evaluate("""
        () => {
            const els = document.querySelectorAll('[class*="price"], [class*="Price"]');
            return [...els].slice(0, 5).map(e => ({
                text: e.innerText?.trim()?.substring(0, 50),
                classes: e.className?.substring(0, 60),
            }));
        }
    """)
    print(f"  Price elements ({len(prices)}):")
    for p in prices:
        print(f"    '{p['text']}' (class: {p['classes']})")

    print("  === END DEBUG ===\n")


async def scrape_eggs_test():
    """
    Single test run: browse eggs category on Kibsons,
    scrape each product page, score and rank, store in knowledge base.
    """
    from consumption_habits import HABITS
    from db_setup import create_database

    # Get eggs config
    eggs_habit = next((h for h in HABITS if h["name"] == "Eggs"), None)
    if not eggs_habit:
        print("ERROR: Eggs not found in HABITS")
        return

    search_terms = eggs_habit.get("search_terms", {}).get("Kibsons", ["eggs"])
    category_urls = CATEGORY_URLS.get("Eggs", [])

    print("=" * 60)
    print("  Kibsons Scraper --Eggs Test Run")
    print("=" * 60)
    if category_urls:
        print(f"  Strategy: Category browsing (unbiased, full inventory)")
        for url in category_urls:
            print(f"    > {url}")
    else:
        print(f"  Strategy: Keyword search (no category URL defined)")
    print(f"  Fallback search terms: {search_terms}")
    print()

    # Launch browser
    print("  Launching Edge with your profile...")
    p, context, page = await launch_edge()

    try:
        products = []

        # Strategy 1: Browse category page
        for cat_url in category_urls:
            products = await browse_category(page, cat_url)
            if products:
                break
            print(f"  Category {cat_url} returned no products. Trying next...")

        # Strategy 2: Fall back to keyword search
        if not products:
            print(f"\n  Category browsing found nothing. Falling back to keyword search...")
            for search_term in search_terms:
                products = await search_kibsons(page, search_term, max_results=30)
                if products:
                    break

        if not products:
            print("\n  No products found.")
            print("  Check the browser window --the page might use different selectors.")
            await _debug_dump_page(page)
            input("  Press Enter to close...")
            await context.close()
            await p.stop()
            return

        # Score all products
        print(f"\n{'='*60}")
        print("  Scoring products against Eggs preference profile...")
        print(f"{'='*60}")

        from product_score import score_products

        # Connect to DB for purchase history
        create_database()
        import sqlite3 as _sqlite3
        from db_setup import DB_PATH as _DB_PATH
        score_conn = _sqlite3.connect(_DB_PATH)
        score_conn.row_factory = _sqlite3.Row

        scored = score_products("Eggs", "Kibsons", products, conn=score_conn)
        score_conn.close()

        for i, result in enumerate(scored, 1):
            p_data = result["product"]
            mark = "★" if result["tier"] == "preferred" else "·"
            dq = " ❌ DISQUALIFIED" if result["disqualified"] else ""
            print(f"\n  {mark} #{i} [{result['tier'].upper()}] Score: {result['score']}/100{dq}")
            print(f"     {p_data.get('name', '?')}")
            print(f"     {p_data.get('price', '?')} AED | {p_data.get('size', '?')}")
            if p_data.get("origin_country"):
                print(f"     Origin: {p_data['origin_country']}")
            for reason in result["reasons"]:
                print(f"       {reason}")

        # Store in knowledge base
        print(f"\n{'='*60}")
        print("  Storing in knowledge base...")
        print(f"{'='*60}")

        create_database()
        from product_knowledge import discover_alternatives
        import sqlite3
        from db_setup import DB_PATH

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT id FROM stores WHERE name = 'Kibsons'")
        store_row = cur.fetchone()
        cur.execute("SELECT id FROM items WHERE name = 'Eggs'")
        item_row = cur.fetchone()

        if store_row and item_row:
            ranked = discover_alternatives(
                item_row["id"], "Eggs", store_row["id"],
                products, conn=conn
            )
            print(f"  Stored {len(products)} products in catalog")
            print(f"  Ranked {len(ranked)} alternatives for Eggs @ Kibsons")

            from product_knowledge import get_ranked_alternatives
            alts = get_ranked_alternatives(item_row["id"], store_row["id"], conn=conn)
            print(f"\n  Knowledge base now has {len(alts)} ranked alternatives:")
            for a in alts:
                avail = "[Y]" if a["is_available"] else "[N]"
                pref = " ** PREFERRED" if a["is_preferred"] else ""
                print(f"    {avail} #{a['rank']} {a['product_name'][:50]} --{a['price']} AED [{a['tier']}]{pref}")
        else:
            print("  WARNING: Eggs or Kibsons not found in DB. Run consumption_habits.py first.")

        conn.close()

        # Save raw JSON
        output_path = Path(__file__).parent / "scrape_output_kibsons_eggs.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(products, f, indent=2, ensure_ascii=False)
        print(f"\n  Raw data saved to: {output_path}")

        print(f"\n{'='*60}")
        print("  Done! Check the browser window to verify results.")
        print("  Press Enter to close the browser.")
        print(f"{'='*60}")
        input()

    except Exception as e:
        print(f"\n  ERROR: {e}")
        import traceback
        traceback.print_exc()
        input("\n  Press Enter to close...")

    finally:
        await context.close()
        await p.stop()


def _filter_relevant_products(item_name, search_terms, products):
    """
    Filter scraped products to keep only ones relevant to the target item.

    When scraping shared category pages (e.g., /en/fruits for "Apple"),
    we get ALL fruits. This filters to keep only apple-related products.

    Uses item name + search term keywords to match against product name,
    description, and URL. Generous matching --keeps anything plausibly related.
    """
    # Build keyword set from item name and search terms
    # "Apple" → {"apple"}
    # Search terms like ["Royal Gala Apple", "Gala Apple"] → {"royal", "gala", "apple"}
    keywords = set()
    for word in item_name.lower().split():
        if len(word) > 2:  # skip tiny words like "of"
            keywords.add(word)
    for term in search_terms:
        for word in term.lower().split():
            if len(word) > 2:
                keywords.add(word)

    # Remove generic words that would match everything
    generic = {'fresh', 'organic', 'free', 'range', 'farm', 'the', 'and', 'for',
               'natural', 'premium', 'best', 'good', 'quality', 'local', 'pack',
               'from', 'with', 'uae', 'dubai'}
    keywords -= generic

    if not keywords:
        return products  # no keywords to filter by

    matched = []
    for p in products:
        # Check product name, URL, description, and breadcrumb category
        text = " ".join([
            p.get("name", ""),
            p.get("product_url", ""),
            p.get("description", "")[:200],
            p.get("category", ""),
        ]).lower()

        # Match if ANY keyword appears in the product text
        if any(kw in text for kw in keywords):
            matched.append(p)

    before = len(products)
    after = len(matched)
    if after < before:
        print(f"  Relevance filter: {before} -> {after} products (keywords: {sorted(keywords)[:5]}...)")
    return matched


async def scrape_all_items():
    """
    Batch scrape ALL items from Kibsons.

    Scrapes every category, scores products, stores in knowledge base.
    Smart about shared URLs --e.g., /en/fruits is scraped once and reused
    for both Apple and Banana scoring.

    Run with: python3 scrape_kibsons.py --all
    """
    from consumption_habits import HABITS
    from db_setup import create_database, DB_PATH
    from product_score import score_products
    from product_knowledge import discover_alternatives, get_ranked_alternatives
    import sqlite3

    create_database()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Get store ID
    cur.execute("SELECT id FROM stores WHERE name = 'Kibsons'")
    store_row = cur.fetchone()
    if not store_row:
        print("ERROR: Kibsons not found in stores table. Run consumption_habits.py first.")
        return
    store_id = store_row["id"]

    # Track results
    all_results = {}
    category_cache = {}  # URL → products (avoid re-scraping shared URLs)
    start_time = time.time()

    print("=" * 70)
    print("  KIBSONS FULL BASELINE SCRAPE")
    print(f"  {len(HABITS)} items to process")
    print("=" * 70)

    # Launch browser
    print("\n  Launching Edge with your profile...")
    p, context, page = await launch_edge()

    try:
        for item_idx, habit in enumerate(HABITS, 1):
            item_name = habit["name"]
            search_terms = habit.get("search_terms", {}).get("Kibsons", [item_name.lower()])
            category_urls = CATEGORY_URLS.get(item_name, [])

            print(f"\n{'='*70}")
            print(f"  [{item_idx}/{len(HABITS)}] {item_name}")
            print(f"{'='*70}")

            # ── Browser health check before each item ──
            if not await check_browser_health(page):
                p, context, page = await relaunch_browser(p, context)
                category_cache.clear()

            try:
                products = []

                # Strategy 1: Browse category pages
                if category_urls:
                    for cat_url in category_urls:
                        # Check cache -- avoid re-scraping shared URLs
                        if cat_url in category_cache:
                            cached = category_cache[cat_url]
                            print(f"  (cached) Using {len(cached)} products from {cat_url.split('/en/')[-1]}")
                            products = cached
                            break

                        cat_products = await browse_category(page, cat_url)
                        if cat_products:
                            category_cache[cat_url] = cat_products
                            products = cat_products
                            break
                        print(f"  Category {cat_url.split('/en/')[-1]} returned 0 products. Trying next...")

                # Strategy 2: Keyword search fallback
                if not products:
                    print(f"  Falling back to keyword search...")
                    for term in search_terms:
                        products = await search_kibsons(page, term, max_results=30)
                        if products:
                            break

                if not products:
                    print(f"  WARNING: No products found for {item_name}. Skipping.")
                    all_results[item_name] = {"products": 0, "priced": 0, "stored": 0}
                    continue

                # ── Relevance filter: keep only products matching this item ──
                # Shared category URLs (e.g., /en/fruits for both Apple and Banana)
                # return ALL products in the category. We filter to keep only relevant ones.
                products = _filter_relevant_products(item_name, search_terms, products)

                # Filter out ready meals (they pollute produce/meat categories)
                before_filter = len(products)
                products = [p for p in products if not p.get("is_ready_meal", False)]
                filtered = before_filter - len(products)
                if filtered > 0:
                    print(f"  Filtered out {filtered} ready meal products ({before_filter} -> {len(products)})")

                if not products:
                    print(f"  WARNING: No relevant products found for {item_name}. Skipping.")
                    all_results[item_name] = {"products": 0, "priced": 0, "stored": 0}
                    continue

                # Score products
                scored = score_products(item_name, "Kibsons", products, conn=conn)

                # Store in knowledge base
                cur.execute("SELECT id FROM items WHERE name = ?", (item_name,))
                item_row = cur.fetchone()

                stored_count = 0
                if item_row:
                    ranked = discover_alternatives(
                        item_row["id"], item_name, store_id,
                        products, conn=conn
                    )
                    stored_count = len(ranked)

                    # Show top 3
                    alts = get_ranked_alternatives(item_row["id"], store_id, conn=conn)
                    print(f"\n  Top alternatives for {item_name}:")
                    for a in alts[:3]:
                        pref = " ** PREFERRED" if a["is_preferred"] else ""
                        print(f"    #{a['rank']} {a['product_name'][:50]} -- {a['price']} AED [{a['tier']}]{pref}")
                else:
                    print(f"  WARNING: {item_name} not found in items table.")

                priced = sum(1 for p in products if p.get("price"))
                all_results[item_name] = {
                    "products": len(products),
                    "priced": priced,
                    "stored": stored_count,
                }

                # Save per-item JSON
                safe_name = item_name.lower().replace(" ", "_")
                output_path = Path(__file__).parent / f"scrape_output_kibsons_{safe_name}.json"
                with open(output_path, "w", encoding="utf-8") as f:
                    json.dump(products, f, indent=2, ensure_ascii=False)

                print(f"  {len(products)} products | {priced} priced | {stored_count} ranked | saved to {output_path.name}")

            except Exception as item_err:
                print(f"\n  ERROR processing {item_name}: {item_err}")
                import traceback
                traceback.print_exc()
                all_results[item_name] = {"products": 0, "priced": 0, "stored": 0, "error": str(item_err)}
                # If the error looks like a dead browser, force relaunch on next iteration
                err_str = str(item_err).lower()
                if "target closed" in err_str or "connection closed" in err_str or "browser" in err_str:
                    print("  [RECOVERY] Browser error detected -- will relaunch for next item.")
                    p, context, page = await relaunch_browser(p, context)
                    category_cache.clear()
                continue

    except Exception as e:
        print(f"\n  FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()

    finally:
        try:
            await context.close()
            await p.stop()
        except Exception:
            pass
        conn.close()

    # ── Final summary ──
    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    print(f"\n\n{'='*70}")
    print(f"  KIBSONS FULL SCRAPE COMPLETE --{minutes}m {seconds}s")
    print(f"{'='*70}")
    print(f"  {'Item':<20} {'Products':>10} {'Priced':>10} {'Ranked':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10}")

    total_products = 0
    total_priced = 0
    total_ranked = 0
    for item_name, r in all_results.items():
        total_products += r["products"]
        total_priced += r["priced"]
        total_ranked += r["stored"]
        status = "OK" if r["priced"] > 0 else "FAIL"
        print(f"  {item_name:<20} {r['products']:>10} {r['priced']:>10} {r['stored']:>10}  {status}")

    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10}")
    print(f"  {'TOTAL':<20} {total_products:>10} {total_priced:>10} {total_ranked:>10}")
    print(f"\n  Category URLs cached: {len(category_cache)} (avoided duplicate scrapes)")
    print(f"  All JSON files saved to: {Path(__file__).parent}")
    print(f"\n  Done! Knowledge base is now populated for all Kibsons items.")


async def main():
    if "--all" in sys.argv:
        await scrape_all_items()
    elif "--search" in sys.argv:
        idx = sys.argv.index("--search")
        query = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "eggs"
        p, context, page = await launch_edge()
        products = await search_kibsons(page, query)
        print(json.dumps(products, indent=2, ensure_ascii=False))
        input("Press Enter to close...")
        await context.close()
        await p.stop()
    elif "--category" in sys.argv:
        idx = sys.argv.index("--category")
        cat_url = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else CATEGORY_URLS.get("Eggs", [""])[0]
        if not cat_url:
            print("ERROR: Provide a category URL")
            return
        p, context, page = await launch_edge()
        products = await browse_category(page, cat_url)
        print(json.dumps(products, indent=2, ensure_ascii=False))
        input("Press Enter to close...")
        await context.close()
        await p.stop()
    else:
        # Default: eggs test
        await scrape_eggs_test()


if __name__ == "__main__":
    asyncio.run(main())
