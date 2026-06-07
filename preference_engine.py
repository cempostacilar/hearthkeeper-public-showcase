"""
AutoRestock - Preference Learning Engine

The brain that learns from every user interaction and improves suggestions.

Architecture: Signal -> Aggregate -> Read

1. EMIT:  Every input channel (app UI, chat, gap analysis, offline) writes
          preference_signals via emit_signal().

2. AGGREGATE: process_pending_signals() reads unprocessed signals and updates
              the preference_state table using blending formulas.

3. READ:  get_scoring_profile(item_id) returns a dict in the exact format
          that product_score.py expects, so the scoring engine needs zero
          changes to its core logic.

Signal Sources:
  - app_ui:       Qty buttons, remove, pin product, block brand, wrong item
  - chat:         "add yogurt", "skip salmon", "I prefer Wagyu", "only organic"
  - gap_analysis: matched, removed, added, qty_changed, substituted
  - offline:      Manual purchases reported via chat
  - seed:         Initial baseline from consumption_habits.py

Dimensions:
  - timing:          avg_days_between (when to restock)
  - quantity:        avg_quantity (how much per order)
  - brand:           preferred / acceptable / avoid brands
  - product:         pinned / blocked specific products
  - store:           preferred store for this item
  - attribute:       require / prefer / avoid keywords, size, price, flexibility
  - item_inclusion:  should this item be on the list at all
"""

import json
import sqlite3
from datetime import datetime
from typing import List, Optional, Any

from db_setup import DB_PATH


# ============================================================
# CONSTANTS
# ============================================================

# Signal strength defaults by type
SIGNAL_STRENGTHS = {
    "pin_product":           1.0,
    "block_brand":           1.0,
    "wrong_item":            0.9,
    "prefer_store":          0.9,
    "brand_pref_stated":     0.85,
    "attribute_pref_stated": 0.85,
    "item_skip":             0.4,   # single skip; streaks boost this
    "qty_change":            0.6,
    "item_add":              0.5,
    "item_approve":          0.3,
    "gap_matched":           0.4,
    "gap_removed":           0.5,
    "gap_added":             0.5,
    "gap_qty_changed":       0.5,
    "gap_substituted":       0.6,
    "purchase_observed":     0.7,
    "offline_purchase":      0.6,
    "seed_baseline":         0.8,
}

# Max change caps per single aggregation pass
MAX_TIMING_CHANGE_PCT = 0.25      # +-25% on avg_days_between
MAX_QUANTITY_CHANGE_PCT = 0.50    # +-50% on avg_quantity
MAX_CONFIDENCE_CHANGE = 0.15      # +-0.15 on confidence

# Signals that should be processed immediately (not batched)
IMMEDIATE_SIGNALS = {"pin_product", "block_brand", "wrong_item", "prefer_store"}

# Seed decay thresholds (days without confirming signal)
SEED_DECAY_MODERATE_DAYS = 60     # confidence * 0.75
SEED_DECAY_HEAVY_DAYS = 120       # confidence * 0.5

# Self-trusting threshold
SELF_TRUST_MIN_OBSERVATIONS = 3
SELF_TRUST_MIN_CONFIDENCE = 0.6
SELF_TRUST_MIN_DAYS = 14


# ============================================================
# EMIT - Create preference signals
# ============================================================

