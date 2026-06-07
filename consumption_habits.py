"""
AutoRestock - Real Consumption Habits Baseline

This is the ground truth for how the household consumes groceries.
Provided directly by the user, not derived from order history.
Order history is used for item/price/brand reference only.

This data drives the prediction engine: when to restock and how much.

With 2 orders per week (Tue + Fri), each order covers ~3.5 days.
Quantities below are per order (not per day).

SEARCH TERMS:
  Each habit has a "search_terms" dict keyed by store name.
  Values are lists of exact product names to type into the store search bar.
  Ordered by preference — the scraper tries the first one, falls back to the next.
  This bridges the gap between "we need Cheese" and "search for Glenstal Mild Irish Cheddar".

Run this script to seed/update restock_rules and items in the DB.
"""

import json
import sqlite3
import math
from db_setup import DB_PATH, create_database

# -------------------------------------------------------------------
# HOUSEHOLD HABITS (source of truth)
# -------------------------------------------------------------------
# Each entry:
#   name: human-readable product name (for shopping list display)
#   category: product category
#   frequency: how often consumed ("daily", "almost_daily", "3x_week", "weekly", "cooking_staple")
#   people: how many people consume it
#   notes: brand, quality, variant preferences (human-readable)
#   preferred_store: which store to buy from
#   typical_qty: how many units per order (estimated for 3.5-day window)
#   typical_size: typical pack size from order history
#   avg_price: approximate price per unit from order history (AED)
#   search_terms: { "Store Name": ["exact product name 1", "fallback name 2", ...] }
#     These are what the scraper types into the store search bar.
#     First match wins. Ordered by preference.

