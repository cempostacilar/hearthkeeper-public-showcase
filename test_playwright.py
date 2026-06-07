"""
AutoRestock - Playwright Feasibility Test

Tests whether Playwright can:
1. Launch a browser (visible, not headless - so you can see what's happening)
2. Reuse your Edge profile (so you're already logged in)
3. Access WhatsApp Web (check if logged in)
4. Access Carrefour UAE (check if logged in)
5. Access Kibsons (check if logged in)

IMPORTANT: Close Edge browser before running this script.
Playwright can't use a profile that's already open by another browser.

Run: python3 test_playwright.py
"""

import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

# Edge user data directory - this is where your logins/cookies live
# Default location on Windows for Edge
EDGE_USER_DATA = str(Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data")


async def test_with_edge_profile():
    """Try launching with your existing Edge profile."""
    print("=" * 60)
    print("TEST 1: Launch with Edge profile (reuse logins)")
    print("=" * 60)
    print(f"  Edge profile path: {EDGE_USER_DATA}")
    print(f"  Profile exists: {Path(EDGE_USER_DATA).exists()}")
    print()
    print("  NOTE: Edge must be CLOSED for this to work.")
    print("  Playwright can't share a profile with a running browser.")
    print()

    async with async_playwright() as p:
        try:
            # Launch Edge (Chromium-based) with your existing profile
            context = await p.chromium.launch_persistent_context(
                user_data_dir=EDGE_USER_DATA,
                channel="msedge",  # Use installed Edge, not Playwright's Chromium
                headless=False,    # Visible so you can see what's happening
                args=["--disable-blink-features=AutomationControlled"],  # Less likely to be detected as bot
            )
            print("  ✓ Launched Edge with your profile!")
            page = context.pages[0] if context.pages else await context.new_page()

            # Test WhatsApp Web
            print("\n--- Testing WhatsApp Web ---")
            await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)  # Give it time to load/check session
            title = await page.title()
            url = page.url
            print(f"  Title: {title}")
            print(f"  URL: {url}")
            # Check if we're logged in (search bar visible = logged in, QR code = not logged in)
            logged_in = await page.query_selector('div[contenteditable="true"][data-tab="3"]') is not None
            qr_code = await page.query_selector('canvas') is not None
            if logged_in:
                print("  ✓ WhatsApp Web: LOGGED IN (search bar found)")
            elif qr_code:
                print("  ✗ WhatsApp Web: NOT LOGGED IN (QR code showing)")
            else:
                print("  ? WhatsApp Web: Can't determine login state. Check the browser window.")

            # Test Carrefour UAE
            print("\n--- Testing Carrefour UAE ---")
            page2 = await context.new_page()
            await page2.goto("https://www.carrefouruae.com/mafuae/en/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            title2 = await page2.title()
            print(f"  Title: {title2}")
            # Check for logged-in indicators
            account_el = await page2.query_selector('[data-testid="header_user_icon"]')
            print(f"  Account icon found: {account_el is not None}")
            if account_el:
                print("  ✓ Carrefour: Likely LOGGED IN")
            else:
                print("  ? Carrefour: Check browser window for login state")

            # Test Kibsons
            print("\n--- Testing Kibsons ---")
            page3 = await context.new_page()
            await page3.goto("https://www.kibsons.com/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            title3 = await page3.title()
            print(f"  Title: {title3}")
            # Kibsons login check
            account_el2 = await page3.query_selector('a[href*="account"], .account-link, .user-menu')
            print(f"  Account element found: {account_el2 is not None}")

            print("\n" + "=" * 60)
            print("Check the browser window to visually verify login states.")
            print("Press Enter here to close the browser and finish the test.")
            print("=" * 60)
            input()

            await context.close()

        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            print()
            if "user data directory is already in use" in str(e).lower():
                print("  → Edge is still running! Close Edge completely and try again.")
            elif "channel" in str(e).lower():
                print("  → Edge might not be installed or not found. Trying with Chromium instead...")
            raise


async def test_with_fresh_profile():
    """Fallback: launch with a fresh profile (won't have logins)."""
    print("\n" + "=" * 60)
    print("TEST 2: Launch with FRESH profile (no logins)")
    print("=" * 60)
    print("  This tests that Playwright itself works, even without your logins.")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto("https://www.carrefouruae.com/mafuae/en/", wait_until="domcontentloaded", timeout=30000)
        title = await page.title()
        print(f"  Carrefour title: {title}")
        print(f"  ✓ Playwright works! (but you won't be logged in)")
        print("  Press Enter to close.")
        input()
        await browser.close()


async def main():
    try:
        await test_with_edge_profile()
    except Exception:
        print("\n  Edge profile test failed. Trying fresh profile as fallback...\n")
        try:
            await test_with_fresh_profile()
        except Exception as e2:
            print(f"  ✗ Fresh profile also failed: {e2}")
            print("  Playwright installation may have issues.")


if __name__ == "__main__":
    asyncio.run(main())
