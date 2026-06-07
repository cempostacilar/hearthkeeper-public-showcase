#!/usr/bin/env python3
"""Slice 2 scaffold: normalize a single Carrefour order into MVP JSON contract.

This script is JSON-first by design:
- In `--demo` mode, it writes a realistic sample extraction payload.
- In `--input` mode, it loads a raw JSON object (from future browser automation),
  validates required fields, normalizes types, and writes canonical output.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class OrderItem:
    name: str
    quantity: float
    unit_price: float | None
    line_total: float | None
    brand: str | None
    size_text: str | None
    product_url: str | None
    category: str | None


REQUIRED_ORDER_FIELDS = {
    "external_order_id",
    "ordered_at",
    "currency",
    "subtotal",
    "delivery_fee",
    "total",
}

REQUIRED_ITEM_FIELDS = {
    "name",
    "quantity",
}


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _validate_top_level(payload: dict[str, Any]) -> None:
    if "order" not in payload or "items" not in payload:
        raise ValueError("Payload must include top-level keys: 'order' and 'items'.")

    missing = REQUIRED_ORDER_FIELDS.difference(payload["order"].keys())
    if missing:
        raise ValueError(f"Order is missing required keys: {sorted(missing)}")

    if not isinstance(payload["items"], list) or not payload["items"]:
        raise ValueError("Items must be a non-empty list.")


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    missing = REQUIRED_ITEM_FIELDS.difference(item.keys())
    if missing:
        raise ValueError(f"Item is missing required keys: {sorted(missing)}")

    normalized = OrderItem(
        name=str(item["name"]).strip(),
        quantity=float(item["quantity"]),
        unit_price=_as_float(item.get("unit_price")),
        line_total=_as_float(item.get("line_total")),
        brand=item.get("brand"),
        size_text=item.get("size_text"),
        product_url=item.get("product_url"),
        category=item.get("category"),
    )

    if not normalized.name:
        raise ValueError("Item name cannot be empty.")

    if normalized.quantity <= 0:
        raise ValueError(f"Item quantity must be > 0 for '{normalized.name}'.")

    return normalized.__dict__


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    _validate_top_level(payload)

    order = payload["order"]
    # Validate datetime parseability (ISO expected)
    datetime.fromisoformat(order["ordered_at"].replace("Z", "+00:00"))

    normalized = {
        "source": "carrefour_uae",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "order": {
            "external_order_id": str(order["external_order_id"]).strip(),
            "ordered_at": str(order["ordered_at"]).strip(),
            "currency": str(order["currency"]).strip(),
            "subtotal": _as_float(order["subtotal"]),
            "delivery_fee": _as_float(order["delivery_fee"]),
            "total": _as_float(order["total"]),
        },
        "items": [_normalize_item(item) for item in payload["items"]],
    }

    if not normalized["order"]["external_order_id"]:
        raise ValueError("external_order_id cannot be empty.")

    return normalized


def demo_payload() -> dict[str, Any]:
    return {
        "order": {
            "external_order_id": "CF-2026-000123",
            "ordered_at": "2026-02-20T10:30:00+04:00",
            "currency": "AED",
            "subtotal": 53.75,
            "delivery_fee": 5.0,
            "total": 58.75,
        },
        "items": [
            {
                "name": "Almarai Fresh Milk Full Fat 2L",
                "quantity": 2,
                "unit_price": 10.25,
                "line_total": 20.5,
                "brand": "Almarai",
                "size_text": "2 L",
                "product_url": "https://www.carrefouruae.com/fmcg/almarai-fresh-milk-2l/p/12345",
                "category": "Dairy",
            },
            {
                "name": "Carrefour Brown Eggs Large 15 pcs",
                "quantity": 1,
                "unit_price": 12.5,
                "line_total": 12.5,
                "brand": "Carrefour",
                "size_text": "15 pcs",
                "product_url": "https://www.carrefouruae.com/fresh-food/eggs/p/67890",
                "category": "Dairy & Eggs",
            },
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize Carrefour single-order extraction JSON")
    parser.add_argument("--input", type=Path, help="Raw JSON input file to normalize")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/raw/carrefour/latest_order.json"),
        help="Destination for normalized JSON",
    )
    parser.add_argument("--demo", action="store_true", help="Write demo payload contract")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.demo and args.input:
        raise SystemExit("Use either --demo or --input, not both.")

    if not args.demo and not args.input:
        raise SystemExit("Provide --demo or --input <file>.")

    raw = demo_payload() if args.demo else json.loads(args.input.read_text(encoding="utf-8"))
    normalized = _normalize_payload(raw)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    print(f"Wrote normalized order JSON: {args.output}")


if __name__ == "__main__":
    main()
