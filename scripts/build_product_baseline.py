#!/usr/bin/env python3
"""Build baseline catalog and suggestion-engine preference signals from order history."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CategoryRule:
    pattern: str
    category_path: str


CATEGORY_RULES: list[CategoryRule] = [
    CategoryRule(r"beef|mince|tenderloin|wagyu|fondue|steak|lamb", "protein/meat/red-meat"),
    CategoryRule(r"sardines|salmon|fish", "protein/seafood/fish"),
    CategoryRule(r"eggs", "dairy-eggs/eggs/free-range-or-conventional"),
    CategoryRule(r"yoghurt|yogurt", "dairy-eggs/yoghurt/cultured-dairy"),
    CategoryRule(r"cheddar|brie|emmental|roquefort|cheese", "dairy-eggs/cheese/specialty-and-daily"),
    CategoryRule(r"milk|oat drink|oatly", "dairy-eggs/milk/animal-and-plant-based"),
    CategoryRule(r"olive oil", "pantry/oils-and-vinegars/olive-oil"),
    CategoryRule(r"vinegar", "pantry/oils-and-vinegars/vinegar"),
    CategoryRule(r"rice", "pantry/grains-and-rice/rice"),
    CategoryRule(r"pasta|fusilli", "pantry/pasta-and-noodles/pasta"),
    CategoryRule(r"lentil", "pantry/legumes/lentils"),
    CategoryRule(r"flour", "pantry/baking/flour"),
    CategoryRule(r"bread|bauernbrot", "bakery/bread/artisan-and-packaged"),
    CategoryRule(r"water|lemonade|juice", "beverages/water-and-soft-drinks/household-drinks"),
    CategoryRule(r"banana|apple|grapes|strawberry|figs|blueberry", "produce/fruits/fresh-fruit"),
    CategoryRule(r"capsicum|onion|okra|kale|cucumber|beans|tomato|rocket|sweet potato", "produce/vegetables/fresh-veg"),
    CategoryRule(r"olives", "produce/olives-and-pickles/olives"),
    CategoryRule(r"walnuts|hazelnuts|pistachios|sunflower|pumpkin seeds", "snacks/nuts-and-seeds/nuts-seeds"),
    CategoryRule(r"oreo|chips|crackers|biscuits|sunbites|chocolate|nutella|popcorn", "snacks/sweet-and-savory/snacks"),
    CategoryRule(r"rosemary", "produce/herbs-and-aromatics/fresh-herbs"),
]

QUALITY_PATTERNS = [
    (r"organic|bio", "organic"),
    (r"free range|free-range|pasture", "free-range"),
    (r"grass fed|grass-fed", "grass-fed"),
    (r"whole|fresh|natural", "fresh-natural"),
]

HEALTH_PATTERNS = [
    (r"chips|biscuits|chocolate|nutella|soft drink|soda", "not-great"),
    (r"organic|free range|free-range|whole|fresh|natural|olive oil|seeds|nuts", "good-healthy"),
]

PREMIUM_HINTS = {"lindt", "wagyu", "bertolli", "bragg", "jenan", "organic"}
NON_BRAND_PREFIXES = {
    "organic",
    "organics",
    "australian",
    "new",
    "fresh",
    "extra",
    "pure",
    "medium",
    "jumbo",
}

SEASONAL_SEEDS = [
    {"category_path": "produce/vegetables/fresh-veg", "product_keyword": "kale", "origin_country": "UAE", "season_start_month": 10, "season_end_month": 4, "peak_months": "11,12,1,2", "availability_confidence": 0.45},
    {"category_path": "produce/vegetables/fresh-veg", "product_keyword": "rocket", "origin_country": "UAE", "season_start_month": 10, "season_end_month": 4, "peak_months": "11,12,1,2", "availability_confidence": 0.45},
    {"category_path": "produce/vegetables/fresh-veg", "product_keyword": "okra", "origin_country": "India", "season_start_month": 4, "season_end_month": 10, "peak_months": "6,7,8", "availability_confidence": 0.4},
    {"category_path": "produce/fruits/fresh-fruit", "product_keyword": "strawberry", "origin_country": "Egypt", "season_start_month": 11, "season_end_month": 3, "peak_months": "12,1,2", "availability_confidence": 0.4},
]

SIZE_RE = re.compile(r"\b\d+(?:[\.]\d+)?\s?(?:kg|g|ml|l|pcs|pc|x\d+)\b", re.IGNORECASE)
NON_WORD_RE = re.compile(r"[^a-z0-9\s]+")


def canonicalize_name(name: str) -> str:
    lower = name.lower()
    stripped = SIZE_RE.sub("", lower)
    stripped = NON_WORD_RE.sub(" ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def infer_category(name: str) -> str:
    lower = name.lower()
    for rule in CATEGORY_RULES:
        if re.search(rule.pattern, lower):
            return rule.category_path
    return "other/uncategorized/needs-review"


def infer_quality_tier(name: str) -> str:
    lower = name.lower()
    for pattern, tier in QUALITY_PATTERNS:
        if re.search(pattern, lower):
            return tier
    return "conventional"


def infer_health_profile(name: str) -> str:
    lower = name.lower()
    for pattern, profile in HEALTH_PATTERNS:
        if re.search(pattern, lower):
            return profile
    return "neutral"


def infer_brand(name: str) -> str | None:
    tokens = re.findall(r"[A-Za-z0-9']+", name)
    if not tokens:
        return None
    first = tokens[0].lower()
    if first in NON_BRAND_PREFIXES:
        return None
    if first == "al" and len(tokens) >= 2:
        return f"{tokens[0]} {tokens[1]}"
    return tokens[0]


def load_orders(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_baseline(orders: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    product_stats: dict[str, dict[str, Any]] = {}
    category_summary: dict[str, dict[str, Any]] = defaultdict(lambda: {"products": 0, "line_items": 0})
    pref_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "brand_counts": Counter(),
            "brand_spend": defaultdict(float),
            "quality_counts": Counter(),
            "health_counts": Counter(),
            "last_seen": None,
        }
    )

    for order in orders:
        ordered_at = datetime.fromisoformat(order["ordered_at"].replace("Z", "+00:00"))
        for item in order["items"]:
            raw_name = item["name"]
            key = canonicalize_name(raw_name)
            category = infer_category(raw_name)
            quality_tier = infer_quality_tier(raw_name)
            health_profile = infer_health_profile(raw_name)
            brand = infer_brand(raw_name)

            if key not in product_stats:
                product_stats[key] = {
                    "canonical_name": key,
                    "example_name": raw_name,
                    "category_path": category,
                    "times_ordered": 0,
                    "total_quantity": 0.0,
                    "price_points": [],
                    "store_counts": Counter(),
                    "order_dates": [],
                    "quality_counts": Counter(),
                    "health_counts": Counter(),
                    "brand_counts": Counter(),
                }

            stat = product_stats[key]
            quantity = float(item.get("quantity", 1))
            unit_price = float(item.get("unit_price")) if item.get("unit_price") is not None else None
            stat["times_ordered"] += 1
            stat["total_quantity"] += quantity
            if unit_price is not None:
                stat["price_points"].append(unit_price)
            stat["store_counts"][order["store"]] += 1
            stat["order_dates"].append(ordered_at)
            stat["quality_counts"][quality_tier] += 1
            stat["health_counts"][health_profile] += 1
            if brand:
                stat["brand_counts"][brand] += 1

            pref = pref_stats[category]
            if brand:
                pref["brand_counts"][brand] += 1
                pref["brand_spend"][brand] += (unit_price or 0.0) * quantity
            pref["quality_counts"][quality_tier] += 1
            pref["health_counts"][health_profile] += 1
            pref["last_seen"] = max(pref["last_seen"], ordered_at) if pref["last_seen"] else ordered_at

    baseline_products: list[dict[str, Any]] = []
    for stat in product_stats.values():
        prices = stat.pop("price_points")
        dates = stat.pop("order_dates")
        stores = stat.pop("store_counts")
        preferred_store = stores.most_common(1)[0][0]

        quality_counts = stat.pop("quality_counts")
        health_counts = stat.pop("health_counts")
        brand_counts = stat.pop("brand_counts")

        stat["avg_unit_price"] = round(sum(prices) / len(prices), 2) if prices else None
        stat["preferred_store"] = preferred_store
        stat["first_seen"] = min(dates).date().isoformat()
        stat["last_seen"] = max(dates).date().isoformat()
        stat["order_count"] = stat.pop("times_ordered")
        stat["avg_quantity_per_order"] = round(stat["total_quantity"] / stat["order_count"], 3)
        stat["baseline_candidate"] = stat["order_count"] >= 2
        stat["quality_tier"] = quality_counts.most_common(1)[0][0]
        stat["health_profile"] = health_counts.most_common(1)[0][0]
        stat["usual_brand"] = brand_counts.most_common(1)[0][0] if brand_counts else None

        category_summary[stat["category_path"]]["products"] += 1
        category_summary[stat["category_path"]]["line_items"] += stat["order_count"]
        baseline_products.append(stat)

    baseline_products.sort(key=lambda x: (not x["baseline_candidate"], -x["order_count"], x["canonical_name"]))

    category_rows = [
        {
            "category_path": category,
            "product_count": values["products"],
            "line_item_count": values["line_items"],
        }
        for category, values in sorted(category_summary.items(), key=lambda kv: kv[0])
    ]

    suggestion_profiles = []
    for category, stat in sorted(pref_stats.items(), key=lambda kv: kv[0]):
        if stat["brand_counts"]:
            usual_brand, usual_count = stat["brand_counts"].most_common(1)[0]
            premium_brand = None
            if stat["brand_spend"]:
                premium_brand = max(stat["brand_spend"], key=stat["brand_spend"].get)
                if premium_brand == usual_brand and usual_brand.lower() not in PREMIUM_HINTS:
                    premium_brand = None
        else:
            usual_brand, usual_count, premium_brand = None, 0, None

        quality_rank = sorted(stat["quality_counts"].items(), key=lambda kv: (-kv[1], kv[0]))
        suggestion_profiles.append(
            {
                "category_path": category,
                "usual_brand": usual_brand,
                "premium_brand": premium_brand,
                "usual_brand_order_count": usual_count,
                "preferred_quality_tiers": [tier for tier, _ in quality_rank],
                "health_profile_priority": [profile for profile, _ in stat["health_counts"].most_common()],
                "fallback_sequence": [
                    "same_brand_same_quality",
                    "same_quality_tier",
                    "same_health_profile",
                    "premium_brand_if_healthier",
                    "ask_user_before_lowering_quality",
                ],
                "last_seen": stat["last_seen"].date().isoformat() if stat["last_seen"] else None,
            }
        )

    return baseline_products, category_rows, suggestion_profiles


def sync_to_db(
    db_path: Path,
    orders: list[dict[str, Any]],
    baseline_products: list[dict[str, Any]],
    suggestion_profiles: list[dict[str, Any]],
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    schema_path = ROOT / "sql" / "schema.sql"
    conn.executescript(schema_path.read_text(encoding="utf-8"))

    stores = sorted({o["store"] for o in orders})
    for store in stores:
        base = "https://www.carrefouruae.com" if "carrefour" in store.lower() else "https://kibsons.com"
        conn.execute("INSERT OR IGNORE INTO stores(name, base_url) VALUES (?, ?)", (store, base))

    store_ids = {name: sid for sid, name in conn.execute("SELECT id, name FROM stores").fetchall()}

    product_ids: dict[str, int] = {}
    for p in baseline_products:
        store_id = store_ids[p["preferred_store"]]
        conn.execute(
            """
            INSERT INTO products(canonical_name, brand, size_text, quality_tier, origin_country, preferred_store_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_name, brand, size_text)
            DO UPDATE SET quality_tier = excluded.quality_tier, preferred_store_id = excluded.preferred_store_id, updated_at = CURRENT_TIMESTAMP
            """,
            (p["canonical_name"], p["usual_brand"], None, p["quality_tier"], None, store_id),
        )
        pid = conn.execute(
            "SELECT id FROM products WHERE canonical_name = ? AND IFNULL(brand, '') = IFNULL(?, '') AND size_text IS NULL",
            (p["canonical_name"], p["usual_brand"]),
        ).fetchone()[0]
        product_ids[p["canonical_name"]] = pid

        brand_tier = "premium" if p["usual_brand"] and p["usual_brand"].lower() in PREMIUM_HINTS else "usual"
        conn.execute(
            """
            INSERT INTO product_signals(product_id, category_path, quality_tier, health_profile, brand_tier, signal_confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
              category_path = excluded.category_path,
              quality_tier = excluded.quality_tier,
              health_profile = excluded.health_profile,
              brand_tier = excluded.brand_tier,
              signal_confidence = excluded.signal_confidence,
              updated_at = CURRENT_TIMESTAMP
            """,
            (pid, p["category_path"], p["quality_tier"], p["health_profile"], brand_tier, 0.55 if p["baseline_candidate"] else 0.35),
        )

    for order in orders:
        store_id = store_ids[order["store"]]
        conn.execute(
            """
            INSERT OR IGNORE INTO orders(store_id, external_order_id, ordered_at, currency, subtotal, delivery_fee, total)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                store_id,
                order["external_order_id"],
                order["ordered_at"],
                order.get("currency", "AED"),
                order.get("subtotal"),
                order.get("delivery_fee", 0.0),
                (order.get("subtotal") or 0.0) + (order.get("delivery_fee") or 0.0) + (order.get("service_fee") or 0.0),
            ),
        )
        order_id = conn.execute(
            "SELECT id FROM orders WHERE store_id = ? AND external_order_id = ?",
            (store_id, order["external_order_id"]),
        ).fetchone()[0]

        for item in order["items"]:
            canonical = canonicalize_name(item["name"])
            conn.execute(
                """
                INSERT OR IGNORE INTO order_items(
                    order_id, product_id, raw_product_name, quantity, unit_price, line_total, product_url, category, source_brand, source_size_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    product_ids.get(canonical),
                    item["name"],
                    float(item.get("quantity", 1)),
                    item.get("unit_price"),
                    (float(item.get("quantity", 1)) * float(item.get("unit_price"))) if item.get("unit_price") is not None else None,
                    None,
                    infer_category(item["name"]),
                    infer_brand(item["name"]),
                    item.get("unit"),
                ),
            )

    for profile in suggestion_profiles:
        if profile["usual_brand"]:
            conn.execute(
                """
                INSERT INTO brand_preferences(user_scope, category_path, brand, preference_label, preference_score, order_count, last_seen)
                VALUES ('driver', ?, ?, 'usual', ?, ?, ?)
                ON CONFLICT(user_scope, category_path, brand, preference_label)
                DO UPDATE SET preference_score = excluded.preference_score, order_count = excluded.order_count, last_seen = excluded.last_seen, updated_at = CURRENT_TIMESTAMP
                """,
                (profile["category_path"], profile["usual_brand"], 0.85, profile["usual_brand_order_count"], profile["last_seen"]),
            )
        if profile["premium_brand"]:
            conn.execute(
                """
                INSERT INTO brand_preferences(user_scope, category_path, brand, preference_label, preference_score, order_count, last_seen)
                VALUES ('driver', ?, ?, 'premium', ?, ?, ?)
                ON CONFLICT(user_scope, category_path, brand, preference_label)
                DO UPDATE SET preference_score = excluded.preference_score, order_count = excluded.order_count, last_seen = excluded.last_seen, updated_at = CURRENT_TIMESTAMP
                """,
                (profile["category_path"], profile["premium_brand"], 0.65, 1, profile["last_seen"]),
            )

        for idx, tier in enumerate(profile["preferred_quality_tiers"]):
            health_profile = profile["health_profile_priority"][0] if profile["health_profile_priority"] else "neutral"
            score = max(0.3, 0.9 - (idx * 0.15))
            conn.execute(
                """
                INSERT INTO quality_preferences(user_scope, category_path, quality_tier, health_profile, preference_score, order_count, last_seen)
                VALUES ('driver', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_scope, category_path, quality_tier, health_profile)
                DO UPDATE SET preference_score = excluded.preference_score, order_count = excluded.order_count, last_seen = excluded.last_seen, updated_at = CURRENT_TIMESTAMP
                """,
                (profile["category_path"], tier, health_profile, score, 1, profile["last_seen"]),
            )

    for row in SEASONAL_SEEDS:
        conn.execute(
            """
            INSERT INTO seasonal_availability(
              category_path, product_keyword, origin_country, season_start_month, season_end_month, peak_months, availability_confidence, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'seeded_knowledge')
            ON CONFLICT(category_path, product_keyword, origin_country)
            DO UPDATE SET season_start_month = excluded.season_start_month,
              season_end_month = excluded.season_end_month,
              peak_months = excluded.peak_months,
              availability_confidence = excluded.availability_confidence,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                row["category_path"],
                row["product_keyword"],
                row["origin_country"],
                row["season_start_month"],
                row["season_end_month"],
                row["peak_months"],
                row["availability_confidence"],
            ),
        )

    conn.commit()
    conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build baseline products + suggestion preference signals")
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "reference" / "orders_seed.json")
    parser.add_argument("--baseline-output", type=Path, default=ROOT / "data" / "derived" / "baseline_products.json")
    parser.add_argument("--category-output", type=Path, default=ROOT / "data" / "derived" / "category_summary.json")
    parser.add_argument("--suggestion-output", type=Path, default=ROOT / "data" / "derived" / "suggestion_profiles.json")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "grocery_agent.db")
    parser.add_argument("--sync-db", action="store_true", help="Write parsed data into SQLite tables")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    orders = load_orders(args.input)
    baseline_products, category_rows, suggestion_profiles = build_baseline(orders)

    args.baseline_output.parent.mkdir(parents=True, exist_ok=True)
    args.baseline_output.write_text(json.dumps(baseline_products, indent=2), encoding="utf-8")
    args.category_output.parent.mkdir(parents=True, exist_ok=True)
    args.category_output.write_text(json.dumps(category_rows, indent=2), encoding="utf-8")
    args.suggestion_output.parent.mkdir(parents=True, exist_ok=True)
    args.suggestion_output.write_text(json.dumps(suggestion_profiles, indent=2), encoding="utf-8")

    if args.sync_db:
        sync_to_db(args.db, orders, baseline_products, suggestion_profiles)

    print(f"Orders processed: {len(orders)}")
    print(f"Unique canonical products: {len(baseline_products)}")
    print("Sample suggestion profiles:")
    for row in suggestion_profiles[:5]:
        print(f"- {row['category_path']}: usual={row['usual_brand']} | quality={','.join(row['preferred_quality_tiers'][:2])}")


if __name__ == "__main__":
    main()
