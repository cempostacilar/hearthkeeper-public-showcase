"""
AutoRestock - Carrefour UAE Scraper

Real browser scraper using Playwright + your Edge profile.
Two modes:
  1. CATEGORY BROWSING (preferred) --Navigate to a category page, click "Load More"
     to get the full product list, then scrape each product page for details.
     Unbiased, gives us the complete inventory for a product type.
  2. KEYWORD SEARCH (fallback) --Search by keyword, scrape results.
     Biased toward the search term, limited results.

IMPORTANT: Close Edge before running. Playwright can't share the profile.

Usage:
  python3 scrape_carrefour.py                          # scrape eggs via category (default)
  python3 scrape_carrefour.py --search "organic eggs"  # keyword search
  python3 scrape_carrefour.py --category URL           # browse a specific category URL
  python3 scrape_carrefour.py --item Eggs              # scrape by habit item name

Outputs scraped products as JSON and optionally feeds into knowledge base.
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from browser_common import launch_edge, check_browser_health, relaunch_browser

# Carrefour UAE base
BASE_URL = "https://www.carrefouruae.com/mafuae/en/"
SEARCH_URL = "https://www.carrefouruae.com/mafuae/en/v4/search?keyword={query}&lang=en&sortBy=relevance&currentPage=0&pageSize=20"

# ────────────────────────────────────────────
# Category URLs --unbiased browsing instead of keyword search
# ────────────────────────────────────────────
# These are the actual category pages on Carrefour UAE.
# Browsing a category gives us the FULL inventory for that product type,
# instead of search results biased by the keyword we type.
#
# Format: item_name -> list of category URLs to check (first match wins)
# Multiple URLs support subcategories: e.g., organic-eggs first, then all eggs
CATEGORY_URLS = {
    # Carrefour UAE uses /c/CODE category IDs, NOT the slug from product URLs.
    # Subcategory first (more specific), broader category as fallback.
    #
    # ── Dairy ──
    "Eggs": [
        "https://www.carrefouruae.com/mafuae/en/c/F1205090",   # Organic Eggs
        "https://www.carrefouruae.com/mafuae/en/c/F21630400",  # All Eggs
    ],
    "Cheese": [
        "https://www.carrefouruae.com/mafuae/en/c/F21630224",  # Cheddar Cheese
        "https://www.carrefouruae.com/mafuae/en/c/F21630200",  # Cheese & Labneh (all)
    ],
    "Yogurt": [
        "https://www.carrefouruae.com/mafuae/en/c/F21630614",  # Greek Yogurt
        "https://www.carrefouruae.com/mafuae/en/c/F21630600",  # Yoghurt (all)
    ],
    "Butter": [
        "https://www.carrefouruae.com/mafuae/en/c/F21630101",  # Unsalted Butter
        "https://www.carrefouruae.com/mafuae/en/c/F21630100",  # Butter & Margarine (all)
    ],
    "Milk": [
        "https://www.carrefouruae.com/mafuae/en/c/F21630500",  # Fresh Milk & Laban
    ],
    "Kefir": [
        "https://www.carrefouruae.com/mafuae/en/c/F21630512",  # Drinking Kefir
    ],

    # ── Produce ──
    "Apple": [
        "https://www.carrefouruae.com/mafuae/en/c/F11660100",  # Fruits (all fresh)
    ],
    "Banana": [
        "https://www.carrefouruae.com/mafuae/en/c/F11660100",  # Fruits (all fresh)
    ],
    "Cucumber": [
        "https://www.carrefouruae.com/mafuae/en/c/F1209000",   # Organic Fruits & Vegetables
        "https://www.carrefouruae.com/mafuae/en/c/F11660500",  # Fresh Vegetables (all)
    ],
    "Pepper": [
        "https://www.carrefouruae.com/mafuae/en/c/F1209000",   # Organic Fruits & Vegetables
        "https://www.carrefouruae.com/mafuae/en/c/F11660500",  # Fresh Vegetables (all)
    ],
    "Spinach": [
        "https://www.carrefouruae.com/mafuae/en/c/F11660500",  # Fresh Vegetables (all)
    ],
    "Rocket": [
        "https://www.carrefouruae.com/mafuae/en/c/F11660500",  # Fresh Vegetables (all)
    ],

    # ── Meat & Fish ──
    "Beef Mince": [
        "https://www.carrefouruae.com/mafuae/en/c/F1670214",   # Minced Meat
        "https://www.carrefouruae.com/mafuae/en/c/F1670200",   # Beef (all)
    ],
    "Beef Sausages": [
        "https://www.carrefouruae.com/mafuae/en/c/F1714504",   # Sausages
        "https://www.carrefouruae.com/mafuae/en/c/F1670200",   # Beef (all)
    ],
    "Striploin": [
        "https://www.carrefouruae.com/mafuae/en/c/F1670200",   # Beef (all)
    ],
    "Chicken Breast": [
        "https://www.carrefouruae.com/mafuae/en/c/F1670302",   # Chicken Breast
        "https://www.carrefouruae.com/mafuae/en/c/F1670300",   # Fresh Chicken (all)
    ],
    "Salmon": [
        "https://www.carrefouruae.com/mafuae/en/c/F1640100",   # Fish (all fresh)
        "https://www.carrefouruae.com/mafuae/en/c/F1640000",   # Fresh Fish & Seafood
    ],

    # ── Bakery & Pantry ──
    "Bread": [
        "https://www.carrefouruae.com/mafuae/en/c/F1610302",   # Brown Sliced Bread
        "https://www.carrefouruae.com/mafuae/en/c/F1610300",   # Bread & Rolls (all)
    ],
    "Pasta": [
        "https://www.carrefouruae.com/mafuae/en/c/F1701225",   # Healthy Pasta
        "https://www.carrefouruae.com/mafuae/en/c/F1701220",   # Pasta (all)
    ],
    "Olive Oil": [
        "https://www.carrefouruae.com/mafuae/en/c/F1760404",   # Olive Oil
    ],
}


async def search_carrefour(page, query, max_results=15):
    """
    Search Carrefour UAE and scrape products.

    Strategy:
      1. Navigate to search URL → get product URLs from the page
      2. Visit each product page to get accurate name, price, details
         (search result cards are unreliable on this React SPA —
          the DOM structure makes card-level extraction fragile)

    This is slower (~2s per product) but gives us rich, accurate data.
    For a one-time discovery of 15 products, ~30 seconds total.

    Returns list of product dicts.
    """
    print(f"\n  Searching Carrefour for: '{query}'")
    search_url = f"https://www.carrefouruae.com/mafuae/en/v4/search?keyword={query.replace(' ', '+')}&lang=en&sortBy=relevance&currentPage=0&pageSize={max_results + 5}"

    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    print(f"  Page loaded. Waiting for products to render...")

    # Wait for SPA to render
    await asyncio.sleep(5)

    # ── Step 1: Extract just the product URLs from search results ──
    # This is the only reliable thing we can get from the search page.
    # The actual product data comes from visiting each product page.
    product_urls = await page.evaluate("""
        (maxResults) => {
            const urls = [];
            const seen = new Set();
            const links = document.querySelectorAll('a[href*="/p/"]');

            for (const link of links) {
                if (urls.length >= maxResults) break;
                let href = link.getAttribute('href') || '';
                if (!href.includes('/p/')) continue;

                // Normalize: strip query params for dedup, keep full URL for navigation
                const baseHref = href.split('?')[0];
                if (seen.has(baseHref)) continue;
                seen.add(baseHref);

                if (href.startsWith('/')) {
                    href = 'https://www.carrefouruae.com' + href;
                }
                urls.push(href);
            }
            return urls;
        }
    """, max_results)

    print(f"  Found {len(product_urls)} unique product URLs")

    if not product_urls:
        print("  WARNING: No product URLs found.")
        await _debug_dump_page(page)
        return []

    # ── Step 2: Visit each product page for real data ──
    # This gives us accurate names, prices, descriptions, everything.
    products = []
    detail_page = await page.context.new_page()

    try:
        for i, url in enumerate(product_urls[:max_results]):
            print(f"    [{i+1}/{min(len(product_urls), max_results)}] Scraping: {url.split('/p/')[0].split('/')[-1][:40]}...")
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

    print(f"\n  Extracted {len(products)} products with full details")
    return products


async def browse_category(page, category_url, max_load_more=10):
    """
    Browse a Carrefour category page and get ALL products by clicking "Load More".

    This is the preferred approach for discovery --gives an unbiased,
    complete view of all products in a category. No keyword bias.

    Strategy:
      1. Navigate to category URL
      2. Click "Load More" / "Show More" until all products are visible
      3. Extract all product URLs
      4. Visit each product page for rich data (same as search approach)

    Args:
        page: Playwright page
        category_url: full URL to a Carrefour category page
        max_load_more: max times to click "Load More" (safety limit)

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
    # Carrefour shows ~20 products initially and has a "Load More" button.
    # We keep clicking until it disappears or we hit our safety limit.
    load_more_clicks = 0
    for attempt in range(max_load_more):
        # Count products BEFORE clicking
        count_before = await page.evaluate("""
            () => {
                const seen = new Set();
                const links = document.querySelectorAll('a[href*="/p/"]');
                for (const link of links) {
                    const href = (link.getAttribute('href') || '').split('?')[0];
                    if (href.includes('/p/')) seen.add(href);
                }
                return seen.size;
            }
        """)

        # Look for the "Load More" button --try multiple selectors
        load_more_btn = None
        load_more_selectors = [
            'button:has-text("Load More")',
            'button:has-text("load more")',
            'button:has-text("Show More")',
            'button:has-text("show more")',
            'button:has-text("View More")',
            '[data-testid*="load-more"]',
            '[data-testid*="loadMore"]',
            '[class*="loadMore"]',
            '[class*="load-more"]',
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
            # No more "Load More" button --we have all products
            print(f"  [OK] All products loaded (no more 'Load More' button)")
            break

        try:
            await load_more_btn.click()
            load_more_clicks += 1
            await asyncio.sleep(3)  # Wait for new products to render

            # Count products AFTER clicking
            count_after = await page.evaluate("""
                () => {
                    const seen = new Set();
                    const links = document.querySelectorAll('a[href*="/p/"]');
                    for (const link of links) {
                        const href = (link.getAttribute('href') || '').split('?')[0];
                        if (href.includes('/p/')) seen.add(href);
                    }
                    return seen.size;
                }
            """)
            new_products = count_after - count_before
            print(f"    Load More #{load_more_clicks}: {count_before} -> {count_after} products (+{new_products})")

            if new_products == 0:
                # Button exists but loaded nothing --we're done
                print(f"  [OK] No new products loaded -- reached end of category")
                break

        except Exception as e:
            print(f"    Load More click failed: {e}")
            break

    # ── Extract all product URLs from the fully-loaded page ──
    product_urls = await page.evaluate("""
        () => {
            const urls = [];
            const seen = new Set();
            const links = document.querySelectorAll('a[href*="/p/"]');

            for (const link of links) {
                let href = link.getAttribute('href') || '';
                if (!href.includes('/p/')) continue;

                const baseHref = href.split('?')[0];
                if (seen.has(baseHref)) continue;
                seen.add(baseHref);

                if (href.startsWith('/')) {
                    href = 'https://www.carrefouruae.com' + href;
                }
                urls.push(href);
            }
            return urls;
        }
    """)

    total_urls = len(product_urls)
    print(f"  Found {total_urls} unique product URLs (after {load_more_clicks} Load More clicks)")

    if not product_urls:
        print("  WARNING: No product URLs found in category.")
        await _debug_dump_page(page)
        return []

    # ── Visit each product page for real data ──
    products = []
    detail_page = await page.context.new_page()

    try:
        for i, url in enumerate(product_urls):
            slug = url.split('/p/')[0].split('/')[-1][:40] if '/p/' in url else url[-40:]
            print(f"    [{i+1}/{total_urls}] Scraping: {slug}...")
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

    print(f"  Extracted {len(products)} products with full details from category")
    return products


async def scrape_product_page(page, product_url, timeout=15000):
    """
    Scrape a single Carrefour product page for ALL available data.

    Carrefour UAE PDP structure:
      - H1: full product name (e.g., "Lurpak Unsalted Butter Block 200g")
      - Price in various React-rendered elements
      - Info boxes: Brand ("More From {brand}"), Origin, Pack Size
      - Accordion sections (must click to expand):
          Ingredients | Nutrition Facts | Information
      - Image from mafrservices CDN

    Returns dict with: name, price, size, brand, description, ingredients,
    nutrition_info, origin_country, is_organic, category, product_url, image_url
    """
    product = {"product_url": product_url}

    try:
        await page.goto(product_url, wait_until="domcontentloaded", timeout=timeout)
        await asyncio.sleep(3)  # SPA needs time to render price + details

        # ── Step 1: Extract basic info (name, price, image, size, breadcrumb) ──
        data = await page.evaluate("""
            () => {
                const result = {};

                // ── Name: h1 ──
                const h1 = document.querySelector('h1');
                if (h1) result.name = h1.innerText?.trim() || '';

                // ── PRICE EXTRACTION (5 strategies in order of reliability) ──

                // Strategy 1: JSON-LD structured data (most reliable --used for Google SEO)
                const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
                for (const script of ldScripts) {
                    try {
                        const ld = JSON.parse(script.textContent);
                        const items = Array.isArray(ld) ? ld : [ld];
                        for (const item of items) {
                            if (item['@type'] === 'Product' && item.offers) {
                                const offers = Array.isArray(item.offers) ? item.offers : [item.offers];
                                for (const offer of offers) {
                                    const p = parseFloat(offer.price || offer.lowPrice);
                                    if (p > 0) { result.price = p; break; }
                                }
                            }
                            if (item['@graph']) {
                                for (const node of item['@graph']) {
                                    if (node['@type'] === 'Product' && node.offers) {
                                        const offers = Array.isArray(node.offers) ? node.offers : [node.offers];
                                        for (const offer of offers) {
                                            const p = parseFloat(offer.price || offer.lowPrice);
                                            if (p > 0) { result.price = p; break; }
                                        }
                                    }
                                }
                            }
                            if (result.price) break;
                        }
                    } catch(e) {}
                    if (result.price) break;
                }

                // Strategy 2: Meta tags (og:price, product:price)
                if (!result.price) {
                    const priceMeta = document.querySelector(
                        'meta[property="product:price:amount"], meta[property="og:price:amount"], meta[name="price"]'
                    );
                    if (priceMeta) {
                        const p = parseFloat(priceMeta.getAttribute('content'));
                        if (p > 0) result.price = p;
                    }
                }

                // Strategy 3: data-testid price elements
                if (!result.price) {
                    const testIdEls = document.querySelectorAll(
                        '[data-testid*="price"], [data-testid*="Price"], [data-testid*="cost"]'
                    );
                    for (const el of testIdEls) {
                        const text = el.innerText?.replace(/\\s+/g, ' ').trim() || '';
                        const m = text.match(/(\\d+)\\s*\\.\\s*(\\d{2})/);
                        if (m) { result.price = parseFloat(m[1] + '.' + m[2]); break; }
                        const m2 = text.match(/(\\d+\\.\\d{2})/);
                        if (m2) { result.price = parseFloat(m2[1]); break; }
                    }
                }

                // Strategy 4: CSS class price elements
                if (!result.price) {
                    const priceEls = document.querySelectorAll(
                        '[class*="price"], [class*="Price"], [class*="cost"], [class*="amount"]'
                    );
                    for (const el of priceEls) {
                        const text = el.innerText?.replace(/\\s+/g, ' ').trim() || '';
                        if (!text || text.length > 100) continue;
                        const m = text.match(/(\\d+)\\s*\\.\\s*(\\d{2})\\s*AED/);
                        if (m) { result.price = parseFloat(m[1] + '.' + m[2]); break; }
                        const m2 = text.match(/AED\\s*(\\d+[\\.,]\\d{2})/);
                        if (m2) { result.price = parseFloat(m2[1].replace(',','.')); break; }
                        const spans = el.querySelectorAll('span');
                        if (spans.length >= 2) {
                            const combined = [...spans].map(s => s.textContent?.trim()).join('');
                            const m3 = combined.match(/(\\d+\\.\\d{2})/);
                            if (m3) { result.price = parseFloat(m3[1]); break; }
                        }
                        if (text.length < 30) {
                            const m4 = text.match(/(\\d+\\.\\d{2})/);
                            if (m4) { result.price = parseFloat(m4[1]); break; }
                        }
                    }
                }

                // Strategy 5: Full page text scan for "AED" near numbers
                if (!result.price) {
                    const bodyText = document.body.innerText || '';
                    const patterns = [
                        /(\\d+\\.\\d{2})\\s*AED/,
                        /AED\\s*(\\d+\\.\\d{2})/,
                        /(\\d+)\\s*\\.\\s*(\\d{2})\\s*AED/,
                    ];
                    for (const pat of patterns) {
                        const m = bodyText.match(pat);
                        if (m) {
                            result.price = m[2]
                                ? parseFloat(m[1] + '.' + m[2])
                                : parseFloat(m[1]);
                            break;
                        }
                    }
                }

                if (result.price) result._price_source = 'js_extraction';

                // ── Size/Weight from product name ──
                if (result.name) {
                    const sizeMatch = result.name.match(/(\\d+\\s*(?:PCS|pcs|Pcs|pc|g|kg|Kg|ml|ML|L|ltr|oz|pack|Pack|Pieces|pieces)\\b[^,]*)/i);
                    if (sizeMatch) result.size = sizeMatch[0].trim();
                }
                const weightEls = document.querySelectorAll('[class*="weight"], [class*="size"], [class*="unit"]');
                for (const el of weightEls) {
                    const t = el.innerText?.trim() || '';
                    if (t && t.length < 50 && t.match(/\\d/)) {
                        result.size = result.size || t;
                        break;
                    }
                }

                // ── Image ──
                const mainImg = document.querySelector('[class*="product"] img, [class*="gallery"] img, [class*="image"] img');
                if (mainImg) result.image_url = mainImg.src || mainImg.getAttribute('data-src') || '';
                if (!result.image_url) {
                    const anyImg = document.querySelector('img[src*="mafrservices"], img[src*="product"]');
                    if (anyImg) result.image_url = anyImg.src || '';
                }

                // ── Category breadcrumb ──
                const breadcrumb = document.querySelector('[class*="breadcrumb"], [class*="Breadcrumb"], nav[aria-label*="bread"]');
                if (breadcrumb) result.category = breadcrumb.innerText?.trim()?.substring(0, 200) || '';

                // ── Brand: "More From {brand}" link ──
                const allLinks = document.querySelectorAll('a');
                for (const link of allLinks) {
                    const text = link.innerText?.trim() || '';
                    const m = text.match(/^More From\\s+(.+)/i);
                    if (m) {
                        result.brand = m[1].trim();
                        break;
                    }
                }

                // ── Info boxes above accordions: Origin, Pack Size, Brand ──
                // These are small boxed sections with label + value
                const infoBoxes = document.querySelectorAll('[class*="rounded"] span, [class*="info"] span, p, div');
                let foundOrigin = false;
                let foundPackSize = false;
                for (const el of infoBoxes) {
                    const t = el.innerText?.trim() || '';
                    if (t.length > 100) continue;

                    // "Country of Origin\\nFrance" or "Origin\\nFrance"
                    if (!foundOrigin && /country.*origin|^origin$/i.test(t)) {
                        // Value is in next sibling or child
                        const next = el.nextElementSibling || el.parentElement?.querySelector(':last-child');
                        if (next && next !== el) {
                            const val = next.innerText?.trim();
                            if (val && val.length < 50) { result.origin_country = val; foundOrigin = true; }
                        }
                    }

                    // Look for "Country of Origin: France" in a single line
                    const originMatch = t.match(/(?:country.*origin|^origin)[:\\s]+([A-Za-z\\s]+)/i);
                    if (!foundOrigin && originMatch) {
                        result.origin_country = originMatch[1].trim();
                        foundOrigin = true;
                    }

                    // Pack size
                    if (!foundPackSize) {
                        const packMatch = t.match(/(?:pack\\s*size|net\\s*(?:weight|content))[:\\s]+(.+)/i);
                        if (packMatch) {
                            result.size = result.size || packMatch[1].trim();
                            foundPackSize = true;
                        }
                    }
                }

                return result;
            }
        """)

        for key, val in data.items():
            if val is not None and val != '':
                product[key] = val

        # ── Step 2: Click accordion sections to get Ingredients, Nutrition, Info ──
        # Carrefour PDP has expandable accordion sections with buttons.
        # IMPORTANT: Only target accordions in the MAIN product detail area,
        # not in "recommended products" or other sections further down the page.
        # We scroll to the accordion area first, then click only the FIRST
        # occurrence of each section type.
        accordion_data = await page.evaluate("""
            async () => {
                const result = {};

                // Strategy: Find the product detail container that holds the
                // accordion sections. On Carrefour, these are typically the first
                // set of expandable sections BELOW the price/add-to-cart area.
                // We limit to buttons within the first 2000px of page height
                // to avoid hitting "recommended products" sections.
                const buttons = document.querySelectorAll('button.w-full, button[class*="accordion"], [class*="rounded-lg"] > button');
                const seen = new Set();

                for (const btn of buttons) {
                    // Skip buttons that are too far down the page (likely recommended products)
                    const rect = btn.getBoundingClientRect();
                    const scrollY = window.scrollY || document.documentElement.scrollTop;
                    const absTop = rect.top + scrollY;
                    if (absTop > 3000) continue;  // Skip anything beyond ~3000px from top

                    const label = btn.innerText?.trim() || '';
                    if (!label || label.length > 50 || seen.has(label)) continue;
                    seen.add(label);

                    const labelLow = label.toLowerCase();

                    // Match known section names (exact or close match)
                    let key = null;
                    if (labelLow === 'ingredients' || labelLow === 'ingredient list') key = 'ingredients';
                    else if (labelLow === 'nutrition facts' || labelLow === 'nutritional information' || labelLow === 'nutrition') key = 'nutrition_info';
                    else if (labelLow === 'information' || labelLow === 'product information') key = 'information';
                    else if (labelLow === 'description' || labelLow === 'product description') key = 'description';
                    else continue;  // skip unknown sections

                    // Only take the FIRST of each type (avoid duplicates from recommended products)
                    if (result[key]) continue;

                    try {
                        // Scroll into view and click
                        btn.scrollIntoView({ block: 'center' });
                        await new Promise(r => setTimeout(r, 200));
                        btn.click();
                        await new Promise(r => setTimeout(r, 400));

                        // Read the expanded content from the parent container
                        const parent = btn.closest('[class*="rounded"]') || btn.parentElement;
                        if (!parent) continue;

                        const allText = parent.innerText?.trim() || '';
                        let content = allText;
                        if (content.startsWith(label)) {
                            content = content.substring(label.length).trim();
                        }

                        if (content.length > 3) {
                            result[key] = content.substring(0, 2000);
                        }
                    } catch(e) {}
                }

                return result;
            }
        """)

        # Store accordion content
        if accordion_data.get("description"):
            product.setdefault("description", accordion_data["description"])
        if accordion_data.get("ingredients"):
            product["ingredients"] = accordion_data["ingredients"]
        if accordion_data.get("nutrition_info"):
            product["nutrition_info"] = accordion_data["nutrition_info"]
        if accordion_data.get("information"):
            # The "Information" section has storage conditions, allergy info, marketing
            product["product_info_raw"] = accordion_data["information"]
            # Try to extract origin from info if not found yet
            if not product.get("origin_country"):
                m = re.search(r'(?:country.*origin|origin)[:\s]+([A-Za-z\s]+)', accordion_data["information"], re.IGNORECASE)
                if m:
                    product["origin_country"] = m.group(1).strip()

        # ── Step 3: Post-processing ──
        _post_process_carrefour(product)

    except Exception as e:
        product["scrape_error"] = str(e)

    return product


def _post_process_carrefour(product):
    """
    Post-process Carrefour product data:
      - Brand extraction and cleanup
      - Organic detection
      - Ready meal detection
    """
    # ── Brand cleanup ──
    brand = product.get("brand", "")

    # Strip "More From" prefix if it leaked through
    if brand.lower().startswith("more from"):
        brand = brand[9:].strip()

    # Fix: "Marketing Message\n..." pollution --the brand regex captured
    # "Brand Marketing Message\n{long description}" from the Information accordion.
    # If brand contains "marketing message" or is too long, discard it.
    if "marketing message" in brand.lower() or len(brand) > 60:
        brand = ""

    # If brand contains newlines, take only the first line
    if "\n" in brand:
        brand = brand.split("\n")[0].strip()

    product["brand"] = brand if brand else ""

    # ── Fallback brand: extract from product name ──
    # Carrefour names are typically "Brand Product Description Size"
    # e.g., "Lurpak Unsalted Butter Block 200g" → "Lurpak"
    # e.g., "Almarai Greek Yoghurt 150g" → "Almarai"
    # We use the first word(s) that don't look like a product type.
    if not product.get("brand") and product.get("name"):
        product["brand"] = _extract_brand_from_name(product["name"])

    # ── Clean product_info_raw: strip "Brand Marketing Message" section ──
    raw = product.get("product_info_raw", "")
    if raw:
        # Remove the marketing message fluff, keep storage/allergy info
        lines = raw.split("\n")
        clean_lines = []
        skip = False
        for line in lines:
            if "brand marketing message" in line.lower():
                skip = True  # skip this line and everything after it
                continue
            if skip and line.strip() and not any(kw in line.lower() for kw in
                    ["storage", "allergy", "package", "flavor", "flavour"]):
                continue  # still in marketing message
            if skip and any(kw in line.lower() for kw in
                    ["storage", "allergy", "package", "flavor", "flavour"]):
                skip = False  # new useful section
            clean_lines.append(line)
        product["product_info_raw"] = "\n".join(clean_lines).strip()
        if not product["product_info_raw"]:
            del product["product_info_raw"]

    # ── Organic detection ──
    all_text = " ".join([
        product.get("name", ""),
        product.get("description", ""),
        product.get("ingredients", ""),
        product.get("product_url", ""),
    ])
    product["is_organic"] = bool(
        re.search(r'\borganic\b', all_text, re.IGNORECASE)
        and not re.search(r'non.organic', all_text, re.IGNORECASE)
    )

    # ── Ready meal detection ──
    ready_meal_signals = ["ready to eat", "ready meal", "meal kit", "heat and eat",
                          "ready to cook", "microwavable", "instant meal"]
    product_text = (product.get("name", "") + " " + product.get("description", "")).lower()
    product["is_ready_meal"] = any(sig in product_text for sig in ready_meal_signals)


def _extract_brand_from_name(name):
    """
    Extract brand from Carrefour product name.

    Carrefour names typically start with the brand:
      "Lurpak Unsalted Butter Block 200g" → "Lurpak"
      "Almarai Plain Greek Style Yoghurt 150g" → "Almarai"
      "President Cheese Cheddar" → "President"
      "Nada Plain Greek Yoghurt 360g" → "Nada"
      "Celtic Red Cheddar Cheese" → "Celtic"
      "Royal Gala Apple" → "" (no brand, just product)

    Strategy: Take words before the first product-type word.
    """
    if not name:
        return ""

    # Product-type words --if we see these, everything before is the brand
    product_words = {
        # Dairy
        'yoghurt', 'yogurt', 'cheese', 'cheddar', 'mozzarella', 'feta',
        'halloumi', 'butter', 'milk', 'cream', 'laban', 'kefir', 'labneh',
        'eggs', 'egg',
        # Meat
        'beef', 'chicken', 'lamb', 'turkey', 'mince', 'minced', 'sausage',
        'sausages', 'breast', 'fillet', 'steak', 'striploin', 'salmon',
        # Produce --these ARE the product, no brand prefix
        'apple', 'banana', 'cucumber', 'pepper', 'spinach', 'rocket',
        'tomato', 'potato', 'onion', 'carrot', 'lettuce', 'kale',
        # Bakery & Pantry
        'bread', 'loaf', 'pasta', 'spaghetti', 'penne', 'fusilli',
        'oil', 'olive', 'vinegar',
        # Generic
        'organic', 'fresh', 'natural', 'plain', 'full', 'low', 'fat',
        'free', 'range', 'unsalted', 'salted', 'sliced', 'shredded',
        'whole', 'semi', 'skimmed', 'greek', 'style',
    }

    words = name.split()
    brand_parts = []

    for word in words:
        clean = re.sub(r'[,.]$', '', word)  # strip trailing punctuation
        if clean.lower() in product_words:
            break
        # Stop at numbers (usually size like "150g", "200g")
        if re.match(r'^\d', clean):
            break
        brand_parts.append(word)

    # Don't return single generic words as brand
    if len(brand_parts) == 1 and brand_parts[0].lower() in {'the', 'a', 'uae', 'bio'}:
        return ""

    # Don't return more than 3 words (too many = probably not a brand)
    if len(brand_parts) > 3:
        brand_parts = brand_parts[:2]

    return " ".join(brand_parts) if brand_parts else ""



async def _debug_dump_page(page):
    """Dump page structure to help find the right selectors."""
    print("\n  === DEBUG: Page structure ===")

    # Check what data-testid attributes exist
    testids = await page.evaluate("""
        () => {
            const els = document.querySelectorAll('[data-testid]');
            return [...new Set([...els].map(e => e.getAttribute('data-testid')))].slice(0, 30);
        }
    """)
    print(f"  data-testid values found: {testids}")

    # Check for product-like links
    links = await page.evaluate("""
        () => {
            const els = document.querySelectorAll('a[href*="/p/"]');
            return els.length;
        }
    """)
    print(f"  Links with /p/ (product pages): {links}")

    # Check for any price-like elements
    prices = await page.evaluate("""
        () => {
            const els = document.querySelectorAll('[class*="price"], [class*="Price"]');
            return els.length;
        }
    """)
    print(f"  Price-like elements: {prices}")

    # Get first 5 product links if they exist
    sample_links = await page.evaluate("""
        () => {
            const els = document.querySelectorAll('a[href*="/p/"]');
            return [...els].slice(0, 5).map(e => ({
                href: e.href,
                text: e.innerText.trim().substring(0, 80),
                classes: e.className.substring(0, 60),
                parent: e.parentElement?.className?.substring(0, 60) || '',
            }));
        }
    """)
    for link in sample_links:
        print(f"    Link: {link['text'][:50]} -> {link['href'][:80]}")
        print(f"      classes: {link['classes'][:60]}")
        print(f"      parent: {link['parent'][:60]}")

    print("  === END DEBUG ===\n")


async def scrape_eggs_test():
    """
    Single test run: browse organic eggs category on Carrefour,
    scrape each product page, score and rank, store in knowledge base.

    Uses CATEGORY BROWSING (not keyword search) for unbiased results.
    Falls back to keyword search if category browsing fails.
    """
    from consumption_habits import HABITS
    from db_setup import create_database

    # Get eggs config
    eggs_habit = next((h for h in HABITS if h["name"] == "Eggs"), None)
    if not eggs_habit:
        print("ERROR: Eggs not found in HABITS")
        return

    search_terms = eggs_habit.get("search_terms", {}).get("Carrefour UAE", ["organic eggs"])
    category_urls = CATEGORY_URLS.get("Eggs", [])

    print("=" * 60)
    print("  Carrefour UAE Scraper --Eggs Test Run")
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

        # ── Strategy 1: Browse category page (preferred --unbiased, complete) ──
        for cat_url in category_urls:
            products = await browse_category(page, cat_url)
            if products:
                break
            print(f"  Category {cat_url} returned no products. Trying next...")

        # ── Strategy 2: Fall back to keyword search if category failed ──
        if not products:
            print(f"\n  Category browsing found nothing. Falling back to keyword search...")
            for search_term in search_terms:
                products = await search_carrefour(page, search_term, max_results=30)
                if products:
                    break

        if not products:
            print("\n  No products found with any search term.")
            print("  The page might use different selectors. Check the browser window.")
            input("  Press Enter to close...")
            await context.close()
            await p.stop()
            return

        # Score all products
        print(f"\n{'='*60}")
        print("  Scoring products against Eggs preference profile...")
        print(f"{'='*60}")

        from product_score import score_products

        # Connect to DB so scoring can use purchase history
        create_database()
        import sqlite3 as _sqlite3
        from db_setup import DB_PATH as _DB_PATH
        score_conn = _sqlite3.connect(_DB_PATH)
        score_conn.row_factory = _sqlite3.Row

        scored = score_products("Eggs", "Carrefour UAE", products, conn=score_conn)
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

        # Phase 4: Store in knowledge base
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

        cur.execute("SELECT id FROM stores WHERE name = 'Carrefour UAE'")
        store_row = cur.fetchone()
        cur.execute("SELECT id FROM items WHERE name = 'Eggs'")
        item_row = cur.fetchone()

        if store_row and item_row:
            ranked = discover_alternatives(
                item_row["id"], "Eggs", store_row["id"],
                products, conn=conn
            )
            print(f"  Stored {len(products)} products in catalog")
            print(f"  Ranked {len(ranked)} alternatives for Eggs @ Carrefour")

            # Show final rankings from knowledge base
            from product_knowledge import get_ranked_alternatives
            alts = get_ranked_alternatives(item_row["id"], store_row["id"], conn=conn)
            print(f"\n  Knowledge base now has {len(alts)} ranked alternatives:")
            for a in alts:
                avail = "[Y]" if a["is_available"] else "[N]"
                pref = " ** PREFERRED" if a["is_preferred"] else ""
                print(f"    {avail} #{a['rank']} {a['product_name'][:50]} --{a['price']} AED [{a['tier']}]{pref}")
        else:
            print("  WARNING: Eggs or Carrefour not found in DB. Run consumption_habits.py first.")

        conn.close()

        # Save raw scraped data as JSON for debugging
        output_path = Path(__file__).parent / "scrape_output_eggs.json"
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

    When scraping shared category pages (e.g., /fruits-vegetables for "Apple"),
    we get ALL fruits/veg. This filters to keep only apple-related products.

    Uses item name + search term keywords to match against product name,
    description, and URL. Generous matching --keeps anything plausibly related.
    """
    # Build keyword set from item name and search terms
    keywords = set()
    for word in item_name.lower().split():
        if len(word) > 2:
            keywords.add(word)
    for term in search_terms:
        for word in term.lower().split():
            if len(word) > 2:
                keywords.add(word)

    # Remove generic words that would match everything
    generic = {'fresh', 'organic', 'free', 'range', 'farm', 'the', 'and', 'for',
               'natural', 'premium', 'best', 'good', 'quality', 'local', 'pack',
               'from', 'with', 'uae', 'dubai', 'carrefour'}
    keywords -= generic

    if not keywords:
        return products  # no keywords to filter by

    matched = []
    for p in products:
        # Check product name, URL, description, and category
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
    Batch scrape ALL items from Carrefour UAE.

    Scrapes every category, scores products, stores in knowledge base.
    Smart about shared URLs --e.g., /c/F11660100 (Fruits) is scraped once
    and reused for both Apple and Banana scoring.

    Run with: python3 scrape_carrefour.py --all
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
    cur.execute("SELECT id FROM stores WHERE name = 'Carrefour UAE'")
    store_row = cur.fetchone()
    if not store_row:
        print("ERROR: Carrefour UAE not found in stores table. Run consumption_habits.py first.")
        return
    store_id = store_row["id"]

    # Track results
    all_results = {}
    category_cache = {}  # URL → products (avoid re-scraping shared URLs)
    start_time = time.time()

    print("=" * 70)
    print("  CARREFOUR UAE FULL BASELINE SCRAPE")
    print(f"  {len(HABITS)} items to process")
    print("=" * 70)

    # Launch browser
    print("\n  Launching Edge with your profile...")
    p, context, page = await launch_edge()

    try:
        for item_idx, habit in enumerate(HABITS, 1):
            item_name = habit["name"]
            search_terms = habit.get("search_terms", {}).get("Carrefour UAE", [item_name.lower()])
            category_urls = CATEGORY_URLS.get(item_name, [])

            print(f"\n{'='*70}")
            print(f"  [{item_idx}/{len(HABITS)}] {item_name}")
            print(f"{'='*70}")

            # ── Browser health check before each item ──
            if not await check_browser_health(page):
                p, context, page = await relaunch_browser(p, context)
                # Clear cache since the old page tabs are gone
                category_cache.clear()

            try:
                products = []

                # Strategy 1: Browse category pages
                if category_urls:
                    for cat_url in category_urls:
                        # Check cache -- avoid re-scraping shared URLs
                        if cat_url in category_cache:
                            cached = category_cache[cat_url]
                            print(f"  (cached) Using {len(cached)} products from {cat_url.split('/c/')[-1]}")
                            products = cached
                            break

                        cat_products = await browse_category(page, cat_url)
                        if cat_products:
                            category_cache[cat_url] = cat_products
                            products = cat_products
                            break
                        print(f"  Category {cat_url.split('/c/')[-1]} returned 0 products. Trying next...")

                # Strategy 2: Keyword search fallback
                if not products:
                    print(f"  Falling back to keyword search...")
                    for term in search_terms:
                        products = await search_carrefour(page, term, max_results=30)
                        if products:
                            break

                if not products:
                    print(f"  WARNING: No products found for {item_name}. Skipping.")
                    all_results[item_name] = {"products": 0, "priced": 0, "stored": 0}
                    continue

                # ── Relevance filter: keep only products matching this item ──
                products = _filter_relevant_products(item_name, search_terms, products)

                # Filter out ready meals
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
                scored = score_products(item_name, "Carrefour UAE", products, conn=conn)

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
                output_path = Path(__file__).parent / f"scrape_output_carrefour_{safe_name}.json"
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
    print(f"  CARREFOUR UAE FULL SCRAPE COMPLETE --{minutes}m {seconds}s")
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
    print(f"\n  Done! Knowledge base is now populated for all Carrefour items.")


async def scrape_retry_failed():
    """
    Re-scrape only items that have 0 Carrefour alternatives in the DB.

    Checks the knowledge base for missing items and only scrapes those,
    using a fresh browser page between items for stability.

    Run with: python3 scrape_carrefour.py --retry-failed
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
    cur.execute("SELECT id FROM stores WHERE name = 'Carrefour UAE'")
    store_row = cur.fetchone()
    if not store_row:
        print("ERROR: Carrefour UAE not found in stores table. Run consumption_habits.py first.")
        return
    store_id = store_row["id"]

    # Find items with 0 Carrefour alternatives
    failed_items = []
    for habit in HABITS:
        item_name = habit["name"]
        cur.execute("SELECT id FROM items WHERE name = ?", (item_name,))
        item_row = cur.fetchone()
        if not item_row:
            failed_items.append(habit)
            continue
        cur.execute("""
            SELECT COUNT(*) as cnt FROM item_alternatives
            WHERE item_id = ? AND store_id = ?
        """, (item_row["id"], store_id))
        count = cur.fetchone()["cnt"]
        if count == 0:
            failed_items.append(habit)

    if not failed_items:
        print("\n  All items have Carrefour alternatives. Nothing to retry.")
        conn.close()
        return

    print("=" * 70)
    print("  CARREFOUR UAE RETRY FAILED ITEMS")
    print(f"  {len(failed_items)} items need scraping:")
    for h in failed_items:
        print(f"    - {h['name']}")
    print("=" * 70)

    # Launch browser
    print("\n  Launching Edge with your profile...")
    p, context, page = await launch_edge()
    all_results = {}
    category_cache = {}
    start_time = time.time()

    try:
        for item_idx, habit in enumerate(failed_items, 1):
            item_name = habit["name"]
            search_terms = habit.get("search_terms", {}).get("Carrefour UAE", [item_name.lower()])
            category_urls = CATEGORY_URLS.get(item_name, [])

            print(f"\n{'='*70}")
            print(f"  [{item_idx}/{len(failed_items)}] {item_name}")
            print(f"{'='*70}")

            # Browser health check
            if not await check_browser_health(page):
                print("  [RECOVERY] Browser unhealthy, relaunching...")
                p, context, page = await relaunch_browser(p, context)
                category_cache.clear()

            try:
                products = []

                # Strategy 1: Browse category pages
                if category_urls:
                    for cat_url in category_urls:
                        if cat_url in category_cache:
                            cached = category_cache[cat_url]
                            print(f"  (cached) Using {len(cached)} products from {cat_url.split('/c/')[-1]}")
                            products = cached
                            break

                        print(f"  Trying category: {cat_url.split('/c/')[-1]}...")
                        cat_products = await browse_category(page, cat_url)
                        if cat_products:
                            category_cache[cat_url] = cat_products
                            products = cat_products
                            break
                        print(f"  Category returned 0 products. Trying next...")

                # Strategy 2: Keyword search fallback
                if not products:
                    print(f"  Falling back to keyword search...")
                    for term in search_terms:
                        print(f"    Searching: '{term}'...")
                        products = await search_carrefour(page, term, max_results=30)
                        if products:
                            break

                if not products:
                    print(f"  WARNING: No products found for {item_name}. Skipping.")
                    all_results[item_name] = {"products": 0, "priced": 0, "stored": 0}
                    continue

                # Relevance filter
                products = _filter_relevant_products(item_name, search_terms, products)

                # Filter out ready meals
                before_filter = len(products)
                products = [p for p in products if not p.get("is_ready_meal", False)]
                filtered = before_filter - len(products)
                if filtered > 0:
                    print(f"  Filtered out {filtered} ready meal products")

                if not products:
                    print(f"  WARNING: No relevant products after filtering for {item_name}.")
                    all_results[item_name] = {"products": 0, "priced": 0, "stored": 0}
                    continue

                # Score products
                scored = score_products(item_name, "Carrefour UAE", products, conn=conn)

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
                output_path = Path(__file__).parent / f"scrape_output_carrefour_{safe_name}.json"
                with open(output_path, "w", encoding="utf-8") as f:
                    json.dump(products, f, indent=2, ensure_ascii=False)

                print(f"  {len(products)} products | {priced} priced | {stored_count} ranked")

            except Exception as item_err:
                print(f"\n  ERROR processing {item_name}: {item_err}")
                import traceback
                traceback.print_exc()
                all_results[item_name] = {"products": 0, "priced": 0, "stored": 0, "error": str(item_err)}
                err_str = str(item_err).lower()
                if "target closed" in err_str or "connection closed" in err_str or "browser" in err_str:
                    print("  [RECOVERY] Browser error detected -- will relaunch.")
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

    # Final summary
    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    print(f"\n\n{'='*70}")
    print(f"  CARREFOUR RETRY COMPLETE -- {minutes}m {seconds}s")
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


async def main():
    # Parse args
    if "--retry-failed" in sys.argv:
        await scrape_retry_failed()
    elif "--all" in sys.argv:
        await scrape_all_items()
    elif "--search" in sys.argv:
        idx = sys.argv.index("--search")
        query = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "organic eggs"
        p, context, page = await launch_edge()
        products = await search_carrefour(page, query)
        print(json.dumps(products, indent=2, ensure_ascii=False))
        input("Press Enter to close...")
        await context.close()
        await p.stop()
    elif "--category" in sys.argv:
        idx = sys.argv.index("--category")
        cat_url = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else CATEGORY_URLS.get("Eggs", [""])[0]
        if not cat_url:
            print("ERROR: Provide a category URL or define one in CATEGORY_URLS")
            return
        p, context, page = await launch_edge()
        products = await browse_category(page, cat_url)
        print(json.dumps(products, indent=2, ensure_ascii=False))
        input("Press Enter to close...")
        await context.close()
        await p.stop()
    else:
        # Default: eggs test (uses category browsing)
        await scrape_eggs_test()


if __name__ == "__main__":
    asyncio.run(main())
