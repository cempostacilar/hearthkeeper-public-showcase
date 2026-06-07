# Kibsons Scraping Architecture v2 — RSC-Based Full Inventory

## Problem Statement

The current Kibsons scraper visits **individual product pages** via Playwright,
clicking through tabs and parsing DOM elements. This approach:

- Is slow: ~3 seconds per product = **60 minutes for 1,200 products**
- Captures only **20 item families** out of ~120 available
- Misses structured data: no category hierarchy, no organic classification, no
  popularity scores, no transport/cold-chain data
- Relies on brittle heuristics: ALL CAPS parsing for brand/origin, regex for
  organic detection, tab-clicking for ingredients
- Returns **zero** for the `category` column (all 1,186 current products)

**Target:** Scrape the full Kibsons grocery inventory (~8,000-10,000 products)
with rich structured data in under 10 minutes.

---

## Discovery: React Server Components (RSC) Data Layer

Kibsons runs **Next.js App Router** with React Server Components. Every
category/family page fetched with the `RSC: 1` header returns the full product
dataset as a structured RSC payload — no DOM rendering needed.

```
GET https://www.kibsons.com/en/dairy/eggs
Headers: { RSC: 1, Next-Router-State-Tree: [...] }

→ Returns: RSC stream containing JSON product objects (100 per page)
```

### What we get per product (~100 fields):

| RSC Field | What it is | Current scraper equivalent |
|---|---|---|
| `id` | Internal product ID | (not captured) |
| `stockCode` | SKU (e.g., "ORGBAEEGG06L1") | parsed from URL |
| `stockDesc` | Product name | `name` |
| `stockRate` | Regular price | `price` (heuristic parse) |
| `stockPromotionRate` | Sale price (0 if none) | (missed often) |
| `stockUnits` | Unit label ("Per Pack", "Per KG") | (not captured) |
| `brandDesc` | Brand name | `brand` (ALL CAPS heuristic) |
| `stockOrigin` | Brand alt text | (not captured) |
| `stockShortDetail` | Size/variant ("6 Pack", "500ml") | `size` (regex parse) |
| `originDesc` | Country of origin | `origin_country` (heuristic) |
| `classification` | `[{"desc":"Organic"}]` | `is_organic` (text + badge heuristic) |
| `featured` | Marketing tags: Organic, Eco, etc. | (not captured) |
| `smallIconOne/Two/Three` | Badge image URLs | (badge detection added recently) |
| `categories` | Full hierarchy: Dairy > Eggs | `category` (**always NULL**) |
| `categories_nested` | Nested JSON tree | (not captured) |
| `productImages` | Detail + display URLs | `image_url` (single) |
| `keywords` | Search keywords | (not captured) |
| `filters` | Filter facets | (not captured) |
| `pl_sku_popularity_score` | Popularity 0-100 | (not captured) |
| `pl_product_popularity_score` | Product popularity | (not captured) |
| `transportRoute` | "CHILLED", "AMBIENT", "FROZEN" | (not captured) |
| `pl_is_fresh` | Fresh produce flag | (not captured) |
| `plStockSubscription` | Subscription eligible | (not captured) |
| `suggest` / `search_suggestions` | Related search terms | (not captured) |

### Key wins over DOM scraping:

1. **`classification`** gives explicit organic/eco/halal tags — no heuristics
2. **`categories`** provides the full hierarchy — no more NULL category column
3. **`brandDesc`** is clean text — no ALL CAPS parsing
4. **`stockRate`/`stockPromotionRate`** are actual numbers — no regex extraction
5. **`transportRoute`** tells us cold-chain requirements — critical for delivery
6. **Popularity scores** enable smart ranking without purchase history

---

## Constraint: Cloudflare WAF

Kibsons sits behind Cloudflare. Raw HTTP requests (curl, requests, httpx,
headless Playwright) all get **403 blocked**. Only a real browser profile with
an established Cloudflare session can fetch pages.

