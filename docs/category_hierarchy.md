# Human-Centric Grocery Category Hierarchy (Baseline v1)

This hierarchy is designed to be understandable in conversation and in WhatsApp summaries.

## Top-level groups

- `produce`
  - `fruits`
  - `vegetables`
  - `herbs-and-aromatics`
  - `olives-and-pickles`
- `protein`
  - `meat`
  - `seafood`
- `dairy-eggs`
  - `eggs`
  - `milk`
  - `yoghurt`
  - `cheese`
- `pantry`
  - `oils-and-vinegars`
  - `grains-and-rice`
  - `pasta-and-noodles`
  - `legumes`
  - `baking`
- `bakery`
  - `bread`
- `snacks`
  - `nuts-and-seeds`
  - `sweet-and-savory`
- `beverages`
  - `water-and-soft-drinks`

## Why this structure

- Mirrors how households think while planning meals.
- Supports preference learning per category (e.g., organic produce, meat origin, snack budget).
- Keeps future store-routing easy (e.g., fresh produce/protein to Kibsons; pantry bulk to Carrefour).

## Expansion method

1. Start with coarse human-friendly groups.
2. Add one level deeper only when needed (e.g., `produce/vegetables/leafy-greens`).
3. Keep unknowns in `other/uncategorized/needs-review` and review weekly.
