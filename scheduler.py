"""Background scheduler tasks.

The nudge scheduler, order-status poller, nightly catalog scraper and
feedback-timeout checker were extracted from app.py so that file no
longer carries ~330 lines of unrelated background-loop code.

These tasks reach shared runtime state (live WebSocket connections, the
browser lock) through app_state. The two helpers they still need from
app.py — _send_feedback_status and _build_order_tracking — are imported
lazily inside the task functions: app.py imports this module at startup,
so importing app.py at this module's top level would be a circular import.
"""
import asyncio
from datetime import datetime, timedelta

import app_state
from app_state import get_db, broadcast
from app_logging import get_logger, log_event
from config import WHATSAPP_ENABLED
from feedback_manager import feedback_manager

logger = get_logger("scheduler")


# Strong refs to in-flight post-cart check tasks. asyncio only holds weak
# refs to tasks, so a fire-and-forget create_task() can be GC'd mid-await
# (see the 2026-05-25 deploy-thread decision). Keeping them in a set until
# done prevents that.
_post_cart_tasks = set()


def schedule_post_cart_checks(delays_minutes=(15, 30, 60)):
    """After a cart is built (the 'prepare cart' action), run extra order
    status checks at the given minute offsets so a freshly-placed order
    (its real ref, status and line items) is picked up fast instead of
    waiting for the hourly poller. Each run respects the same browser-lock
    guards as the poller, and skips if a build/poll is already using the
    browser.

    Safe to call from any running-loop async context (e.g. build_carts).
    No-op if there's no running event loop.
    """
    from order_scraper import check_and_scrape_orders

    async def _delayed(mins):
        try:
            await asyncio.sleep(mins * 60)
            if app_state.cart_build_active or app_state.order_poll_active:
                log_event("post_cart_check_skip", source="scheduler",
                          detail=f"+{mins}m: browser busy, skipping")
                return
            app_state.order_poll_active = True
            try:
                result = await check_and_scrape_orders()
                log_event(
                    "post_cart_check_done", source="scheduler",
                    detail=(f"+{mins}m: new={len(result.get('new_orders', []))} "
                            f"changes={len(result.get('status_changes', []))}"),
                )
            finally:
                app_state.order_poll_active = False
        except Exception as e:
            log_event("post_cart_check_error", source="scheduler",
                      detail=f"+{mins}m: {str(e)[:150]}", level="warning")

    try:
        for m in delays_minutes:
            t = asyncio.create_task(_delayed(m))
            _post_cart_tasks.add(t)
            t.add_done_callback(_post_cart_tasks.discard)
        log_event("post_cart_checks_scheduled", source="scheduler",
                  detail=f"offsets_min={list(delays_minutes)}")
    except RuntimeError as e:
        # No running loop (e.g. called from a sync/CLI context) -- skip.
        logger.warning("schedule_post_cart_checks: no running loop (%s)", e)


# ============================================================
# NUDGE SCHEDULER (background task)
# ============================================================

async def _nudge_scheduler():
    """
    Background task that checks if a nudge should be sent.

    Runs every 30 minutes. Sends check-in nudges on Mon/Thu evenings (7 PM+)
    and post-order check nudges on Wed/Sat mornings.
    """
    from agent_brain import should_checkin_today, should_check_orders_today

    while True:
        try:
            await asyncio.sleep(1800)  # Check every 30 minutes
            now = datetime.now()
            hour = now.hour

            with get_db(row_factory=True) as conn:
                # Don't send duplicate nudges -- check if we already sent one today
                today_str = now.strftime("%Y-%m-%d")
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM nudge_queue WHERE created_at >= ?",
                    (today_str,))
                already_sent_today = cur.fetchone()[0] > 0

                if not already_sent_today:
                    nudge_text = None

                    # Check-in reminder: Mon/Thu, 7 PM onward
                    if should_checkin_today() and hour >= 19:
                        nudge_text = (
                            "Your order is coming up tomorrow! "
                            "Review the list when you can -- I'll wait for your "
                            "approval before preparing any carts."
                        )

                    # Post-order check: Wed/Sat, 9 AM onward
                    elif should_check_orders_today() and hour >= 9:
                        nudge_text = (
                            "Did you place yesterday's order? "
                            "Let me know so I can update my predictions."
                        )

                    if nudge_text:
                        # Try to deliver immediately if WebSocket connected
                        await broadcast({
                            "type": "agent",
                            "text": nudge_text,
                            "nudge": True,
                            "priority": "normal",
                            "time": now.strftime("%H:%M"),
                        })

                        # Always queue it (delivery will be tracked separately)
                        conn.execute(
                            "INSERT INTO nudge_queue (text, priority) VALUES (?, 'normal')",
                            (nudge_text,))
                        log_event("nudge_sent", source="scheduler",
                                  detail=nudge_text[:100])

        except Exception as e:
            logger.error(f"Nudge scheduler error: {e}")
            await asyncio.sleep(60)  # Retry in 1 min on error


# ============================================================
# ORDER STATUS POLLER (background task)
# ============================================================

# Adaptive poll cadence: check often while an order is actually in flight, back
# off when nothing is live (and overnight, when grocery deliveries don't happen).
# This catches "out for delivery" / "delivered" within minutes for BOTH agent-
# placed and manually-placed orders, instead of the old fixed hourly poll that
# could lag a status change by up to an hour.
POLL_URGENT_S = 10 * 60     # an order is out for delivery -> watch closely
POLL_ACTIVE_S = 22 * 60     # an order is placed but not yet dispatched
POLL_IDLE_S = 60 * 60       # nothing in flight -> hourly (also new-order discovery)
POLL_OVERNIGHT_S = 60 * 60  # 22:00-07:00 -> hourly; no deliveries land overnight

# Order statuses, by how closely they warrant watching. Terminal/placeholder
# statuses ('delivered','cancelled','none','expected') don't pull the cadence
# tighter -- 'expected' is an anticipated order discovered fine at the idle rate.
_URGENT_STATUSES = {"dispatched", "out_for_delivery", "on_the_way", "arriving", "shipped"}
_ACTIVE_STATUSES = {"confirmed", "placed", "processing", "preparing", "accepted"}


def _poll_interval_for(open_statuses, hour: int) -> int:
    """Pure cadence rule: given the set of OPEN (non-terminal) order statuses and
    the local hour, return the next poll interval in seconds. Testable."""
    if hour < 7 or hour >= 22:
        return POLL_OVERNIGHT_S
    s = {str(x).lower() for x in open_statuses}
    if s & _URGENT_STATUSES:
        return POLL_URGENT_S
    if s & _ACTIVE_STATUSES:
        return POLL_ACTIVE_S
    return POLL_IDLE_S


