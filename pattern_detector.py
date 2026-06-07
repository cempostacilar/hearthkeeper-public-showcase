"""
Pattern detector for the Tier-1/Tier-2 escalation ladder.

Reads skip_events history and decides whether to:
  - silently nudge cadence (Tier 1: 2-3 recent drops -> extend gap)
  - auto-stop suggesting (Tier 3: 5+ recent drops -> pref_type='stop_suggesting')
  - queue a Tier-2 ask (3-4 drops in a row -> "want me to stop?" prompt,
    deferred for the asking-over-WhatsApp slice)

Designed to run nightly. Idempotent: re-running on the same data
produces the same adjustments (UPDATE ... SET cadence_adjustment = ?
is a write of the same value, not an increment).

Public API:
    detect_drop_patterns(conn, days=30, min_drops=2) -> list[dict]
    apply_pattern_consequences(conn, patterns, dry_run=False) -> list[dict]
    send_tier2_asks(conn, patterns, wa_client=None, dry_run=False)
        -> list[dict]  # E4: one-time "want me to stop?" WhatsApp ask
    run(conn=None, dry_run=False, send_asks=True) -> dict   # full pipeline
"""

import asyncio
import json
import sqlite3
from datetime import datetime

from db_setup import DB_PATH


# Thresholds. Tune by feel later -- starting conservative.
DEFAULT_WINDOW_DAYS = 30
SOFT_NUDGE_AT = 2   # 2+ drops -> cadence_adjustment = 1.25
HARDER_NUDGE_AT = 3  # 3+ drops -> cadence_adjustment = 1.5 + Tier-2 ask
AUTO_STOP_AT = 5     # 5+ drops -> stop_suggesting pref (revertable)

# Sources we count toward the pattern. Skips coming FROM the pattern
# detector itself shouldn't trigger more pattern detection.
COUNTED_SOURCES = (
    "curator_drop",
    "reviewer_drop",
    "reviewer_have_it",
    "curator_have_it",
    "reviewer_wrong",
)


def detect_drop_patterns(
    conn,
    days: int = DEFAULT_WINDOW_DAYS,
    min_drops: int = SOFT_NUDGE_AT,
):
    """Find items with elevated drop counts in the recent window.

    Returns list of dicts:
        {item_id, item_name, drop_count, last_drop, tier, action,
         new_cadence_adjustment OR new_pref}

    Where:
        tier = 'soft' (2x) | 'firmer' (3x-4x) | 'auto_stop' (5x+)
        action = the suggested DB write
    """
    placeholders = ",".join("?" for _ in COUNTED_SOURCES)
    rows = conn.execute(
        f"""SELECT se.item_id,
                   i.name AS item_name,
                   COUNT(*) AS drop_count,
                   MAX(se.created_at) AS last_drop,
                   MIN(se.created_at) AS first_drop
            FROM skip_events se
            LEFT JOIN items i ON i.id = se.item_id
            WHERE se.item_id IS NOT NULL
              AND se.created_at >= datetime('now', ?)
              AND se.source IN ({placeholders})
            GROUP BY se.item_id
            HAVING COUNT(*) >= ?
            ORDER BY drop_count DESC""",
        (f"-{days} days", *COUNTED_SOURCES, min_drops),
    ).fetchall()

    patterns = []
    for r in rows:
        count = int(r["drop_count"])
        item_id = r["item_id"]
        item_name = r["item_name"] or f"item#{item_id}"
        last_drop = r["last_drop"]
        first_drop = r["first_drop"]

        if count >= AUTO_STOP_AT:
            patterns.append({
                "item_id": item_id,
                "item_name": item_name,
                "drop_count": count,
                "first_drop": first_drop,
                "last_drop": last_drop,
                "tier": "auto_stop",
                "action": "stop_suggesting",
                "new_pref_value": "auto:repeated_drops",
            })
        elif count >= HARDER_NUDGE_AT:
            patterns.append({
                "item_id": item_id,
                "item_name": item_name,
                "drop_count": count,
                "first_drop": first_drop,
                "last_drop": last_drop,
                "tier": "firmer",
                "action": "cadence_adjustment",
                "new_cadence_adjustment": 1.5,
                "tier2_ask_pending": True,
            })
        else:  # >= SOFT_NUDGE_AT
            patterns.append({
                "item_id": item_id,
                "item_name": item_name,
                "drop_count": count,
                "first_drop": first_drop,
                "last_drop": last_drop,
                "tier": "soft",
                "action": "cadence_adjustment",
                "new_cadence_adjustment": 1.25,
            })

    return patterns


