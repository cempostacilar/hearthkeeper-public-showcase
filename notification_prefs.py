"""Per-member notification preferences (B7).

Single source of truth for two questions every WhatsApp dispatcher
needs to answer:

  1. Does this member want to be notified about <notification_type>?
  2. Is this member's quiet hours window active right now?

The data lives in `household_members.notification_prefs` as JSON.
Missing or malformed values fall back to defaults that preserve the
historical behavior: the driver gets every notification type, with
22:00-07:00 quiet hours. Other members are off by default.

Usage:
    from notification_prefs import should_notify, is_quiet_hours
    if not should_notify(member_id, "b3_dispatched"):
        continue
    if is_quiet_hours(member_id):
        continue

The fail-open philosophy from the dispatcher pattern applies here:
any DB error or JSON parse failure returns the default behavior
rather than silently skipping notifications.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Optional

from db_setup import DB_PATH


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

# Canonical notification-type keys. Each B-series dispatcher passes
# the matching key into should_notify(). Keep this list aligned with
# the JSON shape in db_setup.py.
NOTIFICATION_KEYS = (
    "b2_order_placed",
    "b3_dispatched",
    "b4_today_deliveries",
    "b5_delivered",
    "b6_reminder",
)

# Default quiet hours: 22:00-07:00 UAE local. Applied when the
# member's prefs JSON doesn't override.
DEFAULT_QUIET_START = 22
DEFAULT_QUIET_END = 7


# ─────────────────────────────────────────────
# Internal: load + parse
# ─────────────────────────────────────────────

def _get_conn(conn=None):
    if conn is not None:
        return conn, False
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c, True


def _load_raw(member_id: int, conn=None) -> tuple[Optional[dict], Optional[str]]:
    """Return (parsed_prefs_dict_or_None, role_or_None).

    Both fields fail-open: a missing member or unparseable JSON
    yields (None, None) and the caller falls back to defaults.
    """
    own = False
    c, own = _get_conn(conn)
    try:
        row = c.execute(
            "SELECT role, notification_prefs FROM household_members WHERE id = ?",
            (member_id,),
        ).fetchone()
    except Exception:
        if own:
            try:
                c.close()
            except Exception:
                pass
        return None, None
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass

    if not row:
        return None, None
    role = row["role"] if isinstance(row, sqlite3.Row) else row[0]
    raw = row["notification_prefs"] if isinstance(row, sqlite3.Row) else row[1]
    if not raw:
        return None, role
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None, role
        return parsed, role
    except Exception:
        return None, role


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def get_member_prefs(member_id: int, conn=None) -> dict:
    """Return the effective prefs for a member with defaults applied.

    Shape:
        {
          "role": "driver" | "member" | ...,
          "notifications": {kind: bool, ...} for every key in NOTIFICATION_KEYS,
          "quiet_hours": {"start": int, "end": int},
        }

    Defaults:
      * Driver: every notification on, quiet 22-07.
      * Other roles: every notification off (so we don't accidentally
        spam family members until they explicitly opt in). Quiet
        hours still default to 22-07 for when they do opt in.
      * Per-key override always wins over role default.
    """
    parsed, role = _load_raw(member_id, conn=conn)
    role = role or ""
    role_default_on = role.lower() == "driver"

    notif_overrides = {}
    quiet_overrides = {}
    if parsed:
        n = parsed.get("notifications")
        if isinstance(n, dict):
            for k, v in n.items():
                if k in NOTIFICATION_KEYS and isinstance(v, bool):
                    notif_overrides[k] = v
        q = parsed.get("quiet_hours")
        if isinstance(q, dict):
            s = q.get("start")
            e = q.get("end")
            if isinstance(s, int) and 0 <= s <= 23:
                quiet_overrides["start"] = s
            if isinstance(e, int) and 0 <= e <= 23:
                quiet_overrides["end"] = e

    notifications = {}
    for k in NOTIFICATION_KEYS:
        notifications[k] = notif_overrides.get(k, role_default_on)

    quiet_hours = {
        "start": quiet_overrides.get("start", DEFAULT_QUIET_START),
        "end":   quiet_overrides.get("end",   DEFAULT_QUIET_END),
    }

    return {
        "role": role,
        "notifications": notifications,
        "quiet_hours": quiet_hours,
    }


def should_notify(member_id: int, kind: str, conn=None) -> bool:
    """True iff `member_id` wants to receive `kind` notifications.

    Unknown notification types return False -- callers should only
    pass values from NOTIFICATION_KEYS. The strictness here prevents
    accidental "send everything to everyone" if a typo lands in
    one of the dispatchers.
    """
    if kind not in NOTIFICATION_KEYS:
        return False
    prefs = get_member_prefs(member_id, conn=conn)
    return bool(prefs["notifications"].get(kind, False))


def is_quiet_hours(member_id: int, now: Optional[datetime] = None,
                   conn=None) -> bool:
    """True iff the current local time falls inside the member's
    quiet-hours window.

    Handles the cross-midnight case: start=22, end=7 means
    "quiet from 22:00 through 06:59". A window where start < end
    (e.g. 12-14, a midday "do not disturb") is treated as a
    normal closed interval [start, end).
    """
    prefs = get_member_prefs(member_id, conn=conn)
    qs = prefs["quiet_hours"]["start"]
    qe = prefs["quiet_hours"]["end"]
    if now is None:
        now = datetime.now()
    hour = now.hour

    if qs == qe:
        # Pathological config: quiet all day or quiet never. Treat
        # equal start+end as "no quiet hours" so we never send the
        # household into permanent silence by accident.
        return False
    if qs < qe:
        # Normal interval (e.g. 12-14): quiet IFF start <= hour < end.
        return qs <= hour < qe
    # Cross-midnight (e.g. 22-7): quiet IFF hour >= start OR hour < end.
    return hour >= qs or hour < qe


def list_recipients(kind: str, conn=None) -> list[dict]:
    """Return every household_member who wants to receive `kind`
    right now (passes both should_notify and NOT is_quiet_hours).

    Each dict: {"id", "name", "phone", "role"}. Empty list when
    nobody is eligible -- caller stays silent.

    Used by dispatchers that want to broadcast to anyone subscribed
    (e.g. B4 today's-deliveries). Single-recipient dispatchers can
    keep their existing `role='driver'` lookup and just call
    should_notify + is_quiet_hours on the resolved id.
    """
    if kind not in NOTIFICATION_KEYS:
        return []
    own = False
    c, own = _get_conn(conn)
    try:
        rows = c.execute(
            """SELECT id, name, phone, role
               FROM household_members
               WHERE COALESCE(phone, '') != ''"""
        ).fetchall()
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass
    out = []
    for r in rows:
        rid = r["id"] if isinstance(r, sqlite3.Row) else r[0]
        if not should_notify(rid, kind, conn=conn):
            continue
        if is_quiet_hours(rid, conn=conn):
            continue
        out.append({
            "id": rid,
            "name": (r["name"] if isinstance(r, sqlite3.Row) else r[1]),
            "phone": (r["phone"] if isinstance(r, sqlite3.Row) else r[2]),
            "role": (r["role"] if isinstance(r, sqlite3.Row) else r[3]),
        })
    return out
