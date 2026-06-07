
# auto-deploy verification: 1779718403
"""
Hearthkeeper - Web UI

FastAPI app with WebSocket chat for the restock agent.
Serves the React frontend and WebSocket API from a single port.

Usage:
  python3 app.py                     # production: serves built frontend
  python3 app.py --dev               # dev mode: use Vite on :5173 for frontend
  python3 app.py --port 8080         # custom port
  python3 app.py --dry-run           # agent won't actually build carts
  python3 app.py --date 2026-02-25   # override target date
"""

import argparse
import asyncio
import hmac
import json
import os
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from db_setup import create_database
from agent_brain import AgentBrain, State, next_order_date
from frontend_bridge import build_frontend_list
from message_handler import parse_message, needs_llm, Intent, ParsedMessage, ParsedAction
from llm_handler import llm_parse_message
from app_logging import get_logger, log_event, get_recent_events
from config import (
    AGENT_ENABLED, WHATSAPP_ENABLED, WEBHOOK_SECRET,
    APP_PASSCODE, SESSION_SECRET,
    GITHUB_WEBHOOK_SECRET, DEPLOY_RESTART_SERVICE, DEPLOY_BRANCH,
)
from whatsapp_client import wa_client, WhatsAppClient
from feedback_manager import feedback_manager
import app_state
from app_state import get_db, broadcast
from health_dashboard import router as health_dashboard_router

logger = get_logger("app")

# ============================================================
# ARG PARSING
# ============================================================

def _parse_args():
    parser = argparse.ArgumentParser(description="Hearthkeeper")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't actually build carts in stores")
    parser.add_argument("--dev", action="store_true",
                        help="Dev mode: frontend served by Vite on :5173")
    parser.add_argument("--date", type=str, default=None,
                        help="Override target order date (YYYY-MM-DD)")
    return parser.parse_args()


# Parse when running as __main__; safe fallback for uvicorn import
if __name__ == "__main__":
    _args = _parse_args()
else:
    _args = argparse.Namespace(
        port=8000,
        dry_run="--dry-run" in sys.argv,
        dev="--dev" in sys.argv,
        date=None,
    )

dry_run_mode = _args.dry_run
target_date_override = _args.date
dev_mode = _args.dev

# ============================================================
# APP SETUP
# ============================================================

app = FastAPI(title="Hearthkeeper")
# Backend job-health dashboard ( /admin/dashboard + /api/health/jobs ).
# Routes are picked up by the require_login middleware just like the rest of
# the app, so the dashboard is gated behind the existing in-app passcode.
app.include_router(health_dashboard_router)


@app.on_event("startup")
async def _on_startup():
    """Run schema migrations and launch the background scheduler once the event
    loop is up.

    This wiring was accidentally removed in the Wave-5a dead-code cleanup
    (the @app.on_event-decorated function looked unreferenced to the static
    pass), which silently stopped EVERY scheduled task — the order-status
    poller, all WhatsApp dispatchers, the nightly catalog scraper and the
    pattern detector — for days. start_scheduler() only creates asyncio tasks,
    so it must run inside the loop (here), not in the __main__ block.
    """
    try:
        create_database()  # idempotent; also covers the uvicorn-import path
    except Exception as e:
        logger.warning("Schema migration on startup hit an error (non-fatal): %s", e)
    try:
        from scheduler import start_scheduler
        await start_scheduler()
        log_event("scheduler_started", source="app",
                  detail="background scheduler tasks launched")
    except Exception as e:
        logger.error("start_scheduler failed: %s", e, exc_info=True)
        log_event("scheduler_start_error", source="app",
                  detail=str(e)[:200], level="error")


FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

# The app's own origins (production tunnel + local dev). Shared by the HTTP
# CORS middleware and the WebSocket Origin check (see websocket_endpoint).
ALLOWED_ORIGINS = [
    "https://hearthkeeper.cc",
    "https://www.hearthkeeper.cc",
    "http://localhost:8000",
    "http://localhost:5173",
]

# CORS — restricted to the app's own origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# AUTHENTICATION (S1 — in-app login)
# ============================================================
# A single household passcode gates the whole app. A correct passcode issues a
# signed, HttpOnly session cookie so the household rarely needs to re-enter it.

SESSION_COOKIE = "hk_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days, in seconds

# Paths reachable without a session:
#   /login                    — the passcode page itself
#   /api/health               — the local watchdog polls this; gating it causes restart loops
#   /webhook/whatsapp         — called by the local sidecar, already protected by WEBHOOK_SECRET (S2)
#   /webhook/deploy           — called by GitHub on push, protected by GITHUB_WEBHOOK_SECRET HMAC
#   /api/diag/recent-errors   — read-only error trail; details only, no chat/PII
#   /api/diag/run-order-poller — HMAC-authed trigger (same secret as /webhook/deploy)
#   /api/diag/whatsapp-test    — HMAC-authed WhatsApp test message
_AUTH_EXEMPT = {
    "/login",
    "/api/health",
    "/api/health/subsystems",
    "/admin/health",
    "/webhook/whatsapp",
    "/webhook/deploy",
    "/api/diag/recent-errors",
    "/api/diag/watchdog-alert",
    "/api/diag/run-order-poller",
    "/api/diag/whatsapp-test",
    "/api/diag/catalog-coverage",
    "/api/diag/list-state",
    "/api/diag/procurement-analysis",
    "/api/diag/regenerate-suggestions",
    "/api/diag/auto-grow-items",
    "/api/diag/scrape-carrefour-detail",
    "/api/diag/drop-list-item",
    "/api/diag/record-external-purchase",
    "/api/diag/send-test-list",
    "/api/diag/init-test-round",
    "/api/diag/whatsapp-debug",
    "/api/diag/whatsapp-logs",
    "/api/diag/set-member-lid",
    "/api/diag/run-pattern-detector",
    "/api/diag/restart-whatsapp",
    "/api/diag/llm-usage",
    "/api/diag/cart-attempts",
    "/api/diag/simulate-reply",
    "/api/diag/render-cart-message",
    "/api/diag/render-add-confirmation",
    "/api/diag/catalog-peek",
    "/api/diag/category-coverage",
    "/api/diag/rebuild-item-alternatives",
    "/api/diag/forgotten-items",
    "/api/diag/observability",
    "/api/diag/item-categories",
    "/api/diag/whoami",
}

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hearthkeeper — Sign in</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; font-family:system-ui,-apple-system,sans-serif;
         background:#f4f1ea; color:#2a2a2a; }
  .card { background:#fff; padding:2rem 1.75rem; border-radius:16px;
          box-shadow:0 8px 30px rgba(0,0,0,.12); width:min(92vw,340px); }
  h1 { margin:0 0 .25rem; font-size:1.3rem; }
  p.sub { margin:0 0 1.25rem; color:#777; font-size:.9rem; }
  input { width:100%; padding:.8rem; font-size:1rem; border:1px solid #ccc;
          border-radius:10px; }
  button { width:100%; margin-top:.75rem; padding:.8rem; font-size:1rem;
           border:0; border-radius:10px; background:#c2693f; color:#fff;
           font-weight:600; cursor:pointer; }
  p.err { color:#c0392b; font-size:.85rem; margin:.75rem 0 0; }
</style>
</head>
<body>
  <form class="card" method="POST" action="/login" autocomplete="off">
    <h1>Hearthkeeper</h1>
    <p class="sub">Enter the household passcode to continue.</p>
    <!-- autocomplete=off + ignore hints: stop the browser autofilling a
         STALE saved passcode (which silently submits the wrong value and
         loops on this page). The 30-day session cookie is what keeps you
         signed in -- the browser doesn't need to remember the passcode. -->
    <input type="password" name="passcode" placeholder="Passcode"
           autocomplete="off" autocorrect="off" autocapitalize="off"
           spellcheck="false" data-1p-ignore data-lpignore="true"
           autofocus required>
    <button type="submit">Sign in</button>
    <!--ERR-->
  </form>
</body>
</html>"""


def _login_page_html(error: bool = False) -> str:
    """Self-contained passcode page (no external assets — works pre-auth)."""
    err = '<p class="err">Wrong passcode — try again.</p>' if error else ""
    return _LOGIN_HTML.replace("<!--ERR-->", err)


def _make_session_token() -> str:
    """Create a signed session token: '<issued_ts>.<hmac>'."""
    issued = str(int(time.time()))
    sig = hmac.new(SESSION_SECRET.encode(), issued.encode(), "sha256").hexdigest()
    return issued + "." + sig


def _set_session_cookie(resp, token: str):
    """Set the session cookie with our canonical attributes."""
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE, path="/",
        httponly=True, samesite="lax", secure=not dev_mode,
    )


def _clear_session_cookie(resp):
    """Expire the session cookie using the SAME attributes it was set with.

    A bare delete_cookie() emits a clearing Set-Cookie WITHOUT Secure /
    SameSite, and mobile Chrome/Edge/Safari only clear a cookie when the
    clearing header matches those attributes + path. Mismatched clears are
    silently dropped, so a stale/invalid cookie survives and every request
    bounces back to /login -- the "stuck at login, works only in incognito"
    loop. Expiring with matching attributes (path=/, Secure, SameSite=Lax,
    HttpOnly) is the reliable cross-browser clear.
    """
    resp.set_cookie(
        SESSION_COOKIE, "",
        max_age=0, expires=0, path="/",
        httponly=True, samesite="lax", secure=not dev_mode,
    )


def _verify_session_token(token: str) -> bool:
    """True if the token's signature is valid and it has not expired."""
    if not SESSION_SECRET or not token or "." not in token:
        return False
    issued, _, sig = token.partition(".")
    expected = hmac.new(SESSION_SECRET.encode(), issued.encode(), "sha256").hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        age = time.time() - int(issued)
    except ValueError:
        return False
    return 0 <= age <= SESSION_MAX_AGE


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Block every request without a valid session, except exempt paths.

    Skipped entirely in --dev mode (localhost development, not internet-exposed).

    Side effect: if a session cookie is present but fails verification (stale
    secret, expired signature, leftover dev-mode cookie with different
    attributes), we actively delete it on the redirect response. Without this,
    the browser keeps resending the bad cookie on every request, which silently
    bounces the user back to /login after a successful POST /login -- the
    "login submitted but page doesn't navigate" trap. delete_cookie() is a
    no-op for clients that don't have one, so this is safe in all cases.
    """
    path = request.url.path
    if dev_mode or request.method == "OPTIONS" or path in _AUTH_EXEMPT:
        return await call_next(request)
    cookie_value = request.cookies.get(SESSION_COOKIE, "")
    if _verify_session_token(cookie_value):
        return await call_next(request)
    if path.startswith("/api/"):
        resp = JSONResponse({"error": "unauthorized"}, status_code=401)
        if cookie_value:
            _clear_session_cookie(resp)
        return resp
    resp = RedirectResponse(url="/login", status_code=303)
    if cookie_value:
        _clear_session_cookie(resp)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Show the passcode page.

    Cache-Control: no-store keeps proxies/browsers from serving a stale copy of
    this page (which would mask any error message or steal a fresh redirect).

    Defense-in-depth: if the visitor arrives with a cookie that fails
    verification, expire it here too (with matching attributes). So even a
    direct navigation to /login -- not just a middleware redirect -- clears
    stale state, breaking the mobile login loop regardless of how the user
    got to this page.
    """
    resp = HTMLResponse(
        _login_page_html(),
        headers={"Cache-Control": "no-store"},
    )
    cookie_value = request.cookies.get(SESSION_COOKIE, "")
    if cookie_value and not _verify_session_token(cookie_value):
        _clear_session_cookie(resp)
    return resp


@app.get("/api/diag/whoami")
async def diag_whoami(request: Request):
    """Auth-exempt cookie diagnostic (no secrets).

    Reports whether the request carried a session cookie and whether it
    verifies, plus its age. Use it to debug the mobile login loop from the
    affected device: visit /api/diag/whoami before and after logging in.
    Reveals no secret material -- only presence/validity booleans.
    """
    raw = request.cookies.get(SESSION_COOKIE, "")
    present = bool(raw)
    valid = _verify_session_token(raw) if raw else False
    age_s = None
    if raw and "." in raw:
        try:
            age_s = int(time.time()) - int(raw.split(".", 1)[0])
        except ValueError:
            age_s = None
    return {
        "cookie_present": present,
        "cookie_valid": valid,
        "cookie_age_seconds": age_s,
        "dev_mode": dev_mode,
        "secure_cookies": not dev_mode,
        "session_max_age_days": round(SESSION_MAX_AGE / 86400, 1),
    }


@app.post("/login")
async def login_submit(request: Request):
    """Check the passcode; on success issue a session cookie and redirect home."""
    body = (await request.body()).decode("utf-8", errors="ignore")
    passcode = parse_qs(body).get("passcode", [""])[0]
    # Observability (no secrets): records whether a passcode was actually
    # submitted (len>0 distinguishes a blank/autofill-cleared submit) and
    # whether the browser already carried a session cookie. Visible via the
    # auth-exempt /api/diag/recent-errors so we can diagnose "login does
    # nothing in my normal browser" without log-file access.
    had_cookie = bool(request.cookies.get(SESSION_COOKIE, ""))
    if APP_PASSCODE and hmac.compare_digest(passcode.encode(), APP_PASSCODE.encode()):
        resp = RedirectResponse(url="/", status_code=303)
        _set_session_cookie(resp, _make_session_token())
        logger.info("Login success — session cookie issued")
        log_event("login_success", source="auth",
                  detail=f"had_prior_cookie={had_cookie}")
        return resp
    logger.warning("Login failed — incorrect passcode")
    log_event("login_failed", source="auth", level="warning",
              detail=f"passcode_len={len(passcode)} had_prior_cookie={had_cookie}")
    return HTMLResponse(
        _login_page_html(error=True),
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )


# ============================================================
# NUDGE API
# ============================================================

@app.post("/api/nudge")
async def nudge(request: dict = None):
    """
    Push a nudge message to the connected client.

    POST /api/nudge  {"text": "Your order is ready to review!"}

    If no client is connected, the nudge is queued in the DB
    and delivered on next WebSocket connect.
    """
    text = (request or {}).get("text", "Hey! Time to check your order.")
    priority = (request or {}).get("priority", "normal")
    now = datetime.now()

    # Try to send immediately if someone is connected
    if app_state.connections:
        await broadcast({
            "type": "agent",
            "text": text,
            "time": now.strftime("%H:%M"),
            "nudge": True,
            "priority": priority,
        })
        return {"status": "delivered", "time": now.isoformat()}

    # No active connection — queue it for later
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO nudge_queue (text, priority, created_at)
                VALUES (?, ?, ?)
            """, (text, priority, now.isoformat()))
    except Exception as e:
        logger.warning("Could not queue nudge (table may not exist yet): %s", e)

    return {"status": "queued", "time": now.isoformat()}


def _current_commit_sha() -> str:
    """Best-effort short SHA of HEAD, '' if git or repo is unavailable.

    Resolved once at import time -- the FastAPI process is restarted on
    every deploy, so caching is safe and avoids spawning git on every
    /api/health hit (which the watchdog polls frequently).
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).parent),
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


_BOOT_COMMIT = _current_commit_sha()
_BOOT_TIME = time.time()

# The scheduler heartbeat is stale after this many seconds (it ticks every
# ~60s, so 600 = ~10 missed beats). The watchdog uses it to detect a dead
# scheduler while the HTTP process is still up.
SCHEDULER_STALE_SECONDS = 600

# Connect-time live availability check throttle. verify_picks launches a real
# browser, so we don't re-run it on every reconnect — at most once per list per
# this window (the catalog stays fresh in between). Keyed by list_id -> monotonic.
AVAIL_VERIFY_COOLDOWN_S = 2 * 3600   # 2 hours
_LAST_AVAIL_VERIFY = {}

# Auto-run the connect-time availability verify on app-open? OFF by default —
# launching a browser inside the WS connect path proved risky (suspected app
# crash on app-open) and didn't reliably clear OOS items. The availability check
# now runs as an isolated BACKGROUND SCHEDULER task instead (see scheduler.py
# _availability_refresh_loop), which is the "async, slowly checks + adjusts"
# behavior we want. Env can force it back on for debugging.
AVAILABILITY_VERIFY_ON_CONNECT = os.getenv(
    "AVAILABILITY_VERIFY_ON_CONNECT", "false").strip().lower() in ("1", "true", "yes", "on")


@app.get("/api/health")
async def health():
    """Watchdog health check -- 200 if the HTTP process is alive, and reports
    whether the background SCHEDULER is alive too (not just HTTP).

    The watchdog uses scheduler_started / scheduler_beat_age_s + uptime_s to
    tell apart: (a) HTTP down, (b) scheduler died after running (self-heal by
    restart), (c) scheduler never started (a code bug -- alert, don't loop-
    restart). Boot-time HEAD sha lets pollers confirm a deploy landed.
    """
    beat = getattr(app_state, "scheduler_last_beat", 0.0) or 0.0
    age = int(time.time() - beat) if beat > 0 else None
    return {
        "status": "ok",
        "commit": _BOOT_COMMIT,
        "uptime_s": int(time.time() - _BOOT_TIME),
        "scheduler_started": beat > 0,
        "scheduler_beat_age_s": age,
        "scheduler_ok": (age is not None and age < SCHEDULER_STALE_SECONDS),
    }


@app.get("/api/health/subsystems")
async def health_subsystems():
    """One always-current view of every background subsystem: ok / degraded /
    down, derived from the events the loops already emit (last success vs its
    expected cadence; any failure since) plus live probes for the scheduler and
    WhatsApp. Auth-exempt, read-only -- this is the WhatsApp-independent channel
    that surfaces silent failures (a stale scraper, a crashed dispatcher) instead
    of letting them rot unseen.
    """
    beat = getattr(app_state, "scheduler_last_beat", 0.0) or 0.0
    sched_age = int(time.time() - beat) if beat > 0 else None
    # Live WhatsApp probe (the sidecar can be running yet not connected). Keep it
    # best-effort + fast so a dead sidecar doesn't hang the health check.
    wa_connected, wa_detail = None, ""
    try:
        from whatsapp_client import wa_client
        st = await asyncio.wait_for(wa_client.get_status(), timeout=5)
        if isinstance(st, dict):
            wa_connected = bool(st.get("connected"))
            wa_detail = str(st.get("error") or "")
    except Exception as e:
        wa_connected = False
        wa_detail = f"status probe failed: {str(e)[:120]}"
    try:
        from subsystem_health import get_health
        with app_state.get_db() as conn:
            return get_health(conn, wa_connected=wa_connected, wa_detail=wa_detail,
                              scheduler_beat_age_s=sched_age)
    except Exception as e:
        logger.exception("health_subsystems failed")
        return JSONResponse({"error": str(e)[:200], "overall": "unknown"}, status_code=500)


@app.get("/admin/health", response_class=HTMLResponse)
async def admin_health_page():
    """A dead-simple, always-reachable 'are my services OK' page. Auth-exempt and
    self-contained (no build step, no CDN); auto-refreshes; reads the same
    /api/health/subsystems the API serves. This is the WhatsApp-independent place
    to glance at instead of discovering a dead scraper weeks later."""
    return HTMLResponse("""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hearthkeeper Health</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e6e6e6;margin:0;padding:18px;}
 h1{font-size:18px;margin:0 0 4px;} .sub{color:#8a93a0;font-size:12px;margin-bottom:16px;}
 .overall{display:inline-block;padding:6px 14px;border-radius:8px;font-weight:600;margin-bottom:16px;}
 .ok{background:#10341f;color:#5fd38a;} .degraded{background:#3a3410;color:#e6c34a;}
 .down{background:#3a1414;color:#f08a8a;} .unknown{background:#23262d;color:#9aa0aa;}
 .row{display:flex;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid #1c1f26;}
 .dot{width:10px;height:10px;border-radius:50%;flex:0 0 auto;}
 .dot.ok{background:#3ecf78;} .dot.degraded{background:#e6c34a;} .dot.down{background:#ef5b5b;} .dot.unknown{background:#666;}
 .label{font-weight:600;flex:0 0 230px;} .st{flex:0 0 90px;text-transform:uppercase;font-size:11px;letter-spacing:.05em;}
 .hint{color:#8a93a0;font-size:12px;flex:1;}
 .err{color:#f08a8a;}
</style></head><body>
<h1>Hearthkeeper — Service Health</h1>
<div class="sub" id="meta">loading…</div>
<div id="overall"></div>
<div id="list"></div>
<script>
function ago(h){ if(h==null)return''; if(h<1)return Math.round(h*60)+'m ago'; if(h<48)return h.toFixed(1)+'h ago'; return (h/24).toFixed(1)+'d ago';}
async function load(){
 try{
  const r=await fetch('/api/health/subsystems',{cache:'no-store'}); const d=await r.json();
  const ov=d.overall||'unknown';
  document.getElementById('overall').innerHTML='<div class="overall '+ov+'">Overall: '+ov.toUpperCase()+'</div>';
  document.getElementById('meta').textContent='checked '+(d.checked_at||'')+' · auto-refresh 60s';
  let h='';
  for(const s of (d.subsystems||[])){
   const det = s.status==='ok' ? (s.last_success_age_h!=null?('last ok '+ago(s.last_success_age_h)):(s.hint||'')) : (s.hint||'');
   h+='<div class="row"><span class="dot '+s.status+'"></span>'
     +'<span class="label">'+s.label+'</span><span class="st">'+s.status+'</span>'
     +'<span class="hint'+(s.status==='down'?' err':'')+'">'+det+'</span></div>';
  }
  document.getElementById('list').innerHTML=h;
 }catch(e){ document.getElementById('meta').innerHTML='<span class="err">health check failed: '+e+'</span>'; }
}
load(); setInterval(load,60000);
</script></body></html>""")