def apply_pattern_consequences(conn, patterns, dry_run: bool = False):
    """Apply each pattern's recommended action.

    For 'cadence_adjustment': UPDATE restock_rules.cadence_adjustment.
    For 'stop_suggesting': INSERT a user_item_prefs row (or reactivate
    if one already exists from a previous detector run -- never inserts
    duplicates).

    Idempotent. Safe to call repeatedly.

    Returns list of {item_id, item_name, before, after, applied}.
    """
    results = []
    for p in patterns:
        item_id = p["item_id"]
        item_name = p["item_name"]

        if p["action"] == "cadence_adjustment":
            new_mult = p["new_cadence_adjustment"]
            try:
                row = conn.execute(
                    "SELECT cadence_adjustment FROM restock_rules WHERE item_id = ?",
                    (item_id,),
                ).fetchone()
                before = (
                    float(row["cadence_adjustment"])
                    if row and row["cadence_adjustment"] is not None
                    else 1.0
                )
            except Exception:
                before = 1.0

            applied = False
            if not dry_run:
                try:
                    # Only escalate -- never reduce.
                    if new_mult > before:
                        conn.execute(
                            """UPDATE restock_rules
                               SET cadence_adjustment = ?,
                                   updated_at = datetime('now')
                               WHERE item_id = ?""",
                            (new_mult, item_id),
                        )
                        conn.commit()
                        applied = True
                except Exception:
                    pass

            results.append({
                "item_id": item_id,
                "item_name": item_name,
                "tier": p["tier"],
                "action": "cadence_adjustment",
                "before": before,
                "after": new_mult if applied else before,
                "applied": applied,
                "drop_count": p["drop_count"],
                "tier2_ask_pending": p.get("tier2_ask_pending", False),
            })

        elif p["action"] == "stop_suggesting":
            existing = None
            try:
                existing = conn.execute(
                    """SELECT id, is_active FROM user_item_prefs
                       WHERE item_id = ? AND pref_type = 'stop_suggesting'
                       LIMIT 1""",
                    (item_id,),
                ).fetchone()
            except Exception:
                existing = None

            applied = False
            if not dry_run:
                try:
                    if existing:
                        # Reactivate if it had been turned off
                        if not existing["is_active"]:
                            conn.execute(
                                """UPDATE user_item_prefs
                                   SET is_active = 1
                                   WHERE id = ?""",
                                (existing["id"],),
                            )
                            applied = True
                    else:
                        conn.execute(
                            """INSERT INTO user_item_prefs
                                   (item_id, pref_type, value, is_active)
                               VALUES (?, 'stop_suggesting', ?, 1)""",
                            (item_id, p["new_pref_value"]),
                        )
                        applied = True
                    conn.commit()
                except Exception:
                    pass

            results.append({
                "item_id": item_id,
                "item_name": item_name,
                "tier": "auto_stop",
                "action": "stop_suggesting",
                "applied": applied,
                "drop_count": p["drop_count"],
                "was_already_stopped": bool(existing),
            })

    return results