**Implication:** We must use Playwright with the real Edge profile
(`HEARTHKEEPER_PROFILE`), running in headed mode (`headless=False`). This is
already how the current scraper works — no change needed for the browser layer.

The RSC approach still needs a browser — we use `page.evaluate(fetch(...))` to
make RSC requests from within the authenticated browser context, inheriting
Cloudflare cookies automatically.

---

## Full Inventory Map

### Category Tree (9 grocery categories, ~120 families)

```
fruits/          (14 families, 453+ products)
  seasonal-fruits, citrus, berries, stone-fruits, tropical-fruits,
  melons, grapes, avocados, apples-pears, bananas, figs,
  pomegranates, dates-&-dried-fruits, mixed-fruit-platters

vegetables/      (19 families, 692+ products)
  salad-essentials, potatoes, onions-garlic, root-vegetables,
  mushrooms, tomatoes, peppers, squash-&-gourds, beans-&-peas,
  corn, asian-vegetables, cooking-essentials, leafy-greens,
  fresh-herbs, organic-vegetables, microgreens-sprouts-wheatgrass,
  imperfect-picks, canned-pickled-veg, mixed-vegetable-platters

meat/            (12 families, 440+ products)
  lamb, beef, chicken-poultry, turkey, duck, wagyu, veal,
  offal, minced-burgers-sausages, cooked-cured-meats,
  plant-based-meat, bbq-marinaded

seafood/         (10 families, 468+ products)
  fresh-fish, salmon, prawns-shrimp, lobster-crab, squid-octopus,
  mussels-clams-oysters, smoked-cured-fish, sushi-sashimi,
  fish-cakes-fingers, mixed-seafood-platters

dairy/           (10 families, 864+ products)
  fresh-milk, cheese, butter, yoghurt, cream, eggs, labneh,
  milk-alternatives, local, coffee-creamer

bakery/          (11 families, 486+ products)
  bread-loaves, flatbreads-wraps, rolls-buns, croissants-pastries,
  muffins-cakes, gluten-free, pizza-dough, crackers-breadsticks,
  baking-ingredients, artisan, bagels

pantry/          (17 families, 1200+ products)
  pasta-noodles, rice-grains, canned-beans-pulses, sauces-condiments,
  oils-vinegar, spices-seasonings, nuts-seeds, honey-spreads,
  cereal-granola, flour-baking, tea-coffee, snacks-chips,
  chocolate-sweets, canned-fish-meat, cooking-pastes, dried-fruits,
  superfoods

drinks/          (11 families, 842+ products)
  water, juices, soft-drinks, energy-drinks, coconut-water,
  kombucha, tea-drinks, coffee-drinks, milkshakes-smoothies,
  non-alcoholic, drink-mixes

frozen/          (10 families, 570+ products)
  frozen-fruits, frozen-vegetables, frozen-fish-seafood,
  frozen-meat-poultry, frozen-meals, ice-cream, frozen-bread-pastry,
  frozen-desserts, frozen-fries-potatoes, frozen-pizza
```

**Total: ~6,015 unique products counted (before pagination)**
**Estimated with pagination: 8,000-10,000+**

Many families show exactly 100 products — meaning the first page is full and
there are more behind `?page=2`, `?page=3`, etc.

### Pagination

RSC pagination works via `?page=N` query parameter:
- Page 1: products 1-100 (default)
- Page 2: products 101-200
- Zero overlap between pages (confirmed)
- Empty page = no more products for that family

---

## Architecture Design

### Overview

```
┌──────────────────────────────────────────────────────────┐
│                   scrape_kibsons_v2.py                   │
│                                                          │
│  1. Launch Edge (Playwright, real profile)                │
│  2. Navigate to kibsons.com (establish Cloudflare sess)  │
│  3. For each category/family:                            │
│     a. Fetch RSC page 1 → parse products                │
│     b. If 100 products: fetch page 2, 3... until empty  │
│     c. Map RSC fields → our schema                      │
│     d. Batch upsert to product_catalog                  │
│  4. Report: products added, updated, removed             │
└──────────────────────────────────────────────────────────┘
```

