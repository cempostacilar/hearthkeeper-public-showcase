"""Focused unit tests for pure, tricky logic (improvement plan R8).

Unlike test_e2e.py (heavy, real-cart-building integration tests), these are
fast, dependency-free unit tests. They cover only the *pure* calculations
where a wrong-but-plausible number would slip silently past the E2E suite:

  - product size parsing + unit-price math   (product_score.py)
  - history-based scoring boosts             (product_score.py)
  - preference-aggregation blend math        (preference_engine.py)
  - order coverage-date logic                (list_generate.py)

No test framework — plain asserts. Run:  python3 test_unit.py
Exit code is 0 if everything passes, 1 otherwise.
"""
import sys
from datetime import datetime

from product_score import (
    _parse_size, _normalize_to_base_unit, _calculate_unit_price, _sizes_match,
    _history_store_boost, _history_quality_boost,
)
from preference_engine import _blend_value, _update_confidence, _cap_change
from list_generate import _coverage_cutoff, _day_name, _next_order_date


# ── product_score.py — size parsing & unit-price math ──────────────

def test_parse_size():
    """_parse_size extracts (quantity, unit) from messy size strings."""
    assert _parse_size("500g") == (500.0, "g")
    assert _parse_size("1kg") == (1.0, "kg")          # 'kg' not mis-read as 'g'
    assert _parse_size("2L") == (2.0, "l")
    assert _parse_size("500ml") == (500.0, "ml")      # 'ml' not mis-read as 'l'
    assert _parse_size("15 PCS") == (15.0, "pcs")
    assert _parse_size("6 Eggs") == (6.0, "pcs")
    assert _parse_size("2x15 pcs") == (30.0, "pcs")   # multiplier is expanded
    assert _parse_size("Medium Brown - 30 pcs") == (30.0, "pcs")
    assert _parse_size("") == (None, None)
    assert _parse_size("no numbers here") == (None, None)


def test_normalize_to_base_unit():
    """_normalize_to_base_unit converts g->kg and ml->l for fair comparison."""
    assert _normalize_to_base_unit(500, "g") == (0.5, "kg")
    assert _normalize_to_base_unit(2, "kg") == (2, "kg")
    assert _normalize_to_base_unit(500, "ml") == (0.5, "l")
    assert _normalize_to_base_unit(2, "l") == (2, "l")
    assert _normalize_to_base_unit(12, "pcs") == (12, "pcs")


def test_calculate_unit_price():
    """_calculate_unit_price reduces price+size to a comparable per-unit price."""
    # 500g @ 10 = 20/kg, but 1kg @ 18 = 18/kg — the bigger pack is better value
    assert _calculate_unit_price(10.0, "500g") == (20.0, "kg")
    assert _calculate_unit_price(18.0, "1kg") == (18.0, "kg")
    assert _calculate_unit_price(34.49, "15 PCS") == (2.30, "pcs")
    assert _calculate_unit_price(13.25, "500ml") == (26.50, "l")
    assert _calculate_unit_price(0, "500g") == (None, None)        # no price
    assert _calculate_unit_price(10.0, "garbage") == (None, None)  # unparseable


def test_sizes_match():
    """_sizes_match treats sizes within 10% (once normalized) as equal."""
    assert _sizes_match("500g", "0.5kg") is True    # equal once normalized
    assert _sizes_match("15 pcs", "15 pcs") is True  # exact
    assert _sizes_match("350g", "360g") is True      # within 10% tolerance
    assert _sizes_match("500g", "1kg") is False      # 2x apart
    assert _sizes_match("500g", "500ml") is False    # different base unit
    assert _sizes_match("", "500g") is False         # empty input


# ── product_score.py — history-based scoring boosts ────────────────

def test_history_store_boost():
    """_history_store_boost rewards the store the household usually buys from."""
    # strong preference — 3 of 4 orders here
    pts, _ = _history_store_boost("kibsons", {"stores": {"kibsons": 3}, "total_orders": 4})
    assert pts == 8
    # moderate preference — 2 of 4
    pts, _ = _history_store_boost("kibsons", {"stores": {"kibsons": 2}, "total_orders": 4})
    assert pts == 4
    # weak — 3 of 10 (ratio 0.3) earns nothing
    pts, _ = _history_store_boost("kibsons", {"stores": {"kibsons": 3}, "total_orders": 10})
    assert pts == 0
    # a single order is below the count floor even at ratio 1.0
    pts, _ = _history_store_boost("kibsons", {"stores": {"kibsons": 1}, "total_orders": 1})
    assert pts == 0
    # no history at all
    assert _history_store_boost("kibsons", None) == (0, None)