def send_tier2_asks(conn, patterns, wa_client=None, dry_run: bool = False):
    """E4 — Tier-2 ask delivery.

    For each 'firmer'-tier pattern with tier2_ask_pending=True, send a
    one-time "want me to stop suggesting X?" WhatsApp message to the
    member who most recently dropped that item. Marks the ask in
    user_item_prefs(pref_type='tier2_asked') so we never re-ask the
    same item, and stows the pending state on
    household_members.member_pending_intent so the reply lands on the
    tier2 handler in feedback_manager.

    Returns a list of {item_id, item_name, member_id, member_name,
    phone, sent, reason} dicts -- one per pattern that was considered.

    Idempotent: items with an existing tier2_asked row are skipped.
    """
    if wa_client is None and not dry_run:
        # No client -- treat the same as dry_run for safety; we'll
        # still mark "would have asked" rows in the return so the
        # caller can see what was planned.
        wa_client = None

    asks = []
    for p in patterns:
        if not p.get("tier2_ask_pending"):
            continue

        item_id = p["item_id"]
        item_name = p["item_name"]

        # Have we already asked about this item?
        already_asked = None
        try:
            already_asked = conn.execute(
                """SELECT id FROM user_item_prefs
                   WHERE item_id = ?
                     AND pref_type = 'tier2_asked'
                     AND is_active = 1
                   LIMIT 1""",
                (item_id,),
            ).fetchone()
        except Exception:
            already_asked = None
        if already_asked:
            asks.append({
                "item_id": item_id,
                "item_name": item_name,
                "sent": False,
                "reason": "already_asked",
            })
            continue

        # Find the most-recent dropper among COUNTED_SOURCES. They're
        # the strongest signal-holder, and the most likely to respond.
        placeholders = ",".join("?" for _ in COUNTED_SOURCES)
        dropper_row = None
        try:
            dropper_row = conn.execute(
                f"""SELECT se.member_id, hm.name, hm.phone, hm.member_pending_intent
                    FROM skip_events se
                    LEFT JOIN household_members hm ON hm.id = se.member_id
                    WHERE se.item_id = ?
                      AND se.member_id IS NOT NULL
                      AND se.source IN ({placeholders})
                    ORDER BY se.created_at DESC
                    LIMIT 1""",
                (item_id, *COUNTED_SOURCES),
            ).fetchone()
        except Exception:
            dropper_row = None

        if not dropper_row or not dropper_row["phone"]:
            asks.append({
                "item_id": item_id,
                "item_name": item_name,
                "sent": False,
                "reason": "no_dropper_with_phone",
            })
            continue

        # Don't double-pend: if the member already has a pending
        # intent we set earlier (Tier-2 ask or any other multi-turn
        # state), skip this round and try next night.
        if dropper_row["member_pending_intent"]:
            asks.append({
                "item_id": item_id,
                "item_name": item_name,
                "member_id": dropper_row["member_id"],
                "member_name": dropper_row["name"],
                "sent": False,
                "reason": "member_busy_with_other_intent",
            })
            continue

        member_id = dropper_row["member_id"]
        member_name = dropper_row["name"]
        phone = dropper_row["phone"]

        # Render and send.
        try:
            from whatsapp_agent import format_tier2_ask
            text = format_tier2_ask(item_name, p["drop_count"])
        except Exception as e:
            asks.append({
                "item_id": item_id,
                "item_name": item_name,
                "sent": False,
                "reason": f"format_failed: {e}",
            })
            continue

        sent_ok = False
        wa_msg_id = None
        if dry_run or wa_client is None:
            sent_ok = False  # planned, not sent
        else:
            try:
                # The wa_client method is async; pattern_detector itself
                # is sync, so we run the send on a fresh event loop. This
                # is fine when called from the scheduler (each nightly
                # task runs in its own asyncio context) and also from
                # the diag endpoint (FastAPI runs in asyncio, so we use
                # asyncio.run_coroutine_threadsafe via a thread wrapper).
                wa_msg_id = _send_sync(
                    wa_client.send_message(phone, text, member_id=member_id)
                )
                sent_ok = bool(wa_msg_id)
            except Exception as e:
                asks.append({
                    "item_id": item_id,
                    "item_name": item_name,
                    "member_id": member_id,
                    "member_name": member_name,
                    "sent": False,
                    "reason": f"send_failed: {e}",
                })
                continue

        # Persist BOTH side effects together so a partial state can't
        # happen: tier2_asked row + member_pending_intent JSON.
        if sent_ok:
            try:
                conn.execute(
                    """INSERT INTO user_item_prefs
                           (item_id, pref_type, value, is_active)
                       VALUES (?, 'tier2_asked', ?, 1)""",
                    (item_id, datetime.now().isoformat(timespec="seconds")),
                )
                pending_payload = {
                    "type": "tier2_ask",
                    "item_id": item_id,
                    "item_name": item_name,
                    "asked_at": datetime.now().isoformat(timespec="seconds"),
                    "drop_count": p["drop_count"],
                }
                conn.execute(
                    "UPDATE household_members SET member_pending_intent = ? WHERE id = ?",
                    (json.dumps(pending_payload), member_id),
                )
                conn.commit()
            except Exception as e:
                # Persistence failed AFTER we sent -- log but report
                # the send anyway so the caller knows the user got
                # the message.
                asks.append({
                    "item_id": item_id,
                    "item_name": item_name,
                    "member_id": member_id,
                    "member_name": member_name,
                    "sent": True,
                    "wa_message_id": wa_msg_id,
                    "reason": f"persist_failed_after_send: {e}",
                })
                continue

        asks.append({
            "item_id": item_id,
            "item_name": item_name,
            "member_id": member_id,
            "member_name": member_name,
            "sent": sent_ok,
            "wa_message_id": wa_msg_id,
            "reason": "ok" if sent_ok else "dry_run_or_no_client",
        })

    return asks