### Phase 1: Browser Setup (reuse existing)

```python
browser, context = await launch_edge()
page = await context.new_page()
await page.goto("https://www.kibsons.com/en/")
# Wait for Cloudflare to pass — the real profile has established cookies
await page.wait_for_load_state("networkidle")
```

### Phase 2: RSC Fetch via page.evaluate()

```python
async def fetch_rsc_products(page, category: str, family: str, page_num: int = 1) -> list[dict]:
    """
    Fetch products from an RSC endpoint within the browser context.
    Returns parsed product list or empty list if no more pages.
    """
    url = f"/en/{category}/{family}" + (f"?page={page_num}" if page_num > 1 else "")

    raw = await page.evaluate(f"""
        async () => {{
            const resp = await fetch('{url}', {{
                headers: {{
                    'RSC': '1',
                    'Next-Router-State-Tree': '%5B%22%22%5D',
                    'Next-Url': '{url}'
                }}
            }});
            return await resp.text();
        }}
    """)

    return parse_rsc_payload(raw)
```

### Phase 3: RSC Payload Parsing

The RSC format is a stream of tagged lines. Product data appears as JSON arrays
embedded in lines starting with specific numeric indices. The parsing approach:

```python
def parse_rsc_payload(raw_text: str) -> list[dict]:
    """
    Extract product objects from Next.js RSC streaming response.

    RSC format: lines like '3:["$","div",null,{"children":...}]'
    Product arrays appear as JSON with stockCode, stockDesc, etc.
    """
    products = []
    for line in raw_text.split('\n'):
        # Skip non-data lines
        if ':' not in line:
            continue

        # Find the JSON portion after the line index
        colon_idx = line.index(':')
        json_part = line[colon_idx + 1:]

        # Look for product-shaped objects
        # Products have stockCode, stockDesc, stockRate as signature fields
        try:
            data = json.loads(json_part)
            _extract_products_recursive(data, products)
        except (json.JSONDecodeError, TypeError):
            continue

    return products


def _extract_products_recursive(obj, results: list):
    """Walk a nested structure looking for product objects."""
    if isinstance(obj, dict):
        if 'stockCode' in obj and 'stockDesc' in obj:
            results.append(obj)
        else:
            for v in obj.values():
                _extract_products_recursive(v, results)
    elif isinstance(obj, list):
        for item in obj:
            _extract_products_recursive(item, results)
```

### Phase 4: Field Mapping (RSC → product_catalog)

```python
def map_rsc_to_catalog(rsc_product: dict, store_id: int, category: str, family: str) -> dict:
    """Map a Kibsons RSC product object to our product_catalog schema."""

    name = rsc_product.get("stockDesc", "").strip()
    brand = rsc_product.get("brandDesc", "").strip()
    sku = rsc_product.get("stockCode", "")
    size = rsc_product.get("stockShortDetail", "").strip()
    origin = rsc_product.get("originDesc", "").strip()

    # Price: prefer promotion rate if > 0
    price = rsc_product.get("stockPromotionRate") or rsc_product.get("stockRate") or 0

    # Organic: from classification array (structured, no heuristics)
    classifications = rsc_product.get("classification") or []
    is_organic = any(c.get("desc", "").lower() == "organic" for c in classifications)

    # Category hierarchy
    cat_hierarchy = f"{category.replace('-', ' ').title()} > {family.replace('-', ' ').title()}"

    # Product URL
    slug = rsc_product.get("slug") or rsc_product.get("stockCode", "")
    product_url = f"https://www.kibsons.com/en/{category}/{family}/{slug}"

    # Images
    images = rsc_product.get("productImages") or []
    image_url = images[0].get("url", "") if images else ""
    if not image_url:
        image_url = rsc_product.get("imageUrl", "")

    return {
        "store_id": store_id,
        "product_name": _build_product_name(name, brand, size),
        "product_url": product_url,
        "brand": brand,
        "price": float(price),
        "size": size,
        "category": cat_hierarchy,
        "origin_country": origin,
        "image_url": image_url,
        "is_organic": int(is_organic),
        "is_available": 1,
        "detail_scraped": 0,  # RSC doesn't include description/ingredients

        # ── New fields (requires schema migration) ──
        "sku": sku,
        "unit": rsc_product.get("stockUnits", ""),
        "regular_price": float(rsc_product.get("stockRate") or 0),
        "sale_price": float(rsc_product.get("stockPromotionRate") or 0),
        "transport_route": rsc_product.get("transportRoute", ""),
        "popularity_score": rsc_product.get("pl_sku_popularity_score") or 0,
        "is_fresh": bool(rsc_product.get("pl_is_fresh")),
        "badges": json.dumps([c.get("desc") for c in classifications]),
        "featured_tags": json.dumps(
            [f.get("desc") for f in (rsc_product.get("featured") or [])]
        ),
        "keywords": json.dumps(rsc_product.get("keywords") or []),
        "kibsons_id": rsc_product.get("id"),
    }
```

