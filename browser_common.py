"""
AutoRestock - Shared Browser Utilities

Common browser launch, health check, recovery, and auto-login functions
used by scrapers and cart builders.

Uses Playwright's Chromium with stealth patches (playwright-stealth) and a
dedicated profile at ~/.hearthkeeper-browser/. This means:
  - Stealth mode avoids Akamai/Cloudflare bot detection
  - Separate profile directory — zero conflict with your normal Edge browsing
  - Sessions persist in the dedicated profile directory
  - Auto-login handles session expiry automatically
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# Dedicated HearthKeeper Chromium profile — completely separate from Edge.
# Login sessions for Carrefour/Kibsons persist here permanently.
# Your normal Edge is never touched.
HEARTHKEEPER_PROFILE = str(Path.home() / ".hearthkeeper-browser")


def _load_credentials():
    """Load store credentials from .env file."""
    env_path = Path(__file__).parent / ".env"
    creds = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                creds[key.strip()] = val.strip()
    return creds


def get_carrefour_proxy():
    """Return the Carrefour residential proxy as a Playwright proxy dict, or None.

    Carrefour's bot protection (Akamai) blocks datacenter IP ranges, so when
    Hearthkeeper runs on a cloud server its Carrefour traffic must egress
    through a residential proxy. The proxy is configured via CARREFOUR_PROXY
    in .env:

        CARREFOUR_PROXY=http://HOST:PORT            (IP-whitelisted — no creds)
        CARREFOUR_PROXY=http://USER:PASS@HOST:PORT  (username/password auth)

    On a home/residential machine the variable is left blank, this returns
    None, and all browser automation connects directly — exactly as before.

    Returns {"server": "http://host:port", "username"?: ..., "password"?: ...}
    (the shape Playwright's launch `proxy` option expects), or None if unset.
    """
    raw = _load_credentials().get("CARREFOUR_PROXY", "").strip()
    if not raw:
        return None
    # Allow a bare "host:port" by defaulting the scheme to http://
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        print("  [PROXY] WARNING: CARREFOUR_PROXY is set but malformed "
              "(expected host:port) — ignoring it and connecting directly.")
        return None
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def _cleanup_lockfiles():
    """
    Remove stale Chromium lockfiles that prevent browser launch.

    When a Chromium instance crashes or is killed (e.g., by task manager, power loss,
    or a previous cart build error), it leaves behind lockfiles like 'SingletonLock',
    'SingletonCookie', and 'SingletonSocket'. These prevent future launches with:
        "Failed to create new browser context"

    This function removes those lockfiles before launching, ensuring a clean start.
    It's safe because we only have ONE HearthKeeper browser at a time.
    """
    profile_path = Path(HEARTHKEEPER_PROFILE)
    if not profile_path.exists():
        return

    lockfile_patterns = ["SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"]
    for pattern in lockfile_patterns:
        lockfile = profile_path / pattern
        if lockfile.exists() or lockfile.is_symlink():
            try:
                lockfile.unlink(missing_ok=True)
                print(f"  [BROWSER] Removed stale lockfile: {pattern}")
            except Exception as e:
                print(f"  [BROWSER] Warning: Could not remove {pattern}: {e}")


def _kill_stale_chromium():
    """
    Kill any orphaned Chromium processes using the HearthKeeper profile.

    When the previous cart build crashed, Chromium may still be running in the
    background with the profile locked. We need to kill it before we can launch
    a new instance with the same profile directory.

    Only kills Chromium processes that are using OUR profile directory —
    never touches Edge or other Chrome instances.
    """
    profile_name = Path(HEARTHKEEPER_PROFILE).name  # ".hearthkeeper-browser"
    try:
        if sys.platform == "win32":
            # On Windows, use PowerShell + CIM to find chrome.exe processes
            # whose command line includes our profile directory name.
            import tempfile
            ps_script = f"""
Get-CimInstance Win32_Process -Filter "name='chrome.exe'" | ForEach-Object {{
    if ($_.CommandLine -like '*{profile_name}*') {{
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host $_.ProcessId
    }}
}}
"""
            with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False) as f:
                f.write(ps_script)
                ps_path = f.name

            try:
                result = subprocess.run(
                    ["powershell", "-ExecutionPolicy", "Bypass", "-File", ps_path],
                    capture_output=True, text=True, timeout=15
                )
                killed = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
                if killed:
                    print(f"  [BROWSER] Killed {len(killed)} stale HearthKeeper Chromium process(es)")
            finally:
                os.unlink(ps_path)
        else:
            # On Linux/Mac, use pkill with pattern matching
            subprocess.run(
                ["pkill", "-f", f"chrome.*{profile_name}"],
                capture_output=True, timeout=5
            )
    except Exception as e:
        # Non-fatal — if we can't kill stale processes, launch will fail
        # with a clear error and the user can close them manually
        print(f"  [BROWSER] Warning: Could not check for stale processes: {e}")


async def launch_browser(proxy=None):
    """
    Launch Playwright's bundled Chromium with the HearthKeeper profile.
    Returns (playwright, context, page).

    Uses a dedicated Chromium install (NOT Edge) with a persistent profile
    at ~/.hearthkeeper-browser/. Edge can be running simultaneously —
    there is zero conflict.

    Handles stale lockfiles and orphaned processes from previous crashes
    to ensure reliable launch.

    proxy: optional Playwright proxy dict (see get_carrefour_proxy()). When set,
        all of this browser's traffic egresses through that proxy — needed on a
        cloud server so Carrefour's Akamai bot check sees a residential IP.
        Left as None on a home machine, where traffic runs direct.
    """
    # Clean up before launch — prevents "Failed to create new browser context"
    _kill_stale_chromium()
    await asyncio.sleep(1)  # Give processes time to fully exit
    _cleanup_lockfiles()

    Path(HEARTHKEEPER_PROFILE).mkdir(parents=True, exist_ok=True)

    if proxy:
        print(f"  [BROWSER] Routing traffic through proxy: {proxy['server']}")

    async def _do_launch():
        p = await async_playwright().start()
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=HEARTHKEEPER_PROFILE,
                # No channel= → uses Playwright's bundled Chromium
                headless=False,
                # proxy is None on a residential machine (direct connection),
                # or a dict on a server where Carrefour must egress through a
                # residential IP to get past Akamai's datacenter-IP block.
                proxy=proxy,
                args=[
                    "--no-sandbox",  # required when running Chromium as root (server)
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=AutomationControlled",
                ],
                viewport={"width": 1280, "height": 900},
            )
        except Exception:
            # Always clean up Playwright on a failed launch; otherwise the
            # _self_heal path or the caller gets a dangling event loop.
            await p.stop()
            raise
        page = context.pages[0] if context.pages else await context.new_page()
        # Apply stealth patches to avoid bot detection (Akamai, Cloudflare, etc.)
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        return p, context, page

    try:
        return await _do_launch()
    except Exception as e:
        err_msg = str(e)
        # ── Self-heal: missing Playwright Chromium binary ──
        # On a fresh host (or when the pip-package version of playwright is
        # upgraded), the bundled Chromium isn't present until
        # `playwright install chromium` runs. The Ally hit exactly this on
        # 2026-05-25 and the order scraper started silently returning new=0.
        # Detect the specific error and self-install once; retry the launch.
        if _looks_like_missing_chromium(err_msg):
            healed = await _self_install_chromium()
            if healed:
                try:
                    return await _do_launch()
                except Exception as e2:
                    err_msg = str(e2)  # fall through to the raise below
                    e = e2
        if "browser" in err_msg.lower() or "launch" in err_msg.lower() \
                or "executable" in err_msg.lower():
            raise RuntimeError(
                f"Could not launch the HearthKeeper browser (Chromium). "
                f"If a previous session crashed, try closing all Chromium windows. "
                f"Original error: {err_msg[:200]}"
            ) from e
        raise


def _looks_like_missing_chromium(err_msg: str) -> bool:
    """Heuristic: is this a 'playwright install chromium' situation?

    Playwright raises BrowserType.launch_persistent_context: Executable
    doesn't exist at <path>. We match on the distinctive phrase plus a
    couple of common synonyms so a minor wording change doesn't bypass
    the self-heal.
    """
    m = (err_msg or "").lower()
    return (
        "executable doesn't exist" in m
        or "executable does not exist" in m
        or "browsertype.launch" in m and "executable" in m
    )


async def _self_install_chromium() -> bool:
    """Run `python -m playwright install chromium` and return True on success.

    Logs a structured event each side of the install so the run is visible
    in /api/diag/recent-errors (the user's primary debugging surface). This
    is a one-shot recovery -- callers should retry the launch ONCE and
    surface the original error if it still fails.
    """
    # Local import: app_logging pulls in the DB layer; we don't want every
    # browser launch to take the import cost when not recovering.
    try:
        from app_logging import log_event
    except Exception:  # pragma: no cover  -- if app_logging itself can't
        log_event = None  # be imported, fall back to print()
    if log_event:
        log_event(
            "playwright_self_heal_start",
            source="browser_common",
            detail="Chromium binary missing; running `playwright install chromium`",
            level="warning",
        )
    else:
        print("  [BROWSER] Self-heal: running `playwright install chromium`")

    def _run():
        # 600s timeout: the download is ~150 MB and can be slow on bad links.
        # capture_output for surfacing rc != 0; check=False so we can log the
        # failure rather than crashing the caller.
        return subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=600, check=False,
        )

    try:
        result = await asyncio.to_thread(_run)
    except Exception as e:
        if log_event:
            log_event(
                "playwright_self_heal_failed",
                source="browser_common",
                detail=f"install subprocess raised: {str(e)[:200]}",
                level="error",
            )
        else:
            print(f"  [BROWSER] Self-heal subprocess raised: {e}")
        return False

    ok = result.returncode == 0
    if log_event:
        log_event(
            "playwright_self_heal_done" if ok else "playwright_self_heal_failed",
            source="browser_common",
            detail=(f"rc={result.returncode} "
                    f"stdout={result.stdout[-200:]!r} "
                    f"stderr={result.stderr[-200:]!r}"),
            level="info" if ok else "error",
        )
    else:
        print(f"  [BROWSER] Self-heal {'OK' if ok else 'FAILED'} rc={result.returncode}")
    return ok


# Backwards compatibility — existing code calls launch_edge()
launch_edge = launch_browser


# ============================================================
# LOGIN CHECK & AUTO-LOGIN
# ============================================================

async def ensure_logged_in(page, store_name):
    """
    Check if we're logged into a store. If not, auto-login using .env credentials.

    Call this AFTER navigating to a store page. Checks the current URL and
    page content for login redirects.

    Returns True if logged in (or login succeeded), False if login failed.
    """
    url = page.url.lower()

    if "carrefour" in store_name.lower():
        return await _ensure_carrefour_login(page)
    elif "kibsons" in store_name.lower():
        return await _ensure_kibsons_login(page)
    else:
        print(f"  [LOGIN] Unknown store: {store_name}")
        return True  # Assume OK for unknown stores


async def _is_login_page(page):
    """Check if the current page is a login/signin page."""
    url = page.url.lower()
    return "login" in url or "signin" in url or "sign-in" in url


async def _ensure_carrefour_login(page):
    """Check and handle Carrefour UAE login."""
    if not await _is_login_page(page):
        # Also check if the page has a "Login & Register" link (not logged in)
        has_login_link = await page.evaluate("""() => {
            const link = document.querySelector('a[href="/login"]');
            if (!link) return false;
            return (link.textContent || '').toLowerCase().includes('login');
        }""")
        if not has_login_link:
            return True  # Already logged in

    print("  [LOGIN] Carrefour session expired. Attempting auto-login...")
    creds = _load_credentials()
    email = creds.get("CARREFOUR_EMAIL", "")
    password = creds.get("CARREFOUR_PASSWORD", "")

    if not email or not password:
        print("  [LOGIN] ERROR: CARREFOUR_EMAIL and CARREFOUR_PASSWORD not set in .env")
        return False

    try:
        # Step 1: Get to the login page
        if "/login" not in page.url.lower():
            # Navigate to the login page directly
            await page.goto(
                "https://www.carrefouruae.com/login",
                wait_until="domcontentloaded", timeout=30000,
            )
            await asyncio.sleep(3)

        # Step 2: If we're on the email entry page, fill email and submit
        email_input = page.locator("input[data-testid='email-input']")
        if await email_input.count() > 0 and await email_input.is_visible():
            await email_input.fill(email)
            await asyncio.sleep(1)

            # Click "Log in" — this navigates to /login/password
            login_btn = page.locator("button[data-testid='login']")
            try:
                async with page.expect_navigation(timeout=10000):
                    await login_btn.click()
            except Exception:
                pass  # Navigation may not trigger if already on password page
            await asyncio.sleep(3)

        # Step 3: Fill password on the password page
        pw_input = page.locator("input[data-testid='password-input']")
        if await pw_input.count() > 0 and await pw_input.is_visible():
            await pw_input.fill(password)
            await asyncio.sleep(1)

            # Click "Continue" to submit
            continue_btn = page.locator("button[data-testid='continue']")
            try:
                async with page.expect_navigation(timeout=15000):
                    await continue_btn.click()
            except Exception:
                pass  # May already have navigated
            await asyncio.sleep(3)
        else:
            # Fallback: try generic password input
            pw_generic = page.locator("input[type='password']")
            if await pw_generic.count() > 0:
                await pw_generic.fill(password)
                await asyncio.sleep(1)
                submit_btn = page.locator("button[data-testid='continue'], button[type='submit'], button:has-text('Continue')")
                try:
                    async with page.expect_navigation(timeout=15000):
                        await submit_btn.click()
                except Exception:
                    pass
                await asyncio.sleep(3)

        # Verify login succeeded — should no longer be on a login page
        if "login" in page.url.lower():
            error_text = await page.evaluate("""() => {
                const err = document.querySelector('[class*="error"], [class*="Error"], [role="alert"]');
                return err ? err.textContent.trim() : '';
            }""")
            if error_text:
                print(f"  [LOGIN] Carrefour login failed: {error_text}")
            else:
                print("  [LOGIN] Carrefour login may have failed (still on login page)")
            return False

        print("  [LOGIN] Carrefour login successful!")
        return True

    except Exception as e:
        print(f"  [LOGIN] Carrefour auto-login error: {e}")
        return False


async def _ensure_kibsons_login(page):
    """Check and handle Kibsons login.

    Kibsons login URL changed from /en/account/login to /en/login.
    The login form has:
      - input[name='email'] (type=email)
      - input[name='password'] (type=password, plus hidden duplicates)
      - button with text "SIGN IN"
    When not logged in, navigating to /en/account/* redirects to /en/login.
    """
    url = page.url.lower()
    is_on_login = "/login" in url

    if not is_on_login:
        # Check if the page shows "Login / Register" (not logged in) vs account menu
        has_login_prompt = await page.evaluate("""() => {
            const el = document.querySelector('a[href*="/login"], [class*="login"]');
            if (!el) return false;
            return (el.textContent || '').toLowerCase().includes('login');
        }""")
        if not has_login_prompt:
            return True  # Already logged in

    print("  [LOGIN] Kibsons session expired. Attempting auto-login...")
    creds = _load_credentials()
    email = creds.get("KIBSONS_EMAIL", "")
    password = creds.get("KIBSONS_PASSWORD", "")

    if not email or not password:
        print("  [LOGIN] ERROR: KIBSONS_EMAIL and KIBSONS_PASSWORD not set in .env")
        return False

    try:
        # Navigate to the correct login page if not already there
        if "/en/login" not in page.url.lower():
            await page.goto(
                "https://www.kibsons.com/en/login",
                wait_until="domcontentloaded", timeout=30000,
            )
            await asyncio.sleep(5)

        # Fill email — use name selector (most reliable)
        email_input = page.locator("input[name='email']")
        if await email_input.count() == 0:
            # Fallback to type-based selector
            email_input = page.locator("input[type='email']").first
        await email_input.wait_for(state="visible", timeout=10000)
        await email_input.fill(email)
        await asyncio.sleep(1)

        # Fill password — use name selector to avoid hidden duplicates
        pw_input = page.locator("input[name='password']")
        if await pw_input.count() == 0:
            pw_input = page.locator("input[type='password']").first
        await pw_input.wait_for(state="visible", timeout=10000)
        await pw_input.fill(password)
        await asyncio.sleep(1)

        # Click SIGN IN button
        sign_in_btn = page.locator("button:has-text('SIGN IN')")
        if await sign_in_btn.count() == 0:
            sign_in_btn = page.locator(
                "button:has-text('Sign in'), button:has-text('Log in'), "
                "button[type='submit']"
            ).first
        await sign_in_btn.click(timeout=5000)
        await asyncio.sleep(6)

        # Verify login succeeded — should no longer be on login page
        current_url = page.url.lower()
        if "/login" in current_url and "/account" not in current_url:
            error_text = await page.evaluate("""() => {
                const err = document.querySelector('[class*="error"], [class*="Error"], [role="alert"]');
                return err ? err.textContent.trim() : '';
            }""")
            if error_text:
                print(f"  [LOGIN] Kibsons login failed: {error_text}")
            else:
                print("  [LOGIN] Kibsons login may have failed (still on login page)")
            return False

        print("  [LOGIN] Kibsons login successful!")
        return True

    except Exception as e:
        print(f"  [LOGIN] Kibsons auto-login error: {e}")
        return False


# ============================================================
# HEALTH & RECOVERY
# ============================================================

async def check_browser_health(page):
    """Test if the browser/page is still alive. Returns True if healthy."""
    try:
        await page.evaluate("1 + 1")
        return True
    except Exception:
        return False


async def relaunch_browser(p, context, proxy=None):
    """
    Close dead browser and launch a fresh one.

    Call this when check_browser_health() returns False or after
    a TargetClosedError / connection error.

    proxy: pass the same proxy the original browser used, so the replacement
        keeps egressing through the residential IP. Without this, a mid-run
        recovery would silently drop Carrefour's proxy and hit Akamai's block.

    Returns (new_playwright, new_context, new_page).
    """
    print("  [RECOVERY] Browser died. Relaunching...")
    try:
        await context.close()
    except Exception:
        pass
    try:
        await p.stop()
    except Exception:
        pass
    await asyncio.sleep(3)
    p_new, context_new, page_new = await launch_browser(proxy=proxy)
    print("  [RECOVERY] Browser relaunched successfully.")
    return p_new, context_new, page_new


async def safe_close(p, context):
    """Safely close browser and playwright, ignoring errors if already dead."""
    try:
        await context.close()
    except Exception:
        pass
    try:
        await p.stop()
    except Exception:
        pass