HABITS = [
    {
        "name": "Eggs",
        "category": "Dairy",
        "frequency": "daily",
        "people": 3,
        "notes": "Organic only. Jenan Organic Free Range preferred. Pack of 12 is enough given order frequency (every 3.5 days). 15-pack is too many — they go bad.",
        "preferred_store": "Carrefour UAE",
        "typical_qty": 1,
        "typical_size": "12 PCS",
        "avg_price": 34.49,
        "search_terms": {
            "Carrefour UAE": [
                "Jenan Organic Free Range Eggs 15",
                "Jenan Organic Eggs",
                "Organic Free Range Eggs",
            ],
            "Kibsons": [
                "Medium Brown Eggs 15",
                "Free Range Eggs",
                "Brown Eggs",
            ],
        },
    },
    {
        "name": "Cheese",
        "category": "Dairy",
        "frequency": "daily",
        "people": 3,
        "notes": "Mostly cheddar variants. Glenstal Irish Cheddar, Snowdonia Smoked Cheddar, sliced cheddar from Kibsons. When buying 2, pick DIFFERENT types for variety (e.g. 1 Glenstal + 1 Snowdonia), not 2 of the same.",
        "preferred_store": "Carrefour UAE",
        "typical_qty": 2,
        "typical_size": "200g",
        "avg_price": 20.00,
        "search_terms": {
            "Carrefour UAE": [
                "Glenstal Mild Irish Cheddar Cheese",
                "Snowdonia Beechwood Smoked Cheddar",
                "Snowdonia Cheddar",
            ],
            "Kibsons": [
                "Sliced Cheddar Cheese",
                "Cheddar Cheese",
            ],
        },
    },
    {
        "name": "Yogurt",
        "category": "Dairy",
        "frequency": "daily",
        "people": 3,
        "notes": "Plain organic and non-organic. Brand: Balade preferred. Greek style yoghurt (950g) and regular (500g) from Kibsons. Almarai full fat from Carrefour.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "950g",
        "avg_price": 40.70,
        "search_terms": {
            "Kibsons": [
                "Balade Greek Style Yoghurt",
                "Balade Yoghurt",
                "Greek Yoghurt",
            ],
            "Carrefour UAE": [
                "Almarai Full Fat Yoghurt",
                "Almarai Yoghurt",
            ],
        },
    },
    {
        "name": "Beef Mince",
        "category": "Meat",
        "frequency": "almost_daily",
        "people": 2,
        "notes": "Used for pasta mostly. Grass fed from Kibsons preferred. 1x 500g pack per order is enough — ordering every 3.5 days means fresh supply.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "500g",
        "avg_price": 36.20,
        "search_terms": {
            "Kibsons": [
                "Beef Mince Grass Fed",
                "Grass Fed Beef Mince",
                "Beef Mince",
            ],
            "Carrefour UAE": [
                "Beef Mince",
            ],
        },
    },
    {
        "name": "Apple",
        "category": "Produce",
        "frequency": "daily",
        "people": 1,
        "notes": "Good quality. Royal Gala from Kibsons, Organic Juliet from Carrefour. Granny Smith occasionally.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "5-7 pieces",
        "avg_price": 13.80,
        "search_terms": {
            "Kibsons": [
                "Royal Gala Apple",
                "Gala Apple",
                "Apple",
            ],
            "Carrefour UAE": [
                "Organic Juliet Apple",
                "Organic Apple",
                "Royal Gala Apple",
            ],
        },
    },
    {
        "name": "Banana",
        "category": "Produce",
        "frequency": "daily",
        "people": 1,
        "notes": "Organic or good quality. Chiquita from Kibsons, Organic from Carrefour.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "1kg",
        "avg_price": 12.25,
        "search_terms": {
            "Kibsons": [
                "Chiquita Banana",
                "Banana",
            ],
            "Carrefour UAE": [
                "Organic Banana",
                "Banana",
            ],
        },
    },
    {
        "name": "Cucumber",
        "category": "Produce",
        "frequency": "daily",
        "people": 1,
        "notes": "Only organic. Organic cucumbers from Carrefour (500g) or regular from Kibsons.",
        "preferred_store": "Carrefour UAE",
        "typical_qty": 1,
        "typical_size": "500g",
        "avg_price": 6.99,
        "search_terms": {
            "Carrefour UAE": [
                "Organic Cucumber",
                "Cucumber Organic",
            ],
            "Kibsons": [
                "Cucumber",
            ],
        },
    },
    {
        "name": "Kefir",
        "category": "Dairy",
        "frequency": "daily",
        "people": 1,
        "notes": "Organic preferred. Kefir yoghurt (350g) or original kefir (500ml) from Kibsons.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "500ml",
        "avg_price": 26.55,
        "search_terms": {
            "Kibsons": [
                "Organic Kefir",
                "Kefir Yoghurt",
                "Kefir",
            ],
            "Carrefour UAE": [
                "Kefir",
            ],
        },
    },
    {
        "name": "Milk",
        "category": "Dairy",
        "frequency": "daily",
        "people": 1,
        "notes": "For 2 lattes daily. Al Ain Farms Double Cream from Carrefour. Oatly oat drink as alternative.",
        "preferred_store": "Carrefour UAE",
        "typical_qty": 1,
        "typical_size": "2L",
        "avg_price": 13.29,
        "search_terms": {
            "Carrefour UAE": [
                "Al Ain Farms Double Cream Milk",
                "Al Ain Double Cream",
                "Al Ain Milk",
            ],
            "Kibsons": [
                "Fresh Milk",
                "Milk",
            ],
        },
    },
    {
        "name": "Pepper",
        "category": "Produce",
        "frequency": "daily",
        "people": 2,
        "notes": "Capsicums — green, red, yellow. Organic preferred from Carrefour. When qty=2, pick DIFFERENT colors (e.g. 1 green + 1 red), not 2 of the same.",
        "preferred_store": "Carrefour UAE",
        "typical_qty": 2,
        "typical_size": "500g",
        "avg_price": 5.99,
        "search_terms": {
            "Carrefour UAE": [
                "Organic Capsicum",
                "Organic Bell Pepper",
                "Capsicum",
            ],
            "Kibsons": [
                "Green Capsicum",
                "Capsicum",
            ],
        },
    },
    {
        "name": "Pasta",
        "category": "Pantry",
        "frequency": "daily",
        "people": 2,
        "notes": "Made from veggies/lentils mostly. Earth Goods Tricolour Fusilli, Earth Goods Truffle Pasta, lentil-based. Penne and orzo from Kibsons.",
        "preferred_store": "Carrefour UAE",
        "typical_qty": 2,
        "typical_size": "500g",
        "avg_price": 11.75,
        "search_terms": {
            "Carrefour UAE": [
                "Earth Goods Tricolour Fusilli",
                "Earth Goods Truffle Pasta",
                "Earth Goods Pasta",
            ],
            "Kibsons": [
                "Penne Pasta",
                "Orzo Pasta",
                "Pasta",
            ],
        },
    },
    {
        "name": "Olive Oil",
        "category": "Pantry",
        "frequency": "cooking_staple",
        "people": 0,
        "notes": "Bertolli Extra Virgin only. Used for cooking. Lasts 2-3 weeks per bottle.",
        "preferred_store": "Carrefour UAE",
        "typical_qty": 1,
        "typical_size": "1L",
        "avg_price": 62.99,
        "search_terms": {
            "Carrefour UAE": [
                "Bertolli Extra Virgin Olive Oil",
                "Bertolli Olive Oil",
            ],
            "Kibsons": [
                "Extra Virgin Olive Oil",
            ],
        },
    },
    {
        "name": "Butter",
        "category": "Dairy",
        "frequency": "cooking_staple",
        "people": 0,
        "notes": "Unsalted only. Lurpak brand. Used for cooking. Lasts ~1 week.",
        "preferred_store": "Carrefour UAE",
        "typical_qty": 1,
        "typical_size": "200g",
        "avg_price": 13.99,
        "search_terms": {
            "Carrefour UAE": [
                "Lurpak Unsalted Soft Butter",
                "Lurpak Unsalted Butter",
                "Lurpak Butter",
            ],
            "Kibsons": [
                "Unsalted Butter",
                "Butter",
            ],
        },
    },
    {
        "name": "Spinach",
        "category": "Produce",
        "frequency": "daily",
        "people": 2,
        "notes": "Baby spinach. Greens go bad fast — 1 small pack per order. Fresh supply every 3.5 days.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "100g",
        "avg_price": 8.00,
        "search_terms": {
            "Kibsons": [
                "Baby Spinach",
                "Spinach",
            ],
            "Carrefour UAE": [
                "Baby Spinach",
            ],
        },
    },
    {
        "name": "Rocket",
        "category": "Produce",
        "frequency": "daily",
        "people": 2,
        "notes": "Wild rocket. Greens go bad fast — 1 pack per order is enough, max 2. Fresh supply every 3.5 days.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "100g",
        "avg_price": 5.00,
        "search_terms": {
            "Kibsons": [
                "Wild Rocket",
                "Rocket",
            ],
            "Carrefour UAE": [
                "Wild Rocket",
                "Rocket Leaves",
            ],
        },
    },
    {
        "name": "Chicken Breast",
        "category": "Meat",
        "frequency": "weekly",
        "people": 0,
        "notes": "Once a week. From Kibsons. 500g pack.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "500g",
        "avg_price": 72.25,
        "search_terms": {
            "Kibsons": [
                "Chicken Breast",
                "Fresh Chicken Breast",
            ],
            "Carrefour UAE": [
                "Chicken Breast",
            ],
        },
    },
    {
        "name": "Beef Sausages",
        "category": "Meat",
        "frequency": "3x_week",
        "people": 2.5,
        "notes": "Good quality, check ingredients. For breakfast 3 days a week.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "500g",
        "avg_price": 35.00,
        "search_terms": {
            "Kibsons": [
                "Beef Sausages",
                "Beef Breakfast Sausages",
            ],
            "Carrefour UAE": [
                "Beef Sausages",
            ],
        },
    },
    {
        "name": "Salmon",
        "category": "Meat",
        "frequency": "weekly",
        "people": 0,
        "notes": "Organic salmon. Once a week.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "500g",
        "avg_price": 55.00,
        "search_terms": {
            "Kibsons": [
                "Organic Salmon",
                "Salmon Fillet",
                "Salmon",
            ],
            "Carrefour UAE": [
                "Organic Salmon",
                "Salmon Fillet",
            ],
        },
    },
    {
        "name": "Striploin",
        "category": "Meat",
        "frequency": "weekly",
        "people": 0,
        "notes": "Once a week. Quality beef.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "500g",
        "avg_price": 60.00,
        "search_terms": {
            "Kibsons": [
                "Beef Striploin",
                "Striploin Steak",
                "Striploin",
            ],
            "Carrefour UAE": [
                "Beef Striploin",
                "Striploin",
            ],
        },
    },
    {
        "name": "Bread",
        "category": "Pantry",
        "frequency": "daily",
        "people": 0,
        "notes": "Daily. Brown bread loaf, rye bread, or flax seed bread. Kibsons or Carrefour.",
        "preferred_store": "Kibsons",
        "typical_qty": 1,
        "typical_size": "400g",
        "avg_price": 9.20,
        "search_terms": {
            "Kibsons": [
                "Brown Bread Loaf",
                "Rye Bread",
                "Flax Seed Bread",
            ],
            "Carrefour UAE": [
                "Brown Bread",
                "Rye Bread",
                "Wholemeal Bread",
            ],
        },
    },
]