### Phase 5: Database Schema Migration

Add new columns to `product_catalog` to store the rich RSC data:

```sql
ALTER TABLE product_catalog ADD COLUMN sku TEXT;
ALTER TABLE product_catalog ADD COLUMN unit TEXT;                -- "Per Pack", "Per KG"
ALTER TABLE product_catalog ADD COLUMN regular_price REAL;       -- before discount
ALTER TABLE product_catalog ADD COLUMN sale_price REAL;          -- 0 if no sale
ALTER TABLE product_catalog ADD COLUMN transport_route TEXT;     -- CHILLED/AMBIENT/FROZEN
ALTER TABLE product_catalog ADD COLUMN popularity_score REAL;    -- 0-100
ALTER TABLE product_catalog ADD COLUMN is_fresh INTEGER DEFAULT 0;
ALTER TABLE product_catalog ADD COLUMN badges TEXT;              -- JSON array
ALTER TABLE product_catalog ADD COLUMN featured_tags TEXT;       -- JSON array
ALTER TABLE product_catalog ADD COLUMN keywords TEXT;            -- JSON array
ALTER TABLE product_catalog ADD COLUMN external_id TEXT;         -- Kibsons internal ID
```

Also update the unique constraint strategy:
- Current: `UNIQUE(store_id, product_url)` — keeps working
- Additionally create index on `(store_id, sku)` for fast SKU lookups

### Phase 6: Orchestrator (Full Inventory Crawl)