def get_active_learning_state(conn):
    """A12 — read-only snapshot of what the ladder has actively done.

    Returns:
        {
          "cadence_adjustments": [
              {"item_id", "item_name", "category", "multiplier",
               "updated_at"} ...
          ],
          "stops": [
              {"item_id", "item_name", "category", "source", "since"} ...
          ],
        }

    Where:
        cadence_adjustments    -- restock_rules rows with cadence_adjustment > 1.0
        stops                  -- active user_item_prefs(stop_suggesting) rows
        source                 -- "auto" for auto:* values (deactivatable by
                                  E6 re-approve), "user" for user:* values
                                  (sticky until explicit override), "manual"
                                  for anything else.

    Used by the WebSocket get_learning_items action to populate the
    "Recently learned" panel on the household frontend.
    """
    cadence = []
    try:
        rows = conn.execute(
            """SELECT r.item_id,
                      i.name AS item_name,
                      i.category,
                      r.cadence_adjustment AS multiplier,
                      r.updated_at
               FROM restock_rules r
               JOIN items i ON i.id = r.item_id
               WHERE r.cadence_adjustment > 1.0
               ORDER BY r.updated_at DESC""",
        ).fetchall()
        for r in rows:
            cadence.append({
                "item_id": r["item_id"],
                "item_name": r["item_name"],
                "category": r["category"],
                "multiplier": float(r["multiplier"]),
                "updated_at": r["updated_at"],
            })
    except Exception:
        cadence = []

    stops = []
    try:
        rows = conn.execute(
            """SELECT uip.item_id,
                      i.name AS item_name,
                      i.category,
                      uip.value,
                      uip.created_at AS since
               FROM user_item_prefs uip
               JOIN items i ON i.id = uip.item_id
               WHERE uip.pref_type = 'stop_suggesting'
                 AND uip.is_active = 1
               ORDER BY uip.created_at DESC""",
        ).fetchall()
        for r in rows:
            value = r["value"] or ""
            if value.startswith("auto:"):
                source = "auto"
            elif value.startswith("user:"):
                source = "user"
            else:
                source = "manual"
            stops.append({
                "item_id": r["item_id"],
                "item_name": r["item_name"],
                "category": r["category"],
                "source": source,
                "since": r["since"],
            })
    except Exception:
        stops = []

    return {"cadence_adjustments": cadence, "stops": stops}


def reset_item_ladder(conn, item_id: int, kind: str = "all") -> dict:
    """A12 — reverse the ladder for an item.

    kind:
        "cadence"  -> only reset restock_rules.cadence_adjustment to 1.0
        "stop"     -> only deactivate stop_suggesting prefs (BOTH auto and
                      user-confirmed) for this item
        "blocks"   -> only clear the user's product/brand blocks (wrong_item +
                      block_brand) so a flagged product/brand can be suggested
                      again
        "all"      -> all of the above (default; the panel's "Reset" affordance)

    Returns a dict describing what changed:
        {"cadence_reset": bool, "stops_deactivated": int, "blocks_cleared": int}

    Used by the WebSocket reset_preference action. Unlike E6 (which only
    touches auto:% stops because it fires on every approve), this is an
    explicit user-driven undo, so deactivating a user-confirmed stop is
    intentional and correct.
    """
    cadence_reset = False
    stops_deactivated = 0
    blocks_cleared = 0
    try:
        if kind in ("cadence", "all"):
            cur = conn.execute(
                """UPDATE restock_rules
                   SET cadence_adjustment = 1.0,
                       updated_at = datetime('now')
                   WHERE item_id = ? AND cadence_adjustment > 1.0""",
                (item_id,),
            )
            cadence_reset = cur.rowcount > 0
        if kind in ("stop", "all"):
            cur = conn.execute(
                """UPDATE user_item_prefs
                   SET is_active = 0
                   WHERE item_id = ?
                     AND pref_type = 'stop_suggesting'
                     AND is_active = 1""",
                (item_id,),
            )
            stops_deactivated = cur.rowcount
        if kind in ("blocks", "all"):
            # Undo "wrong item" / "never brand" so the flagged product/brand can
            # come back. Clears BOTH the user_item_prefs the validator reads AND
            # the learned 'blocked product' state the curator (chat add) reads,
            # so the undo is consistent across surfaces.
            cur = conn.execute(
                """UPDATE user_item_prefs
                   SET is_active = 0
                   WHERE item_id = ?
                     AND pref_type IN ('wrong_item', 'block_brand')
                     AND is_active = 1""",
                (item_id,),
            )
            blocks_cleared = cur.rowcount
            try:
                conn.execute(
                    "DELETE FROM preference_state WHERE item_id = ? "
                    "AND dimension = 'product' AND key = 'blocked'",
                    (item_id,),
                )
            except Exception:
                pass
        conn.commit()
    except Exception:
        pass
    return {
        "cadence_reset": cadence_reset,
        "stops_deactivated": stops_deactivated,
        "blocks_cleared": blocks_cleared,
    }