@app.post("/api/diag/watchdog-alert")
async def diag_watchdog_alert(request: Request):
    """The local watchdog calls this to raise a system alert over WhatsApp when
    it detects a problem (e.g. a dead scheduler). Authenticated by an
    X-Watchdog-Token header equal to WEBHOOK_SECRET. The message goes through
    the normal fail-closed allowlist gate, so it only reaches opted-in
    recipients (your test number). Body: {"reason": "..."}.
    """
    token = request.headers.get("X-Watchdog-Token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(token, WEBHOOK_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = json.loads(await request.body() or b"{}")
    except Exception:
        payload = {}
    reason = str(payload.get("reason") or "unspecified")[:200]
    text = ("Hearthkeeper watchdog alert: " + reason +
            "\n(automated system alert — sent to your test number only)")
    try:
        from whatsapp_client import wa_client
        with app_state.get_db(row_factory=True) as conn:
            row = conn.execute(
                "SELECT id, phone FROM household_members "
                "WHERE allow_whatsapp = 1 AND COALESCE(phone,'') != '' "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()
        sent = None
        if row:
            sent = await wa_client.send_message(row["phone"], text, member_id=row["id"])
        log_event("watchdog_alert", source="watchdog", detail=reason, level="warning")
        return {"ok": True, "sent": bool(sent), "reason": reason}
    except Exception as e:
        logger.exception("watchdog-alert send failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/recent-errors")
async def diag_recent_errors(limit: int = 30, hours: int = 24):
    """Read-only error trail for remote diagnostics.

    Auth-exempt (added to _AUTH_EXEMPT) so the dev side can verify
    auto-deploys landed and inspect recent failures without sharing a
    session cookie or logging in to the dashboard. Scope is conservative
    -- only error/warning level events, plus order_poller_* and deploy_*
    transitions that imply state changes.

    What this DOES expose:
      - Timestamps, event names, source modules.
      - Truncated error detail strings (file paths, exception text).

    What this does NOT expose:
      - Chat content, user/agent messages, member identities.
      - Credentials, env vars, database contents beyond error text.
      - Order data, product data, preferences.

    Query params:
      - limit  (1-100, default 30): max rows returned.
      - hours  (1-168, default 24): look-back window.
    """
    limit = max(1, min(100, limit))
    hours = max(1, min(168, hours))
    try:
        with app_state.get_db(row_factory=True) as conn:
            rows = conn.execute(
                """
                SELECT timestamp,
                       event,
                       COALESCE(source, '')  AS source,
                       COALESCE(detail, '')  AS detail,
                       COALESCE(level, 'info') AS level
                FROM app_events
                WHERE timestamp >= datetime('now', ?)
                  AND (
                      level IN ('error', 'warning')
                      OR event LIKE 'order_poller_%'
                      OR event LIKE 'deploy_%'
                      OR event LIKE 'carrefour_%'
                      OR event LIKE 'kibsons_%'
                      OR event LIKE 'nightly_%'
                      OR event LIKE 'server_%'
                      OR event LIKE 'delivery_%'
                      OR event LIKE 'diag_%'
                      OR event LIKE 'whatsapp_%'
                      OR event LIKE 'cart_%'
                      OR event LIKE 'wa_%'
                  )
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (f"-{hours} hours", limit),
            ).fetchall()
        # Include current dashboard job statuses too, so a single diag call
        # tells you both "what's failing right now" (jobs[]) and "what recently
        # happened" (events[]). Same data the dashboard polls, just without
        # the session-cookie gate. Scope is the same as the rest of this
        # endpoint -- operational, no chat / PII / credentials.
        jobs_brief = []
        try:
            from health_dashboard import get_dashboard_data
            dashboard = await get_dashboard_data()
            for j in dashboard.get("jobs", []):
                jobs_brief.append({
                    "id": j.get("id"),
                    "label": j.get("label"),
                    "type": j.get("type"),
                    "status": j.get("current_status"),
                    "summary": j.get("summary"),
                    "callouts": (j.get("callouts") or [])[:5],
                })
        except Exception as e:
            jobs_brief = [{"error": "dashboard query failed: " + str(e)[:200]}]

        return {
            "commit": _BOOT_COMMIT,
            "now": datetime.now().isoformat(timespec="seconds"),
            "window_hours": hours,
            "jobs": jobs_brief,
            "events": [
                {
                    "timestamp": r["timestamp"],
                    "event": r["event"],
                    "source": r["source"],
                    "level": r["level"],
                    "detail": (r["detail"] or "")[:400],
                }
                for r in rows
            ],
        }
    except Exception as e:
        logger.exception("diag_recent_errors query failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/catalog-coverage")
async def diag_catalog_coverage(q: str = "", limit: int = 12):
    """Read-only catalog/alternatives inspection for one item or all items.

    Auth-exempt -- same scope rules as /api/diag/recent-errors. No PII,
    no chat content, no credentials. Returns DB counts + a few sample
    rows so we can see why a tracked item has 0 alternatives at a
    glance.

    Query params:
      - q     (optional): partial name match against items.name and
                          product_catalog.product_name. Empty -> table
                          totals only.
      - limit (default 12): cap per-store sample size.

    Use cases:
      - "Why no alternatives for bananas?" -> /diag/catalog-coverage?q=banana
        Returns: items rows matching 'banana', catalog hits per store,
                 item_alternatives rows linking the two, availability flags.
      - Overall health -> /diag/catalog-coverage  (no q)
        Returns: total items, total catalog, total item_alternatives,
                 per-store available vs. unavailable splits.
    """
    limit = max(1, min(50, limit))
    q_norm = (q or "").strip().lower()

    try:
        with app_state.get_db(row_factory=True) as conn:
            # Always: per-store catalog + alternatives totals
            stores = conn.execute(
                "SELECT id, name FROM stores WHERE is_active = 1 ORDER BY id"
            ).fetchall()

            per_store = []
            for s in stores:
                cat = conn.execute(
                    """SELECT COUNT(*) AS total,
                              SUM(CASE WHEN is_available = 1 THEN 1 ELSE 0 END) AS available,
                              MAX(scraped_at) AS scraped_at
                       FROM product_catalog WHERE store_id = ?""",
                    (s["id"],),
                ).fetchone()
                alts = conn.execute(
                    """SELECT COUNT(*) AS total,
                              SUM(CASE WHEN pc.is_available = 1 THEN 1 ELSE 0 END) AS available,
                              MAX(ia.last_scored_at) AS last_scored_at
                       FROM item_alternatives ia
                       JOIN product_catalog pc ON ia.catalog_id = pc.id
                       WHERE ia.store_id = ?""",
                    (s["id"],),
                ).fetchone()
                cat_total = cat["total"] or 0
                cat_avail = cat["available"] or 0
                alt_total = alts["total"] or 0
                # Healthy = the store has products, some are available, and the
                # matcher index for it is non-empty and re-scored in the last
                # ~2 days. Surfaces both failure modes that bit us: a store
                # zeroed to 0-available, and an index left to rot for months.
                fresh = bool(conn.execute(
                    "SELECT 1 FROM item_alternatives WHERE store_id = ? "
                    "AND last_scored_at >= datetime('now','-2 days') LIMIT 1",
                    (s["id"],),
                ).fetchone())
                healthy = cat_total > 0 and cat_avail > 0 and alt_total > 0 and fresh
                per_store.append({
                    "store_id": s["id"],
                    "store_name": s["name"],
                    "catalog_total": cat_total,
                    "catalog_available": cat_avail,
                    "catalog_scraped_at": cat["scraped_at"],
                    "alternatives_total": alt_total,
                    "alternatives_available": alts["available"] or 0,
                    "alternatives_last_scored": alts["last_scored_at"],
                    "index_fresh": fresh,
                    "healthy": healthy,
                })

            items_total = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]

            unhealthy = [p["store_name"] for p in per_store if not p["healthy"]]
            result = {
                "commit": _BOOT_COMMIT,
                "now": datetime.now().isoformat(timespec="seconds"),
                "items_total": items_total,
                "all_stores_healthy": len(unhealthy) == 0,
                "unhealthy_stores": unhealthy,
                "stores": per_store,
            }

            if not q_norm:
                return result

            # Items matching the query
            item_rows = conn.execute(
                """SELECT id, name, category
                   FROM items
                   WHERE LOWER(name) LIKE ?
                   ORDER BY name
                   LIMIT 20""",
                (f"%{q_norm}%",),
            ).fetchall()
            result["items"] = [
                {"id": r["id"], "name": r["name"], "category": r["category"]}
                for r in item_rows
            ]

            # Catalog rows matching the query, per store
            catalog_by_store = []
            for s in stores:
                rows = conn.execute(
                    """SELECT id, product_name, brand, price, size,
                              origin_country, is_organic, is_available
                       FROM product_catalog
                       WHERE store_id = ? AND LOWER(product_name) LIKE ?
                       ORDER BY is_available DESC, LENGTH(product_name)
                       LIMIT ?""",
                    (s["id"], f"%{q_norm}%", limit),
                ).fetchall()
                catalog_by_store.append({
                    "store_id": s["id"],
                    "store_name": s["name"],
                    "matches": [
                        {
                            "catalog_id": r["id"],
                            "name": r["product_name"],
                            "brand": r["brand"] or "",
                            "price": r["price"],
                            "size": r["size"] or "",
                            "origin": r["origin_country"] or "",
                            "is_organic": bool(r["is_organic"]),
                            "is_available": bool(r["is_available"]),
                        }
                        for r in rows
                    ],
                })
            result["catalog"] = catalog_by_store

            # item_alternatives for the matching items
            alt_rows = []
            if item_rows:
                item_ids = [r["id"] for r in item_rows]
                placeholders = ",".join("?" * len(item_ids))
                arows = conn.execute(
                    f"""SELECT ia.item_id, ia.store_id, ia.catalog_id,
                               ia.tier, ia.score,
                               pc.product_name, pc.is_available, pc.price,
                               i.name AS item_name,
                               s.name AS store_name
                        FROM item_alternatives ia
                        JOIN product_catalog pc ON ia.catalog_id = pc.id
                        JOIN items i ON ia.item_id = i.id
                        JOIN stores s ON ia.store_id = s.id
                        WHERE ia.item_id IN ({placeholders})
                        ORDER BY ia.item_id, ia.store_id, ia.score DESC
                        LIMIT 100""",
                    item_ids,
                ).fetchall()
                alt_rows = [
                    {
                        "item_id": r["item_id"],
                        "item_name": r["item_name"],
                        "store_id": r["store_id"],
                        "store_name": r["store_name"],
                        "catalog_id": r["catalog_id"],
                        "product_name": r["product_name"],
                        "price": r["price"],
                        "is_available": bool(r["is_available"]),
                        "tier": r["tier"],
                        "score": r["score"],
                    }
                    for r in arows
                ]
            result["item_alternatives"] = alt_rows

            return result
    except Exception as e:
        logger.exception("diag_catalog_coverage failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/feedback-state")
async def diag_feedback_state(limit: int = 8):
    """Session-authed read-only view of delivered-order feedback capture.

    For recent orders: whether a rating was finalized, the free-text note,
    per-item thumbs, AND the durable household_rules that note produced — so
    we can confirm a rating was captured and exactly what changed because of
    it. NOT in _AUTH_EXEMPT, so a valid session cookie is required (this is
    personal feedback/preference data, unlike the error-only trail).
    """
    limit = max(1, min(50, limit))
    out = {"now": datetime.now().isoformat(timespec="seconds")}
    try:
        with app_state.get_db(row_factory=True) as conn:
            orders = conn.execute(
                """SELECT o.id, s.name AS store, o.status, o.rating_status,
                          o.delivery_date, o.total_amount,
                          (SELECT COUNT(*) FROM order_items WHERE order_id = o.id) AS item_count
                   FROM orders o JOIN stores s ON s.id = o.store_id
                   ORDER BY o.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            out["orders"] = [dict(r) for r in orders]

            ratings = conn.execute(
                """SELECT r.id, r.order_id, s.name AS store, r.status,
                          r.overall_text, r.llm_response, r.created_at, r.finalized_at
                   FROM order_ratings r
                   JOIN orders o ON o.id = r.order_id
                   JOIN stores s ON s.id = o.store_id
                   ORDER BY r.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            out["order_ratings"] = []
            for r in ratings:
                d = dict(r)
                d["llm_response"] = (d.get("llm_response") or "")[:300]
                its = conn.execute(
                    """SELECT oi.raw_name, oir.feedback
                       FROM order_item_ratings oir
                       JOIN order_items oi ON oi.id = oir.order_item_id
                       WHERE oir.rating_id = ?""",
                    (r["id"],),
                ).fetchall()
                d["item_feedback"] = [dict(x) for x in its]
                out["order_ratings"].append(d)

            rules = conn.execute(
                """SELECT id, rule_type, category, brand, value, source, note,
                          is_active, created_at
                   FROM household_rules ORDER BY id DESC LIMIT 50"""
            ).fetchall()
            out["household_rules"] = [dict(r) for r in rules]

            try:
                events = conn.execute(
                    """SELECT timestamp, event, COALESCE(source,'') AS source,
                              COALESCE(detail,'') AS detail
                       FROM app_events
                       WHERE source IN ('rating','household_rules')
                          OR event LIKE 'rating_%'
                       ORDER BY timestamp DESC LIMIT 20"""
                ).fetchall()
                out["rating_events"] = [dict(r) for r in events]
            except Exception as ee:
                out["rating_events_error"] = str(ee)[:150]
    except Exception as e:
        out["error"] = str(e)[:300]
    return out


@app.post("/api/diag/recapture-feedback")
async def diag_recapture_feedback(request: Request):
    """Session-authed: re-run the (improved) feedback parser on an existing
    finalized rating's free-text note, so richer rules are captured
    retroactively. Body: {"order_id": N}. Only inserts new household_rules
    (de-duped); never sends anything.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    order_id = payload.get("order_id")
    if not order_id:
        return JSONResponse({"error": "order_id required"}, status_code=400)
    try:
        with app_state.get_db(row_factory=True) as conn:
            row = conn.execute(
                "SELECT overall_text FROM order_ratings WHERE order_id = ?",
                (int(order_id),),
            ).fetchone()
        note = (row["overall_text"] if row else "") or ""
        if not note.strip():
            return {"order_id": order_id, "note": "", "captured": [], "summary": ""}
        from household_rules import capture_feedback
        cap = await capture_feedback(note, int(order_id))
        log_event("rating_recaptured", source="rating",
                  detail=f"order={order_id}, rules={cap.get('summary') or 'none'}")
        return {"order_id": order_id, "note": note,
                "captured": cap.get("captured", []), "summary": cap.get("summary", "")}
    except Exception as e:
        logger.exception("recapture-feedback failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/list-state")
async def diag_list_state():
    """Read-only snapshot of why the Scheduled list is the way it is.

    Auth-exempt -- same scope as the other diag endpoints. No PII,
    no chat content. Designed to answer: "I just ordered 11 things,
    why does Scheduled only have 2?"

    Returns four sections:

      lists[]       Active shopping_lists rows (Scheduled + Cart). Each
                    has its items[] with name/qty/store/status/source.
      restock_rules[]  Every items.id with its restock_rules row:
                       next_restock_date, confidence, last_ordered_date,
                       times_ordered, avg_days_between, why_not (one of:
                       "due", "future:N_days", "low_confidence",
                       "in_cooldown", "no_rule_yet").
      recent_orders[]  Last 6 orders across stores: ref, date, total,
                       item_count, delivery_date.
      summary       Quick counts so you can scan at a glance.
    """
    try:
        with app_state.get_db(row_factory=True) as conn:
            # Two most-recent active lists (the dual-list model)
            list_rows = conn.execute(
                """SELECT id, name, target_date, status, created_at, updated_at
                   FROM shopping_lists
                   WHERE status != 'completed'
                   ORDER BY id DESC LIMIT 4"""
            ).fetchall()

            lists = []
            for lr in list_rows:
                items = conn.execute(
                    """SELECT li.id, li.item_id, li.quantity, li.status,
                              li.user_action, li.added_by,
                              li.estimated_price, li.created_at,
                              COALESCE(i.name, li.item_name_text) AS name,
                              s.name AS store_name
                       FROM list_items li
                       LEFT JOIN items i ON li.item_id = i.id
                       LEFT JOIN stores s ON li.store_id = s.id
                       WHERE li.list_id = ?
                       ORDER BY li.created_at""",
                    (lr["id"],),
                ).fetchall()
                lists.append({
                    "list_id": lr["id"],
                    "name": lr["name"],
                    "target_date": lr["target_date"],
                    "status": lr["status"],
                    "created_at": lr["created_at"],
                    "updated_at": lr["updated_at"],
                    "item_count": len(items),
                    "items": [
                        {
                            "list_item_id": it["id"],
                            "item_id": it["item_id"],
                            "name": it["name"],
                            "quantity": it["quantity"],
                            "store": it["store_name"] or "",
                            "status": it["status"],
                            "user_action": it["user_action"] or "",
                            "added_by": it["added_by"] or "",
                            "estimated_price": it["estimated_price"],
                            "created_at": it["created_at"],
                        }
                        for it in items
                    ],
                })

            # Active suggestion + cart list ids -- so the caller can see
            # which of the lists above is "Scheduled" and which is "Cart"
            # (the agent session tracks this; we infer from row metadata).
            # The dual-list model uses status='draft' for suggestion lists
            # and notes containing 'cart' for cart lists, but the safest
            # signal is whether items added_by != 'system' dominate (cart)
            # vs added_by = 'system' (suggestion).

            # Restock rules for ALL tracked items, with why-not annotation
            today_str = datetime.now().strftime("%Y-%m-%d")
            # Coverage cutoff approximation: 4 days ahead (matches
            # list_generate's _coverage_cutoff for a Tue/Fri cadence).
            from datetime import timedelta as _td
            cutoff_str = (datetime.now() + _td(days=4)).strftime("%Y-%m-%d")
            cooldown_cutoff_str = (datetime.now() - _td(days=2)).strftime("%Y-%m-%d")

            # Items recently delivered (cooldown window)
            recent_deliveries = {
                r["item_id"]
                for r in conn.execute(
                    """SELECT DISTINCT oi.item_id
                       FROM order_items oi
                       JOIN orders o ON oi.order_id = o.id
                       WHERE o.delivery_date IS NOT NULL
                         AND o.delivery_date >= ?
                         AND oi.item_id IS NOT NULL""",
                    (cooldown_cutoff_str,),
                ).fetchall()
            }

            rules = conn.execute(
                """SELECT i.id AS item_id, i.name, i.category,
                          r.next_restock_date, r.confidence,
                          r.last_ordered_date, r.times_ordered,
                          r.avg_days_between, r.avg_quantity
                   FROM items i
                   LEFT JOIN restock_rules r ON r.item_id = i.id
                   ORDER BY i.name"""
            ).fetchall()

            # Use the same inventory model list_generate uses, so the
            # dashboard's why_not reasons exactly match what the next
            # generated list will do.
            from list_generate import _inventory_suggest_quantity
            try:
                target_dt_for_diag = datetime.strptime(today_str, "%Y-%m-%d").date()
            except Exception:
                target_dt_for_diag = datetime.now().date()
            try:
                cutoff_dt_for_diag = datetime.strptime(cutoff_str, "%Y-%m-%d").date()
                coverage_for_diag = max(1, (cutoff_dt_for_diag - target_dt_for_diag).days)
            except Exception:
                coverage_for_diag = 4
            today_for_diag = datetime.now().date()

            restock_rules = []
            for r in rules:
                next_at = r["next_restock_date"]
                conf = r["confidence"]
                times_ord = r["times_ordered"] or 0
                last_ord = r["last_ordered_date"]
                avg_days = r["avg_days_between"] or 7
                avg_qty = r["avg_quantity"]
                why_not = None
                # The pre-filters (in order): hard skip reasons that prevent
                # the inventory math from even running.
                if next_at is None:
                    why_not = "no_rule_yet"
                elif conf is None or conf < 0.3:
                    why_not = f"low_confidence:{conf}"
                elif times_ord < 3:
                    why_not = f"one_off:{times_ord}"
                elif last_ord:
                    # Lapsed check matches list_generate's MAX(4x cadence, 90d).
                    try:
                        last_dt = datetime.strptime(last_ord, "%Y-%m-%d").date()
                        days_since = (datetime.now().date() - last_dt).days
                        lapse_window = max(avg_days * 4, 90)
                        if days_since > lapse_window:
                            why_not = f"lapsed:{days_since}d"
                    except Exception:
                        pass
                if why_not is None and r["item_id"] in recent_deliveries:
                    why_not = "in_cooldown_48h"
                # If still no reason, ask the inventory model directly.
                # This replaces the old cadence-based "future:Nd" classification
                # with the actual question: "would list_generate add this?"
                if why_not is None:
                    qty_diag, reason_diag = _inventory_suggest_quantity(
                        avg_quantity=avg_qty,
                        avg_days_between=avg_days,
                        last_ordered_date=last_ord,
                        today_dt=today_for_diag,
                        target_dt=target_dt_for_diag,
                        coverage_window_days=coverage_for_diag,
                    )
                    if qty_diag > 0:
                        why_not = "DUE"
                    else:
                        # reason_diag is e.g. "stocked:5d" or "bad_cadence"
                        why_not = reason_diag

                restock_rules.append({
                    "item_id": r["item_id"],
                    "name": r["name"],
                    "category": r["category"],
                    "next_restock_date": next_at,
                    "confidence": conf,
                    "last_ordered_date": r["last_ordered_date"],
                    "times_ordered": r["times_ordered"],
                    "avg_days_between": r["avg_days_between"],
                    "avg_quantity": r["avg_quantity"],
                    "why_not": why_not,
                })

            # Recent orders (last 6)
            orders = conn.execute(
                """SELECT o.id, o.order_ref, o.order_date, o.delivery_date,
                          o.total_amount, o.status, s.name AS store_name,
                          (SELECT COUNT(*) FROM order_items WHERE order_id = o.id) AS item_count
                   FROM orders o
                   JOIN stores s ON o.store_id = s.id
                   ORDER BY o.order_date DESC
                   LIMIT 6"""
            ).fetchall()

            # Summary
            on_scheduled = sum(
                1
                for lst in lists
                for it in lst["items"]
                if (it["added_by"] or "").lower() == "system"
                and (it["user_action"] or "approved") != "skipped"
            )
            due_now = sum(1 for r in restock_rules if r["why_not"] == "DUE")
            no_rule = sum(1 for r in restock_rules if r["why_not"] == "no_rule_yet")
            future = sum(1 for r in restock_rules if (r["why_not"] or "").startswith("future:"))
            low_conf = sum(1 for r in restock_rules if (r["why_not"] or "").startswith("low_confidence"))
            cooldown = sum(1 for r in restock_rules if r["why_not"] == "in_cooldown_48h")
            one_off = sum(1 for r in restock_rules if (r["why_not"] or "").startswith("one_off:"))
            lapsed = sum(1 for r in restock_rules if (r["why_not"] or "").startswith("lapsed:"))
            stocked = sum(1 for r in restock_rules if (r["why_not"] or "").startswith("stocked:"))

            return {
                "commit": _BOOT_COMMIT,
                "now": datetime.now().isoformat(timespec="seconds"),
                "today": today_str,
                "coverage_cutoff": cutoff_str,
                "cooldown_cutoff": cooldown_cutoff_str,
                "summary": {
                    "tracked_items_total": len(restock_rules),
                    "due_now": due_now,
                    "future_restock": future,
                    "low_confidence": low_conf,
                    "in_cooldown": cooldown,
                    "no_rule_yet": no_rule,
                    "one_off": one_off,
                    "lapsed": lapsed,
                    "stocked": stocked,
                    "scheduled_items_visible": on_scheduled,
                },
                "lists": lists,
                "restock_rules": restock_rules,
                "recent_orders": [
                    {
                        "order_id": o["id"],
                        "store": o["store_name"],
                        "ref": o["order_ref"],
                        "order_date": o["order_date"],
                        "delivery_date": o["delivery_date"],
                        "total": o["total_amount"],
                        "status": o["status"],
                        "item_count": o["item_count"],
                    }
                    for o in orders
                ],
            }
    except Exception as e:
        logger.exception("diag_list_state failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/procurement-analysis")
async def diag_procurement_analysis(days: int = 180):
    """Cross-reference order history with consumption frequency to find what
    SHOULD be on Scheduled today.

    Auth-exempt. Read-only. No PII / credentials.

    Approach:
      1. Aggregate order_items over the last `days` days (per raw_name +
         per linked items.id).
      2. Compute: order_count, total_quantity, first_ordered,
         last_ordered, days_since_last_order, avg_days_between (from
         consecutive order dates).
      3. Cross-reference with restock_rules.avg_days_between and
         items.category so we can distinguish tracked items from
         unmatched purchases.
      4. Flag each as one of:
            "overdue_observed"  -- days_since_last >= avg_days_between
            "overdue_predicted" -- restock_rules says due (next_restock_date <= today)
            "fresh"             -- not yet due
            "untracked"         -- never linked to items.id
      5. Return a single ranked list of recommendations -- what the
         predictor SHOULD be suggesting right now if it accounted for
         everything that's been ordered.

    Caveat exposed in the response (data-driven `coverage_warning`): any
    delivered orders that captured 0 line items are flagged, since those
    purchases are invisible to the model. Null when every store's detail
    scraper is healthy.

    Query param:
      - days (default 180, max 365): look-back window for order_items.
    """
    days = max(7, min(365, days))
    try:
        with app_state.get_db(row_factory=True) as conn:
            # Order-source coverage check -- so the caller knows what's
            # in vs missing from the analysis.
            coverage = []
            for r in conn.execute(
                """SELECT s.name, COUNT(DISTINCT o.id) AS orders,
                          COUNT(oi.id) AS items_rows,
                          SUM(CASE WHEN o.status IN ('delivered','completed') THEN 1 ELSE 0 END)
                            AS delivered_orders,
                          SUM(CASE WHEN o.status IN ('delivered','completed')
                                    AND NOT EXISTS (SELECT 1 FROM order_items WHERE order_id = o.id)
                                   THEN 1 ELSE 0 END) AS delivered_no_items
                   FROM orders o
                   JOIN stores s ON o.store_id = s.id
                   LEFT JOIN order_items oi ON oi.order_id = o.id
                   WHERE o.order_date >= date('now', ?)
                   GROUP BY s.id ORDER BY s.id""",
                (f"-{days} days",),
            ).fetchall():
                coverage.append({
                    "store": r["name"],
                    "orders": r["orders"],
                    "items_rows_total": r["items_rows"] or 0,
                    "delivered_orders": r["delivered_orders"] or 0,
                    "delivered_with_no_items": r["delivered_no_items"] or 0,
                })

            # Recency-scoped gap: delivered orders with 0 items that are recent
            # enough that a missing capture means an ACTIONABLE scraper problem
            # (vs. an ancient order whose items were never scraped and whose
            # purchase is far outside any restock cadence anyway). julianday on
            # a non-ISO/NULL date yields NULL -> excluded, i.e. treated as old.
            RECENT_GAP_DAYS = 30
            recent_gap = {
                r["store"]: r["n"]
                for r in conn.execute(
                    """SELECT s.name AS store, COUNT(*) AS n
                       FROM orders o JOIN stores s ON s.id = o.store_id
                       WHERE o.status IN ('delivered','completed')
                         AND NOT EXISTS (
                             SELECT 1 FROM order_items oi WHERE oi.order_id = o.id)
                         AND julianday('now') - julianday(
                             COALESCE(o.delivery_date, o.order_date)) <= ?
                       GROUP BY s.id ORDER BY s.id""",
                    (RECENT_GAP_DAYS,),
                ).fetchall()
            }

            # Aggregate by lowercased raw_name (so "Banana" == "banana" ==
            # "BANANA"). Use min/max order_date for first/last seen, and
            # the count of distinct orders for purchase frequency.
            rows = conn.execute(
                """SELECT LOWER(TRIM(oi.raw_name)) AS norm_name,
                          oi.raw_name AS sample_name,
                          MAX(oi.brand) AS brand,
                          MAX(oi.item_id) AS item_id,
                          COUNT(DISTINCT o.id) AS order_count,
                          SUM(oi.quantity) AS total_quantity,
                          MIN(o.order_date) AS first_ordered,
                          MAX(o.order_date) AS last_ordered,
                          MAX(o.delivery_date) AS last_delivery,
                          AVG(oi.unit_price) AS avg_price
                   FROM order_items oi
                   JOIN orders o ON oi.order_id = o.id
                   WHERE o.order_date >= date('now', ?)
                     AND oi.raw_name IS NOT NULL
                     AND TRIM(oi.raw_name) != ''
                   GROUP BY LOWER(TRIM(oi.raw_name))
                   ORDER BY order_count DESC, last_ordered DESC""",
                (f"-{days} days",),
            ).fetchall()

            # Map tracked items -> restock_rules for cross-reference
            rules_by_item = {}
            for r in conn.execute(
                """SELECT r.item_id, i.name, i.category,
                          r.next_restock_date, r.last_ordered_date,
                          r.avg_days_between, r.avg_quantity,
                          r.confidence, r.times_ordered
                   FROM restock_rules r JOIN items i ON r.item_id = i.id"""
            ).fetchall():
                rules_by_item[r["item_id"]] = {
                    "tracked_name": r["name"],
                    "category": r["category"],
                    "next_restock_date": r["next_restock_date"],
                    "last_ordered_date": r["last_ordered_date"],
                    "predictor_avg_days_between": r["avg_days_between"],
                    "predictor_avg_quantity": r["avg_quantity"],
                    "confidence": r["confidence"],
                    "times_ordered": r["times_ordered"],
                }

            today = datetime.now().date()

            analysis = []
            for r in rows:
                # Days since last ordered (from order history, not predictor)
                try:
                    last_ord = datetime.strptime(r["last_ordered"], "%Y-%m-%d").date()
                    days_since_last = (today - last_ord).days
                except Exception:
                    days_since_last = None

                # Observed cadence between consecutive orders.
                # Approximation: (last - first) / (count - 1).
                try:
                    if r["order_count"] > 1:
                        first_ord = datetime.strptime(r["first_ordered"], "%Y-%m-%d").date()
                        last_ord_d = datetime.strptime(r["last_ordered"], "%Y-%m-%d").date()
                        span_days = (last_ord_d - first_ord).days
                        observed_cadence = round(span_days / (r["order_count"] - 1), 1)
                    else:
                        observed_cadence = None
                except Exception:
                    observed_cadence = None

                rules = rules_by_item.get(r["item_id"]) if r["item_id"] else None

                # Flag decision.
                # "overdue_observed" if we have 2+ data points and the gap
                # since last is >= the observed cadence.
                # "overdue_predicted" if the predictor's next_restock_date
                # has passed.
                # Tie-break: prefer the more conservative cadence.
                effective_cadence = None
                if observed_cadence:
                    effective_cadence = observed_cadence
                if rules and rules["predictor_avg_days_between"]:
                    if effective_cadence is None or rules["predictor_avg_days_between"] < effective_cadence:
                        effective_cadence = rules["predictor_avg_days_between"]

                flag = "fresh"
                if r["item_id"] is None:
                    flag = "untracked"
                elif effective_cadence and days_since_last is not None:
                    if days_since_last >= effective_cadence:
                        flag = "overdue_observed"
                if rules and rules.get("next_restock_date"):
                    if rules["next_restock_date"] <= today.strftime("%Y-%m-%d") and flag != "overdue_observed":
                        flag = "overdue_predicted"

                analysis.append({
                    "name": r["sample_name"],
                    "brand": r["brand"] or "",
                    "item_id": r["item_id"],
                    "tracked_name": rules["tracked_name"] if rules else None,
                    "category": rules["category"] if rules else None,
                    "order_count": r["order_count"],
                    "total_quantity": float(r["total_quantity"]) if r["total_quantity"] is not None else None,
                    "first_ordered": r["first_ordered"],
                    "last_ordered": r["last_ordered"],
                    "days_since_last": days_since_last,
                    "observed_cadence_days": observed_cadence,
                    "predictor_cadence_days": rules["predictor_avg_days_between"] if rules else None,
                    "effective_cadence_days": effective_cadence,
                    "predictor_next_restock_date": rules["next_restock_date"] if rules else None,
                    "predictor_confidence": rules["confidence"] if rules else None,
                    "avg_unit_price": round(r["avg_price"], 2) if r["avg_price"] else None,
                    "flag": flag,
                })

            # Recommended-now: the items that should be on Scheduled today.
            # Order by (overdue_observed first, then overdue_predicted),
            # then by how many days overdue, then by order frequency.
            def recommendation_priority(a):
                rank = {"overdue_observed": 0, "overdue_predicted": 1, "fresh": 2, "untracked": 3}
                overdue_days = 0
                if a["days_since_last"] and a["effective_cadence_days"]:
                    overdue_days = a["days_since_last"] - a["effective_cadence_days"]
                return (rank.get(a["flag"], 9), -overdue_days, -a["order_count"])

            recommend = [
                a for a in analysis
                if a["flag"] in ("overdue_observed", "overdue_predicted")
            ]
            recommend.sort(key=recommendation_priority)

            # Data-driven coverage warning. Previously a hardcoded "Carrefour
            # items=0" caveat; now it reflects reality and, crucially,
            # distinguishes a FRESH scraper regression (recent delivered orders
            # with 0 items -- actionable, loud) from ANCIENT misses (old orders
            # whose items were never scraped and whose purchase is far outside
            # any restock cadence -- informational, not an alarm). This keeps
            # the signal high-value: a silent items=[] regression still fires,
            # but a stale un-backfillable order doesn't cry wolf forever.
            _recent_stores = [(s, n) for s, n in recent_gap.items() if (n or 0) > 0]
            _recent_total = sum(n for _, n in _recent_stores)
            _all_total = sum((c.get("delivered_with_no_items") or 0)
                             for c in coverage)
            if _recent_total > 0:
                coverage_warning = (
                    f"{_recent_total} order(s) delivered in the last "
                    f"{RECENT_GAP_DAYS}d captured 0 line items ("
                    + ", ".join(f"{s}: {n}" for s, n in _recent_stores)
                    + "). Those purchases are invisible to the consumption "
                    "model -- re-run the order-detail scraper "
                    "(/api/diag/scrape-carrefour-detail for Carrefour; list "
                    "via /api/diag/orders-missing-items)."
                )
            elif _all_total > 0:
                coverage_warning = (
                    f"{_all_total} older delivered order(s) captured 0 line "
                    f"items but none in the last {RECENT_GAP_DAYS}d -- likely "
                    "ancient/unrecoverable, not a live scraper regression "
                    "(see /api/diag/orders-missing-items)."
                )
            else:
                coverage_warning = None

            return {
                "commit": _BOOT_COMMIT,
                "now": datetime.now().isoformat(timespec="seconds"),
                "days_window": days,
                "coverage_warning": coverage_warning,
                "data_coverage": coverage,
                "summary": {
                    "distinct_items_in_history": len(analysis),
                    "tracked_in_items_table": sum(1 for a in analysis if a["item_id"]),
                    "untracked": sum(1 for a in analysis if a["flag"] == "untracked"),
                    "overdue_observed": sum(1 for a in analysis if a["flag"] == "overdue_observed"),
                    "overdue_predicted": sum(1 for a in analysis if a["flag"] == "overdue_predicted"),
                    "fresh": sum(1 for a in analysis if a["flag"] == "fresh"),
                },
                "should_suggest_now": recommend,
                "all_items": analysis,
            }
    except Exception as e:
        logger.exception("diag_procurement_analysis failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/whatsapp-logs")
async def diag_whatsapp_logs(tail: int = 100, stream: str = "both"):
    """Return the last N lines of the WhatsApp sidecar's stdout/stderr.

    Auth-exempt -- read-only, just a file tail. Used to diagnose
    sidecar-level inbound events without needing RDP to the Ally.

    Query params:
      tail   (default 100, max 500): lines to return
      stream ("stdout" | "stderr" | "both", default "both")

    Log file paths are taken from NSSM service config; falls back
    to the conventional logs/ folder if NSSM lookup fails.
    """
    tail = max(10, min(500, tail))
    out = {"now": datetime.now().isoformat(timespec="seconds")}

    # Standard NSSM log location for the hearthkeeper-whatsapp service.
    # Hardcoded here since the service is fixed by the deploy convention.
    base = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "logs"
    )
    paths = {
        "stdout": os.path.join(base, "service-whatsapp-stdout.log"),
        "stderr": os.path.join(base, "service-whatsapp-stderr.log"),
    }

    streams = ["stdout", "stderr"] if stream == "both" else [stream]
    for s in streams:
        path = paths.get(s)
        if not path or not os.path.exists(path):
            out[s] = {"error": f"log file not found: {path}"}
            continue
        try:
            # Efficient tail: read the last ~8KB per line target (tail*100).
            # Avoids slurping a 100MB log file just to print 100 lines.
            with open(path, "rb") as f:
                f.seek(0, 2)               # end
                size = f.tell()
                read = min(size, max(8192, tail * 200))
                f.seek(size - read)
                tail_bytes = f.read()
            text = tail_bytes.decode("utf-8", errors="replace")
            lines = text.splitlines()
            out[s] = {
                "path": path,
                "lines_returned": min(len(lines), tail),
                "size_bytes": size,
                "tail": lines[-tail:],
            }
        except Exception as e:
            out[s] = {"error": str(e)[:200]}

    return out


@app.get("/api/diag/whatsapp-debug")
async def diag_whatsapp_debug():
    """Read-only diagnosis of the WhatsApp pipeline.

    Returns:
      - sidecar status (reachable? connected to WA?)
      - active household_members (the lookup table for inbound)
      - recent feedback_rounds + member_status (so we can see if a
        round is actually open and which phones can reply into it)
      - recent rows from whatsapp_messages (both outbound and
        inbound, so we can confirm the user's reply did or did not
        reach the sidecar -> webhook path)

    Auth-exempt -- read-only, no PII beyond what the curator already
    sees in the app.
    """
    out = {"now": datetime.now().isoformat(timespec="seconds")}

    # 1) Sidecar status
    try:
        from whatsapp_client import wa_client
        out["sidecar"] = await wa_client.get_status()
    except Exception as e:
        out["sidecar"] = {"error": str(e)[:200]}

    # 2) Members + rounds + messages
    try:
        with app_state.get_db(row_factory=True) as conn:
            members = conn.execute(
                """SELECT id, name, display_name, phone, role, is_active
                   FROM household_members ORDER BY id DESC LIMIT 20"""
            ).fetchall()
            out["members"] = [dict(m) for m in members]

            rounds = conn.execute(
                """SELECT id, list_id, status, initiated_at, deadline_at
                   FROM feedback_rounds ORDER BY id DESC LIMIT 5"""
            ).fetchall()
            out["recent_rounds"] = [dict(r) for r in rounds]

            mstats = conn.execute(
                """SELECT round_id, member_id, status, notified_at, replied_at
                   FROM feedback_member_status ORDER BY id DESC LIMIT 10"""
            ).fetchall()
            out["recent_member_status"] = [dict(s) for s in mstats]

            # whatsapp_messages -- show direction + member + first 80 chars.
            # Schema: round_id, member_id, direction, wa_message_id,
            # content, status, error_detail, created_at.
            try:
                msgs = conn.execute(
                    """SELECT wm.id, wm.direction, wm.round_id, wm.member_id,
                              wm.content, wm.wa_message_id, wm.status,
                              wm.error_detail, wm.created_at,
                              hm.name AS member_name
                       FROM whatsapp_messages wm
                       LEFT JOIN household_members hm ON hm.id = wm.member_id
                       ORDER BY wm.id DESC LIMIT 20"""
                ).fetchall()
                out["recent_messages"] = [
                    {
                        "id": m["id"],
                        "direction": m["direction"],
                        "round_id": m["round_id"],
                        "member": m["member_name"] or f"#{m['member_id']}",
                        "preview": (m["content"] or "")[:80],
                        "status": m["status"],
                        "error": (m["error_detail"] or "")[:80],
                        "wa_id": (m["wa_message_id"] or "")[-16:],
                        "ts": m["created_at"],
                    }
                    for m in msgs
                ]
            except Exception as e:
                out["recent_messages"] = {"error": f"query failed: {str(e)[:200]}"}
    except Exception as e:
        out["db_error"] = str(e)[:200]

    return out


@app.post("/api/diag/set-member-lid")
async def diag_set_member_lid(request: Request):
    """HMAC-authed: backfill the wa_lid for an existing household_member.

    Used when we've observed a member's LID in the sidecar logs but
    haven't yet linked it to their household_members row. After this
    is set, inbound LID-format messages from that contact will route
    correctly through get_member_by_phone -> handle_member_reply.

    Body (JSON):
      {
        "phone": "+90XXXXXXXXXX",    // required, identifies member
        "wa_lid": "WA_LID_REDACTED"  // required, the LID numeric part
      }
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse({"error": "diag trigger not configured"}, status_code=503)
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    phone = (payload.get("phone") or "").strip()
    wa_lid = (payload.get("wa_lid") or "").strip().replace("@lid", "")
    if not phone or not wa_lid:
        return JSONResponse(
            {"error": "phone + wa_lid required"}, status_code=400
        )
    if phone.isdigit() and not phone.startswith("0"):
        phone = "+" + phone

    try:
        with app_state.get_db(row_factory=True) as conn:
            result = conn.execute(
                "UPDATE household_members SET wa_lid = ? WHERE phone = ?",
                (wa_lid, phone),
            )
            updated = result.rowcount
            row = conn.execute(
                "SELECT id, name, phone, wa_lid FROM household_members WHERE phone = ?",
                (phone,),
            ).fetchone()
        log_event(
            "diag_set_member_lid", source="diag",
            detail=f"phone=…{phone[-4:]} lid={wa_lid} updated={updated}",
        )
        return {
            "ok": True,
            "updated": updated,
            "member": dict(row) if row else None,
        }
    except Exception as e:
        logger.exception("diag_set_member_lid failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/diag/rebuild-item-alternatives")
async def diag_rebuild_item_alternatives(request: Request):
    """Re-score every (item, store) pair's alternatives using the
    CURRENT category-aware matcher config in product_knowledge.

    Iterates over every (item_id, store_id) pair that currently has
    rows in item_alternatives, collects the previously-discovered
    catalog candidates, WIPES that pair's rows, then re-runs
    score_and_rank_alternatives. The new category + form-modifier
    gates filter out obviously-wrong matches before insert, so the
    table ends up cleaner after the rebuild.

    Body (optional): { "dry_run": true, "item_id": int }
       dry_run     -- report counts without writing
       item_id     -- limit to one item (otherwise: all items)

    Fail-closed: 503 if GITHUB_WEBHOOK_SECRET is unset.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured"}, status_code=503,
        )
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    dry_run = False
    only_item_id = None
    if body:
        try:
            payload = json.loads(body)
            dry_run = bool(payload.get("dry_run", False))
            only_item_id = payload.get("item_id")
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)

    try:
        from product_knowledge import (
            score_and_rank_alternatives, search_catalog,
            _primary_nouns, _noun_variants,
        )
        pairs_processed = 0
        candidates_total = 0
        candidates_after = 0
        candidates_freshly_discovered = 0
        with app_state.get_db(row_factory=True) as conn:
            # Iterate items x stores (CROSS JOIN) rather than just
            # existing item_alternatives pairs. If an earlier pass had
            # filtered out every candidate for a pair, that pair no
            # longer appears in item_alternatives -- and we'd never
            # reconsider it on rebuild. Walking items x stores keeps
            # every item in the loop so a fresh search can rediscover.
            q = (
                "SELECT i.id AS item_id, s.id AS store_id "
                "FROM items i CROSS JOIN stores s "
                "WHERE i.id IS NOT NULL AND s.id IS NOT NULL"
            )
            args = ()
            if only_item_id:
                q += " AND i.id = ?"
                args = (int(only_item_id),)
            pairs = conn.execute(q, args).fetchall()

            for pair in pairs:
                item_id = pair["item_id"]
                store_id = pair["store_id"]
                item_row = conn.execute(
                    "SELECT name FROM items WHERE id = ?", (item_id,),
                ).fetchone()
                if not item_row:
                    continue
                item_name = item_row["name"]

                # Source 1: existing catalog candidates from
                # item_alternatives (the matcher's current pool).
                existing = conn.execute(
                    "SELECT catalog_id FROM item_alternatives "
                    "WHERE item_id = ? AND store_id = ?",
                    (item_id, store_id),
                ).fetchall()
                existing_ids = {r["catalog_id"] for r in existing}

                # Source 2: a FRESH search of product_catalog for the
                # item name + every noun spelling variant. search_catalog
                # uses LIKE %word% on product_name -- so a search for
                # "Yogurt" cannot find a "Yoghurt" product. We compensate
                # by also searching each variant of each primary noun.
                # Catches previously-filtered-out candidates that the
                # current gates would accept (Yogurt <-> Yoghurt,
                # Eggplant <-> Aubergine, etc.).
                search_terms = {item_name}
                for noun in _primary_nouns(item_name):
                    for variant in _noun_variants(noun):
                        if variant != noun:
                            search_terms.add(variant)
                fresh_ids = set()
                for term in search_terms:
                    results = search_catalog(
                        term, store_id=store_id, limit=30, conn=conn,
                    ) or []
                    for r in results:
                        cid = r.get("catalog_id")
                        if cid is not None:
                            fresh_ids.add(cid)

                merged_ids = sorted(existing_ids | fresh_ids)
                new_fresh = len(fresh_ids - existing_ids)
                candidates_freshly_discovered += new_fresh

                before_counts_n = len(merged_ids)
                candidates_total += before_counts_n

                if dry_run:
                    continue

                # Wipe + re-score against the union of sources
                conn.execute(
                    "DELETE FROM item_alternatives "
                    "WHERE item_id = ? AND store_id = ?",
                    (item_id, store_id),
                )
                conn.commit()

                score_and_rank_alternatives(
                    item_id, item_name, store_id, merged_ids, conn=conn,
                )

                # Count what survived
                surviving = conn.execute(
                    "SELECT COUNT(*) FROM item_alternatives "
                    "WHERE item_id = ? AND store_id = ?",
                    (item_id, store_id),
                ).fetchone()[0]
                candidates_after += surviving
                pairs_processed += 1

        log_event(
            "diag_rebuild_alts", source="diag",
            detail=(
                f"dry_run={dry_run} pairs={pairs_processed} "
                f"candidates_before={candidates_total} "
                f"candidates_after={candidates_after} "
                f"freshly_discovered={candidates_freshly_discovered}"
            ),
        )
        return {
            "ok": True,
            "dry_run": dry_run,
            "pairs_processed": pairs_processed,
            "pairs_seen": len(pairs),
            "candidates_before": candidates_total,
            "candidates_after": candidates_after,
            "candidates_dropped": candidates_total - candidates_after,
            "candidates_freshly_discovered": candidates_freshly_discovered,
        }
    except Exception as e:
        logger.exception("diag_rebuild_item_alternatives failed")
        return JSONResponse({"error": str(e)[:300]}, status_code=500)


@app.get("/api/diag/item-categories")
async def diag_item_categories(name_contains: str = None):
    """List items with their category for quick matcher debugging."""
    try:
        with app_state.get_db(row_factory=True) as conn:
            if name_contains:
                rows = conn.execute(
                    "SELECT id, name, category FROM items "
                    "WHERE name LIKE ? ORDER BY name",
                    (f"%{name_contains}%",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, name, category FROM items ORDER BY category, name"
                ).fetchall()
        return {
            "count": len(rows),
            "items": [
                {"id": r["id"], "name": r["name"], "category": r["category"]}
                for r in rows
            ],
        }
    except Exception as e:
        logger.exception("diag_item_categories failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/category-coverage")
async def diag_category_coverage():
    """How well-populated are items.category and product_catalog.category?

    Tells us whether a category-aware catalog matcher would actually
    have data to work with, OR whether we'd need to populate categories
    first.

    Returns: per-table coverage stats + the distinct category values
    seen in each. No PII, no chat — just counts and category names.
    """
    try:
        with app_state.get_db(row_factory=True) as conn:
            it_total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            it_with_cat = conn.execute(
                "SELECT COUNT(*) FROM items "
                "WHERE category IS NOT NULL AND TRIM(category) != ''"
            ).fetchone()[0]
            it_cats = [
                {"category": r["category"], "n": r["n"]}
                for r in conn.execute(
                    "SELECT category, COUNT(*) AS n FROM items "
                    "WHERE category IS NOT NULL AND TRIM(category) != '' "
                    "GROUP BY category ORDER BY n DESC"
                ).fetchall()
            ]

            pc_total = conn.execute("SELECT COUNT(*) FROM product_catalog").fetchone()[0]
            pc_with_cat = conn.execute(
                "SELECT COUNT(*) FROM product_catalog "
                "WHERE category IS NOT NULL AND TRIM(category) != ''"
            ).fetchone()[0]
            pc_top_cats = [
                {"category": r["category"], "n": r["n"]}
                for r in conn.execute(
                    "SELECT category, COUNT(*) AS n FROM product_catalog "
                    "WHERE category IS NOT NULL AND TRIM(category) != '' "
                    "GROUP BY category ORDER BY n DESC LIMIT 30"
                ).fetchall()
            ]
            pc_distinct = conn.execute(
                "SELECT COUNT(DISTINCT category) FROM product_catalog "
                "WHERE category IS NOT NULL AND TRIM(category) != ''"
            ).fetchone()[0]
            it_distinct = conn.execute(
                "SELECT COUNT(DISTINCT category) FROM items "
                "WHERE category IS NOT NULL AND TRIM(category) != ''"
            ).fetchone()[0]

        return {
            "items": {
                "total": it_total,
                "with_category": it_with_cat,
                "coverage_pct": round(100 * it_with_cat / it_total, 1) if it_total else 0,
                "distinct_categories": it_distinct,
                "by_category": it_cats,
            },
            "product_catalog": {
                "total": pc_total,
                "with_category": pc_with_cat,
                "coverage_pct": round(100 * pc_with_cat / pc_total, 1) if pc_total else 0,
                "distinct_categories": pc_distinct,
                "top_30_categories": pc_top_cats,
            },
        }
    except Exception as e:
        logger.exception("diag_category_coverage failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/render-cart-message")
async def diag_render_cart_message(
    list_id: int = None,
    member_name: str = "Cem",
    target_day: str = "Tuesday",
    deliveries: str = "Tuesday 7-11am",
    variant: str = "default",
):
    """Pure-render endpoint: returns the exact text format_cart_message
    would produce for the given list, with no side effects. No WhatsApp
    send, no DB write, no round creation. Read-only.

    Auth-exempt (no PII -- the rendered text contains item names +
    prices + the member name we passed in).

    Defaults to the latest draft suggestion list when list_id is omitted.
    """
    try:
        from frontend_bridge import build_frontend_list
        from whatsapp_agent import format_cart_message

        if not list_id:
            with app_state.get_db() as conn:
                row = conn.execute(
                    """SELECT id FROM shopping_lists
                       WHERE status IN ('draft', 'confirmed')
                         AND (list_type = 'suggestion' OR list_type IS NULL)
                         AND dismissed_at IS NULL
                       ORDER BY id DESC LIMIT 1"""
                ).fetchone()
                if not row:
                    return JSONResponse(
                        {"error": "no active suggestion list"},
                        status_code=404,
                    )
                list_id = row[0]

        list_data = build_frontend_list(int(list_id))
        if (variant or "default").lower() == "category":
            from whatsapp_agent import format_cart_message_category
            rendered = format_cart_message_category(
                list_data,
                member_name=member_name,
                target_day=target_day or None,
                deliveries=deliveries or None,
            )
        else:
            rendered = format_cart_message(
                list_data,
                member_name=member_name,
                target_day=target_day or None,
                deliveries=deliveries or None,
            )
        return {
            "list_id": int(list_id),
            "char_count": len(rendered),
            "variant": variant or "default",
            "text": rendered,
        }
    except Exception as e:
        logger.exception("diag_render_cart_message failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/render-add-confirmation")
async def diag_render_add_confirmation(list_id: int, list_item_ids: str = ""):
    """Pure-render: the exact 'add' confirmation message for one or more
    added list items, using the same per-category line logic as the list.
    Read-only, no side effects, no WhatsApp send.

    list_item_ids: comma-separated list_item ids to confirm. Omit to
    confirm every user-added item (item added_by='user') on the list.
    """
    try:
        from frontend_bridge import build_frontend_list
        from whatsapp_agent import format_add_result

        wanted = {int(x) for x in list_item_ids.split(",") if x.strip().isdigit()}
        data = build_frontend_list(int(list_id))

        # Flatten vendors -> categories -> items, attaching the simplified
        # category name (the category tree key) onto each item.
        added = []
        for v in data.get("vendors", []):
            for c in v.get("categories", []):
                cat_name = c.get("name") or "Other"
                for it in c.get("items", []):
                    if wanted and it.get("id") not in wanted:
                        continue
                    if not wanted and it.get("addedBy") != "user":
                        continue
                    added.append({**it, "_category": cat_name})

        text = format_add_result(added, not_found=[])
        return {
            "list_id": int(list_id),
            "matched": [a.get("id") for a in added],
            "char_count": len(text),
            "text": text,
        }
    except Exception as e:
        logger.exception("diag_render_add_confirmation failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/catalog-peek")
async def diag_catalog_peek(q: str, organic: int = -1, limit: int = 20):
    """Read-only catalog inspection: rows whose product_name contains `q`,
    ordered available-first then cheapest. Used to verify stock/price
    reality on this host (each host scrapes its own catalog snapshot, so
    availability/price can differ from the dev box).

    organic: -1 any, 1 organic only, 0 non-organic only.
    """
    try:
        clauses = ["LOWER(product_name) LIKE LOWER(?)"]
        params = [f"%{q}%"]
        if organic in (0, 1):
            clauses.append("is_organic = ?")
            params.append(organic)
        params.append(max(1, min(limit, 60)))
        with app_state.get_db(row_factory=True) as conn:
            rows = conn.execute(
                f"""SELECT id, product_name, brand, size, price, is_organic,
                           is_available, origin_country, category, store_id
                    FROM product_catalog
                    WHERE {' AND '.join(clauses)}
                    ORDER BY is_available DESC, price ASC
                    LIMIT ?""",
                params,
            ).fetchall()
        return {
            "q": q, "organic_filter": organic, "count": len(rows),
            "rows": [
                {
                    "id": r["id"], "product_name": r["product_name"],
                    "brand": r["brand"], "size": r["size"], "price": r["price"],
                    "organic": bool(r["is_organic"]),
                    "available": bool(r["is_available"]),
                    "origin": r["origin_country"], "store_id": r["store_id"],
                    "category": r["category"],
                }
                for r in rows
            ],
        }
    except Exception as e:
        logger.exception("diag_catalog_peek failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/resolution-attempts")
async def diag_resolution_attempts(item_name: str = "", item_id: int = 0, limit: int = 12):
    """Read-only: the validator's per-item resolution attempt log
    (item_resolution_attempts). Shows what score_candidate decided for an item
    -- how many candidates were seen / met the bar / were in stock, the outcome
    (matched/switched/skipped/no_match), the store tried, and the notes. Use it
    to triage 'why did this item get skipped?' without DB access.
    """
    try:
        with app_state.get_db(row_factory=True) as conn:
            if item_id:
                irow = conn.execute("SELECT id, name, category FROM items WHERE id = ?", (item_id,)).fetchone()
            else:
                irow = conn.execute("SELECT id, name, category FROM items WHERE LOWER(name) = LOWER(?)", (item_name,)).fetchone()
            if not irow:
                return JSONResponse({"error": "item not found"}, status_code=404)
            rows = conn.execute(
                """SELECT id, list_id, store_id_tried, candidates_total,
                          candidates_meeting_bar, candidates_in_stock, outcome,
                          resolved_catalog_id, resolved_store_id, notes
                   FROM item_resolution_attempts
                   WHERE item_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (irow["id"], max(1, min(limit, 50))),
            ).fetchall()
            cur_li = conn.execute(
                """SELECT li.id, li.list_id, li.status, li.user_action, li.added_by,
                          li.store_id, li.catalog_id
                   FROM list_items li
                   JOIN shopping_lists sl ON sl.id = li.list_id
                   WHERE li.item_id = ? AND sl.status = 'draft'
                     AND COALESCE(sl.dismissed_at,'') = ''
                   ORDER BY li.id DESC LIMIT 1""",
                (irow["id"],),
            ).fetchone()
            return {
                "item": {"id": irow["id"], "name": irow["name"], "category": irow["category"]},
                "current_list_item": dict(cur_li) if cur_li else None,
                "attempts": [dict(r) for r in rows],
            }
    except Exception as e:
        logger.exception("diag_resolution_attempts failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/validator-query")
async def diag_validator_query(item_name: str = "", term: str = ""):
    """Replicate the validator's candidate retrieval for an item so we can see
    why it found 0 candidates. Shows the item's category/preferred store, the
    store_order, and for each store: the category-filtered count, the broad
    (no-category) count, and a sample of names+categories. Read-only.
    """
    try:
        with app_state.get_db(row_factory=True) as conn:
            irow = conn.execute(
                "SELECT id, name, category, preferred_store_id, search_terms "
                "FROM items WHERE LOWER(name) = LOWER(?)", (item_name,)).fetchone()
            if not irow:
                return JSONResponse({"error": "item not found"}, status_code=404)
            item = dict(irow)
            t = term or item["name"]
            item_cat = (item.get("category") or "").lower().strip()
            stores = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM stores").fetchall()}
            out = {"item": {k: item.get(k) for k in ("id", "name", "category", "preferred_store_id")},
                   "term": t, "item_cat": item_cat, "stores": stores, "per_store": []}
            for sid in stores:
                catf = conn.execute(
                    "SELECT id, product_name, category FROM product_catalog "
                    "WHERE store_id=? AND LOWER(product_name) LIKE LOWER(?) "
                    "AND LOWER(COALESCE(category,'')) LIKE LOWER(?) LIMIT 20",
                    (sid, f"%{t}%", f"%{item_cat}%")).fetchall()
                broad = conn.execute(
                    "SELECT id, product_name, category, is_organic, is_available FROM product_catalog "
                    "WHERE store_id=? AND LOWER(product_name) LIKE LOWER(?) LIMIT 20",
                    (sid, f"%{t}%")).fetchall()
                out["per_store"].append({
                    "store_id": sid, "store": stores.get(sid),
                    "cat_filtered_count": len(catf),
                    "broad_count": len(broad),
                    "broad_sample": [{"name": r["product_name"], "cat": r["category"],
                                      "org": r["is_organic"], "avail": r["is_available"]}
                                     for r in broad[:8]],
                })
            return out
    except Exception as e:
        logger.exception("diag_validator_query failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/diag/simulate-reply")
async def diag_simulate_reply(request: Request):
    """Run the WhatsApp reply dispatcher AGAINST a real round but
    intercept every outbound send -- no actual WhatsApp message goes
    out. Returns the captured outbound text(s) + the action_result so
    we can evaluate "what would the user have seen" without spamming.

    Body (HMAC-authed):
      {
        "phone":  "+90...",         // required, must be a household_member
        "text":   "skip 3",         // required, the simulated inbound
        "set_pending_tier2": {      // optional, for E4 tests
            "item_id": int,
            "item_name": str,
        }
      }

    Returns:
      {
        "input_text": str,
        "captured_outbound": [{"phone": str, "text": str}, ...],
        "action_result": {...},  // whatever handle_member_reply returned
        "list_item_snapshot": [   // pre/post state of list items
            {"id": ..., "status": ..., "user_action": ..., "name": ...},
            ...
        ]
      }

    The endpoint monkey-patches wa_client.send_message for the duration
    of the call. Single-threaded use only.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured"}, status_code=503,
        )
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    phone = (payload.get("phone") or "").strip()
    text = payload.get("text") or ""
    if not phone or not text:
        return JSONResponse({"error": "phone and text required"}, status_code=400)
    set_pending_tier2 = payload.get("set_pending_tier2")
    reset_list_items_first = bool(payload.get("reset_list_items_first"))

    try:
        # Look up member
        from whatsapp_client import WhatsAppClient, wa_client
        from feedback_manager import feedback_manager
        member = WhatsAppClient.get_member_by_phone(phone)
        if not member:
            return JSONResponse(
                {"error": f"no household_member with phone {phone}"},
                status_code=404,
            )

        # Optional: reset all list_items on the member's active round
        # back to pending so each test scenario starts from a known
        # state. Also clears member_pending_intent + the round's
        # pending_intent so we're not stuck in a multi-turn flow from
        # a previous simulation.
        if reset_list_items_first:
            try:
                with app_state.get_db() as conn:
                    conn.execute(
                        """UPDATE list_items
                           SET status = 'pending', user_action = NULL
                           WHERE list_id IN (
                               SELECT fr.list_id
                               FROM feedback_rounds fr
                               JOIN feedback_member_status fms
                                   ON fms.round_id = fr.id
                               WHERE fms.member_id = ?
                                 AND fr.status = 'collecting'
                           )""",
                        (member["id"],),
                    )
                    conn.execute(
                        """UPDATE feedback_member_status
                           SET pending_intent = NULL
                           WHERE member_id = ? AND round_id IN (
                               SELECT id FROM feedback_rounds
                               WHERE status = 'collecting'
                           )""",
                        (member["id"],),
                    )
                    conn.execute(
                        "UPDATE household_members SET member_pending_intent = NULL WHERE id = ?",
                        (member["id"],),
                    )
                    conn.commit()
            except Exception as e:
                logger.warning("simulate-reply reset failed: %s", e)

        # Optionally pre-set a tier2_ask pending intent (for E4 testing)
        if set_pending_tier2:
            with app_state.get_db() as conn:
                pending_payload = {
                    "type": "tier2_ask",
                    "item_id": set_pending_tier2.get("item_id"),
                    "item_name": set_pending_tier2.get("item_name", "TestItem"),
                    "asked_at": datetime.now().isoformat(timespec="seconds"),
                    "drop_count": 3,
                }
                conn.execute(
                    "UPDATE household_members SET member_pending_intent = ? WHERE id = ?",
                    (json.dumps(pending_payload), member["id"]),
                )
                conn.commit()

        # Snapshot list_items BEFORE
        list_item_before = []
        try:
            with app_state.get_db() as conn:
                rows = conn.execute(
                    """SELECT li.id, li.status, li.user_action,
                              i.name AS item_name
                       FROM list_items li
                       JOIN shopping_lists sl ON sl.id = li.list_id
                       JOIN feedback_rounds fr ON fr.list_id = sl.id
                       JOIN feedback_member_status fms ON fms.round_id = fr.id
                       LEFT JOIN items i ON i.id = li.item_id
                       WHERE fms.member_id = ?
                         AND fr.status = 'collecting'
                       ORDER BY li.id""",
                    (member["id"],),
                ).fetchall()
                list_item_before = [dict(r) for r in rows]
        except Exception:
            list_item_before = []

        # Monkey-patch wa_client.send_message to capture
        captured = []
        orig_send = wa_client.send_message

        async def _capture_send(phone_arg, text_arg, **kw):
            captured.append({
                "phone": phone_arg,
                "text": text_arg,
                "round_id": kw.get("round_id"),
                "member_id": kw.get("member_id"),
            })
            # Return a fake message id so callers that check truthiness work
            return f"sim_msg_{len(captured)}"

        wa_client.send_message = _capture_send
        try:
            action_result = await feedback_manager.handle_member_reply(
                member, text, message_id=f"sim_{int(datetime.now().timestamp())}",
            )
        finally:
            wa_client.send_message = orig_send

        # Snapshot AFTER
        list_item_after = []
        try:
            with app_state.get_db() as conn:
                rows = conn.execute(
                    """SELECT li.id, li.status, li.user_action,
                              i.name AS item_name
                       FROM list_items li
                       JOIN shopping_lists sl ON sl.id = li.list_id
                       JOIN feedback_rounds fr ON fr.list_id = sl.id
                       JOIN feedback_member_status fms ON fms.round_id = fr.id
                       LEFT JOIN items i ON i.id = li.item_id
                       WHERE fms.member_id = ?
                         AND fr.status = 'collecting'
                       ORDER BY li.id""",
                    (member["id"],),
                ).fetchall()
                list_item_after = [dict(r) for r in rows]
        except Exception:
            list_item_after = []

        # Diff
        diff = []
        before_by_id = {r["id"]: r for r in list_item_before}
        for after in list_item_after:
            before = before_by_id.get(after["id"])
            if before and (
                before.get("status") != after.get("status")
                or before.get("user_action") != after.get("user_action")
            ):
                diff.append({
                    "list_item_id": after["id"],
                    "item_name": after.get("item_name"),
                    "before_status": before.get("status"),
                    "after_status": after.get("status"),
                    "before_action": before.get("user_action"),
                    "after_action": after.get("user_action"),
                })

        return {
            "input_text": text,
            "captured_outbound": captured,
            "action_result": action_result,
            "list_item_changes": diff,
        }
    except Exception as e:
        logger.exception("diag_simulate_reply failed")
        return JSONResponse({"error": str(e)[:300]}, status_code=500)


@app.post("/api/diag/init-test-round")
async def diag_init_test_round(request: Request):
    """HMAC-authed: full end-to-end test round to one phone number.

    Unlike /api/diag/send-test-list (one-shot), this:
      1. Ensures the phone number is registered as a household_member
         (upsert by phone; creates if missing).
      2. Initiates a real feedback_rounds row.
      3. Sends the v2 format_cart_message via wa_client.
      4. Records member status, so when the recipient replies,
         feedback_manager.handle_member_reply finds the active round
         and processes the message.

    Body (JSON):
      {
        "phone": "+90XXXXXXXXXX",     // required
        "list_id": 1107,              // optional, default latest draft
        "member_name": "Cem (Test)",  // shown in greeting
        "target_day": "Tuesday",
        "deliveries": "Tuesday 7-11am",
        "timeout_minutes": 30         // round window
      }

    Returns round_id + wa_message_id so the caller can verify the
    interactive loop is live.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured"}, status_code=503
        )
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    phone = (payload.get("phone") or "").strip()
    if not phone:
        return JSONResponse({"error": "phone required"}, status_code=400)
    if phone.isdigit() and not phone.startswith("0"):
        phone = "+" + phone

    member_name = (payload.get("member_name") or "Test User").strip()
    target_day = (payload.get("target_day") or "").strip() or None
    deliveries = (payload.get("deliveries") or "").strip() or None
    list_id = payload.get("list_id")
    timeout_minutes = int(payload.get("timeout_minutes") or 30)

    try:
        # 1) Resolve / pick list
        with app_state.get_db(row_factory=True) as conn:
            if not list_id:
                row = conn.execute(
                    """SELECT id FROM shopping_lists
                       WHERE status IN ('draft', 'confirmed')
                         AND (list_type = 'suggestion' OR list_type IS NULL)
                         AND dismissed_at IS NULL
                       ORDER BY id DESC LIMIT 1"""
                ).fetchone()
                if not row:
                    return JSONResponse({"error": "no active list"}, status_code=404)
                list_id = row["id"]

            # Derive target_day if missing
            if not target_day:
                try:
                    row = conn.execute(
                        "SELECT target_date FROM shopping_lists WHERE id = ?",
                        (list_id,),
                    ).fetchone()
                    if row and row["target_date"]:
                        dt = datetime.fromisoformat(row["target_date"])
                        target_day = dt.strftime("%A")
                except Exception:
                    pass

            # 2) Upsert member by phone
            existing = conn.execute(
                "SELECT * FROM household_members WHERE phone = ?", (phone,)
            ).fetchone()
            if existing:
                member_id = existing["id"]
                # Make sure they're active + role='member' so handle_member_reply finds them
                conn.execute(
                    """UPDATE household_members
                       SET name = ?, display_name = ?, role = 'member', is_active = 1
                       WHERE id = ?""",
                    (member_name, member_name, member_id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO household_members (name, display_name, phone, role, is_active)
                       VALUES (?, ?, ?, 'member', 1)""",
                    (member_name, member_name, phone),
                )
                member_id = cur.lastrowid

            # 3) Create a fresh feedback round (cancel any open rounds for this list first)
            conn.execute(
                """UPDATE feedback_rounds
                   SET status = 'cancelled', completed_at = datetime('now')
                   WHERE list_id = ? AND status IN ('pending', 'collecting')""",
                (list_id,),
            )
            deadline = (datetime.utcnow() + timedelta(minutes=timeout_minutes)).isoformat()
            cur = conn.execute(
                """INSERT INTO feedback_rounds (list_id, status, deadline_at)
                   VALUES (?, 'collecting', ?)""",
                (int(list_id), deadline),
            )
            round_id = cur.lastrowid

            # 4) Insert member_status row so the reply path can find the round
            conn.execute(
                """INSERT INTO feedback_member_status
                   (round_id, member_id, status, notified_at)
                   VALUES (?, ?, 'notified', ?)""",
                (round_id, member_id, datetime.utcnow().isoformat()),
            )

        # 5) Render + send the v2 message
        from frontend_bridge import build_frontend_list
        from whatsapp_agent import format_cart_message
        from whatsapp_client import wa_client
        list_data = build_frontend_list(int(list_id))
        rendered = format_cart_message(
            list_data,
            member_name=member_name,
            target_day=target_day,
            deliveries=deliveries,
        )
        wa_msg_id = await wa_client.send_message(
            phone, rendered, round_id=round_id, member_id=member_id,
        )

        log_event(
            "diag_init_test_round",
            source="diag",
            detail=(f"phone=…{phone[-4:]} list={list_id} round={round_id} "
                    f"member={member_id} sent={bool(wa_msg_id)}"),
        )
        return {
            "ok": True,
            "round_id": round_id,
            "list_id": int(list_id),
            "member_id": member_id,
            "phone_tail": phone[-4:],
            "rendered_chars": len(rendered),
            "wa_message_id": wa_msg_id,
            "deadline_at": deadline,
        }
    except Exception as e:
        logger.exception("diag_init_test_round failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/diag/send-test-list")
async def diag_send_test_list(request: Request):
    """HMAC-authed: render the current draft suggestion list using
    the v2 WhatsApp format and send it to a specific phone number.

    Used to preview the new message format on a real device without
    initiating a full feedback round (no member rows changed, no
    round opened). Pure dry-run of the formatting.

    Body (JSON):
      {
        "phone": "+90XXXXXXXXXX",      // required, E.164
        "list_id": 1107,               // optional, defaults to latest draft
        "member_name": "Cem (Turkey)", // optional, used in greeting
        "target_day": "Tuesday",       // optional
        "deliveries": "Tuesday 7-11am · Wed 9-11am"  // optional
      }

    Returns the rendered message + WA send result.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured (GITHUB_WEBHOOK_SECRET unset)"},
            status_code=503,
        )

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        log_event("diag_send_list_unauthorized", source="diag",
                  detail="HMAC verification failed", level="warning")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    phone = (payload.get("phone") or "").strip()
    if not phone:
        return JSONResponse({"error": "phone required (E.164)"}, status_code=400)
    if phone.isdigit() and not phone.startswith("0"):
        phone = "+" + phone

    member_name = (payload.get("member_name") or "Test User").strip()
    target_day = (payload.get("target_day") or "").strip() or None
    deliveries = (payload.get("deliveries") or "").strip() or None
    list_id = payload.get("list_id")

    try:
        # Resolve list_id: explicit > most recent suggestion draft > most recent draft.
        with app_state.get_db(row_factory=True) as conn:
            if not list_id:
                row = conn.execute(
                    """SELECT id FROM shopping_lists
                       WHERE status IN ('draft', 'confirmed')
                         AND (list_type = 'suggestion' OR list_type IS NULL)
                         AND dismissed_at IS NULL
                       ORDER BY id DESC LIMIT 1"""
                ).fetchone()
                if not row:
                    return JSONResponse(
                        {"error": "no active suggestion list found"},
                        status_code=404,
                    )
                list_id = row["id"]

        # If target_day wasn't supplied, derive it from the list's target_date.
        if not target_day:
            try:
                with app_state.get_db(row_factory=True) as conn:
                    row = conn.execute(
                        "SELECT target_date FROM shopping_lists WHERE id = ?",
                        (list_id,),
                    ).fetchone()
                if row and row["target_date"]:
                    dt = datetime.fromisoformat(row["target_date"])
                    target_day = dt.strftime("%A")
            except Exception:
                pass

        # Render the v2 format using the live list data.
        from frontend_bridge import build_frontend_list
        from whatsapp_agent import format_cart_message
        list_data = build_frontend_list(int(list_id))
        rendered = format_cart_message(
            list_data,
            member_name=member_name,
            target_day=target_day,
            deliveries=deliveries,
        )

        # Send via the WA client. Returns the WA message id on success.
        from whatsapp_client import wa_client
        wa_msg_id = await wa_client.send_message(phone, rendered)

        log_event("diag_send_test_list", source="diag",
                  detail=f"phone={phone[-4:]} list_id={list_id} "
                         f"len={len(rendered)} sent={bool(wa_msg_id)}")

        return {
            "ok": True,
            "list_id": int(list_id),
            "to_phone_tail": phone[-4:],
            "rendered_chars": len(rendered),
            "wa_message_id": wa_msg_id,
            "rendered_preview": rendered,
        }
    except Exception as e:
        logger.exception("diag_send_test_list failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/send-list-to-member")
async def admin_send_list_to_member(request: Request):
    """Session-authed: send the current scheduled list to a household
    member by name. Twin of /api/diag/send-test-list but reachable from
    a normal logged-in browser (or anyone with SESSION_SECRET who can
    synthesize a cookie) rather than only from a process that holds
    GITHUB_WEBHOOK_SECRET.

    Why both endpoints exist:
      - /api/diag/send-test-list (HMAC) is for ops scripts / CI that
        don't have a session, only a shared secret.
      - this one (session) is for the dashboard and for ad-hoc sends
        from anyone already authed into the household app.

    The recipient's phone number is looked up from household_members
    by name -- no phone in the request body, no PII in URL or logs.

    Body (JSON):
      {
        "member_name": "Sara",       // either this OR "phone" required.
                                     // matched against household_members.name
                                     // OR display_name (case-insensitive)
        "phone":       "+905...",    // alternative to member_name -- send
                                     // to an explicit E.164 phone (used for
                                     // admin test sends to numbers not
                                     // registered in household_members).
                                     // member_name still used as greeting
                                     // text if supplied alongside.
        "regen":       true,         // optional, default false. When
                                     // true, regenerates the scheduled
                                     // list FIRST so the recipient sees
                                     // the freshest inventory-model
                                     // output, not a stale draft.
        "list_id":     1109,         // optional, explicit list to send
        "target_day":  "Tuesday",    // optional override
        "deliveries":  "..."         // optional human delivery summary
      }
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    name_query = (payload.get("member_name") or "").strip()
    explicit_phone = (payload.get("phone") or "").strip()
    if not name_query and not explicit_phone:
        return JSONResponse(
            {"error": "either member_name or phone required"}, status_code=400
        )

    list_id = payload.get("list_id")
    target_day = (payload.get("target_day") or "").strip() or None
    deliveries = (payload.get("deliveries") or "").strip() or None
    do_regen = bool(payload.get("regen"))
    # include_links defaults to True for parity with format_cart_message's
    # signature. Pass false to skip the per-item productUrl lines (useful
    # when the URL clutter outweighs the convenience).
    include_links = payload.get("include_links")
    if include_links is None:
        include_links = True
    include_links = bool(include_links)
    # variant="default" (current vendor-grouped format with optional URLs)
    # variant="category" (2026-05-27 category-grouped, no per-item URLs,
    #                     numbered globally 1..N across categories)
    variant = (payload.get("variant") or "default").strip().lower()

    try:
        # Resolve the recipient. Prefer member_name lookup so the
        # response reveals identity (not just a phone fragment). If only
        # phone is supplied, send to it directly with a generic greeting.
        member_id = None
        if name_query:
            with app_state.get_db(row_factory=True) as conn:
                row = conn.execute(
                    """SELECT id, name, display_name, phone
                       FROM household_members
                       WHERE is_active = 1
                         AND (LOWER(name) = LOWER(?)
                              OR LOWER(display_name) = LOWER(?))
                       ORDER BY role DESC, id ASC
                       LIMIT 1""",
                    (name_query, name_query),
                ).fetchone()
                if row:
                    member_id = row["id"]
                    phone = explicit_phone or row["phone"]
                    member_name = row["display_name"] or row["name"]
                elif explicit_phone:
                    # name_query didn't match but phone is provided --
                    # treat name_query as a free-text greeting label.
                    phone = explicit_phone
                    member_name = name_query
                else:
                    return JSONResponse(
                        {"error": f"no active household member named '{name_query}'"},
                        status_code=404,
                    )
        else:
            # phone-only path: no household_members row, use a generic label
            phone = explicit_phone
            member_name = "there"

        # Normalize: ensure phone has a leading +
        if phone and phone[0] != "+" and phone[0].isdigit():
            phone = "+" + phone

        # Optional: regen the scheduled list before sending. Lifts the
        # inventory-model logic out so a brand-new list lands rather
        # than whatever stale draft happens to be sitting around.
        if do_regen:
            try:
                from cadence_calibration import refresh_observed_cadences
                from list_generate import list_generate as _generate
                from agent_brain import next_order_date as _next_order_date
                with app_state.get_db(row_factory=True) as conn:
                    refresh_observed_cadences(conn=conn)
                gen = _generate(target_date=_next_order_date())
                if gen and gen.get("list_id"):
                    list_id = int(gen["list_id"])
                log_event(
                    "admin_send_list_regen",
                    source="admin",
                    detail=f"member={member_name} new_list_id={list_id}",
                )
            except Exception as e:
                logger.exception("admin_send_list_regen failed")
                return JSONResponse(
                    {"error": f"regen failed: {str(e)[:160]}"}, status_code=500
                )

        # Resolve list_id if still missing.
        if not list_id:
            with app_state.get_db(row_factory=True) as conn:
                r = conn.execute(
                    """SELECT id FROM shopping_lists
                       WHERE status IN ('draft', 'confirmed')
                         AND (list_type = 'suggestion' OR list_type IS NULL)
                         AND dismissed_at IS NULL
                       ORDER BY id DESC LIMIT 1"""
                ).fetchone()
                if not r:
                    return JSONResponse(
                        {"error": "no active suggestion list found"},
                        status_code=404,
                    )
                list_id = r["id"]

        # Derive target_day from the list's target_date if caller didn't supply.
        if not target_day:
            try:
                with app_state.get_db(row_factory=True) as conn:
                    r = conn.execute(
                        "SELECT target_date FROM shopping_lists WHERE id = ?",
                        (list_id,),
                    ).fetchone()
                if r and r["target_date"]:
                    target_day = datetime.fromisoformat(r["target_date"]).strftime("%A")
            except Exception:
                pass

        from frontend_bridge import build_frontend_list
        from whatsapp_agent import format_cart_message
        from whatsapp_client import wa_client

        list_data = build_frontend_list(int(list_id))
        if variant == "category":
            from whatsapp_agent import format_cart_message_category
            rendered = format_cart_message_category(
                list_data,
                member_name=member_name,
                target_day=target_day,
                deliveries=deliveries,
            )
        else:
            rendered = format_cart_message(
                list_data,
                member_name=member_name,
                target_day=target_day,
                deliveries=deliveries,
                include_links=include_links,
            )
        wa_msg_id = await wa_client.send_message(phone, rendered, member_id=member_id)

        log_event(
            "admin_send_list_to_member",
            source="admin",
            detail=f"member={member_name} list_id={list_id} "
                   f"len={len(rendered)} sent={bool(wa_msg_id)}",
        )

        # Mask the phone in the response too -- the operator already
        # knows who they sent it to, no need to echo PII back.
        masked_phone = phone[:3] + "***" + phone[-3:] if len(phone) > 6 else "***"
        return {
            "ok": True,
            "member_name": member_name,
            "to_phone_masked": masked_phone,
            "list_id": int(list_id),
            "rendered_chars": len(rendered),
            "wa_message_id": wa_msg_id,
        }

    except Exception as e:
        logger.exception("admin_send_list_to_member failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/validate-list")
async def admin_validate_list(request: Request):
    """Session-authed: run the quality validator on a specific list.

    The validator already runs as the final step of list_generate, so
    this is the explicit-trigger path -- useful for re-validating an
    existing list after preferences or catalog data change.

    Body (JSON):
      { "list_id": 1110 }   // optional, defaults to latest draft
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        payload = {}

    list_id = payload.get("list_id")
    if not list_id:
        with app_state.get_db(row_factory=True) as conn:
            r = conn.execute(
                """SELECT id FROM shopping_lists
                   WHERE status IN ('draft', 'confirmed')
                     AND (list_type = 'suggestion' OR list_type IS NULL)
                     AND dismissed_at IS NULL
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if not r:
                return JSONResponse(
                    {"error": "no active suggestion list found"},
                    status_code=404,
                )
            list_id = r["id"]

    try:
        from list_quality_validator import validate_list_quality
        report = validate_list_quality(int(list_id))
        return report
    except Exception as e:
        logger.exception("admin_validate_list failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/evaluate-origin-relevance")
async def admin_evaluate_origin_relevance(request: Request):
    """Session-authed: run the LLM origin-relevance evaluator across
    items. Each item gets a single Haiku call evaluating "does showing
    country of origin help a shopper decide about THIS item?" -- the
    verdict is cached in items.origin_signal_llm forever (until force).

    Body (JSON, all optional):
      {
        "item_ids":     [1, 8, 22],    // explicit subset
        "list_id":      1110,          // evaluate items on this list
        "force":        false,         // re-eval even if already set
        "only_missing": true,          // skip items already evaluated
        "limit":        50             // cap per run
      }
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        payload = {}

    item_ids = payload.get("item_ids")
    list_id = payload.get("list_id")
    force = bool(payload.get("force", False))
    only_missing = bool(payload.get("only_missing", True))
    limit = max(1, min(200, int(payload.get("limit", 50))))

    # If a list_id was given but no explicit item_ids, expand to all
    # system-generated items on that list -- the common admin path.
    if list_id and not item_ids:
        try:
            with app_state.get_db(row_factory=True) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT item_id FROM list_items "
                    "WHERE list_id = ? AND item_id IS NOT NULL",
                    (int(list_id),),
                ).fetchall()
                item_ids = [r["item_id"] for r in rows]
        except Exception as e:
            return JSONResponse({"error": f"list expand failed: {e}"}, status_code=500)

    try:
        from item_origin_evaluator import evaluate_items
        # Inside FastAPI's running loop -- can't call asyncio.run().
        # Use await directly.
        result = await evaluate_items(
            item_ids=item_ids,
            force=force,
            only_missing=only_missing,
            limit=limit,
        )
        log_event(
            "admin_evaluate_origin_relevance",
            source="admin",
            detail=f"evaluated={result.get('evaluated')} "
                   f"list_id={list_id} force={force}",
        )
        return result
    except Exception as e:
        logger.exception("admin_evaluate_origin_relevance failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/whatsapp-allow")
async def admin_whatsapp_allow(request: Request):
    """Session-authed: toggle a household member's allow_whatsapp flag so the
    fail-closed send gate will permit (or stop permitting) that member's
    number — without editing the Ally .env or putting any number in the repo.
    Default stays fail-closed; this is an explicit per-member opt-in.
    Body: {member_name, allow: true|false}.
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    member_name = (payload.get("member_name") or "").strip()
    allow = bool(payload.get("allow", True))
    if not member_name:
        return JSONResponse({"error": "member_name required"}, status_code=400)
    try:
        with app_state.get_db(row_factory=True) as conn:
            row = conn.execute(
                """SELECT id, phone, name, display_name FROM household_members
                   WHERE is_active = 1
                     AND (LOWER(name) = LOWER(?) OR LOWER(display_name) = LOWER(?))
                   ORDER BY role DESC, id ASC LIMIT 1""",
                (member_name, member_name),
            ).fetchone()
            if not row:
                return JSONResponse(
                    {"error": f"member '{member_name}' not found"}, status_code=404)
            conn.execute(
                "UPDATE household_members SET allow_whatsapp = ? WHERE id = ?",
                (1 if allow else 0, row["id"]),
            )
            conn.commit()
        phone = row["phone"] or ""
        masked = (phone[:3] + "***" + phone[-3:]) if len(phone) > 6 else "***"
        log_event("whatsapp_allow_set", source="admin",
                  detail=f"member={row['name']} allow={allow} phone={masked}")
        return {"ok": True, "member": row["name"], "allow": allow, "to_phone_masked": masked}
    except Exception as e:
        logger.exception("admin_whatsapp_allow failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/send-message-to-member")
async def admin_send_message_to_member(request: Request):
    """Session-authed: send a plain-text WhatsApp message to a household
    member by name. For confirmations / replies (not list rendering).
    Body: {member_name, text}.
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    member_name = (payload.get("member_name") or "").strip()
    text = (payload.get("text") or "").strip()
    if not member_name or not text:
        return JSONResponse({"error": "member_name and text required"}, status_code=400)
    try:
        with app_state.get_db(row_factory=True) as conn:
            row = conn.execute(
                """SELECT id, phone, display_name, name FROM household_members
                   WHERE is_active = 1
                     AND (LOWER(name) = LOWER(?) OR LOWER(display_name) = LOWER(?))
                   ORDER BY role DESC, id ASC LIMIT 1""",
                (member_name, member_name),
            ).fetchone()
            if not row:
                return JSONResponse(
                    {"error": f"no active member named '{member_name}'"},
                    status_code=404,
                )
            phone = row["phone"]
            member_id = row["id"]
        from whatsapp_client import wa_client
        wa_msg_id = await wa_client.send_message(phone, text, member_id=member_id)
        masked = phone[:3] + "***" + phone[-3:] if len(phone) > 6 else "***"
        log_event("admin_send_message_to_member", source="admin",
                  detail=f"member={member_name} len={len(text)} sent={bool(wa_msg_id)}")
        return {"ok": True, "member": member_name, "to_phone_masked": masked,
                "wa_message_id": wa_msg_id}
    except Exception as e:
        logger.exception("admin_send_message_to_member failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/map-member-lid")
async def admin_map_member_lid(request: Request):
    """Session-authed: map a WhatsApp LID to a household member so their
    inbound replies resolve and get captured. Body: {member_name, lid}.
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    member_name = (payload.get("member_name") or "").strip()
    lid = (payload.get("lid") or "").strip()
    if not member_name or not lid:
        return JSONResponse({"error": "member_name and lid required"}, status_code=400)
    try:
        with app_state.get_db(row_factory=True) as conn:
            cur = conn.execute(
                "UPDATE household_members SET wa_lid = ? "
                "WHERE LOWER(name) = LOWER(?) OR LOWER(display_name) = LOWER(?)",
                (lid, member_name, member_name),
            )
            conn.commit()
            rows = cur.rowcount
        log_event("admin_map_member_lid", source="admin",
                  detail=f"member={member_name} lid_tail={lid[-6:]} rows={rows}")
        return {"ok": True, "member": member_name, "lid": lid, "rows_updated": rows}
    except Exception as e:
        logger.exception("admin_map_member_lid failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/apply-list-edits")
async def admin_apply_list_edits(request: Request):
    """Session-authed: apply a reviewer's edits to a list.

    Body (JSON):
      {
        "list_id":    1110,
        "have_names": ["Apple", "Carrots", ...],  // mark skipped (already has)
        "drop_names": ["X", ...],                 // mark skipped (don't want)
        "add_texts":  ["Organic Red Cabbage"],    // add new items
        "by":         "Sara"                       // who requested (audit)
      }

    Returns the structured list of actions carried out.
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    list_id = payload.get("list_id")
    if not list_id:
        return JSONResponse({"error": "list_id required"}, status_code=400)
    have_names = payload.get("have_names") or []
    drop_names = payload.get("drop_names") or []
    add_texts = payload.get("add_texts") or []
    by = (payload.get("by") or "reviewer").strip()

    actions = []
    try:
        with app_state.get_db(row_factory=True) as conn:
            def _skip(name, reason):
                cur = conn.execute(
                    """UPDATE list_items
                       SET status='skipped', user_action=?
                       WHERE list_id=? AND id IN (
                         SELECT li.id FROM list_items li
                         LEFT JOIN items i ON i.id = li.item_id
                         WHERE li.list_id=?
                           AND LOWER(COALESCE(i.name, li.item_name_text)) = LOWER(?)
                           AND COALESCE(li.status,'') != 'skipped'
                       )""",
                    (reason, list_id, list_id, name),
                )
                return cur.rowcount

            for nm in have_names:
                n = _skip(nm, "have_it")
                actions.append({"action": "dropped (already has)", "item": nm,
                                "applied": n > 0})
            for nm in drop_names:
                n = _skip(nm, "skipped")
                actions.append({"action": "dropped (not wanted)", "item": nm,
                                "applied": n > 0})
            conn.commit()

        # Adds resolve to a concrete catalog product FIRST (organic-aware,
        # noun-matched), then pin its catalog_id. Without this, free-text
        # adds (item_id=None, catalog_id=NULL) fall through to
        # frontend_bridge's Tier-3 name search, which matched
        # "Organic Red Cabbage" -> "Organic Red Chillies" on the shared
        # "organic red" tokens. Pinning catalog_id makes Tier-0
        # (_get_product_from_catalog) authoritative so the wrong product
        # can never re-enter via name search.
        from list_generate import list_add_item
        from list_quality_validator import resolve_freetext_item
        for text in add_texts:
            try:
                with app_state.get_db(row_factory=True) as rconn:
                    prod = resolve_freetext_item(text, rconn, prefers_organic=True)
                if prod:
                    li_id = list_add_item(
                        int(list_id),
                        prod.get("_clean_generic") or text, item_id=None,
                        catalog_id=prod.get("id"),
                        store_id=prod.get("store_id"),
                        estimated_price=prod.get("price"),
                        quantity=1,
                    )
                    actions.append({
                        "action": "added", "item": text,
                        "list_item_id": li_id, "applied": bool(li_id),
                        "resolved": {
                            "product_name": prod.get("product_name"),
                            "brand": prod.get("brand"),
                            "size": prod.get("size"),
                            "price": prod.get("price"),
                            "origin": prod.get("origin_country"),
                            "organic": bool(prod.get("is_organic")),
                            "catalog_id": prod.get("id"),
                            "store_id": prod.get("store_id"),
                            "match_reason": prod.get("_match_reason"),
                        },
                    })
                else:
                    # No clean catalog match -- still add it (don't lose the
                    # request) but flag for a manual pick rather than silently
                    # mis-matching.
                    li_id = list_add_item(int(list_id), text, item_id=None, quantity=1)
                    actions.append({
                        "action": "added", "item": text,
                        "list_item_id": li_id, "applied": bool(li_id),
                        "resolved": None,
                        "note": "no clean catalog match -- needs manual pick",
                    })
            except Exception as e:
                actions.append({"action": "added", "item": text,
                                "applied": False, "error": str(e)[:120]})

        log_event("admin_apply_list_edits", source="admin",
                  detail=f"list={list_id} by={by} "
                         f"have={len(have_names)} drop={len(drop_names)} "
                         f"add={len(add_texts)}")
        return {"ok": True, "list_id": int(list_id), "by": by, "actions": actions}
    except Exception as e:
        logger.exception("admin_apply_list_edits failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/reresolve-list-adds")
async def admin_reresolve_list_adds(request: Request):
    """Session-authed: re-run free-text resolution on a list's added
    items (item_id IS NULL) and re-pin catalog_id/store_id/price to the
    correct organic-aware, noun-matched product.

    Fixes legacy rows added before resolve_freetext_item existed -- e.g.
    a cabbage that mis-resolved to "Organic Red Chillies" because the
    whole phrase was name-searched. Idempotent: safe to re-run.

    Body: {list_id, list_item_id?(restrict to one row), prefers_organic?}.
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    list_id = payload.get("list_id")
    if not list_id:
        return JSONResponse({"error": "list_id required"}, status_code=400)
    only_item = payload.get("list_item_id")
    prefers_organic = bool(payload.get("prefers_organic", True))

    try:
        from list_quality_validator import resolve_freetext_item
        results = []
        with app_state.get_db(row_factory=True) as conn:
            q = ("SELECT id, item_name_text, catalog_id, store_id "
                 "FROM list_items WHERE list_id = ? AND item_id IS NULL "
                 "AND COALESCE(status,'') != 'skipped'")
            params = [int(list_id)]
            if only_item:
                q += " AND id = ?"
                params.append(int(only_item))
            rows = conn.execute(q, params).fetchall()

            for r in rows:
                text = (r["item_name_text"] or "").strip()
                if not text:
                    continue
                prod = resolve_freetext_item(text, conn, prefers_organic=prefers_organic)
                if prod:
                    conn.execute(
                        """UPDATE list_items
                           SET catalog_id = ?,
                               store_id = COALESCE(?, store_id),
                               estimated_price = ?,
                               item_name_text = ?
                           WHERE id = ?""",
                        (prod.get("id"), prod.get("store_id"),
                         prod.get("price"),
                         prod.get("_clean_generic") or text, r["id"]),
                    )
                    results.append({
                        "list_item_id": r["id"],
                        "asked_for": text,
                        "was_catalog_id": r["catalog_id"],
                        "resolved": {
                            "product_name": prod.get("product_name"),
                            "brand": prod.get("brand"),
                            "size": prod.get("size"),
                            "price": prod.get("price"),
                            "origin": prod.get("origin_country"),
                            "organic": bool(prod.get("is_organic")),
                            "catalog_id": prod.get("id"),
                            "store_id": prod.get("store_id"),
                            "match_reason": prod.get("_match_reason"),
                        },
                    })
                else:
                    results.append({
                        "list_item_id": r["id"], "asked_for": text,
                        "was_catalog_id": r["catalog_id"], "resolved": None,
                        "note": "no clean catalog match -- needs manual pick",
                    })
            conn.commit()

        log_event("admin_reresolve_list_adds", source="admin",
                  detail=f"list={list_id} rows={len(results)}")
        return {"ok": True, "list_id": int(list_id), "results": results}
    except Exception as e:
        logger.exception("admin_reresolve_list_adds failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/set-list-item-product")
async def admin_set_list_item_product(request: Request):
    """Session-authed: pin a SPECIFIC catalog product to a list item.
    Sets catalog_id + store_id + estimated_price from the chosen catalog
    row so frontend_bridge Tier-0 renders exactly that product. Used to
    override an auto-resolved free-text add with a human pick.

    Body: {list_item_id, catalog_id}.
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    li_id = payload.get("list_item_id")
    catalog_id = payload.get("catalog_id")
    if not li_id or not catalog_id:
        return JSONResponse(
            {"error": "list_item_id and catalog_id required"}, status_code=400)
    try:
        with app_state.get_db(row_factory=True) as conn:
            prod = conn.execute(
                """SELECT id, product_name, brand, size, price, is_organic,
                          origin_country, store_id
                   FROM product_catalog WHERE id = ?""",
                (int(catalog_id),),
            ).fetchone()
            if not prod:
                return JSONResponse({"error": "catalog_id not found"}, status_code=404)
            conn.execute(
                """UPDATE list_items
                   SET catalog_id = ?, store_id = ?, estimated_price = ?
                   WHERE id = ?""",
                (prod["id"], prod["store_id"], prod["price"], int(li_id)),
            )
            conn.commit()
        log_event("admin_set_list_item_product", source="admin",
                  detail=f"li={li_id} catalog={catalog_id}")
        return {
            "ok": True, "list_item_id": int(li_id),
            "pinned": {
                "catalog_id": prod["id"], "product_name": prod["product_name"],
                "brand": prod["brand"], "size": prod["size"],
                "price": prod["price"], "organic": bool(prod["is_organic"]),
                "origin": prod["origin_country"], "store_id": prod["store_id"],
            },
        }
    except Exception as e:
        logger.exception("admin_set_list_item_product failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/regenerate-suggestions")
async def admin_regenerate_suggestions(request: Request):
    """Session-authed force-regen of the Scheduled (suggestion) list.

    Same effect as the HMAC-authed /api/diag/regenerate-suggestions but
    reachable with a session cookie (the diag one needs the webhook
    secret). Refreshes observed cadences, regenerates for the next order
    date (which now supersedes stale drafts), marks the result as the
    active suggestion list, and returns counts.

    Body (optional): { "target_date": "YYYY-MM-DD" }.
    """
    body = await request.body()
    target_date = None
    if body:
        try:
            payload = json.loads(body)
            target_date = (payload.get("target_date") or "").strip() or None
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
    try:
        from cadence_calibration import refresh_observed_cadences
        cad = refresh_observed_cadences()
        from list_generate import list_generate
        from agent_brain import next_order_date as _next
        target = target_date or _next()
        result = list_generate(target_date=target)
        new_list_id = result.get("list_id") if result else None
        item_count = len(result.get("items", [])) if result else 0
        if new_list_id:
            with app_state.get_db() as conn:
                conn.execute(
                    "UPDATE shopping_lists SET list_type = 'suggestion', "
                    "source = 'admin_regen' WHERE id = ?",
                    (new_list_id,),
                )
        log_event("admin_regen_suggestions", source="admin",
                  detail=f"target={target} list_id={new_list_id} items={item_count}")
        return {"ok": True, "target_date": target, "new_list_id": new_list_id,
                "item_count": item_count, "cadence_stats": cad}
    except Exception as e:
        logger.exception("admin_regenerate_suggestions failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/rescore-alternatives")
async def admin_rescore_alternatives(request: Request):
    """Session-authed rebuild of the item_alternatives index against the CURRENT
    catalog using the CURRENT scorer + matcher gates.

    Needed after a matcher/scorer fix: the curator filters candidates on the
    STORED tier (it skips tier='avoid'), so a code fix alone has no effect until
    the index is re-scored. (The index had gone stale — every row last scored
    2026-02-27, before the noun/category gates and the is_organic-flag fix — so
    e.g. real cucumbers were stuck at 'avoid' and the curator returned eggplant.)
    Runs synchronously; expect ~30-60s for all items x stores.
    """
    try:
        from rescore_alternatives import rescore_store
        stats = []
        with app_state.get_db(row_factory=True) as conn:
            stores = conn.execute(
                "SELECT id, name FROM stores WHERE is_active = 1").fetchall()
            for s in stores:
                r = rescore_store(s["id"], conn=conn, verbose=False)
                stats.append({"store": s["name"], **r})
        log_event("admin_rescore_alternatives", source="admin",
                  detail="; ".join(
                      f"{x['store']}:{x['alternatives_scored']}sc/{x['preferred']}pref"
                      for x in stats))
        return {"ok": True, "stores": stats}
    except Exception as e:
        logger.exception("admin_rescore_alternatives failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/purge-alternatives")
async def admin_purge_alternatives(request: Request):
    """Session-authed: delete item_alternatives rows whose product now FAILS the
    matcher gates (foreign-produce-noun, prepared-form, ml-volume, etc). Cheap —
    no catalog re-search, unlike rescore — for clearing matcher pollution
    (e.g. Banana matching shallots / baby food / banana-milk drinks) immediately
    after a gate fix. Future rescores won't re-add it."""
    try:
        from product_knowledge import purge_gated_alternatives
        with app_state.get_db(row_factory=True) as conn:
            deleted, sample = purge_gated_alternatives(conn=conn)
        log_event("admin_purge_alternatives", source="admin",
                  detail=f"deleted={deleted}; e.g. " + " | ".join(sample[:6]))
        return {"ok": True, "deleted": deleted, "sample": sample}
    except Exception as e:
        logger.exception("admin_purge_alternatives failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/verify-list-availability")
async def admin_verify_list_availability(request: Request):
    """Session-authed: live-check availability + price for the products a list
    currently shows, writing the truth back to product_catalog so OOS items
    auto-swap to an in-stock alternative on the next render / cart build.

    Body (optional): { "list_id": int }. Defaults to the active Scheduled
    (suggestion) list. Launches a real browser on the Ally; expect ~30-60s.
    """
    body = await request.body()
    list_id = None
    if body:
        try:
            list_id = (json.loads(body) or {}).get("list_id")
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
    try:
        if not list_id:
            from agent_context import _active_suggestion_list_id
            with app_state.get_db(row_factory=True) as conn:
                list_id = _active_suggestion_list_id(conn)
        if not list_id:
            return JSONResponse({"error": "no active suggestion list"}, status_code=404)
        from live_verify import live_verify_list_availability
        result = await live_verify_list_availability(int(list_id))
        log_event("admin_verify_availability", source="admin",
                  detail=f"list={list_id} {result}")
        return {"ok": True, "list_id": int(list_id), "result": result}
    except Exception as e:
        logger.exception("admin_verify_list_availability failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/restore-recent-oos")
async def admin_restore_recent_oos(request: Request):
    """Session-authed recovery: re-mark products flagged unavailable in the last
    N minutes back to available. Undoes a bad live-verify run that mass-marked
    OOS off a failed store search. Body (optional): { "minutes": 15 }."""
    body = await request.body()
    minutes = 15
    if body:
        try:
            minutes = int((json.loads(body) or {}).get("minutes", 15))
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
    minutes = max(1, min(minutes, 1440))
    try:
        with app_state.get_db() as conn:
            cur = conn.execute(
                "UPDATE product_catalog SET is_available = 1 "
                "WHERE is_available = 0 AND scraped_at >= datetime('now', ?)",
                (f"-{minutes} minutes",),
            )
            n = cur.rowcount
        log_event("admin_restore_oos", source="admin",
                  detail=f"minutes={minutes} restored={n}")
        return {"ok": True, "minutes": minutes, "restored": n}
    except Exception as e:
        logger.exception("admin_restore_recent_oos failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/restore-store-availability")
async def admin_restore_store_availability(request: Request):
    """Session-authed recovery: re-mark a WHOLE store's products available, then
    re-index its alternatives. Use after a failed/incomplete catalog scrape
    wrongly marked the entire store is_available=0 (this is how Carrefour hit 0
    of 60,714). Unlike restore-recent-oos this is NOT time-windowed — the bad
    sweep doesn't bump scraped_at, so a recent-window recovery can't see it.
    Optimistically marks everything available; live-verify / cart-build re-flag
    anything genuinely out of stock.

    Body: { "store_id": int }  (required).
    """
    body = await request.body()
    store_id = None
    if body:
        try:
            store_id = (json.loads(body) or {}).get("store_id")
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
    if store_id is None:
        return JSONResponse({"error": "store_id required"}, status_code=400)
    try:
        store_id = int(store_id)
        with app_state.get_db() as conn:
            cur = conn.execute(
                "UPDATE product_catalog SET is_available = 1 "
                "WHERE store_id = ? AND is_available = 0",
                (store_id,),
            )
            restored = cur.rowcount
        # Re-index this store so item_alternatives reflects the recovery.
        from rescore_alternatives import rescore_store
        with app_state.get_db(row_factory=True) as conn:
            rr = rescore_store(store_id, conn=conn, verbose=False)
        log_event("admin_restore_store_availability", source="admin",
                  detail=f"store={store_id} restored={restored} "
                         f"alts={rr.get('alternatives_scored')}")
        return {"ok": True, "store_id": store_id, "restored": restored,
                "alternatives_scored": rr.get("alternatives_scored"),
                "preferred": rr.get("preferred")}
    except Exception as e:
        logger.exception("admin_restore_store_availability failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


def _summarize_cart_build(report, err):
    """One-line, user-facing summary of a build_cart result."""
    if err:
        low = err.lower()
        if "login" in low or "not logged" in low or "auth" in low:
            return ("Build needs you to log into the stores on the Ally "
                    "(close the regular Edge window, then retry).")
        return f"Build hit an error: {err}"
    if not report or report.get("error"):
        e = (report or {}).get("error", "unknown")
        if e == "availability_check":
            n = len((report.get("preflight") or {}).get("problems", []))
            return (f"Paused before building: {n} item(s) look unavailable "
                    "and need a swap/skip. Review in the app.")
        return f"Build issue: {e}"
    stores = report.get("stores", {})
    total_added = total_failed = 0
    parts = []
    for s, sr in stores.items():
        a = sr.get("items_added", 0)
        f = sr.get("items_failed", 0)
        total_added += a
        total_failed += f
        parts.append(f"{s}: {a} added" + (f", {f} failed" if f else ""))
    head = (f"{total_added} item(s) added"
            + (f", {total_failed} couldn't be added" if total_failed else ""))
    return head + (". " + "; ".join(parts) if parts else ".")


@app.post("/api/admin/build-cart")
async def admin_build_cart(request: Request):
    """Session-authed: build the cart from a list in the BACKGROUND (a real
    browser job on the Ally that adds items to the store carts; it always stops
    BEFORE checkout). Returns immediately; poll /api/admin/build-cart-status.
    Body (optional): { "list_id": int }. Defaults to the active Scheduled list."""
    body = await request.body()
    payload = {}
    if body:
        try:
            payload = json.loads(body) or {}
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
    list_id = payload.get("list_id")
    approve_all = bool(payload.get("approve_all"))
    try:
        if not list_id:
            from agent_context import _active_suggestion_list_id
            with app_state.get_db(row_factory=True) as conn:
                list_id = _active_suggestion_list_id(conn)
        if not list_id:
            return JSONResponse({"error": "no list to build"}, status_code=404)
        if app_state.cart_build_active:
            return JSONResponse({"error": "a build or scrape is already running"},
                                status_code=409)
        list_id = int(list_id)
        approved_n = 0
        if approve_all:
            # Mirror the app's "ready cart": approve every non-skipped item so the
            # whole list gets built, not just items already marked approved.
            with app_state.get_db() as conn:
                cur = conn.execute(
                    "UPDATE list_items SET status='approved', user_action='approved' "
                    "WHERE list_id=? AND COALESCE(status,'')!='skipped' "
                    "AND COALESCE(user_action,'')!='skipped'",
                    (list_id,))
                approved_n = cur.rowcount
        app_state.cart_build_active = True
        app_state.last_cart_build = {"status": "running", "list_id": list_id,
                                     "started_at": datetime.now().isoformat()}
        log_event("admin_build_cart_start", source="admin",
                  detail=f"list_id={list_id} approve_all={approve_all} approved={approved_n}")

        async def _run():
            report = None
            err = None
            try:
                from cart_builder import build_cart
                report = await build_cart(list_id)
            except Exception as e:
                err = str(e)[:300]
                logger.exception("admin background build_cart failed")
            finally:
                app_state.cart_build_active = False
            summary = _summarize_cart_build(report, err)
            app_state.last_cart_build = {
                "status": "error" if err else "done",
                "list_id": list_id, "error": err, "report": report,
                "summary": summary, "finished_at": datetime.now().isoformat(),
            }
            log_event("admin_build_cart_end", source="admin", detail=summary[:200])

        asyncio.create_task(_run())
        return {"started": True, "list_id": list_id, "approved": approved_n}
    except Exception as e:
        app_state.cart_build_active = False
        logger.exception("admin_build_cart failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/build-cart-status")
async def admin_build_cart_status():
    """Session-authed: poll the latest background cart build."""
    return {"ok": True, "active": app_state.cart_build_active,
            "build": app_state.last_cart_build}


@app.get("/api/admin/whatsapp-recipients")
async def admin_whatsapp_recipients():
    """Session-authed: list members allowed to receive WhatsApp (allow_whatsapp=1),
    name + masked phone — so a caller can confirm a message will only reach the
    intended (test) number before sending."""
    try:
        with app_state.get_db(row_factory=True) as conn:
            rows = conn.execute(
                "SELECT name, display_name, phone FROM household_members "
                "WHERE is_active = 1 AND allow_whatsapp = 1 ORDER BY id ASC"
            ).fetchall()
        out = []
        for r in rows:
            p = r["phone"] or ""
            out.append({"name": r["display_name"] or r["name"],
                        "phone_masked": (p[:3] + "***" + p[-3:]) if len(p) > 6 else "***"})
        return {"ok": True, "recipients": out}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/debug-search")
async def admin_debug_search(request: Request):
    """Session-authed: run ONE live store search and return the raw response, to
    debug why availability verification comes back empty. Launches a browser on
    the Ally (~15-40s). Body: { "store_id": 1|2, "query": "milk" }
    (store_id 1 = Kibsons, 2 = Carrefour)."""
    body = await request.body()
    payload = {}
    if body:
        try:
            payload = json.loads(body)
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
    try:
        from live_verify import debug_search
        store_id = int(payload.get("store_id", 1))
        query = (payload.get("query") or "milk").strip()
        pre = payload.get("pre_visit_store")
        pre = int(pre) if pre is not None else None
        fetch_path = payload.get("fetch_path") or None
        capture_search = bool(payload.get("capture_search"))
        out = await debug_search(store_id, query, pre_visit_store=pre,
                                 fetch_path=fetch_path, capture_search=capture_search)
        return {"ok": True, "result": out}
    except Exception as e:
        logger.exception("admin_debug_search failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/app-log")
async def admin_app_log(lines: int = 150, grep: str = ""):
    """Session-authed: tail the Python log file (hearthkeeper.log) for crash
    tracebacks / warnings not captured in app_events. Optional `grep` substring."""
    try:
        import collections
        from app_logging import LOG_FILE
        n = max(10, min(int(lines), 800))
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            tail = list(collections.deque(f, maxlen=n))
        if grep:
            g = grep.lower()
            tail = [ln for ln in tail if g in ln.lower()]
        return {"ok": True, "file": str(LOG_FILE), "count": len(tail), "lines": tail}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/add-household-rule")
async def admin_add_household_rule(request: Request):
    """Session-authed: add a durable household rule directly (no LLM).

    Body: {rule_type, category, brand?, note?}. rule_type is one of
    avoid_brand_in_category / prefer_brand_in_category / prefer_variety.
    Used to capture preferences stated in chat ("we like cola not pepsi").
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    rule_type = (payload.get("rule_type") or "").strip()
    category = (payload.get("category") or "").strip()
    brand = (payload.get("brand") or "").strip() or None
    note = (payload.get("note") or "").strip() or None
    valid = {"avoid_brand_in_category", "prefer_brand_in_category", "prefer_variety"}
    if rule_type not in valid or not category:
        return JSONResponse(
            {"error": f"rule_type must be one of {sorted(valid)} and category required"},
            status_code=400)
    try:
        from household_rules import add_household_rule
        added = add_household_rule(rule_type, category, brand=brand,
                                   source="chat", note=note)
        log_event("admin_add_household_rule", source="admin",
                  detail=f"{rule_type} brand={brand} cat={category} added={added}")
        return {"ok": True, "added": added, "rule_type": rule_type,
                "brand": brand, "category": category}
    except Exception as e:
        logger.exception("admin_add_household_rule failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/backfill-last-ordered")
async def admin_backfill_last_ordered(request: Request):
    """Session-authed one-time backfill: advance each item's
    last_ordered_date to the most recent finalized list that contained it,
    correcting stale dates left by incomplete historical order-item capture.
    Forward-only + idempotent. Returns the changes.
    """
    try:
        from list_generate import backfill_last_ordered_from_lists
        changes = backfill_last_ordered_from_lists()
        log_event("admin_backfill_last_ordered", source="admin",
                  detail=f"changed={len(changes)}")
        # Sort biggest corrections first for readability
        changes.sort(key=lambda c: (c.get("old") or ""))
        return {"ok": True, "changed": len(changes), "changes": changes}
    except Exception as e:
        logger.exception("admin_backfill_last_ordered failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/preflight-cart")
async def admin_preflight_cart(list_id: int):
    """Session-authed pre-build availability check for a list (no browser).

    Returns which items resolve to an in-stock product and which don't
    (with same/cross-vendor alternatives), so unavailable items can be
    swapped/skipped BEFORE the cart is built.
    """
    try:
        from cart_builder import preflight_cart
        result = preflight_cart(int(list_id))
        return result
    except Exception as e:
        logger.exception("admin_preflight_cart failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/delivery-analysis")
async def admin_delivery_analysis(list_id: int):
    """Session-authed: per-vendor delivery-fee picture for a list (subtotal,
    effective fee, shortfall to free delivery). Lightweight — no browser, no
    per-item availability resolution. Feeds the fee-aware cart UI."""
    try:
        from delivery_optimizer import analyze_list_delivery
        return analyze_list_delivery(int(list_id))
    except Exception as e:
        logger.exception("admin_delivery_analysis failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/price-intel")
async def admin_price_intel(category: str = None, item_id: int = None,
                            price: float = None, size: str = None,
                            name: str = None):
    """Session-authed price intelligence. ?item_id= returns that item's market +
    HISTORICAL (accepted) baselines, the learned organic preference, and the
    accepted price bands (organic / non-organic). Add &price=&size= to PROBE a
    candidate: cross-references our accepted band with our type preference and
    returns recommend/verdict (the 2-vs-9 AED/egg call). ?category= returns the
    category band + historical + what drives price (organic premium %, origin,
    brand). Read-only."""
    try:
        import price_intel as pi
        out = {}
        if item_id:
            iid = int(item_id)
            out["item_baseline"] = pi.item_price_baseline(iid)
            out["historical_baseline"] = pi.historical_baseline(iid)
            out["organic_preferred"] = pi.item_organic_preference(iid)
            out["accepted_band_organic"] = pi.accepted_band(
                iid, organic_required=True, category=category)
            out["accepted_band_conventional"] = pi.accepted_band(
                iid, organic_required=False, category=category)
            if price is not None and size:
                # default the probe to the item's preferred type, so a bare
                # price+size is judged as the type we'd actually buy
                probe_name = name
                if probe_name is None:
                    probe_name = "organic" if pi.item_organic_preference(iid) else ""
                out["probe"] = pi.evaluate_option(
                    iid, probe_name, float(price), size, category=category)
        if category:
            out["category_baseline"] = pi.category_price_baseline(category)
            out["category_historical"] = pi.category_historical_baseline(category)
            out["price_drivers"] = pi.attribute_price_effect(category)
        if not out:
            return JSONResponse(
                {"error": "pass ?item_id= or ?category="}, status_code=400)
        return out
    except Exception as e:
        logger.exception("admin_price_intel failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/price-callouts")
async def admin_price_callouts(list_id: int):
    """Session-authed: for each item on a list, check whether any AVAILABLE
    product is within our historically-accepted price band for the preferred
    type. Returns the items where nothing qualifies — a call-out plus the best
    next option — so a pricier-than-usual cycle is flagged, not silently
    accepted. Read-only."""
    try:
        import price_intel as pi
        return pi.list_price_callouts(int(list_id))
    except Exception as e:
        logger.exception("admin_price_callouts failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/vendor-savings")
async def admin_vendor_savings(list_id: int):
    """Session-authed: per-item cross-vendor price comparison for a list. For each
    item, compares the assigned vendor's preferred product against the preferred
    product at other vendors BY UNIT PRICE (same base unit, no organic
    downgrade), and surfaces cheaper options. Read-only — suggests, never
    switches. Feeds the 'buy this item cheaper at X' UI."""
    try:
        from vendor_select import analyze_list_vendor_savings
        return analyze_list_vendor_savings(int(list_id))
    except Exception as e:
        logger.exception("admin_vendor_savings failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/preview-order-notif")
async def admin_preview_order_notif(stage: str, order_ids: str):
    """Session-authed: render the exact order-notification text for a stage
    WITHOUT sending. stage in placed/on_the_way/delivered; order_ids is a
    comma-separated list of order ids. Lets the wording be reviewed before
    it ever goes to the household.
    """
    try:
        from order_notifications import render_stage, STAGES
        if stage not in STAGES:
            return JSONResponse(
                {"error": f"stage must be one of {list(STAGES)}"}, status_code=400)
        ids = [int(x) for x in str(order_ids).split(",") if x.strip().isdigit()]
        if not ids:
            return JSONResponse({"error": "order_ids required"}, status_code=400)
        text = render_stage(stage, ids)
        return {"stage": stage, "order_ids": ids, "text": text or "(no orders found)"}
    except Exception as e:
        logger.exception("admin_preview_order_notif failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/admin/test-send-order-notif")
async def admin_test_send_order_notif(request: Request):
    """Session-authed: actually SEND a stage notification (gated by
    ORDER_NOTIFS_TEST_ONLY -> curator only by default), so the wording +
    delivery path can be verified before going live. Does NOT touch the
    idempotent notif_*_at stamps. Body: {stage, order_ids:[...]}.
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    stage = (payload.get("stage") or "").strip()
    ids = [int(x) for x in (payload.get("order_ids") or []) if str(x).isdigit()]
    try:
        from order_notifications import send_stage, STAGES
        if stage not in STAGES:
            return JSONResponse(
                {"error": f"stage must be one of {list(STAGES)}"}, status_code=400)
        if not ids:
            return JSONResponse({"error": "order_ids required"}, status_code=400)
        res = await send_stage(stage, ids)
        return {"ok": True, "stage": stage, "order_ids": ids, **res}
    except Exception as e:
        logger.exception("admin_test_send_order_notif failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/whatsapp-messages")
async def admin_whatsapp_messages(member: str = "", direction: str = "",
                                  limit: int = 40, hours: int = 168):
    """Session-authed: authoritative dump of whatsapp_messages rows
    (NOT the lossy stdout log). Used to answer "what did X actually
    reply, and was it attributed to them?"

    Query params:
      member:    filter by household_members name/display_name (optional)
      direction: 'inbound' | 'outbound' (optional)
      limit:     max rows (default 40)
      hours:     look-back window (default 168 = 7 days)
    """
    try:
        rows_out = []
        with app_state.get_db(row_factory=True) as conn:
            where = ["wm.created_at >= datetime('now', ?)"]
            params = [f"-{int(hours)} hours"]
            if member:
                where.append(
                    "(LOWER(hm.name) = LOWER(?) OR LOWER(hm.display_name) = LOWER(?))"
                )
                params += [member, member]
            if direction:
                where.append("wm.direction = ?")
                params.append(direction)
            sql = (
                "SELECT wm.id, wm.created_at, wm.direction, wm.member_id, "
                "       wm.round_id, wm.message_type, wm.status, "
                "       wm.content, wm.wa_message_id, "
                "       hm.name AS member_name, hm.phone AS member_phone, "
                "       hm.wa_lid AS member_lid "
                "FROM whatsapp_messages wm "
                "LEFT JOIN household_members hm ON hm.id = wm.member_id "
                "WHERE " + " AND ".join(where) +
                " ORDER BY wm.id DESC LIMIT ?"
            )
            params.append(int(limit))
            for r in conn.execute(sql, params).fetchall():
                d = dict(r)
                # Truncate long outbound list bodies for readability
                if d.get("content") and len(d["content"]) > 400:
                    d["content"] = d["content"][:400] + "…"
                rows_out.append(d)
        return {"count": len(rows_out), "messages": rows_out}
    except Exception as e:
        logger.exception("admin_whatsapp_messages failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/category-audit")
async def admin_category_audit():
    """Session-authed: dump the household's tracked items grouped by
    category, with the attributes available for each. Used to design
    the per-category display schema (what attributes are relevant +
    populated for each category) instead of patching render output
    one item at a time.

    Returns:
      { categories: { CategoryName: {
            item_count, items: [ {name, brand, size, origin, organic,
            quality_tier, notes_excerpt} ],
            attribute_coverage: {brand: N, size: N, origin: N, organic: N}
        } } }
    """
    try:
        out = {"categories": {}}
        with app_state.get_db(row_factory=True) as conn:
            rows = conn.execute(
                """SELECT id, name, COALESCE(category,'Other') AS category,
                          brand, size, origin_country, quality_tier, notes,
                          subcategory
                   FROM items
                   ORDER BY category, name"""
            ).fetchall()
            from collections import defaultdict
            by_cat = defaultdict(list)
            for r in rows:
                by_cat[r["category"]].append(dict(r))
            for cat, items in by_cat.items():
                cov = {"brand": 0, "size": 0, "origin": 0, "organic_note": 0,
                       "subcategory": 0}
                item_list = []
                for it in items:
                    if (it.get("brand") or "").strip():
                        cov["brand"] += 1
                    if (it.get("size") or "").strip():
                        cov["size"] += 1
                    if (it.get("origin_country") or "").strip():
                        cov["origin"] += 1
                    if "organic" in (it.get("notes") or "").lower():
                        cov["organic_note"] += 1
                    if (it.get("subcategory") or "").strip():
                        cov["subcategory"] += 1
                    item_list.append({
                        "name": it["name"],
                        "brand": it.get("brand") or "",
                        "size": it.get("size") or "",
                        "origin": it.get("origin_country") or "",
                        "quality_tier": it.get("quality_tier") or "",
                        "subcategory": it.get("subcategory") or "",
                        "notes": (it.get("notes") or "")[:120],
                    })
                out["categories"][cat] = {
                    "item_count": len(items),
                    "attribute_coverage": cov,
                    "items": item_list,
                }
        return out
    except Exception as e:
        logger.exception("admin_category_audit failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/admin/explain-catalog-match")
async def admin_explain_catalog_match(item_name: str = "", item_id: int = 0):
    """Session-authed: explain why a given item resolved to the product
    it did. Pass either `item_name` (case-insensitive lookup) or
    `item_id`.

    Returns:
      item: the items row + search_terms parsed
      search_terms_by_store: { store_name: [terms] }
      candidates_by_term: { term: [top 5 product_catalog matches] }
      recent_match: the most recent list_item for this item with its
                    resolved product (suggestion_snapshots row)

    Use this to triage "why did the matcher pick the wrong product?"
    questions without needing SSH/DB access.
    """
    if not item_name and not item_id:
        return JSONResponse(
            {"error": "pass item_name or item_id"}, status_code=400
        )
    try:
        out = {"item": None, "search_terms_by_store": {},
               "candidates_by_term": {}, "recent_match": None,
               "household_preferences_relevant": []}
        with app_state.get_db(row_factory=True) as conn:
            if item_id:
                row = conn.execute(
                    "SELECT * FROM items WHERE id = ?", (item_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM items WHERE LOWER(name) = LOWER(?)",
                    (item_name,),
                ).fetchone()
            if not row:
                return JSONResponse({"error": "item not found"}, status_code=404)
            item = dict(row)
            out["item"] = {
                "id": item["id"],
                "name": item["name"],
                "category": item.get("category"),
                "preferred_store_id": item.get("preferred_store_id"),
                "notes": item.get("notes"),
                "avg_price": item.get("avg_price"),
                "quality_tier": item.get("quality_tier"),
            }
            search_terms = {}
            try:
                search_terms = json.loads(item.get("search_terms") or "{}")
            except Exception:
                search_terms = {}
            out["search_terms_by_store"] = search_terms

            # Pull the top candidates per search term, per store
            stores = conn.execute(
                "SELECT id, name FROM stores"
            ).fetchall()
            store_id_by_name = {s["name"]: s["id"] for s in stores}
            for store_name, terms in search_terms.items():
                store_id = store_id_by_name.get(store_name)
                if not store_id:
                    continue
                for term in (terms or [])[:5]:
                    cands = conn.execute(
                        """SELECT id, product_name, brand, size, price,
                                  is_organic, is_available, origin_country
                           FROM product_catalog
                           WHERE store_id = ?
                             AND (LOWER(product_name) LIKE LOWER(?)
                                  OR LOWER(COALESCE(brand,'')) LIKE LOWER(?))
                           ORDER BY is_organic DESC,
                                    is_available DESC,
                                    LENGTH(product_name) ASC
                           LIMIT 5""",
                        (store_id, f"%{term}%", f"%{term}%"),
                    ).fetchall()
                    out["candidates_by_term"].setdefault(store_name, {})[term] = [
                        {
                            "catalog_id": c["id"],
                            "product_name": c["product_name"],
                            "brand": c["brand"],
                            "size": c["size"],
                            "price": c["price"],
                            "is_organic": bool(c["is_organic"]),
                            "is_available": bool(c["is_available"]),
                            "origin": c["origin_country"],
                        }
                        for c in cands
                    ]

            # Most recent list_item for this item -> resolved product
            recent = conn.execute(
                """SELECT li.id AS list_item_id, li.list_id, li.created_at,
                          ss.original_catalog_id, ss.original_product_name,
                          pc.brand, pc.size, pc.is_organic, pc.is_available
                   FROM list_items li
                   LEFT JOIN suggestion_snapshots ss
                       ON ss.list_id = li.list_id AND ss.item_id = li.item_id
                   LEFT JOIN product_catalog pc
                       ON pc.id = ss.original_catalog_id
                   WHERE li.item_id = ?
                   ORDER BY li.id DESC LIMIT 1""",
                (item["id"],),
            ).fetchone()
            if recent:
                out["recent_match"] = dict(recent)

            # Household-wide organic preference (if any)
            prefs = conn.execute(
                """SELECT pref_key, pref_value FROM household_preferences
                   WHERE LOWER(pref_key) LIKE '%organic%'
                      OR LOWER(pref_value) LIKE '%organic%'"""
            ).fetchall()
            out["household_preferences_relevant"] = [
                {"key": p["pref_key"], "value": p["pref_value"]}
                for p in prefs
            ]

        return out
    except Exception as e:
        logger.exception("admin_explain_catalog_match failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/diag/record-external-purchase")
async def diag_record_external_purchase(request: Request):
    """HMAC-authed external-purchase recorder. Used for testing
    Slice 2 (the "just bought milk" channel) without typing into chat.

    Body (JSON):
      One of:
        { "text": "just bought milk and bread" }   (run the parser)
        { "raw_name": "milk", "purchase_date": "2026-05-26" }
                                                   (record directly)

    Returns the inserted row(s) with resolved item_id where matched.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured (GITHUB_WEBHOOK_SECRET unset)"},
            status_code=503,
        )

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        log_event("diag_ext_purchase_unauthorized", source="diag",
                  detail="HMAC verification failed", level="warning")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    text = payload.get("text")
    raw_name = payload.get("raw_name")
    purchase_date = payload.get("purchase_date")

    if not text and not raw_name:
        return JSONResponse(
            {"error": "supply 'text' (free-text) or 'raw_name'"},
            status_code=400,
        )

    try:
        from external_purchases import (
            record_external_purchase,
            record_many_from_text,
        )
        if text:
            result = record_many_from_text(text, source="diag")
            log_event("external_purchase", source="diag",
                      detail=f"text='{text[:80]}' parsed={result['parsed']} "
                             f"records={len(result['records'])}")
            return {"ok": True, "mode": "from_text", **result}
        else:
            rec = record_external_purchase(
                raw_name=raw_name,
                purchase_date=purchase_date,
                source="diag",
            )
            log_event("external_purchase", source="diag",
                      detail=f"direct raw_name='{raw_name}' item_id={rec.get('item_id') if rec else None}")
            return {"ok": True, "mode": "direct", "record": rec}
    except Exception as e:
        logger.exception("diag_record_external_purchase failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/diag/drop-list-item")
async def diag_drop_list_item(request: Request):
    """HMAC-authed Tier-0 drop trigger. Used for testing the
    "dropped items don't reappear next cycle" story without needing
    a WebSocket client.

    Body (JSON, required):
      {
        "list_item_id": 123,         (required)
        "source": "curator_drop",    (optional, default 'curator_drop')
        "reason": "have_it"          (optional)
      }

    Effect:
      * UPDATE list_items SET status='removed', user_action='skipped'
      * INSERT INTO skip_events
      * UPDATE restock_rules SET next_restock_date = (past next cycle)

    Returns the resolved item_id + new next_restock_date so tests can
    verify the push happened.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured (GITHUB_WEBHOOK_SECRET unset)"},
            status_code=503,
        )

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        log_event("diag_drop_unauthorized", source="diag",
                  detail="HMAC verification failed", level="warning")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    list_item_id = payload.get("list_item_id")
    if list_item_id is None:
        return JSONResponse(
            {"error": "list_item_id required"}, status_code=400
        )

    source = payload.get("source") or "curator_drop"
    reason = payload.get("reason")

    try:
        # Same path as handle_remove_item -- mark list_item removed,
        # log skip_event, push next_restock_date.
        from skip_tracking import apply_tier_zero_drop
        from list_generate import list_update_item

        # Mark list item as skipped (same DB effect as WS remove action).
        # list_update_item only knows 'approve' / 'skip' / 'modify' --
        # 'skip' sets status='skipped', user_action='skipped'.
        try:
            list_update_item(int(list_item_id), "skip")
        except Exception as e:
            logger.warning("list_update_item failed in diag drop: %s", e)

        result = apply_tier_zero_drop(
            list_item_id=int(list_item_id),
            source=source,
            reason=reason,
        )
        log_event("tier0_drop", source="diag",
                  detail=f"list_item={list_item_id} source={source} "
                         f"item={result.get('item_id')} "
                         f"next_restock={result.get('next_restock_date')}")
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("diag_drop_list_item failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


# Module-global store for the async Carrefour backfill diagnostic.
# The synchronous backfill (cold UC Chrome launch + per-order invoice
# pages with sleep(8) each) routinely exceeds Cloudflare's ~100s edge
# timeout, which returns HTTP 524 and DISCARDS the debug payload we need
# to see why extraction yields 0 items. `background:true` runs it in a
# thread and stashes the outcome here for a follow-up GET to read back.
# Single uvicorn worker (see uvicorn.run(app, ...)) makes a module global
# safe; we always REPLACE the dict wholesale, so a concurrent reader sees
# either the old or new dict, never a half-written one -- no lock needed.
_carrefour_backfill_state = {"status": "idle"}


def _run_carrefour_backfill_bg(order_id, dry_run):
    """Thread body for the background Carrefour detail backfill."""
    global _carrefour_backfill_state
    from order_scraper import backfill_carrefour_order_items
    try:
        result = backfill_carrefour_order_items(order_id=order_id, dry_run=dry_run)
        _carrefour_backfill_state = {
            "status": "done",
            "dry_run": dry_run,
            "order_id": order_id,
            **result,
        }
        log_event("diag_carrefour_detail_done", source="diag",
                  detail=f"orders={result.get('orders_processed', 0)} "
                         f"items_inserted={result.get('items_inserted', 0)} "
                         f"errors={len(result.get('errors', []))} "
                         f"dry_run={dry_run} bg=1")
    except Exception as e:
        logger.exception("carrefour backfill bg thread failed")
        _carrefour_backfill_state = {
            "status": "error",
            "error": str(e)[:300],
            "dry_run": dry_run,
            "order_id": order_id,
        }


@app.post("/api/diag/scrape-carrefour-detail")
async def diag_scrape_carrefour_detail(request: Request):
    """HMAC-authed Carrefour order-detail re-scrape.

    Re-runs the order-invoice page extractor for ALL delivered
    Carrefour orders with zero order_items rows (the known scraper
    gap). Or, with body { "order_id": N }, for one specific order
    only (useful when iterating on the DOM extraction).

    Body (optional JSON):
      { "order_id": 123, "dry_run": false }

    Operationally:
      * Acquires the same UC Chrome browser lock as the regular
        order poller. If a cart build is in progress, returns an
        error rather than fighting for the browser.
      * Polite 2s delay between detail pages.
      * Calls enrich_new_order_items after insert so cadence
        calculations pick the new rows up.
    """
    # Accept EITHER a valid GitHub-style HMAC signature (for headless curl)
    # OR a valid app session cookie (so the logged-in household / dashboard
    # can trigger it the same way as the "Run scrape now" button).
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    _hmac_ok = bool(GITHUB_WEBHOOK_SECRET) and _verify_github_signature(body, sig)
    _session_ok = _verify_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not (_hmac_ok or _session_ok):
        log_event("diag_carrefour_detail_unauthorized", source="diag",
                  detail="no valid HMAC or session", level="warning")
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    order_id = None
    dry_run = False
    background = False
    if body:
        try:
            payload = json.loads(body)
            raw_id = payload.get("order_id")
            if raw_id is not None:
                order_id = int(raw_id)
            dry_run = bool(payload.get("dry_run", False))
            background = bool(payload.get("background", False))
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)

    # Background mode: the synchronous scrape blows past Cloudflare's ~100s
    # edge timeout (HTTP 524) and the debug payload is lost. Run it in a
    # thread and let the caller poll /api/diag/carrefour-backfill-result.
    if background:
        global _carrefour_backfill_state
        if _carrefour_backfill_state.get("status") == "running":
            return JSONResponse(
                {"error": "backfill already running",
                 "state": _carrefour_backfill_state},
                status_code=409,
            )
        _carrefour_backfill_state = {
            "status": "running", "dry_run": dry_run, "order_id": order_id,
        }
        import threading
        threading.Thread(
            target=_run_carrefour_backfill_bg,
            args=(order_id, dry_run),
            daemon=True,
        ).start()
        log_event("diag_carrefour_detail_started", source="diag",
                  detail=f"order_id={order_id} dry_run={dry_run} bg=1")
        return {"ok": True, "status": "running",
                "poll": "/api/diag/carrefour-backfill-result"}

    try:
        # Selenium is blocking. asyncio.to_thread keeps the event
        # loop responsive (cart_build_active flag still gates against
        # concurrent UC sessions).
        from order_scraper import backfill_carrefour_order_items
        result = await asyncio.to_thread(
            backfill_carrefour_order_items,
            order_id=order_id,
            dry_run=dry_run,
        )
        log_event("diag_carrefour_detail_done", source="diag",
                  detail=f"orders={result.get('orders_processed', 0)} "
                         f"items_inserted={result.get('items_inserted', 0)} "
                         f"errors={len(result.get('errors', []))} "
                         f"dry_run={dry_run}")
        return {"ok": True, "dry_run": dry_run, **result}
    except Exception as e:
        logger.exception("diag_scrape_carrefour_detail failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/carrefour-backfill-result")
async def diag_carrefour_backfill_result(request: Request):
    """Read the outcome of the most recent BACKGROUND Carrefour detail backfill.

    Pairs with POST /api/diag/scrape-carrefour-detail {background:true}.
    Returns {"status": "idle"|"running"|"done"|"error", ...}; on "done"
    the payload includes debug_per_order (productLinkCount, firstFewLinks,
    page title, used_url) so Carrefour DOM drift is visible WITHOUT holding
    an HTTP connection open through the Cloudflare tunnel for 100s+.
    """
    sig = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()
    _hmac_ok = bool(GITHUB_WEBHOOK_SECRET) and _verify_github_signature(body, sig)
    _session_ok = _verify_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not (_hmac_ok or _session_ok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return _carrefour_backfill_state


@app.get("/api/diag/orders-missing-items")
async def diag_orders_missing_items(request: Request):
    """Read-only: delivered/completed orders that captured 0 order_items rows.

    These are the orders invisible to the consumption model (their purchases
    were never line-item scraped). The aggregate count drives
    procurement-analysis.coverage_warning; this endpoint names the actual
    orders -- store, ref, dates, total, detail_url, age -- so a backfill can
    target them, and so a stale/ancient miss can be distinguished from a
    fresh scraper regression. Session OR HMAC auth.
    """
    sig = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()
    _hmac_ok = bool(GITHUB_WEBHOOK_SECRET) and _verify_github_signature(body, sig)
    _session_ok = _verify_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not (_hmac_ok or _session_ok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        with app_state.get_db(row_factory=True) as conn:
            rows = conn.execute(
                """SELECT o.id, s.name AS store, o.order_ref, o.status,
                          o.order_date, o.delivery_date, o.total_amount,
                          o.detail_url,
                          CAST(julianday('now') - julianday(
                              COALESCE(o.delivery_date, o.order_date)) AS INT)
                            AS age_days
                   FROM orders o JOIN stores s ON s.id = o.store_id
                   WHERE o.status IN ('delivered', 'completed')
                     AND NOT EXISTS (
                         SELECT 1 FROM order_items oi WHERE oi.order_id = o.id)
                   ORDER BY COALESCE(o.delivery_date, o.order_date) DESC"""
            ).fetchall()
        return {"count": len(rows), "orders": [dict(r) for r in rows]}
    except Exception as e:
        logger.exception("diag_orders_missing_items failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/item-purchases")
async def diag_item_purchases(request: Request, item_id: int = None, name: str = None):
    """Read-only: every order_item + external_purchase linked to an item, so an
    enrichment MIS-LINK (a different product matched onto this items row) can be
    told apart from a genuine purchase. Pass ?item_id=N or ?name=substring.
    Session OR HMAC auth.
    """
    sig = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()
    _hmac_ok = bool(GITHUB_WEBHOOK_SECRET) and _verify_github_signature(body, sig)
    _session_ok = _verify_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not (_hmac_ok or _session_ok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        with app_state.get_db(row_factory=True) as conn:
            if item_id is None and name:
                r = conn.execute(
                    "SELECT id FROM items WHERE LOWER(name) LIKE LOWER(?) ORDER BY id LIMIT 1",
                    (f"%{name}%",)).fetchone()
                item_id = r["id"] if r else None
            if item_id is None:
                return JSONResponse({"error": "item_id or name required"}, status_code=400)
            item = conn.execute("SELECT id, name, category FROM items WHERE id = ?",
                                (item_id,)).fetchone()
            oi = conn.execute(
                """SELECT oi.raw_name, oi.brand, oi.quantity, oi.unit_price,
                          o.id AS order_id, s.name AS store, o.order_date,
                          o.delivery_date, o.status
                   FROM order_items oi
                   JOIN orders o ON o.id = oi.order_id
                   JOIN stores s ON s.id = o.store_id
                   WHERE oi.item_id = ?
                   ORDER BY o.id DESC""", (item_id,)).fetchall()
            ext = conn.execute(
                """SELECT id, raw_name, quantity, purchase_date, source, confidence
                   FROM external_purchases WHERE item_id = ? ORDER BY id DESC""",
                (item_id,)).fetchall()
        return {
            "item": dict(item) if item else None,
            "order_items": [dict(r) for r in oi],
            "external_purchases": [dict(r) for r in ext],
            "counts": {"order_items": len(oi), "external_purchases": len(ext)},
        }
    except Exception as e:
        logger.exception("diag_item_purchases failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/diag/auto-grow-items")
async def diag_auto_grow_items(request: Request):
    """HMAC-authed auto-grow of the items table from order history.

    Body (optional JSON):
      { "dry_run": true, "min_purchases": 3, "max_new": 25, "days": 180 }

    Returns the list of items that were created (or would be created
    in dry_run mode), with per-item cadence + category + purchase
    count. Read auto_grow_items.py docstring for the rules.

    Operationally:
      * dry_run defaults to FALSE -- this commits by default. Set
        dry_run=true to preview without changes.
      * After a successful apply you'll typically want to also call
        /api/diag/regenerate-suggestions so the new items show up
        on Scheduled immediately.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured (GITHUB_WEBHOOK_SECRET unset)"},
            status_code=503,
        )

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        log_event("diag_grow_unauthorized", source="diag",
                  detail="HMAC verification failed", level="warning")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    dry_run = False
    min_purchases = 3
    max_new = 25
    days = 180
    if body:
        try:
            payload = json.loads(body)
            dry_run = bool(payload.get("dry_run", False))
            min_purchases = max(2, min(10, int(payload.get("min_purchases", 3))))
            max_new = max(1, min(100, int(payload.get("max_new", 25))))
            days = max(7, min(365, int(payload.get("days", 180))))
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)

    try:
        from auto_grow_items import grow_items_from_history
        result = grow_items_from_history(
            min_purchases=min_purchases,
            max_new=max_new,
            days_window=days,
            dry_run=dry_run,
        )
        log_event("diag_grow_done", source="diag",
                  detail=f"created={len(result)} dry_run={dry_run} "
                         f"min_purchases={min_purchases} max_new={max_new}")
        return {
            "ok": True,
            "dry_run": dry_run,
            "created": len(result),
            "items": result,
        }
    except Exception as e:
        logger.exception("diag_auto_grow_items failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/diag/regenerate-suggestions")
async def diag_regenerate_suggestions(request: Request):
    """HMAC-authed force-regen of the Scheduled (suggestion) list.

    Runs cadence_calibration.refresh_observed_cadences() first so the
    next_restock_date numbers reflect actual purchase behaviour, then
    calls list_generate() and marks the result as the active
    suggestion list. Returns counts for the caller.

    Body (optional JSON): { "target_date": "YYYY-MM-DD" } -- defaults
    to next_order_date().

    Fail-closed: returns 503 if GITHUB_WEBHOOK_SECRET is unset.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured (GITHUB_WEBHOOK_SECRET unset)"},
            status_code=503,
        )

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        log_event("diag_regen_unauthorized", source="diag",
                  detail="HMAC verification failed", level="warning")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    target_date = None
    if body:
        try:
            payload = json.loads(body)
            target_date = (payload.get("target_date") or "").strip() or None
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)

    try:
        # 1. Cadence calibration -- corrects the 3-10x predictor over-eagerness.
        from cadence_calibration import refresh_observed_cadences
        cad = refresh_observed_cadences()

        # 2. Force generate. list_generate also calls cadence refresh,
        # but doing it here gives us the stats in the response.
        from list_generate import list_generate
        from agent_brain import next_order_date as _next
        target = target_date or _next()
        result = list_generate(target_date=target)

        new_list_id = result.get("list_id") if result else None
        item_count = len(result.get("items", [])) if result else 0

        # 3. Mark as suggestion in the DB so restore_session finds it.
        if new_list_id:
            try:
                with app_state.get_db() as conn:
                    conn.execute(
                        "UPDATE shopping_lists SET list_type = 'suggestion', source = 'auto_diag' WHERE id = ?",
                        (new_list_id,),
                    )
            except Exception as e:
                logger.warning("Failed to mark list as suggestion: %s", e)

        log_event("diag_regen_done", source="diag",
                  detail=f"target={target} list_id={new_list_id} items={item_count} "
                         f"cadence_updated={cad['updated']}")

        return {
            "ok": True,
            "target_date": target,
            "new_list_id": new_list_id,
            "item_count": item_count,
            "cadence_stats": cad,
        }
    except Exception as e:
        logger.exception("diag_regenerate_suggestions failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/diag/run-pattern-detector")
async def diag_run_pattern_detector(request: Request):
    """HMAC-authed run of the Tier-1/Tier-2 drop-pattern detector.

    Reads skip_events history and either nudges restock_rules.cadence_adjustment
    (soft / firmer tiers) or inserts user_item_prefs(pref_type='stop_suggesting')
    for items that keep getting dropped (auto-stop tier). See
    pattern_detector.py for the full tier definitions.

    Body (optional JSON):
      {
        "dry_run":  bool,  // default false  -- no writes if true
        "days":     int,   // default 30     -- skip_events lookback
        "send_asks": bool  // default FALSE  -- whether to also fire
                           //   E4 Tier-2 WhatsApp asks. Default off
                           //   for the diag path so the manual
                           //   trigger never blasts unintended
                           //   messages. The nightly scheduler runs
                           //   with send_asks=True.
      }

    Idempotent on the apply path; idempotent on the ask path too
    because user_item_prefs(pref_type='tier2_asked') is checked
    before sending.

    Fail-closed: returns 503 if GITHUB_WEBHOOK_SECRET is unset.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured (GITHUB_WEBHOOK_SECRET unset)"},
            status_code=503,
        )

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        log_event("diag_pattern_unauthorized", source="diag",
                  detail="HMAC verification failed", level="warning")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    dry_run = False
    days = 30
    send_asks = False  # diag defaults safe-off; scheduler defaults on
    if body:
        try:
            payload = json.loads(body)
            dry_run = bool(payload.get("dry_run", False))
            days = max(1, min(365, int(payload.get("days", 30))))
            send_asks = bool(payload.get("send_asks", False))
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)

    try:
        from pattern_detector import run as run_pattern_detector
        result = run_pattern_detector(
            days=days, dry_run=dry_run, send_asks=send_asks,
        )
        log_event(
            "diag_pattern_done",
            source="diag",
            detail=(
                f"dry_run={dry_run} days={days} send_asks={send_asks} "
                f"soft={result['summary'].get('soft', 0)} "
                f"firmer={result['summary'].get('firmer', 0)} "
                f"auto_stop={result['summary'].get('auto_stop', 0)} "
                f"asks_sent={result['summary'].get('asks_sent', 0)}"
            ),
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("diag_run_pattern_detector failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/cart-attempts")
async def diag_cart_attempts(list_id: int = None, limit: int = 100,
                             hours: int = 48):
    """Per-item forensic trail of recent cart-build attempts.

    Reads the cart_attempts table that the builder now writes on every
    add (success or fail). Lets us answer "WHY did this build fail
    10/20 items?" after the fact.

    Query params:
      list_id  -- optional. If given, all attempts for that list.
                  Otherwise, all attempts in the last `hours`.
      limit    -- 1..500, default 100.
      hours    -- 1..168, default 48. Ignored when list_id is set.

    Returns: { attempts: [...], summary: {total, success, failed,
              by_class: {oos: N, not_found: N, ...}}, stores: {...} }

    Auth-exempt (read-only aggregates + reason strings; no PII /
    no chat). Mirrors the recent-errors model.
    """
    limit = max(1, min(500, limit))
    hours = max(1, min(168, hours))
    try:
        with app_state.get_db(row_factory=True) as conn:
            if list_id:
                rows = conn.execute(
                    """SELECT ca.*, i.name AS item_name, s.name AS store_name
                       FROM cart_attempts ca
                       LEFT JOIN items i ON i.id = ca.item_id
                       LEFT JOIN stores s ON s.id = ca.store_id
                       WHERE ca.list_id = ?
                       ORDER BY ca.created_at DESC, ca.attempt_n ASC
                       LIMIT ?""",
                    (list_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT ca.*, i.name AS item_name, s.name AS store_name
                        FROM cart_attempts ca
                        LEFT JOIN items i ON i.id = ca.item_id
                        LEFT JOIN stores s ON s.id = ca.store_id
                        WHERE ca.created_at >= datetime('now', '-{hours} hours')
                        ORDER BY ca.created_at DESC, ca.attempt_n ASC
                        LIMIT ?""",
                    (limit,),
                ).fetchall()
        attempts = [dict(r) for r in rows]
        summary = {
            "total": len(attempts),
            "success": sum(1 for a in attempts if a["success"]),
            "failed": sum(1 for a in attempts if not a["success"]),
            "by_class": {},
            "by_store": {},
        }
        for a in attempts:
            cls = a.get("reason_class") or "ok"
            summary["by_class"][cls] = summary["by_class"].get(cls, 0) + 1
            st = a.get("store_name") or "?"
            summary["by_store"].setdefault(st, {"total": 0, "success": 0, "failed": 0})
            summary["by_store"][st]["total"] += 1
            if a["success"]:
                summary["by_store"][st]["success"] += 1
            else:
                summary["by_store"][st]["failed"] += 1
        return {"summary": summary, "attempts": attempts}
    except Exception as e:
        logger.exception("diag_cart_attempts failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/forgotten-items")
async def diag_forgotten_items(request: Request):
    """A2 — items the household normally buys but aren't on the
    active list. Sorted by how-overdue.

    Query params:
        list_id (optional): which list to compare against. If
            omitted, picks the most recent draft/suggestion list.
        limit (optional, default 20, max 100).

    GET, no body, no auth -- read-only, surfaces only item names +
    cadence metadata that the in-app UI already exposes elsewhere.
    """
    try:
        list_id = request.query_params.get("list_id")
        if list_id is not None:
            list_id = int(list_id)
        limit = int(request.query_params.get("limit") or 20)
        if limit < 1 or limit > 100:
            limit = 20
        from list_generate import get_forgotten_items
        with app_state.get_db(row_factory=True) as conn:
            items = get_forgotten_items(
                list_id=list_id, limit=limit, conn=conn,
            )
        return {"list_id": list_id, "count": len(items), "items": items}
    except ValueError:
        return JSONResponse({"error": "bad list_id or limit"}, status_code=400)
    except Exception as e:
        logger.exception("diag_forgotten_items failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/diag/observability")
async def diag_observability():
    """Wave 7 -- unified system-visibility snapshot for the admin dashboard.

    Aggregates everything the curator needs to know at-a-glance:
      * Cart-build success rate per store over the last 7 days
        (from cart_attempts)
      * Notification delivery counts per B-series channel over 7d
        (from app_events sent_at)
      * LLM spend today + month against the cap (from llm_usage)
      * Last 24h error counts grouped by source (from app_events)
      * Last-run timestamps for each scheduler task (from app_events)

    GET, no body, no auth. Read-only aggregates; no row content is
    exposed. Safe to expose under the dashboard's existing trust
    boundary.
    """
    out = {
        "cart_success": {},
        "notifications": {},
        "llm": {},
        "errors_24h": {},
        "scheduler_heartbeats": {},
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with app_state.get_db(row_factory=True) as conn:
            # --- Cart success (last 7 days) ---
            try:
                rows = conn.execute(
                    """SELECT s.name AS store, ca.success,
                              COUNT(*) AS n
                       FROM cart_attempts ca
                       JOIN stores s ON s.id = ca.store_id
                       WHERE ca.created_at >= datetime('now', '-7 days')
                       GROUP BY s.name, ca.success
                       ORDER BY s.name"""
                ).fetchall()
                by_store = {}
                for r in rows:
                    s = r["store"]
                    by_store.setdefault(s, {"attempted": 0, "succeeded": 0})
                    by_store[s]["attempted"] += r["n"]
                    if r["success"]:
                        by_store[s]["succeeded"] += r["n"]
                for s, stats in by_store.items():
                    stats["rate"] = (
                        stats["succeeded"] / stats["attempted"]
                        if stats["attempted"] else 0
                    )
                out["cart_success"]["by_store_7d"] = by_store
                fail_classes = conn.execute(
                    """SELECT reason_class, COUNT(*) AS n
                       FROM cart_attempts
                       WHERE success = 0
                         AND created_at >= datetime('now', '-7 days')
                       GROUP BY reason_class ORDER BY n DESC LIMIT 6"""
                ).fetchall()
                out["cart_success"]["top_failure_classes_7d"] = [
                    {"class": r["reason_class"], "count": r["n"]}
                    for r in fail_classes
                ]
            except Exception as e:
                out["cart_success"]["error"] = str(e)[:120]

            # --- Notifications (last 7 days, per B-channel) ---
            try:
                channels = {
                    "b2_order_placed": "b2_order_placed_sent",
                    "b3_dispatched":   "status_update_sent",
                    "b4_today_deliveries": "b4_today_summary_sent",
                    "b5_delivered":    "delivered_prompt_sent",
                    "b6_reminder":     "rating_reminder_sent",
                }
                for label, ev in channels.items():
                    row = conn.execute(
                        """SELECT COUNT(*) AS n, MAX(timestamp) AS last_at
                           FROM app_events
                           WHERE event = ?
                             AND timestamp >= datetime('now', '-7 days')""",
                        (ev,),
                    ).fetchone()
                    out["notifications"][label] = {
                        "sent_7d": row["n"] or 0,
                        "last_at": row["last_at"],
                    }
            except Exception as e:
                out["notifications"]["error"] = str(e)[:120]

            # --- LLM spend status (delegate to llm_budget) ---
            try:
                from llm_budget import get_usage_summary, get_budget_status
                out["llm"]["usage"] = get_usage_summary()
                out["llm"]["status"] = get_budget_status()
            except Exception as e:
                out["llm"]["error"] = str(e)[:120]

            # --- Errors last 24h ---
            try:
                rows = conn.execute(
                    """SELECT source, COUNT(*) AS n
                       FROM app_events
                       WHERE level = 'error'
                         AND timestamp >= datetime('now', '-1 day')
                       GROUP BY source ORDER BY n DESC LIMIT 10"""
                ).fetchall()
                total = sum(r["n"] for r in rows)
                out["errors_24h"] = {
                    "total": total,
                    "by_source": {r["source"]: r["n"] for r in rows},
                }
            except Exception as e:
                out["errors_24h"]["error"] = str(e)[:120]

            # --- Scheduler heartbeats (most recent log per task) ---
            # We look for the *done* / *sent* events each task emits.
            try:
                heartbeats = {
                    "_delivered_rating_dispatcher": "delivered_prompt_sent",
                    "_status_update_dispatcher": "status_update_sent",
                    "_rating_reminder_dispatcher": "rating_reminder_sent",
                    "_today_deliveries_morning_dispatcher": "b4_today_summary_sent",
                    "_nightly_pattern_detector": "pattern_detector_done",
                    "_nightly_whatsapp_restart": "wa_nightly_restart_done",
                    "_nightly_catalog_scraper": "catalog_scrape_done",
                    "_order_status_poller": "order_poller_done",
                }
                for task, ev in heartbeats.items():
                    row = conn.execute(
                        "SELECT MAX(timestamp) AS last_at FROM app_events "
                        "WHERE event = ?",
                        (ev,),
                    ).fetchone()
                    out["scheduler_heartbeats"][task] = row["last_at"]
            except Exception as e:
                out["scheduler_heartbeats"]["error"] = str(e)[:120]
    except Exception as e:
        logger.exception("diag_observability failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)
    return out


@app.get("/api/diag/llm-usage")
async def diag_llm_usage():
    """Read-only spend snapshot for the LLM budget cap.

    Returns today/MTD totals + remaining headroom against each cap,
    plus a breakdown by model and intent. Also reports the CURRENT
    budget status (`ok`, `reason`) so you can tell at a glance
    whether the cap is actively blocking calls -- and a count of
    `llm_budget_exceeded` skip events from the last 24h so you can
    see how often the cap fired.

    GET, no body, no auth -- read-only, no PII. The numbers are
    just COUNT/SUM aggregates over llm_usage + events; no message
    content is exposed.
    """
    try:
        from llm_budget import get_usage_summary, get_budget_status
        base = get_usage_summary()
        base["current_status"] = get_budget_status()

        # Add a recent-skips count so we can see how often the cap
        # has fired in the last 24h, broken down by reason. Reads
        # the app_events log (which log_event writes to) and
        # parses `reason=...` out of the detail string.
        skips = {"daily_cap": 0, "monthly_cap": 0, "db_error": 0, "other": 0}
        last_skip = None
        try:
            with app_state.get_db() as conn:
                rows = conn.execute(
                    """SELECT detail, timestamp FROM app_events
                       WHERE event = 'llm_budget_exceeded'
                         AND timestamp >= datetime('now', '-1 day')
                       ORDER BY timestamp DESC""",
                ).fetchall()
                for r in rows:
                    detail = r["detail"] or ""
                    matched = False
                    for k in ("daily_cap", "monthly_cap", "db_error"):
                        if f"reason={k}" in detail:
                            skips[k] += 1
                            matched = True
                            break
                    if not matched:
                        skips["other"] += 1
                    if last_skip is None:
                        last_skip = {
                            "at": r["timestamp"],
                            "detail": detail[:200],
                        }
        except Exception as e:
            logger.warning("diag_llm_usage skip-count failed: %s", e)

        base["recent_skips_24h"] = {
            "total": sum(skips.values()),
            "by_reason": skips,
            "last_skip": last_skip,
        }
        return base
    except Exception as e:
        logger.exception("diag_llm_usage failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/diag/restart-whatsapp")
async def diag_restart_whatsapp(request: Request):
    """HMAC-authed trigger to restart the WhatsApp sidecar process.

    Same auth model as the other diag endpoints: HMAC-SHA256 over
    GITHUB_WEBHOOK_SECRET via X-Hub-Signature-256.

    Body (optional JSON):  { "reason": "manual-test" }

    Returns: { ok, ack } where `ack` is the sidecar's response
    (true if it accepted /restart, false otherwise). NSSM brings
    the process back up; expect ~10-15s before is_connected()
    returns true again.

    Fail-closed: returns 503 if GITHUB_WEBHOOK_SECRET is unset.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured (GITHUB_WEBHOOK_SECRET unset)"},
            status_code=503,
        )

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        log_event("diag_wa_restart_unauthorized", source="diag",
                  detail="HMAC verification failed", level="warning")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    reason = "manual"
    if body:
        try:
            payload = json.loads(body)
            reason = (payload.get("reason") or "manual")[:64]
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)

    try:
        from whatsapp_client import wa_client
        ack = await wa_client.request_restart(reason=f"diag:{reason}")
        log_event(
            "diag_wa_restart",
            source="diag",
            detail=f"reason={reason} ack={ack}",
            level="info" if ack else "warning",
        )
        return {"ok": True, "ack": bool(ack), "reason": reason}
    except Exception as e:
        logger.exception("diag_restart_whatsapp failed")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/api/diag/run-order-poller")
async def diag_run_order_poller(request: Request):
    """HMAC-authed order-poller trigger.

    Same internal behaviour as POST /api/admin/run-order-poller (which is
    session-authed), but using HMAC over GITHUB_WEBHOOK_SECRET so the dev
    side can fire scrapes from anywhere without a session cookie. The
    HMAC is exactly the GitHub-style X-Hub-Signature-256 header so we
    reuse _verify_github_signature.

    Fail-closed: if GITHUB_WEBHOOK_SECRET is unset, returns 503.

    Body can be empty; the signature is computed over whatever bytes are
    sent (use {} for an empty signed body). Returns 200 immediately --
    the actual scrape runs in the same asyncio task pattern as the
    session-authed admin endpoint.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured (GITHUB_WEBHOOK_SECRET unset)"},
            status_code=503,
        )

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        log_event("diag_run_order_poller_unauthorized",
                  source="diag",
                  detail="HMAC verification failed",
                  level="warning")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    # Same guards as /api/admin/run-order-poller
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
            "message": "A cart build is currently using the browser.",
        }, status_code=409)

    async def _run():
        from order_scraper import check_and_scrape_orders
        app_state.order_poll_active = True
        log_event("order_poller_start", source="diag",
                  detail="Triggered via /api/diag/run-order-poller (HMAC)")
        try:
            result = await check_and_scrape_orders()
            new_n = len(result.get("new_orders", []))
            chg_n = len(result.get("status_changes", []))
            log_event(
                "order_poller_done", source="diag",
                detail=f"diag run: new={new_n}, changes={chg_n}",
            )
        except Exception as e:
            logger.exception("Diag-triggered order-poller run failed")
            log_event("order_poller_error", source="diag",
                      detail=str(e)[:200], level="error")
        finally:
            app_state.order_poll_active = False

    app_state.order_poll_task = asyncio.create_task(_run())
    log_event("order_poller_manual_trigger", source="diag",
              detail="HMAC-authed run kicked off")
    return {
        "status": "started",
        "message": "Order poller running -- watch /api/diag/recent-errors for events.",
    }


@app.post("/api/diag/whatsapp-test")
async def diag_whatsapp_test(request: Request):
    """HMAC-authed WhatsApp test message.

    Sends a single test message via the sidecar to verify the send path
    is wired end-to-end: backend -> wa_client -> sidecar HTTP ->
    whatsapp-web.js -> phone.

    Body (optional JSON):
      {
        "phone":   "+9715..."     // E.164. If omitted, sends to the
                                  // household's 'driver' member.
        "message": "custom text"  // If omitted, a generated test ping.
      }

    Phone formats accepted (best-effort normalisation):
      "+9715XXXXXXXX"  E.164, preferred
      "9715XXXXXXXX"   missing "+"
      "0XXXXXXXX"      local with leading 0 -- ambiguous, NOT auto-prefixed.
                       Tell us the country to send the test to a local number.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "diag trigger not configured (GITHUB_WEBHOOK_SECRET unset)"},
            status_code=503,
        )

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        log_event("diag_whatsapp_test_unauthorized", source="diag",
                  detail="HMAC verification failed", level="warning")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    # Parse optional override
    custom_phone = None
    custom_message = None
    if body:
        try:
            payload = json.loads(body)
            custom_phone = (payload.get("phone") or "").strip() or None
            custom_message = (payload.get("message") or "").strip() or None
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)

    target_name = "Cem"   # default friendly name; overridden below if from DB

    if custom_phone:
        # Normalise: ensure E.164. Allow "9715..." -> "+9715...".
        phone = custom_phone
        if phone.isdigit() and not phone.startswith("0"):
            phone = "+" + phone
        target_member_id = None
        target_display = "ad-hoc"
        phone_full = phone
    else:
        # Pick the driver member -- safest default for a test ping.
        try:
            with app_state.get_db(row_factory=True) as conn:
                row = conn.execute(
                    "SELECT id, name, display_name, phone FROM household_members "
                    "WHERE role = 'driver' AND is_active = 1 "
                    "ORDER BY id LIMIT 1"
                ).fetchone()
        except Exception as e:
            log_event("diag_whatsapp_test_error", source="diag",
                      detail=f"DB lookup failed: {str(e)[:180]}", level="error")
            return JSONResponse({"error": "db lookup failed"}, status_code=500)

        if not row:
            return JSONResponse(
                {"status": "no_driver",
                 "message": "No active 'driver' member in household_members."},
                status_code=200,
            )
        phone = row["phone"]
        target_member_id = row["id"]
        target_display = row["display_name"]
        phone_full = phone

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    message = custom_message or (
        f"Hearthkeeper diag ping at {ts}.\n\n"
        f"If you got this, the full send path is working:\n"
        f"  backend -> sidecar -> whatsapp-web.js -> your phone.\n\n"
        f"Reply STOP if you want diag pings disabled."
    )

    try:
        wa_message_id = await wa_client.send_message(
            phone=phone, text=message, member_id=target_member_id,
        )
    except Exception as e:
        log_event("diag_whatsapp_test_error", source="diag",
                  detail=str(e)[:200], level="error")
        return {"status": "send_failed", "phone": phone_full, "error": str(e)[:200]}

    # rebind for the response payload below (the original "row" branch sets these)
    row = {"display_name": target_display, "phone": phone_full}

    # Include phone in the response so the dev side can verify the target
    # is right. Full phone (not just last-4) because the endpoint is
    # HMAC-authed -- same trust level as the deploy webhook.
    phone_full = row["phone"] or ""
    phone_tail = phone_full[-4:] if len(phone_full) >= 4 else phone_full

    if wa_message_id:
        log_event("diag_whatsapp_test_sent", source="diag",
                  detail=f"to={row['display_name']} ({phone_tail}) msgId={wa_message_id}")
        return {
            "status": "sent",
            "to": row["display_name"],
            "phone": phone_full,
            "wa_message_id": wa_message_id,
        }
    else:
        log_event("diag_whatsapp_test_failed", source="diag",
                  detail=f"to={row['display_name']} ({phone_tail}) sidecar reported failure",
                  level="error")
        return {
            "status": "send_failed",
            "to": row["display_name"],
            "phone": phone_full,
            "message": "Sidecar reported failure -- check WhatsApp Sidecar tile",
        }


@app.get("/api/status")
async def status():
    """Quick health check — is anyone connected?"""
    return {
        "connected": bool(app_state.connections),
        "agent_state": app_state.active_agent.session.state if app_state.active_agent else None,
    }


@app.post("/api/event")
async def track_event(request: dict = None):
    """
    Receive an event from the frontend.

    POST /api/event  {"event": "chat_open", "source": "frontend", "detail": "..."}
    """
    data = request or {}
    event = data.pop("event", "frontend")
    source = data.pop("source", "frontend")
    detail = data.pop("detail", None)
    level = data.pop("level", "info")
    log_event(event, source=source, detail=detail, level=level, **data)
    return {"ok": True}


@app.get("/api/logs")
async def view_logs(limit: int = 50, event: str = None, level: str = None):
    """
    View recent app events.

    GET /api/logs                  — last 50 events
    GET /api/logs?event=error      — only errors
    GET /api/logs?level=warning    — warnings and above
    GET /api/logs?limit=100        — more results
    """
    events = get_recent_events(limit=limit, event_filter=event, level_filter=level)
    return {"events": events, "count": len(events)}


# ============================================================
# WHATSAPP WEBHOOK
# ============================================================

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    Receives incoming WhatsApp messages forwarded from the Node.js sidecar.
    Payload: {from, body, timestamp, type, messageId}
    """
    # Verify shared secret — rejects forged requests not from the trusted sidecar.
    # Fail-closed: if WEBHOOK_SECRET is unset, every request is rejected.
    provided_secret = request.headers.get("X-Webhook-Secret", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(
        provided_secret.encode(), WEBHOOK_SECRET.encode()
    ):
        logger.warning("Rejected /webhook/whatsapp: missing or invalid X-Webhook-Secret")
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    phone_raw = payload.get("from", "")
    body = payload.get("body", "")
    msg_type = payload.get("type", "chat")
    message_id = payload.get("messageId")

    # Silently ignore status broadcasts and group messages
    if "broadcast" in phone_raw or "@g.us" in phone_raw:
        return {"ok": True, "action": "ignored", "reason": "broadcast_or_group"}

    # Only process text messages — send friendly reply for media from known members
    if msg_type != "chat":
        logger.info("Ignoring non-text WhatsApp message (type=%s) from %s", msg_type, phone_raw)
        member = WhatsAppClient.get_member_by_phone(phone_raw)
        if member:
            from whatsapp_agent import format_non_text
            await wa_client.send_message(member["phone"], format_non_text(), member_id=member["id"])
        return {"ok": True, "action": "ignored", "reason": "non-text"}

    # Look up member
    member = WhatsAppClient.get_member_by_phone(phone_raw)
    if not member:
        logger.info("WhatsApp message from unknown number: %s", phone_raw)
        return {"ok": True, "action": "ignored", "reason": "unknown_sender"}

    logger.info("WhatsApp from %s: %s", member["name"], body[:80])
    log_event("whatsapp_incoming", source="webhook",
              detail=f"{member['name']}: {body[:80]}")

    # Route to feedback manager
    try:
        result = await feedback_manager.handle_member_reply(member, body, message_id)

        # Broadcast updated feedback status to connected WebSockets
        if app_state.active_agent:
            await _send_feedback_status(app_state.active_agent)

        return {"ok": True, "action": result.get("action", "received"), "member": member["name"]}
    except Exception as e:
        logger.error("Feedback processing failed: %s", e, exc_info=True)
        log_event("error", source="webhook", detail=f"feedback processing: {e}", level="error")
        return {"ok": True, "action": "error", "member": member["name"]}


@app.get("/api/whatsapp/status")
async def whatsapp_status():
    """Check WhatsApp sidecar connection status."""
    status = await wa_client.get_status()
    return status


# ============================================================
# GITHUB AUTO-DEPLOY WEBHOOK   (thread-based; tested end-to-end)
# ============================================================
#
# History: this started as a synchronous `await git pull` inside the
# webhook handler -- blew GitHub's 10 s budget. Refactored to
# asyncio.create_task; tasks were silently GC'd between awaits even with
# a strong reference set. Final design (this one): kick the deploy off
# in a plain threading.Thread, sidesteps all the asyncio task-lifecycle
# subtlety. See _perform_deploy_sync for the per-step log_event trail.


def _verify_github_signature(body: bytes, signature_header: str) -> bool:
    """Constant-time HMAC verify for GitHub's X-Hub-Signature-256 header."""
    if not GITHUB_WEBHOOK_SECRET or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        body,
        "sha256",
    ).hexdigest()
    return hmac.compare_digest(expected.encode(), signature_header.encode())


async def _git_cmd(args, timeout=60):
    """Run a git subcommand in the project dir off the event loop."""
    proc = await asyncio.to_thread(
        subprocess.run,
        ["git"] + list(args),
        cwd=str(Path(__file__).parent),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc


def _perform_deploy_sync(branch: str, pushed_sha: str):
    """Run git pull + schedule restart -- in a regular OS thread.

    History: this was originally an async coroutine fired via
    asyncio.create_task. Even with a strong reference held in a
    module-level set, deploys consistently logged deploy_pull_start
    and then never progressed -- no deploy_pulled, no deploy_pull_error,
    no deploy_pull_failed. The 120 s subprocess timeout never fired,
    which would be the case if the asyncio task itself was being killed
    silently somewhere between event-loop ticks.

    Switching to a plain threading.Thread sidesteps the entire asyncio
    task-lifecycle question. Threads aren't subject to weakref-based GC,
    they don't share an event loop with the request that spawned them,
    and we get rich step-by-step log_event() visibility because each
    call is a normal synchronous call (no awaits to silently drop).

    Trade-off: we use blocking subprocess.run instead of awaiting
    asyncio.to_thread. That's fine here because we're already in a
    dedicated background thread -- nothing else is sharing it.
    """
    project_dir = str(Path(__file__).parent)

    log_event("deploy_thread_started", source="webhook_deploy",
              detail=f"branch={branch} pushed_sha={pushed_sha}")

    # 1) git pull --ff-only
    #
    # Fast-fail credential handling. The webhook receiver runs in the
    # FastAPI service, which NSSM defaults to running as LocalSystem.
    # LocalSystem has its own credential store and typically *no* GitHub
    # HTTPS credentials cached -- so a naked `git pull` spawns Git
    # Credential Manager, which puts up a GUI dialog in the service
    # session that nobody can see or close. The whole call hangs in a
    # state subprocess timeout cannot reliably interrupt on Windows
    # (observed: timeout=120 elapsed without firing, no event surfaced
    # for several minutes -- which is exactly the symptom we hit).
    #
    # Setting GIT_TERMINAL_PROMPT=0 + disabling credential.helper /
    # core.askpass for this single invocation forces the pull to either
    # succeed with cached creds or fail fast with a clean
    # "fatal: could not read Username" error we can log and act on.
    import os as _os  # local to keep the import close to use
    log_event("deploy_pull_start", source="webhook_deploy",
              detail=f"branch={branch} pushed_sha={pushed_sha}")
    env = _os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        pull = subprocess.run(
            [
                "git",
                "-c", "credential.helper=",
                "-c", "core.askpass=",
                "pull", "--ff-only",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        log_event("deploy_pull_error", source="webhook_deploy",
                  detail=f"timeout after 60s (likely hung on creds): {str(e)[:120]}",
                  level="error")
        return
    except Exception as e:
        log_event("deploy_pull_error", source="webhook_deploy",
                  detail=f"{type(e).__name__}: {str(e)[:180]}",
                  level="error")
        return

    if pull.returncode != 0:
        log_event(
            "deploy_pull_failed", source="webhook_deploy",
            detail=f"rc={pull.returncode} stderr={(pull.stderr or '')[:240]} "
                   f"stdout={(pull.stdout or '')[:60]}",
            level="error",
        )
        return

    # 2) capture new HEAD sha
    new_sha = "unknown"
    try:
        sha_proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_dir, capture_output=True, text=True, timeout=10,
        )
        new_sha = (sha_proc.stdout or "").strip() or "unknown"
    except Exception as e:
        logger.warning("Could not read post-pull HEAD sha: %s", e)

    log_event(
        "deploy_pulled", source="webhook_deploy",
        detail=f"branch={branch} head={new_sha} pushed_sha={pushed_sha}",
    )

    # 2.5) Rebuild the frontend if its source changed in this pull.
    #
    # The React frontend's built artefacts live in frontend/dist/ which
    # is .gitignored, so source-only pulls don't actually update what
    # gets served. We detect "did this pull touch frontend/src/, the
    # public html, or the package manifests?" via git diff and run
    # `npm run build` if so.
    #
    # Best-effort: if npm isn't on PATH or the build fails, log it and
    # carry on with the restart -- the existing dist/ will keep serving
    # the previous frontend (slightly stale UI), the backend code
    # changes still take effect.
    try:
        diff_proc = subprocess.run(
            [
                "git", "diff", "--name-only",
                f"{new_sha}~1..{new_sha}",
            ],
            cwd=project_dir, capture_output=True, text=True, timeout=15,
        )
        changed = (diff_proc.stdout or "").splitlines()
        frontend_changed = any(
            f.startswith("frontend/src/")
            or f == "frontend/index.html"
            or f == "frontend/package.json"
            or f == "frontend/vite.config.js"
            for f in changed
        )
    except Exception as e:
        logger.warning("Could not diff frontend files: %s", e)
        frontend_changed = False

    if frontend_changed:
        log_event("deploy_frontend_build_start", source="webhook_deploy",
                  detail=f"{len([f for f in changed if f.startswith('frontend/')])} frontend file(s) changed")
        try:
            npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
            build = subprocess.run(
                [npm_cmd, "run", "build"],
                cwd=str(Path(__file__).parent / "frontend"),
                capture_output=True, text=True, timeout=300,
            )
            if build.returncode == 0:
                log_event("deploy_frontend_build_done", source="webhook_deploy",
                          detail="npm run build OK")
            else:
                log_event(
                    "deploy_frontend_build_failed",
                    source="webhook_deploy",
                    detail=f"rc={build.returncode} stderr={(build.stderr or '')[:240]}",
                    level="error",
                )
        except FileNotFoundError:
            log_event("deploy_frontend_build_failed", source="webhook_deploy",
                      detail="npm not on PATH -- frontend dist not rebuilt",
                      level="warning")
        except Exception as e:
            log_event("deploy_frontend_build_failed", source="webhook_deploy",
                      detail=f"{type(e).__name__}: {str(e)[:180]}", level="error")

    # 3) Trigger the restart by killing ourselves.
    #
    # History: we tried two external-restart paths and both fail when
    # the service account is a regular user:
    #   - `nssm restart hearthkeeper-app` from a detached subprocess
    #     fails silently because nssm.exe isn't on the service's PATH.
    #   - `powershell Restart-Service hearthkeeper-app` from a detached
    #     subprocess fails because Restart-Service requires admin rights,
    #     and the Cem user account isn't elevated in the service session
    #     (verified on prod: deploy_restart_scheduled fires but the
    #     running process never actually flips to the new SHA).
    #
    # A process is always allowed to terminate itself with no special
    # privileges. NSSM's default "AppExit" behavior is to restart the
    # application when it exits, so os._exit() in a delayed thread is
    # the cleanest way to swap to the new code:
    #   - No admin needed
    #   - No subprocess dance
    #   - Works regardless of NSSM service account
    #   - Restart is governed entirely by NSSM, which is the right
    #     authority for it
    #
    # The 3 s sleep ensures the deploy_restart_scheduled event has been
    # written to SQLite and that any in-flight HTTP responses get a
    # chance to flush.
    try:
        import os as _os
        import threading as _threading
        import time as _time

        def _self_terminate_for_restart():
            _time.sleep(3)
            # os._exit bypasses Python's atexit handlers and uvicorn's
            # graceful shutdown path -- that's intentional. We want NSSM
            # to see a clean process exit and start a fresh one with the
            # new code; we don't want a graceful shutdown that re-enters
            # the new code partially loaded into the old process.
            _os._exit(0)

        _threading.Thread(
            target=_self_terminate_for_restart,
            name="deploy-self-restart",
            daemon=True,
        ).start()
        log_event("deploy_restart_scheduled", source="webhook_deploy",
                  detail=f"head={new_sha} via os._exit+nssm")
    except Exception as e:
        log_event("deploy_restart_failed", source="webhook_deploy",
                  detail=f"{type(e).__name__}: {str(e)[:180]}",
                  level="error")


@app.post("/webhook/deploy")
async def webhook_deploy(request: Request):
    """Auto-deploy on GitHub push -- returns 200 IMMEDIATELY.

    Wired to a GitHub repo webhook (Settings > Webhooks) pointing at
    https://hearthkeeper.cc/webhook/deploy, content type application/json,
    secret = GITHUB_WEBHOOK_SECRET in the Ally's .env.

    Critical: GitHub gives webhook receivers ~10 s to respond. A
    synchronous `git pull` from the Ally's home internet blows that
    budget, GitHub records "delivery timed out", and even successful
    deploys look broken in the GitHub UI. So this handler:

      1) HMAC-verifies the body (fast).
      2) Filters event type and branch ref (fast).
      3) Spawns _perform_deploy_sync in a daemon=False Thread.
      4) Returns 200 immediately.

    _perform_deploy_sync logs each step (deploy_thread_started ->
    deploy_pull_start -> deploy_pulled -> deploy_restart_scheduled, or
    a *_failed / *_error variant) to app_events, visible from the
    /api/diag/recent-errors endpoint. Failures don't surface to GitHub
    because we've already returned 200 by then, but they're queryable
    from the dashboard.

    Returns:
      200 {status: "queued",  branch, pushed_sha}   -- accepted, deploy running
      200 {status: "pong"}                          -- GitHub "ping" event
      200 {status: "ignored", reason}               -- branch / event mismatch
      401 {error: "bad signature"}                  -- HMAC failed
      503 {error: "webhook deploy not configured"}  -- secret unset
    """
    if not GITHUB_WEBHOOK_SECRET:
        return JSONResponse(
            {"error": "webhook deploy not configured (GITHUB_WEBHOOK_SECRET unset)"},
            status_code=503,
        )

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body, sig):
        log_event("deploy_unauthorized", source="webhook_deploy",
                  detail="HMAC verification failed", level="warning")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"status": "pong"}
    if event != "push":
        return {"status": "ignored", "reason": f"event={event}"}

    try:
        payload = json.loads(body)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    pushed_ref = payload.get("ref", "")
    pushed_sha = (payload.get("after") or "")[:12]

    # Detect which branch to compare against. Explicit env override wins;
    # otherwise we accept pushes to whatever branch HEAD currently points at.
    if DEPLOY_BRANCH:
        expected_branch = DEPLOY_BRANCH
    else:
        try:
            br = await _git_cmd(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
            expected_branch = (br.stdout or "").strip() or "HEAD"
        except Exception as e:
            return JSONResponse(
                {"error": f"could not detect branch: {e}"},
                status_code=500,
            )

    expected_ref = f"refs/heads/{expected_branch}"
    if pushed_ref != expected_ref:
        log_event("deploy_ignored", source="webhook_deploy",
                  detail=f"pushed {pushed_ref} != expected {expected_ref}")
        return {
            "status": "ignored",
            "reason": f"ref {pushed_ref} != {expected_ref}",
        }

    # Hand off to a background thread (not asyncio.create_task -- see
    # _perform_deploy_sync docstring for the history of why). Daemon=False
    # so the thread can outlive its parent request without being torn
    # down. We don't need to join() -- the thread runs to completion or
    # the FastAPI process restarts itself via the Restart-Service Popen.
    import threading
    threading.Thread(
        target=_perform_deploy_sync,
        args=(expected_branch, pushed_sha),
        name=f"deploy-{pushed_sha}",
        daemon=False,
    ).start()
    log_event("deploy_queued", source="webhook_deploy",
              detail=f"branch={expected_branch} pushed_sha={pushed_sha}")

    return {
        "status": "queued",
        "branch": expected_branch,
        "pushed_sha": pushed_sha,
    }


# ============================================================
# STATIC FILE SERVING
# ============================================================

# Mount frontend/dist/assets/ for JS, CSS, images
if not dev_mode and (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="static-assets")

# Mount frontend/dist/logos/ for vendor logos
if not dev_mode and (FRONTEND_DIST / "logos").exists():
    app.mount("/logos", StaticFiles(directory=FRONTEND_DIST / "logos"), name="static-logos")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the React frontend (production) or dev mode landing page."""
    if dev_mode:
        return HTMLResponse(
            "<html><body style='font-family:system-ui;padding:2em'>"
            "<h3>Hearthkeeper — Dev Mode</h3>"
            "<p>Frontend: <a href='http://localhost:5173'>http://localhost:5173</a></p>"
            "<p>Backend API running on this port. WebSocket at /ws.</p>"
            "</body></html>"
        )
    html_path = FRONTEND_DIST / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<html><body style='font-family:system-ui;padding:2em'>"
        "<h3>Frontend not built</h3>"
        "<p>Run: <code>cd frontend && npm run build</code></p>"
        "</body></html>"
    )


# ============================================================
# PERSISTENT CONVERSATION
# ============================================================

def _save_chat_message(role, text, list_id=None, metadata=None):
    """Save a chat message to the database for persistence."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO chat_messages (list_id, role, text, metadata) VALUES (?, ?, ?, ?)",
                (list_id, role, text[:2000], json.dumps(metadata) if metadata else None)
            )
    except Exception as e:
        logger.debug(f"Could not save chat message: {e}")


def _load_chat_history(list_id=None, limit=50):
    """Load the most recent chat messages for this household.

    Deliberately NOT scoped by list_id. Chat history used to filter
    "WHERE list_id = ?", but that orphaned half the conversation: the edit
    tools reassign session.list_id mid-turn (see _resolve_edit_list, which
    sets session.list_id to the active Scheduled list). So within a single
    "remove the cola" / "set salmon to 3" turn, the USER echo was saved under
    the connect-time list_id while the ASSISTANT confirmation was saved under
    the (now different) scheduled-list id. On the next connect, loading
    WHERE list_id = <connect-time id> returned the user lines but dropped
    every edit confirmation. The scheduled list id also changes whenever the
    list is regenerated/superseded, orphaning older history wholesale.

    This is a single household with one continuous conversation, so the right
    scope is "all recent messages" in chronological order. The list_id arg is
    kept for call-site compatibility but no longer filters.
    """
    try:
        with get_db(row_factory=True) as conn:
            # Over-fetch a little, then drop legacy boilerplate (welcome
            # banners persisted before they were switched to save=False), so
            # those stale rows don't eat into the visible window before they
            # age out of the table naturally.
            rows = conn.execute(
                "SELECT role, text, metadata, created_at FROM chat_messages "
                "WHERE text NOT LIKE 'Hearthkeeper agent ready.%' "
                "ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        # Return in chronological order (oldest first)
        messages = []
        for row in reversed(rows):
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            messages.append({
                "role": "assistant" if row["role"] == "assistant" else "user",
                "text": row["text"],
                "time": row["created_at"][-8:-3] if row["created_at"] else "",
                **meta,
            })
        return messages
    except Exception as e:
        logger.debug(f"Could not load chat history: {e}")
        return []


# ============================================================
# WEBSOCKET HELPERS
# ============================================================

# Cap free-text length — bounds LLM token use and prompt-injection room.
MAX_MESSAGE_LEN = 2000

# Cap inbound WebSocket messages per connection — a rolling-window flood
# guard that bounds LLM-triggering traffic from a buggy or abusive client.
WS_MSG_RATE_LIMIT = 120      # max messages...
WS_MSG_RATE_WINDOW = 60      # ...per this many seconds (rolling)


def _build_catalog_suggestion(item_name, store_hint, quantity, session):
    """Stage real catalog products for the suggestion bottom sheet when an
    "add X" can't be auto-matched.

    Uses a NAME-based catalog search (product_knowledge.search_catalog) rather
    than the basket curator's scorer — the curator can badly mis-rank produce
    (it scored "cucumber" -> "eggplant"/"pudding rice"), which is exactly why
    the auto-match failed. A name search returns the actual requested item.

    Side effect: stores the candidate list on session.pending_suggestions so
    the next_suggestion / skip_suggestion / add_suggestion actions can cycle
    through and commit the user's pick. Returns the sheet payload for
    _send_suggestion, or None when the catalog has nothing matching the name.
    """
    try:
        from product_knowledge import search_catalog
        results = search_catalog(item_name, limit=6) or []
    except Exception as e:
        logger.warning("catalog suggestion search failed for %r: %s", item_name, e)
        return None
    # Real, priced products only (defensive — the sheet needs a catalog_id).
    results = [p for p in results
               if p.get("catalog_id") and (p.get("price") or 0) > 0]
    if not results:
        return None
    # If the user named a store, float its products to the front.
    if store_hint:
        sh = store_hint.strip().lower()
        results.sort(key=lambda p: 0 if sh in (p.get("store_name") or "").lower() else 1)
    session.pending_suggestions = {
        "item_name": item_name,
        "query": item_name,
        "products": results,
        "current_index": 0,
        "quantity": quantity,
        "store_hint": store_hint,
    }
    return {
        "item_name": item_name,
        "product": results[0],
        "has_more": len(results) > 1,
        "total": len(results),
    }


def _try_fast_tool_command(user_text, session):
    """#3 Deterministic fast path for unambiguous add / remove / skip / set-qty.

    Reuses the regex parser (message_handler.parse_message) but dispatches to
    the SAME view-aware agent_tools the Store Manager would call — so a plain
    "add milk" or "remove the cola" runs in well under a second instead of the
    ~15s LLM round-trip, while still:
      - editing the list the user is VIEWING (scheduled vs cart, via session)
      - confirming ONLY from the tool's real return status (truthful)

    Returns {"text": confirmation, "tool": name} when it confidently handled
    the command, else None — in which case the caller defers to the LLM agent
    (so anything ambiguous, multi-item, not-found, or needs-clarification still
    gets the smart path). Conservative by design: false negatives just cost
    latency, false positives would mis-edit the list.
    """
    from message_handler import parse_message, Intent
    from agent_tools import (
        _tool_add_item_to_list,
        _tool_remove_item_from_list,
        _tool_change_item_quantity,
    )

    if "?" in user_text:
        return None  # questions always go to the agent

    parsed = parse_message(user_text)
    if not parsed or not parsed.actions or len(parsed.actions) != 1:
        return None  # multi-action or unparsed → agent

    a = parsed.actions[0]
    name = (a.item_name or "").strip()
    if not name or getattr(a, "confidence", 0) < 0.8:
        return None

    # ── ADD: single, simple noun phrase only ──
    if a.intent == Intent.ADD_ITEM:
        low = name.lower()
        if (" and " in low or "," in low or " for " in low or " or " in low
                or any(w in low for w in ("something", "whatever", "stuff"))):
            return None  # smells like reasoning / multi-item → agent
        qty = int(a.quantity) if (a.quantity and a.quantity > 0) else 1
        res = _tool_add_item_to_list(
            {"item_name": name, "quantity": qty,
             "store_hint": getattr(a, "store_hint", None)},
            session,
        )
        if res.get("status") != "added":
            # Curator couldn't confidently match. Don't dump to the slow LLM
            # (it historically dead-ended: ~50s, nothing added, no view
            # refresh). Instead OFFER real catalog matches via the suggestion
            # sheet for the user to tap-approve. Per the user's choice: offer
            # options, don't auto-add.
            payload = _build_catalog_suggestion(
                name, getattr(a, "store_hint", None), qty, session)
            if payload:
                return {"suggest": payload, "tool": "suggest_product",
                        "item_name": name}
            # Catalog has nothing by that name — a fast, honest reply beats a
            # slow LLM non-answer.
            return {
                "text": (f"I couldn't find “{name}” in Kibsons or "
                         f"Carrefour. Try a more specific name?"),
                "tool": "add_not_found",
            }
        prod = res.get("product_name") or res.get("item_name") or name
        txt = f"Added {prod}"
        q = res.get("quantity", qty)
        if q and q != 1:
            txt += f" ×{q}"
        if res.get("store"):
            txt += f" from {res['store']}"
        if res.get("price_aed"):
            try:
                txt += f" (AED {float(res['price_aed']):.2f})"
            except (TypeError, ValueError):
                pass
        return {"text": txt + ".", "tool": "add_item_to_list"}

    # ── REMOVE / SKIP ──
    if a.intent == Intent.SKIP_ITEM:
        res = _tool_remove_item_from_list({"item_name": name}, session)
        if res.get("status") != "removed":
            return None  # not_found → agent can reason about fuzzy matches
        return {
            "text": f"Removed {res.get('item_name', name)} from your list.",
            "tool": "remove_item_from_list",
        }

    # ── CHANGE QTY: only an absolute set to a positive number ──
    # (Negative = relative "reduce by N"; let the agent read current qty.)
    if a.intent == Intent.CHANGE_QTY and a.quantity is not None and a.quantity > 0:
        qty = int(a.quantity)
        res = _tool_change_item_quantity({"item_name": name, "quantity": qty}, session)
        if res.get("status") != "updated":
            return None
        return {
            "text": f"Updated {res.get('item_name', name)} to ×{res.get('new_quantity', qty)}.",
            "tool": "change_item_quantity",
        }

    return None


async def send_list_data(ws: WebSocket, agent: AgentBrain):
    """
    Build and send structured list data to the frontend.
    Sends both suggestion and cart data when available (two-entity model).
    """
    suggestion_id = getattr(agent.session, "suggestion_list_id", None)
    cart_id = getattr(agent.session, "cart_list_id", None)
    # Fallback for backward compat
    list_id = agent.session.list_id

    if not suggestion_id and not cart_id and not list_id:
        # No lists — send "all caught up"
        try:
            from frontend_bridge import _default_conversation
            await ws.send_json({
                "type": "list_data",
                "vendors": [],
                "suggestions": None,
                "cart": None,
                "conversation": _default_conversation(),
                "highlights": [],
                "listId": None,
                "agentState": agent.session.state if agent.session else "done",
            })
        except Exception as e:
            logger.debug("Empty list_data send failed (client likely disconnected): %s", e)
        return

    try:
        # Build suggestion data
        suggestion_data = None
        if suggestion_id:
            try:
                suggestion_data = build_frontend_list(suggestion_id)
            except Exception as e:
                logger.warning(f"Failed to build suggestion list: {e}")

        # Build cart data
        cart_data = None
        if cart_id:
            try:
                cart_data = build_frontend_list(cart_id)
            except Exception as e:
                logger.warning(f"Failed to build cart list: {e}")

        # Determine the "primary" list for backward compat (vendors field)
        primary_id = cart_id or suggestion_id or list_id
        primary_data = cart_data or suggestion_data

        if not primary_data:
            # Fallback to single-list mode
            primary_data = build_frontend_list(primary_id)

        # Build the top-level message dict (copy primary for backward compat,
        # then add dual-list fields separately to avoid circular refs)
        data = {
            "type": "list_data",
            "listId": primary_id,
            "agentState": agent.session.state,
            "vendors": primary_data.get("vendors", []),
            "diff": primary_data.get("diff"),
            "conversation": primary_data.get("conversation"),
            "highlights": primary_data.get("highlights", []),
            "suggestions": suggestion_data,
            "cart": cart_data,
            "suggestionListId": suggestion_id,
            "cartListId": cart_id,
            # Per-view conversation headers so frontend can show the right one
            "suggestionConversation": suggestion_data.get("conversation") if suggestion_data else None,
            "cartConversation": cart_data.get("conversation") if cart_data else None,
        }

        await ws.send_json(data)
    except Exception as e:
        logger.error(f"send_list_data failed: {e}", exc_info=True)
        log_event("error", source="frontend_bridge", detail=str(e)[:200], level="error")
        try:
            await ws.send_json({
                "type": "agent",
                "text": "Could not load the shopping list. Try refreshing.",
                "time": datetime.now().strftime("%H:%M"),
            })
        except Exception as e:
            logger.debug("List-load error message send failed (client likely disconnected): %s", e)


async def _send_feedback_status(agent: AgentBrain):
    """Send feedback round status + items to the frontend."""
    try:
        # Find list_id from agent session
        list_id = None
        if agent.session:
            list_id = (getattr(agent.session, "suggestion_list_id", None)
                       or getattr(agent.session, "cart_list_id", None)
                       or agent.session.list_id)

        if not list_id:
            return

        round_status = feedback_manager.get_round_status(list_id=list_id)
        feedback_items = feedback_manager.get_feedback_items(list_id=list_id)

        await broadcast({
            "type": "feedback_status",
            "data": round_status,
            "items": feedback_items,
            "whatsappEnabled": WHATSAPP_ENABLED,
        })
    except Exception as e:
        logger.debug("_send_feedback_status failed: %s", e)


async def send_item_update(ws: WebSocket, list_item_id: int, changes: dict):
    """Send an incremental item update to the frontend."""
    try:
        await ws.send_json({
            "type": "item_update",
            "listItemId": list_item_id,
            "changes": changes,
        })
    except Exception as e:
        logger.debug("Item update send failed (client likely disconnected): %s", e)


async def send_item_removed(ws: WebSocket, list_item_id: int):
    """Notify frontend an item was removed."""
    try:
        await ws.send_json({
            "type": "item_removed",
            "listItemId": list_item_id,
        })
    except Exception as e:
        logger.debug("Item removed send failed (client likely disconnected): %s", e)


async def _send_suggestion(ws: WebSocket, agent: AgentBrain, suggestion_data: dict):
    """
    Send a product suggestion to the frontend as a bottom sheet.
    Called when _add_item() finds catalog results for an unknown item.
    """
    try:
        product = suggestion_data.get("product", {})
        item_name = suggestion_data.get("item_name", "Unknown")
        has_more = suggestion_data.get("has_more", False)
        total = suggestion_data.get("total", 1)

        await ws.send_json({
            "type": "suggestion",
            "itemName": item_name,
            "isAlternative": False,
            "currentIndex": 0,
            "totalOptions": total,
            "quantity": agent.session.pending_suggestions.get("quantity", 1) if agent.session.pending_suggestions else 1,
            "product": {
                "catalogId": product.get("catalog_id"),
                "name": product.get("product_name", ""),
                "brand": product.get("brand", ""),
                "price": product.get("price"),
                "size": product.get("size", ""),
                "image": product.get("image_url", ""),
                "isOrganic": product.get("is_organic", False),
                "storeName": product.get("store_name", ""),
            },
            "hasMore": has_more,
        })
        log_event("suggestion_sent", source="suggestion",
                  detail=f"Sent suggestion for '{item_name}': {product.get('product_name', '?')}")
    except Exception as e:
        logger.error(f"_send_suggestion failed: {e}", exc_info=True)
        log_event("suggestion_send_error", source="suggestion",
                  detail=f"Failed to send suggestion: {e}", level="error")


# ============================================================
# WEBSOCKET ENDPOINT
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket chat endpoint for the agent."""
    # Origin gate — CORS does not cover the WebSocket upgrade, so the Origin
    # header must be checked explicitly to block cross-site WebSocket hijacking.
    # Skipped in --dev (localhost only).
    origin = ws.headers.get("origin")
    if not dev_mode and origin not in ALLOWED_ORIGINS:
        await ws.close(code=1008)  # 1008 = policy violation
        logger.warning("Rejected WebSocket connection — disallowed origin: %r", origin)
        return

    # Auth gate — the HTTP middleware does not run for WebSocket scope, so the
    # session cookie must be checked here. Skipped in --dev (localhost only).
    if not dev_mode and not _verify_session_token(ws.cookies.get(SESSION_COOKIE, "")):
        await ws.close(code=1008)  # 1008 = policy violation
        logger.warning("Rejected WebSocket connection — no valid session")
        return

    await ws.accept()
    app_state.connections.add(ws)
    logger.info("WebSocket client connected")
    log_event("ws_connect", source="app")

    # Create agent with WebSocket output
    agent = AgentBrain(dry_run=dry_run_mode)
    app_state.active_agent = agent

    async def send_message(text, save=True):
        """Send agent message to the WebSocket client and persist it."""
        try:
            await ws.send_json({
                "type": "agent",
                "text": text,
                "time": datetime.now().strftime("%H:%M"),
            })
            if save:
                _save_chat_message("assistant", text,
                                   list_id=agent.session.list_id if agent.session else None)
        except Exception as e:
            logger.debug("Agent message send failed (client likely disconnected): %s", e)

    # The agent's say() is sync, but we need async WebSocket sends.
    # Buffer messages and flush them.
    message_buffer = []

    def buffer_message(text):
        message_buffer.append(text)

    agent.set_output(buffer_message)

    async def flush_messages():
        while message_buffer:
            msg = message_buffer.pop(0)
            await send_message(msg)
            await asyncio.sleep(0.1)  # tiny delay for UI feel

    try:
        # Flush any queued nudges from when user was offline
        await _flush_nudge_queue(ws)

        # Send welcome + status. save=False: this is ephemeral per-connect
        # boilerplate, not conversation. Persisting it meant every reconnect
        # (mobile wake, tab refocus) appended another "Hearthkeeper agent
        # ready" line, which piled up and pushed real chat out of the
        # most-recent-50 history window.
        target = target_date_override or next_order_date()
        await send_message(
            f"Hearthkeeper agent ready.\n"
            f"Today: {datetime.now().strftime('%A %b %d')}\n"
            f"Target order date: {target}"
            + (" | DRY RUN MODE" if dry_run_mode else ""),
            save=False,
        )

        # Start check-in
        try:
            agent.start_checkin(target_date=target_date_override)
            await flush_messages()
        except Exception as e:
            logger.error(f"start_checkin failed: {e}", exc_info=True)
            log_event("error", source="agent", detail=f"start_checkin: {e}", level="error")
            await send_message(
                "Something went wrong loading your shopping list. "
                "Check if the database has restock rules set up, or try restarting."
            )

        # ── Send all UI data FIRST (fast — user sees everything immediately) ──
        await send_list_data(ws, agent)

        # Load and send chat history from previous sessions
        history = _load_chat_history(list_id=agent.session.list_id, limit=50)
        if history:
            try:
                await ws.send_json({"type": "chat_history", "messages": history})
            except Exception as e:
                logger.debug("Chat history send failed (client likely disconnected): %s", e)

        # Send order tracking data (active + delivered-unrated orders)
        try:
            tracking = _build_order_tracking()
            if tracking.get("activeOrder") or tracking.get("deliveredOrder"):
                await ws.send_json({"type": "order_tracking", **tracking})
        except Exception as e:
            logger.debug(f"Could not send order tracking: {e}")

        # Send feedback round status (if any active round)
        try:
            await _send_feedback_status(agent)
        except Exception as e:
            logger.debug(f"Could not send feedback status: {e}")

        # ── Process any pending preference signals ──
        # Clears backlog from UI actions, gap analysis, delivery ratings etc.
        try:
            from preference_engine import process_pending_signals
            sig_count = process_pending_signals(batch_size=500)
            if sig_count:
                logger.info(f"Processed {sig_count} pending preference signals on init")
        except Exception as e:
            logger.debug(f"Could not process preference signals: {e}")

        # ── Live verification on predicted list picks (BACKGROUND) ──
        # This hits live store websites, so it can take 10-25s. It MUST NOT
        # run inline here: the message-receive loop below only starts once
        # this returns, so awaiting it inline means the user's first typed
        # command sits unprocessed — no echo, no typing indicator, no reply —
        # until the slow HTTP checks finish (looked like a ~20s hang on the
        # first message). Run it as a background task so the loop starts
        # immediately; when prices change it re-sends list data on its own.
        async def _post_connect_live_verify(list_id):
            # Refresh LIVE availability + price for the products this list shows,
            # then re-render so OOS items auto-swap to an in-stock alternative
            # (the render path already does the swap once the catalog is fresh).
            # Guards: skip while the cart/scraper owns a browser, and throttle so
            # we don't relaunch the verify browser on every reconnect.
            try:
                if getattr(app_state, "cart_build_active", False):
                    return
            except Exception:
                pass
            now = time.monotonic()
            last = _LAST_AVAIL_VERIFY.get(list_id, 0)
            if now - last < AVAIL_VERIFY_COOLDOWN_S:
                return
            _LAST_AVAIL_VERIFY[list_id] = now
            try:
                from live_verify import live_verify_list_availability
            except ImportError:
                logger.debug("live_verify module not available, skipping")
                return
            try:
                result = await live_verify_list_availability(list_id)
                if result.get("changed"):
                    logger.info(f"Checkin availability verify: {result}")
                    await send_list_data(ws, agent)  # re-render: OOS swaps + fresh prices
            except Exception as e:
                logger.warning(f"Background availability verification failed: {e}")

        # Keep a reference so the task isn't garbage-collected mid-flight; the
        # enclosing frame lives for the whole connection (the loop below).
        _verify_task = None
        if AVAILABILITY_VERIFY_ON_CONNECT and agent.session and agent.session.list_id:
            _verify_task = asyncio.create_task(
                _post_connect_live_verify(agent.session.list_id)
            )

        # Main conversation loop (runs even in DONE state so WS stays alive for chat/actions)
        msg_times = deque()    # monotonic timestamps of recently accepted messages
        rate_limited = False   # True while this connection is being throttled
        while True:
            try:
                data = await ws.receive_json()
            except WebSocketDisconnect:
                break

            # Per-connection rate limit — once a connection exceeds
            # WS_MSG_RATE_LIMIT within the rolling window, drop its messages
            # until its traffic falls back under the cap.
            now_ts = time.monotonic()
            while msg_times and now_ts - msg_times[0] > WS_MSG_RATE_WINDOW:
                msg_times.popleft()
            if len(msg_times) >= WS_MSG_RATE_LIMIT:
                if not rate_limited:
                    rate_limited = True
                    logger.warning("WebSocket message rate limit hit — throttling a connection")
                    log_event("ws_rate_limited", source="app")
                    try:
                        await ws.send_json({
                            "type": "agent",
                            "text": "You're sending messages too fast — give me a moment to catch up.",
                            "time": datetime.now().strftime("%H:%M"),
                        })
                    except Exception as e:
                        logger.debug("Rate-limit notice send failed: %s", e)
                continue
            rate_limited = False
            msg_times.append(now_ts)

            # Cap free-text length before it reaches processing or the LLM.
            if isinstance(data.get("text"), str) and len(data["text"]) > MAX_MESSAGE_LEN:
                data["text"] = data["text"][:MAX_MESSAGE_LEN]

            # Handle feedback messages (thumbs up/down)
            if data.get("type") == "feedback":
                _log_feedback(data.get("msg_id"), data.get("vote"), agent)
                continue

            # Handle structured actions from frontend UI
            if data.get("type") == "action":
                await _handle_action(ws, agent, data, message_buffer, flush_messages)
                continue

            user_text = data.get("text", "").strip()
            if not user_text:
                continue

            # Which tab the user is looking at, so chat edits apply to the
            # list they SEE (scheduled vs cart) instead of always the cart.
            _view = (data.get("view") or "").strip().lower()
            if _view in ("scheduled", "suggestions", "suggestion"):
                agent.session.active_view = "scheduled"
            elif _view == "cart":
                agent.session.active_view = "cart"

            logger.info(f"User: {user_text[:120]}")
            log_event("user_message", source="chat", detail=user_text[:200],
                       state=agent.session.state)

            # Echo user message back (for chat UI) and persist
            await ws.send_json({
                "type": "user",
                "text": user_text,
                "time": datetime.now().strftime("%H:%M"),
            })
            _save_chat_message("user", user_text,
                               list_id=agent.session.list_id if agent.session else None)

            # ── Slice 2: external-purchase shortcut ──
            # If the user is telling us about something they already
            # bought ("just got milk", "we have bread"), short-circuit
            # the agent and record it as an external purchase. The
            # cadence calibrator will then treat it like a real
            # order and suppress the item from next-cycle suggestions.
            #
            # Defaults to False; only takes the shortcut if the
            # regex parser confidently matched and at least one item
            # resolved to a tracked items.id (we don't want to silently
            # eat random user messages).
            _external_purchase_handled = False
            try:
                from external_purchases import record_many_from_text
                ep_result = record_many_from_text(user_text, source="chat")
                if ep_result["parsed"] and ep_result["records"]:
                    resolved_records = [
                        r for r in ep_result["records"] if r.get("item_id")
                    ]
                    if resolved_records:
                        names = []
                        unresolved_names = []
                        for r in ep_result["records"]:
                            display = r.get("resolved_name") or r["raw_name"]
                            if r.get("item_id"):
                                names.append(display)
                            else:
                                unresolved_names.append(display)
                        ack_parts = [
                            f"Got it. Marking {', '.join(names)} as bought "
                            f"on {ep_result['purchase_date']}. "
                            f"I won't suggest "
                            f"{'them' if len(names) > 1 else 'it'} "
                            f"until next cycle."
                        ]
                        if unresolved_names:
                            # Light note: we recorded the raw text but
                            # didn't match it to a tracked item, so it
                            # won't affect cadence math for now.
                            ack_parts.append(
                                f"\n\n(I noted {', '.join(unresolved_names)} "
                                f"too, but couldn't match to a tracked item -- "
                                f"add it to your habits to take effect.)"
                            )
                        ack = "".join(ack_parts)
                        await ws.send_json({
                            "type": "agent",
                            "text": ack,
                            "time": datetime.now().strftime("%H:%M"),
                        })
                        _save_chat_message(
                            "assistant", ack,
                            list_id=agent.session.list_id if agent.session else None,
                        )
                        log_event(
                            "external_purchase",
                            source="chat",
                            detail=(
                                f"records={len(resolved_records)} "
                                f"items={[r.get('resolved_name') for r in resolved_records]}"
                            )[:300],
                        )
                        # Refresh list_data so the UI reflects the
                        # cadence change on the next regen / view-switch.
                        # (Next list_generate call will pick up the
                        # external_purchase via the calibrator union.)
                        await send_list_data(ws, agent)
                        _external_purchase_handled = True
            except Exception as e:
                logger.warning(
                    "external-purchase shortcut failed (non-fatal): %s", e
                )

            if _external_purchase_handled:
                continue

            # ── Store Manager Agent: route → fast path or agent path ──
            if AGENT_ENABLED:
                try:
                    from agent_router import route_message
                    from store_manager import run_agent_turn, log_agent_turn
                except ImportError as e:
                    logger.error(f"Store Manager import failed: {e}")
                    AGENT_ENABLED_LOCAL = False
                else:
                    AGENT_ENABLED_LOCAL = True
            else:
                AGENT_ENABLED_LOCAL = False

            agent_handled = False  # Track if Store Manager handled this turn

            # ── #3 Deterministic fast-tool path (sub-second, no LLM) ──
            # Clear "add/remove/skip/set-qty X" commands run the SAME
            # view-aware tools the agent would call, skipping the ~15s LLM
            # round-trip. Anything ambiguous returns None → defer to the agent.
            fast_tool = None
            if AGENT_ENABLED_LOCAL:
                try:
                    fast_tool = _try_fast_tool_command(user_text, agent.session)
                except Exception as e:
                    logger.warning(f"fast-tool path errored, deferring to agent: {e}")
                    fast_tool = None

            if fast_tool is not None:
                if fast_tool.get("suggest"):
                    # Add couldn't be auto-matched — open the suggestion sheet
                    # with real catalog options for the user to tap-approve
                    # (fast, no LLM round-trip).
                    sheet = fast_tool["suggest"]
                    logger.info(f"Fast-tool suggest sheet: '{user_text[:60]}'")
                    log_event("fast_tool_suggest", source="chat",
                              detail=f"{sheet.get('total', 1)} opt(s): {user_text[:60]}")
                    opts = sheet.get("total", 1)
                    await send_message(
                        f"I couldn't auto-match “{sheet['item_name']}” — "
                        + ("here are some options to pick from."
                           if opts > 1 else "here's the closest option.")
                    )
                    await _send_suggestion(ws, agent, sheet)
                    agent.persist_state()
                else:
                    logger.info(f"Fast-tool path: {fast_tool['tool']} '{user_text[:60]}'")
                    log_event("fast_tool_path", source="chat",
                              detail=f"{fast_tool['tool']}: {user_text[:80]}")
                    await send_message(fast_tool["text"])
                    agent.persist_state()
                # agent_handled stays False → the refresh below runs so the UI
                # reflects any change immediately.

            elif AGENT_ENABLED_LOCAL:
                path, parsed = route_message(user_text, agent.session.state)

                if path == "fast":
                    # ── Fast path: regex handled, no LLM cost ──
                    logger.debug(f"Fast path: {parsed.actions[0].intent}")
                    try:
                        agent.process_input(user_text, parsed=parsed)
                    except Exception as e:
                        logger.error(f"Fast path process_input crashed: {e}", exc_info=True)
                        message_buffer.append(
                            "Something went wrong. Try rephrasing or use the buttons."
                        )
                    await flush_messages()
                else:
                    # ── Agent path: full Store Manager with tool calling ──
                    logger.info(f"Agent path: '{user_text[:80]}'")

                    async def _on_progress(phase, label):
                        """Single debounced progress callback — replaces dual thinking + tool_activity."""
                        try:
                            await ws.send_json({
                                "type": "agent_progress",
                                "phase": phase,
                                "label": label,
                            })
                        except Exception as e:
                            logger.debug("Agent progress send failed (client likely disconnected): %s", e)

                    try:
                        # Track if cart was auto-created during this turn
                        _cart_before = getattr(agent.session, "cart_list_id", None)

                        result = await run_agent_turn(
                            user_text,
                            agent.session,
                            on_progress=_on_progress,
                        )

                        # Send agent response. Always send SOMETHING so the
                        # turn terminates cleanly (typing indicator clears on
                        # an 'agent' message) -- never leave the user with a
                        # hung spinner. store_manager already guarantees
                        # non-empty text; this is the defensive backstop.
                        await send_message(
                            result.text
                            or "Done — take a look at your list and let me know if anything needs changing."
                        )

                        # Log usage and persist state
                        log_agent_turn(agent.session, user_text, result)
                        agent.persist_state()
                        agent_handled = True

                        # If a cart was auto-created, send a toast (not a forced tab switch)
                        _cart_after = getattr(agent.session, "cart_list_id", None)
                        if _cart_after and not _cart_before:
                            try:
                                await ws.send_json({
                                    "type": "toast",
                                    "text": "Cart created",
                                    "action": "view_cart",
                                })
                            except Exception as e:
                                logger.debug("Cart-created toast send failed (client likely disconnected): %s", e)

                        if result.error:
                            logger.warning(f"Agent error: {result.error}")
                            log_event("agent_error", source="store_manager",
                                      detail=result.error, level="warning")

                    except Exception as e:
                        logger.error(f"Agent turn failed: {e}", exc_info=True)
                        log_event("error", source="store_manager",
                                  detail=str(e)[:200], level="error")
                        # Fallback to regex path
                        try:
                            regex_result = parse_message(user_text)
                            agent.process_input(user_text, parsed=regex_result)
                        except Exception:
                            message_buffer.append(
                                "Something went wrong. Try rephrasing or use the buttons."
                            )
                        await flush_messages()

            else:
                # ── Legacy path: hybrid NLP (regex + old LLM classifier) ──
                try:
                    regex_result = parse_message(user_text)
                except Exception as e:
                    logger.error(f"parse_message crashed: {e}", exc_info=True)
                    regex_result = ParsedMessage(
                        raw_text=user_text,
                        actions=[ParsedAction(intent=Intent.UNKNOWN, raw_text=user_text)],
                    )
                llm_response_text = None

                routed_to_llm = False
                llm_succeeded = False
                if needs_llm(regex_result):
                    routed_to_llm = True
                    try:
                        from llm_handler import build_rich_list_context
                        list_summary = build_rich_list_context(agent)
                        recent = [{"role": "user" if m.get("role") == "user" else "assistant",
                                   "text": m.get("text", "")[:200]}
                                  for m in (agent.session.messages_sent or [])[-5:]]
                        llm_result = await llm_parse_message(
                            user_text, state=agent.session.state,
                            list_summary=list_summary, recent_messages=recent,
                        )
                        if llm_result:
                            regex_result = llm_result.parsed
                            llm_response_text = llm_result.response_text
                            llm_succeeded = True
                            _log_llm_interaction(user_text, llm_result, agent)
                        else:
                            llm_response_text = _smart_fallback(user_text, agent)
                    except Exception as e:
                        logger.error(f"LLM routing failed: {e}", exc_info=True)
                        llm_response_text = _smart_fallback(user_text, agent)

                if routed_to_llm and not llm_succeeded and llm_response_text:
                    regex_result = ParsedMessage(
                        raw_text=user_text,
                        actions=[ParsedAction(intent=Intent.QUESTION, raw_text=user_text)],
                    )

                add_count = sum(
                    1 for a in regex_result.actions
                    if a.intent == Intent.ADD_ITEM and a.item_name
                )
                if add_count >= 3:
                    await send_message(
                        f"Searching for {add_count} items across stores...",
                        save=False,
                    )

                try:
                    agent.process_input(
                        user_text, parsed=regex_result, llm_response=llm_response_text
                    )
                except Exception as e:
                    logger.error(f"process_input crashed: {e}", exc_info=True)
                    message_buffer.append(
                        "Something went wrong. Try rephrasing or use the buttons."
                    )
                await flush_messages()

            # ── Live verification on batch curation picks ──
            # After curation adds items to the list, verify them against
            # the live store websites before presenting to the user.
            batch_result = getattr(agent, '_batch_curation_result', None)
            if batch_result and batch_result.get('items'):
                matched_items = [i for i in batch_result['items']
                                 if i.get('status') in ('matched', 'catalog_only')]
                if matched_items:
                    try:
                        from live_verify import (
                            extract_picks_from_curation, verify_picks,
                            apply_verification, persist_verification_to_list,
                            format_verification_summary,
                        )
                        picks = extract_picks_from_curation(batch_result)
                        if picks:
                            # Show as progress indicator, NOT a chat bubble
                            try:
                                await ws.send_json({
                                    "type": "agent_progress",
                                    "phase": "verifying",
                                    "label": "Checking prices & stock...",
                                })
                            except Exception as e:
                                logger.debug("Verification progress send failed (client likely disconnected): %s", e)
                            try:
                                verification = await verify_picks(picks)
                                stats = apply_verification(batch_result, verification)
                                persist_verification_to_list(
                                    agent.session.list_id, batch_result, verification)
                                # Only mention notable issues in the agent's
                                # EXISTING response, not a separate bubble
                                summary_text = format_verification_summary(
                                    stats, batch_result)
                                if summary_text:
                                    logger.info(f"Verification: {summary_text}")
                            except Exception as e:
                                logger.warning(
                                    f"Live verification failed, using catalog data: {e}")
                    except ImportError:
                        logger.debug("live_verify module not available, skipping")
                agent._batch_curation_result = None

            # Check if agent has a pending product suggestion to send
            pending = getattr(agent, "_pending_suggestion", None)
            if pending and pending.get("suggestion"):
                await _send_suggestion(ws, agent, pending)
                agent._pending_suggestion = None  # Clear trigger after sending

            # Always refresh the Scheduled/Cart view after a chat turn.
            #
            # The user asked for the Scheduled view to CONSISTENTLY reflect
            # chat actions ("it doesn't have to work instant but it needs to
            # be consistent"). The previous version skipped the refresh unless
            # the agent happened to call one of a hard-coded set of tools, but
            # other list-mutating tools (e.g. stop_suggesting_item,
            # set_item_frequency, set_brand_rule) fell OUTSIDE that set — so
            # those edits hit the DB while the view stayed stale until the next
            # reconnect. A refresh is cheap (one list build + WS send) and the
            # frontend only re-renders when the data actually differs, so the
            # right trade-off here is to always refresh and never lie about the
            # list state.
            await send_list_data(ws, agent)

            # Check if we need to build carts
            if agent.session.state == State.BUILDING:
                await send_message("Building carts... this may take a minute.", save=False)
                await ws.send_json({"type": "state_change", "state": "building"})

                async def _cart_progress_chat(step, **kwargs):
                    try:
                        await ws.send_json({"type": "cart_progress", "step": step, **kwargs})
                    except Exception as e:
                        logger.debug("Cart progress send failed (client likely disconnected): %s", e)

                cart_success = False
                try:
                    await agent.build_carts(progress_cb=_cart_progress_chat)
                except Exception as e:
                    logger.error(f"Cart building error: {e}", exc_info=True)
                    log_event("cart_error", source="cart_builder", detail=str(e)[:200], level="error")
                    err_str = str(e).lower()
                    if "login" in err_str or "not logged" in err_str or "auth" in err_str:
                        buffer_message(
                            "Not logged into store websites.\n"
                            "Close your regular Edge browser, tap Ready Cart again, "
                            "then log into the stores in the Edge window that opens."
                        )
                    elif "timeout" in err_str or "network" in err_str:
                        buffer_message("Cart building timed out. The store website may be slow. Try again in a few minutes.")
                    elif "browser" in err_str or "playwright" in err_str or "executable" in err_str:
                        buffer_message("Could not open the browser. Make sure Edge is closed and try again.")
                    else:
                        buffer_message(f"Cart building hit an issue: {str(e)[:100]}. Check the logs for details.")

                # Determine success from actual report
                report = agent.session.cart_report
                if report and "error" not in report:
                    total_added = sum(
                        sr.get("items_added", 0) for sr in report.get("stores", {}).values()
                    )
                    cart_success = total_added > 0

                # Capture any error messages before flushing
                error_detail = " ".join(message_buffer) if message_buffer else ""
                await flush_messages()

                # Build a detailed completion message
                if cart_success:
                    summary = report.get("summary", "Carts ready!")
                    cart_msg = f"Your carts are ready! {summary}\nHead to the store websites to place your orders."
                elif report and report.get("error") == "availability_check":
                    # Pre-build availability gate -- the detailed item list +
                    # alternatives was already sent via the agent's messages above.
                    cart_msg = report.get("summary") or "A few items need attention before I build the cart — swap or skip them above, then build again."
                else:
                    # Include the actual error in the banner so the user doesn't have to dig
                    if error_detail:
                        cart_msg = error_detail[:200]
                    elif report and report.get("error"):
                        # Sanitize — only show first line, no browser details
                        clean_err = str(report['error']).split("\n")[0][:120]
                        if any(kw in clean_err.lower() for kw in ["playwright", "browser", "launch", "chromium"]):
                            cart_msg = "Could not open the browser. Make sure Edge is closed and try again."
                        else:
                            cart_msg = f"Cart building failed: {clean_err}"
                    else:
                        cart_msg = "Cart building failed. No items were added to your carts."

                await ws.send_json({
                    "type": "cart_complete",
                    "success": cart_success,
                    "message": cart_msg,
                })
                await ws.send_json({"type": "state_change", "state": "idle"})
                await send_list_data(ws, agent)

                # If we're now in AWAITING_ALT, the messages are already buffered
                if agent.session.state == State.AWAITING_ALT:
                    await flush_messages()

            # Check if cycle is done (skip for Store Manager agent turns —
            # agent keeps the WS alive and manages its own state)
            if not agent_handled:
                if agent.session.state == State.DONE:
                    await asyncio.sleep(0.5)
                    await send_message("\n---\nSession complete. Refresh the page to start a new cycle.")
                    await ws.send_json({"type": "state_change", "state": "done"})
                    break

                # Send state change if state transitioned
                if agent.session.state == State.IDLE:
                    await ws.send_json({"type": "state_change", "state": "idle"})
                    break

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
        log_event("ws_disconnect", source="app")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        log_event("error", source="app", detail=f"WebSocket error: {e}", level="error")
        try:
            await send_message(f"Error: {e}")
        except Exception as e:
            logger.debug("Error message send failed (client likely disconnected): %s", e)
    finally:
        app_state.connections.discard(ws)
        if not app_state.connections:
            app_state.active_agent = None


# ============================================================
# ACTION HANDLER
# ============================================================

async def handle_change_qty(ws, agent, data, message_buffer, flush_messages):
    list_item_id = data.get("listItemId")
    new_qty = max(0, round(data.get("quantity", 1)))
    if list_item_id is not None:
        try:
            # Get old qty + item_id + added_by before the update
            old_qty, li_item_id, added_by = _get_list_item_info(list_item_id)
            agent.update_item_direct(list_item_id, "change_qty", quantity=new_qty)
            await send_item_update(ws, list_item_id, {"quantity": new_qty})
            # Only emit preference signal for AI-suggested items (not user-added)
            if li_item_id and added_by == "system":
                _emit_pref_signal(li_item_id, "qty_change", "quantity",
                                  old_value={"qty": old_qty},
                                  new_value={"qty": new_qty},
                                  context={"list_item_id": list_item_id})
        except Exception as e:
            logger.error(f"change_qty failed: {e}", exc_info=True)
            log_event("error", source="action", detail=f"change_qty: {e}", level="error")
        _mark_user_edits(list_item_id)
        agent.persist_state()
        await send_list_data(ws, agent)


async def handle_remove_item(ws, agent, data, message_buffer, flush_messages):
    list_item_id = data.get("listItemId")
    if list_item_id is not None:
        try:
            _, li_item_id, added_by = _get_list_item_info(list_item_id)
            agent.update_item_direct(list_item_id, "remove")
            await send_item_removed(ws, list_item_id)
            # Only emit preference signal for AI-suggested items (not user-added)
            if li_item_id and added_by == "system":
                _emit_pref_signal(li_item_id, "item_skip", "item_inclusion",
                                  new_value={"action": "skip"},
                                  context={"list_item_id": list_item_id})
            # Tier-0 drop tracking: log to skip_events + push the
            # next_restock_date past the upcoming list's coverage so
            # the item doesn't reappear immediately on regen. Reason
            # field is optional and currently unset from the app drop
            # (the reason chip ribbon will populate it in a later
            # slice). Always called -- both system-suggested and
            # user-added items count as "I don't want this right now".
            try:
                from skip_tracking import apply_tier_zero_drop
                tier0_result = apply_tier_zero_drop(
                    list_item_id=list_item_id,
                    source="curator_drop",
                    reason=data.get("reason"),
                )
                log_event("tier0_drop", source="action",
                          detail=f"list_item={list_item_id} item={tier0_result.get('item_id')} "
                                 f"next_restock={tier0_result.get('next_restock_date')}")
            except Exception as e:
                logger.warning("Tier-0 drop tracking failed (non-fatal): %s", e)
            # Offer a one-tap upgrade chip: "🛒 Had it" toast button.
            # If tapped, the drop becomes an external-purchase
            # (advances cadence math) instead of just a "skip this
            # cycle". Toast auto-dismisses; ignoring keeps the default
            # Tier-0 behavior.
            try:
                if li_item_id:
                    with get_db(row_factory=True) as conn:
                        item_row = conn.execute(
                            "SELECT name FROM items WHERE id = ?", (li_item_id,)
                        ).fetchone()
                    item_name = item_row["name"] if item_row else "item"
                    await ws.send_json({
                        "type": "toast",
                        "text": f"Removed {item_name}",
                        "action": "mark_have_it",
                        "context": {
                            "item_id": li_item_id,
                            "name": item_name,
                            "list_item_id": list_item_id,
                        },
                    })
            except Exception as e:
                logger.debug("Have-it toast send failed (client likely disconnected): %s", e)
        except Exception as e:
            logger.error(f"remove_item failed: {e}", exc_info=True)
            log_event("error", source="action", detail=f"remove_item: {e}", level="error")
        _mark_user_edits(list_item_id)
        agent.persist_state()
        await send_list_data(ws, agent)


async def handle_mark_have_it(ws, agent, data, message_buffer, flush_messages):
    """One-tap upgrade from "skip this cycle" to "I have it":
    record an external_purchase row so cadence math advances. The
    matching list_item is already marked skipped from handle_remove_item
    (which fires first) -- this just adds the purchase signal.

    Payload (from the toast click):
      { item_id, name, list_item_id }

    Effect:
      - INSERT external_purchases row (item_id, raw_name=name, date=today,
        source='chat_chip'). The cadence calibrator picks this up on
        the next list_generate / regenerate.
    """
    item_id = data.get("item_id") or data.get("itemId")
    name = data.get("name") or "item"
    if not item_id:
        return

    try:
        from external_purchases import record_external_purchase
        rec = record_external_purchase(
            raw_name=name,
            source="chat_chip",
        )
        log_event("external_purchase", source="chip",
                  detail=f"item_id={item_id} name='{name}' "
                         f"ext_id={rec.get('id') if rec else None}")
        # Replace the previous toast with a confirmation that the
        # upgrade landed. Short text so the user knows the chip
        # took effect.
        try:
            await ws.send_json({
                "type": "toast",
                "text": f"Got it -- skipping {name} this cycle and next",
            })
        except Exception:
            pass
        await send_list_data(ws, agent)
    except Exception as e:
        logger.error("mark_have_it failed: %s", e, exc_info=True)
        try:
            await ws.send_json({
                "type": "toast",
                "text": "Couldn't save that, try again.",
            })
        except Exception:
            pass


def _is_rejection_feedback(text):
    """True when 'wrong item' feedback means 'we don't buy this' (remove +
    stop suggesting) rather than 'swap it for a different product of the same
    thing'. The UI prompts "What should this be instead?", but users also type
    rejections like "we never buy soft drinks" -- which previously got handled
    as a swap, returning a near-identical product (7Up -> another 7Up) so the
    list looked unchanged. Detect the rejection so we actually drop the item."""
    if not text:
        return False
    t = " " + text.lower().strip() + " "
    REJECT = (
        "never", "don't buy", "dont buy", "do not buy", "don't want",
        "dont want", "not want", "won't buy", "wont buy", "remove", "delete",
        "get rid", "take off", "take it off", "stop suggest", "don't suggest",
        "dont suggest", "no more", "we don't", "we dont", "not this",
        "don't need", "dont need", "no need", "not needed", "don't like",
        "dont like", "not for us", "we don't drink", "we dont drink",
        "we don't eat", "we dont eat", "wrong category", "shouldn't be",
        "shouldnt be", "stop showing", "don't show", "dont show",
    )
    return any(k in t for k in REJECT)


async def handle_item_pref(ws, agent, data, message_buffer, flush_messages):
    # SKU-level feedback: block_brand, pin_product, wrong_item, prefer_store
    pref_type = data.get("prefType")
    item_id = data.get("itemId")       # items table ID
    value = data.get("value")
    list_item_id = data.get("listItemId")  # list_items table ID
    feedback_text = data.get("feedbackText")  # user text for wrong_item
    store_id = data.get("storeId")
    if not (pref_type and item_id is not None):
        return

    # 1. Save preference + emit signal (works for all types)
    _save_item_pref(item_id, pref_type, value)
    log_event("item_pref", source="app",
              detail=f"{pref_type}: item={item_id} value={value}")

    dimension = ("brand" if pref_type == "block_brand"
                 else "product" if pref_type in ("pin_product", "unpin_product", "wrong_item")
                 else "store")
    new_value = ({"brand": value} if pref_type == "block_brand"
                 else {"catalog_id": value} if pref_type in ("pin_product", "wrong_item")
                 else {"store_id": value})
    context = {"feedback_text": feedback_text} if feedback_text else None
    _emit_pref_signal(item_id, pref_type, dimension,
                      new_value=new_value, context=context)

    # 2. Type-specific active behavior
    list_id = (getattr(agent.session, "suggestion_list_id", None)
               or getattr(agent.session, "cart_list_id", None)
               or agent.session.list_id)

    if pref_type in ("pin_product", "unpin_product"):
        # Pin/unpin: just toast + refresh (no item change)
        toast_text = "Pinned — I'll always pick this one" if pref_type == "pin_product" else "Unpinned"
        try:
            await ws.send_json({"type": "toast", "text": toast_text})
        except Exception as e:
            logger.debug("Pin/unpin toast send failed (client likely disconnected): %s", e)
        await send_list_data(ws, agent)

    elif pref_type == "block_brand" and list_item_id and list_id:
        # Never Brand: remove item → find replacement without this brand → add
        old_name = _get_list_item_name(list_item_id) or "that item"
        agent.update_item_direct(list_item_id, "remove")
        await send_item_removed(ws, list_item_id)

        replacement = _find_replacement_product(
            item_id, old_name, store_id,
            exclude_brand=value)

        if replacement:
            from list_generate import list_add_item
            new_li_id = list_add_item(
                list_id, replacement["product_name"],
                store_id=replacement.get("store_id"),
                item_id=item_id,
                quantity=1,
                estimated_price=replacement.get("price"),
                catalog_id=replacement.get("catalog_id"),
                status="approved",
            )
            toast_text = f"Replaced with {replacement['product_name']} (blocked {value})"
        else:
            toast_text = f"Removed — no alternative found without {value}"

        try:
            await ws.send_json({"type": "toast", "text": toast_text})
        except Exception as e:
            logger.debug("Replacement toast send failed (client likely disconnected): %s", e)
        _mark_user_edits(list_item_id)
        agent.persist_state()
        await send_list_data(ws, agent)

    elif pref_type == "wrong_item" and list_item_id and list_id:
        # Wrong Item: remove item → either stop suggesting it (rejection) or
        # use the feedback text to find a replacement (swap request).
        old_name = _get_list_item_name(list_item_id) or "that item"
        agent.update_item_direct(list_item_id, "remove")
        await send_item_removed(ws, list_item_id)

        if feedback_text and _is_rejection_feedback(feedback_text):
            # "We never buy this" -> drop it AND stop suggesting the item, so
            # it doesn't come back next cycle. Previously this was treated as a
            # swap and returned a near-identical product (e.g. 7Up -> 7Up), so
            # the list looked unchanged -- the exact "nothing changes" bug.
            _save_item_pref(item_id, "stop_suggesting", "user:wrong_item")
            log_event("item_pref", source="app",
                      detail=f"wrong_item->stop_suggesting item={item_id} "
                             f"feedback={feedback_text[:60]}")
            toast_text = f"Removed {old_name} — I won't suggest it again"
        elif feedback_text:
            replacement = _find_replacement_product(
                item_id, old_name, store_id,
                search_hint=feedback_text,
                exclude_catalog_ids=[value] if value else None)

            if replacement:
                from list_generate import list_add_item
                new_li_id = list_add_item(
                    list_id, replacement["product_name"],
                    store_id=replacement.get("store_id"),
                    item_id=item_id,
                    quantity=1,
                    estimated_price=replacement.get("price"),
                    catalog_id=replacement.get("catalog_id"),
                    status="approved",
                )
                toast_text = f"Based on your feedback, replaced with {replacement['product_name']}"
            else:
                toast_text = f"Removed {old_name} — couldn't find a match for \"{feedback_text}\""
        else:
            toast_text = f"Removed {old_name}"

        # Understand the free-text via the SAME LLM step delivered-order
        # feedback uses, so "too expensive" / "we want organic" / "wrong brand"
        # becomes a DURABLE household rule the validator honors (avoid_brand /
        # prefer_attribute / prefer_cheaper / ...) — not just a one-off swap.
        # The product-level block was already saved + is consumed by the
        # validator; this adds the broader, category-scoped understanding.
        # Best-effort + budget-capped; non-blocking on failure.
        if feedback_text and feedback_text.strip():
            try:
                from household_rules import capture_item_feedback
                cap = await capture_item_feedback(feedback_text, old_name)
                if cap.get("summary"):
                    log_event("item_feedback_rules", source="app",
                              detail=f"item={item_id}: {cap['summary'][:140]}")
                    toast_text += f" · Noted: {cap['summary']}"
            except Exception as e:
                logger.debug("item feedback rule capture failed (non-fatal): %s", e)

        try:
            await ws.send_json({"type": "toast", "text": toast_text})
        except Exception as e:
            logger.debug("Wrong-item toast send failed (client likely disconnected): %s", e)
        _mark_user_edits(list_item_id)
        agent.persist_state()
        await send_list_data(ws, agent)

    else:
        # Fallback: just refresh
        await send_list_data(ws, agent)
    return


async def handle_find_alternatives(ws, agent, data, message_buffer, flush_messages):
    """
    Look up alternative products for a list item and send them back to
    the React AlternativesBottomSheet. The frontend renders up to 3
    candidates so the user can swap to a "better" choice without
    leaving the app.

    scope:
      "same_vendor"   -> show options at the same store
      "other_vendors" -> show options at DIFFERENT stores (for cart
                         consolidation: "find this in Kibsons so I can
                         order everything from one place").
    """
    list_item_id = data.get("listItemId")
    scope = data.get("scope", "same_vendor")
    if not list_item_id:
        return

    alternatives = []
    try:
        with get_db(row_factory=True) as conn:
            row = conn.execute(
                "SELECT item_id, store_id, catalog_id FROM list_items WHERE id = ?",
                (list_item_id,),
            ).fetchone()
            if not row:
                await ws.send_json({
                    "type": "alternatives_result",
                    "listItemId": list_item_id,
                    "scope": scope,
                    "alternatives": [],
                })
                return

            from frontend_bridge import find_alternatives_for_item
            alternatives = find_alternatives_for_item(
                item_id=row["item_id"],
                store_id=row["store_id"],
                current_catalog_id=row["catalog_id"],
                scope=scope,
                conn=conn,
                limit=3,
            )
    except Exception as e:
        logger.error("handle_find_alternatives failed: %s", e, exc_info=True)
        alternatives = []

    log_event("find_alternatives", source="app",
              detail=f"list_item={list_item_id} scope={scope} found={len(alternatives)}")
    try:
        await ws.send_json({
            "type": "alternatives_result",
            "listItemId": list_item_id,
            "scope": scope,
            "alternatives": alternatives,
        })
    except Exception as e:
        logger.debug("alternatives_result send failed (client likely disconnected): %s", e)


def _swap_differentiators(chosen, original):
    """Thin adapter — the canonical pure decision lives in household_rules so the
    in-app alternatives sheet and the WhatsApp swap pick learn X-vs-Y identically.
    See household_rules.swap_differentiators."""
    from household_rules import swap_differentiators
    return swap_differentiators(chosen, original)


def _learn_from_swap(item_id, chosen, original, category):
    """Thin adapter for the in-app alternatives sheet (signal_source=app_ui). The
    shared X-vs-Y comparison learning lives in household_rules.learn_from_swap so
    WhatsApp swap picks generalize the same way (see _perform_swap_commit)."""
    from household_rules import learn_from_swap
    return learn_from_swap(item_id, chosen, original, category, signal_source="app_ui")


async def handle_accept_alternative(ws, agent, data, message_buffer, flush_messages):
    """
    Swap a list item to a chosen alternative + log the choice as a
    preference. User decisions feed back into the recommendation engine
    so next time the agent picks something it remembers what we liked.
    """
    list_item_id = data.get("listItemId")
    catalog_id = data.get("catalogId")
    scope = data.get("scope", "same_vendor")
    if not (list_item_id and catalog_id):
        return

    pinned_item_id = None
    swap_chosen = swap_original = None
    swap_category = ""
    try:
        with get_db(row_factory=True) as conn:
            prod = conn.execute(
                "SELECT id, product_name, price, store_id, brand, size, "
                "is_organic, origin_country FROM product_catalog WHERE id = ?",
                (catalog_id,),
            ).fetchone()
            if not prod:
                await ws.send_json({"type": "toast", "text": "That product isn't available anymore."})
                return

            li = conn.execute(
                "SELECT li.id, li.item_id, li.store_id, li.list_id, "
                "li.catalog_id AS orig_catalog_id, i.category "
                "FROM list_items li JOIN items i ON i.id = li.item_id "
                "WHERE li.id = ?",
                (list_item_id,),
            ).fetchone()
            if not li:
                return

            # Capture the REPLACED product (Y) BEFORE the swap overwrites it, so
            # we can compare the chosen X vs Y and learn WHY X was preferred —
            # the cleanest preference signal we get.
            swap_chosen = dict(prod)
            swap_category = li["category"] or ""
            if li["orig_catalog_id"]:
                y = conn.execute(
                    "SELECT brand, price, size, is_organic, origin_country "
                    "FROM product_catalog WHERE id = ?",
                    (li["orig_catalog_id"],),
                ).fetchone()
                swap_original = dict(y) if y else None

            # Cross-vendor swaps move the item to the new store. Same-vendor
            # swaps keep the existing store_id (the chosen product is also
            # there, just a different SKU).
            new_store_id = prod["store_id"] if scope == "other_vendors" else li["store_id"]
            conn.execute(
                """UPDATE list_items
                   SET catalog_id = ?, store_id = ?, estimated_price = ?,
                       item_name_text = ?, status = 'approved',
                       user_action = 'approved'
                   WHERE id = ?""",
                (catalog_id, new_store_id, prod["price"], prod["product_name"], list_item_id),
            )
            pinned_item_id = li["item_id"]
    except Exception as e:
        logger.error("handle_accept_alternative failed: %s", e, exc_info=True)
        try:
            await ws.send_json({"type": "toast", "text": "Couldn't swap that item. Try again."})
        except Exception:
            pass
        return

    # Preference logging -- pin this catalog_id so subsequent
    # recommendations for the same item keep this choice. Done outside
    # the DB context manager because _save_item_pref opens its own.
    if pinned_item_id:
        _save_item_pref(pinned_item_id, "pin_product", catalog_id)
        log_event("item_pref", source="app",
                  detail=f"pin_product via accept_alternative: item={pinned_item_id} catalog={catalog_id} scope={scope}")
        _emit_pref_signal(pinned_item_id, "pin_product", "product",
                          new_value={"catalog_id": catalog_id},
                          context={"source": "accept_alternative", "scope": scope})

        # X-vs-Y comparison learning. The pin above locks the exact SKU; this
        # GENERALIZES the choice by diffing the chosen product against the one
        # it replaced and capturing the differentiator (brand / organic /
        # cheaper) as a durable preference both surfaces honor. Best-effort.
        if swap_original is not None:
            try:
                learned = _learn_from_swap(
                    pinned_item_id, swap_chosen, swap_original, swap_category)
                if learned:
                    log_event("swap_learned", source="app",
                              detail=f"item={pinned_item_id}: {', '.join(learned)}")
            except Exception as e:
                logger.debug("swap comparison learning failed (non-fatal): %s", e)

    _mark_user_edits(list_item_id)
    agent.persist_state()

    toast_text = ("Swapped — I'll remember this for next time"
                  if scope == "same_vendor"
                  else "Moved to the other store — preference saved")
    try:
        await ws.send_json({"type": "toast", "text": toast_text})
    except Exception as e:
        logger.debug("accept_alternative toast send failed (client likely disconnected): %s", e)

    await send_list_data(ws, agent)


async def handle_reorder_from_history(ws, agent, data, message_buffer, flush_messages):
    """
    Add a delivered-order item back to the Scheduled list with one tap.
    Lets the user re-stock things they liked without typing.

    Payload:
        orderItemId  -- order_items.id (the row in the past order)

    Behavior:
        - Resolves the item's item_id (if linked) and raw_name.
        - Adds it to the active suggestion_list (preferred), falling
          back to a new list if none exists.
        - Sends a toast + refreshed list_data.
    """
    order_item_id = data.get("orderItemId")
    if not order_item_id:
        return

    suggestion_list_id = (getattr(agent.session, "suggestion_list_id", None)
                          or agent.session.list_id)
    if not suggestion_list_id:
        try:
            await ws.send_json({"type": "toast", "text": "Start a list first, then I can add this."})
        except Exception:
            pass
        return

    try:
        with get_db(row_factory=True) as conn:
            row = conn.execute("""
                SELECT oi.id, oi.raw_name, oi.quantity, oi.unit_price,
                       oi.item_id, o.store_id
                FROM order_items oi
                JOIN orders o ON oi.order_id = o.id
                WHERE oi.id = ?
            """, (order_item_id,)).fetchone()
        if not row:
            await ws.send_json({"type": "toast", "text": "That order item is no longer available."})
            return

        from list_generate import list_add_item
        added = list_add_item(
            suggestion_list_id,
            row["raw_name"] or "Re-ordered item",
            store_id=row["store_id"],
            item_id=row["item_id"],
            quantity=max(1, int(row["quantity"] or 1)),
            estimated_price=row["unit_price"] or None,
            status="pending",
        )
        if not added:
            await ws.send_json({"type": "toast", "text": "Couldn't re-add that item. Try via chat."})
            return

        log_event("reorder_from_history", source="app",
                  detail=f"order_item={order_item_id} -> list={suggestion_list_id}")
        _emit_pref_signal(row["item_id"], "reorder_from_history", "product",
                          new_value={"order_item_id": order_item_id,
                                     "raw_name": row["raw_name"]})
    except Exception as e:
        logger.error("handle_reorder_from_history failed: %s", e, exc_info=True)
        try:
            await ws.send_json({"type": "toast", "text": "Couldn't re-add that item."})
        except Exception:
            pass
        return

    try:
        await ws.send_json({
            "type": "toast",
            "text": f"Added {row['raw_name']} to Scheduled",
            "action": "view_scheduled",
        })
    except Exception:
        pass
    agent.persist_state()
    await send_list_data(ws, agent)


async def handle_add_suggestion(ws, agent, data, message_buffer, flush_messages):
    # User approved a suggested product from the bottom sheet.
    catalog_id = data.get("catalogId")
    item_name = data.get("itemName", "Unknown")
    quantity = data.get("quantity", 1)
    # Add the EXACT product they approved, to the list they're VIEWING.
    # Two prior bugs lived here: (1) it added to session.list_id instead of
    # the active Scheduled list, and (2) it dropped catalog_id and re-matched
    # by NAME — re-running the very matcher that failed (which is why the
    # sheet was shown). Now we resolve the viewed list and pin the chosen
    # product's store/price/category so what lands is what the user picked.
    try:
        from agent_tools import _resolve_edit_list
        list_id = _resolve_edit_list(agent.session)
    except Exception:
        list_id = agent.session.list_id if agent.session else None
    if catalog_id is not None and list_id:
        try:
            from list_generate import list_add_item
            store_id = store_name = price = category = None
            with get_db(row_factory=True) as conn:
                row = conn.execute(
                    """SELECT pc.price, pc.store_id, pc.category,
                              s.name AS store_name
                       FROM product_catalog pc
                       JOIN stores s ON s.id = pc.store_id
                       WHERE pc.id = ?""",
                    (catalog_id,),
                ).fetchone()
            if row:
                store_id = row["store_id"]
                store_name = row["store_name"]
                price = row["price"]
                category = row["category"]
            result = list_add_item(
                list_id,
                item_name,
                store_id=store_id,
                store_name=store_name,
                quantity=quantity,
                estimated_price=price,
                catalog_id=catalog_id,
                category_override=category,
                status="approved",
            )
            if result:
                log_event("suggestion_accepted", source="suggestion",
                          detail=f"Added catalog_id={catalog_id}, item='{item_name}'")
                # Emit preference signal — user accepted this product
                _emit_pref_signal(None, "suggestion_accepted", "product",
                                  new_value={"catalog_id": catalog_id, "item_name": item_name},
                                  context={"list_id": list_id})
                message_buffer.append(f"Added: {item_name}")
            else:
                log_event("suggestion_add_failed", source="suggestion",
                          detail=f"list_add_item returned None for '{item_name}'", level="warning")
                message_buffer.append(f"Couldn't add '{item_name}' to the list. Try again.")
        except Exception as e:
            logger.error(f"add_suggestion failed: {e}", exc_info=True)
            log_event("suggestion_error", source="suggestion",
                      detail=f"add_suggestion: {e}", level="error")
            message_buffer.append("Something went wrong adding that item. Try again or add it manually.")
        # Clear pending suggestions
        agent.session.pending_suggestions = None
        await flush_messages()
        await send_list_data(ws, agent)
        # Dismiss the suggestion sheet
        await ws.send_json({"type": "suggestion_dismiss"})
    return


async def handle_next_suggestion(ws, agent, data, message_buffer, flush_messages):
    # User wants to see the next alternative in the suggestion bottom sheet
    suggestions = agent.session.pending_suggestions
    if suggestions and suggestions.get("products"):
        idx = suggestions.get("current_index", 0) + 1
        products = suggestions["products"]
        if idx < len(products):
            suggestions["current_index"] = idx
            product = products[idx]
            log_event("suggestion_next", source="suggestion",
                      detail=f"Showing alternative {idx+1}/{len(products)}: {product['product_name']}")
            await ws.send_json({
                "type": "suggestion",
                "itemName": suggestions["item_name"],
                "isAlternative": True,
                "currentIndex": idx,
                "totalOptions": len(products),
                "quantity": suggestions.get("quantity", 1),
                "product": {
                    "catalogId": product["catalog_id"],
                    "name": product["product_name"],
                    "brand": product.get("brand", ""),
                    "price": product.get("price"),
                    "size": product.get("size", ""),
                    "image": product.get("image_url", ""),
                    "isOrganic": product.get("is_organic", False),
                    "storeName": product.get("store_name", ""),
                },
                "hasMore": idx + 1 < len(products),
            })
        else:
            log_event("suggestion_exhausted", source="suggestion",
                      detail=f"No more alternatives for '{suggestions['item_name']}'")
            message_buffer.append(f"No more alternatives for '{suggestions['item_name']}'.")
            await flush_messages()
            await ws.send_json({"type": "suggestion_dismiss"})
            agent.session.pending_suggestions = None
    else:
        log_event("suggestion_next_none", source="suggestion",
                  detail="next_suggestion called but no pending suggestions", level="warning")
    return


async def handle_skip_suggestion(ws, agent, data, message_buffer, flush_messages):
    # User skipped the suggestion entirely
    suggestions = agent.session.pending_suggestions
    item_name = suggestions.get("item_name", "Unknown") if suggestions else "Unknown"
    log_event("suggestion_skipped", source="suggestion",
              detail=f"User skipped suggestion for '{item_name}'")
    # Emit signal — user rejected these catalog results
    if suggestions:
        _emit_pref_signal(None, "suggestion_skipped", "product",
                          new_value={"item_name": item_name, "query": suggestions.get("query", "")},
                          context={"list_id": agent.session.list_id if agent.session else None})
    agent.session.pending_suggestions = None
    await ws.send_json({"type": "suggestion_dismiss"})
    message_buffer.append(f"Skipped. '{item_name}' won't be added.")
    await flush_messages()
    return


async def handle_get_order_tracking(ws, agent, data, message_buffer, flush_messages):
    # Frontend requests current order tracking state
    tracking = _build_order_tracking()
    await ws.send_json({"type": "order_tracking", **tracking})
    return


async def handle_submit_rating(ws, agent, data, message_buffer, flush_messages):
    # User finalized a delivery rating
    order_id = data.get("orderId")
    item_ratings = data.get("items", [])  # [{orderItemId, feedback}]
    feedback_text = data.get("text", "")
    llm_response = data.get("llmResponse", "")
    if order_id:
        try:
            _save_rating(order_id, item_ratings, feedback_text, llm_response)
            _emit_rating_signals(order_id, item_ratings, feedback_text)
            # Parse the free-text note into durable household rules
            # (e.g. "no more Angus beef" -> avoid Angus in meat;
            #  "buy varied pasta" -> prefer_variety in pasta).
            rule_summary = ""
            if feedback_text and feedback_text.strip():
                try:
                    from household_rules import capture_feedback
                    cap = await capture_feedback(feedback_text, int(order_id))
                    rule_summary = cap.get("summary", "")
                except Exception as e:
                    logger.warning("feedback rule capture failed (non-fatal): %s", e)
            log_event("rating_submitted", source="rating",
                      detail=f"order={order_id}, items={len(item_ratings)}, "
                             f"has_text={bool(feedback_text)}, rules={rule_summary or 'none'}")
            if rule_summary:
                message_buffer.append(
                    f"Thanks! Going forward I'll {rule_summary}."
                )
            else:
                message_buffer.append("Thanks for the feedback! I'll use it to improve future orders.")
        except Exception as e:
            logger.error(f"submit_rating failed: {e}", exc_info=True)
            log_event("rating_error", source="rating",
                      detail=f"submit_rating: {e}", level="error")
            message_buffer.append("Something went wrong saving your rating. Your feedback may not have been recorded.")
        await flush_messages()
        # Send updated tracking (delivered order should disappear)
        tracking = _build_order_tracking()
        await ws.send_json({"type": "order_tracking", **tracking})
    return


async def handle_dismiss_rating(ws, agent, data, message_buffer, flush_messages):
    # User dismissed the rating without completing it
    order_id = data.get("orderId")
    if order_id:
        try:
            _dismiss_rating(order_id)
            log_event("rating_dismissed", source="rating",
                      detail=f"order={order_id}")
        except Exception as e:
            logger.error(f"dismiss_rating failed: {e}", exc_info=True)
        # Send updated tracking
        tracking = _build_order_tracking()
        await ws.send_json({"type": "order_tracking", **tracking})
    return


async def handle_get_llm_feedback(ws, agent, data, message_buffer, flush_messages):
    # User typed improvement text and wants LLM conversational response
    feedback_text = data.get("text", "")
    item_feedback = data.get("items", [])
    if feedback_text:
        try:
            from llm_handler import llm_process_rating_feedback
            llm_reply = await llm_process_rating_feedback(feedback_text, item_feedback)
            await ws.send_json({
                "type": "rating_llm_response",
                "response": llm_reply or "Got it, I'll keep that in mind for next time!",
            })
            log_event("rating_llm_feedback", source="rating",
                      detail=f"text={feedback_text[:100]}")
        except Exception as e:
            logger.error(f"get_llm_feedback failed: {e}", exc_info=True)
            await ws.send_json({
                "type": "rating_llm_response",
                "response": "Got it, I'll keep that in mind for next time!",
            })
    return


async def handle_feedback_alert(ws, agent, data, message_buffer, flush_messages):
    # Alert feedback with detail text from the ! button
    msg_id = data.get("msg_id")
    detail = data.get("detail", "")
    _log_feedback(msg_id, "alert", agent, detail=detail)
    return


# ── Cart Lifecycle Actions (two-entity model) ──

async def handle_create_cart(ws, agent, data, message_buffer, flush_messages):
    # User tapped "New Cart" button — create an empty cart
    if agent.session.cart_list_id:
        # Already have an active cart — ask user to dismiss first
        message_buffer.append("You already have an active cart. Dismiss it first to start a new one.")
        await flush_messages()
        return

    # Create a new empty cart via the existing start_user_order (silent=True)
    agent.start_user_order(silent=True)
    agent.persist_state()
    log_event("cart_created", source="action",
              detail=f"cart_id={agent.session.cart_list_id}")
    message_buffer.append("New cart created! Tell me what you need, or add items from your suggestions.")
    await flush_messages()
    await send_list_data(ws, agent)
    # Tell frontend to switch to cart tab
    await ws.send_json({"type": "view_switch", "view": "cart"})
    return


async def handle_dismiss_cart(ws, agent, data, message_buffer, flush_messages):
    # User tapped dismiss — soft-delete the active cart
    cart_id = agent.session.cart_list_id
    if not cart_id:
        message_buffer.append("No active cart to dismiss.")
        await flush_messages()
        return

    try:
        with get_db() as conn:
            conn.execute("""
                UPDATE shopping_lists
                SET dismissed_at = datetime('now'), status = 'done',
                    updated_at = datetime('now')
                WHERE id = ?
            """, (cart_id,))

        agent.session.cart_list_id = None
        # Point list_id back to suggestion if available
        if agent.session.suggestion_list_id:
            agent.session.list_id = agent.session.suggestion_list_id
        agent.session.state = State.DONE
        agent.persist_state()

        log_event("cart_dismissed", source="action",
                  detail=f"cart_id={cart_id}")
        message_buffer.append("Cart dismissed.")
        await flush_messages()
        await send_list_data(ws, agent)
        # Tell frontend to switch to suggestions tab
        await ws.send_json({"type": "view_switch", "view": "suggestions"})
    except Exception as e:
        logger.error(f"dismiss_cart failed: {e}", exc_info=True)
        message_buffer.append("Something went wrong dismissing the cart.")
        await flush_messages()
    return


async def handle_cherry_pick(ws, agent, data, message_buffer, flush_messages):
    # User tapped "+" on a suggestion item to copy it to the cart
    list_item_id = data.get("listItemId")
    if not list_item_id:
        return

    # Auto-create cart if needed
    if not agent.session.cart_list_id:
        agent.start_user_order(silent=True)

    cart_id = agent.session.cart_list_id
    try:
        with get_db(row_factory=True) as conn:
            # Read the source item from suggestions (use actual column names)
            src = conn.execute("""
                SELECT item_id, item_name_text, quantity, catalog_id,
                       store_id, user_note, estimated_price, added_by
                FROM list_items WHERE id = ?
            """, (list_item_id,)).fetchone()

            if not src:
                message_buffer.append("Couldn't find that item.")
                await flush_messages()
                return

            # Insert a copy into the cart
            conn.execute("""
                INSERT INTO list_items
                (list_id, item_id, item_name_text, quantity, catalog_id,
                 store_id, user_note, estimated_price, status, added_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'approved', 'cherry_pick')
            """, (
                cart_id, src["item_id"], src["item_name_text"], src["quantity"],
                src["catalog_id"], src["store_id"], src["user_note"],
                src["estimated_price"],
            ))

        item_label = src["item_name_text"] or "item"
        log_event("cherry_pick", source="action",
                  detail=f"item='{item_label}' from suggestion → cart {cart_id}")
        await flush_messages()
        agent.persist_state()
        await send_list_data(ws, agent)
        # Toast instead of forced tab switch — user stays on suggestions
        await ws.send_json({
            "type": "toast",
            "text": f"Added {item_label} to cart ✓",
            "action": "view_cart",
        })
    except Exception as e:
        logger.error(f"cherry_pick failed: {e}", exc_info=True)
        message_buffer.append("Something went wrong adding that item to your cart.")
        await flush_messages()
    return


async def handle_request_feedback(ws, agent, data, message_buffer, flush_messages):
    # Start a WhatsApp feedback round
    list_id = (getattr(agent.session, "suggestion_list_id", None)
               or getattr(agent.session, "cart_list_id", None)
               or agent.session.list_id)
    if list_id:
        round_id = await feedback_manager.initiate_feedback(list_id)
        if round_id:
            message_buffer.append("Feedback requested — messages sent to Sara and Max.")
        else:
            message_buffer.append("Could not send feedback request. Check WhatsApp connection.")
        await flush_messages()
        await _send_feedback_status(agent)
    return


async def handle_approve_feedback(ws, agent, data, message_buffer, flush_messages):
    feedback_item_id = data.get("feedbackItemId")
    list_id = (getattr(agent.session, "suggestion_list_id", None)
               or getattr(agent.session, "cart_list_id", None)
               or agent.session.list_id)
    if feedback_item_id and list_id:
        result = feedback_manager.approve_feedback_item(feedback_item_id, list_id)
        if result:
            log_event("feedback_item_approved", source="action",
                      detail=f"fi={feedback_item_id}")
        await send_list_data(ws, agent)
        await _send_feedback_status(agent)
    return


async def handle_reject_feedback(ws, agent, data, message_buffer, flush_messages):
    feedback_item_id = data.get("feedbackItemId")
    if feedback_item_id:
        feedback_manager.reject_feedback_item(feedback_item_id)
        await _send_feedback_status(agent)
    return


async def handle_cancel_feedback(ws, agent, data, message_buffer, flush_messages):
    round_id = data.get("roundId")
    if round_id:
        feedback_manager.cancel_round(round_id)
        await _send_feedback_status(agent)
    return


async def handle_order_action(ws, agent, data, message_buffer, flush_messages):
    command = data.get("command", "")
    view = data.get("view", "suggestions")  # which tab the user is on
    # Map frontend buttons to agent text commands
    command_map = {
        "ready": "ok",
        "skip": "cancel",
        "delay": "delay",
        "reset": "reset",
    }
    user_text = command_map.get(command, command)

    # ── Route to the correct list based on active tab ──
    # "Prepare Scheduled" / "Reset" on suggestions → use suggestion list
    # "Prepare Curated"   / "Reset" on cart        → use cart list
    original_list_id = agent.session.list_id
    if command in ("ready", "reset") and agent.session:
        if view == "suggestions" and agent.session.suggestion_list_id:
            agent.session.list_id = agent.session.suggestion_list_id
            logger.info(f"order_action ({command}): routed to suggestion list {agent.session.suggestion_list_id}")
        elif view == "cart" and agent.session.cart_list_id:
            agent.session.list_id = agent.session.cart_list_id
            logger.info(f"order_action ({command}): routed to cart list {agent.session.cart_list_id}")

    # Echo the action as a user message and persist
    labels = {"ready": "Prepare order", "skip": "Skip this order",
              "delay": "Delay", "reset": "Reset to original"}
    label = labels.get(command, command)
    await ws.send_json({
        "type": "user",
        "text": label,
        "time": datetime.now().strftime("%H:%M"),
    })
    _save_chat_message("user", label,
                       list_id=agent.session.list_id if agent.session else None)

    if command == "ready":
        # Direct path: approve all items and force BUILDING state
        # (bypasses agent state machine which may be in wrong state for this list)
        agent._approve_all_items()
        agent.say("Building carts...")
        agent.session.state = State.BUILDING
        logger.info(f"order_action: direct BUILDING for list_id={agent.session.list_id}")
    else:
        # Other commands (reset, skip, delay) go through agent brain
        agent.process_input(user_text)

    await flush_messages()
    await send_list_data(ws, agent)

    # Handle cart building if triggered
    if agent.session.state == State.BUILDING:
        app_state.cart_build_active = True
        log_event("cart_build_start", source="app", detail=f"list_id={agent.session.list_id}")
        await ws.send_json({"type": "state_change", "state": "building"})

        # Progress callback — sends real-time step updates to frontend
        async def _cart_progress(step, **kwargs):
            try:
                await ws.send_json({"type": "cart_progress", "step": step, **kwargs})
            except Exception as e:
                logger.debug("Cart progress send failed (client likely disconnected): %s", e)

        cart_success = False
        try:
            await agent.build_carts(progress_cb=_cart_progress)
        except Exception as e:
            logger.error(f"Cart building error: {e}", exc_info=True)
            log_event("cart_error", source="cart_builder", detail=str(e)[:200], level="error")
            err_str = str(e).lower()
            if "login" in err_str or "not logged" in err_str or "auth" in err_str:
                message_buffer.append(
                    "Not logged into store websites.\n"
                    "Close your regular Edge browser, tap Ready Cart again, "
                    "then log into the stores in the Edge window that opens."
                )
            elif "timeout" in err_str or "network" in err_str:
                message_buffer.append("Cart building timed out. The store website may be slow. Try again in a few minutes.")
            elif "browser" in err_str or "playwright" in err_str or "executable" in err_str:
                message_buffer.append("Could not open the browser. Make sure Edge is closed and try again.")
            else:
                message_buffer.append(f"Cart building hit an issue: {str(e)[:100]}. Check the logs for details.")

        app_state.cart_build_active = False  # Release browser lock

        # Determine actual success from the cart report, not just exception absence
        report = agent.session.cart_report
        if report and "error" not in report:
            total_added = sum(
                sr.get("items_added", 0) for sr in report.get("stores", {}).values()
            )
            cart_success = total_added > 0
            log_event("cart_build_end", source="app",
                      detail=f"added={total_added}, success={cart_success}")
        else:
            log_event("cart_build_end", source="app",
                      detail=f"failed: {report.get('error', 'unknown') if report else 'no report'}")

        # Archive the built list on success (two-entity model)
        built_list_id = agent.session.list_id  # whichever list was built
        if cart_success and built_list_id:
            try:
                with get_db() as conn:
                    conn.execute("""
                        UPDATE shopping_lists
                        SET status = 'done', updated_at = datetime('now')
                        WHERE id = ?
                    """, (built_list_id,))
                log_event("list_archived", source="app",
                          detail=f"list_id={built_list_id}, view={view}")
                # Clear the archived list from session
                if built_list_id == agent.session.cart_list_id:
                    agent.session.cart_list_id = None
                    # Point to suggestions after cart is archived
                    if agent.session.suggestion_list_id:
                        agent.session.list_id = agent.session.suggestion_list_id
                elif built_list_id == agent.session.suggestion_list_id:
                    agent.session.suggestion_list_id = None
                    # Point to cart if it exists, otherwise stay
                    if agent.session.cart_list_id:
                        agent.session.list_id = agent.session.cart_list_id
                agent.session.state = State.DONE
            except Exception as e:
                logger.error(f"List archive failed: {e}", exc_info=True)

        # Capture any error messages before flushing
        error_detail = " ".join(message_buffer) if message_buffer else ""
        await flush_messages()

        # Build a detailed completion message
        if cart_success:
            summary = report.get("summary", "Carts ready!")
            cart_msg = f"Your carts are ready! {summary}\nHead to the store websites to place your orders."
        elif report and report.get("error") == "availability_check":
            # Pre-build availability gate -- the detailed item list +
            # alternatives was already sent via the agent's messages above.
            cart_msg = report.get("summary") or "A few items need attention before I build the cart — swap or skip them above, then build again."
        else:
            # Include the actual error in the banner so the user doesn't have to dig
            if error_detail:
                cart_msg = error_detail[:200]
            elif report and report.get("error"):
                # Sanitize — only show first line, no browser details
                clean_err = str(report['error']).split("\n")[0][:120]
                if any(kw in clean_err.lower() for kw in ["playwright", "browser", "launch", "chromium"]):
                    cart_msg = "Could not open the browser. Make sure Edge is closed and try again."
                else:
                    cart_msg = f"Cart building failed: {clean_err}"
            else:
                cart_msg = "Cart building failed. No items were added to your carts."

        await ws.send_json({
            "type": "cart_complete",
            "success": cart_success,
            "message": cart_msg,
        })
        agent.persist_state()
        await ws.send_json({"type": "state_change", "state": "idle"})
        await send_list_data(ws, agent)
        # After successful build, switch to the remaining tab
        if cart_success:
            # If we built the cart, switch to suggestions (and vice versa)
            switch_to = "suggestions" if view == "cart" else "cart" if agent.session.cart_list_id else "suggestions"
            await ws.send_json({"type": "view_switch", "view": switch_to})

    if agent.session.state == State.DONE:
        await ws.send_json({"type": "state_change", "state": "done"})

    if agent.session.state == State.IDLE:
        await ws.send_json({"type": "state_change", "state": "idle"})


def _list_active_preferences(conn):
    """A13 — return ALL active user_item_prefs grouped by pref_type.

    Result shape:
        {
          "block_brand":     [{id, item_id, item_name, value, created_at}, ...],
          "pin_product":     [...],
          "prefer_store":    [...],
          "wrong_item":      [...],
          "stop_suggesting": [..., each row also tagged 'source':
                               'auto' | 'user' | 'manual'],
          "tier2_asked":     [...],  # informational
        }

    pref_types not in the canonical list still appear in the result
    under their own key (we don't filter), so a future pref_type
    introduced by another module shows up automatically.

    Used by the WebSocket get_preferences action. The Settings sheet
    renders each group with an inline Deactivate button.
    """
    grouped = {}
    try:
        rows = conn.execute(
            """SELECT uip.id, uip.item_id, uip.pref_type, uip.value,
                      uip.is_active, uip.created_at,
                      i.name AS item_name, i.category
               FROM user_item_prefs uip
               LEFT JOIN items i ON i.id = uip.item_id
               WHERE uip.is_active = 1
               ORDER BY uip.pref_type, uip.created_at DESC""",
        ).fetchall()
    except Exception:
        rows = []
    for r in rows:
        pref_type = r["pref_type"] or "unknown"
        entry = {
            "id": r["id"],
            "item_id": r["item_id"],
            "item_name": r["item_name"] or (f"item#{r['item_id']}"
                                            if r["item_id"] else "(no item)"),
            "category": r["category"],
            "value": r["value"],
            "created_at": r["created_at"],
        }
        if pref_type == "stop_suggesting":
            v = r["value"] or ""
            if v.startswith("auto:"):
                entry["source"] = "auto"
            elif v.startswith("user:"):
                entry["source"] = "user"
            else:
                entry["source"] = "manual"
        grouped.setdefault(pref_type, []).append(entry)
    # Always include the canonical pref types in the result so the
    # frontend can render empty sections instead of guessing.
    for canonical in ("block_brand", "pin_product", "prefer_store",
                      "wrong_item", "stop_suggesting", "tier2_asked"):
        grouped.setdefault(canonical, [])
    return grouped


def _deactivate_preference(conn, pref_id: int) -> dict:
    """Set is_active=0 for a single user_item_prefs row.

    Returns {ok, was_stop_suggesting, item_id} so the caller knows
    whether to also push updated learning state (a deactivated
    stop_suggesting changes the ladder visibility) and refresh the
    suggestion list (a lifted stop may grow it).
    """
    try:
        row = conn.execute(
            "SELECT item_id, pref_type FROM user_item_prefs WHERE id = ?",
            (pref_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "was_stop_suggesting": False, "item_id": None}
        cur = conn.execute(
            "UPDATE user_item_prefs SET is_active = 0 WHERE id = ?",
            (pref_id,),
        )
        conn.commit()
        return {
            "ok": cur.rowcount > 0,
            "was_stop_suggesting": row["pref_type"] == "stop_suggesting",
            "item_id": row["item_id"],
        }
    except Exception:
        return {"ok": False, "was_stop_suggesting": False, "item_id": None}


async def handle_get_preferences(ws, agent, data, message_buffer, flush_messages):
    """A13 — push the full active-preferences snapshot to the client."""
    try:
        with app_state.get_db() as conn:
            prefs = _list_active_preferences(conn)
    except Exception as e:
        logger.warning("get_preferences failed: %s", e)
        prefs = {}
    await ws.send_json({"type": "preferences", **prefs})


async def handle_deactivate_preference(ws, agent, data, message_buffer, flush_messages):
    """A13 — deactivate a single user_item_prefs row.

    Body: {action: "deactivate_preference", prefId: int}

    After the write we re-push the preferences snapshot. If the row
    was a stop_suggesting, also re-push learning_items (the panel
    updates) and the suggestion list (the unblocked item may appear).
    """
    pref_id = data.get("prefId")
    if not pref_id:
        return
    try:
        with app_state.get_db() as conn:
            result = _deactivate_preference(conn, int(pref_id))
            prefs = _list_active_preferences(conn)
        log_event(
            "pref_deactivated",
            source="action",
            detail=(
                f"pref_id={pref_id} "
                f"was_stop={result['was_stop_suggesting']} "
                f"item_id={result['item_id']}"
            ),
        )
        await ws.send_json({"type": "preferences", **prefs})
        if result["was_stop_suggesting"]:
            from pattern_detector import get_active_learning_state
            with app_state.get_db() as conn:
                learning = get_active_learning_state(conn)
            await ws.send_json({"type": "learning_items", **learning})
            await send_list_data(ws, agent)
    except Exception as e:
        logger.exception("deactivate_preference failed: %s", e)


async def handle_get_forgotten_items(ws, agent, data, message_buffer, flush_messages):
    """A2 — push items the household normally buys but aren't on the
    active list yet, sorted by how-overdue.

    Surface for "did we forget X?" in the in-app Scheduled tab.
    Frontend renders these as tap-to-add chips below the main list.

    Returns up to 8 items by default; the action body can override
    via `limit`.
    """
    limit = int(data.get("limit") or 8)
    if limit < 1 or limit > 50:
        limit = 8
    try:
        from list_generate import get_forgotten_items
        list_id = (
            getattr(agent.session, "suggestion_list_id", None)
            or getattr(agent.session, "cart_list_id", None)
            or agent.session.list_id
        )
        with app_state.get_db(row_factory=True) as conn:
            items = get_forgotten_items(
                list_id=list_id, limit=limit, conn=conn,
            )
    except Exception as e:
        logger.warning("get_forgotten_items failed: %s", e)
        items = []
    await ws.send_json({
        "type": "forgotten_items",
        "items": items,
        "count": len(items),
    })


async def handle_get_learning_items(ws, agent, data, message_buffer, flush_messages):
    """A12 — push the current ladder state to the client.

    Snapshot of restock_rules.cadence_adjustment > 1.0 (silent nudges
    the pattern detector is holding) + active user_item_prefs(stop_suggesting)
    rows (items the agent has stopped suggesting). The frontend renders
    each as a row in the "Recently learned" panel with a one-tap reset.
    """
    try:
        from pattern_detector import get_active_learning_state
        with app_state.get_db() as conn:
            state = get_active_learning_state(conn)
    except Exception as e:
        logger.warning("get_learning_items failed: %s", e)
        state = {"cadence_adjustments": [], "stops": []}
    await ws.send_json({"type": "learning_items", **state})


async def handle_reset_preference(ws, agent, data, message_buffer, flush_messages):
    """A12 — user-driven reset of the ladder for one item.

    Body shape from the frontend:
        {action: "reset_preference", itemId: int, kind: "cadence"|"stop"|"all"}

    Deactivates BOTH auto and user-confirmed stop_suggesting rows when
    kind includes 'stop' -- this is an explicit user undo, so the
    user-confirmed-stop stickiness from E4/E6 is intentionally
    overridden. After the reset we re-push learning_items + send_list_data
    so the panel updates AND any newly-unblocked items appear on the
    suggestion list if they're due.
    """
    item_id = data.get("itemId")
    kind = data.get("kind", "all")
    if kind not in ("cadence", "stop", "blocks", "all"):
        kind = "all"
    if not item_id:
        return
    try:
        from pattern_detector import reset_item_ladder, get_active_learning_state
        with app_state.get_db() as conn:
            result = reset_item_ladder(conn, int(item_id), kind=kind)
            state = get_active_learning_state(conn)
        log_event(
            "ladder_reset",
            source="action",
            detail=(
                f"item_id={item_id} kind={kind} "
                f"cadence_reset={result['cadence_reset']} "
                f"stops_deactivated={result['stops_deactivated']} "
                f"blocks_cleared={result.get('blocks_cleared', 0)}"
            ),
        )
        await ws.send_json({"type": "learning_items", **state})
        # If a stop was lifted OR a product/brand block was cleared, the
        # suggestion list may change next generation -- push fresh list data
        # so the UI catches up.
        if result["stops_deactivated"] > 0 or result.get("blocks_cleared", 0) > 0:
            await send_list_data(ws, agent)
    except Exception as e:
        logger.exception("reset_preference failed: %s", e)


ACTION_HANDLERS = {
    "change_qty": handle_change_qty,
    "remove_item": handle_remove_item,
    "mark_have_it": handle_mark_have_it,
    "item_pref": handle_item_pref,
    "find_alternatives": handle_find_alternatives,
    "accept_alternative": handle_accept_alternative,
    "reorder_from_history": handle_reorder_from_history,
    "add_suggestion": handle_add_suggestion,
    "next_suggestion": handle_next_suggestion,
    "skip_suggestion": handle_skip_suggestion,
    "get_order_tracking": handle_get_order_tracking,
    "submit_rating": handle_submit_rating,
    "dismiss_rating": handle_dismiss_rating,
    "get_llm_feedback": handle_get_llm_feedback,
    "feedback_alert": handle_feedback_alert,
    "create_cart": handle_create_cart,
    "dismiss_cart": handle_dismiss_cart,
    "cherry_pick": handle_cherry_pick,
    "request_feedback": handle_request_feedback,
    "approve_feedback": handle_approve_feedback,
    "reject_feedback": handle_reject_feedback,
    "cancel_feedback": handle_cancel_feedback,
    "order_action": handle_order_action,
    "get_learning_items": handle_get_learning_items,
    "get_forgotten_items": handle_get_forgotten_items,
    "reset_preference": handle_reset_preference,
    "get_preferences": handle_get_preferences,
    "deactivate_preference": handle_deactivate_preference,
}


async def _handle_action(ws, agent, data, message_buffer, flush_messages):
    """Dispatch a structured frontend action to its handler."""
    action = data.get("action")
    handler = ACTION_HANDLERS.get(action)
    if handler:
        await handler(ws, agent, data, message_buffer, flush_messages)
    else:
        logger.debug("Unknown action: %s", action)


# ============================================================
# CATCH-ALL ROUTE (must be after /ws and other routes)
# ============================================================

@app.get("/{path:path}", response_class=HTMLResponse)
async def catch_all(path: str):
    """Serve index.html for any unknown path (client-side routing)."""
    if dev_mode:
        return HTMLResponse("Dev mode. Use Vite.", status_code=404)
    html_path = FRONTEND_DIST / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("Not found", status_code=404)


# ============================================================
# FEEDBACK & LLM LOGGING
# ============================================================

def _smart_fallback(user_text, agent):
    """Generate a helpful response when LLM is unavailable."""
    text_lower = user_text.lower().strip()
    state = agent.session.state

    # Positive/acknowledgment messages
    if any(w in text_lower for w in ["looks good", "this is ok", "looks ok", "looks fine",
                                      "perfect", "great", "nice", "cool", "awesome"]):
        if state in ("checkin", "reviewing"):
            return "Got it! If you're happy with the list, say 'ok' to approve and I'll start building carts. Or tell me any changes first."
        return "Thanks!"

    # Questions about the list
    if any(w in text_lower for w in ["why", "how come", "what", "which", "where", "when"]):
        return ("Good question! I'd love to explain, but my AI brain needs API credits to have "
                "proper conversations. For now, I can handle commands like:\n"
                "  - 'add olive oil'\n"
                "  - 'skip salmon'\n"
                "  - '3 packs of eggs'\n"
                "  - 'ok' to approve the list")

    # Greetings
    if any(w in text_lower for w in ["hello", "hi", "hey", "good morning", "good evening"]):
        return f"Hey! Your order is in '{state}' mode. Say 'ok' to approve or make changes to the list."

    # Generic fallback
    return ("I got your message but I'm running in basic mode right now (no AI credits). "
            "I can handle:\n"
            "  - 'add [item]' to add something\n"
            "  - 'skip [item]' to remove\n"
            "  - 'ok' to approve the list\n"
            "  - '2 packs of [item]' to change quantity")


def _get_list_item_name(list_item_id):
    """Get the display name for a list item."""
    try:
        with get_db(row_factory=True) as conn:
            row = conn.execute(
                "SELECT item_name_text FROM list_items WHERE id = ?",
                (list_item_id,)
            ).fetchone()
        return row["item_name_text"] if row else "item"
    except Exception:
        return "item"


def _find_replacement_product(item_id, item_name, store_id=None,
                               exclude_brand=None, exclude_catalog_ids=None,
                               search_hint=None):
    """
    Find the best replacement product from the catalog.

    Uses search_catalog + score_products, filtering out the excluded brand
    or specific catalog IDs. Returns a product dict or None.
    """
    from product_knowledge import search_catalog
    from product_score import score_products

    # Use the hint text (from wrong_item feedback) or the item name
    query = search_hint or item_name
    if not query:
        return None

    results = search_catalog(query, store_id=store_id, limit=20)
    if not results:
        # Fallback: try with just the item name if hint didn't match
        if search_hint and item_name:
            results = search_catalog(item_name, store_id=store_id, limit=20)
        if not results:
            return None

    # Filter out excluded brand
    if exclude_brand:
        brand_lower = exclude_brand.lower()
        results = [r for r in results
                   if brand_lower not in (r.get("brand") or "").lower()]

    # Filter out excluded catalog IDs
    if exclude_catalog_ids:
        exclude_set = set(exclude_catalog_ids)
        results = [r for r in results
                   if r.get("catalog_id") not in exclude_set]

    if not results:
        return None

    # Score the remaining products (preference-aware scoring)
    # Convert catalog results to the format score_products expects
    store_name = None
    if store_id:
        try:
            with get_db() as conn:
                row = conn.execute("SELECT name FROM stores WHERE id = ?", (store_id,)).fetchone()
            store_name = row[0] if row else "Store"
        except Exception:
            store_name = "Store"

    with get_db(row_factory=True) as conn:
        scored = score_products(
            item_name or query, store_name or "Store",
            [{"id": r.get("catalog_id"), "name": r.get("product_name"),
              "brand": r.get("brand"), "price": r.get("price"),
              "size": r.get("size")} for r in results],
            conn=conn,
            item_id=item_id,
        )

    # Pick the best non-disqualified product
    for s in scored:
        if not s.get("disqualified"):
            best_product = s["product"]
            # Map back to catalog result for full data
            for r in results:
                if r.get("catalog_id") == best_product.get("id"):
                    return r
            # Fallback: return scored product info
            return {
                "catalog_id": best_product.get("id"),
                "product_name": best_product.get("name"),
                "brand": best_product.get("brand"),
                "price": best_product.get("price"),
                "store_id": store_id,
            }

    # All disqualified — just return the first catalog result
    return results[0] if results else None


def _save_item_pref(item_id, pref_type, value):
    """Save a user item preference (block brand, pin product, etc.)."""
    try:
        with get_db() as conn:
            if pref_type == "pin_product":
                # Un-pin any existing pin for this item first
                conn.execute(
                    "UPDATE user_item_prefs SET is_active = 0 WHERE item_id = ? AND pref_type = 'pin_product'",
                    (item_id,))
            conn.execute(
                "INSERT INTO user_item_prefs (item_id, pref_type, value) VALUES (?, ?, ?)",
                (item_id, pref_type, str(value) if value is not None else None))
        logger.info(f"Item pref saved: item={item_id} type={pref_type} value={value}")
    except Exception as e:
        logger.error(f"Could not save item pref: {e}")


def _get_list_item_info(list_item_id):
    """Get (quantity, item_id, added_by) for a list_item. Returns (None, None, None) if not found."""
    try:
        with get_db(row_factory=True) as conn:
            row = conn.execute(
                "SELECT quantity, item_id, added_by FROM list_items WHERE id = ?",
                (list_item_id,)
            ).fetchone()
        if row:
            return row["quantity"], row["item_id"], row["added_by"]
    except Exception as e:
        logger.warning("Could not read list_item info: %s", e)
    return None, None, None


def _mark_user_edits(list_item_id):
    """Mark the parent shopping_list as having user edits (has_user_edits=1)."""
    try:
        with get_db() as conn:
            conn.execute("""
                UPDATE shopping_lists SET has_user_edits = 1, updated_at = datetime('now')
                WHERE id = (SELECT list_id FROM list_items WHERE id = ?)
            """, (list_item_id,))
    except Exception as e:
        logger.warning("Could not mark list as user-edited: %s", e)


def _emit_pref_signal(item_id, signal_type, dimension, old_value=None,
                      new_value=None, context=None):
    """Emit a preference signal from a UI action. Fails silently."""
    try:
        from preference_engine import emit_signal
        emit_signal(
            item_id=item_id,
            signal_type=signal_type,
            signal_source="app_ui",
            dimension=dimension,
            old_value=old_value,
            new_value=new_value,
            context=context,
        )
    except Exception as e:
        logger.warning(f"Could not emit preference signal: {e}")


def _save_rating(order_id, item_ratings, feedback_text, llm_response):
    """
    Save a finalized delivery rating to the database.

    Args:
        order_id: The order being rated
        item_ratings: [{orderItemId, feedback}] where feedback is 'thumbs_up'/'thumbs_down'/null
        feedback_text: User's free text improvement suggestions
        llm_response: LLM's conversational reply
    """
    with get_db() as conn:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Create or update the rating record
            conn.execute("""
                INSERT INTO order_ratings (order_id, overall_text, llm_response, status, finalized_at)
                VALUES (?, ?, ?, 'finalized', ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    overall_text = excluded.overall_text,
                    llm_response = excluded.llm_response,
                    status = 'finalized',
                    finalized_at = excluded.finalized_at
            """, (order_id, feedback_text, llm_response, now))

            # Get the rating_id
            rating_id = conn.execute(
                "SELECT id FROM order_ratings WHERE order_id = ?", (order_id,)
            ).fetchone()[0]

            # Save per-item feedback
            for item in item_ratings:
                order_item_id = item.get("orderItemId")
                feedback = item.get("feedback")  # thumbs_up, thumbs_down, or null
                if order_item_id and feedback:
                    # Get item_id from order_items for preference linking
                    row = conn.execute(
                        "SELECT item_id FROM order_items WHERE id = ?",
                        (order_item_id,)
                    ).fetchone()
                    item_id = row[0] if row else None

                    conn.execute("""
                        INSERT INTO order_item_ratings (rating_id, order_item_id, item_id, feedback)
                        VALUES (?, ?, ?, ?)
                    """, (rating_id, order_item_id, item_id, feedback))

            # Mark order as rated
            conn.execute(
                "UPDATE orders SET rating_status = 'finalized' WHERE id = ?",
                (order_id,)
            )
            logger.info(f"Rating saved for order {order_id}: {len(item_ratings)} items")
        except Exception as e:
            logger.error(f"_save_rating error: {e}", exc_info=True)
            raise


def _dismiss_rating(order_id):
    """Mark an order's rating as dismissed (user chose not to rate)."""
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE orders SET rating_status = 'dismissed' WHERE id = ?",
                (order_id,)
            )
        logger.info(f"Rating dismissed for order {order_id}")
    except Exception as e:
        logger.error(f"_dismiss_rating error: {e}")


def _emit_rating_signals(order_id, item_ratings, feedback_text):
    """
    Convert per-item rating feedback into preference signals.

    Thumbs up emits 3 signals:
      1. purchase_observed / brand   — boost the brand in preferred list
      2. purchase_observed / product — track product approval in history
      3. item_approve / item_inclusion — confirm item is wanted, reset skip streak

    Thumbs down emits 2 signals:
      1. wrong_item / product — block this specific catalog product
      2. wrong_item / brand   — add brand to avoid list (if known)

    Brand is enriched from catalog if order_items.brand is empty.
    """

    with get_db(row_factory=True) as conn:
        try:
            for item in item_ratings:
                order_item_id = item.get("orderItemId")
                feedback = item.get("feedback")
                if not order_item_id or not feedback:
                    continue

                # Look up item_id, raw_name, brand from order_items
                row = conn.execute(
                    "SELECT item_id, raw_name, brand FROM order_items WHERE id = ?",
                    (order_item_id,)
                ).fetchone()
                if not row or not row["item_id"]:
                    continue

                item_id = row["item_id"]
                raw_name = row["raw_name"] or ""
                brand = row["brand"] or ""

                # Enrich from catalog: get catalog_product_id and brand fallback
                catalog_id = None
                try:
                    cat_row = conn.execute("""
                        SELECT pc.id, pc.brand, pc.product_name
                        FROM item_alternatives ia
                        JOIN product_catalog pc ON ia.catalog_id = pc.id
                        WHERE ia.item_id = ?
                        ORDER BY ia.score DESC LIMIT 1
                    """, (item_id,)).fetchone()
                    if cat_row:
                        catalog_id = cat_row["id"]
                        if not brand:
                            brand = cat_row["brand"] or ""
                except Exception as e:
                    logger.debug("Catalog enrichment failed (best-effort): %s", e)

                ctx = {"source": "delivery_rating", "order_id": order_id}

                if feedback == "thumbs_up":
                    # Signal 1: Brand boost (only if brand is known)
                    if brand:
                        _emit_pref_signal(
                            item_id, "purchase_observed", "brand",
                            new_value={"brand": brand},
                            context=ctx,
                        )

                    # Signal 2: Product approval — tracks which products are good picks
                    _emit_pref_signal(
                        item_id, "purchase_observed", "product",
                        new_value={"product": raw_name, "catalog_id": catalog_id},
                        context=ctx,
                    )

                    # Signal 3: Item inclusion — confirms this item belongs on the list
                    _emit_pref_signal(
                        item_id, "item_approve", "item_inclusion",
                        new_value={"action": "approve"},
                        context=ctx,
                    )

                elif feedback == "thumbs_down":
                    # Signal 1: Block this specific product
                    _emit_pref_signal(
                        item_id, "wrong_item", "product",
                        new_value={"raw_name": raw_name, "brand": brand,
                                   "catalog_id": catalog_id},
                        context=ctx,
                    )

                    # Signal 2: Brand penalization (only if brand is known)
                    if brand:
                        _emit_pref_signal(
                            item_id, "wrong_item", "brand",
                            new_value={"brand": brand, "action": "penalize"},
                            context=ctx,
                        )

            # Process all non-immediate signals
            try:
                from preference_engine import process_pending_signals
                count = process_pending_signals()
                logger.info(f"Processed {count} rating signals for order {order_id}")
            except Exception as e:
                logger.warning(f"Could not process rating signals: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"_emit_rating_signals error: {e}", exc_info=True)


def _log_feedback(msg_id, vote, agent, detail=None):
    """Log user feedback (thumbs up/down/alert) to agent_log and llm_log."""
    try:
        with get_db() as conn:
            # Log to agent_log
            recent = agent.session.messages_sent[-3:] if agent.session.messages_sent else []
            recent_text = [m.get("text", "")[:200] for m in recent]
            feedback_data = {
                "msg_id": msg_id,
                "vote": vote,
                "recent_context": recent_text,
                "state": agent.session.state,
            }
            if detail:
                feedback_data["detail"] = detail
            details = json.dumps(feedback_data)
            conn.execute("""
                INSERT INTO agent_log (action_type, details, outcome)
                VALUES ('user_feedback', ?, ?)
            """, (details, vote))

            # Also link feedback to most recent LLM log entry
            feedback_value = f"{vote}: {detail}" if detail else vote
            conn.execute("""
                UPDATE llm_log
                SET user_feedback = ?, feedback_at = datetime('now')
                WHERE id = (SELECT id FROM llm_log ORDER BY id DESC LIMIT 1)
                  AND user_feedback IS NULL
            """, (feedback_value,))

        print(f"  Feedback logged: {vote}" + (f" - {detail[:50]}" if detail else ""))
    except Exception as e:
        print(f"  Warning: could not log feedback: {e}")


def _log_llm_interaction(user_text, llm_result, agent):
    """Log an LLM interaction to the llm_log table."""
    try:
        with get_db() as conn:
            actions_json = json.dumps([{
                "intent": a.intent,
                "item_name": a.item_name,
                "quantity": a.quantity,
                "confidence": a.confidence,
            } for a in llm_result.parsed.actions])

            conn.execute("""
                INSERT INTO llm_log
                (user_text, agent_state, model, parsed_intent, parsed_actions,
                 response_text, prompt_tokens, completion_tokens, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_text,
                agent.session.state,
                llm_result.model_used,
                llm_result.parsed.primary_intent,
                actions_json,
                llm_result.response_text,
                llm_result.prompt_tokens,
                llm_result.completion_tokens,
                llm_result.latency_ms,
            ))
    except Exception as e:
        print(f"  Warning: could not log LLM interaction: {e}")



# ============================================================
# HELPERS
# ============================================================

async def _flush_nudge_queue(ws):
    """Deliver any queued nudges that were sent while user was offline."""
    try:
        with get_db(row_factory=True) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, text, priority, created_at FROM nudge_queue
                WHERE delivered_at IS NULL
                ORDER BY created_at
            """)
            nudges = cur.fetchall()
            for nudge_row in nudges:
                await ws.send_json({
                    "type": "agent",
                    "text": nudge_row["text"],
                    "time": datetime.now().strftime("%H:%M"),
                    "nudge": True,
                    "priority": nudge_row["priority"],
                })
                conn.execute(
                    "UPDATE nudge_queue SET delivered_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), nudge_row["id"])
                )
                await asyncio.sleep(0.2)
    except Exception as e:
        print(f"  Nudge flush: {e}")


def _get_local_ip():
    """Get this machine's local IP on the network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


def _build_order_tracking():
    """
    Build the order tracking payload for the frontend.
    Returns {activeOrder, deliveredOrder, deliveredOrders} — deliveredOrders is the full array.
    """
    from order_scraper import get_active_order_for_frontend, get_delivered_unrated_order, get_all_delivered_unrated_orders
    try:
        active = get_active_order_for_frontend()
        delivered = get_delivered_unrated_order()
        all_delivered = get_all_delivered_unrated_orders()
        return {
            "activeOrder": active,
            "deliveredOrder": delivered,
            "deliveredOrders": all_delivered,
        }
    except Exception as e:
        logger.error(f"_build_order_tracking failed: {e}")
        return {"activeOrder": None, "deliveredOrder": None, "deliveredOrders": []}



# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    import uvicorn

    create_database()

    port = _args.port
    local_ip = _get_local_ip()
    mode = "DRY RUN" if dry_run_mode else ("DEV" if dev_mode else "PRODUCTION")

    logger.info(f"Starting Hearthkeeper — mode={mode}, port={port}")
    log_event("server_start", source="app", detail=f"mode={mode}, port={port}")

    print(f"\n  Hearthkeeper")
    print(f"  {'='*45}")
    if dev_mode:
        print(f"  Mode:     DEV (frontend via Vite on :5173)")
        print(f"  Backend:  http://localhost:{port}")
    else:
        print(f"  Mode:     {mode}")
        print(f"  Local:    http://localhost:{port}")
        print(f"  Network:  http://{local_ip}:{port}")
    if target_date_override:
        print(f"  Date:     {target_date_override}")
    print(f"  Logs:     logs/hearthkeeper.log")
    print(f"  Events:   http://localhost:{port}/api/logs")
    print(f"  {'='*45}\n")

    # ws_max_size caps WebSocket frame size (uvicorn's default is 16 MB).
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", ws_max_size=65536)