def test_history_quality_boost():
    """_history_quality_boost rewards products matching past quality keywords."""
    # keyword seen 5x (>=3) earns the full +12
    pts, _ = _history_quality_boost("Grass Fed Beef", {"origins": {"grass fed": 5}})
    assert pts == 12
    # two strong keywords sum to 24 but the cap holds the total at 15
    pts, _ = _history_quality_boost(
        "Organic Grass Fed Beef", {"origins": {"organic": 3, "grass fed": 3}})
    assert pts == 15
    # a product matching no keyword earns nothing
    pts, _ = _history_quality_boost("Plain Beef", {"origins": {"grass fed": 5}})
    assert pts == 0
    # no history
    assert _history_quality_boost("Grass Fed Beef", None) == (0, None)


# ── preference_engine.py — preference-aggregation math ─────────────

def test_blend_value():
    """_blend_value weights new observations less as history accumulates."""
    assert _blend_value(10.0, 20.0, 1.0, 3) == 14.0   # early: 60/40
    assert _blend_value(10.0, 20.0, 1.0, 10) == 13.0  # medium: 70/30
    assert _blend_value(10.0, 20.0, 1.0, 20) == 11.5  # mature: 85/15
    assert _blend_value(10.0, 20.0, 1.0, 5) == 13.0   # boundary into 'medium'
    assert _blend_value(10.0, 20.0, 1.0, 15) == 11.5  # boundary into 'mature'
    assert _blend_value(10.0, 20.0, 0.5, 3) == 12.0   # weaker signal moves less


def test_update_confidence():
    """_update_confidence rises on confirming signals, falls harder on contradicting."""
    assert _update_confidence(0.50, 1.0, True) == 0.55   # +0.05
    assert _update_confidence(0.50, 1.0, False) == 0.42  # -0.08 (penalised harder)
    assert _update_confidence(0.99, 1.0, True) == 1.0    # clamped at ceiling 1.0
    assert _update_confidence(0.12, 1.0, False) == 0.1   # clamped at floor 0.1


def test_cap_change():
    """_cap_change limits a single update to a percentage of the current value."""
    assert _cap_change(100.0, 200.0, 0.2) == 120.0  # +100 capped to +20%
    assert _cap_change(100.0, 110.0, 0.2) == 110.0  # small change passes through
    assert _cap_change(100.0, 50.0, 0.2) == 80.0    # large drop capped too
    assert _cap_change(0.0, 42.0, 0.2) == 42.0      # no base value -> no cap


# ── list_generate.py — order coverage-date logic ──────────────────

def test_coverage_dates():
    """_coverage_cutoff finds the next order day; _day_name maps date -> weekday."""
    # a Tuesday order must last until the next order day (Friday)
    assert _coverage_cutoff("2026-02-24") == "2026-02-27"
    # a Friday order must last until the following Tuesday
    assert _coverage_cutoff("2026-02-27") == "2026-03-03"
    # unparseable input is returned unchanged (caller-safe fallback)
    assert _coverage_cutoff("not-a-date") == "not-a-date"
    assert _day_name("2026-02-24") == "Tuesday"
    assert _day_name("2026-02-27") == "Friday"
    assert _day_name("bad") == ""


def test_next_order_date_invariants():
    """_next_order_date always returns a near-future Tuesday or Friday."""
    result = _next_order_date()
    d = datetime.strptime(result, "%Y-%m-%d").date()
    today = datetime.now().date()
    assert d.weekday() in (1, 4)            # Tuesday or Friday
    assert 0 <= (d - today).days <= 7       # today, or within the next week


# ── runner ────────────────────────────────────────────────────────

def _run():
    tests = [
        test_parse_size,
        test_normalize_to_base_unit,
        test_calculate_unit_price,
        test_sizes_match,
        test_history_store_boost,
        test_history_quality_boost,
        test_blend_value,
        test_update_confidence,
        test_cap_change,
        test_coverage_dates,
        test_next_order_date_invariants,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS   {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL   {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    print("Unit tests - pure logic (R8)\n")
    sys.exit(0 if _run() else 1)
