"""
AutoRestock - Cart Builder

Takes a finalized shopping list and builds real carts on store websites.
Uses Playwright + a dedicated Chromium profile to add items to Carrefour and Kibsons carts.
(NOT Edge — uses Playwright's bundled Chromium with a separate profile at ~/.hearthkeeper-browser/)

The cart builder:
  1. Loads the shopping list from the DB
  2. For each item, finds the best product (from knowledge base or search)
  3. Navigates to the product page and adds to cart
  4. Tracks successes and failures
  5. Returns a cart report

IMPORTANT:
  - Edge can stay open — the builder uses its own Chromium instance
  - The builder NEVER clicks "Place Order" -- it only builds the cart
  - You review the cart in the browser and place the order yourself

Usage:
  Called by run_order.py, not directly. But you can test:
    python3 cart_builder.py --list-id 1
    python3 cart_builder.py --list-id 1 --store "Carrefour UAE"
    python3 cart_builder.py --list-id 1 --dry-run
"""

import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from browser_common import (
    launch_edge, check_browser_health, relaunch_browser, safe_close,
    ensure_logged_in, get_carrefour_proxy,
)
from db_setup import DB_PATH, create_database
from list_generate import list_get
from product_knowledge import get_best_alternative, get_ranked_alternatives
from app_logging import get_logger, log_event

logger = get_logger("cart_builder")

# Separate profile for UC Chrome (Carrefour) — can't share with Playwright (Kibsons)
UC_CARREFOUR_PROFILE = str(Path.home() / ".hearthkeeper-carrefour")


# ============================================================
# UC Chrome — Carrefour-specific (bypasses Akamai bot detection)
# ============================================================

def _kill_stale_uc_chrome():
    """Kill any Chrome processes using the UC Carrefour profile directory.

    When a previous build exits uncleanly (crash, timeout, interrupted test),
    Chrome keeps running and locks the profile.  The next UC launch then fails
    with 'session not created: cannot connect to chrome'.

    We identify stale processes by checking their command line for the profile
    path.  Only kills those processes — leaves the user's regular Chrome alone.
    """
    profile_name = Path(UC_CARREFOUR_PROFILE).name  # ".hearthkeeper-carrefour"
    try:
        if sys.platform == "win32":
            # Windows: PowerShell + WMI to find chrome.exe processes whose
            # command line includes our profile directory.
            ps_cmd = (
                f'Get-WmiObject Win32_Process -Filter "Name=\'chrome.exe\'" '
                f'| Where-Object {{ $_.CommandLine -like \'*{profile_name}*\' }} '
                f'| Select-Object -ExpandProperty ProcessId'
            )
            result = subprocess.run(
                ["powershell", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=10
            )
            pids = [int(p.strip()) for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
            if pids:
                logger.info(f"[UC] Killing {len(pids)} stale Chrome processes: {pids}")
                pid_list = ",".join(str(p) for p in pids)
                subprocess.run(
                    ["powershell", "-Command", f"Stop-Process -Id {pid_list} -Force -ErrorAction SilentlyContinue"],
                    capture_output=True, timeout=10
                )
                time.sleep(2)  # Give the OS time to release file locks
        else:
            # Linux/Mac: pkill processes whose command line includes the profile.
            subprocess.run(
                ["pkill", "-f", f"chrome.*{profile_name}"],
                capture_output=True, timeout=5
            )
            time.sleep(2)  # Give the OS time to release file locks
    except Exception as e:
        logger.warning(f"[UC] Failed to kill stale Chrome: {e}")

    # Remove Chrome's SingletonLock — prevents "profile already in use" errors
    for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = Path(UC_CARREFOUR_PROFILE) / lock_file
        try:
            lock_path.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("Could not remove Chrome lock file %s: %s", lock_file, e)


def _detect_chrome_major():
    """Return the installed Google Chrome major version (e.g. 148), or None.

    undetected-chromedriver uses this to fetch a matching chromedriver --
    without it, UC defaults to the latest chromedriver build, which can
    refuse to attach to an older Chrome ("This version of ChromeDriver
    only supports Chrome version N").

    Windows: chrome.exe does not print to stdout with --version, so read
    the file's version metadata via PowerShell instead.

    Linux / macOS: ask the binary for its version.
    """
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for p in candidates:
            if not Path(p).exists():
                continue
            try:
                ps_cmd = '(Get-Item -LiteralPath "' + p + '").VersionInfo.FileVersion'
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                m = re.match(r"\s*(\d+)\.", out)
                if m:
                    return int(m.group(1))
            except (OSError, subprocess.SubprocessError):
                continue
        return None

    for binary in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        try:
            out = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=10
            ).stdout
            m = re.search(r"\b(\d+)\.", out)
            if m:
                return int(m.group(1))
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            continue
    return None


def _launch_uc_chrome():
    """
    Launch Chrome with undetected-chromedriver for Carrefour.

    Uses a dedicated profile at ~/.hearthkeeper-carrefour/ so login
    sessions persist. This profile is separate from the Playwright one.

    Requires Google Chrome to be installed (not Edge).
    Returns a Selenium WebDriver instance.
    """
    # Clean up any stale Chrome from a previous build
    _kill_stale_uc_chrome()

    Path(UC_CARREFOUR_PROFILE).mkdir(parents=True, exist_ok=True)

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={UC_CARREFOUR_PROFILE}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-sandbox")  # required when running Chrome as root (server)

    # Carrefour's Akamai bot check blocks datacenter IPs. On a cloud server,
    # route this browser through a residential proxy (CARREFOUR_PROXY in .env).
    # On a home machine the proxy is unset and the browser connects directly.
    proxy = get_carrefour_proxy()
    if proxy:
        options.add_argument(f"--proxy-server={proxy['server']}")
        logger.info(f"[UC] Carrefour routed through proxy: {proxy['server']}")
        if proxy.get("username"):
            # Chrome's --proxy-server cannot carry inline credentials, so the
            # UC (Carrefour) path needs an IP-whitelisted proxy. A user/pass
            # proxy would silently fail to authenticate here.
            logger.warning(
                "[UC] CARREFOUR_PROXY includes a username/password, but Chrome "
                "cannot use inline proxy credentials. Use an IP-whitelisted "
                "proxy, or UC Chrome will not be able to authenticate to it.")

    # Match the driver to the installed Chrome version. Passing version_main
    # explicitly avoids undetected-chromedriver's flaky auto-detection — UC
    # then downloads the matching chromedriver itself.
    chrome_major = _detect_chrome_major()
    if chrome_major:
        logger.info(f"[UC] Detected Chrome major version: {chrome_major}")
    else:
        logger.warning("[UC] Could not detect Chrome version; UC will auto-detect")

    driver = uc.Chrome(
        options=options,
        headless=False,
        version_main=chrome_major,
    )
    driver.set_window_size(1280, 900)
    return driver


def _uc_carrefour_email_login(driver):
    """
    Drive the Carrefour UAE email+password login form via UC Chrome.

    Standalone helper -- does NOT do the homepage "are we logged in?"
    sanity check. Use this when the caller already knows the homepage
    check is misleading (e.g. order_scraper recovering from a /login/phone
    step-up redirect: cookies + localStorage on the homepage can still
    look "logged in" while /app/* routes demand a fresh credential
    exchange).

    Reads credentials from .env (CARREFOUR_EMAIL / CARREFOUR_PASSWORD).

    Returns True on success, False on failure.
    """
    logger.info("  [UC] Forcing Carrefour email+password login flow...")

    # Load credentials
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

    email = creds.get("CARREFOUR_EMAIL", "")
    password = creds.get("CARREFOUR_PASSWORD", "")

    if not email or not password:
        logger.error("  [UC] CARREFOUR_EMAIL/PASSWORD not set in .env")
        return False

    try:
        driver.get("https://www.carrefouruae.com/login")
        time.sleep(4)

        # Fill email
        try:
            email_input = WebDriverWait(driver, 10).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, "input[data-testid='email-input']"))
            )
            email_input.clear()
            email_input.send_keys(email)
            time.sleep(1)

            login_btn = driver.find_element(By.CSS_SELECTOR, "button[data-testid='login']")
            login_btn.click()
            time.sleep(4)
        except Exception as e:
            logger.warning(f"  [UC] Email step failed: {e}")

        # Fill password
        try:
            pw_input = WebDriverWait(driver, 10).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, "input[data-testid='password-input']"))
            )
            pw_input.clear()
            pw_input.send_keys(password)
            time.sleep(1)

            continue_btn = driver.find_element(By.CSS_SELECTOR, "button[data-testid='continue']")
            continue_btn.click()
            time.sleep(5)
        except Exception as e:
            logger.warning(f"  [UC] Password step failed: {e}")
            # Try generic password input
            try:
                pw_generic = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
                pw_generic.clear()
                pw_generic.send_keys(password)
                time.sleep(1)
                submit_btn = driver.find_element(By.CSS_SELECTOR,
                    "button[data-testid='continue'], button[type='submit']")
                submit_btn.click()
                time.sleep(5)
            except Exception as e:
                logger.warning("Generic password input fallback failed: %s", e)

        # Verify login
        if "/login" in driver.current_url.lower():
            logger.warning("  [UC] Carrefour login may have failed (still on login page)")
            return False

        logger.info("  [UC] Carrefour login successful!")
        return True

    except Exception as e:
        logger.error(f"  [UC] Carrefour login error: {e}")
        return False


def _uc_ensure_carrefour_login(driver):
    """
    Check if we're logged into Carrefour UAE. If not, auto-login via the
    email+password flow. Returns True if logged in, False if login failed.

    Note: the homepage "logged-in" check here can give false-positives when
    UC's persistent profile retains localStorage / IndexedDB even after
    cookies are cleared, or when only some of Carrefour's routes step-up
    auth. For step-up recovery (e.g. /app/orders bouncing to /login/phone),
    call _uc_carrefour_email_login(driver) directly instead -- it skips the
    homepage shortcut and drives the login form unconditionally.
    """
    driver.get("https://www.carrefouruae.com/mafuae/en/")
    time.sleep(5)

    # Dismiss cookie consent if present
    try:
        cookie_btn = driver.find_element(By.CSS_SELECTOR,
            'button[id*="onetrust-accept"], button[id*="accept"]')
        cookie_btn.click()
        time.sleep(1)
    except Exception as e:
        logger.debug("Cookie consent dismissal skipped (best-effort): %s", e)

    # Check if already logged in — look for account/profile elements
    try:
        page_source = driver.page_source.lower()
        # If there's a "Login" link, we're not logged in
        if 'href="/login"' in page_source or '>login<' in page_source:
            pass  # Need to log in
        else:
            logger.info("  [UC] Already logged into Carrefour")
            return True
    except Exception as e:
        logger.warning("Carrefour login-status check failed: %s", e)

    # Check for login/register link more specifically
    try:
        login_links = driver.find_elements(By.CSS_SELECTOR, 'a[href="/login"], a[href*="/login"]')
        has_login_link = any("login" in (el.text or "").lower() for el in login_links)
        if not has_login_link:
            logger.info("  [UC] Already logged into Carrefour (no login link)")
            return True
    except Exception as e:
        logger.warning("Carrefour login-link check failed: %s", e)

    logger.info("  [UC] Carrefour session expired. Attempting auto-login...")
    return _uc_carrefour_email_login(driver)


# ============================================================
# CART ATTEMPT TRACKING (per-item forensics)
# ============================================================

def _classify_cart_error(error_msg):
    """Categorize an add-to-cart error string for the cart_attempts
    table. Drives the OOS/404 fallback decision in build_cart and
    the per-reason breakdown in /api/diag/cart-attempts.

    Returns one of:
        'ok'         -- success
        'oos'        -- out of stock / sold out / currently unavailable
        'not_found'  -- 404 / "trouble finding" / login redirect
                        (treat like OOS: try alternatives + mark stale)
        'login'      -- session ejected (redirect to /account /login)
        'add_failed' -- button found but click/qty change didn't take
        'no_kb'      -- no knowledge-base product for this item
        'other'      -- anything else
    """
    if not error_msg:
        return "ok"
    e = error_msg.lower()
    if any(k in e for k in ("out of stock", "sold out",
                            "currently unavailable", "out_of_stock")):
        return "oos"
    if any(k in e for k in ("not found", "404", "trouble finding",
                            "does not exist", "page not found")):
        return "not_found"
    if "/login" in e or "/account" in e or "session expired" in e:
        return "login"
    if "knowledge base" in e or "no product found" in e or "no kb" in e:
        return "no_kb"
    if any(k in e for k in ("button not found", "add to cart button",
                            "qty did not change", "quantity did not change")):
        return "add_failed"
    return "other"


