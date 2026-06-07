"""Hearthkeeper backend dashboard.

A read-only operations view of every backend job running on this host:
the long-running Windows services, the OS-level scheduled tasks, the
in-process scheduler tasks owned by the FastAPI app, and the on-demand
operations triggered from the UI / WhatsApp / agent.

For each job we surface:
  - a coarse health status: green / amber / red / unknown
  - a one-line summary
  - zero or more callouts (the "what is wrong" text the dashboard shows
    in the red/amber section -- recent failures, latency hits, drop-off)
  - up to ~24 recent outcomes for a sparkline

Two endpoints, both registered through an APIRouter so they pick up the
existing app-level login middleware in app.py:

  GET  /api/health/jobs           -- the JSON the dashboard polls every 20 s
  GET  /admin/dashboard           -- the dashboard HTML page
  GET  /api/admin/orders-summary  -- per-store order totals + recent poller events
  POST /api/admin/run-order-poller-- manually trigger an order-history scrape
"""

import asyncio
import json
import subprocess
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from app_logging import get_logger
from db_setup import DB_PATH


logger = get_logger("health_dashboard")
router = APIRouter()

PROJECT_DIR = Path(__file__).parent
STATIC_HTML = PROJECT_DIR / "static" / "admin_dashboard.html"


# ============================================================
# JOB CATALOG
# ============================================================

# Each entry describes one backend job. The `type` field selects which
# health-computation function to use; the other fields parameterise it.
JOBS = [
    # -- Long-running Windows services -----------------------------------
    {
        "id": "hearthkeeper-app",
        "label": "App (FastAPI)",
        "type": "service",
        "tier": "core",
        "windows_service": "hearthkeeper-app",
        "http_health": "http://localhost:8000/api/health",
        "description": "FastAPI app + scheduler on port 8000",
    },
    {
        "id": "hearthkeeper-cloudflared",
        "label": "Cloudflare Tunnel",
        "type": "service",
        "tier": "ingress",
        "windows_service": "hearthkeeper-cloudflared",
        "description": "Tunnel routing hearthkeeper.cc to port 8000",
    },
    {
        "id": "hearthkeeper-whatsapp",
        "label": "WhatsApp Sidecar",
        "type": "service",
        "tier": "ingress",
        "windows_service": "hearthkeeper-whatsapp",
        "http_health": "http://localhost:3001/status",
        "is_whatsapp": True,
        "description": "Node sidecar on port 3001",
    },

    # -- OS-level scheduled tasks -----------------------------------------
    {
        "id": "watchdog",
        "label": "Watchdog",
        "type": "log_task",
        "tier": "ops",
        "log_file": "logs/watchdog.log",
        "expected_interval_minutes": 5,
        "description": "Restarts the app via NSSM if /api/health fails. Every 5 min.",
    },
    {
        "id": "db_backup",
        "label": "DB Backup",
        "type": "backup",
        "tier": "ops",
        "backup_dir": "backups",
        "expected_interval_minutes": 24 * 60,
        "description": "Online SQLite backup with 7-day rotation. Daily at 3 AM.",
    },

    # -- In-process scheduler tasks (events-based) ------------------------
    {
        "id": "nightly_scraper",
        "label": "Nightly Catalog Scraper",
        "type": "scheduler_event",
        "tier": "scheduler",
        "events_start": ["nightly_scrape_start"],
        "events_done": ["nightly_scrape_done"],
        "events_error": ["nightly_scrape_error"],
        "expected_interval_minutes": 24 * 60,
        "description": "Refreshes product_catalog. Daily at 4 AM.",
    },
    {
        "id": "order_poller",
        "label": "Order Poller",
        "type": "scheduler_event",
        "tier": "scheduler",
        "events_start": ["order_poller_start"],
        "events_done": ["order_poller_done"],
        "events_error": ["order_poller_error"],
        "events_skip": ["order_poller_skip"],
        "expected_interval_minutes": 360,
        "description": "Scrapes order history + statuses. Every 2 h (skips if cart build in progress).",
    },
    {
        "id": "nudge_scheduler",
        "label": "Nudge Scheduler",
        "type": "scheduler_event",
        "tier": "scheduler",
        "events_done": ["nudge_sent"],
        "expected_interval_minutes": None,
        "description": "Check-in Mon/Thu 7 PM+, post-order Wed/Sat 9 AM+. Wakes every 30 min.",
    },
    {
        "id": "feedback_timeout",
        "label": "Feedback Timeout Checker",
        "type": "parent_service",
        "tier": "scheduler",
        "parent": "hearthkeeper-app",
        "description": "Expires stalled WhatsApp feedback rounds. Wakes every 5 min.",
    },

    # -- On-demand operations ---------------------------------------------
    {
        "id": "cart_builder",
        "label": "Cart Builder",
        "type": "on_demand_event",
        "tier": "ondemand",
        "events_done": ["cart_built"],
        "events_error": ["cart_error"],
        "description": "Builds Carrefour + Kibsons carts when the approver confirms a list.",
    },
]


