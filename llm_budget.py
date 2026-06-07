"""
Hearthkeeper - LLM budget cap.

Records every Anthropic call to the llm_usage table with input/output
token counts + an estimated USD cost, and exposes can_spend() so the
call sites can circuit-break BEFORE making the request once the daily
or monthly cap is exceeded.

Caps default to:
  daily   = $0.50 USD
  monthly = $5.00 USD

Overridable via env (LLM_DAILY_CAP_USD, LLM_MONTHLY_CAP_USD).

Pricing table is hand-maintained from public Anthropic pricing. Add
new entries when we change models. The pricing dict is consulted on
every record_call -- if a model is missing, we log a warning and
estimate as Sonnet (the worse case) so we err on the side of
under-spending.

Public API:
    can_spend(estimated_cost_usd, conn=None) -> bool
    record_call(provider, model, intent, input_tokens, output_tokens,
                conn=None) -> dict (the row that was inserted)
    estimate_cost(model, input_tokens, output_tokens) -> float
    get_usage_summary(conn=None) -> dict
    init_schema(conn) -- idempotent (db_setup.py runs this too)
"""

import os
import sqlite3
from datetime import datetime

from app_logging import get_logger
from db_setup import DB_PATH

logger = get_logger("llm_budget")


# ── Caps ──

DEFAULT_DAILY_CAP_USD = 0.50
DEFAULT_MONTHLY_CAP_USD = 5.00


def _daily_cap() -> float:
    try:
        return float(os.environ.get("LLM_DAILY_CAP_USD", DEFAULT_DAILY_CAP_USD))
    except ValueError:
        return DEFAULT_DAILY_CAP_USD


def _monthly_cap() -> float:
    try:
        return float(os.environ.get("LLM_MONTHLY_CAP_USD", DEFAULT_MONTHLY_CAP_USD))
    except ValueError:
        return DEFAULT_MONTHLY_CAP_USD


# ── Pricing (USD per 1M tokens) ──
#
# Source: public Anthropic pricing as of 2025-Q1. Update when we change
# models. If a model isn't in this dict we fall back to SONNET_PRICING
# (the most expensive) so the budget guard errs on the side of caution.

HAIKU_PRICING = {"input_per_mtok": 0.80, "output_per_mtok": 4.00}
SONNET_PRICING = {"input_per_mtok": 3.00, "output_per_mtok": 15.00}
OPUS_PRICING = {"input_per_mtok": 15.00, "output_per_mtok": 75.00}

_MODEL_PRICING = {
    # Haiku family
    "claude-haiku-4-5": HAIKU_PRICING,
    "claude-haiku-4": HAIKU_PRICING,
    "claude-3-5-haiku-20241022": HAIKU_PRICING,
    "claude-3-5-haiku-latest": HAIKU_PRICING,
    # Sonnet family
    "claude-sonnet-4-5": SONNET_PRICING,
    "claude-sonnet-4-20250514": SONNET_PRICING,
    "claude-sonnet-4-5-20250929": SONNET_PRICING,
    "claude-3-5-sonnet-20241022": SONNET_PRICING,
    "claude-3-5-sonnet-latest": SONNET_PRICING,
    "claude-3-7-sonnet-20250219": SONNET_PRICING,
    "claude-3-7-sonnet-latest": SONNET_PRICING,
    # Opus family
    "claude-opus-4": OPUS_PRICING,
    "claude-opus-4-20250514": OPUS_PRICING,
    "claude-opus-4-5": OPUS_PRICING,
}