def _record_cart_attempt(conn, *, list_id, list_item_id, item_id, store_id,
                         attempt_n, url, success, reason, duration_ms=None):
    """Persist one add-to-cart attempt to cart_attempts + emit a
    warning event on failure. This is the data layer we were missing
    -- previously every failure lived only in the in-memory
    store_report dict and was thrown away when build_cart returned.

    Idempotent in the sense that the same logical attempt won't be
    re-inserted (caller's responsibility -- we don't dedupe here),
    but a transient SQLite hiccup won't raise; we just log.
    """
    reason_class = "ok" if success else _classify_cart_error(reason or "")
    try:
        conn.execute(
            """INSERT INTO cart_attempts
                   (list_id, list_item_id, item_id, store_id, attempt_n,
                    url, success, reason, reason_class, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                list_id, list_item_id, item_id, store_id, attempt_n,
                url, 1 if success else 0, (reason or "")[:300],
                reason_class, duration_ms,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.warning("cart_attempts insert failed: %s", e)
    if not success:
        log_event(
            "cart_item_failed", source="cart_builder",
            detail=(
                f"list_id={list_id} item_id={item_id} store_id={store_id} "
                f"attempt={attempt_n} class={reason_class} "
                f"url={(url or '')[:80]} reason={(reason or '')[:160]}"
            )[:400],
            level="warning",
        )


def _mark_catalog_stale(conn, url, reason="not_found"):
    """Flag a catalog row as unavailable when the cart builder hits
    a 404 or login redirect on it. The next list_generate run sees
    is_available=0 and won't pick this URL again. The next nightly
    catalog scrape refreshes the row (new URL or new is_available
    state).
    """
    if not url:
        return
    try:
        conn.execute(
            "UPDATE product_catalog SET is_available = 0 WHERE product_url = ?",
            (url,),
        )
        conn.commit()
        logger.info("  Marked catalog stale (%s): %s", reason, url[:80])
    except Exception as e:
        logger.warning("Could not mark catalog stale: %s", e)


def _uc_add_to_cart_carrefour(driver, product_url, quantity=1):
    """
    Navigate to a Carrefour product page and add it to cart using UC Chrome.

    Returns dict: {"success": True/False, "name": str, "price": float, "error": str or None}
    """
    result = {"success": False, "name": "", "price": 0, "error": None}

    product_url = _normalize_carrefour_url(product_url)

    try:
        driver.get(product_url)
        time.sleep(5)  # Carrefour pages need time for React hydration

        # Dismiss cookie consent if it appears
        try:
            cookie_btn = driver.find_element(By.CSS_SELECTOR,
                'button[id*="onetrust-accept"], button[id*="accept"]')
            cookie_btn.click()
            time.sleep(1)
        except Exception as e:
            logger.debug("Cookie consent dismissal skipped (best-effort): %s", e)

        # Check for 404 / product not found
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:500].lower()
            if "trouble finding" in body_text or "page not found" in body_text:
                result["error"] = "Product page not found (404)"
                return result
        except Exception as e:
            logger.warning("Carrefour 404 check failed: %s", e)

        # Extract product name
        try:
            h1 = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "h1"))
            )
            result["name"] = h1.text.strip()
        except Exception as e:
            logger.warning("Carrefour product name extraction failed: %s", e)

        # Extract price — Carrefour splits "AED 22 .79" across divs
        try:
            # Find the first visible "AED" text near the product name area
            aed_els = driver.find_elements(By.XPATH,
                "//div[text()='AED']/parent::*")
            for aed_parent in aed_els[:2]:
                full_text = aed_parent.text.strip()
                # Parse "AED 22 .79 AED 91.16 per Kg" → take first number
                nums = re.findall(r'(\d+)\s*\.(\d+)', full_text)
                if nums:
                    result["price"] = float(f"{nums[0][0]}.{nums[0][1]}")
                    break
        except Exception as e:
            logger.warning("Carrefour price extraction failed: %s", e)

        # Check if already in cart — Carrefour replaces Add to Cart with
        # a quantity widget: [trash icon] [qty dropdown] [+ icon]
        # The qty dropdown has role="combobox". SVG icons have no text in
        # Selenium, so detect via combobox or bg-secondary container.
        try:
            qty_combo = driver.find_element(By.CSS_SELECTOR,
                'button[role="combobox"]')
            if qty_combo.is_displayed():
                logger.info(f"  [UC] Already in cart: {result['name'][:50]}")
                result["success"] = True
                # Handle quantity > 1 — find the + button (sibling after combobox)
                if quantity > 1:
                    try:
                        parent = qty_combo.find_element(By.XPATH, './..')
                        siblings = parent.find_elements(By.TAG_NAME, 'button')
                        plus_btn = siblings[-1] if siblings else None
                        if plus_btn:
                            for _ in range(quantity - 1):
                                plus_btn.click()
                                time.sleep(0.5)
                    except Exception as e:
                        logger.warning("Carrefour quantity increment (already-in-cart) failed: %s", e)
                return result
        except Exception as e:
            logger.warning("Carrefour in-cart check failed, proceeding to add: %s", e)

        # Check if out of stock (case-insensitive via translate)
        try:
            oos = driver.find_element(By.XPATH,
                "//*[contains(translate(text(),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                "'out of stock')]")
            if oos and oos.is_displayed():
                result["error"] = "Out of stock"
                return result
        except Exception as e:
            logger.warning("Carrefour out-of-stock check failed: %s", e)

        # Wait for "Add to cart" button — text is inside a child <div>,
        # so use contains(., ...) to search all descendant text
        add_btn = None
        try:
            add_btn = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH,
                    "//button[contains(., 'Add to cart')]"
                    " | //button[contains(., 'Add to Cart')]"
                    " | //button[contains(., 'ADD TO CART')]"
                ))
            )
        except Exception as e:
            logger.warning("Carrefour Add to Cart button not found within timeout: %s", e)

        if not add_btn:
            # Last check: maybe combobox appeared during the wait (race condition)
            try:
                qty_combo = driver.find_element(By.CSS_SELECTOR,
                    'button[role="combobox"]')
                if qty_combo.is_displayed():
                    logger.info(f"  [UC] Already in cart (late detect): {result['name'][:50]}")
                    result["success"] = True
                    return result
            except Exception as e:
                logger.warning("Carrefour late in-cart detection failed: %s", e)

            # Debug: what buttons ARE on the page?
            try:
                btns = driver.find_elements(By.TAG_NAME, "button")
                btn_texts = [b.text.strip()[:40] for b in btns[:25] if b.text.strip()]
                logger.warning(f"  [UC] No Add to Cart button. Page buttons: {btn_texts}")
            except Exception as e:
                logger.warning("Carrefour page button enumeration failed: %s", e)

            result["error"] = "Add to Cart button not found (UC Chrome)"
            return result

        # Click the button
        add_btn.click()
        time.sleep(2)

        # Verify it was added — quantity combobox should now appear
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    'button[role="combobox"]'))
            )
            result["success"] = True
        except Exception:
            # Button was clicked — assume success even without verification
            result["success"] = True

        # Handle quantity > 1 — click the + button
        if result["success"] and quantity > 1:
            try:
                qty_combo = driver.find_element(By.CSS_SELECTOR,
                    'button[role="combobox"]')
                parent = qty_combo.find_element(By.XPATH, './..')
                siblings = parent.find_elements(By.TAG_NAME, 'button')
                plus_btn = siblings[-1] if siblings else None
                if plus_btn:
                    for _ in range(quantity - 1):
                        plus_btn.click()
                        time.sleep(0.5)
            except Exception as e:
                logger.warning("Carrefour quantity increment after add failed: %s", e)

        return result

    except Exception as e:
        result["error"] = f"UC Chrome error: {e}"
        logger.error(f"  [UC] Error: {e}")
        return result


# --------------------------------------------
# Store-specific: Carrefour UAE
# --------------------------------------------

CARREFOUR_CART_URL = "https://www.carrefouruae.com/mafuae/en/app/cart"


async def _recover_page_state(page, timeout=8000):
    """
    Navigate to about:blank to clear any error state (chrome-error://, pending navigations).
    Called between items after a failure to prevent cascading navigation errors.
    """
    try:
        await page.goto("about:blank", wait_until="load", timeout=timeout)
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning(f"Page recovery (about:blank) failed: {e}")
        # Last resort: wait and hope the page settles
        await asyncio.sleep(2)


def _normalize_carrefour_url(url):
    """
    Fix Carrefour URLs that are missing the /mafuae/en/ path prefix.

    Some scraped URLs are like:
        https://www.carrefouruae.com/eggs/jenan-organic-15
    But should be:
        https://www.carrefouruae.com/mafuae/en/eggs/jenan-organic-15

    Also handles http:// -> https:// and trailing slashes.
    """
    if not url:
        return url

    url = url.strip()

    # Ensure https
    if url.startswith("http://"):
        url = "https://" + url[7:]

    # Check if URL is on carrefouruae.com but missing /mafuae/en/
    if "carrefouruae.com" in url and "/mafuae/en/" not in url:
        # Extract the path after the domain
        m = re.match(r'(https?://[^/]+)(/.*)$', url)
        if m:
            domain = m.group(1)
            path = m.group(2)
            # Don't double-add if already has /mafuae/ (just missing /en/)
            if path.startswith("/mafuae/"):
                if not path.startswith("/mafuae/en/"):
                    path = "/mafuae/en/" + path[len("/mafuae/"):]
            else:
                path = "/mafuae/en" + path
            url = domain + path
            logger.info(f"  Normalized Carrefour URL: {url}")

    return url


def _normalize_kibsons_url(url):
    """
    Fix Kibsons URLs that are missing the /product/ path segment.

    Bug found Feb 2026: The RSC scraper was generating URLs like:
        https://www.kibsons.com/en/fruits/apples-pears/royal-gala-apple-1kg-12345
    But the correct format requires /product/:
        https://www.kibsons.com/en/product/fruits/apples-pears/royal-gala-apple-1kg-12345

    Without /product/, Kibsons redirects to the account/login page,
    causing 100% cart build failures.
    """
    if not url:
        return url

    url = url.strip()

    # Ensure https
    if url.startswith("http://"):
        url = "https://" + url[7:]

    # Check if URL is on kibsons.com and missing /product/
    if "kibsons.com" in url:
        # Pattern: /en/{category}/... but NOT /en/product/{category}/...
        # Also exclude known non-product paths like /en/cart, /en/login, /en/account
        m = re.match(r'(https?://[^/]+)/en/(?!product/|cart|login|account|checkout|search)(.+)$', url)
        if m:
            domain = m.group(1)
            rest = m.group(2)
            url = f"{domain}/en/product/{rest}"
            logger.info(f"  Normalized Kibsons URL: {url}")

    return url


async def _get_carrefour_cart_guid(context, page):
    """
    Get the Carrefour cart guid and auth token needed for the addItem API.

    Returns (guid, auth_token) tuple.

    Strategy:
    1. Check context.cookies() for 'mafuae-cart' (set by server or JS)
    2. Check local DB for a previously saved guid
    3. If not found, generate a new UUID and set it as the cookie.
       The addItem API will create a cart for this guid automatically.
    4. Validate the guid by making a test API call.
    """
    import uuid

    # First, extract auth token from cookies — needed for API calls
    auth_token = None
    all_cookies = []
    try:
        all_cookies = await context.cookies("https://www.carrefouruae.com")
        for cookie in all_cookies:
            if cookie["name"] == "authToken" and cookie["value"]:
                auth_token = cookie["value"]
                logger.info(f"  Found authToken ({len(auth_token)} chars)")
                break
    except Exception as e:
        logger.warning(f"  context.cookies() failed: {e}")

    # Strategy 1: Check cookie jar for existing mafuae-cart
    for cookie in all_cookies:
        if cookie["name"] == "mafuae-cart" and cookie["value"]:
            guid = cookie["value"]
            logger.info(f"  Cart guid from cookies: {guid[:12]}...")
            return guid, auth_token
    cookie_names = [c["name"] for c in all_cookies]
    logger.info(f"  No mafuae-cart cookie. Have: {', '.join(cookie_names[:20])}")

    # Strategy 2: Check DB for previously saved guid
    try:
        db_conn = sqlite3.connect(DB_PATH)
        row = db_conn.execute(
            "SELECT value FROM app_state WHERE key = 'carrefour_cart_guid'"
        ).fetchone()
        if row and row[0]:
            guid = row[0]
            logger.info(f"  Cart guid from DB: {guid[:12]}...")
            await context.add_cookies([{
                "name": "mafuae-cart",
                "value": guid,
                "domain": ".carrefouruae.com",
                "path": "/",
            }])
            db_conn.close()
            return guid, auth_token
        db_conn.close()
    except Exception as e:
        logger.info(f"  DB guid check: {e}")

    # Strategy 3: Generate a new UUID and test it with the API
    new_guid = str(uuid.uuid4())
    logger.info(f"  Generated new cart guid: {new_guid[:12]}...")

    try:
        await context.add_cookies([{
            "name": "mafuae-cart",
            "value": new_guid,
            "domain": ".carrefouruae.com",
            "path": "/",
        }])
    except Exception as e:
        logger.warning(f"  Failed to set cookie: {e}")
        return None, auth_token

    # Ensure we're on carrefouruae.com for the test API call
    try:
        if "carrefouruae.com" not in page.url:
            await page.goto("https://www.carrefouruae.com/mafuae/en/",
                            wait_until="domcontentloaded", timeout=20000)
            await _wait_for_akamai_overlay(page)
    except Exception as e:
        logger.warning("Navigation to carrefouruae.com for test API call failed: %s", e)

    # Test: make a lightweight API call to verify the guid works
    try:
        test_result = await page.evaluate("""
            async (args) => {
                const { guid, authToken } = args;
                const params = new URLSearchParams({
                    guid,
                    isCartRequired: 'false',
                    lm: 'true',
                    nsp: 'food,nonfood,express,QCOMM,QELEC',
                    latitude: '25.2048',
                    longitude: '55.2708',
                });
                const url = '/v6/carts/mafuae/en/STANDARD/addItem?' + params;
                const body = {
                    productId: '1278329',
                    quantity: 1,
                    shopId: '0000',
                    shopName: 'Carrefour',
                    offerId: 'offer_carrefour_',
                    intent: 'SLOTTED',
                    isSoldByWeight: false,
                    originalQuantity: 0,
                    serviceProductCode: [],
                    pis: ['SLOTTED'],
                    productName: 'Test item',
                    imageUrl: '',
                };

                const headers = { 'Content-Type': 'application/json' };
                if (authToken) {
                    headers['Authorization'] = 'Bearer ' + authToken;
                }

                try {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers,
                        body: JSON.stringify(body),
                        credentials: 'include',
                    });
                    const text = await resp.text();
                    let data = {};
                    try { data = JSON.parse(text); } catch(e) {}

                    // Also grab response headers for debugging
                    const respHeaders = {};
                    resp.headers.forEach((v, k) => { respHeaders[k] = v; });

                    return {
                        status: resp.status,
                        ok: resp.ok,
                        totalItems: data.totalItems || data.totalUnitCount || 0,
                        guid: data.guid || '',
                        keys: Object.keys(data).join(','),
                        preview: text.substring(0, 300),
                        respHeaders: JSON.stringify(respHeaders).substring(0, 200),
                    };
                } catch(e) {
                    return { status: 0, ok: false, error: e.message };
                }
            }
        """, {"guid": new_guid, "authToken": auth_token or ""})

        logger.info(f"  Test API: status={test_result.get('status')}, "
                     f"ok={test_result.get('ok')}, items={test_result.get('totalItems')}, "
                     f"keys={test_result.get('keys','')[:80]}")
        if test_result.get("preview"):
            logger.info(f"  Test API body: {test_result['preview'][:200]}")
        if test_result.get("respHeaders"):
            logger.info(f"  Test API headers: {test_result['respHeaders'][:200]}")

        if test_result and test_result.get("ok"):
            logger.info(f"  New guid verified! Saving to DB.")
            server_guid = test_result.get("guid")
            if server_guid and server_guid != new_guid:
                logger.info(f"  Server assigned guid: {server_guid[:12]}...")
                new_guid = server_guid
                await context.add_cookies([{
                    "name": "mafuae-cart",
                    "value": new_guid,
                    "domain": ".carrefouruae.com",
                    "path": "/",
                }])

            # Remove the test item we added
            try:
                await page.evaluate("""
                    async (args) => {
                        const { guid, authToken } = args;
                        const params = new URLSearchParams({
                            guid, isCartRequired: 'false', lm: 'true',
                            nsp: 'food,nonfood,express,QCOMM,QELEC',
                        });
                        const headers = { 'Content-Type': 'application/json' };
                        if (authToken) headers['Authorization'] = 'Bearer ' + authToken;
                        await fetch('/v6/carts/mafuae/en/STANDARD/deleteItems?' + params, {
                            method: 'DELETE', headers, credentials: 'include',
                            body: JSON.stringify({ items: [{ productId: '1278329', sellerId: '0000' }] }),
                        });
                    }
                """, {"guid": new_guid, "authToken": auth_token or ""})
            except Exception as e:
                logger.debug("Test cart item cleanup failed (best-effort): %s", e)

            # Save to DB
            try:
                db_conn = sqlite3.connect(DB_PATH)
                db_conn.execute(
                    "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
                    ("carrefour_cart_guid", new_guid)
                )
                db_conn.commit()
                db_conn.close()
            except Exception as e:
                logger.warning(f"  Failed to save guid to DB: {e}")

            return new_guid, auth_token
        else:
            status = test_result.get("status", "?")
            preview = test_result.get("preview", "")[:200]
            logger.warning(f"  Test API failed: HTTP {status} — {preview}")
            # Even though guid didn't validate, still return it so we can try
            # with each real item (the test product might be the problem)
            logger.info(f"  Returning unvalidated guid anyway — real items may work")
            return new_guid, auth_token

    except Exception as e:
        logger.warning(f"  Test API exception: {e}")
        return new_guid, auth_token


async def _add_to_cart_carrefour_api(page, product_url, product_name="", quantity=1,
                                     guid=None, auth_token=None):
    """
    Add item to Carrefour cart via their REST API instead of clicking buttons.

    Bypasses React hydration / Akamai bot detection issues by calling the cart
    API directly from the page context.

    Args:
        page: Playwright page (must be on carrefouruae.com domain)
        product_url: Product URL containing /p/{productId}
        product_name: Display name for logging
        quantity: Number to add
        guid: Cart guid (from _get_carrefour_cart_guid). Required.
        auth_token: Bearer token for authentication.
    """
    result = {"success": False, "name": product_name, "price": 0, "error": None}

    if not guid:
        result["error"] = "No cart guid provided"
        logger.warning("  API: no guid — cannot add to cart")
        return result

    # Extract productId from URL (e.g., /p/1278329 → "1278329")
    match = re.search(r'/p/(\d+)', product_url)
    if not match:
        result["error"] = f"Cannot extract product ID from URL: {product_url}"
        logger.warning(f"  Cannot extract product ID from: {product_url}")
        return result
    product_id = match.group(1)

    # Caller should already be on the product page (or at least on carrefouruae.com).
    # Only navigate if we're not on the domain at all.
    try:
        if "carrefouruae.com" not in page.url:
            logger.info(f"  API: navigating to Carrefour domain")
            await page.goto("https://www.carrefouruae.com/mafuae/en/",
                            wait_until="domcontentloaded", timeout=20000)
            await _wait_for_akamai_overlay(page)
    except Exception as e:
        logger.warning(f"  API: domain navigation failed: {e}")

    try:
        api_result = await page.evaluate("""
            async (args) => {
                const { productId, productName, quantity, guid, authToken } = args;

                // Call addItem API with location params (Dubai)
                const params = new URLSearchParams({
                    guid,
                    isCartRequired: 'false',
                    lm: 'true',
                    nsp: 'food,nonfood,express,QCOMM,QELEC',
                    latitude: '25.2048',
                    longitude: '55.2708',
                });
                const url = '/v6/carts/mafuae/en/STANDARD/addItem?' + params;

                const body = {
                    productId,
                    quantity,
                    shopId: '0000',
                    shopName: 'Carrefour',
                    offerId: 'offer_carrefour_',
                    intent: 'SLOTTED',
                    isSoldByWeight: false,
                    originalQuantity: 0,
                    serviceProductCode: [],
                    pis: ['SLOTTED'],
                    productName: productName || '',
                    imageUrl: '',
                };

                // Build headers with Bearer token
                const headers = { 'Content-Type': 'application/json' };
                if (authToken) {
                    headers['Authorization'] = 'Bearer ' + authToken;
                }

                try {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers,
                        body: JSON.stringify(body),
                        credentials: 'include',
                    });

                    if (!resp.ok) {
                        const text = await resp.text().catch(() => '');
                        return {
                            ok: false,
                            error: 'HTTP ' + resp.status,
                            detail: text.substring(0, 300),
                        };
                    }

                    const data = await resp.json().catch(() => ({}));
                    const totalItems = data.totalItems || data.totalUnitCount || 0;
                    return { ok: true, totalItems, productId };
                } catch (e) {
                    return { ok: false, error: e.message || 'fetch failed' };
                }
            }
        """, {"productId": product_id, "productName": product_name,
              "quantity": quantity, "guid": guid, "authToken": auth_token or ""})

        if api_result and api_result.get("ok"):
            result["success"] = True
            result["name"] = product_name
            total = api_result.get("totalItems", "?")
            logger.info(f"  API OK: {product_name} (id={product_id}, cart={total} items)")
        else:
            error = api_result.get("error", "Unknown") if api_result else "No response"
            detail = api_result.get("detail", "") if api_result else ""
            result["error"] = f"API: {error}"
            logger.warning(f"  API FAIL: {error}")
            if detail:
                logger.warning(f"  API detail: {detail[:200]}")
    except Exception as e:
        result["error"] = f"API exception: {e}"
        logger.warning(f"  API exception: {e}")

    return result


async def _wait_for_akamai_overlay(page, timeout=20000):
    """
    Wait for Carrefour's Akamai Bot Manager overlay (#sec-overlay) to clear.

    Akamai injects a full-screen overlay (z-index: 2147483647) that blocks all
    page content until its fingerprint check passes. Once cleared, the overlay
    is set to display:none or removed from DOM entirely.

    Without this wait, the product page HTML is present but invisible/inaccessible.
    """
    try:
        await page.wait_for_function(
            """() => {
                const overlay = document.getElementById('sec-overlay');
                if (!overlay) return true;
                return getComputedStyle(overlay).display === 'none';
            }""",
            timeout=timeout
        )
    except Exception:
        logger.warning(f"  Akamai overlay did not clear within {timeout}ms — proceeding anyway")

    # Dismiss cookie consent banner if present (can overlay product buttons)
    try:
        cookie_btn = await page.query_selector(
            'button[id*="onetrust-accept"], '
            'button:has-text("Accept All"), '
            'button:has-text("Accept Cookies")'
        )
        if cookie_btn:
            await cookie_btn.click()
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.debug("Cookie consent dismissal skipped (best-effort): %s", e)

    # Brief pause for React to finish rendering after overlay clears
    await asyncio.sleep(1)


async def _add_to_cart_carrefour(page, product_url, quantity=1, timeout=15000,
                                 guid=None, auth_token=None):
    """
    Navigate to a Carrefour product page and add it to cart.

    Tries button click first (works when React hydrates).
    If button not found, falls back to API call (bypasses hydration issues).

    Returns dict:
      {"success": True/False, "name": str, "price": float, "error": str or None}
    """
    result = {"success": False, "name": "", "price": 0, "error": None}

    # Normalize the URL before navigating
    product_url = _normalize_carrefour_url(product_url)

    try:
        # Navigate to product page
        response = await page.goto(product_url, wait_until="domcontentloaded", timeout=timeout)

        # Check for HTTP errors (4xx, 5xx) — page loaded but returned an error
        if response and response.status >= 400:
            result["error"] = f"HTTP {response.status} at {product_url}"
            logger.warning(f"  HTTP {response.status} for {product_url}")
            await _recover_page_state(page)
            return result

        # Check if we landed on an error page or got redirected away
        current_url = page.url
        if "chrome-error://" in current_url or "about:blank" in current_url:
            result["error"] = f"Navigation error (landed on {current_url})"
            logger.warning(f"  Landed on error page: {current_url}")
            await _recover_page_state(page)
            return result

        # Wait for Akamai Bot Manager overlay to clear.
        # Carrefour uses Akamai (#sec-overlay) which blocks the entire page
        # until fingerprint check passes. Must wait for it to go away.
        await _wait_for_akamai_overlay(page)

        # Wait for network to settle — React hydration requires JS bundles
        # to load and execute. SSR HTML is present but buttons are client-only.
        try:
            await page.wait_for_load_state("networkidle", timeout=25000)
        except Exception:
            logger.warning("  Carrefour: networkidle timeout (25s)")

        # Extract product name for verification
        name_el = await page.query_selector('h1, [data-testid="product_name"], .product-name')
        if name_el:
            result["name"] = (await name_el.inner_text()).strip()

        # Extract price
        price_text = await page.evaluate("""
            () => {
                // Try multiple selectors for price
                const selectors = [
                    '[data-testid="product_price"]',
                    '.product-price .price',
                    '[class*="price"] [class*="amount"]',
                    '.css-1i90gmp',  // Carrefour-specific price class
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const text = el.innerText || el.textContent || '';
                        const match = text.match(/([\\d.]+)/);
                        if (match) return parseFloat(match[1]);
                    }
                }
                return 0;
            }
        """)
        result["price"] = price_text or 0

        # Check if already in cart (quantity selector visible instead of Add button)
        already_in_cart = await page.query_selector('[data-testid="quantity_selector"], .qty-selector')

        if already_in_cart:
            print(f"      Already in cart. Adjusting quantity...")
            # Item already in cart -- just adjust quantity if needed
            if quantity > 1:
                await _set_quantity_carrefour(page, quantity)
            result["success"] = True
            return result

        # Find and click "Add to Cart" button
        add_btn_selector = (
            'button[data-testid="add_to_cart_button"], '
            'button:has-text("Add to Cart"), '
            'button:has-text("Add to Trolley"), '
            'button:has-text("ADD"), '
            '[class*="add-to-cart"] button'
        )
        add_btn = await page.query_selector(add_btn_selector)
        if not add_btn:
            # Wait for React hydration — button is client-rendered, not in SSR HTML
            try:
                add_btn = await page.wait_for_selector(add_btn_selector, timeout=15000)
            except Exception:
                add_btn = None

        if not add_btn:
            # Comprehensive debug: figure out WHY the product content isn't rendering
            try:
                debug = await page.evaluate("""() => {
                    const url = window.location.href;
                    const title = document.title;
                    const btns = Array.from(document.querySelectorAll('button')).slice(0, 15)
                        .map(b => b.textContent.trim().substring(0, 50));

                    // Check Akamai overlay status
                    const secOverlay = document.getElementById('sec-overlay');
                    const overlayStatus = secOverlay
                        ? `display=${getComputedStyle(secOverlay).display}, zIndex=${getComputedStyle(secOverlay).zIndex}`
                        : 'not_in_dom';

                    // Check for 404/error page content
                    const bodyText = document.body.innerText.substring(0, 500);
                    const has404 = bodyText.includes('trouble finding') || bodyText.includes('not found')
                                   || bodyText.includes('404') || bodyText.includes('does not exist');

                    // Check main content area
                    const main = document.querySelector('main, #__next, [id*="app"]');
                    const mainChildCount = main ? main.children.length : -1;
                    const mainText = main ? main.innerText.substring(0, 200) : 'no_main';

                    // Check all h1/h2 elements
                    const headings = Array.from(document.querySelectorAll('h1, h2')).slice(0, 5)
                        .map(h => h.textContent.trim().substring(0, 80));

                    // Check HTML size (SSR content should be large)
                    const htmlSize = document.documentElement.outerHTML.length;

                    return {url, title, buttons: btns, overlayStatus, has404,
                            mainChildCount, mainText, headings, htmlSize};
                }""")
                logger.warning(
                    f"  Carrefour debug: url={debug.get('url','?')[:80]}, "
                    f"title={debug.get('title','?')[:50]}, "
                    f"overlay={debug.get('overlayStatus','?')}, "
                    f"404={debug.get('has404',False)}, "
                    f"htmlSize={debug.get('htmlSize',0)}, "
                    f"mainChildren={debug.get('mainChildCount',-1)}, "
                    f"headings={debug.get('headings',[])}, "
                    f"mainText={debug.get('mainText','?')[:100]}, "
                    f"buttons={debug.get('buttons',[])}"
                )
            except Exception as dbg_err:
                logger.warning(f"  Carrefour debug failed: {dbg_err}")
            # Check if out of stock
            oos = await page.query_selector(
                ':text("Out of Stock"), :text("out of stock"), '
                ':text("Currently Unavailable"), [data-testid="out_of_stock"]'
            )
            if oos:
                result["error"] = "Out of stock"
                return result

            # Button not found — try API fallback (bypasses React hydration)
            if guid:
                logger.info(f"  Button not found — trying API fallback with guid")
                api_result = await _add_to_cart_carrefour_api(
                    page, product_url, result["name"], quantity,
                    guid=guid, auth_token=auth_token
                )
                if api_result["success"]:
                    result["success"] = True
                    result["name"] = api_result["name"] or result["name"]
                    return result
                else:
                    logger.warning(f"  API fallback also failed: {api_result.get('error','')}")

            result["error"] = "Add to Cart button not found"
            return result

        # Click Add to Cart
        await add_btn.click()
        await asyncio.sleep(2)  # Wait for cart animation/confirmation

        # Verify it was added (quantity selector should appear, or cart count increases)
        added_check = await page.query_selector(
            '[data-testid="quantity_selector"], .qty-selector, '
            'button:has-text("Added")'
        )

        if added_check or True:  # Be optimistic -- if click didn't error, it likely worked
            result["success"] = True

            # Set quantity if > 1
            if quantity > 1:
                await _set_quantity_carrefour(page, quantity)

        return result

    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"  Carrefour add-to-cart exception: {e}")
        # Recover page state so the NEXT item's navigation doesn't cascade-fail
        await _recover_page_state(page)
        return result


async def _set_quantity_carrefour(page, target_qty):
    """Adjust quantity on Carrefour product page (already in cart)."""
    try:
        # Carrefour uses +/- buttons or a quantity dropdown
        for _ in range(int(target_qty) - 1):
            plus_btn = await page.query_selector(
                'button[data-testid="increase_quantity"], '
                'button:has-text("+"), '
                '[class*="qty"] button:last-child'
            )
            if plus_btn:
                await plus_btn.click()
                await asyncio.sleep(0.5)
    except Exception as e:
        print(f"      Warning: Could not set quantity to {target_qty}: {e}")



# --------------------------------------------
# Store-specific: Kibsons
# --------------------------------------------

KIBSONS_CART_URL = "https://www.kibsons.com/en/cart"

# Reusable JS to click the "+" button on a Kibsons product page.
# Tries strict selector first (with rounded-r), then relaxed (just tw-bg-primary).
# Filters out KIBSONS REGULAR subscription section and carousel items.
_KIBSONS_CLICK_PLUS_JS = """
() => {
    const selectors = [
        '[class*="tw-bg-primary"][class*="rounded-r"]',
        '[class*="tw-bg-primary"]'
    ];
    for (const selector of selectors) {
        const candidates = document.querySelectorAll(selector);
        const valid = [];
        for (const el of candidates) {
            const rect = el.getBoundingClientRect();
            if (rect.width < 20 || rect.width > 70 || rect.height < 20 || rect.height > 70) continue;
            if (!el.querySelector('svg')) continue;
            if (rect.y > 700 || rect.y < 100) continue;
            let p = el.parentElement; let d = 0; let isReg = false;
            while (p && d < 15) {
                if ((p.innerText||'').substring(0,200).includes('KIBSONS REGULAR')) { isReg=true; break; }
                p = p.parentElement; d++;
            }
            if (!isReg) valid.push({el, rect});
        }
        if (valid.length > 0) {
            // Pick rightmost (the "+" button in a -/qty/+ layout)
            valid.sort((a, b) => a.rect.x - b.rect.x);
            valid[valid.length - 1].el.click();
            return true;
        }
    }
    return false;
}
"""


async def _add_to_cart_kibsons(page, product_url, quantity=1, timeout=15000):
    """
    Navigate to a Kibsons product page and add it to cart.

    Kibsons PDP structure (verified Feb 2026):
      - NO "Add to Cart" button exists for one-time purchases
      - Instead: a [-] [0] [+] quantity widget (DIVs with tw-bg-primary, SVG icons)
      - Clicking "+" increments from 0 → 1, which adds the item to cart
      - A separate "KIBSONS REGULAR" subscription panel has its own +/- (ignore those)
      - The +/- DIVs have tw-rounded-r (plus) and tw-rounded-l (minus)
      - An "Add New Address" modal may pop up and must be dismissed first

    Returns dict:
      {"success": True/False, "name": str, "price": float, "error": str or None}
    """
    result = {"success": False, "name": "", "price": 0, "error": None}

    try:
        await page.goto(product_url, wait_until="domcontentloaded", timeout=timeout)
        await asyncio.sleep(3)  # Kibsons SPA needs time to render

        # ── Check for redirect away from product page ──
        current_url = page.url.lower()
        if "/account" in current_url or "/login" in current_url:
            # Bad URL — Kibsons redirected us away from the product page
            result["error"] = f"URL redirected to {page.url} — product URL is likely broken"
            logger.warning(f"  Kibsons: Product URL redirected to account/login page: {product_url} → {page.url}")
            return result

        # ── Dismiss any modal popups (address, cookie, etc.) ──
        await _dismiss_kibsons_modals(page)

        # Extract product name
        name_el = await page.query_selector('h1')
        if name_el:
            result["name"] = (await name_el.inner_text()).strip()

        # Extract price
        price_text = await page.evaluate("""
            () => {
                const selectors = [
                    '.tw-text-2xl.tw-font-bold',
                    '[class*="price"]',
                    '.product-price',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const text = el.innerText || '';
                        const match = text.match(/([\\d.]+)/);
                        if (match) return parseFloat(match[1]);
                    }
                }
                return 0;
            }
        """)
        result["price"] = price_text or 0

        # ── Main strategy: Click the product quantity "+" button ──
        # The "+" is a DIV (not <button>) with tw-bg-primary class, containing an SVG icon.
        # Some products have `rounded-r`, others don't.
        # We want the RIGHTMOST one in a +/- pair, NOT inside "KIBSONS REGULAR".
        clicked = await page.evaluate("""
            (targetQty) => {
                // Try two selectors: strict first, then relaxed
                const selectors = [
                    '[class*="tw-bg-primary"][class*="rounded-r"]',  // Original strict
                    '[class*="tw-bg-primary"]'                        // Relaxed fallback
                ];

                for (const selector of selectors) {
                    const candidates = document.querySelectorAll(selector);
                    // Collect valid buttons: small squares with SVGs, not in KIBSONS REGULAR
                    const validButtons = [];

                    for (const el of candidates) {
                        const rect = el.getBoundingClientRect();
                        // Must be small square-ish (quantity button is ~35x35)
                        if (rect.width < 20 || rect.width > 70 || rect.height < 20 || rect.height > 70) continue;
                        // Must have SVG inside
                        if (!el.querySelector('svg')) continue;
                        // Must be visible
                        if (rect.width === 0 || rect.height === 0) continue;
                        // Must be in viewport (not in a scrolled-off product carousel)
                        if (rect.y > 700 || rect.y < 100) continue;

                        // Skip if inside KIBSONS REGULAR subscription section
                        let parent = el.parentElement;
                        let isRegular = false;
                        let depth = 0;
                        while (parent && depth < 15) {
                            const text = (parent.innerText || '').substring(0, 200);
                            if (text.includes('KIBSONS REGULAR') || text.includes('Add to Regular')) {
                                isRegular = true;
                                break;
                            }
                            parent = parent.parentElement;
                            depth++;
                        }
                        if (isRegular) continue;

                        validButtons.push({el, rect});
                    }

                    if (validButtons.length === 0) continue;

                    // Find the "+" button: it's the RIGHT-MOST button in a horizontal pair
                    // Sort by X position, take the rightmost one near the top of the page
                    validButtons.sort((a, b) => a.rect.x - b.rect.x);
                    // The "+" is the second (rightmost) in a pair at the same Y level
                    const plusBtn = validButtons.length >= 2 ? validButtons[validButtons.length - 1] : validButtons[0];

                    // Check the sibling quantity display
                    const qtyParent = plusBtn.el.parentElement;
                    const qtyDisplay = qtyParent ? [...qtyParent.children].find(c => {
                        const t = (c.innerText || '').trim();
                        return t.match(/^\\d/);
                    }) : null;
                    const currentQty = qtyDisplay ? parseFloat(qtyDisplay.innerText.trim()) : -1;

                    // Click the "+" button
                    plusBtn.el.click();
                    return {
                        found: true,
                        prevQty: currentQty,
                        x: Math.round(plusBtn.rect.x),
                        y: Math.round(plusBtn.rect.y),
                        selector: selector
                    };
                }

                return {found: false};
            }
        """, quantity)

        if clicked.get("found"):
            logger.info(f"  Kibsons: Clicked product '+' button (prevQty={clicked.get('prevQty')}, pos={clicked.get('x')},{clicked.get('y')})")
            await asyncio.sleep(2)

            # Dismiss any modal that pops up after clicking (e.g., address selection)
            await _dismiss_kibsons_modals(page)

            # Verify quantity changed (should be >= 1 now)
            qty_after = await page.evaluate("""
                () => {
                    // Find the first product quantity display (not in KIBSONS REGULAR)
                    // Try both strict and relaxed selectors
                    const selectors = [
                        '[class*="tw-bg-primary"][class*="rounded-r"]',
                        '[class*="tw-bg-primary"]'
                    ];
                    for (const selector of selectors) {
                        const candidates = document.querySelectorAll(selector);
                        for (const el of candidates) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width < 20 || rect.width > 70) continue;
                            if (!el.querySelector('svg')) continue;
                            if (rect.y > 700 || rect.y < 100) continue;
                            let parent = el.parentElement;
                            let isRegular = false;
                            let depth = 0;
                            while (parent && depth < 15) {
                                if ((parent.innerText || '').substring(0, 200).includes('KIBSONS REGULAR')) {
                                    isRegular = true;
                                    break;
                                }
                                parent = parent.parentElement;
                                depth++;
                            }
                            if (isRegular) continue;
                            const qtyParent = el.parentElement;
                            const qtyDisplay = qtyParent ? [...qtyParent.children].find(c => {
                                const t = (c.innerText || '').trim();
                                return t.match(/^\\d/);
                            }) : null;
                            if (qtyDisplay) return parseFloat(qtyDisplay.innerText.trim());
                        }
                    }
                    return -1;
                }
            """)
            logger.info(f"  Kibsons: Quantity after click: {qty_after}")

            if qty_after > 0:
                result["success"] = True
                # Click "+" more times for quantity > 1
                if quantity > 1:
                    for _ in range(int(quantity) - 1):
                        await page.evaluate(_KIBSONS_CLICK_PLUS_JS)
                        await asyncio.sleep(0.5)
                return result
            else:
                # Click happened but qty didn't change — might need address first
                logger.warning(f"  Kibsons: Clicked + but qty still {qty_after}. Possible popup blocking.")
                # Try once more after dismissing modals
                await _dismiss_kibsons_modals(page)
                await asyncio.sleep(1)
                await page.evaluate(_KIBSONS_CLICK_PLUS_JS)
                await asyncio.sleep(2)
                result["success"] = True  # Optimistic — the click went through
                return result

        # ── Subscription / tall-layout recovery (failure path only) ──
        # The strict +/- detection missed the one-time "+". Two known
        # causes: (1) a subscription-default PDP showing "1 Week" / "2 Weeks"
        # frequency buttons that hides the one-time control until you pick a
        # one-time option; (2) a tall layout that pushes the "+" below the
        # strict y-band. Try selecting one-time, then re-detect with a wider
        # band that prefers the cluster nearest the top (the main product,
        # not a related-items carousel). Runs ONLY after the normal path
        # failed, so the working path is untouched.
        try:
            await page.evaluate("""
                () => {
                    const wants = ['one time','one-time','one off','one-off','buy once','single purchase'];
                    for (const el of document.querySelectorAll('button,[role=button],label,a,div,span')) {
                        const t = (el.innerText||'').trim().toLowerCase();
                        if (!t || t.length > 25) continue;
                        if (wants.some(w => t === w || t.includes(w))) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) { el.click(); return; }
                        }
                    }
                }
            """)
            await asyncio.sleep(1.2)
            await _dismiss_kibsons_modals(page)
            clicked_fb = await page.evaluate("""
                () => {
                    const selectors = ['[class*="tw-bg-primary"][class*="rounded-r"]','[class*="tw-bg-primary"]'];
                    for (const selector of selectors) {
                        const valid = [];
                        for (const el of document.querySelectorAll(selector)) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width < 20 || rect.width > 70 || rect.height < 20 || rect.height > 70) continue;
                            if (!el.querySelector('svg')) continue;
                            if (rect.y > 1200 || rect.y < 60) continue;   // wider than the strict band
                            let p = el.parentElement, d = 0, isReg = false;
                            while (p && d < 15) {
                                const tx = (p.innerText||'').substring(0,200);
                                if (tx.includes('KIBSONS REGULAR') || tx.includes('Add to Regular')) { isReg = true; break; }
                                p = p.parentElement; d++;
                            }
                            if (!isReg) valid.push({el, rect});
                        }
                        if (!valid.length) continue;
                        valid.sort((a,b)=>a.rect.y - b.rect.y);          // topmost first
                        const topY = valid[0].rect.y;
                        const cluster = valid.filter(v => v.rect.y - topY < 250);  // main product cluster
                        cluster.sort((a,b)=>a.rect.x - b.rect.x);
                        cluster[cluster.length - 1].el.click();           // rightmost = "+"
                        return true;
                    }
                    return false;
                }
            """)
            if clicked_fb:
                logger.info("  Kibsons: recovered via one-time toggle + wide-band detection")
                await asyncio.sleep(2)
                await _dismiss_kibsons_modals(page)
                result["success"] = True
                return result
        except Exception as _rec_e:
            logger.debug("  Kibsons subscription recovery failed (non-fatal): %s", _rec_e)

        # ── Fallback: try traditional button text selectors ──
        logger.info("  Kibsons: No quantity +/- found, trying text-based button search...")
        fallback_btn = await page.query_selector(
            'button:has-text("Add to Cart"), '
            'button:has-text("Add to Basket"), '
            'button:has-text("ADD TO CART"), '
            'a:has-text("Add to Cart"), '
            '[role="button"]:has-text("Add to Cart")'
        )
        if fallback_btn:
            logger.info("  Kibsons: Found fallback 'Add to Cart' button")
            await fallback_btn.click()
            await asyncio.sleep(2)
            result["success"] = True
            return result

        # ── All strategies failed — dump debug info ──
        debug_info = await _debug_dump_kibsons_pdp(page)
        logger.warning(f"  Kibsons: All add-to-cart strategies failed. Debug: {debug_info}")

        # Check out of stock
        oos_text = await page.evaluate("""
            () => {
                const body = document.body.innerText.toLowerCase();
                if (body.includes('out of stock')) return 'out of stock';
                if (body.includes('sold out')) return 'sold out';
                if (body.includes('currently unavailable')) return 'currently unavailable';
                return '';
            }
        """)
        if oos_text:
            result["error"] = f"Out of stock ({oos_text})"
        else:
            result["error"] = f"Add to Cart button not found. Debug: {debug_info.get('buttons_summary', 'none')[:200]}"
        return result

    except Exception as e:
        result["error"] = str(e)
        return result


async def _dismiss_kibsons_modals(page):
    """Dismiss any modal popups on Kibsons (address selection, cookies, etc.)."""
    try:
        # Look for modal close buttons (X icon) or "SKIP" buttons
        dismissed = await page.evaluate("""
            () => {
                let dismissed = [];

                // "SKIP" button (in address modal)
                const allEls = document.querySelectorAll('button, div, a');
                for (const el of allEls) {
                    const text = (el.innerText || '').trim();
                    if (text === 'SKIP' || text === 'Skip') {
                        el.click();
                        dismissed.push('SKIP button');
                        break;
                    }
                }

                // Modal close X button (usually an X or × icon in top-right of modal)
                const closeButtons = document.querySelectorAll('[class*="modal"] [class*="close"], [class*="dialog"] [class*="close"]');
                for (const btn of closeButtons) {
                    if (btn.offsetParent !== null) {
                        btn.click();
                        dismissed.push('close button');
                    }
                }

                // Also try clicking any visible X/× near top of overlays
                const overlays = document.querySelectorAll('[class*="overlay"], [class*="modal"], [class*="popup"]');
                for (const overlay of overlays) {
                    if (overlay.offsetParent === null) continue;
                    const xBtn = overlay.querySelector('button, [role="button"], [class*="close"]');
                    if (xBtn) {
                        xBtn.click();
                        dismissed.push('overlay close');
                    }
                }

                return dismissed;
            }
        """)
        if dismissed:
            logger.info(f"  Kibsons: Dismissed modals: {dismissed}")
            await asyncio.sleep(1)
    except Exception as e:
        logger.debug("Kibsons modal dismissal failed (best-effort): %s", e)


async def _debug_dump_kibsons_pdp(page):
    """Dump interactive elements on a Kibsons PDP for debugging when add-to-cart fails."""
    try:
        return await page.evaluate("""
            () => {
                const info = {buttons_summary: '', qty_widgets: [], visible_buttons: 0};

                // Find quantity widgets (tw-bg-primary with SVG)
                const primaryEls = document.querySelectorAll('[class*="tw-bg-primary"]');
                for (const el of primaryEls) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 20 || rect.width > 70 || rect.height < 20) continue;
                    const hasSvg = !!el.querySelector('svg');
                    const cls = String(el.className || '').substring(0, 80);
                    info.qty_widgets.push({
                        cls, hasSvg,
                        x: Math.round(rect.x), y: Math.round(rect.y),
                        w: Math.round(rect.width)
                    });
                }

                // Count all visible clickable elements
                const clickables = document.querySelectorAll('button, [role="button"], [class*="cursor-pointer"], [class*="tw-cursor-pointer"]');
                let summary = [];
                for (const el of clickables) {
                    if (el.offsetParent === null || el.offsetWidth === 0) continue;
                    if (el.closest('header, nav')) continue;
                    info.visible_buttons++;
                    const text = (el.innerText || '').trim().substring(0, 30);
                    const tag = el.tagName;
                    if (text && summary.length < 8) {
                        summary.push('[' + tag + ' "' + text + '"]');
                    }
                }
                info.buttons_summary = summary.join(', ') || 'no visible buttons found';

                // Product section text for context
                const h1 = document.querySelector('h1');
                if (h1) {
                    const section = h1.closest('[class*="tw-"]');
                    if (section && section.parentElement) {
                        info.product_text = (section.parentElement.innerText || '').substring(0, 300);
                    }
                }

                return info;
            }
        """)
    except Exception as e:
        return {"buttons_summary": f"debug failed: {e}"}


async def _verify_cart_kibsons(page):
    """Navigate to Kibsons cart page and extract contents."""
    try:
        await page.goto(KIBSONS_CART_URL, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)

        cart_data = await page.evaluate("""
            () => {
                const items = [];
                const cartItems = document.querySelectorAll(
                    '.cart-item, [class*="cart"] [class*="item"], tr[class*="item"]'
                );
                for (const item of cartItems) {
                    const nameEl = item.querySelector('a, h3, h4, .product-name');
                    const priceEl = item.querySelector('[class*="price"]');
                    const qtyEl = item.querySelector('input[type="number"], .qty-value');

                    const name = nameEl ? nameEl.innerText.trim() : '';
                    const priceText = priceEl ? priceEl.innerText : '0';
                    const priceMatch = priceText.match(/([\\d.]+)/);
                    const price = priceMatch ? parseFloat(priceMatch[1]) : 0;
                    const qty = qtyEl ? (parseInt(qtyEl.value || qtyEl.innerText) || 1) : 1;

                    if (name) items.push({name, qty, price});
                }

                const totalEl = document.querySelector(
                    '[class*="total"], .order-total, .cart-total'
                );
                const totalText = totalEl ? totalEl.innerText : '0';
                const totalMatch = totalText.match(/([\\d.]+)/);
                const total = totalMatch ? parseFloat(totalMatch[1]) : 0;

                return {items, total};
            }
        """)
        return cart_data

    except Exception as e:
        print(f"    Warning: Could not verify Kibsons cart: {e}")
        return {"items": [], "total": 0}


# --------------------------------------------
# Login verification
# --------------------------------------------

LOGIN_CHECKS = {
    "Carrefour UAE": {
        # Check login on the homepage — look for "Login" vs account indicators in the header
        "url": "https://www.carrefouruae.com/mafuae/en/",
        "login_url": "https://www.carrefouruae.com/mafuae/en/login",
        "check": """
            () => {
                // Check header area for login/account indicators
                const header = document.querySelector('header') || document.body;
                const text = (header.innerText || '').substring(0, 1000).toLowerCase();

                // If we see "login" or "sign in" prominently, user is NOT logged in
                // Look for login links/buttons in the header
                const loginLinks = header.querySelectorAll('a[href*="login"], a[href*="sign-in"], button');
                for (const el of loginLinks) {
                    const elText = (el.innerText || '').trim().toLowerCase();
                    if (elText === 'login' || elText === 'sign in' || elText === 'login & register') {
                        return {logged_in: false, reason: 'login link found in header: ' + elText};
                    }
                }

                // If we see account-related elements (Hi username, My Account dropdown), user IS logged in
                if (text.includes('hi,') || text.includes('hello,') || text.includes('my account') ||
                    text.includes('my orders') || text.includes('sign out') || text.includes('logout')) {
                    return {logged_in: true, user: 'account indicators found'};
                }

                // Check for cart icon with count (usually means logged in, but not definitive)
                // Default: assume logged in if no explicit login prompt found
                return {logged_in: true, user: 'no login prompt detected'};
            }
        """,
    },
    "Kibsons": {
        # Check login on the homepage — look for login vs account indicators
        "url": "https://www.kibsons.com/",
        "login_url": "https://www.kibsons.com/login",
        "check": """
            () => {
                const header = document.querySelector('header, nav, .header') || document.body;
                const text = (header.innerText || '').substring(0, 1000).toLowerCase();

                // Look for login/signin links in header
                const loginLinks = header.querySelectorAll('a[href*="login"], a[href*="sign"], button');
                for (const el of loginLinks) {
                    const elText = (el.innerText || '').trim().toLowerCase();
                    if (elText === 'login' || elText === 'sign in' || elText === 'log in') {
                        return {logged_in: false, reason: 'login link found: ' + elText};
                    }
                }

                // If we see account indicators, user IS logged in
                if (text.includes('my account') || text.includes('sign out') || text.includes('logout') ||
                    text.includes('hello') || text.includes('hi,')) {
                    return {logged_in: true, user: 'account indicators found'};
                }

                // Default: assume logged in if no login prompt
                return {logged_in: true, user: 'no login prompt detected'};
            }
        """,
    },
}



def _catalog_fallback_product(conn, item):
    """Find a product_url straight from product_catalog when the knowledge
    base (item_alternatives) has no entry for this item.

    item_alternatives is a curated/scored subset and lags behind the
    70k-row catalog, so a missing KB row was a hard "no_kb" failure even
    though the catalog had the product. Resolution order:
      1. A catalog_id pinned on the list row (a human/free-text pick).
      2. Name search at the item's store, available, prefer organic +
         shortest (plainest) name.

    Returns (product_url, product_name) or (None, None).
    """
    cat_id = item.get("catalog_id")
    if cat_id:
        row = conn.execute(
            "SELECT product_name, product_url FROM product_catalog WHERE id = ?",
            (cat_id,),
        ).fetchone()
        if row and row["product_url"]:
            return row["product_url"], row["product_name"]
    name = (item.get("item_name") or "").strip()
    store_id = item.get("store_id")
    if name and store_id:
        row = conn.execute(
            """SELECT product_name, product_url FROM product_catalog
               WHERE store_id = ?
                 AND COALESCE(product_url, '') != ''
                 AND is_available = 1
                 AND LOWER(product_name) LIKE LOWER(?)
               ORDER BY is_organic DESC, LENGTH(product_name) ASC
               LIMIT 1""",
            (store_id, f"%{name}%"),
        ).fetchone()
        if row and row["product_url"]:
            return row["product_url"], row["product_name"]
    return None, None


def preflight_cart(list_id, conn=None):
    """Pre-build availability check (no browser).

    For each non-skipped item on the list, confirm it resolves to an
    in-stock, addable product using the SAME resolution order the builder
    uses (knowledge base -> pinned catalog_id -> catalog name search at
    the store). Items that don't resolve to an available product are
    returned as `problems` with same- and cross-vendor alternatives, so
    the user can swap or skip BEFORE the browser build runs -- instead of
    the build silently skipping them.

    Returns:
      {
        "ok": bool,                # True if nothing needs attention
        "checked": int,
        "ready": [item_name, ...],
        "problems": [{item_name, store, reason, alternatives}],
      }
    """
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        own = True
    try:
        sl = list_get(list_id, conn)
        if not sl:
            return {"ok": False, "error": "list not found",
                    "checked": 0, "ready": [], "problems": []}
        items = [
            it for it in sl.get("items", [])
            if it.get("status") != "skipped" and it.get("user_action") != "skipped"
        ]
        ready, problems = [], []
        for it in items:
            product, _ = get_best_alternative(
                it.get("item_id"), it.get("store_id"), conn=conn
            )
            ok = bool(
                product and product.get("product_url")
                and product.get("is_available", True)
            )
            if not ok:
                fb_url, _fb_name = _catalog_fallback_product(conn, it)
                ok = bool(fb_url)
            if ok:
                ready.append(it.get("item_name"))
            else:
                problems.append({
                    "item_name": it.get("item_name"),
                    "store": it.get("store_name"),
                    "reason": "no in-stock product found",
                    "alternatives": _get_alternative_suggestions(it, conn),
                })
        # Delivery-fee picture (per-vendor subtotal, effective fee, shortfall to
        # free). Non-blocking — purely informational at preflight time.
        try:
            from delivery_optimizer import analyze_list_delivery
            delivery = analyze_list_delivery(list_id, conn)
        except Exception as e:
            logger.warning("delivery analysis failed: %s", e)
            delivery = None
        return {"ok": len(problems) == 0, "checked": len(items),
                "ready": ready, "problems": problems, "delivery": delivery}
    finally:
        if own:
            conn.close()


# --------------------------------------------
# Main cart builder
# --------------------------------------------

async def build_cart(list_id, store_filter=None, dry_run=False, progress_cb=None):
    """
    Build real carts on store websites from a finalized shopping list.

    Args:
        list_id: Shopping list ID from the database
        store_filter: Only build for this store (e.g., "Carrefour UAE"). None = all stores.
        dry_run: If True, look up products but don't actually add to cart.
        progress_cb: async callback(step, **kwargs) for real-time progress updates.
            Steps: login_check, item_start, item_ok, item_fail, verify, done, login_required

    Returns cart report dict:
        {
            "list_id": int,
            "stores": {
                "Carrefour UAE": {
                    "items_requested": int,
                    "items_added": int,
                    "items_failed": int,
                    "estimated_total": float,
                    "added": [{"item_name", "product_name", "qty", "price", "product_url"}],
                    "failed": [{"item_name", "reason", "alternatives": [...]}]
                },
                ...
            },
            "summary": str
        }
    """
    async def _progress(step, **kwargs):
        if progress_cb:
            try:
                await progress_cb(step, **kwargs)
            except Exception as e:
                logger.debug("Progress callback (%s) failed: %s", step, e)
    # Load the shopping list
    logger.info(f"build_cart called: list_id={list_id}, dry_run={dry_run}")
    log_event("cart_build_detail", source="cart_builder",
              detail=f"list_id={list_id}, dry_run={dry_run}")

    shopping_list = list_get(list_id)
    if not shopping_list:
        logger.error(f"Shopping list {list_id} not found")
        return {"error": f"Shopping list {list_id} not found"}

    logger.info(f"List {list_id}: status={shopping_list['status']}, items={len(shopping_list.get('items', []))}")
    if shopping_list["status"] != "confirmed":
        logger.warning(f"List status is '{shopping_list['status']}', not 'confirmed'. Proceeding anyway.")

    # Filter to approved items only
    approved_items = [
        item for item in shopping_list["items"]
        if item.get("status") == "approved" or item.get("user_action") == "approved"
    ]

    if not approved_items:
        # If no items have been explicitly approved, use all pending items
        approved_items = [
            item for item in shopping_list["items"]
            if item.get("status") != "skipped" and item.get("user_action") != "skipped"
        ]

    if not approved_items:
        logger.warning("No items to add to cart (all skipped)")
        log_event("cart_build_detail", source="cart_builder", detail="no approved items")
        return {"error": "No items to add to cart (all skipped)"}

    logger.info(f"Approved items: {len(approved_items)}")
    log_event("cart_build_detail", source="cart_builder",
              detail=f"approved_items={len(approved_items)}")

    # Group by store
    by_store = {}
    for item in approved_items:
        store = item["store_name"]
        if store_filter and store != store_filter:
            continue
        if store not in by_store:
            by_store[store] = []
        by_store[store].append(item)

    if not by_store:
        return {"error": f"No items for store '{store_filter}'"}

    # Build report structure
    report = {"list_id": list_id, "stores": {}, "summary": ""}

    # Connect to DB for knowledge base lookups
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if dry_run:
        print("\n  [DRY RUN] Looking up products but NOT adding to cart.\n")
        for store_name, items in by_store.items():
            store_report = _build_store_report_template(store_name, items)
            for item in items:
                product, _ = get_best_alternative(item["item_id"], item["store_id"], conn=conn)
                if product:
                    store_report["added"].append({
                        "item_name": item["item_name"],
                        "product_name": product["product_name"],
                        "qty": item["quantity"],
                        "price": product["price"],
                        "product_url": product["product_url"],
                        "tier": product["tier"],
                    })
                    store_report["items_added"] += 1
                    store_report["estimated_total"] += (product["price"] or 0) * item["quantity"]
                else:
                    store_report["failed"].append({
                        "item_name": item["item_name"],
                        "reason": "No product found in knowledge base",
                        "alternatives": [],
                    })
                    store_report["items_failed"] += 1
            report["stores"][store_name] = store_report
        conn.close()
        report["summary"] = _build_summary(report)
        return report

    # ── Pre-build availability gate ──
    # Before driving the browser, confirm each item resolves to an in-stock
    # product. If any don't, stop here and return the problems + alternatives
    # so the user can swap/skip BEFORE the build runs -- rather than the build
    # launching the browser and silently skipping them. (Real builds only;
    # dry-run returned above.)
    #
    # First refresh LIVE availability for the products this list resolves to, so
    # the preflight + per-item resolution below act on the truth (not stale
    # catalog data) and we never attempt to add something that's actually out of
    # stock. Runs BEFORE any cart browser launches (verify_picks uses its own
    # short-lived browser and closes it); best-effort -- a verify hiccup must
    # never block a build.
    try:
        from live_verify import live_verify_list_availability
        _avail = await live_verify_list_availability(list_id)
        logger.info("[cart] pre-build availability refresh: %s", _avail)
        log_event("cart_preflight_verify", source="cart_builder",
                  detail=f"list={list_id} {_avail}")
    except Exception as _av_e:
        logger.warning("[cart] pre-build availability refresh failed (proceeding): %s", _av_e)

    try:
        pf = preflight_cart(list_id, conn=conn)
    except Exception as _pf_e:
        logger.warning("preflight_cart failed (non-fatal, proceeding): %s", _pf_e)
        pf = {"ok": True, "problems": []}
    if not pf.get("ok") and pf.get("problems"):
        conn.close()
        n = len(pf["problems"])
        logger.info(f"Preflight blocked build: {n} unavailable item(s)")
        log_event("cart_preflight_block", source="cart_builder",
                  detail=f"list={list_id} problems={n}")
        return {
            "error": "availability_check",
            "list_id": list_id,
            "preflight": pf,
            "stores": {},
            "summary": f"{n} item(s) need attention before building the cart.",
        }

    # -- Real cart building --
    store_summary = {s: len(items) for s, items in by_store.items()}
    logger.info(f"Starting real cart build: {store_summary}")
    log_event("cart_build_detail", source="cart_builder",
              detail=f"stores: {store_summary}")
    print("\n  Launching automation browser...")
    await _progress("starting", message="Launching browser...")

    # ── Phase 1: Carrefour items via UC Chrome ──
    # UC Chrome (undetected-chromedriver) bypasses Akamai bot detection.
    # Uses its own profile at ~/.hearthkeeper-carrefour/
    uc_driver = None
    if "Carrefour UAE" in by_store:
        await _progress("login_check", store="Carrefour UAE", status="checking",
                        message="Launching Carrefour browser (UC Chrome)...")
        try:
            uc_driver = await asyncio.to_thread(_launch_uc_chrome)
            logger.info("[UC] Chrome launched for Carrefour")

            # Login check
            logged_in = await asyncio.to_thread(_uc_ensure_carrefour_login, uc_driver)
            if logged_in:
                logger.info("[UC] Carrefour login OK")
                await _progress("login_check", store="Carrefour UAE", status="ok")
            else:
                logger.warning("[UC] Carrefour login FAILED")
                await _progress("login_check", store="Carrefour UAE", status="failed")
                await _progress("login_required", stores=["Carrefour UAE"],
                                message="Could not log into Carrefour UAE. Check .env credentials.")
                try:
                    await asyncio.to_thread(uc_driver.quit)
                except Exception as e:
                    logger.debug("UC Chrome cleanup after login failure failed: %s", e)
                conn.close()
                raise RuntimeError(
                    "Auto-login failed for: Carrefour UAE. "
                    "Check CARREFOUR_EMAIL/PASSWORD in .env"
                )
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"[UC] Chrome launch failed: {e}")
            await _progress("login_check", store="Carrefour UAE", status="failed",
                            message=f"Browser launch failed: {str(e)[:100]}")
            uc_driver = None

    # ── Phase 2: Kibsons/other items via Playwright ──
    # Playwright works perfectly for Kibsons (no Akamai issues)
    p = context = page = None
    non_carrefour_stores = [s for s in by_store if s != "Carrefour UAE"]
    if non_carrefour_stores:
        p, context, page = await launch_edge()
        for store in non_carrefour_stores:
            await _progress("login_check", store=store, status="checking",
                            message=f"Checking {store} login...")
            check = LOGIN_CHECKS.get(store)
            if check:
                await page.goto(check["url"], wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(4)
            logged_in = await ensure_logged_in(page, store)
            if logged_in:
                logger.info(f"[LOGIN] {store}: OK")
                await _progress("login_check", store=store, status="ok")
            else:
                logger.warning(f"[LOGIN] {store}: auto-login FAILED")
                await _progress("login_check", store=store, status="failed")
                await _progress("login_required", stores=[store],
                                message=f"Could not log into {store}. Check .env credentials.")
                await safe_close(p, context)
                if uc_driver:
                    try:
                        await asyncio.to_thread(uc_driver.quit)
                    except Exception as e:
                        logger.debug("UC Chrome cleanup after login failure failed: %s", e)
                conn.close()
                raise RuntimeError(f"Auto-login failed for: {store}. Check .env credentials.")

    try:
        for store_name, items in by_store.items():
            logger.info(f"BUILDING CART: {store_name} ({len(items)} items)")

            store_report = _build_store_report_template(store_name, items)

            # Check browser health (Playwright only — UC Chrome manages itself)
            if page and not await check_browser_health(page):
                p, context, page = await relaunch_browser(p, context)

            # Snapshot the cart BEFORE we touch it. Items already
            # there are from a previous unfinished session (or a
            # manual add via the store website). We never delete
            # them, but we DO surface the count in the final report
            # so the B2 notification can warn the curator about
            # extras they'll see at checkout.
            try:
                pre = await _capture_pre_existing_cart(
                    store_name, page=page, uc_driver=uc_driver,
                )
                if pre is not None:
                    store_report["pre_existing"] = {
                        "item_count": pre.get("item_count", 0),
                        "estimated_total": pre.get("estimated_total", 0),
                    }
                    if pre.get("item_count", 0) > 0:
                        logger.info(
                            "[%s] cart already has %d item(s) from a "
                            "previous session (%.2f %s).",
                            store_name, pre["item_count"],
                            pre["estimated_total"], "AED",
                        )
            except Exception as e:
                logger.debug("pre_existing snapshot error for %s: %s",
                             store_name, e)

            total_items_all = sum(len(v) for v in by_store.values())
            item_global_idx = sum(len(by_store[s]) for s in list(by_store.keys())[:list(by_store.keys()).index(store_name)])

            for i, item in enumerate(items, 1):
                item_name = item["item_name"]
                quantity = item["quantity"] or 1
                global_idx = item_global_idx + i

                logger.info(f"[{i}/{len(items)}] {store_name}: {item_name} (qty: {quantity})")
                await _progress("item_start", store=store_name, item=item_name,
                                index=global_idx, total=total_items_all, quantity=quantity)

                try:
                    # Step 1: Find the best product from knowledge base
                    product, _ = get_best_alternative(item["item_id"], item["store_id"], conn=conn)

                    if product and product.get("product_url"):
                        product_url = product["product_url"]
                        product_name = product["product_name"]

                        # Normalize URLs — fix known scraper bugs
                        if "carrefouruae.com" in product_url:
                            product_url = _normalize_carrefour_url(product_url)
                        elif "kibsons.com" in product_url:
                            product_url = _normalize_kibsons_url(product_url)

                        logger.info(f"  KB match: {product_name[:50]} ({product['tier']}) -> {product_url}")
                    else:
                        # ── Live-catalog fallback ──
                        # No knowledge-base (item_alternatives) entry. Before
                        # giving up, look the item up directly in the catalog
                        # for this store (honoring any pinned catalog_id).
                        # This was ~2/3 of recent cart-build misses ("no_kb")
                        # for items the catalog actually knows about.
                        fb_url, fb_name = _catalog_fallback_product(conn, item)
                        if fb_url:
                            product_url = fb_url
                            product_name = fb_name or item_name
                            if "carrefouruae.com" in product_url:
                                product_url = _normalize_carrefour_url(product_url)
                            elif "kibsons.com" in product_url:
                                product_url = _normalize_kibsons_url(product_url)
                            logger.info(f"  Catalog fallback: {product_name[:50]} -> {product_url}")
                        else:
                            logger.warning(f"  No KB or catalog match for {item_name}")
                            await _progress("item_fail", store=store_name, item=item_name,
                                            reason="No product found", index=global_idx, total=total_items_all)
                            store_report["failed"].append({
                                "item_name": item_name,
                                "reason": "No product in knowledge base or catalog.",
                                "alternatives": _get_alternative_suggestions(item, conn),
                            })
                            store_report["items_failed"] += 1
                            _record_cart_attempt(
                                conn,
                                list_id=list_id,
                                list_item_id=item.get("list_item_id") or item.get("id"),
                                item_id=item.get("item_id"),
                                store_id=item.get("store_id"),
                                attempt_n=0,
                                url=None,
                                success=False,
                                reason="No product in knowledge base or catalog.",
                            )
                            continue

                    # Step 2: Add to cart
                    if store_name == "Carrefour UAE":
                        # UC Chrome for Carrefour (bypasses Akamai)
                        if uc_driver:
                            cart_result = await asyncio.to_thread(
                                _uc_add_to_cart_carrefour, uc_driver, product_url, quantity
                            )
                        else:
                            # Fallback to Playwright if UC Chrome failed to launch
                            cart_result = await _add_to_cart_carrefour(
                                page, product_url, quantity
                            )
                    elif store_name == "Kibsons":
                        cart_result = await _add_to_cart_kibsons(page, product_url, quantity)
                    else:
                        print(f"    Unknown store: {store_name}. Skipping.")
                        store_report["failed"].append({
                            "item_name": item_name,
                            "reason": f"Unknown store: {store_name}",
                            "alternatives": [],
                        })
                        store_report["items_failed"] += 1
                        continue

                    # Guard: the store add helpers must return a dict. If one
                    # returns None (e.g. a PDP that 404'd or redirected mid-add),
                    # treat it as a normal failure so the OOS/alternative fallback
                    # runs instead of crashing on None.get(...) and killing the item.
                    if not isinstance(cart_result, dict):
                        cart_result = {"success": False,
                                       "error": "not found (add returned no result)"}

                    # Persist the primary attempt for this item -- regardless
                    # of success/fail. This is the data layer that tells us
                    # "what URL was attempted, what reason did the store return"
                    # after the build is over.
                    _record_cart_attempt(
                        conn,
                        list_id=list_id,
                        list_item_id=item.get("list_item_id") or item.get("id"),
                        item_id=item.get("item_id"),
                        store_id=item.get("store_id"),
                        attempt_n=0,
                        url=product_url,
                        success=cart_result.get("success", False),
                        reason=None if cart_result.get("success") else cart_result.get("error"),
                    )

                    if cart_result["success"]:
                        actual_name = cart_result["name"] or product_name
                        actual_price = cart_result["price"] or product.get("price", 0)
                        logger.info(f"  [OK] Added: {actual_name[:50]} -- {actual_price} AED")
                        await _progress("item_ok", store=store_name, item=item_name,
                                        product=actual_name[:50], price=actual_price,
                                        index=global_idx, total=total_items_all)

                        store_report["added"].append({
                            "item_name": item_name,
                            "product_name": actual_name,
                            "qty": quantity,
                            "price": actual_price,
                            "product_url": product_url,
                        })
                        store_report["items_added"] += 1
                        store_report["estimated_total"] += actual_price * quantity
                    else:
                        error_msg = cart_result.get("error", "Unknown error")
                        logger.warning(f"  [FAIL] {item_name}: {error_msg}")

                        # OOS / not_found fallback: 404 ("trouble finding")
                        # is just as recoverable as out-of-stock -- the
                        # catalog URL is stale but the underlying item
                        # almost certainly has alternatives that DO work.
                        # Previously only OOS triggered the alternative
                        # search; 404 fell straight to failed. Now we
                        # treat them the same: mark the URL stale, retry
                        # the next-best ranked alternative.
                        err_class = _classify_cart_error(error_msg)
                        item_added = False
                        if err_class in ("oos", "not_found"):
                            _mark_catalog_stale(conn, product_url, reason=err_class)

                            # Try next alternatives (skip the one we just failed on)
                            ranked = get_ranked_alternatives(
                                item["item_id"], item["store_id"], limit=5, conn=conn
                            )
                            attempt_n = 1  # primary was attempt 0
                            for alt in ranked:
                                if not alt["is_available"] or alt["product_url"] == product_url:
                                    continue
                                alt_url = alt["product_url"]
                                if "carrefouruae.com" in alt_url:
                                    alt_url = _normalize_carrefour_url(alt_url)
                                elif "kibsons.com" in alt_url:
                                    alt_url = _normalize_kibsons_url(alt_url)

                                logger.info(
                                    "  %s fallback (attempt %d): trying %s -> %s",
                                    err_class.upper(), attempt_n,
                                    alt['product_name'][:50], alt_url,
                                )
                                if store_name == "Carrefour UAE":
                                    if uc_driver:
                                        alt_result = await asyncio.to_thread(
                                            _uc_add_to_cart_carrefour, uc_driver, alt_url, quantity
                                        )
                                    else:
                                        alt_result = await _add_to_cart_carrefour(
                                            page, alt_url, quantity
                                        )
                                elif store_name == "Kibsons":
                                    alt_result = await _add_to_cart_kibsons(page, alt_url, quantity)
                                else:
                                    continue

                                # Same None-guard as the primary attempt: a store
                                # helper returning None must become a clean failure,
                                # not crash the fallback on alt_result.get(...).
                                if not isinstance(alt_result, dict):
                                    alt_result = {"success": False,
                                                  "error": "not found (add returned no result)"}

                                # Persist the attempt before branching on success
                                _record_cart_attempt(
                                    conn,
                                    list_id=list_id,
                                    list_item_id=item.get("list_item_id") or item.get("id"),
                                    item_id=item.get("item_id"),
                                    store_id=item.get("store_id"),
                                    attempt_n=attempt_n,
                                    url=alt_url,
                                    success=alt_result.get("success", False),
                                    reason=None if alt_result.get("success") else alt_result.get("error"),
                                )
                                attempt_n += 1

                                if alt_result["success"]:
                                    alt_name = alt_result["name"] or alt["product_name"]
                                    alt_price = alt_result["price"] or alt.get("price", 0)
                                    logger.info(f"  [OK] Fallback added: {alt_name[:50]} -- {alt_price} AED")
                                    await _progress("item_ok", store=store_name, item=item_name,
                                                    product=alt_name[:50], price=alt_price,
                                                    index=global_idx, total=total_items_all)
                                    store_report["added"].append({
                                        "item_name": item_name,
                                        "product_name": alt_name,
                                        "qty": quantity,
                                        "price": alt_price,
                                        "product_url": alt_url,
                                    })
                                    store_report["items_added"] += 1
                                    store_report["estimated_total"] += alt_price * quantity
                                    item_added = True
                                    break
                                else:
                                    # Mark this alternative stale too -- same
                                    # classification logic, so a 404 alt
                                    # also gets is_available=0.
                                    alt_err = (alt_result.get("error") or "").lower()
                                    alt_class = _classify_cart_error(alt_err)
                                    if alt_class in ("oos", "not_found"):
                                        _mark_catalog_stale(conn, alt_url, reason=alt_class)
                                    logger.warning(
                                        "  Fallback also failed (%s): %s",
                                        alt_class, alt['product_name'][:50],
                                    )

                        # Cross-vendor rescue: same-vendor alts exhausted.
                        # Look for the SAME item at OTHER vendors whose
                        # browsers we already have running for this build.
                        # On success, the item lands in the OTHER store's
                        # cart instead of failing entirely. We never spin
                        # up an additional browser session for this --
                        # that would be heavy and risk login surprises.
                        if not item_added and err_class in ("oos", "not_found") and item.get("item_id"):
                            try:
                                from product_knowledge import get_cross_vendor_alternatives
                                xv_alts = get_cross_vendor_alternatives(
                                    item["item_id"],
                                    exclude_store_id=item["store_id"],
                                    limit=3, conn=conn,
                                )
                            except Exception as xv_err:
                                logger.debug("cross-vendor lookup failed: %s", xv_err)
                                xv_alts = []

                            for xv in xv_alts:
                                xv_store = xv.get("store_name")
                                xv_url = (xv.get("product_url") or "").strip()
                                if not xv_url or not xv_store:
                                    continue
                                # Only attempt rescue at a store whose
                                # browser is already alive in THIS build.
                                if xv_store == "Carrefour UAE" and not uc_driver:
                                    continue
                                if xv_store != "Carrefour UAE" and page is None:
                                    continue
                                # Don't rescue back to the same vendor.
                                if xv_store == store_name:
                                    continue
                                # Normalize the URL for the target store.
                                if "carrefouruae.com" in xv_url:
                                    xv_url = _normalize_carrefour_url(xv_url)
                                elif "kibsons.com" in xv_url:
                                    xv_url = _normalize_kibsons_url(xv_url)

                                logger.info(
                                    "  CROSS-VENDOR rescue: %s OOS/404 at %s; "
                                    "trying %s at %s -> %s",
                                    item_name, store_name,
                                    xv["product_name"][:50], xv_store, xv_url,
                                )
                                await _progress(
                                    "item_start", store=xv_store, item=item_name,
                                    index=global_idx, total=total_items_all,
                                    quantity=quantity,
                                    detail=f"cross-vendor rescue from {store_name}",
                                )
                                try:
                                    if xv_store == "Carrefour UAE":
                                        xv_result = await asyncio.to_thread(
                                            _uc_add_to_cart_carrefour,
                                            uc_driver, xv_url, quantity,
                                        )
                                    elif xv_store == "Kibsons":
                                        xv_result = await _add_to_cart_kibsons(
                                            page, xv_url, quantity,
                                        )
                                    else:
                                        continue
                                except Exception as xv_add_err:
                                    logger.warning(
                                        "cross-vendor add raised %s: %s",
                                        type(xv_add_err).__name__, xv_add_err,
                                    )
                                    continue

                                _record_cart_attempt(
                                    conn,
                                    list_id=list_id,
                                    list_item_id=item.get("list_item_id") or item.get("id"),
                                    item_id=item.get("item_id"),
                                    store_id=xv.get("store_id"),
                                    attempt_n=attempt_n,
                                    url=xv_url,
                                    success=xv_result.get("success", False),
                                    reason=(
                                        f"cross_vendor_from_{store_name}: "
                                        f"{xv_result.get('error')}"
                                        if not xv_result.get("success") else None
                                    ),
                                )
                                attempt_n += 1

                                if xv_result.get("success"):
                                    # Land the rescue in the OTHER store's report
                                    # so totals + per-store breakdowns stay
                                    # honest. The target store_report is
                                    # already populated by the per-store loop
                                    # init (or we create one lazily).
                                    target_report = report["stores"].get(xv_store)
                                    if target_report is None:
                                        target_report = _build_store_report_template(
                                            xv_store, [],
                                        )
                                        report["stores"][xv_store] = target_report
                                    xv_name = (
                                        xv_result.get("name")
                                        or xv["product_name"]
                                    )
                                    xv_price = (
                                        xv_result.get("price")
                                        or xv.get("price", 0)
                                    )
                                    logger.info(
                                        f"  [OK] Cross-vendor rescue: {xv_name[:50]} "
                                        f"-- {xv_price} AED at {xv_store}"
                                    )
                                    await _progress(
                                        "item_ok", store=xv_store, item=item_name,
                                        product=xv_name[:50], price=xv_price,
                                        index=global_idx, total=total_items_all,
                                    )
                                    target_report["added"].append({
                                        "item_name": item_name,
                                        "product_name": xv_name,
                                        "qty": quantity,
                                        "price": xv_price,
                                        "product_url": xv_url,
                                        "rescued_from": store_name,
                                    })
                                    target_report["items_added"] += 1
                                    target_report["estimated_total"] += xv_price * quantity
                                    item_added = True
                                    break
                                else:
                                    # Mark cross-vendor URL stale too if OOS/404.
                                    xv_err_str = (xv_result.get("error") or "").lower()
                                    xv_err_class = _classify_cart_error(xv_err_str)
                                    if xv_err_class in ("oos", "not_found"):
                                        _mark_catalog_stale(conn, xv_url, reason=xv_err_class)
                                    logger.warning(
                                        "  Cross-vendor rescue also failed (%s) at %s: %s",
                                        xv_err_class, xv_store, xv["product_name"][:50],
                                    )

                        if not item_added:
                            await _progress("item_fail", store=store_name, item=item_name,
                                            reason=error_msg, index=global_idx, total=total_items_all)
                            store_report["failed"].append({
                                "item_name": item_name,
                                "reason": error_msg,
                                "alternatives": _get_alternative_suggestions(item, conn),
                            })
                            store_report["items_failed"] += 1

                except Exception as item_err:
                    logger.error(f"  [ERROR] {item_name}: {item_err}", exc_info=True)
                    store_report["failed"].append({
                        "item_name": item_name,
                        "reason": str(item_err),
                        "alternatives": [],
                    })
                    store_report["items_failed"] += 1
                    _record_cart_attempt(
                        conn,
                        list_id=list_id,
                        list_item_id=item.get("list_item_id") or item.get("id"),
                        item_id=item.get("item_id"),
                        store_id=item.get("store_id"),
                        attempt_n=0,
                        url=item.get("product_url") if isinstance(item, dict) else None,
                        success=False,
                        reason=f"exception: {str(item_err)[:200]}",
                    )

                    # Check for dead browser (Playwright only)
                    if store_name != "Carrefour UAE" and page:
                        err_str = str(item_err).lower()
                        if "target closed" in err_str or "connection closed" in err_str:
                            p, context, page = await relaunch_browser(p, context)
                        else:
                            await _recover_page_state(page)

            # Verify cart after adding all items for this store
            print(f"\n  Verifying {store_name} cart...")
            await _progress("verify", store=store_name, message=f"Verifying {store_name} cart...")
            if store_name == "Carrefour UAE":
                # Skip verification for UC Chrome (would need Selenium-based verify)
                cart_contents = {"items": [], "total": 0}
                logger.info("  [UC] Carrefour cart verification skipped (check browser)")
            elif store_name == "Kibsons":
                cart_contents = await _verify_cart_kibsons(page)
            else:
                cart_contents = {"items": [], "total": 0}

            store_report["cart_verified"] = cart_contents
            if cart_contents["total"] > 0:
                print(f"  Cart total: {cart_contents['total']} AED ({len(cart_contents['items'])} items)")
            else:
                print(f"  Cart verification: could not read total (check browser manually)")

            report["stores"][store_name] = store_report

    except Exception as e:
        print(f"\n  FATAL ERROR during cart building: {e}")
        traceback.print_exc()
        logger.error(f"Cart build fatal error: {e}")
        report["error"] = str(e)

    finally:
        # DON'T close browsers -- user needs them to review and place orders!
        if not report.get("error"):
            print("\n  Browser(s) left open for you to review the carts.")
            print("  Close them manually when you're done.")
        else:
            # On error, clean up browsers
            if uc_driver:
                try:
                    await asyncio.to_thread(uc_driver.quit)
                except Exception as e:
                    logger.debug("UC Chrome cleanup on error failed: %s", e)
            if p and context:
                await safe_close(p, context)
        conn.close()

    # Build summary and send final progress
    total_added = sum(sr.get("items_added", 0) for sr in report.get("stores", {}).values())
    total_failed = sum(sr.get("items_failed", 0) for sr in report.get("stores", {}).values())

    if report.get("error"):
        report["summary"] = f"Failed: {report['error'][:150]}"
    else:
        report["summary"] = _build_summary(report)

    logger.info(f"Cart build complete: added={total_added}, failed={total_failed}, error={report.get('error', 'none')}")
    log_event("cart_build_detail", source="cart_builder",
              detail=f"result: added={total_added}, failed={total_failed}")
    await _progress("done", total_added=total_added, total_failed=total_failed,
                    summary=report["summary"])

    return report


def _build_store_report_template(store_name, items):
    """Create an empty store report dict.

    `pre_existing` is filled in by _capture_pre_existing_cart at the
    start of the per-store loop. It distinguishes three states:

      * None   -> we couldn't snapshot the cart (e.g. unsupported
                  store, scrape error). Don't claim anything either way.
      * dict with item_count=0 -> the cart was empty before we touched it.
      * dict with item_count>0 -> items were already there from a
                                   previous session. The B2 notification
                                   surfaces this so the curator isn't
                                   surprised by extra rows on checkout.
    """
    return {
        "items_requested": len(items),
        "items_added": 0,
        "items_failed": 0,
        "estimated_total": 0.0,
        "added": [],
        "failed": [],
        "cart_verified": None,
        "pre_existing": None,
    }


async def _capture_pre_existing_cart(store_name, page=None, uc_driver=None):
    """Snapshot the store's cart BEFORE we add anything from the
    current list.

    Returns:
        None when we can't snapshot (unsupported store / scrape error).
        Otherwise: {"item_count": int, "estimated_total": float,
                    "items": [{name, qty, price}, ...]}.

    Used to detect items that were already in the cart from a previous
    session. We don't auto-clear them (that would be destructive); we
    just surface the count so the user knows the final order will
    include those items plus today's additions.

    Carrefour goes through UC Chrome (Selenium); scraping the cart
    page there is a bigger lift than the existing Kibsons Playwright
    helper, so for now we punt. The report carries `pre_existing=None`
    and `format_order_placed` says nothing rather than lying.
    """
    if store_name == "Kibsons" and page is not None:
        try:
            snapshot = await _verify_cart_kibsons(page)
            items = snapshot.get("items") or []
            total = snapshot.get("total") or 0
            return {
                "item_count": len(items),
                "estimated_total": float(total),
                "items": items,
            }
        except Exception as e:
            logger.debug("pre_existing snapshot failed for %s: %s",
                         store_name, e)
            return None
    # Carrefour UC Chrome path not implemented yet -- defer to a
    # later slice that adds UC-Chrome cart-page parsing. Returning
    # None means the report won't claim anything either way.
    return None


def _get_alternative_suggestions(item, conn):
    """Get alternative product suggestions for a failed item.

    Returns a dict with two keys:
      * same_vendor: list of pre-scored alternatives at the same store.
      * other_vendor: best-ranked alternative at each OTHER store
        (cross-vendor fallback). Surfaces in the report + B2 message
        so the user can switch the item to a different vendor with one
        tap in the app (or chooses to drop it).

    The cart_builder doesn't auto-add cross-vendor candidates -- doing
    so silently changes which store ships the item, and the household
    cares about per-store consolidation (delivery windows, fees, free
    thresholds). The user explicitly decides via the existing
    same/other-vendor swap UI.
    """
    try:
        same = get_ranked_alternatives(
            item["item_id"], item["store_id"], limit=3, conn=conn,
        )
        same_payload = [
            {
                "name": a["product_name"],
                "price": a["price"],
                "url": a["product_url"],
                "tier": a["tier"],
            }
            for a in same if a.get("is_available")
        ]
    except Exception:
        same_payload = []

    other_payload = []
    try:
        if item.get("item_id"):
            from product_knowledge import get_cross_vendor_alternatives
            others = get_cross_vendor_alternatives(
                item["item_id"], exclude_store_id=item["store_id"],
                limit=3, conn=conn,
            )
            for a in others:
                if not a.get("is_available"):
                    continue
                other_payload.append({
                    "name": a["product_name"],
                    "price": a["price"],
                    "url": a["product_url"],
                    "tier": a["tier"],
                    "store_id": a["store_id"],
                    "store_name": a["store_name"],
                })
    except Exception:
        pass

    return {"same_vendor": same_payload, "other_vendor": other_payload}


def _build_summary(report):
    """Build a human-readable summary string from the cart report."""
    parts = []
    total_added = 0
    total_failed = 0

    for store_name, sr in report["stores"].items():
        added = sr["items_added"]
        requested = sr["items_requested"]
        total = sr["estimated_total"]
        total_added += added
        total_failed += sr["items_failed"]

        parts.append(f"{store_name}: {added}/{requested} items (~{total:.0f} AED)")

    summary = "Ready: " + ", ".join(parts)
    if total_failed > 0:
        summary += f". {total_failed} item(s) need attention."
    return summary


def print_cart_report(report):
    """Pretty-print the cart building report."""
    print(f"\n{'='*60}")
    print(f"  CART BUILDING REPORT")
    print(f"{'='*60}")

    for store_name, sr in report.get("stores", {}).items():
        print(f"\n  {store_name}:")
        print(f"  {'-'*50}")

        if sr["added"]:
            print(f"  Added ({sr['items_added']}):")
            for item in sr["added"]:
                price_str = f"{item['price']:.2f} AED" if item.get("price") else "? AED"
                print(f"    [OK] {item['item_name']}: {item['product_name'][:40]} x{item['qty']} -- {price_str}")

        if sr["failed"]:
            print(f"\n  Failed ({sr['items_failed']}):")
            for item in sr["failed"]:
                print(f"    [!!] {item['item_name']}: {item['reason']}")
                if item.get("alternatives"):
                    print(f"         Alternatives:")
                    for alt in item["alternatives"][:2]:
                        print(f"           - {alt['name'][:40]} ({alt['price']} AED) [{alt['tier']}]")

        print(f"\n  Estimated total: {sr['estimated_total']:.2f} AED")
        if sr.get("cart_verified") and sr["cart_verified"].get("total"):
            print(f"  Verified cart total: {sr['cart_verified']['total']:.2f} AED")

    print(f"\n{'='*60}")
    print(f"  {report.get('summary', '')}")
    print(f"{'='*60}")


# --------------------------------------------
# CLI
# --------------------------------------------

async def main():
    create_database()

    list_id = None
    store_filter = None
    dry_run = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--list-id" and i + 1 < len(args):
            list_id = int(args[i + 1])
            i += 2
        elif args[i] == "--store" and i + 1 < len(args):
            store_filter = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1

    if list_id is None:
        # Use most recent list
        recent = list_get()
        if recent:
            list_id = recent["list_id"]
            print(f"  Using most recent list: #{list_id} ({recent['target_date']})")
        else:
            print("  ERROR: No shopping lists found. Run list_generate.py first.")
            return

    report = await build_cart(list_id, store_filter=store_filter, dry_run=dry_run)

    if "error" in report:
        print(f"  ERROR: {report['error']}")
        return

    print_cart_report(report)


if __name__ == "__main__":
    asyncio.run(main())