# Directed edges for the flow chart. Mostly data flow; the watchdog ->
# app edge is more "monitors" than "data flow" but we keep it simple.
EDGES = [
    ("external_cloudflare", "hearthkeeper-cloudflared"),
    ("external_whatsapp", "hearthkeeper-whatsapp"),
    ("hearthkeeper-cloudflared", "hearthkeeper-app"),
    ("hearthkeeper-whatsapp", "hearthkeeper-app"),
    ("watchdog", "hearthkeeper-app"),
    ("hearthkeeper-app", "nudge_scheduler"),
    ("hearthkeeper-app", "order_poller"),
    ("hearthkeeper-app", "feedback_timeout"),
    ("hearthkeeper-app", "nightly_scraper"),
    ("hearthkeeper-app", "cart_builder"),
    ("hearthkeeper-app", "db_backup"),
]


# ============================================================
# LOW-LEVEL HELPERS
# ============================================================

def _query_service_sync(name):
    """Return one of 'running', 'stopped', 'paused', 'notfound', 'error'.

    Blocks on subprocess.run -- call via asyncio.to_thread from async code
    so we don't freeze the FastAPI event loop on slow sc.exe responses.
    """
    try:
        r = subprocess.run(
            ["sc", "query", name],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return "notfound"
        out = r.stdout.upper()
        if "RUNNING" in out:
            return "running"
        if "STOPPED" in out:
            return "stopped"
        if "PAUSED" in out:
            return "paused"
        return "unknown"
    except Exception as e:
        logger.warning("sc query %s failed: %s", name, e)
        return "error"


async def _query_service(name):
    """Async wrapper around the blocking sc.exe call."""
    return await asyncio.to_thread(_query_service_sync, name)


async def _http_check(url, timeout=5):
    """Return (ok, body_snippet) for a GET to the given URL.

    Uses httpx.AsyncClient so a self-call to localhost (which is what the
    App (FastAPI) tile does to check its own /api/health) does not freeze
    the event loop -- a sync urllib.request.urlopen here would block the
    single uvicorn worker that needs to serve the very response we're
    waiting for, deadlocking until the timeout expired.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            body = resp.text[:800]
            return resp.status_code < 400, body
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}"
    except Exception as e:
        return False, str(e)[:200]


def _recent_events(events_in, since=None, limit=100):
    """Query the app_events table for rows matching any of the event names."""
    if not events_in:
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(events_in))
        params = list(events_in)
        where = "event IN (" + placeholders + ")"
        if since:
            where += " AND timestamp >= ?"
            params.append(since.strftime("%Y-%m-%d %H:%M:%S"))
        rows = conn.execute(
            "SELECT timestamp, event, source, detail, level FROM app_events "
            "WHERE " + where + " ORDER BY id DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("_recent_events failed: %s", e)
        return []


def _fmt_ago(when_str):
    """Pretty 'N min ago' for a YYYY-MM-DD HH:MM:SS or ISO timestamp."""
    try:
        when = datetime.strptime(when_str[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            when = datetime.fromisoformat(when_str)
        except Exception:
            return when_str
    delta = (datetime.now() - when).total_seconds()
    if delta < 60:
        return "%ds ago" % int(delta)
    if delta < 3600:
        return "%dm ago" % int(delta / 60)
    if delta < 86400:
        return "%.1fh ago" % (delta / 3600)
    return "%.1fd ago" % (delta / 86400)


# ============================================================
# STATUS COMPUTATION PER JOB TYPE
# ============================================================

async def _status_service(job):
    svc = await _query_service(job["windows_service"])
    callouts = []
    if svc == "running":
        status = "green"
        summary = "Service Running"
        if "http_health" in job:
            ok, body = await _http_check(job["http_health"], timeout=5)
            if not ok:
                status = "amber"
                summary = "Service running but HTTP check failed"
                callouts.append(body[:300])
            elif job.get("is_whatsapp"):
                try:
                    data = json.loads(body)
                    if not data.get("connected"):
                        status = "amber"
                        summary = "Service up but WhatsApp not linked"
                        if data.get("hasQR"):
                            callouts.append("Waiting for QR scan -- open http://<host>:3001/qr to link")
                        if data.get("error"):
                            callouts.append("WhatsApp error: " + str(data["error"])[:300])
                    else:
                        summary = "Service Running, WhatsApp connected"
                except Exception:
                    pass
    elif svc == "stopped":
        status = "red"
        summary = "Service STOPPED"
        callouts.append("Start with: nssm start " + job["windows_service"])
    elif svc == "notfound":
        status = "red"
        summary = "Service NOT INSTALLED"
    else:
        status = "amber"
        summary = "Service state: " + svc
    return {
        "current_status": status,
        "summary": summary,
        "callouts": callouts,
        "history": [],
    }


def _status_log_task(job):
    log_path = PROJECT_DIR / job["log_file"]
    if not log_path.exists():
        return {
            "current_status": "red",
            "summary": "Log file not found -- task may not be installed",
            "callouts": ["Expected at " + str(log_path)],
            "history": [],
        }
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {
            "current_status": "amber",
            "summary": "Could not read log: " + str(e),
            "callouts": [],
            "history": [],
        }
    parsed = []
    for line in text.splitlines()[-400:]:
        if len(line) > 21 and line.startswith("[") and line[20] == "]":
            try:
                ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
                parsed.append((ts, line))
            except Exception:
                continue
    if not parsed:
        return {
            "current_status": "amber",
            "summary": "Log has no parseable timestamped entries",
            "callouts": [],
            "history": [],
        }
    last_ts = parsed[-1][0]
    minutes_since = (datetime.now() - last_ts).total_seconds() / 60
    expected = job.get("expected_interval_minutes", 5)
    callouts = []
    if minutes_since <= expected * 1.5:
        status = "green"
    elif minutes_since <= expected * 3:
        status = "amber"
        callouts.append(
            "Overdue: last run %d min ago, expected every %d min" % (int(minutes_since), expected)
        )
    else:
        status = "red"
        callouts.append(
            "Overdue: last run %d min ago, expected every %d min" % (int(minutes_since), expected)
        )

    # History: per "Watchdog check starting" -> next, classify outcome
    history = []
    starts = [i for i, (_, line) in enumerate(parsed) if "watchdog check starting" in line.lower()]
    for idx_pos, idx in enumerate(starts[-24:]):
        end = starts[starts.index(idx) + 1] if (starts.index(idx) + 1) < len(starts) else len(parsed)
        chunk = " | ".join(parsed[k][1].lower() for k in range(idx, end))
        if "failed to restart" in chunk:
            history.append("red")
        elif "not healthy" in chunk or "restarted successfully" in chunk:
            history.append("amber")
        else:
            history.append("green")

    unhealthy = [p for p in parsed[-150:] if "app is not healthy" in p[1].lower()]
    if unhealthy:
        last = unhealthy[-1][0].strftime("%Y-%m-%d %H:%M:%S")
        callouts.append(
            "%d unhealthy detection(s) in recent log; latest %s" % (len(unhealthy), _fmt_ago(last))
        )

    return {
        "current_status": status,
        "summary": "Last check %d min ago" % int(minutes_since),
        "callouts": callouts,
        "history": history,
        "last_run": last_ts.isoformat(timespec="seconds"),
    }


def _status_backup(job):
    backup_dir = PROJECT_DIR / job["backup_dir"]
    if not backup_dir.exists():
        return {
            "current_status": "red",
            "summary": "Backups directory not found",
            "callouts": ["Expected at " + str(backup_dir)],
            "history": [],
        }
    backups = sorted(
        backup_dir.glob("autostock-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        return {
            "current_status": "red",
            "summary": "No backups present",
            "callouts": ["Daily backup task has not produced a file yet"],
            "history": [],
        }
    latest = backups[0]
    mtime = datetime.fromtimestamp(latest.stat().st_mtime)
    age_hours = (datetime.now() - mtime).total_seconds() / 3600
    size_mb = latest.stat().st_size / (1024 * 1024)
    callouts = []
    if age_hours <= 25:
        status = "green"
    elif age_hours <= 49:
        status = "amber"
        callouts.append("Latest backup is %d h old (expected daily)" % int(age_hours))
    else:
        status = "red"
        callouts.append("Latest backup is %d h old (expected daily)" % int(age_hours))
    return {
        "current_status": status,
        "summary": "%s (%.1f MB, %s)" % (latest.name, size_mb, _fmt_ago(mtime.strftime("%Y-%m-%d %H:%M:%S"))),
        "callouts": callouts,
        "history": ["green"] * min(len(backups), 14),
        "last_run": mtime.isoformat(timespec="seconds"),
    }


def _status_scheduler_event(job):
    since = datetime.now() - timedelta(hours=72)
    all_events = (
        list(job.get("events_start", []))
        + list(job.get("events_done", []))
        + list(job.get("events_error", []))
        + list(job.get("events_skip", []))
    )
    rows = _recent_events(all_events, since=since, limit=200)
    expected = job.get("expected_interval_minutes")
    callouts = []
    if not rows:
        if expected and expected < 24 * 60:
            return {
                "current_status": "amber",
                "summary": "No events in last 72 h",
                "callouts": ["Expected every %d min" % expected],
                "history": [],
            }
        return {
            "current_status": "green",
            "summary": "No recent events (OK for sparse jobs)",
            "callouts": [],
            "history": [],
        }
    last_done = next((e for e in rows if e["event"] in job.get("events_done", [])), None)
    last_start = next((e for e in rows if e["event"] in job.get("events_start", [])), None)
    last_error = next(
        (e for e in rows if e["event"] in job.get("events_error", []) or e["level"] == "error"),
        None,
    )

    now = datetime.now()
    if last_done:
        try:
            done_ts = datetime.strptime(last_done["timestamp"], "%Y-%m-%d %H:%M:%S")
            minutes_since = (now - done_ts).total_seconds() / 60
            summary = "Last success %s" % _fmt_ago(last_done["timestamp"])
            if expected:
                if minutes_since <= expected * 1.5:
                    status = "green"
                elif minutes_since <= expected * 3:
                    status = "amber"
                    callouts.append(
                        "Overdue: expected every %d min, last success %d min ago"
                        % (expected, int(minutes_since))
                    )
                else:
                    status = "red"
                    callouts.append(
                        "Overdue: expected every %d min, last success %d min ago"
                        % (expected, int(minutes_since))
                    )
            else:
                status = "green"
        except Exception:
            status = "amber"
            summary = "Could not parse last-success timestamp"
    else:
        status = "amber"
        summary = "No success events found in lookback window"

    if last_error:
        callouts.append(
            "Most recent error (%s): %s"
            % (_fmt_ago(last_error["timestamp"]), (last_error.get("detail") or "")[:300])
        )

    history = []
    for e in reversed(rows[:24]):
        if e["event"] in job.get("events_done", []):
            history.append("green")
        elif e["event"] in job.get("events_error", []) or e["level"] == "error":
            history.append("red")
        elif e["event"] in job.get("events_skip", []):
            history.append("gray")
        elif e["event"] in job.get("events_start", []):
            history.append("amber")
        else:
            history.append("gray")

    return {
        "current_status": status,
        "summary": summary,
        "callouts": callouts,
        "history": history,
        "last_run": (last_start or last_done or {}).get("timestamp"),
        "last_success": last_done["timestamp"] if last_done else None,
        "last_failure": last_error["timestamp"] if last_error else None,
    }


def _status_parent_service(job, statuses):
    parent = statuses.get(job["parent"], {})
    parent_status = parent.get("current_status", "unknown")
    if parent_status == "green":
        return {
            "current_status": "green",
            "summary": "Riding on parent service",
            "callouts": [],
            "history": [],
        }
    return {
        "current_status": parent_status if parent_status != "unknown" else "amber",
        "summary": "Mirrors parent (" + job["parent"] + ")",
        "callouts": [],
        "history": [],
    }


def _status_on_demand_event(job):
    since = datetime.now() - timedelta(days=30)
    rows = _recent_events(
        job.get("events_done", []) + job.get("events_error", []),
        since=since, limit=100,
    )
    if not rows:
        return {
            "current_status": "green",
            "summary": "No invocations yet (on-demand)",
            "callouts": [],
            "history": [],
        }
    last_done = next((e for e in rows if e["event"] in job.get("events_done", [])), None)
    last_error = next(
        (e for e in rows if e["event"] in job.get("events_error", []) or e["level"] == "error"),
        None,
    )
    callouts = []
    if last_error and last_done:
        try:
            err_ts = datetime.strptime(last_error["timestamp"], "%Y-%m-%d %H:%M:%S")
            done_ts = datetime.strptime(last_done["timestamp"], "%Y-%m-%d %H:%M:%S")
            if err_ts > done_ts:
                status = "amber"
                summary = "Last attempt failed (%s)" % _fmt_ago(last_error["timestamp"])
                callouts.append((last_error.get("detail") or "")[:300])
            else:
                status = "green"
                summary = "Last success %s" % _fmt_ago(last_done["timestamp"])
        except Exception:
            status = "amber"
            summary = "Could not parse timestamps"
    elif last_error and not last_done:
        status = "red"
        summary = "Last attempt failed (%s)" % _fmt_ago(last_error["timestamp"])
        callouts.append((last_error.get("detail") or "")[:300])
    else:
        status = "green"
        summary = "Last success %s" % _fmt_ago(last_done["timestamp"])
    history = []
    for e in reversed(rows[:24]):
        if e["event"] in job.get("events_done", []):
            history.append("green")
        elif e["event"] in job.get("events_error", []) or e["level"] == "error":
            history.append("red")
        else:
            history.append("gray")
    return {
        "current_status": status,
        "summary": summary,
        "callouts": callouts,
        "history": history,
        "last_success": last_done["timestamp"] if last_done else None,
        "last_failure": last_error["timestamp"] if last_error else None,
    }


# ============================================================
# DASHBOARD ASSEMBLY
# ============================================================

_STATUS_FN = {
    "service": _status_service,
    "log_task": _status_log_task,
    "backup": _status_backup,
    "scheduler_event": _status_scheduler_event,
    "on_demand_event": _status_on_demand_event,
}


async def get_dashboard_data():
    """Build the dashboard JSON.

    Status functions are mostly sync (sqlite reads, filesystem checks) so
    we call them directly. _status_service is async because it does HTTP
    self-checks + sc.exe subprocess calls -- we await it so the FastAPI
    event loop stays free to serve the very /api/health request the
    self-check is waiting on (otherwise we'd deadlock at single-worker).
    """
    statuses = {}
    deferred = []
    for job in JOBS:
        fn = _STATUS_FN.get(job["type"])
        if fn:
            try:
                result = fn(job)
                if asyncio.iscoroutine(result):
                    result = await result
                statuses[job["id"]] = result
            except Exception as e:
                logger.exception("Failed to compute status for %s", job["id"])
                statuses[job["id"]] = {
                    "current_status": "unknown",
                    "summary": "Status check raised: " + str(e)[:200],
                    "callouts": [],
                    "history": [],
                }
        elif job["type"] == "parent_service":
            deferred.append(job)
        else:
            statuses[job["id"]] = {
                "current_status": "unknown",
                "summary": "Unhandled job type: " + job["type"],
                "callouts": [],
                "history": [],
            }
    for job in deferred:
        statuses[job["id"]] = _status_parent_service(job, statuses)

    jobs_payload = []
    for job in JOBS:
        st = statuses.get(job["id"], {})
        payload = {
            "id": job["id"],
            "label": job["label"],
            "tier": job.get("tier", "core"),
            "type": job["type"],
            "description": job.get("description", ""),
        }
        payload.update(st)
        jobs_payload.append(payload)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "jobs": jobs_payload,
        "edges": [{"from": a, "to": b} for a, b in EDGES],
    }


# ============================================================
# HTTP ROUTES
# ============================================================

@router.get("/api/health/jobs")
async def health_jobs():
    return await get_dashboard_data()


@router.get("/admin/dashboard")
async def admin_dashboard():
    if not STATIC_HTML.exists():
        return JSONResponse(
            {"error": "Dashboard HTML missing", "path": str(STATIC_HTML)},
            status_code=500,
        )
    return FileResponse(STATIC_HTML, media_type="text/html; charset=utf-8")


@router.get("/api/admin/orders-summary")
async def admin_orders_summary():
    """Per-store order totals + status breakdown + recent poller events.

    Read-only. Auth-gated by app.py's require_login middleware (the route
    prefix is not in _AUTH_EXEMPT). Feeds the Orders tab of the backend
    dashboard.

    Response shape (JSON):
      {
        "generated_at": ISO timestamp,
        "totals": { "orders": N, "items": N, "gross_aed": F },
        "stores": [
          {
            "store_id": N, "name": str,
            "order_count": N, "item_count": N, "gross_aed": F,
            "oldest_order": YYYY-MM-DD, "newest_order": YYYY-MM-DD,
            "status_breakdown": [{"status": str, "count": N}, ...]
          },
          ...
        ],
        "recent_poller_events": [
          {"timestamp", "event", "level", "source", "detail"},
          ...
        ]
      }
    """
    db_path = Path(DB_PATH)
    if not db_path.exists():
        return JSONResponse(
            {"error": "autostock.db missing", "path": str(db_path)},
            status_code=500,
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        stores = []
        store_rows = conn.execute(
            """
            SELECT s.id, s.name,
                   COUNT(o.id) AS order_count,
                   MIN(o.order_date) AS oldest,
                   MAX(o.order_date) AS newest,
                   COALESCE(SUM(o.total_amount), 0) AS gross_aed,
                   (SELECT COUNT(*) FROM order_items oi
                    JOIN orders o2 ON o2.id = oi.order_id
                    WHERE o2.store_id = s.id) AS item_count
            FROM stores s
            LEFT JOIN orders o ON o.store_id = s.id
            GROUP BY s.id, s.name
            ORDER BY order_count DESC, s.name
            """
        ).fetchall()
        for r in store_rows:
            status_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM orders WHERE store_id = ?
                GROUP BY status ORDER BY n DESC
                """,
                (r["id"],),
            ).fetchall()
            stores.append({
                "store_id": r["id"],
                "name": r["name"],
                "order_count": r["order_count"] or 0,
                "item_count": r["item_count"] or 0,
                "gross_aed": round(float(r["gross_aed"] or 0), 2),
                "oldest_order": r["oldest"],
                "newest_order": r["newest"],
                "status_breakdown": [
                    {"status": sr["status"], "count": sr["n"]}
                    for sr in status_rows
                ],
            })

        events = []
        for r in conn.execute(
            """
            SELECT timestamp, event,
                   COALESCE(level, 'info') AS level,
                   COALESCE(detail, '') AS detail,
                   COALESCE(source, '') AS source
            FROM app_events
            WHERE (event LIKE 'order_poller_%'
                   OR event LIKE 'carrefour_%'
                   OR event LIKE 'kibsons_%')
              AND timestamp >= datetime('now', '-2 day')
            ORDER BY timestamp DESC
            LIMIT 50
            """
        ).fetchall():
            events.append({
                "timestamp": r["timestamp"],
                "event": r["event"],
                "level": r["level"],
                "source": r["source"],
                "detail": r["detail"],
            })

        # Recent orders (60 most recent across all stores) -- powers the
        # "Recent orders" list on the Orders tab. Joined to stores for the
        # name, plus an item-count subquery so the row can show how big
        # each order was without an N+1 round-trip per row.
        recent_orders = []
        for r in conn.execute(
            """
            SELECT o.id,
                   o.order_ref,
                   o.order_date,
                   o.total_amount,
                   o.currency,
                   o.status,
                   COALESCE(o.delivery_type, '') AS delivery_type,
                   o.delivery_date,
                   o.scraped_at,
                   s.name AS store_name,
                   (SELECT COUNT(*) FROM order_items oi WHERE oi.order_id = o.id) AS item_count
            FROM orders o
            JOIN stores s ON s.id = o.store_id
            ORDER BY o.order_date DESC, o.id DESC
            LIMIT 60
            """
        ).fetchall():
            recent_orders.append({
                "id": r["id"],
                "store": r["store_name"],
                "order_ref": r["order_ref"],
                "order_date": r["order_date"],
                "delivery_date": r["delivery_date"],
                "status": r["status"],
                "delivery_type": r["delivery_type"],
                "total_amount": round(float(r["total_amount"] or 0), 2),
                "currency": r["currency"] or "AED",
                "item_count": r["item_count"] or 0,
                "scraped_at": r["scraped_at"],
            })

        total_orders = sum(s["order_count"] for s in stores)
        total_items = sum(s["item_count"] for s in stores)
        total_gross = round(sum(s["gross_aed"] for s in stores), 2)

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "totals": {
                "orders": total_orders,
                "items": total_items,
                "gross_aed": total_gross,
            },
            "stores": stores,
            "recent_orders": recent_orders,
            "recent_poller_events": events,
        }
    except Exception as e:
        logger.exception("orders-summary query failed")
        return JSONResponse({"error": str(e)[:300]}, status_code=500)
    finally:
        conn.close()