# Map frequency to avg days between orders (for 2x/week schedule)
FREQUENCY_TO_DAYS = {
    "daily": 3.5,           # every order
    "almost_daily": 3.5,    # every order
    "3x_week": 3.5,         # every order (3 days of 3.5-day window)
    "weekly": 7.0,           # every other order
    "cooking_staple": 14.0,  # roughly every 2 weeks
}


def seed_habits():
    """Insert/update items and restock_rules from real consumption habits."""
    create_database()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Add search_terms column if it doesn't exist yet (migration-safe)
    try:
        cur.execute("ALTER TABLE items ADD COLUMN search_terms TEXT")
        conn.commit()
        print("  [migration] Added search_terms column to items table")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Make sure stores exist
    for store_name, url in [
        ("Kibsons", "https://www.kibsons.com"),
        ("Carrefour UAE", "https://www.carrefouruae.com"),
    ]:
        cur.execute("SELECT id FROM stores WHERE name = ?", (store_name,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO stores (name, website_url) VALUES (?, ?)",
                (store_name, url),
            )

    conn.commit()

    for h in HABITS:
        # Get store ID
        cur.execute("SELECT id FROM stores WHERE name = ?", (h["preferred_store"],))
        store_row = cur.fetchone()
        if not store_row:
            print(f"  WARNING: Store '{h['preferred_store']}' not found, skipping {h['name']}")
            continue
        store_id = store_row["id"]

        # Serialize search_terms to JSON
        search_terms_json = json.dumps(h.get("search_terms", {}))

        # Upsert into items
        cur.execute("SELECT id FROM items WHERE name = ?", (h["name"],))
        row = cur.fetchone()
        if row:
            item_id = row["id"]
            cur.execute("""
                UPDATE items SET
                    category = ?, preferred_store_id = ?, avg_price = ?,
                    size = ?, quality_tier = 'preferred', notes = ?,
                    search_terms = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (h["category"], store_id, h["avg_price"], h["typical_size"],
                  h["notes"], search_terms_json, item_id))
        else:
            cur.execute("""
                INSERT INTO items (name, category, preferred_store_id, avg_price, size, quality_tier, notes, search_terms)
                VALUES (?, ?, ?, ?, ?, 'preferred', ?, ?)
            """, (h["name"], h["category"], store_id, h["avg_price"],
                  h["typical_size"], h["notes"], search_terms_json))
            item_id = cur.lastrowid

        # Upsert restock rule
        avg_days = FREQUENCY_TO_DAYS.get(h["frequency"], 7.0)
        # Confidence is high — this is directly from user, not derived
        confidence = 0.95

        cur.execute("""
            INSERT INTO restock_rules (item_id, avg_days_between, avg_quantity, last_ordered_date, next_restock_date, confidence, times_ordered)
            VALUES (?, ?, ?, date('now'), date('now', '+' || ? || ' days'), ?, 99)
            ON CONFLICT(item_id) DO UPDATE SET
                avg_days_between = excluded.avg_days_between,
                avg_quantity = excluded.avg_quantity,
                confidence = excluded.confidence,
                times_ordered = excluded.times_ordered,
                updated_at = datetime('now')
        """, (item_id, avg_days, h["typical_qty"], int(math.ceil(avg_days)), confidence))

        # Show search terms summary
        terms = h.get("search_terms", {}).get(h["preferred_store"], [])
        first_term = terms[0] if terms else h["name"]
        print(f"  {h['name']}: every {avg_days} days, qty {h['typical_qty']} @ {h['preferred_store']} -> \"{first_term}\"")

    conn.commit()
    conn.close()
    print(f"\nSeeded {len(HABITS)} habits with search terms into {DB_PATH}")


if __name__ == "__main__":
    seed_habits()