def _pricing_for(model: str) -> dict:
    """Return per-mtok pricing for a model name. Conservative fallback
    on unknown models: assume Sonnet pricing so we under-spend rather
    than overshoot."""
    if not model:
        return SONNET_PRICING
    if model in _MODEL_PRICING:
        return _MODEL_PRICING[model]
    # Heuristic match on substrings -- new aliases get reasonable
    # estimates without a code change.
    lower = model.lower()
    if "haiku" in lower:
        return HAIKU_PRICING
    if "sonnet" in lower:
        return SONNET_PRICING
    if "opus" in lower:
        return OPUS_PRICING
    logger.warning(
        "Unknown model %r — defaulting to Sonnet pricing for budget purposes",
        model,
    )
    return SONNET_PRICING


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Convert token counts to USD using the per-model pricing table."""
    p = _pricing_for(model)
    return (
        (input_tokens / 1_000_000.0) * p["input_per_mtok"]
        + (output_tokens / 1_000_000.0) * p["output_per_mtok"]
    )


# ── Schema ──

def init_schema(conn=None):
    """Create the llm_usage table if absent. Idempotent. db_setup.py
    already runs this on app startup; this exists for tests + manual
    one-off scripts that want to use the budget API in isolation."""
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        own = True
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_usage (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                provider      TEXT NOT NULL DEFAULT 'anthropic',
                model         TEXT NOT NULL,
                intent        TEXT,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd      REAL NOT NULL DEFAULT 0,
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_usage_created_at "
            "ON llm_usage(created_at)"
        )
        conn.commit()
    finally:
        if own:
            conn.close()


# ── Spend tracking ──

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _spent_today(conn) -> float:
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total "
        "FROM llm_usage WHERE created_at >= ?",
        (today,),
    ).fetchone()
    return float(row["total"] or 0)


def _spent_month_to_date(conn) -> float:
    mtd_start = datetime.now().strftime("%Y-%m-01")
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total "
        "FROM llm_usage WHERE created_at >= ?",
        (mtd_start,),
    ).fetchone()
    return float(row["total"] or 0)


def can_spend(estimated_cost_usd: float = 0.0, conn=None) -> tuple[bool, str]:
    """Return (ok, reason) for whether the projected cost fits the caps.

    Callers should invoke this BEFORE making the LLM request. The
    estimated_cost is the amount about to be added; we check daily
    AND monthly so a single $0.10 call near month-end can't blow
    the monthly cap even if the daily is fine.

    Returns:
        (True,  "ok")
        (False, "daily_cap")     -- daily total + est would exceed LLM_DAILY_CAP_USD
        (False, "monthly_cap")   -- monthly total + est would exceed LLM_MONTHLY_CAP_USD
        (False, "db_error")      -- could not read the usage table (fail-closed)

    The reason is the same string the caller can pass to log_event
    detail strings, propagate up into action data (e.g.
    `{"type": "budget_skipped", "reason": "daily_cap"}`), and surface
    in the user-visible message so it's obvious WHY the LLM didn't
    run -- not "I don't understand" but "AI budget reached, try
    again tomorrow".
    """
    own = False
    if conn is None:
        conn = _conn()
        own = True
    try:
        d = _spent_today(conn)
        m = _spent_month_to_date(conn)
        if d + estimated_cost_usd > _daily_cap():
            logger.warning(
                "LLM budget: daily cap exceeded (spent=%.4f + est=%.4f > %.4f)",
                d, estimated_cost_usd, _daily_cap(),
            )
            return (False, "daily_cap")
        if m + estimated_cost_usd > _monthly_cap():
            logger.warning(
                "LLM budget: monthly cap exceeded (spent=%.4f + est=%.4f > %.4f)",
                m, estimated_cost_usd, _monthly_cap(),
            )
            return (False, "monthly_cap")
        return (True, "ok")
    except Exception as e:
        logger.warning("LLM budget can_spend failed (fail-closed): %s", e)
        return (False, "db_error")
    finally:
        if own:
            conn.close()


def get_budget_status(conn=None) -> dict:
    """Return a snapshot of the current budget state for the diag
    endpoint and any caller that wants to know "am I about to be
    rejected by can_spend?" without making a call.

    Result:
        {
          "ok":                bool,    -- True if a $0 check would pass
          "reason":            str,     -- "ok" | "daily_cap" | "monthly_cap" | "db_error"
          "daily_cap_usd":     float,
          "monthly_cap_usd":   float,
          "today_spend_usd":   float,
          "monthly_spend_usd": float,
          "daily_remaining_usd":   float,  -- 0 if we're already over
          "monthly_remaining_usd": float,
        }
    """
    own = False
    if conn is None:
        conn = _conn()
        own = True
    try:
        d = _spent_today(conn)
        m = _spent_month_to_date(conn)
        dcap = _daily_cap()
        mcap = _monthly_cap()
        ok, reason = can_spend(0.0, conn=conn)
        return {
            "ok": ok,
            "reason": reason,
            "daily_cap_usd": dcap,
            "monthly_cap_usd": mcap,
            "today_spend_usd": d,
            "monthly_spend_usd": m,
            "daily_remaining_usd": max(0.0, dcap - d),
            "monthly_remaining_usd": max(0.0, mcap - m),
        }
    finally:
        if own:
            conn.close()


def record_call(
    provider: str,
    model: str,
    intent: str | None,
    input_tokens: int,
    output_tokens: int,
    conn=None,
) -> dict:
    """Persist one LLM call. Computes cost from the pricing table.

    Should be called AFTER a successful Anthropic response when the
    token counts are known. Returns a dict with id, cost_usd, etc.
    for the caller to log if it wants to.
    """
    cost = estimate_cost(model, input_tokens, output_tokens)
    own = False
    if conn is None:
        conn = _conn()
        own = True
    try:
        cur = conn.execute(
            """INSERT INTO llm_usage
                   (provider, model, intent, input_tokens,
                    output_tokens, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (provider, model, intent, input_tokens, output_tokens, cost),
        )
        conn.commit()
        row_id = cur.lastrowid
    except Exception as e:
        logger.warning("LLM budget record_call failed: %s", e)
        row_id = None
    finally:
        if own:
            conn.close()
    return {
        "id": row_id,
        "provider": provider,
        "model": model,
        "intent": intent,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
    }