def _send_sync(coro):
    """Run an async coroutine to completion from sync code.

    Tries asyncio.run() first; if we're already inside a running loop
    (e.g. called from a FastAPI request), uses a fresh thread + new
    event loop so we don't conflict.
    """
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # "asyncio.run() cannot be called from a running event loop"
        import threading
        result = {"value": None, "error": None}

        def _runner():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result["value"] = loop.run_until_complete(coro)
                finally:
                    loop.close()
            except Exception as e:
                result["error"] = e

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=30)
        if result["error"]:
            raise result["error"]
        return result["value"]


def run(conn=None, days: int = DEFAULT_WINDOW_DAYS, dry_run: bool = False,
        send_asks: bool = True, wa_client=None):
    """Full pipeline: detect patterns + apply consequences + send Tier-2 asks.

    Args:
        conn: optional SQLite connection (otherwise opens DB_PATH).
        days: lookback window for skip_events (default 30).
        dry_run: if True, no writes happen and no WhatsApp messages send.
        send_asks: if True (default), also dispatch Tier-2 asks for
            firmer-tier patterns flagged tier2_ask_pending. The diag
            endpoint can pass send_asks=False to inspect detection
            without firing messages even on a live run.
        wa_client: optional WhatsAppClient. If None and send_asks=True,
            we import the module-level wa_client lazily so this module
            doesn't require WhatsApp at import time.

    Returns:
        {
          "now": ISO timestamp,
          "window_days": int,
          "dry_run": bool,
          "patterns_detected": [...],
          "applied": [...],
          "tier2_asks": [...]   # only present when send_asks=True
          "summary": {soft: N, firmer: N, auto_stop: N,
                      asks_sent: N, asks_skipped: N},
        }
    """
    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own_conn = True

    try:
        patterns = detect_drop_patterns(conn, days=days)
        applied = apply_pattern_consequences(conn, patterns, dry_run=dry_run)

        summary = {"soft": 0, "firmer": 0, "auto_stop": 0}
        for a in applied:
            tier = a.get("tier")
            if tier in summary:
                summary[tier] += 1

        tier2_asks = None
        if send_asks:
            if wa_client is None and not dry_run:
                try:
                    from whatsapp_client import wa_client as _wa_client
                    wa_client = _wa_client
                except Exception:
                    wa_client = None
            tier2_asks = send_tier2_asks(
                conn, patterns, wa_client=wa_client, dry_run=dry_run,
            )
            summary["asks_sent"] = sum(1 for a in tier2_asks if a.get("sent"))
            summary["asks_skipped"] = sum(1 for a in tier2_asks if not a.get("sent"))

        result = {
            "now": datetime.now().isoformat(timespec="seconds"),
            "window_days": days,
            "dry_run": dry_run,
            "patterns_detected": patterns,
            "applied": applied,
            "summary": summary,
        }
        if tier2_asks is not None:
            result["tier2_asks"] = tier2_asks
        return result
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    result = run(dry_run=dry)
    print(json.dumps(result, indent=2, default=str))