def _order_poll_interval_s() -> int:
    """Next order-poll interval, from the most-in-flight open order."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT LOWER(COALESCE(status,'')) FROM orders "
                "WHERE LOWER(COALESCE(status,'')) NOT IN "
                "('delivered','cancelled','dismissed','none','expected','')"
            ).fetchall()
        statuses = {r[0] for r in rows}
    except Exception:
        statuses = {"confirmed"}  # fail toward more-frequent, never less
    return _poll_interval_for(statuses, datetime.now().hour)


async def _order_status_poller():
    """
    Background task that scrapes store websites for NEW orders and
    checks status of pending orders.

    Two modes:
    - New order discovery: runs every 6 hours (3x/day during waking hours)
      Always runs — this is how we detect orders the user placed manually.
    - Status tracking: runs every 2 hours when there are pending orders.

    When a status change is detected, pushes order_tracking update to frontend.
    When delivery is detected, sends a nudge and triggers the rating flow.
    """
    from order_scraper import has_pending_orders, check_and_scrape_orders
    from order_scraper import sync_restock_dates_from_deliveries
    from enrich_order_history import enrich_order_items
    from app import _build_order_tracking

    _last_full_scrape = 0  # epoch timestamp of last new-order scrape
    first_run = True

    while True:
        try:
            if first_run:
                await asyncio.sleep(30)  # Brief delay on startup for app to settle
                first_run = False
            else:
                # Adaptive: ~10 min while an order is out for delivery, ~22 min
                # while one is placed, hourly when idle / overnight.
                await asyncio.sleep(_order_poll_interval_s())
            now = datetime.now()
            hour = now.hour

            # Only run during waking hours (7am - 10pm)
            if hour < 7 or hour >= 22:
                continue

            import time

            # Skip if cart builder is using the browser — order_scraper would
            # kill it via _kill_stale_chromium() in launch_browser()
            if app_state.cart_build_active:
                log_event("order_poller_skip", source="scheduler",
                          detail="Cart build in progress, skipping to avoid browser conflict")
                continue

            has_pending = has_pending_orders()
            hours_since_scrape = (time.time() - _last_full_scrape) / 3600

            # Decide what to run:
            # - Full scrape (new orders + status): hourly OR if pending orders
            # - Skip: if no pending and scraped within the last hour
            if not has_pending and hours_since_scrape < 1:
                log_event("order_poller_skip", source="scheduler",
                          detail=f"No pending orders, last scrape {hours_since_scrape:.1f}h ago")
                continue

            log_event("order_poller_start", source="scheduler",
                      detail=f"pending={has_pending}, hours_since_scrape={hours_since_scrape:.1f}, "
                             f"next_cadence={_order_poll_interval_s()//60}min")

            try:
                result = await check_and_scrape_orders()
                _last_full_scrape = time.time()  # mark successful scrape
                status_changes = result.get("status_changes", [])
                new_orders = result.get("new_orders", [])

                log_event("order_poller_done", source="scheduler",
                          detail=f"changes={len(status_changes)}, new={len(new_orders)}")

                # Push updated tracking data to connected frontend
                if status_changes or new_orders:
                    tracking = _build_order_tracking()
                    await broadcast({
                        "type": "order_tracking",
                        **tracking,
                    })

                # Check if any order just got delivered
                deliveries_detected = False
                for change in status_changes:
                    if change.get("new") == "delivered":
                        deliveries_detected = True
                        order_ref = change.get("order_ref", "")
                        nudge_text = f"Your order just arrived! 🎉 Rate it to help me learn your preferences."
                        await broadcast({
                            "type": "agent",
                            "text": nudge_text,
                            "nudge": True,
                            "priority": "normal",
                            "time": now.strftime("%H:%M"),
                        })
                        log_event("delivery_detected", source="scheduler",
                                  detail=f"Order {change.get('order_id')} delivered")

                # DOWNSTREAM: When delivery detected, update preferences & consumption
                if deliveries_detected:
                    try:
                        # 1. Enrich order items (link to brands, catalog, items table)
                        with get_db(row_factory=True) as enrich_conn:
                            enrich_stats = enrich_order_items(enrich_conn)
                        log_event("delivery_enrich", source="scheduler",
                                  detail=f"Enriched: {enrich_stats}")

                        # 2. Sync restock dates from delivery dates
                        sync_restock_dates_from_deliveries()
                        log_event("delivery_restock_sync", source="scheduler",
                                  detail="Restock dates synced from delivery")
                    except Exception as e:
                        logger.error(f"Downstream delivery processing failed: {e}",
                                     exc_info=True)
                        log_event("delivery_downstream_error", source="scheduler",
                                  detail=str(e)[:200], level="error")

            except Exception as e:
                logger.error(f"Order status check failed: {e}", exc_info=True)
                log_event("order_poller_error", source="scheduler",
                          detail=str(e)[:200], level="error")

        except Exception as e:
            logger.error(f"Order poller error: {e}")
            await asyncio.sleep(300)  # Retry in 5 min on error


async def _feedback_timeout_checker():
    """
    Background task that checks for expired feedback rounds
    and sends follow-up messages. Runs every 5 minutes.
    """
    from app import _send_feedback_status
    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            await feedback_manager.check_timeouts()
            # Broadcast updated status if someone is connected
            if app_state.active_agent:
                await _send_feedback_status(app_state.active_agent)
        except Exception as e:
            logger.error("Feedback timeout check failed: %s", e)


# ============================================================
# NIGHTLY CATALOG SCRAPER (background task)
# ============================================================

async def _nightly_catalog_scraper():
    """
    Background task that runs full catalog scrapes every night at 4:00 AM.

    Sequence: Kibsons first (~5 min), then Carrefour (~15 min).
    Both use the browser, so we set cart_build_active to prevent
    the order poller from conflicting.
    """
    while True:
        try:
            # Calculate seconds until next 4:00 AM
            now = datetime.now()
            target = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if now >= target:
                # Already past 4 AM today — schedule for tomorrow
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()

            logger.info(
                "Nightly scraper: next run at %s (in %.1f hours)",
                target.strftime("%Y-%m-%d %H:%M"), wait_seconds / 3600,
            )
            await asyncio.sleep(wait_seconds)

            # Acquire browser lock
            if app_state.cart_build_active:
                logger.warning("Nightly scraper: browser busy (cart build), retrying in 30 min")
                await asyncio.sleep(1800)
                continue

            app_state.cart_build_active = True
            log_event("nightly_scrape_start", source="scheduler")

            try:
                # Kibsons first (smaller, ~5 min)
                logger.info("Nightly scraper: starting Kibsons...")
                from scrape_kibsons_rsc import scrape_inventory as scrape_kibsons
                kibsons_stats = await scrape_kibsons()
                logger.info(
                    "Nightly scraper: Kibsons done — %d products (%d new, %d updated)",
                    kibsons_stats.get("total", 0),
                    kibsons_stats.get("new", 0),
                    kibsons_stats.get("updated", 0),
                )
                log_event("nightly_scrape_kibsons", source="scheduler",
                          detail=f"total={kibsons_stats.get('total', 0)}, "
                                 f"new={kibsons_stats.get('new', 0)}, "
                                 f"updated={kibsons_stats.get('updated', 0)}, "
                                 f"duration={kibsons_stats.get('duration_sec', 0):.0f}s")

                # Brief pause between scrapers
                await asyncio.sleep(10)

                # Carrefour (larger, ~15 min)
                logger.info("Nightly scraper: starting Carrefour...")
                from scrape_carrefour_rsc import scrape_inventory as scrape_carrefour
                carrefour_stats = await scrape_carrefour()
                logger.info(
                    "Nightly scraper: Carrefour done — %d products (%d new, %d updated)",
                    carrefour_stats.get("total", 0),
                    carrefour_stats.get("new", 0),
                    carrefour_stats.get("updated", 0),
                )
                log_event("nightly_scrape_carrefour", source="scheduler",
                          detail=f"total={carrefour_stats.get('total', 0)}, "
                                 f"new={carrefour_stats.get('new', 0)}, "
                                 f"updated={carrefour_stats.get('updated', 0)}, "
                                 f"duration={carrefour_stats.get('duration_sec', 0):.0f}s")

                log_event("nightly_scrape_done", source="scheduler",
                          detail=f"kibsons={kibsons_stats.get('total', 0)}, "
                                 f"carrefour={carrefour_stats.get('total', 0)}")

            except Exception as e:
                logger.error("Nightly scraper failed: %s", e, exc_info=True)
                log_event("nightly_scrape_error", source="scheduler",
                          detail=str(e)[:200], level="error")
            finally:
                app_state.cart_build_active = False

        except Exception as e:
            logger.error("Nightly scraper scheduler error: %s", e)
            await asyncio.sleep(3600)  # Retry in 1 hour on unexpected errors


# ============================================================
# DAILY RESCORE BACKSTOP (background task)
# ============================================================

RESCORE_BACKSTOP_HOUR = 5
RESCORE_BACKSTOP_MIN = 45


async def _daily_rescore_backstop():
    """Keep the matcher index (item_alternatives) from silently rotting.

    The catalog scrapers already re-index after a successful run, but if the
    nightly scrape errors the index never refreshes — it once sat stale for
    ~3 months, which is what made "add cucumber" return eggplant. Once a day, a
    bit after the 4 AM scrape, check the index: if nothing has been re-scored in
    the last ~26 hours (i.e. a scrape didn't already refresh it), re-score every
    store against the current catalog. Pure SQL + scoring, no browser; cheap and
    idempotent. Skips itself when a scrape already kept the index fresh today.
    """
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=RESCORE_BACKSTOP_HOUR,
                                 minute=RESCORE_BACKSTOP_MIN, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            # Already fresh? (a scrape re-scored it in the last day.) Uses
            # SQLite's UTC clock to match last_scored_at, which is also UTC.
            with app_state.get_db(row_factory=True) as conn:
                fresh = bool(conn.execute(
                    "SELECT 1 FROM item_alternatives "
                    "WHERE last_scored_at >= datetime('now','-26 hours') LIMIT 1"
                ).fetchone())
                newest = conn.execute(
                    "SELECT MAX(last_scored_at) AS m FROM item_alternatives"
                ).fetchone()["m"]
            if fresh:
                logger.info("Rescore backstop: index fresh (newest=%s) — skipping", newest)
                continue
            if app_state.cart_build_active:
                logger.warning("Rescore backstop: browser busy — skipping today")
                continue

            logger.info("Rescore backstop: index stale (newest=%s) — rescoring all stores", newest)
            log_event("rescore_backstop_start", source="scheduler", detail=f"newest={newest}")
            from rescore_alternatives import rescore_store
            total = 0
            with app_state.get_db(row_factory=True) as conn:
                stores = conn.execute(
                    "SELECT id FROM stores WHERE is_active = 1").fetchall()
                for s in stores:
                    try:
                        rr = rescore_store(s["id"], conn=conn, verbose=False)
                        total += rr.get("alternatives_scored", 0)
                    except Exception as e:
                        logger.error("Rescore backstop: store %s failed: %s", s["id"], e)
            log_event("rescore_backstop_done", source="scheduler",
                      detail=f"alternatives={total}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Rescore backstop error: %s", e, exc_info=True)
            await asyncio.sleep(3600)


# ============================================================
# NIGHTLY PATTERN DETECTOR (background task)
# ============================================================

async def _nightly_pattern_detector():
    """Background task that runs the Tier-1/Tier-2 drop-pattern detector
    every night at 5:00 AM.

    5 AM is intentionally after the 4 AM catalog scraper (Kibsons + Carrefour
    finish by ~4:30 AM), so the catalog is fresh and we don't fight for
    the browser. The detector itself is pure SQL — no browser, no network —
    and is idempotent, so even if it runs multiple times in a day nothing
    bad happens (the apply function only escalates, never reduces).

    See pattern_detector.py for the tier rules. Summary:
        2 drops in 30d -> cadence_adjustment = 1.25 (soft nudge)
        3-4 drops      -> cadence_adjustment = 1.50 + queue Tier-2 ask
        5+ drops       -> auto stop_suggesting (revertable)
    """
    while True:
        try:
            # Calculate seconds until next 5:00 AM
            now = datetime.now()
            target = now.replace(hour=5, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()

            logger.info(
                "Pattern detector: next run at %s (in %.1f hours)",
                target.strftime("%Y-%m-%d %H:%M"), wait_seconds / 3600,
            )
            await asyncio.sleep(wait_seconds)

            log_event("pattern_detector_start", source="scheduler")
            try:
                from pattern_detector import run as run_pattern_detector
                result = run_pattern_detector(dry_run=False)
                summary = result.get("summary", {})
                log_event(
                    "pattern_detector_done",
                    source="scheduler",
                    detail=(
                        f"soft={summary.get('soft', 0)} "
                        f"firmer={summary.get('firmer', 0)} "
                        f"auto_stop={summary.get('auto_stop', 0)} "
                        f"detected={len(result.get('patterns_detected', []))}"
                    ),
                )
            except Exception as e:
                logger.error("Pattern detector failed: %s", e, exc_info=True)
                log_event("pattern_detector_error", source="scheduler",
                          detail=str(e)[:200], level="error")

        except Exception as e:
            logger.error("Pattern detector scheduler error: %s", e)
            await asyncio.sleep(3600)  # Retry in 1 hour on unexpected errors


# ============================================================
# WHATSAPP RESILIENCE (background tasks)
# ============================================================
#
# Two layers, both target the whatsapp-service sidecar (Node.js,
# under NSSM as hearthkeeper-whatsapp):
#
#   Layer 1 -- preemptive nightly restart at 3:30 AM. whatsapp-web.js
#   has slow memory leaks and session-staleness bugs that build up
#   over days; a clean restart every 24h heads most of them off.
#
#   Layer 2 -- liveness probe every 2 minutes. If is_connected()
#   returns false N times in a row, request_restart() and reset the
#   counter. Cooldown enforced so we never restart more than once
#   per RESTART_COOLDOWN_S even if the probe keeps failing.
#
# Both rely on the sidecar's /restart endpoint (auth via
# WEBHOOK_SECRET). NSSM AppExit=Restart brings the process back up.

# Tunables. Adjust by feel after observing real-world stability.
PROBE_INTERVAL_S = 120              # check every 2 min
PROBE_FAIL_THRESHOLD = 3            # 3 consecutive fails -> restart
RESTART_COOLDOWN_S = 600            # never restart more than every 10 min
# Circuit breaker: if this many restarts in a row don't bring the sidecar back,
# auto-restart can't fix it (logged out / needs QR re-link). Stop thrashing,
# trip the breaker, and surface it -- a successful probe re-arms it.
MAX_RESTART_ATTEMPTS = 4
CIRCUIT_REARM_S = 6 * 3600          # re-try auto-restart at most every 6h while open


async def _nightly_whatsapp_restart():
    """Preemptive restart of the WhatsApp sidecar every night at 3:30 AM.

    3:30 sits 30 min before the catalog scrape (4:00) so a slow
    sidecar reconnect doesn't push catalog work into morning
    waking hours. WhatsApp-web.js usually reconnects within
    10-15 s of NSSM bringing the process back up; the window is
    generous.

    Lives behind WHATSAPP_ENABLED so test environments without the
    sidecar never see this fire.
    """
    if not WHATSAPP_ENABLED:
        return
    from whatsapp_client import wa_client
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=3, minute=30, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_s = (target - now).total_seconds()
            logger.info(
                "WhatsApp nightly restart: next at %s (in %.1f hours)",
                target.strftime("%Y-%m-%d %H:%M"), wait_s / 3600,
            )
            await asyncio.sleep(wait_s)

            log_event("wa_nightly_restart_start", source="scheduler",
                      detail="preemptive nightly restart")
            ok = await wa_client.request_restart(reason="nightly")
            log_event(
                "wa_nightly_restart_done",
                source="scheduler",
                detail=f"ack={ok}",
                level="info" if ok else "warning",
            )
        except Exception as e:
            logger.error("WhatsApp nightly restart error: %s", e)
            await asyncio.sleep(3600)


async def _whatsapp_liveness_probe():
    """Periodic is_connected() probe with auto-restart after N failures.

    The sidecar can be running yet not connected (auth lost,
    Puppeteer crash, WhatsApp Web session ejected). is_connected()
    returns false in all those cases. After PROBE_FAIL_THRESHOLD
    consecutive failures we ask the sidecar to exit so NSSM picks
    it back up; the LocalAuth dir persists the WhatsApp session
    across restarts, so a healthy startup is typically silent
    (no QR scan needed).

    Cooldown: we never trigger more than one restart per
    RESTART_COOLDOWN_S window even if probes keep failing. That
    avoids a restart loop if the underlying issue (e.g. network
    outage) isn't fixed by bouncing the process.
    """
    if not WHATSAPP_ENABLED:
        return
    from whatsapp_client import wa_client
    import time as _time
    consecutive_failures = 0
    last_restart_at = 0
    restart_attempts = 0     # consecutive restarts that haven't restored the link
    circuit_open = False     # tripped when auto-restart clearly can't fix it
    while True:
        try:
            await asyncio.sleep(PROBE_INTERVAL_S)
            connected = await wa_client.is_connected()
            if connected:
                if consecutive_failures > 0 or circuit_open:
                    log_event(
                        "wa_liveness_recovered",
                        source="scheduler",
                        detail=(f"after {consecutive_failures} failed probes"
                                + (" (circuit re-armed)" if circuit_open else "")),
                    )
                consecutive_failures = 0
                restart_attempts = 0
                circuit_open = False
                continue
            consecutive_failures += 1
            logger.warning(
                "WhatsApp liveness probe: not connected "
                "(%d/%d before restart)",
                consecutive_failures, PROBE_FAIL_THRESHOLD,
            )
            if consecutive_failures < PROBE_FAIL_THRESHOLD:
                continue
            # Threshold hit -- maybe restart. Once the breaker is OPEN, auto-
            # restart has clearly failed to fix it (logged out / needs a QR re-
            # link), so we stop thrashing and only re-try every CIRCUIT_REARM_S.
            now = _time.time()
            min_gap = CIRCUIT_REARM_S if circuit_open else RESTART_COOLDOWN_S
            if now - last_restart_at < min_gap:
                logger.warning(
                    "WhatsApp liveness probe: restart skipped (%s %.0fs)",
                    "breaker open" if circuit_open else "cooldown",
                    min_gap - (now - last_restart_at),
                )
                continue
            restart_attempts += 1
            log_event(
                "wa_liveness_restart",
                source="scheduler",
                detail=f"{consecutive_failures} consecutive failures "
                       f"(restart attempt {restart_attempts})",
                level="warning",
            )
            ok = await wa_client.request_restart(reason="liveness")
            last_restart_at = now
            if ok:
                # Give the sidecar time to come back before sampling again.
                # Reset the counter so we don't re-trigger immediately.
                consecutive_failures = 0
                await asyncio.sleep(30)
            else:
                log_event(
                    "wa_liveness_restart_failed",
                    source="scheduler",
                    detail="sidecar did not ack /restart",
                    level="error",
                )
            # Trip the breaker once repeated restarts clearly aren't recovering
            # the link. Stop the 25-restarts-a-day thrash and surface the stuck
            # state loudly (the in-app health panel reads this).
            if restart_attempts >= MAX_RESTART_ATTEMPTS and not circuit_open:
                circuit_open = True
                log_event(
                    "wa_circuit_open",
                    source="scheduler",
                    detail=(f"auto-restart not recovering after {restart_attempts} "
                            f"attempts -- needs a manual re-link "
                            f"(WhatsApp > Linked Devices > scan the QR on the Ally)"),
                    level="error",
                )
        except Exception as e:
            logger.error("WhatsApp liveness probe error: %s", e)
            await asyncio.sleep(60)


async def _nightly_carrefour_order_detail_backfill():
    """Backfill Carrefour order_items for any delivered orders that
    landed with items=[] (the detail-scrape JS failed because of DOM
    drift, the detail page redirected, etc.).

    Runs at 5:30 AM:
      4:00  catalog scrape (Kibsons + Carrefour, ~25 min)
      5:00  pattern detector (~1 s)
      5:30  this -- has the browser to itself
      7:00+ user-facing activity starts

    Idempotent: backfill_carrefour_order_items only touches orders
    with 0 order_items rows. So even multiple runs in a day are
    safe; we just usually only fire it once a night.

    The actual scrape is sync (selenium + UC Chrome under the hood)
    so we run it via asyncio.to_thread to keep the event loop free.
    """
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=5, minute=30, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_s = (target - now).total_seconds()
            logger.info(
                "Carrefour detail backfill: next at %s (in %.1f hours)",
                target.strftime("%Y-%m-%d %H:%M"), wait_s / 3600,
            )
            await asyncio.sleep(wait_s)

            if app_state.cart_build_active:
                log_event("carrefour_backfill_skip", source="scheduler",
                          detail="browser busy (cart build) -- retrying in 30 min",
                          level="warning")
                await asyncio.sleep(1800)
                continue

            log_event("carrefour_backfill_start", source="scheduler")
            try:
                from order_scraper import backfill_carrefour_order_items
                # Sync function -- offload so we don't block the loop.
                result = await asyncio.to_thread(
                    backfill_carrefour_order_items,
                )
                log_event(
                    "carrefour_backfill_done",
                    source="scheduler",
                    detail=(
                        f"orders_processed={result.get('orders_processed', 0)} "
                        f"items_inserted={result.get('items_inserted', 0)} "
                        f"errors={len(result.get('errors', []))}"
                    ),
                )
                if result.get("errors"):
                    log_event(
                        "carrefour_backfill_errors",
                        source="scheduler",
                        detail="; ".join(result["errors"])[:300],
                        level="warning",
                    )
            except Exception as e:
                logger.error("Carrefour backfill failed: %s", e, exc_info=True)
                log_event("carrefour_backfill_error", source="scheduler",
                          detail=str(e)[:200], level="error")
        except Exception as e:
            logger.error("Carrefour backfill scheduler error: %s", e)
            await asyncio.sleep(3600)


# ============================================================
# B5 — DELIVERED-ORDER RATING PROMPT DISPATCHER
# ============================================================
#
# Once an order's status flips to 'delivered' (or 'completed'), send
# the curator a short WhatsApp asking how it went. Drives the
# feedback loop that the rating engine needs.
#
# Decoupled from the scraper (the scraper just updates orders.status
# + order_status_log). This task sweeps every 10 minutes for orders
# that need a prompt and haven't gotten one yet.
#
# Guards:
#   - skip during quiet hours (22:00-07:00 UAE local)
#   - skip orders older than 24h (we missed our window; user has
#     likely moved on)
#   - skip orders with 0 items (Carrefour scraper gap -- nothing
#     to anchor feedback on)
#   - skip if WHATSAPP_ENABLED=0 (test envs)
#   - one prompt per order, gated by orders.rating_status='pending'
#     once we send

DELIVERED_PROMPT_INTERVAL_S = 600    # 10 min sweep
DELIVERED_PROMPT_MAX_AGE_HOURS = 24  # don't ask about old deliveries
DELIVERED_PROMPT_MIN_AGE_MIN = 2     # give the system a beat after status flip


async def _delivered_rating_dispatcher():
    """Send delivered-order rating prompts on WhatsApp.

    Sweeps every DELIVERED_PROMPT_INTERVAL_S (default 10 min) and
    picks up any order where:
      * status IN ('delivered', 'completed')
      * rating_status = 'none'
      * delivery_date BETWEEN -24h AND -2min ago
      * EXISTS at least one order_items row

    Sends one WhatsApp message per qualifying order to the household
    driver (curator). Marks orders.rating_status='pending' + sets the
    member's member_pending_intent so the reply handler routes
    correctly.
    """
    if not WHATSAPP_ENABLED:
        logger.info("delivered_rating_dispatcher disabled (WHATSAPP_ENABLED=0)")
        return

    from whatsapp_client import wa_client
    from whatsapp_agent import format_delivered_rating_prompt
    from notification_prefs import should_notify, is_quiet_hours

    while True:
        try:
            await asyncio.sleep(DELIVERED_PROMPT_INTERVAL_S)
            now = datetime.now()

            with get_db(row_factory=True) as conn:
                # Bounds: window starts max_age hours ago, ends
                # min_age minutes ago (don't fire instantly on the
                # status update -- it can flicker).
                max_age_iso = (
                    now - timedelta(hours=DELIVERED_PROMPT_MAX_AGE_HOURS)
                ).strftime("%Y-%m-%d %H:%M:%S")
                min_age_iso = (
                    now - timedelta(minutes=DELIVERED_PROMPT_MIN_AGE_MIN)
                ).strftime("%Y-%m-%d %H:%M:%S")

                orders = conn.execute(
                    """
                    SELECT o.id, o.order_ref, o.delivery_date,
                           o.total_amount, o.currency,
                           s.name AS store_name,
                           (SELECT COUNT(*) FROM order_items
                              WHERE order_id = o.id) AS item_count
                    FROM orders o
                    JOIN stores s ON o.store_id = s.id
                    WHERE o.status IN ('delivered', 'completed')
                      AND COALESCE(o.rating_status, 'none') = 'none'
                      AND o.delivery_date IS NOT NULL
                      AND o.delivery_date <= ?
                      AND o.delivery_date >= ?
                    ORDER BY o.delivery_date DESC
                    LIMIT 5
                    """,
                    (min_age_iso, max_age_iso),
                ).fetchall()

                if not orders:
                    continue

                # Resolve the curator (driver role). B7's per-member
                # opt-in is honored via should_notify + is_quiet_hours
                # below -- defaults preserve the driver-gets-all,
                # 22-07 quiet behavior.
                driver = conn.execute(
                    """SELECT id, name, phone FROM household_members
                       WHERE role = 'driver' AND COALESCE(phone, '') != ''
                       LIMIT 1"""
                ).fetchone()
                if not driver:
                    logger.warning(
                        "delivered_rating_dispatcher: no driver to message; "
                        "skipping %d order(s)", len(orders),
                    )
                    continue

                if not should_notify(driver["id"], "b5_delivered", conn=conn):
                    logger.debug(
                        "delivered_rating_dispatcher: driver opted out of "
                        "b5_delivered; skipping %d order(s)", len(orders),
                    )
                    continue

                if is_quiet_hours(driver["id"], now=now, conn=conn):
                    logger.debug(
                        "delivered_rating_dispatcher: quiet hours for driver, "
                        "skipping %d order(s)", len(orders),
                    )
                    continue

                # Also skip if the driver already has an open
                # member_pending_intent of any kind -- don't pile up
                # multi-turn state across asks. Retry next sweep.
                existing_pending = conn.execute(
                    "SELECT member_pending_intent FROM household_members WHERE id = ?",
                    (driver["id"],),
                ).fetchone()
                if existing_pending and existing_pending["member_pending_intent"]:
                    logger.debug(
                        "delivered_rating_dispatcher: driver already has pending "
                        "intent, deferring %d order(s)", len(orders),
                    )
                    continue

                # Send for the most recent qualifying order only --
                # if multiple delivered in this window, we'll catch
                # the others on subsequent sweeps after this one is
                # resolved.
                order = orders[0]
                if (order["item_count"] or 0) == 0:
                    # 0-item order: mark dismissed so we never ask
                    # about it (no items to rate). Keep cycling for
                    # the next qualifying order on the next sweep.
                    conn.execute(
                        "UPDATE orders SET rating_status = 'dismissed' WHERE id = ?",
                        (order["id"],),
                    )
                    conn.commit()
                    log_event(
                        "delivered_prompt_skipped_no_items", source="scheduler",
                        detail=f"order={order['id']} store={order['store_name']}",
                    )
                    continue

                msg = format_delivered_rating_prompt(dict(order))

                # Mark BEFORE the send so a transient send failure
                # doesn't cause repeated re-sends. The actual send
                # error is logged but the rating_status='pending'
                # row stands; manual recovery via diag if needed.
                conn.execute(
                    "UPDATE orders SET rating_status = 'pending' WHERE id = ?",
                    (order["id"],),
                )

                import json as _json
                pending_payload = _json.dumps({
                    "type": "rating_pending",
                    "order_id": order["id"],
                    "order_ref": order["order_ref"],
                    "store_name": order["store_name"],
                    "item_count": order["item_count"],
                    "asked_at": now.isoformat(timespec="seconds"),
                })
                conn.execute(
                    "UPDATE household_members SET member_pending_intent = ? WHERE id = ?",
                    (pending_payload, driver["id"]),
                )
                conn.commit()

            # Send outside the DB context manager (network call).
            try:
                await wa_client.send_message(driver["phone"], msg)
            except Exception as e:
                logger.error(
                    "delivered_rating_dispatcher: send failed for order=%s: %s",
                    order["id"], e,
                )
                # Leave rating_status='pending' so we don't re-ask;
                # the user can rate via the in-app UI if they want.
                log_event(
                    "delivered_prompt_send_error", source="scheduler",
                    detail=f"order={order['id']} err={str(e)[:160]}",
                    level="warning",
                )
                continue

            log_event(
                "delivered_prompt_sent", source="scheduler",
                detail=(
                    f"order={order['id']} store={order['store_name']} "
                    f"items={order['item_count']} to_member={driver['name']}"
                ),
            )
        except Exception as e:
            logger.error("delivered_rating_dispatcher error: %s", e, exc_info=True)
            log_event(
                "delivered_rating_dispatcher_error", source="scheduler",
                detail=str(e)[:200], level="error",
            )
            await asyncio.sleep(600)  # back off on unexpected errors


# ============================================================
# B4 — TODAY'S-DELIVERIES MORNING HEADS-UP
# ============================================================
#
# Once a day at 07:30 UAE local, bundle every order expected to
# deliver today into a single WhatsApp summary. Replaces what would
# otherwise be 1-3 per-order notifications scattered through the
# morning. Stays silent if there's nothing to surface.
#
# Idempotency: queries app_events for a "b4_today_summary_sent" log
# entry on today's date. If present, skip. This avoids a duplicate
# send if the service restarts shortly after the scheduled run.

DAY_SUMMARY_HOUR = 7
DAY_SUMMARY_MIN = 30


async def _today_deliveries_morning_dispatcher():
    """Send the morning today's-deliveries summary at 07:30 UAE local.

    Sleeps until the next 07:30 mark, then sweeps. Stays idle the
    rest of the day. Bundle-or-silence; never re-fires for the
    same day.
    """
    if not WHATSAPP_ENABLED:
        logger.info(
            "today_deliveries_dispatcher disabled (WHATSAPP_ENABLED=0)"
        )
        return

    from whatsapp_client import wa_client
    from whatsapp_agent import format_today_deliveries
    from notification_prefs import should_notify, is_quiet_hours

    while True:
        try:
            now = datetime.now()
            target = now.replace(
                hour=DAY_SUMMARY_HOUR, minute=DAY_SUMMARY_MIN,
                second=0, microsecond=0,
            )
            if now >= target:
                target += timedelta(days=1)
            wait_s = (target - now).total_seconds()
            logger.info(
                "Today's-deliveries dispatcher: next run at %s (in %.1f h)",
                target.strftime("%Y-%m-%d %H:%M"), wait_s / 3600,
            )
            await asyncio.sleep(wait_s)

            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            with get_db(row_factory=True) as conn:
                # Idempotency: did we already send today?
                already = conn.execute(
                    """SELECT 1 FROM app_events
                       WHERE event = 'b4_today_summary_sent'
                         AND DATE(timestamp) = ? LIMIT 1""",
                    (today,),
                ).fetchone()
                if already:
                    logger.info(
                        "today_deliveries_dispatcher: already sent today, skip"
                    )
                    continue

                # Orders expected today and not yet terminal.
                rows = conn.execute(
                    """
                    SELECT o.id, o.order_ref, o.status, o.expected_by,
                           o.delivery_type,
                           s.name AS store_name
                    FROM orders o
                    JOIN stores s ON o.store_id = s.id
                    WHERE o.expected_by IS NOT NULL
                      AND DATE(o.expected_by) = ?
                      AND o.status NOT IN
                          ('delivered', 'completed', 'cancelled')
                    ORDER BY o.expected_by ASC
                    """,
                    (today,),
                ).fetchall()

                if not rows:
                    logger.info(
                        "today_deliveries_dispatcher: no deliveries today, "
                        "staying silent"
                    )
                    # Still mark as "checked today" so we don't loop
                    # if the service restarts before midnight.
                    log_event(
                        "b4_today_summary_sent", source="scheduler",
                        detail="no_deliveries silent",
                    )
                    continue

                driver = conn.execute(
                    """SELECT id, name, phone FROM household_members
                       WHERE role = 'driver' AND COALESCE(phone, '') != ''
                       LIMIT 1"""
                ).fetchone()
                if not driver:
                    logger.warning(
                        "today_deliveries_dispatcher: no driver phone; "
                        "skipping %d order(s)", len(rows),
                    )
                    continue

                # B7 -- per-member opt-out. b4 fires at a fixed
                # morning time so is_quiet_hours rarely matters here
                # (07:30 is past the default quiet window), but
                # honor it anyway in case the user moves their quiet
                # window forward.
                if not should_notify(driver["id"], "b4_today_deliveries", conn=conn):
                    logger.debug(
                        "today_deliveries_dispatcher: driver opted out, skip"
                    )
                    continue
                if is_quiet_hours(driver["id"], now=now, conn=conn):
                    logger.debug(
                        "today_deliveries_dispatcher: in quiet hours for "
                        "driver, skip"
                    )
                    continue

                orders_payload = [dict(r) for r in rows]
                msg = format_today_deliveries(orders_payload)
                if not msg:
                    continue

            try:
                await wa_client.send_message(driver["phone"], msg)
            except Exception as e:
                logger.error(
                    "today_deliveries_dispatcher: send failed: %s", e,
                )
                log_event(
                    "b4_today_summary_send_error", source="scheduler",
                    detail=str(e)[:160], level="warning",
                )
                continue

            log_event(
                "b4_today_summary_sent", source="scheduler",
                detail=f"orders={len(rows)} to_member={driver['name']}",
            )
        except Exception as e:
            logger.error(
                "today_deliveries_dispatcher error: %s", e, exc_info=True,
            )
            log_event(
                "today_deliveries_dispatcher_error", source="scheduler",
                detail=str(e)[:200], level="error",
            )
            await asyncio.sleep(3600)


# ============================================================
# B6 — 48-HOUR RATING REMINDER DISPATCHER
# ============================================================
#
# If a delivered-order rating prompt (B5) sat unanswered for 48
# hours, send one gentle reminder. Cap: ONE reminder per order,
# never two. Without this we either spam (multiple sweeps re-firing
# the same ask) or never follow up (silent forever).
#
# Schema: orders.rating_reminded_at. NULL = never reminded; set to
# now() once we fire the reminder.

REMINDER_SWEEP_INTERVAL_S = 3600    # hourly is plenty
REMINDER_AGE_HOURS = 48              # ask after 2 days


async def _rating_reminder_dispatcher():
    """Once a day (well, every hour during waking time), find orders
    that asked for a rating 48+ hours ago and never got an answer.
    Send one reminder + stamp rating_reminded_at.

    Member-pending-intent on the driver still indicates the original
    ask is open. We DON'T clear it -- if the user finally replies
    after the reminder, the existing _maybe_handle_rating_reply
    dispatcher still resolves it.
    """
    if not WHATSAPP_ENABLED:
        logger.info("rating_reminder_dispatcher disabled (WHATSAPP_ENABLED=0)")
        return

    from whatsapp_client import wa_client
    from whatsapp_agent import format_rating_reminder
    from notification_prefs import should_notify, is_quiet_hours

    while True:
        try:
            await asyncio.sleep(REMINDER_SWEEP_INTERVAL_S)
            now = datetime.now()

            threshold = (
                now - timedelta(hours=REMINDER_AGE_HOURS)
            ).strftime("%Y-%m-%d %H:%M:%S")

            with get_db(row_factory=True) as conn:
                rows = conn.execute(
                    """
                    SELECT o.id, o.order_ref, o.delivery_date,
                           s.name AS store_name
                    FROM orders o
                    JOIN stores s ON o.store_id = s.id
                    WHERE o.rating_status = 'pending'
                      AND o.rating_reminded_at IS NULL
                      AND o.delivery_date IS NOT NULL
                      AND o.delivery_date <= ?
                    ORDER BY o.delivery_date DESC
                    LIMIT 3
                    """,
                    (threshold,),
                ).fetchall()

                if not rows:
                    continue

                driver = conn.execute(
                    """SELECT id, name, phone FROM household_members
                       WHERE role = 'driver' AND COALESCE(phone, '') != ''
                       LIMIT 1"""
                ).fetchone()
                if not driver:
                    logger.warning(
                        "rating_reminder_dispatcher: no driver phone; "
                        "skipping %d order(s)", len(rows),
                    )
                    continue

                if not should_notify(driver["id"], "b6_reminder", conn=conn):
                    logger.debug(
                        "rating_reminder_dispatcher: driver opted out, skip"
                    )
                    continue
                if is_quiet_hours(driver["id"], now=now, conn=conn):
                    logger.debug(
                        "rating_reminder_dispatcher: quiet hours for driver, skip"
                    )
                    continue

                # Pick the most recent stale ask. Stamp the others
                # at the same time so the next sweep moves forward
                # rather than re-considering them.
                pick = rows[0]
                others = [r["id"] for r in rows[1:]]

                msg = format_rating_reminder(dict(pick))
                conn.execute(
                    "UPDATE orders SET rating_reminded_at = ? WHERE id = ?",
                    (now.strftime("%Y-%m-%d %H:%M:%S"), pick["id"]),
                )
                # Don't actually stamp the others -- they may still
                # be in their own 48h window per their delivery_date.
                # The WHERE clause already filters by delivery_date,
                # so we'll just pick them up on the next sweep.
                _ = others
                conn.commit()

            try:
                await wa_client.send_message(driver["phone"], msg)
            except Exception as e:
                logger.error(
                    "rating_reminder_dispatcher: send failed order=%s: %s",
                    pick["id"], e,
                )
                log_event(
                    "rating_reminder_send_error", source="scheduler",
                    detail=f"order={pick['id']} err={str(e)[:160]}",
                    level="warning",
                )
                continue

            log_event(
                "rating_reminder_sent", source="scheduler",
                detail=(
                    f"order={pick['id']} store={pick['store_name']} "
                    f"to_member={driver['name']}"
                ),
            )
        except Exception as e:
            logger.error("rating_reminder_dispatcher error: %s", e, exc_info=True)
            log_event(
                "rating_reminder_dispatcher_error", source="scheduler",
                detail=str(e)[:200], level="error",
            )
            await asyncio.sleep(1800)


# ============================================================
# B3 — STATUS-UPDATE NOTIFICATION DISPATCHER
# ============================================================
#
# When an order transitions to a "noteworthy" status (out_for_delivery /
# dispatched), the curator should get a one-line WhatsApp heads-up.
# B5 already handles `delivered` (sends the rating prompt instead).
#
# Decoupled from the scraper: scraper writes order_status_log rows,
# this task sweeps for forward-progress transitions that haven't been
# notified yet, sends + stamps notified_at to dedupe.
#
# Guards:
#   - skip flickered / backwards transitions (whatsapp_agent.status_is_forward)
#   - skip non-noteworthy statuses (_STATUS_NOTIFY_SET in whatsapp_agent)
#   - quiet hours 22-07 UAE local (same as B5)
#   - WHATSAPP_ENABLED=0 -> no-op
#   - one in-flight pending_intent on the driver -> defer

STATUS_UPDATE_INTERVAL_S = 600   # 10 min, same cadence as B5


async def _status_update_dispatcher():
    """Send WhatsApp notifications for noteworthy order-status
    transitions.

    Sweeps every STATUS_UPDATE_INTERVAL_S. Finds order_status_log
    rows with notified_at IS NULL AND new_status in the noteworthy
    set, validates forward-progress, sends the one-line message,
    and stamps notified_at.
    """
    if not WHATSAPP_ENABLED:
        logger.info("status_update_dispatcher disabled (WHATSAPP_ENABLED=0)")
        return

    from whatsapp_client import wa_client
    from whatsapp_agent import (
        format_status_update, status_is_forward, _STATUS_NOTIFY_SET,
    )
    from notification_prefs import should_notify, is_quiet_hours

    while True:
        try:
            await asyncio.sleep(STATUS_UPDATE_INTERVAL_S)
            now = datetime.now()

            # Build placeholders for the noteworthy-status filter.
            placeholders = ",".join("?" * len(_STATUS_NOTIFY_SET))
            notify_list = list(_STATUS_NOTIFY_SET)

            with get_db(row_factory=True) as conn:
                # Age window: only notify on transitions within the
                # last 24h. Anything older is either a historical
                # backlog (notified_at is a freshly added column, so
                # every pre-existing row will look "unnotified") or
                # genuinely too stale to surface. The user can't act
                # on a delivery that was dispatched two days ago.
                rows = conn.execute(
                    f"""
                    SELECT osl.id AS log_id, osl.order_id,
                           osl.old_status, osl.new_status, osl.checked_at,
                           o.order_ref, o.delivery_type, o.expected_by,
                           s.name AS store_name
                    FROM order_status_log osl
                    JOIN orders o ON osl.order_id = o.id
                    JOIN stores s ON o.store_id = s.id
                    WHERE osl.notified_at IS NULL
                      AND LOWER(osl.new_status) IN ({placeholders})
                      AND osl.checked_at >= datetime('now', '-24 hours')
                    ORDER BY osl.checked_at DESC
                    LIMIT 10
                    """,
                    [s.lower() for s in notify_list],
                ).fetchall()

                # Silently age out anything older than 24h so we don't
                # re-query it every sweep. This is a one-time backfill
                # per row -- once stamped, the row won't reappear.
                conn.execute(
                    f"""UPDATE order_status_log
                       SET notified_at = datetime('now')
                       WHERE notified_at IS NULL
                         AND LOWER(new_status) IN ({placeholders})
                         AND checked_at < datetime('now', '-24 hours')""",
                    [s.lower() for s in notify_list],
                )
                conn.commit()

                if not rows:
                    continue

                driver = conn.execute(
                    """SELECT id, name, phone, member_pending_intent
                       FROM household_members
                       WHERE role = 'driver' AND COALESCE(phone, '') != ''
                       LIMIT 1"""
                ).fetchone()
                if not driver:
                    logger.warning(
                        "status_update_dispatcher: no driver phone; skipping %d row(s)",
                        len(rows),
                    )
                    continue

                if not should_notify(driver["id"], "b3_dispatched", conn=conn):
                    logger.debug(
                        "status_update_dispatcher: driver opted out, skip"
                    )
                    continue
                if is_quiet_hours(driver["id"], now=now, conn=conn):
                    logger.debug(
                        "status_update_dispatcher: quiet hours for driver, skip"
                    )
                    continue

                # Pick the most recent forward-progress transition.
                # Mark backwards/sideways ones as notified=now so they
                # don't keep showing up in the sweep.
                pick = None
                for r in rows:
                    if status_is_forward(r["old_status"], r["new_status"]):
                        pick = r
                        break
                    conn.execute(
                        "UPDATE order_status_log SET notified_at = ? WHERE id = ?",
                        (now.strftime("%Y-%m-%d %H:%M:%S"), r["log_id"]),
                    )
                conn.commit()

                if not pick:
                    continue

                # Don't pile on top of an open pending_intent (Tier-2 /
                # rating). Defer to next sweep.
                if driver["member_pending_intent"]:
                    logger.debug(
                        "status_update_dispatcher: driver has pending "
                        "intent, deferring order=%s", pick["order_id"],
                    )
                    continue

                msg = format_status_update(dict(pick), pick["new_status"])
                # Stamp notified_at BEFORE the network call so a
                # transient failure can't trigger re-sends.
                conn.execute(
                    "UPDATE order_status_log SET notified_at = ? WHERE id = ?",
                    (now.strftime("%Y-%m-%d %H:%M:%S"), pick["log_id"]),
                )
                conn.commit()

            try:
                await wa_client.send_message(driver["phone"], msg)
            except Exception as e:
                logger.error(
                    "status_update_dispatcher: send failed log_id=%s: %s",
                    pick["log_id"], e,
                )
                log_event(
                    "status_update_send_error", source="scheduler",
                    detail=f"log_id={pick['log_id']} err={str(e)[:160]}",
                    level="warning",
                )
                continue

            log_event(
                "status_update_sent", source="scheduler",
                detail=(
                    f"order={pick['order_id']} {pick['old_status']}->"
                    f"{pick['new_status']} to_member={driver['name']}"
                ),
            )
        except Exception as e:
            logger.error("status_update_dispatcher error: %s", e, exc_info=True)
            log_event(
                "status_update_dispatcher_error", source="scheduler",
                detail=str(e)[:200], level="error",
            )
            await asyncio.sleep(600)


AVAILABILITY_REFRESH_INTERVAL_S = 20 * 60  # 20 min — intraday availability refresh


async def _availability_refresh_loop():
    """Keep the active Scheduled list IN-STOCK without making the user wait.

    Replaces the old browser-on-connect availability check (which blocked / risked
    the app-open path). Every ~20 min, in the background, live-verify the active
    suggestion list and write fresh availability back to product_catalog, so the
    render-time swap shows an available alternative for any pick that went out of
    stock — by the time the user opens / refreshes the app, the list is already
    in-stock. Skips while a cart/scraper owns the browser; best-effort so a verify
    hiccup never kills the loop.
    """
    await asyncio.sleep(90)  # let the app settle after boot, then first pass
    while True:
        try:
            if not getattr(app_state, "cart_build_active", False):
                from agent_context import _active_suggestion_list_id
                with get_db(row_factory=True) as conn:
                    list_id = _active_suggestion_list_id(conn)
                if list_id:
                    from live_verify import live_verify_list_availability
                    result = await live_verify_list_availability(int(list_id))
                    if result.get("changed"):
                        log_event("availability_refresh", source="scheduler",
                                  detail=str(result)[:200])
                        try:
                            # nudge live clients to re-fetch the (now fresher) list
                            await broadcast({"type": "list_refresh"})
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("Availability refresh loop error: %s", e)
        await asyncio.sleep(AVAILABILITY_REFRESH_INTERVAL_S)


SCHEDULED_LIST_REFRESH_INTERVAL_S = 8 * 3600  # ~3x/day


async def _stale_list_refresh_loop():
    """Refresh the Scheduled list ~3x/day AND roll it forward when its cycle ends.

    Two jobs in one loop:
      - In-cycle (target_date >= today): regenerate for the SAME target_date so
        the list reflects the latest learned preferences, catalog, prices and
        availability. Regeneration MERGES into the existing draft — user edits
        (skips, swaps, qty changes, pins, "wrong item" blocks) are preserved;
        only the system-suggested rows are refreshed. This is what makes the
        feedback loop reactive: a flag you set is reflected within hours, not
        only at the next order date.
      - Stale (target_date passed) or no list: roll forward to the next order
        date so the user never opens the app to last cycle's suggestions.

    Runs every SCHEDULED_LIST_REFRESH_INTERVAL_S (~3x/day). Best-effort; skipped
    while a cart build holds the DB busy.
    """
    await asyncio.sleep(120)  # settle after boot
    while True:
        try:
            from datetime import date
            from agent_context import _active_suggestion_list_id
            from list_generate import list_generate
            from agent_brain import next_order_date
            lid = td = None
            with get_db(row_factory=True) as conn:
                lid = _active_suggestion_list_id(conn)
                if lid:
                    row = conn.execute(
                        "SELECT target_date FROM shopping_lists WHERE id = ?", (lid,)
                    ).fetchone()
                    td = (row["target_date"] if row else None)
            today = date.today().isoformat()
            nxt = next_order_date()
            # Stale/none -> next order date; in-cycle -> same target (merge-refresh).
            target = nxt if ((not lid) or (td and td < today)) else (td or nxt)
            if target and not app_state.cart_build_active:
                list_generate(target_date=target)
                log_event("scheduled_list_refresh", source="scheduler",
                          detail=f"refreshed Scheduled list for {target} (was target {td})")
        except Exception as e:
            logger.warning("Scheduled list refresh loop error: %s", e)
        await asyncio.sleep(SCHEDULED_LIST_REFRESH_INTERVAL_S)


async def _scheduler_heartbeat():
    """Unconditional liveness tick: stamp app_state.scheduler_last_beat every
    ~60s so /api/health can prove the scheduler (and the event loop) is alive —
    not just the HTTP process. The watchdog self-heals when this goes stale."""
    import time as _t
    while True:
        try:
            app_state.scheduler_last_beat = _t.time()
        except Exception:
            pass
        await asyncio.sleep(60)


async def start_scheduler():
    """Start background tasks: liveness heartbeat + nudge scheduler + order status poller + catalog scraper + pattern detector + Carrefour detail backfill + feedback timeout + WhatsApp resilience + delivered-rating dispatcher + status-update dispatcher."""
    import time as _t
    app_state.scheduler_last_beat = _t.time()  # mark alive immediately on boot
    asyncio.create_task(_scheduler_heartbeat())
    asyncio.create_task(_nudge_scheduler())
    asyncio.create_task(_availability_refresh_loop())
    logger.info("Availability refresh loop started (every %ds)", AVAILABILITY_REFRESH_INTERVAL_S)
    asyncio.create_task(_stale_list_refresh_loop())
    logger.info("Scheduled-list refresh loop started (~3x/day, every %ds)",
                SCHEDULED_LIST_REFRESH_INTERVAL_S)
    asyncio.create_task(_order_status_poller())
    asyncio.create_task(_nightly_catalog_scraper())
    asyncio.create_task(_daily_rescore_backstop())
    asyncio.create_task(_nightly_pattern_detector())
    asyncio.create_task(_nightly_carrefour_order_detail_backfill())
    logger.info("Nightly catalog scraper scheduled (4:00 AM daily)")
    logger.info("Daily rescore backstop scheduled (5:45 AM daily, runs only if index stale)")
    logger.info("Nightly pattern detector scheduled (5:00 AM daily)")
    logger.info("Carrefour order-detail backfill scheduled (5:30 AM daily)")
    if WHATSAPP_ENABLED:
        asyncio.create_task(_feedback_timeout_checker())
        asyncio.create_task(_nightly_whatsapp_restart())
        asyncio.create_task(_whatsapp_liveness_probe())
        asyncio.create_task(_delivered_rating_dispatcher())
        asyncio.create_task(_status_update_dispatcher())
        asyncio.create_task(_rating_reminder_dispatcher())
        asyncio.create_task(_today_deliveries_morning_dispatcher())
        logger.info("WhatsApp feedback timeout checker started")
        logger.info("WhatsApp nightly restart scheduled (3:30 AM daily)")
        logger.info(
            "WhatsApp liveness probe started (every %ds, restart after %d failures)",
            PROBE_INTERVAL_S, PROBE_FAIL_THRESHOLD,
        )
        logger.info(
            "Delivered-rating dispatcher started (every %ds, quiet 22-07)",
            DELIVERED_PROMPT_INTERVAL_S,
        )
        logger.info(
            "Status-update dispatcher started (every %ds, quiet 22-07)",
            STATUS_UPDATE_INTERVAL_S,
        )
        logger.info(
            "Rating-reminder dispatcher started (every %ds, threshold %dh)",
            REMINDER_SWEEP_INTERVAL_S, REMINDER_AGE_HOURS,
        )
        logger.info(
            "Today's-deliveries morning dispatcher scheduled (%02d:%02d daily)",
            DAY_SUMMARY_HOUR, DAY_SUMMARY_MIN,
        )
    logger.info("Background tasks started (nudge scheduler + order poller + nightly scraper + pattern detector + WhatsApp resilience + delivered-rating + status-update + rating-reminder + today's-deliveries)")