def get_usage_summary(conn=None) -> dict:
    """For diag/admin: today's spend, MTD spend, totals, model breakdown."""
    own = False
    if conn is None:
        conn = _conn()
        own = True
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        mtd_start = datetime.now().strftime("%Y-%m-01")
        today_total = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total, COUNT(*) AS n "
            "FROM llm_usage WHERE created_at >= ?",
            (today,),
        ).fetchone()
        mtd_total = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total, COUNT(*) AS n "
            "FROM llm_usage WHERE created_at >= ?",
            (mtd_start,),
        ).fetchone()
        by_model = conn.execute(
            """SELECT model,
                      COUNT(*) AS calls,
                      COALESCE(SUM(cost_usd), 0) AS cost_usd,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens
               FROM llm_usage
               WHERE created_at >= ?
               GROUP BY model
               ORDER BY cost_usd DESC""",
            (mtd_start,),
        ).fetchall()
        by_intent = conn.execute(
            """SELECT COALESCE(intent, '(none)') AS intent,
                      COUNT(*) AS calls,
                      COALESCE(SUM(cost_usd), 0) AS cost_usd
               FROM llm_usage
               WHERE created_at >= ?
               GROUP BY intent
               ORDER BY cost_usd DESC""",
            (mtd_start,),
        ).fetchall()
        return {
            "daily_cap_usd": _daily_cap(),
            "monthly_cap_usd": _monthly_cap(),
            "today": {
                "spend_usd": float(today_total["total"] or 0),
                "calls": int(today_total["n"] or 0),
                "remaining_usd": max(
                    0.0, _daily_cap() - float(today_total["total"] or 0),
                ),
            },
            "month_to_date": {
                "spend_usd": float(mtd_total["total"] or 0),
                "calls": int(mtd_total["n"] or 0),
                "remaining_usd": max(
                    0.0, _monthly_cap() - float(mtd_total["total"] or 0),
                ),
            },
            "by_model": [dict(r) for r in by_model],
            "by_intent": [dict(r) for r in by_intent],
        }
    finally:
        if own:
            conn.close()
