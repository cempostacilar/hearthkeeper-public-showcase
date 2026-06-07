"""Shared runtime state and connection helpers.

In its own module so app.py and scheduler.py can both reach the live
WebSocket connections without importing each other (a circular import).
"""
import sqlite3
from contextlib import contextmanager

from db_setup import DB_PATH
from app_logging import get_logger

logger = get_logger("app_state")

# Live WebSocket connections — a set, not a single global, so a second
# household device no longer kicks the first one off.
connections = set()

# The most-recently-connected agent; schedulers read session state from it.
active_agent = None

# True while the cart builder owns the browser (blocks the nightly scraper).
cart_build_active = False

# True while a manual or scheduled order_poller run is in flight. Guards
# the /api/admin/run-order-poller endpoint against double-clicks (UC can't
# launch two Chrome instances on the same shared profile dir).
order_poll_active = False

# Liveness heartbeat. The scheduler's _scheduler_heartbeat task stamps this
# (epoch seconds) every ~60s. /api/health exposes its age so the watchdog can
# distinguish "HTTP process is up" from "background work is actually running".
# 0.0 = the scheduler has never ticked (e.g. it never started).
scheduler_last_beat = 0.0

# Result of the most recent admin-triggered background cart build (for polling
# /api/admin/build-cart-status). Shape: {status, list_id, report?, error?, summary?}.
last_cart_build = None


@contextmanager
def get_db(row_factory=False):
    """Open a SQLite connection that commits on a clean exit and always closes."""
    conn = sqlite3.connect(DB_PATH)
    if row_factory:
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


async def broadcast(payload: dict):
    """Send a JSON payload to every live WebSocket connection."""
    for ws in list(connections):
        try:
            await ws.send_json(payload)
        except Exception as e:
            logger.debug("Broadcast to a connection failed, dropping it: %s", e)
            connections.discard(ws)