@router.post("/api/admin/run-order-poller")
async def admin_run_order_poller():
    """Manually trigger an order-history scrape (both Kibsons + Carrefour).

    Auth-gated by app.py's require_login middleware. Fires
    check_and_scrape_orders() as a background asyncio task and returns
    immediately so the dashboard isn't held for the multi-minute run.

    Guarded against:
      - Concurrent cart-build (shared UC profile dir): app_state.cart_build_active
      - Double-click (multiple manual triggers): app_state.order_poll_active

    Progress is visible on the Orders tab -- new order_poller_start/_done/
    _error rows land in the Recent Events table on the next 20 s tick.
    """
    import asyncio as _asyncio
    import app_state
    from app_logging import log_event

    # Check our own flag first so a same-op double-click gets the precise
    # "already running" message. Carrefour's internal cart_build_active is
    # also true while our run is in flight (process_carrefour_orders_uc
    # takes the shared profile lock), but reporting "cart build" there
    # would be misleading.
    if app_state.order_poll_active:
        return JSONResponse({
            "status": "skipped",
            "reason": "already_running",
            "message": "An order-poller run is already in flight.",
        }, status_code=409)
    if app_state.cart_build_active:
        return JSONResponse({
            "status": "skipped",
            "reason": "cart_build_active",
            "message": "A cart build is currently using the browser. Try again in a minute.",
        }, status_code=409)

    async def _run():
        # Lazy-import inside the task: order_scraper has heavy transitive
        # imports (playwright, selenium) we don't want to pay at module load.
        from order_scraper import check_and_scrape_orders
        app_state.order_poll_active = True
        log_event("order_poller_start", source="manual",
                  detail="Triggered from /admin/dashboard")
        try:
            result = await check_and_scrape_orders()
            new_n = len(result.get("new_orders", []))
            chg_n = len(result.get("status_changes", []))
            log_event(
                "order_poller_done", source="manual",
                detail=f"manual run: new={new_n}, changes={chg_n}",
            )
        except Exception as e:
            logger.exception("Manual order-poller run failed")
            log_event("order_poller_error", source="manual",
                      detail=str(e)[:200], level="error")
        finally:
            app_state.order_poll_active = False

    # Fire-and-forget. Capture the task on app_state so it isn't GC'd mid-flight.
    app_state.order_poll_task = _asyncio.create_task(_run())
    log_event("order_poller_manual_trigger", source="health_dashboard",
              detail="Manual run kicked off")
    return {
        "status": "started",
        "message": "Order poller running -- watch the Recent Events table.",
    }