```python
KIBSONS_STORE_ID = 1

GROCERY_CATEGORIES = {
    "fruits": [
        "seasonal-fruits", "citrus", "berries", "stone-fruits",
        "tropical-fruits", "melons", "grapes", "avocados",
        "apples-pears", "bananas", "figs", "pomegranates",
        "dates-&-dried-fruits", "mixed-fruit-platters",
    ],
    "vegetables": [
        "salad-essentials", "potatoes", "onions-garlic",
        "root-vegetables", "mushrooms", "tomatoes", "peppers",
        "squash-&-gourds", "beans-&-peas", "corn",
        "asian-vegetables", "cooking-essentials", "leafy-greens",
        "fresh-herbs", "organic-vegetables",
        "microgreens-sprouts-wheatgrass", "imperfect-picks",
        "canned-pickled-veg", "mixed-vegetable-platters",
    ],
    "meat": [
        "lamb", "beef", "chicken-poultry", "turkey", "duck",
        "wagyu", "veal", "offal", "minced-burgers-sausages",
        "cooked-cured-meats", "plant-based-meat", "bbq-marinaded",
    ],
    "seafood": [
        "fresh-fish", "salmon", "prawns-shrimp", "lobster-crab",
        "squid-octopus", "mussels-clams-oysters", "smoked-cured-fish",
        "sushi-sashimi", "fish-cakes-fingers", "mixed-seafood-platters",
    ],
    "dairy": [
        "fresh-milk", "cheese", "butter", "yoghurt", "cream",
        "eggs", "labneh", "milk-alternatives", "local", "coffee-creamer",
    ],
    "bakery": [
        "bread-loaves", "flatbreads-wraps", "rolls-buns",
        "croissants-pastries", "muffins-cakes", "gluten-free",
        "pizza-dough", "crackers-breadsticks", "baking-ingredients",
        "artisan", "bagels",
    ],
    "pantry": [
        "pasta-noodles", "rice-grains", "canned-beans-pulses",
        "sauces-condiments", "oils-vinegar", "spices-seasonings",
        "nuts-seeds", "honey-spreads", "cereal-granola",
        "flour-baking", "tea-coffee", "snacks-chips",
        "chocolate-sweets", "canned-fish-meat", "cooking-pastes",
        "dried-fruits", "superfoods",
    ],
    "drinks": [
        "water", "juices", "soft-drinks", "energy-drinks",
        "coconut-water", "kombucha", "tea-drinks", "coffee-drinks",
        "milkshakes-smoothies", "non-alcoholic", "drink-mixes",
    ],
    "frozen": [
        "frozen-fruits", "frozen-vegetables", "frozen-fish-seafood",
        "frozen-meat-poultry", "frozen-meals", "ice-cream",
        "frozen-bread-pastry", "frozen-desserts",
        "frozen-fries-potatoes", "frozen-pizza",
    ],
}


async def scrape_full_inventory():
    """Scrape the complete Kibsons grocery catalog via RSC."""

    browser, context = await launch_edge()
    page = await context.new_page()

    # Establish Cloudflare session
    await page.goto("https://www.kibsons.com/en/")
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(3)

    stats = {"total": 0, "new": 0, "updated": 0, "families": 0, "errors": []}
    conn = get_db_connection()

    try:
        for category, families in GROCERY_CATEGORIES.items():
            for family in families:
                page_num = 1
                family_products = []

                while True:
                    products = await fetch_rsc_products(page, category, family, page_num)

                    if not products:
                        break  # No more pages

                    family_products.extend(products)
                    stats["families"] += 1

                    # If fewer than 100, this is the last page
                    if len(products) < 100:
                        break

                    page_num += 1
                    await asyncio.sleep(0.5)  # Polite delay between pages

                # Map and upsert
                for rsc_prod in family_products:
                    catalog_row = map_rsc_to_catalog(rsc_prod, KIBSONS_STORE_ID, category, family)
                    upserted = upsert_product(conn, catalog_row)
                    stats["total"] += 1
                    if upserted == "new":
                        stats["new"] += 1
                    else:
                        stats["updated"] += 1

                conn.commit()
                print(f"  {category}/{family}: {len(family_products)} products")

                # Brief pause between families to be polite
                await asyncio.sleep(1)

    finally:
        await safe_close(browser)
        conn.close()

    return stats
```

---

## Performance Estimate

| Metric | Current (DOM) | RSC v2 |
|---|---|---|
| Products per request | 1 | 100 |
| Time per product | ~3 sec | ~0.01 sec |
| Total requests | ~1,200 | ~100-150 |
| Total time | ~60 min | ~5-8 min |
| Category data | None (all NULL) | Full hierarchy |
| Organic detection | Heuristic (text + badge) | Structured (classification) |
| Brand parsing | ALL CAPS heuristic | Clean field (brandDesc) |
| Price accuracy | Regex from text | Exact numbers |
| Fields captured | 14 | 25+ |

---

## What RSC Does NOT Give Us

RSC product listings contain ~100 fields but **not** these:

1. **Description** — The product detail text (marketing copy)
2. **Ingredients** — The ingredient list
3. **Nutrition info** — Nutritional facts table
4. **Storage instructions** — How to store the product

These are on the individual product detail page only. Two strategies:

### Strategy A: RSC First, Detail on Demand (Recommended)

1. Full RSC scrape first → gives us 8,000-10,000 products with structured data
2. Mark all as `detail_scraped = 0`
3. When the scoring engine selects a product as a top-3 alternative, **then**
   scrape its detail page for description/ingredients/nutrition
