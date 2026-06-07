# Suggestion Engine Data Model (MVP)

This layer is preference + behavior driven and should improve continuously as more orders and user decisions are collected.

## Core preference dimensions

1. **Brand level**
   - `usual`: most frequently selected in category.
   - `premium`: higher-spend/high-quality option user has accepted.

2. **SKU quality level**
   - Organic, free-range, grass-fed, fresh-natural, conventional.
   - Used as first-class substitution criteria.

3. **Health profile**
   - `good-healthy`, `neutral`, `not-great`.
   - Used to avoid quality regressions when swapping products.

4. **Seasonality (produce-first)**
   - Seeded season windows by product keyword and origin.
   - Over time should be replaced by observed availability from actual scrape data.

## Conversational reference policy

The assistant should explain substitutions in user language:

- "Your usual Jenan eggs are unavailable. I found a free-range option with similar quality."
- "Only conventional eggs are available. Do you want me to switch this time or skip?"

## Continuous feedback loop

Every suggestion should be logged in `suggestion_events` with:
- requested item
- suggested item
- reason text
- user response (accepted/rejected/custom)

That feedback can later update preference scores automatically.

## Data quality note

Current Kibsons entries are screenshot-derived seeds. As full invoice exports/scraped histories are added, these records should be reconciled and confidence scores increased.

---

## 2026-06-07 — Preference-learning re-architecture (slices 1–6)

Goal (user): suggestions must **align with what's actually ordered** — "we are not
trying to build a new behaviour." The added value is picking the right product
(brand/price/quality) and actioning feedback, not inventing preferences. This pass
took a step back over the whole capture → understand → action flow, root-caused the
gaps, and unified it.

### The flow (upstream → downstream)

**Inputs (capture):**
- Scraped vendor orders (Kibsons + Carrefour) — the real "all orders" positive signal.
- In-app scheduled-list feedback: pin / never-brand / wrong-item / accept-alternative.
- WhatsApp family edits: skip / have-it / swap.
- Delivered-order ratings → LLM → durable rules.

**Stores (3, now consistently written AND read):**
- `user_item_prefs` — per-item: pin_product / block_brand / wrong_item / stop_suggesting.
- `preference_state` — the learning engine (timing/quantity/brand/product/store/
  attribute/item_inclusion dims, confidence + obs counts), fed by `emit_signal`.
- `household_rules` — category-level prefer_brand / avoid_brand / prefer_attribute /
  prefer_cheaper, from delivered feedback + accepted swaps.

**Consumers (now unified):**
- `list_quality_validator.score_candidate` — picks the scheduled-list product.
- `product_score.score_products` — the chat curator + scrapers + knowledge builder.
- Both now honor `household_rules` + `user_item_prefs` + purchase history with the
  **same** category-match logic and magnitudes.

### Root-caused gaps and the slices that closed them

1. **Slice 1 — feedback didn't action the list.** The validator ignored
   `user_item_prefs`/`preference_state`, so a "wrong item" flag neither removed the
   item nor stopped it reappearing. Validator now consumes per-item feedback
   (blocked SKU rejected, pin wins, never-brand avoided); free-text runs through an
   LLM (`capture_item_feedback`); undo (`reset_item_ladder` kind="blocks") clears it.
2. **Slice 2 — schedule refresh 3×/day** (`SCHEDULED_LIST_REFRESH_INTERVAL_S = 8h`),
   regenerating the in-cycle list or rolling forward, preserving user edits.
3. **Slice 3 — orders → positive learning.** Validator boosts the brand the
   household actually buys (graduated by frequency from `get_purchase_history`).
4. **Slice 4 — alternatives X-vs-Y.** Accepting an alternative now compares the
   chosen product X vs the replaced Y and learns the differentiator (brand /
   organic / cheaper), not just a blind SKU pin.
5. **Slice 5 — WhatsApp edits → durable learning.** The X-vs-Y comparison is now
   canonical in `household_rules` (`swap_differentiators` + `learn_from_swap`); the
   in-app sheet AND the WhatsApp swap pick share it (tagged by `signal_source`). A
   WhatsApp skip now emits an `item_skip` inclusion signal (was a silent one-cycle
   drop).
6. **Slice 6 — unify the read side.** The chat curator (`product_score`) had ZERO
   `household_rules` awareness, so a brand learned in one place was ignored in the
   other. It now resolves the applicable rules once per item (`_resolve_rules_view`)
   and applies prefer_brand (+25) / avoid_brand (-40), mirroring the validator.

### Architecture principle

The **SKU pin** locks the *specific* choice (exact product, incl. its size/type).
`swap_differentiators` *generalizes* the transferable axes (brand / organic / price)
into category rules both surfaces honor. We deliberately do **not** capture signals
we can't action (the user's own rule): a WhatsApp "have-it" still records the
external purchase but emits no learning signal, because the existing
`offline_purchase` signal shrinks the restock interval — the wrong direction for
"I already have it." That needs its own design.

### QA

`scripts/e2e_smoke.py` Phases 34–39 cover the loop (validator honors feedback,
purchase-history alignment, X-vs-Y, WhatsApp learning, curator/validator unification).
497 passed, 0 failed. Deployed across commits faaf2c6 → 9b2dfd8.

### Known follow-ups (separate from preference learning)

- External-purchase ("have it" / "I bought milk") → a correctly-directed learning
  signal (reset cadence clock, not shrink interval).
- "Added value" optimization epics the user raised but not yet built: vendor choice
  by price+availability, delivery-fee-aware cart building, capturing missed items,
  faster matching. These need product decisions before implementation.