@router.get("/admin/qrcode.min.js")
async def qrcode_lib():
    """Serve the QR-rendering JS library locally to avoid a CDN dependency.

    The library (davidshimjs's qrcodejs, ~20 KB, MIT-licensed) is vendored
    into the repo at static/qrcode.min.js so the dashboard renders the
    WhatsApp pairing QR even when external CDNs are blocked (e.g. by the
    user's mobile carrier). Same auth gate as the rest of the app.
    """
    lib_path = PROJECT_DIR / "static" / "qrcode.min.js"
    if not lib_path.exists():
        return JSONResponse({"error": "qrcode.min.js missing"}, status_code=500)
    return FileResponse(lib_path, media_type="application/javascript")


@router.get("/api/health/whatsapp/qr")
async def whatsapp_qr():
    """Proxy the sidecar's /qr endpoint to the dashboard.

    The WhatsApp sidecar is bound to 127.0.0.1:3001 (S4 hardening), so
    phones on the LAN can't reach it directly. The dashboard fetches the
    pairing token through this proxy -- which sits behind the same login
    as the rest of the app -- and renders the QR client-side.
    """
    ok, body = await _http_check("http://127.0.0.1:3001/qr", timeout=5)
    if not ok:
        return JSONResponse(
            {"error": "Cannot reach WhatsApp sidecar: " + body[:200]},
            status_code=502,
        )
    try:
        return json.loads(body)
    except Exception:
        return JSONResponse(
            {"error": "Sidecar returned non-JSON: " + body[:200]},
            status_code=502,
        )