def emit_signal(
    item_id: int,
    signal_type: str,
    signal_source: str,
    dimension: str,
    old_value: Any = None,
    new_value: Any = None,
    strength: float = None,
    context: Any = None,
    conn: sqlite3.Connection = None,
):
    """
    Record a preference signal from any input channel.

    If signal_type is in IMMEDIATE_SIGNALS, also triggers immediate
    processing so the effect is visible right away.

    Args:
        item_id:       Which item this is about (can be None for global signals)
        signal_type:   What happened (see SIGNAL_STRENGTHS keys)
        signal_source: Where the signal came from (app_ui, chat, gap_analysis, offline, seed)
        dimension:     What aspect of preference (timing, quantity, brand, product, store, attribute, item_inclusion)
        old_value:     Previous value (dict/str/number, will be JSON-encoded)
        new_value:     New/implied value (dict/str/number, will be JSON-encoded)
        strength:      Signal weight 0.0-1.0 (defaults to SIGNAL_STRENGTHS[signal_type])
        context:       Extra context (dict, will be JSON-encoded)
        conn:          Optional DB connection (creates one if not provided)
    """
    if strength is None:
        strength = SIGNAL_STRENGTHS.get(signal_type, 0.5)

    # JSON-encode complex values
    old_json = json.dumps(old_value) if old_value is not None else None
    new_json = json.dumps(new_value) if new_value is not None else None
    ctx_json = json.dumps(context) if context is not None else None

    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    conn.execute("""
        INSERT INTO preference_signals
            (item_id, signal_type, signal_source, dimension,
             old_value, new_value, strength, context)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (item_id, signal_type, signal_source, dimension,
          old_json, new_json, strength, ctx_json))
    conn.commit()

    # Immediate processing for explicit user actions
    if signal_type in IMMEDIATE_SIGNALS and item_id:
        _process_single_signal_immediate(item_id, signal_type, dimension,
                                         new_json, strength, conn)
        conn.commit()

    if close_conn:
        conn.close()


def _process_single_signal_immediate(item_id, signal_type, dimension,
                                      new_json, strength, conn):
    """Process high-priority signals immediately (pin, block, wrong_item, prefer_store)."""
    val = json.loads(new_json) if new_json else {}

    if signal_type == "pin_product":
        _upsert_preference_state(
            conn, item_id, "product", "pinned",
            json.dumps({"catalog_id": val.get("catalog_id"),
                        "product_name": val.get("product_name", "")}),
            confidence=1.0, source_type="learned"
        )

    elif signal_type == "block_brand":
        # Add to the avoid list
        current = _get_preference_state(conn, item_id, "brand", "avoid")
        brands = json.loads(current["value"]).get("brands", []) if current else []
        brand = val.get("brand", "")
        if brand and brand not in brands:
            brands.append(brand)
        _upsert_preference_state(
            conn, item_id, "brand", "avoid",
            json.dumps({"brands": brands}),
            confidence=1.0, source_type="learned"
        )

    elif signal_type == "wrong_item":
        if dimension == "product":
            # Block specific catalog product
            current = _get_preference_state(conn, item_id, "product", "blocked")
            ids = json.loads(current["value"]).get("catalog_ids", []) if current else []
            cat_id = val.get("catalog_id")
            if cat_id and cat_id not in ids:
                ids.append(cat_id)
            _upsert_preference_state(
                conn, item_id, "product", "blocked",
                json.dumps({"catalog_ids": ids}),
                confidence=1.0, source_type="learned"
            )
        elif dimension == "brand":
            # Penalize this brand (add to avoid list)
            brand = val.get("brand", "")
            if brand:
                current = _get_preference_state(conn, item_id, "brand", "avoid")
                avoid_data = json.loads(current["value"]) if current else {"brands": []}
                avoid_list = avoid_data.get("brands", [])
                if brand not in avoid_list:
                    avoid_list.append(brand)
                _upsert_preference_state(
                    conn, item_id, "brand", "avoid",
                    json.dumps({"brands": avoid_list}),
                    confidence=0.8, source_type="learned"
                )

    elif signal_type == "prefer_store":
        _upsert_preference_state(
            conn, item_id, "store", "preferred",
            json.dumps({"store_id": val.get("store_id")}),
            confidence=1.0, source_type="learned"
        )


# ============================================================
# AGGREGATE - Process pending signals
# ============================================================

def process_pending_signals(batch_size: int = 100, conn: sqlite3.Connection = None):
    """
    Process all unprocessed signals and update preference_state.

    Called at end-of-interaction (list finalize), post-order (gap analysis),
    or on a timer.

    Returns count of signals processed.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    signals = conn.execute("""
        SELECT * FROM preference_signals
        WHERE is_processed = 0
        ORDER BY created_at ASC
        LIMIT ?
    """, (batch_size,)).fetchall()

    processed = 0
    for sig in signals:
        try:
            _process_signal(sig, conn)
            conn.execute("UPDATE preference_signals SET is_processed = 1 WHERE id = ?",
                         (sig["id"],))
            processed += 1
        except Exception as e:
            # Log but don't stop — one bad signal shouldn't block the rest
            print(f"[preference_engine] Error processing signal {sig['id']}: {e}")

    conn.commit()
    if close_conn:
        conn.close()

    return processed


def _process_signal(sig, conn):
    """Route a signal to the appropriate dimension handler."""
    dim = sig["dimension"]
    item_id = sig["item_id"]

    if not item_id:
        return  # Global signals — future use

    # Skip immediate signals that were already processed
    if sig["signal_type"] in IMMEDIATE_SIGNALS:
        return

    if dim == "timing":
        _aggregate_timing(item_id, sig, conn)
    elif dim == "quantity":
        _aggregate_quantity(item_id, sig, conn)
    elif dim == "brand":
        _aggregate_brand(item_id, sig, conn)
    elif dim == "product":
        _aggregate_product(item_id, sig, conn)
    elif dim == "store":
        _aggregate_store(item_id, sig, conn)
    elif dim == "attribute":
        _aggregate_attribute(item_id, sig, conn)
    elif dim == "item_inclusion":
        _aggregate_inclusion(item_id, sig, conn)


# --- Timing aggregation ---

def _aggregate_timing(item_id, sig, conn):
    """Update avg_days_between based on timing signals."""
    current = _get_preference_state(conn, item_id, "timing", "avg_days_between")

    if not current:
        return  # No baseline to adjust from

    current_val = json.loads(current["value"])
    current_days = current_val.get("days", 3.5)
    obs_count = current["observation_count"]

    signal_type = sig["signal_type"]
    new_val = json.loads(sig["new_value"]) if sig["new_value"] else {}
    strength = sig["strength"]

    if signal_type == "gap_matched":
        # Good prediction — boost confidence, don't change timing
        new_conf = _update_confidence(current["confidence"], strength, is_confirming=True)
        _upsert_preference_state(
            conn, item_id, "timing", "avg_days_between",
            current["value"], confidence=new_conf,
            source_type=_evolve_source_type(current["source_type"])
        )

    elif signal_type == "gap_removed":
        reason = new_val.get("reason", "unknown")
        if reason in ("still_have_stock", "unknown"):
            # Stretch the interval — they didn't need it yet
            new_days = _blend_value(current_days, current_days * 1.20, strength, obs_count)
            new_days = _cap_change(current_days, new_days, MAX_TIMING_CHANGE_PCT)
            new_conf = _update_confidence(current["confidence"], strength, is_confirming=False)
            _upsert_preference_state(
                conn, item_id, "timing", "avg_days_between",
                json.dumps({"days": new_days}),
                confidence=new_conf,
                source_type=_evolve_source_type(current["source_type"])
            )

    elif signal_type in ("gap_added", "offline_purchase"):
        # Needed sooner — shrink the interval
        new_days = _blend_value(current_days, current_days * 0.80, strength, obs_count)
        new_days = _cap_change(current_days, new_days, MAX_TIMING_CHANGE_PCT)
        new_conf = _update_confidence(current["confidence"], strength, is_confirming=False)
        _upsert_preference_state(
            conn, item_id, "timing", "avg_days_between",
            json.dumps({"days": new_days}),
            confidence=new_conf,
            source_type=_evolve_source_type(current["source_type"])
        )

    # Also update restock_rules for backward compatibility
    _sync_to_restock_rules(item_id, conn)


# --- Quantity aggregation ---

def _aggregate_quantity(item_id, sig, conn):
    """Update avg_quantity based on quantity signals."""
    current = _get_preference_state(conn, item_id, "quantity", "avg_quantity")

    if not current:
        return

    current_val = json.loads(current["value"])
    current_qty = current_val.get("qty", 1.0)
    obs_count = current["observation_count"]
    strength = sig["strength"]

    new_val = json.loads(sig["new_value"]) if sig["new_value"] else {}

    if sig["signal_type"] in ("qty_change", "gap_qty_changed"):
        observed_qty = new_val.get("qty", current_qty)
        new_qty = _blend_value(current_qty, observed_qty, strength, obs_count)
        new_qty = max(0.5, _cap_change(current_qty, new_qty, MAX_QUANTITY_CHANGE_PCT))
        new_qty = round(new_qty, 1)

        is_confirming = abs(observed_qty - current_qty) < 0.3
        new_conf = _update_confidence(current["confidence"], strength, is_confirming)

        _upsert_preference_state(
            conn, item_id, "quantity", "avg_quantity",
            json.dumps({"qty": new_qty}),
            confidence=new_conf,
            source_type=_evolve_source_type(current["source_type"])
        )

    elif sig["signal_type"] == "gap_matched":
        # Quantity was right — boost confidence
        new_conf = _update_confidence(current["confidence"], strength, is_confirming=True)
        _upsert_preference_state(
            conn, item_id, "quantity", "avg_quantity",
            current["value"], confidence=new_conf,
            source_type=_evolve_source_type(current["source_type"])
        )

    _sync_to_restock_rules(item_id, conn)


# --- Brand aggregation ---

def _aggregate_brand(item_id, sig, conn):
    """Update brand preferences based on brand signals."""
    signal_type = sig["signal_type"]
    new_val = json.loads(sig["new_value"]) if sig["new_value"] else {}

    if signal_type in ("purchase_observed", "gap_substituted"):
        # Extract brand from observed product name
        product_name = new_val.get("product", "")
        brand = new_val.get("brand", "")

        if brand:
            # Boost this brand in the preferred list
            current = _get_preference_state(conn, item_id, "brand", "preferred")
            pref_data = json.loads(current["value"]) if current else {"brands": [], "scores": {}}
            brands = pref_data.get("brands", [])
            scores = pref_data.get("scores", {})

            # Increment score for this brand
            scores[brand] = scores.get(brand, 0) + sig["strength"]

            # Re-sort brands by score
            sorted_brands = sorted(scores.keys(), key=lambda b: scores[b], reverse=True)
            brands = sorted_brands[:5]  # keep top 5

            _upsert_preference_state(
                conn, item_id, "brand", "preferred",
                json.dumps({"brands": brands, "scores": scores}),
                confidence=min(1.0, sig["strength"]),
                source_type="learned"
            )

    elif signal_type in ("brand_pref_stated", "brand_avoid_stated"):
        # Direct statement: "I prefer X brand" or "no more X"
        # Support both key formats: "brand"/"action" and "value"/"pref_type"
        brand = new_val.get("brand", "") or new_val.get("value", "")
        action = new_val.get("action", "")
        if not action:
            pref_type = new_val.get("pref_type", "")
            if "avoid" in pref_type or "avoid" in signal_type:
                action = "avoid"
            else:
                action = "prefer"

        if action == "prefer" and brand:
            current = _get_preference_state(conn, item_id, "brand", "preferred")
            pref_data = json.loads(current["value"]) if current else {"brands": [], "scores": {}}
            brands = pref_data.get("brands", [])
            scores = pref_data.get("scores", {})
            scores[brand] = scores.get(brand, 0) + 2.0  # strong boost for explicit statement
            sorted_brands = sorted(scores.keys(), key=lambda b: scores[b], reverse=True)
            brands = sorted_brands[:5]
            _upsert_preference_state(
                conn, item_id, "brand", "preferred",
                json.dumps({"brands": brands, "scores": scores}),
                confidence=0.9, source_type="learned"
            )

        elif action == "avoid" and brand:
            current = _get_preference_state(conn, item_id, "brand", "avoid")
            avoid_data = json.loads(current["value"]) if current else {"brands": []}
            avoid_list = avoid_data.get("brands", [])
            if brand not in avoid_list:
                avoid_list.append(brand)
            _upsert_preference_state(
                conn, item_id, "brand", "avoid",
                json.dumps({"brands": avoid_list}),
                confidence=0.9, source_type="learned"
            )

    elif signal_type == "wrong_item":
        # Thumbs-down on delivered item → penalize the brand
        brand = new_val.get("brand", "")
        if brand:
            current = _get_preference_state(conn, item_id, "brand", "avoid")
            avoid_data = json.loads(current["value"]) if current else {"brands": []}
            avoid_list = avoid_data.get("brands", [])
            if brand not in avoid_list:
                avoid_list.append(brand)
            _upsert_preference_state(
                conn, item_id, "brand", "avoid",
                json.dumps({"brands": avoid_list}),
                confidence=0.8, source_type="learned"
            )


# --- Product aggregation ---

def _aggregate_product(item_id, sig, conn):
    """Update product-level preferences (pinned, blocked)."""
    # Pin/block are handled immediately in emit_signal.
    # This handles softer signals like observed purchases.
    signal_type = sig["signal_type"]
    new_val = json.loads(sig["new_value"]) if sig["new_value"] else {}

    if signal_type == "purchase_observed":
        # Track which products are actually bought (for ranking boost)
        product_name = new_val.get("product", "")
        if product_name:
            current = _get_preference_state(conn, item_id, "product", "history")
            history = json.loads(current["value"]) if current else {"products": {}}
            products = history.get("products", {})
            products[product_name] = products.get(product_name, 0) + 1
            _upsert_preference_state(
                conn, item_id, "product", "history",
                json.dumps({"products": products}),
                confidence=0.7, source_type="learned"
            )


# --- Store aggregation ---

def _aggregate_store(item_id, sig, conn):
    """Update store preferences. prefer_store is handled immediately."""
    # Non-immediate store signals could come from gap analysis
    # (e.g., item always bought at a different store than suggested)
    pass  # Future: detect cross-store purchase patterns


# --- Attribute aggregation ---

def _aggregate_attribute(item_id, sig, conn):
    """Update attribute preferences (require, prefer, avoid keywords)."""
    if sig["signal_type"] == "attribute_pref_stated":
        new_val = json.loads(sig["new_value"]) if sig["new_value"] else {}
        # Support both key formats: "action"/"keyword" and "pref_type"/"value"
        action = new_val.get("action", "")
        if not action:
            pref_type = new_val.get("pref_type", "")
            if "require" in pref_type:
                action = "require"
            elif "avoid" in pref_type:
                action = "avoid"
            else:
                action = "prefer"
        keyword = new_val.get("keyword", "") or new_val.get("value", "")
        keywords = new_val.get("keywords", [keyword] if keyword else [])

        if not keywords:
            return

        key = "require" if action == "require" else "prefer" if action == "prefer" else "avoid"
        current = _get_preference_state(conn, item_id, "attribute", key)
        current_data = json.loads(current["value"]) if current else {"keywords": []}
        existing = current_data.get("keywords", [])

        for kw in keywords:
            if kw and kw.lower() not in [k.lower() for k in existing]:
                existing.append(kw)

        _upsert_preference_state(
            conn, item_id, "attribute", key,
            json.dumps({"keywords": existing}),
            confidence=0.85, source_type="learned"
        )


# --- Item inclusion aggregation ---

def _aggregate_inclusion(item_id, sig, conn):
    """Track whether an item should be included. Detects skip streaks."""
    current = _get_preference_state(conn, item_id, "item_inclusion", "active")
    current_data = json.loads(current["value"]) if current else {"is_active": True, "skip_streak": 0}

    if sig["signal_type"] == "item_skip":
        streak = current_data.get("skip_streak", 0) + 1
        _upsert_preference_state(
            conn, item_id, "item_inclusion", "active",
            json.dumps({"is_active": streak < 5, "skip_streak": streak}),
            confidence=min(0.9, 0.3 + streak * 0.15),
            source_type="learned"
        )

        # If 3+ skips, also emit a timing stretch
        if streak >= 3:
            timing = _get_preference_state(conn, item_id, "timing", "avg_days_between")
            if timing:
                days = json.loads(timing["value"]).get("days", 3.5)
                new_days = round(days * 1.25, 1)
                _upsert_preference_state(
                    conn, item_id, "timing", "avg_days_between",
                    json.dumps({"days": new_days}),
                    confidence=timing["confidence"],
                    source_type=_evolve_source_type(timing["source_type"])
                )
                _sync_to_restock_rules(item_id, conn)

    elif sig["signal_type"] in ("item_add", "item_approve", "gap_matched",
                                "purchase_observed"):
        # Reset skip streak — the item is confirmed wanted (re-added, approved,
        # matched a gap, OR observed purchased, incl. an external "have it"). This
        # reaffirms inclusion WITHOUT touching the restock interval: a "have it"
        # means they restocked now, not that they need it sooner, so the clock
        # reset is left to the external_purchases cadence math, not an
        # interval-shrinking timing signal.
        _upsert_preference_state(
            conn, item_id, "item_inclusion", "active",
            json.dumps({"is_active": True, "skip_streak": 0}),
            confidence=max(current_data.get("skip_streak", 0) * 0.1, 0.5),
            source_type="learned"
        )


# ============================================================
# READ - Query current preference state
# ============================================================

def get_scoring_profile(item_id: int, conn: sqlite3.Connection = None) -> dict:
    """
    Build a scoring profile from preference_state.

    Returns a dict in the EXACT same format as PREFERENCE_PROFILES in
    product_score.py, so the scoring engine needs zero changes.

    Falls back to static PREFERENCE_PROFILES if no preference_state exists.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    rows = conn.execute("""
        SELECT dimension, key, value, confidence, source_type, last_signal_at
        FROM preference_state
        WHERE item_id = ?
    """, (item_id,)).fetchall()

    if close_conn:
        conn.close()

    if not rows:
        # No learned data — fall back to static profiles
        return _fallback_to_static(item_id)

    # Build profile from preference_state
    profile = {
        "require": [],
        "prefer": [],
        "avoid": [],
        "brands": {"preferred": [], "acceptable": [], "avoid": []},
        "varieties": [],
        "size_target": "",
        "price_range": {},
        "flexibility": "moderate",
    }

    for row in rows:
        dim = row["dimension"]
        key = row["key"]
        val = json.loads(row["value"])
        conf = _apply_seed_decay(row)

        if dim == "attribute":
            if key == "require":
                profile["require"] = val.get("keywords", [])
            elif key == "prefer":
                profile["prefer"] = val.get("keywords", [])
            elif key == "avoid":
                profile["avoid"] = val.get("keywords", [])
            elif key == "size_target":
                profile["size_target"] = val.get("size", "")
            elif key == "price_range":
                profile["price_range"] = val
            elif key == "flexibility":
                profile["flexibility"] = val.get("level", "moderate")
            elif key == "varieties":
                profile["varieties"] = val.get("varieties", [])

        elif dim == "brand":
            if key == "preferred":
                profile["brands"]["preferred"] = val.get("brands", [])
            elif key == "acceptable":
                profile["brands"]["acceptable"] = val.get("brands", [])
            elif key == "avoid":
                profile["brands"]["avoid"] = val.get("brands", [])

        elif dim == "product":
            if key == "pinned":
                profile["_pinned"] = val  # special key for scoring engine
            elif key == "blocked":
                profile["_blocked"] = val

    return profile



def get_preference_explanation(item_id: int, conn: sqlite3.Connection = None) -> List[str]:
    """
    Human-readable explanation of current preferences for an item.

    Returns list of strings like:
      "Brand: Jenan (preferred) -- bought 8 times"
      "Timing: every 3.5 days -- seed value, confirmed by 4 orders"
      "Organic: required -- from initial setup, never contradicted"
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    rows = conn.execute("""
        SELECT dimension, key, value, confidence, observation_count, source_type
        FROM preference_state
        WHERE item_id = ?
        ORDER BY dimension, key
    """, (item_id,)).fetchall()

    # Get item name
    item_row = conn.execute("SELECT name FROM items WHERE id = ?", (item_id,)).fetchone()
    item_name = item_row["name"] if item_row else f"Item #{item_id}"

    if close_conn:
        conn.close()

    explanations = [f"Preferences for {item_name}:"]

    for row in rows:
        val = json.loads(row["value"])
        dim = row["dimension"]
        key = row["key"]
        source = row["source_type"]
        obs = row["observation_count"]
        conf = row["confidence"]
        source_label = "from your setup" if source == "seed" else f"learned from {obs} observations" if source == "learned" else f"initial + {obs - 1} observations"

        if dim == "timing" and key == "avg_days_between":
            days = val.get("days", "?")
            explanations.append(f"  Timing: every {days} days ({source_label}, {conf:.0%} confidence)")

        elif dim == "quantity" and key == "avg_quantity":
            qty = val.get("qty", "?")
            explanations.append(f"  Quantity: {qty} per order ({source_label})")

        elif dim == "brand" and key == "preferred":
            brands = val.get("brands", [])
            if brands:
                explanations.append(f"  Preferred brands: {', '.join(brands)} ({source_label})")

        elif dim == "brand" and key == "avoid":
            brands = val.get("brands", [])
            if brands:
                explanations.append(f"  Avoided brands: {', '.join(brands)} ({source_label})")

        elif dim == "attribute" and key == "require":
            kws = val.get("keywords", [])
            if kws:
                explanations.append(f"  Required: {', '.join(kws)} ({source_label})")

        elif dim == "store" and key == "preferred":
            store_id = val.get("store_id")
            explanations.append(f"  Store: #{store_id} ({source_label})")

        elif dim == "product" and key == "pinned":
            name = val.get("product_name", "")
            explanations.append(f"  Pinned product: {name}")

        elif dim == "item_inclusion" and key == "active":
            active = val.get("is_active", True)
            streak = val.get("skip_streak", 0)
            if not active:
                explanations.append(f"  Status: INACTIVE (skipped {streak} times)")
            elif streak > 0:
                explanations.append(f"  Status: active but skipped {streak} recent times")

    return explanations


def is_self_trusting(item_id: int, dimension: str, conn: sqlite3.Connection = None) -> bool:
    """
    Check if the system has enough real data to trust its own predictions
    for this item/dimension, without relying on seed data.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    rows = conn.execute("""
        SELECT observation_count, confidence, source_type, last_signal_at
        FROM preference_state
        WHERE item_id = ? AND dimension = ?
    """, (item_id, dimension)).fetchall()

    if close_conn:
        conn.close()

    for row in rows:
        if _is_self_trusting(row):
            return True
    return False


# ============================================================
# SEED MIGRATION
# ============================================================

def seed_initial_preferences(conn: sqlite3.Connection = None):
    """
    One-time migration: convert static HABITS + PREFERENCE_PROFILES
    into preference_state rows.

    This is the bridge from hardcoded seed data to the dynamic learning engine.
    After this runs, get_scoring_profile() will return data from preference_state
    instead of falling back to static dicts.
    """
    from consumption_habits import HABITS, FREQUENCY_TO_DAYS
    from product_score import PREFERENCE_PROFILES

    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    cur = conn.cursor()

    for habit in HABITS:
        # Find item_id
        row = cur.execute("SELECT id FROM items WHERE name = ?",
                          (habit["name"],)).fetchone()
        if not row:
            continue
        item_id = row["id"]

        # Check if already seeded
        existing = cur.execute(
            "SELECT COUNT(*) FROM preference_state WHERE item_id = ? AND source_type = 'seed'",
            (item_id,)
        ).fetchone()[0]
        if existing > 0:
            continue  # Already seeded

        avg_days = FREQUENCY_TO_DAYS.get(habit["frequency"], 7.0)

        # --- Timing ---
        _upsert_preference_state(
            conn, item_id, "timing", "avg_days_between",
            json.dumps({"days": avg_days}),
            confidence=0.8, source_type="seed"
        )

        # --- Quantity ---
        _upsert_preference_state(
            conn, item_id, "quantity", "avg_quantity",
            json.dumps({"qty": habit.get("typical_qty", 1.0)}),
            confidence=0.8, source_type="seed"
        )

        # --- Store ---
        store_name = habit.get("preferred_store", "")
        store_row = cur.execute("SELECT id FROM stores WHERE name = ?",
                                (store_name,)).fetchone()
        if store_row:
            _upsert_preference_state(
                conn, item_id, "store", "preferred",
                json.dumps({"store_id": store_row["id"], "store_name": store_name}),
                confidence=0.8, source_type="seed"
            )

        # --- Brand + Attribute from PREFERENCE_PROFILES ---
        profile = PREFERENCE_PROFILES.get(habit["name"], {})
        if not profile:
            continue

        # Brand preferences
        brands = profile.get("brands", {})
        if brands.get("preferred"):
            _upsert_preference_state(
                conn, item_id, "brand", "preferred",
                json.dumps({"brands": brands["preferred"],
                            "scores": {b: 2.0 for b in brands["preferred"]}}),
                confidence=0.8, source_type="seed"
            )
        if brands.get("acceptable"):
            _upsert_preference_state(
                conn, item_id, "brand", "acceptable",
                json.dumps({"brands": brands["acceptable"]}),
                confidence=0.8, source_type="seed"
            )
        if brands.get("avoid"):
            _upsert_preference_state(
                conn, item_id, "brand", "avoid",
                json.dumps({"brands": brands["avoid"]}),
                confidence=0.8, source_type="seed"
            )

        # Attribute preferences
        if profile.get("require"):
            _upsert_preference_state(
                conn, item_id, "attribute", "require",
                json.dumps({"keywords": profile["require"]}),
                confidence=0.9, source_type="seed"  # requirements are high confidence
            )
        if profile.get("prefer"):
            _upsert_preference_state(
                conn, item_id, "attribute", "prefer",
                json.dumps({"keywords": profile["prefer"]}),
                confidence=0.8, source_type="seed"
            )
        if profile.get("avoid"):
            _upsert_preference_state(
                conn, item_id, "attribute", "avoid",
                json.dumps({"keywords": profile["avoid"]}),
                confidence=0.8, source_type="seed"
            )
        if profile.get("varieties"):
            _upsert_preference_state(
                conn, item_id, "attribute", "varieties",
                json.dumps({"varieties": profile["varieties"]}),
                confidence=0.7, source_type="seed"
            )
        if profile.get("size_target"):
            _upsert_preference_state(
                conn, item_id, "attribute", "size_target",
                json.dumps({"size": profile["size_target"]}),
                confidence=0.7, source_type="seed"
            )
        if profile.get("price_range"):
            _upsert_preference_state(
                conn, item_id, "attribute", "price_range",
                json.dumps(profile["price_range"]),
                confidence=0.7, source_type="seed"
            )
        if profile.get("flexibility"):
            _upsert_preference_state(
                conn, item_id, "attribute", "flexibility",
                json.dumps({"level": profile["flexibility"]}),
                confidence=0.8, source_type="seed"
            )

        # --- Item inclusion ---
        _upsert_preference_state(
            conn, item_id, "item_inclusion", "active",
            json.dumps({"is_active": True, "skip_streak": 0}),
            confidence=0.8, source_type="seed"
        )

    conn.commit()
    if close_conn:
        conn.close()

    print("[preference_engine] Seed migration complete.")


# ============================================================
# INTERNAL HELPERS
# ============================================================

def _get_preference_state(conn, item_id, dimension, key):
    """Get a single preference_state row."""
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT * FROM preference_state
        WHERE item_id = ? AND dimension = ? AND key = ?
    """, (item_id, dimension, key)).fetchone()


