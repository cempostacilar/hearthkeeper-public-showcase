"""
Hearthkeeper Watchdog — keeps the app alive.

Runs via Windows Task Scheduler every 5 minutes.
Checks if the app is healthy (HTTP 200 on /api/health).
If not, kills any stuck processes and restarts.

Logs everything to logs/watchdog.log.
"""

import subprocess
import sys
import time
import urllib.request
import urllib.error
import os
import json
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).parent
LOG_FILE = APP_DIR / "logs" / "watchdog.log"
APP_CONSOLE_LOG = APP_DIR / "logs" / "app_console.log"
HEALTH_URL = "http://localhost:8000/api/health"
APP_SCRIPT = APP_DIR / "app.py"
# Absolute interpreter path — the "python3" alias is a Windows Store shim that
# Task Scheduler and detached subprocesses cannot reliably resolve.
PYTHON = sys.executable

# Watchdog -> app system alert (a gated WhatsApp ping). Token = WEBHOOK_SECRET.
ALERT_URL = "http://localhost:8000/api/diag/watchdog-alert"
ALERT_STATE = APP_DIR / "logs" / "watchdog_alert_state.txt"
ALERT_COOLDOWN_S = 1800  # don't re-alert more than once per 30 min

# Ensure logs dir exists
LOG_FILE.parent.mkdir(exist_ok=True)


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def is_app_healthy():
    """Check if the app responds with HTTP 200."""
    try:
        req = urllib.request.Request(HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        pass
    return False


def is_port_in_use(port=8000):
    """Check if port 8000 is being listened on."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTEN" in line:
                return True
    except Exception:
        pass
    return False


def kill_stuck_app():
    """Kill any python process holding port 8000."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if ":8000" in line and "LISTEN" in line:
                parts = line.split()
                pid = parts[-1]
                log(f"  Killing stuck process PID {pid} on port 8000")
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True, timeout=10,
                )
                time.sleep(2)
    except Exception as e:
        log(f"  Error killing stuck process: {e}")


def start_app():
    """Start the app in production mode as a detached process."""
    log("  Starting Hearthkeeper in production mode...")
    # Use subprocess.Popen with CREATE_NEW_PROCESS_GROUP to fully detach
    with open(APP_CONSOLE_LOG, "w", encoding="utf-8") as console_log:
        proc = subprocess.Popen(
            [PYTHON, str(APP_SCRIPT)],
            cwd=str(APP_DIR),
            stdout=console_log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
    log(f"  Started with PID {proc.pid}")
    return proc.pid


def nssm_service_exists(name):
    """Return True if a Windows service with the given name is installed."""
    try:
        result = subprocess.run(
            ["sc", "query", name],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def nssm_restart(name):
    """Restart an NSSM-managed Windows service via `nssm restart`.

    Atomic stop+start handled by NSSM itself, which avoids the race
    window where this watchdog kills the stuck python.exe and NSSM also
    tries to bring up a replacement at the same time.
    """
    log(f"  nssm restart {name}...")
    try:
        result = subprocess.run(
            ["nssm", "restart", name],
            capture_output=True, text=True, timeout=60,
        )
        out = (result.stdout.strip() or result.stderr.strip())
        if out:
            log(f"  nssm: {out}")
    except Exception as e:
        log(f"  nssm restart failed: {e}")


def get_health():
    """Return the parsed /api/health JSON, or None if the app isn't responding."""
    try:
        req = urllib.request.Request(HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, ValueError):
        pass
    return None


def _webhook_secret():
    """Read WEBHOOK_SECRET from the Ally .env so we can authenticate the alert
    POST. The number itself never lives here — the app resolves it from the DB."""
    try:
        for line in (APP_DIR / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("WEBHOOK_SECRET") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def send_alert(reason):
    """Ping the household (test number) via the app's gated alert endpoint.
    Rate-limited so a sustained outage doesn't spam. Best-effort."""
    try:
        last = float(ALERT_STATE.read_text().strip()) if ALERT_STATE.exists() else 0.0
    except Exception:
        last = 0.0
    if time.time() - last < ALERT_COOLDOWN_S:
        log("  (alert suppressed — within cooldown)")
        return
    secret = _webhook_secret()
    if not secret:
        log("  (alert skipped — no WEBHOOK_SECRET in .env)")
        return
    try:
        data = json.dumps({"reason": reason}).encode("utf-8")
        req = urllib.request.Request(
            ALERT_URL, data=data, method="POST",
            headers={"Content-Type": "application/json", "X-Watchdog-Token": secret},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            log("  alert sent: " + resp.read().decode("utf-8")[:160])
        ALERT_STATE.write_text(str(time.time()))
    except Exception as e:
        log(f"  alert send failed: {e}")


def restart_app():
    """Restart via NSSM where available, else standalone kill + Popen."""
    service_name = os.environ.get("HEARTHKEEPER_SERVICE_NAME", "hearthkeeper-app")
    if nssm_service_exists(service_name):
        log(f"  Detected service '{service_name}' -- using nssm restart")
        nssm_restart(service_name)
    else:
        log("  No NSSM service detected -- using standalone kill+restart")
        if is_port_in_use():
            log("  Port 8000 bound but app unhealthy, killing stuck process...")
            kill_stuck_app()
            time.sleep(3)
        start_app()
    time.sleep(15)
    if is_app_healthy():
        log("  App restarted successfully")
    elif is_port_in_use():
        log("  App listening but health check still failing -- may still be starting")
    else:
        log("  FAILED to restart app. Manual intervention needed.")


def main():
    log("Watchdog check starting...")
    health = get_health()

    # (a) HTTP process down / unresponsive -> restart, then alert.
    if health is None:
        log("App HTTP not responding -- restarting")
        restart_app()
        send_alert("app stopped responding to /api/health — restarted it")
        return

    started = bool(health.get("scheduler_started"))
    sched_ok = bool(health.get("scheduler_ok"))
    age = health.get("scheduler_beat_age_s")
    uptime = int(health.get("uptime_s") or 0)

    # (b) Scheduler NEVER started after boot -> a code bug; a restart won't fix
    #     it and would just loop. Alert loudly instead.
    if not started and uptime > 180:
        log(f"Scheduler never started (uptime {uptime}s) -- code bug; alerting, NOT restarting")
        send_alert(f"background scheduler never started after boot (up {uptime}s) — needs a code fix; restart won't help")
        return

    # (c) Scheduler ran but went stale -> self-heal by restarting, then alert.
    if started and not sched_ok:
        log(f"Scheduler stale (last heartbeat {age}s ago) -- self-healing via restart")
        restart_app()
        send_alert(f"background scheduler stalled (no heartbeat for {age}s) — restarted to recover")
        return

    log("App + scheduler healthy. Nothing to do.")


if __name__ == "__main__":
    main()
