"""
Hearthkeeper - Centralized Logging & Event Tracking

Two systems:
  1. Python logging → file + console (for debugging crashes)
  2. app_events table → SQLite (for tracking what happened over time)

Log files rotate at 5 MB, keeps last 3 files.
Events persist in DB forever (tiny rows, ~100 bytes each).

Usage:
  from app_logging import get_logger, log_event

  logger = get_logger("agent_brain")
  logger.info("Starting check-in cycle")
  logger.error("Cart build failed", exc_info=True)

  log_event("cycle_start", target_date="2026-02-25")
  log_event("error", source="cart_builder", detail="Edge profile locked")
"""

import json
import logging
import sqlite3
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from db_setup import DB_PATH

# ============================================================
# LOG FILE SETUP
# ============================================================

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "hearthkeeper.log"

# Shared formatter
_FORMAT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# File handler — rotates at 5 MB, keeps 3 backups
_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(_FORMAT)
_file_handler.setLevel(logging.DEBUG)

# Console handler — INFO and above
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_FORMAT)
_console_handler.setLevel(logging.INFO)

# Root logger config
_root = logging.getLogger("hearthkeeper")
_root.setLevel(logging.DEBUG)
if not _root.handlers:
    _root.addHandler(_file_handler)
    _root.addHandler(_console_handler)


def get_logger(name: str) -> logging.Logger:
    """Get a named child logger (e.g., get_logger('agent_brain'))."""
    return logging.getLogger(f"hearthkeeper.{name}")


# ============================================================
# DB EVENT TRACKING
# ============================================================

_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS app_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    event TEXT NOT NULL,
    source TEXT,
    detail TEXT,
    data TEXT,
    level TEXT DEFAULT 'info'
)
"""

_events_table_ready = False


def _ensure_events_table():
    """Create app_events table if it doesn't exist."""
    global _events_table_ready
    if _events_table_ready:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(_EVENTS_TABLE)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_event
            ON app_events (event)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_timestamp
            ON app_events (timestamp)
        """)
        conn.commit()
        conn.close()
        _events_table_ready = True
    except Exception:
        pass  # DB might not exist yet at import time


def log_event(event: str, source: str = None, detail: str = None,
              level: str = "info", **extra):
    """
    Persist an event to the app_events table.

    Args:
        event: Event type (e.g., "cycle_start", "error", "cart_built",
               "user_action", "ws_connect", "ws_disconnect")
        source: Where it happened (e.g., "agent_brain", "cart_builder", "frontend")
        detail: Human-readable description
        level: "debug", "info", "warning", "error"
        **extra: Any additional data (serialized to JSON)

    Common events:
        cycle_start     — New order cycle began
        cycle_end       — Order cycle completed
        list_generated  — Shopping list created
        user_action     — User did something (approve, skip, add, etc.)
        cart_built      — Cart building finished
        cart_error      — Cart building failed
        ws_connect      — WebSocket client connected
        ws_disconnect   — WebSocket client disconnected
        llm_call        — LLM API called
        error           — Something went wrong
        frontend        — Event from the web UI
    """
    _ensure_events_table()

    # Also log to file
    logger = get_logger(source or "events")
    log_msg = f"[{event}] {detail or ''}"
    if extra:
        log_msg += f" | {extra}"
    getattr(logger, level, logger.info)(log_msg)

    # Persist to DB
    try:
        data_json = json.dumps(extra) if extra else None
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO app_events (event, source, detail, data, level)
            VALUES (?, ?, ?, ?, ?)
        """, (event, source, detail, data_json, level))
        conn.commit()
        conn.close()
    except Exception as e:
        # Don't crash the app over logging failures
        logging.getLogger("hearthkeeper.events").warning(
            f"Could not persist event: {e}"
        )


def get_recent_events(limit: int = 50, event_filter: str = None,
                      level_filter: str = None) -> list:
    """
    Retrieve recent events from the DB.

    Args:
        limit: Max events to return
        event_filter: Only this event type (e.g., "error")
        level_filter: Only this level or above

    Returns list of dicts.
    """
    _ensure_events_table()
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        query = "SELECT * FROM app_events WHERE 1=1"
        params = []

        if event_filter:
            query += " AND event = ?"
            params.append(event_filter)

        if level_filter:
            levels = {"debug": 0, "info": 1, "warning": 2, "error": 3}
            min_level = levels.get(level_filter, 0)
            allowed = [k for k, v in levels.items() if v >= min_level]
            placeholders = ",".join("?" for _ in allowed)
            query += f" AND level IN ({placeholders})"
            params.extend(allowed)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        return [dict(r) for r in rows]
    except Exception:
        return []


def log_exception(source: str, detail: str = None):
    """Log the current exception with full traceback."""
    tb = traceback.format_exc()
    log_event(
        "error",
        source=source,
        detail=detail or "Unhandled exception",
        level="error",
        traceback=tb,
    )