def _upsert_preference_state(conn, item_id, dimension, key, value,
                              confidence=None, source_type=None):
    """Insert or update a preference_state row. Increments observation_count."""
    existing = _get_preference_state(conn, item_id, dimension, key)

    if existing:
        obs = existing["observation_count"] + 1
        conf = confidence if confidence is not None else existing["confidence"]
        src = source_type if source_type is not None else existing["source_type"]
        conn.execute("""
            UPDATE preference_state
            SET value = ?, confidence = ?, observation_count = ?,
                source_type = ?, last_signal_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id = ?
        """, (value, conf, obs, src, existing["id"]))
    else:
        conf = confidence if confidence is not None else 0.5
        src = source_type if source_type is not None else "learned"
        conn.execute("""
            INSERT INTO preference_state
                (item_id, dimension, key, value, confidence,
                 observation_count, source_type, last_signal_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, datetime('now'))
        """, (item_id, dimension, key, value, conf, src))


def _blend_value(current: float, observed: float, strength: float,
                 observation_count: int) -> float:
    """
    Blend old value with new observation.

    More observations = less weight on each new one (stabilizing).
    - Early (<5 observations):  60/40 — learn fast
    - Medium (5-15):            70/30 — current behavior
    - Mature (15+):             85/15 — resist noise
    """
    if observation_count < 5:
        weight_new = 0.4 * strength
    elif observation_count < 15:
        weight_new = 0.3 * strength
    else:
        weight_new = 0.15 * strength

    weight_old = 1.0 - weight_new
    return round(current * weight_old + observed * weight_new, 2)


