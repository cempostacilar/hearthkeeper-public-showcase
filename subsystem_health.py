"""
Subsystem health ledger.

The reliability problem this solves: Hearthkeeper runs ~18 background loops on a
home box. When one fails it logs to app_events and (used to) escalate over
WhatsApp -- but WhatsApp is usually the thing that's broken, so failures stayed
invisible for weeks (the nightly scrape and the today-deliveries dispatcher both
ran broken with nobody told).

This module gives ONE always-current answer to "is everything OK", derived from
the events the loops already emit -- no per-loop rewiring. A subsystem is:

  * ok       -- succeeded within its expected cadence and its last run didn't fail
  * degraded -- stale (no success within cadence + grace) OR last run failed
  * down     -- stale AND failing / never succeeded (or a live probe says down)

Staleness is the safety net: a loop that silently dies (or whose success event
stops arriving) goes stale and shows up, even if no error was ever logged.

Pure + testable: get_health() takes the live signals (WhatsApp connected,
scheduler beat age) as arguments rather than reaching into app_state, so it can
be unit-tested against an in-memory app_events table.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional


def _utcnow() -> datetime:
    """Naive UTC now (matches app_events.timestamp, which is naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

from db_setup import DB_PATH

# An event-gated subsystem (a dispatcher) is "degraded" if it crashed within this
# many hours. Slightly over a day so a daily dispatcher that errors stays visible
# until one clean day passes.
ERROR_ONLY_WINDOW_H = 26

# Each entry maps a subsystem to the events its loop already emits and how often
# it SHOULD succeed. ok/fail are app_events.event names; cadence_h is the
# expected success interval (None = "error-only", no staleness check); grace_h is
# slack before we call a missed cadence "stale".
SUBSYSTEMS = [
    {"key": "catalog_scrape", "label": "Nightly catalog refresh",
     "ok": ["nightly_scrape_done"], "fail": ["nightly_scrape_error"],
     "cadence_h": 24, "grace_h": 14,
     "hint": "Kibsons + Carrefour prices/availability. Stale = picks use old data."},
    {"key": "order_poller", "label": "Order tracking poller",
     "ok": ["order_poller_done"], "fail": ["order_poller_error"],
     "cadence_h": 2, "grace_h": 2,
     "hint": "Watches placed orders for status/delivery changes."},
    {"key": "today_deliveries", "label": "Today's-deliveries heads-up",
     "ok": ["b4_today_summary_sent"],
     "fail": ["today_deliveries_dispatcher_error"],
     "cadence_h": None, "grace_h": None,  # event-gated: only sends when deliveries exist
     "hint": "Morning 'arriving today' message (degraded = the dispatcher itself crashed)."},
    {"key": "pattern_detector", "label": "Repeat-drop detector",
     "ok": ["pattern_detector_done"], "fail": ["pattern_detector_error"],
     "cadence_h": 24, "grace_h": 18,
     "hint": "Learns items you keep dropping."},
    {"key": "carrefour_backfill", "label": "Carrefour order backfill",
     "ok": ["carrefour_backfill_done"], "fail": ["carrefour_backfill_error"],
     "cadence_h": 24, "grace_h": 18,
     "hint": "Fills in missing Carrefour order line items."},
    {"key": "notifications", "label": "Notification dispatchers",
     "ok": ["status_update_sent", "delivered_prompt_sent",
            "rating_reminder_sent", "b4_today_summary_sent",
            "status_update_dispatcher_ok"],
     "fail": ["status_update_dispatcher_error", "delivered_rating_dispatcher_error",
              "rating_reminder_dispatcher_error", "today_deliveries_dispatcher_error"],
     "cadence_h": None, "grace_h": None,  # error-only: gated by WhatsApp/quiet hours
     "hint": "Order placed / on-the-way / delivered + rating prompts (machinery only; sending is the WhatsApp subsystem)."},
]


def _latest(conn: sqlite3.Connection, events: list) -> Optional[str]:
    """Most recent app_events.timestamp (UTC) for any of these event names."""
    if not events:
        return None
    ph = ",".join("?" for _ in events)
    row = conn.execute(
        f"SELECT MAX(timestamp) FROM app_events WHERE event IN ({ph})", events
    ).fetchone()
    return row[0] if row and row[0] else None


def _age_hours(ts_str: Optional[str], now: datetime) -> Optional[float]:
    """Hours between an app_events UTC timestamp ('YYYY-MM-DD HH:MM:SS') and now."""
    if not ts_str:
        return None
    s = str(ts_str).strip().replace(" ", "T")
    if "." in s:
        s = s.split(".", 1)[0]
    try:
        ts = datetime.fromisoformat(s)
    except ValueError:
        return None
    return (now - ts).total_seconds() / 3600.0


def _classify(sub: dict, last_ok: Optional[str], last_fail: Optional[str],
              now: datetime) -> dict:
    ok_age = _age_hours(last_ok, now)
    fail_age = _age_hours(last_fail, now)
    if sub.get("cadence_h"):
        # Cadence-based subsystem (should succeed on a regular interval).
        limit = sub["cadence_h"] + (sub.get("grace_h") or 0)
        stale = (ok_age is None) or (ok_age > limit)
        # Its most recent run failed (failure at/after the last success).
        failed_recently = bool(last_fail) and (last_ok is None or str(last_fail) >= str(last_ok))
    else:
        # Event-gated subsystem (dispatchers only act when there's something to
        # do, so we can't expect periodic successes). Health = "did it crash
        # recently", windowed so one clean day clears a stale error.
        stale = False
        failed_recently = (fail_age is not None) and (fail_age < ERROR_ONLY_WINDOW_H)

    if stale and (failed_recently or last_ok is None):
        status = "down"
    elif stale or failed_recently:
        status = "degraded"
    else:
        status = "ok"
    return {
        "key": sub["key"], "label": sub["label"], "status": status,
        "last_success": last_ok, "last_failure": last_fail,
        "last_success_age_h": round(ok_age, 1) if ok_age is not None else None,
        "last_failure_age_h": round(fail_age, 1) if fail_age is not None else None,
        "failed_last_run": failed_recently,
        "cadence_h": sub.get("cadence_h"),
        "hint": sub.get("hint", ""),
    }


def get_health(conn: Optional[sqlite3.Connection] = None, *,
               wa_connected: Optional[bool] = None,
               wa_detail: str = "",
               scheduler_beat_age_s: Optional[int] = None) -> dict:
    """Compute current subsystem health. Live signals are passed in so the core
    logic stays pure/testable. Returns {overall, subsystems[], checked_at}."""
    close = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close = True
    now = _utcnow()
    subs = []
    try:
        for sub in SUBSYSTEMS:
            last_ok = _latest(conn, sub["ok"])
            last_fail = _latest(conn, sub["fail"])
            subs.append(_classify(sub, last_ok, last_fail, now))
    finally:
        if close:
            conn.close()

    # Scheduler liveness (live signal, not an event): a stale heartbeat means the
    # whole background engine is dead even if the HTTP process is up.
    if scheduler_beat_age_s is not None:
        sched_status = "ok" if scheduler_beat_age_s < 600 else "down"
        subs.insert(0, {
            "key": "scheduler", "label": "Background scheduler", "status": sched_status,
            "last_success": None, "last_failure": None,
            "last_success_age_h": round(scheduler_beat_age_s / 3600.0, 2),
            "failed_last_run": sched_status != "ok", "cadence_h": None,
            "hint": f"Heartbeat {scheduler_beat_age_s}s ago. Drives every background loop.",
        })

    # WhatsApp (live probe -- never trust events alone; the sidecar can be running
    # yet not connected).
    if wa_connected is not None:
        subs.append({
            "key": "whatsapp", "label": "WhatsApp sidecar",
            "status": "ok" if wa_connected else "down",
            "last_success": None, "last_failure": None, "last_success_age_h": None,
            "failed_last_run": not wa_connected, "cadence_h": None,
            "hint": (wa_detail or "")[:160] if not wa_connected else "Connected.",
        })

    order = {"down": 0, "degraded": 1, "ok": 2, "unknown": 3}
    overall = min((s["status"] for s in subs),
                  key=lambda st: order.get(st, 3), default="ok")
    return {
        "overall": overall,
        "subsystems": subs,
        "checked_at": now.isoformat() + "Z",
        "degraded": [s["key"] for s in subs if s["status"] != "ok"],
    }