4. This means we only deep-scrape ~60-100 products (top alternatives for our
   20 items) instead of 10,000

**Why this works:** The scoring engine primarily uses name, brand, price, organic
status, origin, and popularity — all available from RSC. Description/ingredients
are nice-to-have for display purposes and for the preference learning model
later. We don't need them for initial ranking.

### Strategy B: Batch Detail Scrape (Future)

If we later need deep data for all products (e.g., for AI-based recipe
matching or ingredient analysis), we can run a background detail scraper:

1. Query `product_catalog WHERE detail_scraped = 0 AND store_id = 1`
2. Visit each product page, click tabs, extract text
3. Rate-limit to ~1 page per second (3,600/hour)
4. Run overnight: full catalog in ~3 hours

---

## Deduplication Strategy

Products appear in multiple families (e.g., "Organic Eggs" in both
`dairy/eggs` and `dairy/local`). Deduplication approach:

1. **Primary key:** `UNIQUE(store_id, product_url)` — same product URL = same
   product, upsert with latest data
2. **SKU index:** `(store_id, sku)` — quick lookup by Kibsons stock code
3. **Category handling:** When the same product appears in multiple families,
   store the **most specific** category (e.g., "Dairy > Eggs" beats
   "Dairy > Local")

---

## Availability Tracking

On each full scrape:

1. Before scraping, record all existing Kibsons `product_url` values
2. After scraping, any existing products NOT seen in the RSC data are
   potentially unavailable
3. Mark them `is_available = 0` (don't delete — they may return)
4. Products that reappear get `is_available = 1` on upsert

---

## Refresh Cadence

- **Full inventory:** Weekly (Sunday night) — catches new products, price
  changes, availability changes
- **Top alternatives only:** Daily — just re-fetch the families that contain
  our top-ranked products to catch price changes
- **On-demand:** When basket curator can't find a product, trigger a targeted
  family refresh

---

## Migration Plan (from v1 to v2)

### Step 1: Schema Migration
- Add new columns to `product_catalog`
- Non-destructive: all new columns are nullable

### Step 2: Build RSC Scraper
- New file: `scrape_kibsons_rsc.py`
- Keep existing `scrape_kibsons.py` as fallback
- RSC scraper uses same `launch_edge()` and Playwright infrastructure

### Step 3: Full Inventory Scrape
- Run RSC scraper → populates 8,000-10,000 products
- Existing 1,186 products get updated via upsert (new fields filled in)
- ~7,000+ new products added

### Step 4: Re-score Alternatives
- Run `score_and_rank_alternatives()` for all 20 items against the expanded
  catalog
- This will find better alternatives now that we have 8x more products

### Step 5: Deprecate DOM Scraper
- Once RSC scraper is proven, the DOM-based category browser and product page
  scraper become the "detail enrichment" tool only
- Main discovery and pricing is RSC-only

---

## Error Handling

| Scenario | Handling |
|---|---|
| Cloudflare blocks RSC fetch | Retry with full page navigation first to refresh session |
| RSC payload format changes | Log raw payload, fall back to DOM scraper for that family |
| Empty RSC response | Skip family, log warning, continue with next |
| Browser crash mid-scrape | Save progress, relaunch browser, resume from last family |
| Rate limiting (429) | Exponential backoff: 2s, 4s, 8s, then skip and retry later |

---

## Summary

| | Current v1 | Proposed v2 |
|---|---|---|
| **Method** | DOM scraping per product | RSC payload per 100 products |
| **Coverage** | 20 families, ~1,200 products | 120 families, ~10,000 products |
| **Speed** | 60 min | 5-8 min |
| **Category data** | None | Full hierarchy |
| **Organic** | Text + badge heuristic | Structured classification |
| **Brand** | ALL CAPS parsing | Clean brandDesc field |
| **Price** | Regex from text | Exact numeric fields |
| **New fields** | 0 | 11 (sku, unit, transport, popularity...) |
| **Maintenance** | Fragile (breaks on UI changes) | Stable (data structure, not UI) |