def _update_confidence(current: float, strength: float,
                       is_confirming: bool) -> float:
    """
    Adjust confidence based on signal.
    Confirming signals boost, contradicting signals penalize harder.
    """
    if is_confirming:
        delta = 0.05 * strength
        return min(1.0, round(current + delta, 3))
    else:
        delta = 0.08 * strength
        return max(0.1, round(current - delta, 3))


def _cap_change(current: float, proposed: float, max_pct: float) -> float:
    """Cap the change to a maximum percentage of the current value."""
    if current == 0:
        return proposed
    max_delta = abs(current * max_pct)
    delta = proposed - current
    if abs(delta) > max_delta:
        delta = max_delta if delta > 0 else -max_delta
    return round(current + delta, 2)


def _apply_seed_decay(row) -> float:
    """
    Seed data confidence decays if no confirming signals arrive.
    After 60 days: * 0.75. After 120 days: * 0.5.
    Real observations (non-seed source_type) are unaffected.
    """
    if row["source_type"] != "seed":
        return row["confidence"]

    last_signal = row["last_signal_at"]
    if not last_signal:
        return row["confidence"]

    try:
        last_dt = datetime.strptime(last_signal, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return row["confidence"]

    days_since = (datetime.now() - last_dt).days

    if days_since > SEED_DECAY_HEAVY_DAYS:
        return max(0.3, row["confidence"] * 0.5)
    elif days_since > SEED_DECAY_MODERATE_DAYS:
        return max(0.5, row["confidence"] * 0.75)
    else:
        return row["confidence"]


def _is_self_trusting(row) -> bool:
    """Check if a preference_state row represents self-trusting learned data."""
    if row["source_type"] == "seed":
        return False

    real_obs = row["observation_count"]
    if row["source_type"] == "mixed":
        real_obs -= 1  # subtract the seed observation

    if real_obs < SELF_TRUST_MIN_OBSERVATIONS:
        return False
    if row["confidence"] < SELF_TRUST_MIN_CONFIDENCE:
        return False

    # Check time since first real signal
    last_signal = row["last_signal_at"]
    if not last_signal:
        return False

    return True  # Simplified — proper check would need first_signal_at


def _evolve_source_type(current_source: str) -> str:
    """Evolve source_type as real data accumulates."""
    if current_source == "seed":
        return "mixed"
    return current_source  # "mixed" stays "mixed" until self-trusting flips to "learned"


def _sync_to_restock_rules(item_id: int, conn: sqlite3.Connection):
    """
    Keep restock_rules in sync with preference_state for backward compatibility.
    The list generator reads from restock_rules, so we mirror timing/quantity there.
    """
    timing = _get_preference_state(conn, item_id, "timing", "avg_days_between")
    quantity = _get_preference_state(conn, item_id, "quantity", "avg_quantity")

    updates = []
    params = []

    if timing:
        t_val = json.loads(timing["value"])
        updates.append("avg_days_between = ?")
        params.append(t_val.get("days"))
        updates.append("confidence = ?")
        params.append(timing["confidence"])
        updates.append("source_type = ?")
        params.append(timing["source_type"])

    if quantity:
        q_val = json.loads(quantity["value"])
        updates.append("avg_quantity = ?")
        params.append(q_val.get("qty"))

    if updates:
        updates.append("updated_at = datetime('now')")
        params.append(item_id)
        conn.execute(
            f"UPDATE restock_rules SET {', '.join(updates)} WHERE item_id = ?",
            params
        )


def _fallback_to_static(item_id: int) -> dict:
    """Fall back to static PREFERENCE_PROFILES when no preference_state exists."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT name FROM items WHERE id = ?", (item_id,)).fetchone()
        conn.close()
        if row:
            from product_score import PREFERENCE_PROFILES
            return PREFERENCE_PROFILES.get(row["name"], {})
    except Exception:
        pass
    return {}


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python preference_engine.py <command>")
        print("Commands:")
        print("  seed      - Run initial seed migration")
        print("  process   - Process pending signals")
        print("  explain N - Show preferences for item N")
        print("  status    - Show signal/state counts")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "seed":
        seed_initial_preferences()

    elif cmd == "process":
        count = process_pending_signals()
        print(f"Processed {count} signals.")

    elif cmd == "explain":
        item_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        for line in get_preference_explanation(item_id):
            print(line)

    elif cmd == "status":
        conn = sqlite3.connect(DB_PATH)
        sig_count = conn.execute("SELECT COUNT(*) FROM preference_signals").fetchone()[0]
        unprocessed = conn.execute("SELECT COUNT(*) FROM preference_signals WHERE is_processed=0").fetchone()[0]
        state_count = conn.execute("SELECT COUNT(*) FROM preference_state").fetchone()[0]
        seed_count = conn.execute("SELECT COUNT(*) FROM preference_state WHERE source_type='seed'").fetchone()[0]
        learned_count = conn.execute("SELECT COUNT(*) FROM preference_state WHERE source_type='learned'").fetchone()[0]
        mixed_count = conn.execute("SELECT COUNT(*) FROM preference_state WHERE source_type='mixed'").fetchone()[0]
        conn.close()
        print(f"Signals: {sig_count} total, {unprocessed} unprocessed")
        print(f"State:   {state_count} total ({seed_count} seed, {learned_count} learned, {mixed_count} mixed)")

    else:
        print(f"Unknown command: {cmd}")
