# Hearthkeeper - Technical Stack Breakdown

## System Architecture

```
                                     +-------------------+
                                     |   Web Browser     |
                                     | hearthkeeper.cc   |
                                     +--------+----------+
                                              |
                                          WebSocket
                                              |
+-------------------+              +----------+----------+
|  React Frontend   |   built     |      app.py         |
|  (Vite + Tailwind)|   assets    |   FastAPI server     |
|  Two-tab layout   +------------>+   WebSocket handler  |
+-------------------+              +----------+----------+
                                              |
                                   +----------+----------+
                                   |   agent_router.py   |
                                   |  Routes to agents   |
                                   +----+----------+-----+
                                        |          |
                           +------------+    +-----+-----------+
                           |                 |                  |
                  +--------+------+  +-------+--------+  +-----+--------+
                  |store_manager  |  |agent_brain.py  |  |frontend_     |
                  |.py            |  |(legacy state   |  |bridge.py     |
                  |16-tool agent  |  | machine)       |  |DB → React    |
                  +-------+-------+  +-------+--------+  +------+-------+
                          |                  |                   |
                  +-------+-------+  +-------+--------+         |
                  |agent_tools.py |  |list_generate.py|  +------+-------+
                  |agent_context  |  |basket_curator  |  |product_score |
                  +---------------+  +----------------+  |product_know  |
                                                         |product_grade |
                                                         +--------------+

Cart Building (Dual-Browser):
+------------------+     +------------------+
| UC Chrome        |     | Playwright       |
| (undetected-     |     | (Chromium)       |
| chromedriver)    |     |                  |
| Carrefour UAE    |     | Kibsons          |
| Profile:         |     | Profile:         |
| ~/.hearthkeeper- |     | ~/.hearthkeeper- |
|   carrefour/     |     |   browser/       |
+------------------+     +------------------+

Product Knowledge Pipeline:
+------------------+     +------------------+     +-------------------+
| scrape_carrefour |     | product_score.py |     | product_knowledge |
| _rsc.py          +---->+ 100-point scorer +---->+ .py               |
| scrape_kibsons   |     | product_grading  |     | (KB manager)      |
| _rsc.py          |     | (quality tags)   |     |                   |
+------------------+     +------------------+     +-------------------+
  RSC-based: 15K+                                         |
  products in 3 min                              +--------+---------+
                                                 | product_catalog  |
                                                 | item_alternatives|
                                                 | (SQLite)         |
                                                 +------------------+
```

## Services & Key Functions

### 1. app.py - Web Server
**Role:** FastAPI server + WebSocket hub + static frontend serving
**Framework:** FastAPI + Uvicorn

| Function | Purpose |
|----------|---------|
| `websocket_endpoint()` | WebSocket handler: routes messages, manages state |
| `handle_chat_message()` | Routes to Store Manager agent or legacy brain |
| `handle_action()` | Processes UI actions (ready, dismiss, cherry-pick, reset) |
| `build_vendor_carts()` | Triggers cart builder for a shopping list |
| `_broadcast_list_data()` | Sends suggestion + cart data to frontend |

**Features:**
- Two-entity data model (suggestions list + cart list)
- Toast notifications for non-intrusive feedback
- Cart lifecycle: create → modify → ready → build → archive
- Cherry-pick: move items from suggestions to cart

---

### 2. store_manager.py - Conversational Agent
**Role:** 16-tool AI agent that handles natural language grocery requests
**Model:** Claude (via Anthropic API)

| Tool Category | Tools | Purpose |
|--------------|-------|---------|
| Search | `search_catalog`, `search_by_category` | Find products in catalog |
| Cart | `add_to_cart`, `remove_from_cart`, `update_quantity` | Modify cart |
| Knowledge | `get_product_details`, `get_alternatives` | Product info |
| Profile | `get_household_preferences`, `get_consumption_patterns` | User context |
| History | `get_order_history`, `get_last_ordered` | Past purchases |
| Analysis | `compare_prices`, `check_availability` | Decision support |

**Agent loop:** Receives user message → selects tools → executes → responds with confirmation.
Max tool rounds: 5 (configurable).

---

### 3. cart_builder.py - Dual-Browser Cart Automation
**Role:** Builds real carts on store websites

**Dual-browser architecture:**

| Store | Browser | Profile | Why |
|-------|---------|---------|-----|
| Carrefour UAE | UC Chrome (undetected-chromedriver v3.5.5) | `~/.hearthkeeper-carrefour/` | Bypasses Akamai Bot Manager |
| Kibsons | Playwright (Chromium) | `~/.hearthkeeper-browser/` | No bot detection |

**UC Chrome specifics (Carrefour):**
- `_kill_stale_uc_chrome()`: Kills zombie Chrome processes via PowerShell before launch
- `_launch_uc_chrome()`: Launches with pinned chromedriver version matching Chrome
- `_detect_chrome_major()`: Reads `chrome.exe` FileVersion on Windows so the
  pinned chromedriver matches the installed Chrome major version
- `_uc_ensure_carrefour_login()`: Homepage check + auto-login via .env credentials
- `_uc_carrefour_email_login()`: Standalone helper — drives the `/login` form
  unconditionally (no homepage shortcut). Used by `order_scraper` when the
  homepage check gives a false-positive but `/app/*` still triggers step-up
  auth (cookies + localStorage on the homepage can look "logged in" while
  `/app/orders` bounces to `/login/phone`).
- `_uc_add_to_cart_carrefour()`: Navigate → detect state → add/adjust quantity

**Carrefour DOM selectors (as of Feb 2026):**
- Add to Cart: `//button[contains(., 'Add to cart')]` (text in child `<div>`)
- In-cart detection: `button[role="combobox"]` (quantity widget)
- OOS: Case-insensitive XPath with `translate()`
- Price: `//div[text()='AED']/parent::*` + regex

**Build flow:**
1. Phase 1: Launch UC Chrome → login → process all Carrefour items
2. Phase 2: Launch Playwright → login → process all Kibsons items
3. Per-item: KB lookup → navigate → add to cart → verify → quantity adjust
4. OOS fallback: mark unavailable in DB → try next ranked alternative
5. Cleanup: quit both browsers

---

### 4. frontend_bridge.py - Data Transformer
**Role:** Transforms SQLite data into React-friendly JSON

| Function | Purpose |
|----------|---------|
| `build_frontend_data()` | Builds complete vendor → category → item tree |
| `_determine_frontend_status()` | Maps DB state to UI status badges |
| `_check_price_change()` | Compares current vs last ordered price |
| `_compute_diff()` | Tracks changes vs original snapshot for reset button |

**Two-entity output:**
```json
{
  "suggestionListId": 123,
  "vendors": [...],          // suggestion items
  "cartListId": 456,
  "cartVendors": [...],      // cart items
  "highlights": {...},
  "diff": {"has_changes": true, ...}
}
```

---

### 5. list_generate.py - Shopping List Generator
**Role:** Predicts what to buy based on consumption model

| Function | Purpose |
|----------|---------|
| `list_generate()` | Creates list from items due by target date |
| `list_get()` | Fetches list by ID or most recent |
| `list_update_item()` | Update item: approve, skip, modify qty |
| `list_add_item()` | Add user-requested item |
| `list_finalize()` | Mark confirmed, bump next restock date |

---

### 6. product_score.py - Scoring Engine
**Role:** Ranks products on a 0-100 scale

**Scoring dimensions:**
| Dimension | Max Points | Description |
|-----------|-----------|-------------|
| Name relevance | +15 / -20 | Product name must match item name |
| Required attributes | +/-40 each | Deal-breakers (e.g., "organic" for eggs) |
| Preferred keywords | +8 each | Nice-to-haves (e.g., "free range") |
| Avoid keywords | -15 each | Unwanted (e.g., "frozen", "processed") |
| Brand match | +20 / -20 | Preferred vs avoided brands |
| Variety match | +12 | Produce types (e.g., "Royal Gala") |
| Unit price | +/-5 | Normalized per kg/L/piece |
| Purchase history | +3 to +15 | Products previously ordered |

**Tiers:** preferred (70+), acceptable (45-69), fallback (20-44), avoid (<20)

---

### 7. product_knowledge.py - Knowledge Base
**Role:** 3-phase product discovery pipeline

| Phase | Function | Speed | When |
|-------|----------|-------|------|
| Phase 1 | `discover_alternatives()` | ~20 min total | One-time per item |
| Phase 2 | `get_best_alternative()` | Instant (DB) | Each order |
| Phase 3 | `refresh_alternatives()` | ~30 sec | When preferred unavailable |

---

### 8. RSC Scrapers
**Role:** High-speed product catalog scrapers using React Server Components

| Scraper | Products | Speed | Method |
|---------|----------|-------|--------|
| `scrape_carrefour_rsc.py` | 15,719 | ~3 min | RSC payload parsing |
| `scrape_kibsons_rsc.py` | 8,700+ | ~3 min | RSC payload parsing |

Replaced old Playwright-based scrapers (which took 20+ minutes).

---

### 9. basket_curator.py - Basket Optimization
**Role:** Cross-vendor basket curation and optimization

- Assigns items to optimal stores based on scoring + availability
- Handles minimum order thresholds per store
- Generates substitution recommendations for OOS items

---

### 10. Frontend (React + Tailwind)
**Role:** Two-tab approval UI for the user

**Stack:** React 19 + Tailwind CSS 4 + Framer Motion + Vite

**Components:**
| Component | Purpose |
|-----------|---------|
| `App.jsx` | Two-tab layout (Suggestions / Cart), WebSocket state |
| `useWebSocket.js` | WebSocket connection, message handling, state management |
| `SkuCard.jsx` | Product card with qty controls, status badges, cherry-pick |
| `StickyActionBar.jsx` | Bottom action bar (Ready/Reset per tab) |
| `ChatPanel.jsx` | Chat interface for Store Manager agent |
| `VendorSection.jsx` | Vendor grouping with logo and item count |
| `CategoryRow.jsx` | Category grouping within vendors |
| `OrderTrackingCard.jsx` | Order status tracking display |

---

### 11. db_setup.py - Database
**Role:** SQLite schema (17+ tables)

| Table | Purpose |
|-------|---------|
| stores | Carrefour UAE, Kibsons |
| items | Master item list (20+ items) |
| orders / order_items | Past order records |
| restock_rules | Per-item prediction rules |
| shopping_lists / list_items | Generated shopping lists |
| suggestion_snapshots | Frozen original suggestions for diff/reset |
| product_catalog | Scraped product data (24K+ products) |
| item_alternatives | Pre-scored rankings per item per store |
| agent_log / llm_log | Agent actions + LLM calls |
| nudge_queue | Offline notification queue |
| cart_snapshots / cart_snapshot_items | Cart state tracking |
| app_events | Structured event log |

---

## Data Flow

```
1. SCRAPE:   scrape_*_rsc.py → product_catalog (24K+ products)
2. SCORE:    product_score.py → item_alternatives (ranked with reasons)
3. PREDICT:  list_generate.py → shopping_lists + list_items
4. BRIDGE:   frontend_bridge.py → React JSON (vendors → categories → items)
5. REVIEW:   React frontend → user modifies via UI or chat
6. AGENT:    store_manager.py → handles chat requests (16 tools)
7. BUILD:    cart_builder.py → UC Chrome (Carrefour) + Playwright (Kibsons)
8. LOG:      agent_log → feedback loop for learning
```

## Dependencies

Python dependencies are pinned in `requirements.txt` (9 packages) — install with
`python3 -m pip install -r requirements.txt`.

```
# Core
fastapi           # Web server
uvicorn           # ASGI server
websockets        # WebSocket support

# Browser automation
playwright        # Kibsons cart building
undetected-chromedriver  # Carrefour cart building (Akamai bypass)
selenium          # WebDriver API for UC Chrome
setuptools        # Required by undetected-chromedriver on Python 3.14

# AI
anthropic         # Claude API for Store Manager agent

# Frontend
react             # UI framework
tailwindcss       # Styling
framer-motion     # Animations
vite              # Build tool

# Database
sqlite3           # Built into Python
```

## Environment Variables (.env)

```
ANTHROPIC_API_KEY=sk-ant-...     # Claude API key
CARREFOUR_EMAIL=...              # Carrefour UAE login
CARREFOUR_PASSWORD=...           # Carrefour UAE password
KIBSONS_EMAIL=...                # Kibsons login
KIBSONS_PASSWORD=...             # Kibsons password

# App security (added 2026-05-21)
APP_PASSCODE=...                 # Household login passcode for the /login page
SESSION_SECRET=...               # Signing key for the hk_session cookie
WEBHOOK_SECRET=...               # Shared secret for the WhatsApp webhook (X-Webhook-Secret header)

# Auto-deploy (added 2026-05-25, host-only)
GITHUB_WEBHOOK_SECRET=...        # Shared secret for the GitHub deploy webhook
                                 # (HMAC-SHA256 X-Hub-Signature-256 header). Empty = disabled.
DEPLOY_RESTART_SERVICE=...       # NSSM service name to restart on deploy. Default 'hearthkeeper-app'.
DEPLOY_BRANCH=...                # Optional override for the branch the deploy webhook accepts.
                                 # Empty = use the host's currently-checked-out branch at request time.
```

All data stored locally in SQLite. No cloud services for data storage.

---

## Backend Admin Dashboard + Operations

**Added 2026-05-25.** A new `health_dashboard.py` module hosts a backend
admin dashboard and a small operations API. It's mounted at the same FastAPI
app so it inherits the session-cookie auth middleware; one route is
explicitly auth-exempt for remote diagnostics.

### Routes

| Route | Auth | Purpose |
|-------|------|---------|
| `GET /admin/dashboard` | session | Serves the dashboard HTML (`static/admin_dashboard.html`) |
| `GET /admin/qrcode.min.js` | session | Vendored QR-rendering library for the WhatsApp pairing tile |
| `GET /api/health/jobs` | session | JSON for the **Health** tab — per-job status, flow chart edges, callouts |
| `GET /api/admin/orders-summary` | session | JSON for the **Orders** tab — KPI totals, per-store breakdown, recent orders list with delivery-type pills, recent poller / deploy / scrape events |
| `POST /api/admin/run-order-poller` | session | Manually trigger an order-history scrape from the dashboard. Guarded against double-clicks (`app_state.order_poll_active`) and the cart-builder lock (`app_state.cart_build_active`). |
| `GET /api/health/whatsapp/qr` | session | Proxies the local sidecar's QR endpoint so the pairing QR can be scanned from a phone without exposing port 3001 to the LAN |
| `GET /api/diag/recent-errors` | **none** | Read-only error trail — last N error / warning / `order_poller_*` / `deploy_*` / `carrefour_*` / `kibsons_*` events. Exists so the dev side can verify deploys + triage failures from anywhere, without sharing a session cookie. Scope is intentionally narrow: no chat content, no PII, no credentials. |

### Dashboard frontend

`static/admin_dashboard.html` — single-file Tailwind + vanilla JS. Two tabs:

- **Health** — overall "actions required" banner, SVG flow chart of every
  job, per-job detail cards with status pills + 24-outcome sparkline +
  callouts, inline WhatsApp pairing QR when the sidecar is unlinked.
- **Orders** — KPI cards, per-store breakdown with the "▶ Run scrape now"
  button in the section header, a filterable **recent orders** table
  (store + delivery type filters, NOW / Scheduled / unlabeled pills,
  status pills), and a collapsible recent-events section that defaults to
  closed so the orders list owns the screen.

Poll cadence: dashboard polls `/api/health/jobs` every 20 s and re-fetches
`/api/admin/orders-summary` lazily on Orders-tab activation + the same
heartbeat. The Self-call deadlock fix (commit `fbe0c7e`) was needed
because the per-tick health check makes an HTTP self-call to `/api/health`
to verify the service is responsive — that call now uses `httpx.AsyncClient`
instead of blocking `urllib.request.urlopen`, so the FastAPI event loop
stays free to serve the very request the self-check is waiting on.

---

## Auto-Deploy on GitHub Push

**Added 2026-05-25.** The host pulls new code automatically when a push lands
on the deploy branch — no SSH, no manual `nssm restart`.

### Request path

```
git push -> GitHub repo
            └── webhook fires
                POST hearthkeeper.cc/webhook/deploy
                content-type: application/json
                X-Hub-Signature-256: sha256=<hmac>
                payload: standard GitHub push event JSON
            │
            ▼
        Cloudflare Tunnel  ──>  FastAPI on the host
                                │
                                ├─ HMAC-verify body against GITHUB_WEBHOOK_SECRET
                                ├─ Filter event type (push) + branch ref
                                ├─ Spawn background `threading.Thread`
                                └─ Return 200 {"status":"queued"} (<2 s)
                                          │
                                          ▼
                                 _perform_deploy_sync()
                                  ├─ git pull --ff-only
                                  │   (PAT-embedded remote URL bypasses
                                  │    the user's Credential Manager,
                                  │    which the service session can't
                                  │    decrypt under Windows Hello)
                                  ├─ log_event deploy_pulled head=<sha>
                                  └─ thread.sleep(3) then os._exit(0)
                                          │
                                          ▼
                                NSSM detects exit -> auto-restart with new code
                                          │
                                          ▼
                              /api/health returns the new commit SHA
```

### Why each piece

| Design choice | Why not the alternative |
|---------------|-------------------------|
| `threading.Thread`, not `asyncio.create_task` | asyncio only holds weak references to running tasks. A fire-and-forget background coroutine was silently GC'd between awaits in production, even with a strong-ref set — observed via the diag endpoint: `deploy_pull_start` logged, then nothing for minutes. Plain OS thread sidesteps the entire async-task-lifecycle question. |
| Return 200 immediately, do work in background | GitHub gives webhook receivers ~10 s to respond. A synchronous `git pull` from the host's home internet routinely blew that budget, GitHub recorded "delivery timed out" even though the pull actually succeeded. |
| `os._exit(0)` + NSSM auto-restart, not `Restart-Service` | Restarting a Windows service requires admin elevation. The FastAPI service runs as a regular user account (`.\CemPo`), not LocalSystem, so `Restart-Service` silently fails with "Access denied" from a detached subprocess. A process is *always* allowed to terminate itself; NSSM's default `AppExit=Restart` brings it back in seconds. |
| PAT embedded in remote URL, not SSH or system credential helper | Windows Hello (PIN/biometric) login encrypts Credential Manager entries with TPM-bound DPAPI keys that only the *interactive* session can decrypt. The service runs as the same user account but in Session 0 — it can't access those keys. Cached `git credential` entries from the user's interactive PowerShell are unreadable to the service. The PAT in `.git/config` is read-only filesystem-protected, scoped to a single repo with **Contents: Read** only, and rotatable in two minutes. |
| `GIT_TERMINAL_PROMPT=0` + `-c credential.helper=` + `-c core.askpass=` | Defence-in-depth: if for any reason the PAT URL stops working, the pull fails fast with a clean stderr instead of hanging on an invisible credential prompt that Windows' subprocess timeout can't reliably kill. |

### Per-step event trail

Every step of `_perform_deploy_sync` writes a row to `app_events`, visible
via the diag endpoint:

| Event | Source | Meaning |
|-------|--------|---------|
| `deploy_unauthorized` | webhook_deploy | HMAC failed — rejected (warning level) |
| `deploy_ignored` | webhook_deploy | Wrong branch or non-push event |
| `deploy_queued` | webhook_deploy | Webhook accepted, thread spawned |
| `deploy_thread_started` | webhook_deploy | Background thread entered |
| `deploy_pull_start` | webhook_deploy | About to call `git pull` |
| `deploy_pull_error` | webhook_deploy | (error) Exception or timeout in subprocess |
| `deploy_pull_failed` | webhook_deploy | (error) `git pull --ff-only` returned non-zero — stderr captured |
| `deploy_pulled` | webhook_deploy | Pull succeeded, HEAD captured |
| `deploy_restart_scheduled` | webhook_deploy | Self-terminate timer started — NSSM will restart the service |
| `deploy_restart_failed` | webhook_deploy | (error) Could not schedule the self-exit |

A successful deploy fires `deploy_queued`, `deploy_thread_started`,
`deploy_pull_start`, `deploy_pulled`, `deploy_restart_scheduled` in
sequence — typically within ~3 seconds, after which the service restarts
and `/api/health` returns the new `commit` field within another ~1 second
(NSSM `AppRestartDelay=0`).

---

## Carrefour Order Scraping

**Updated 2026-05-25.** Carrefour's order history (`/mafuae/en/app/orders`)
is now scraped through UC Chrome instead of Playwright. Akamai Bot Manager
fingerprints Playwright's Chromium and 403s its auth API even from a
residential IP; UC Chrome isn't fingerprinted the same way.

`process_carrefour_orders_uc(dry_run, pending_dict)` in `order_scraper.py`
is the live entry point. It:

1. Takes the shared `app_state.cart_build_active` lock to avoid fighting
   the cart builder for the `~/.hearthkeeper-carrefour/` profile.
2. Opens its own SQLite connection (the function runs under
   `asyncio.to_thread`; sqlite connections are thread-bound).
3. Launches UC Chrome via `_launch_uc_chrome()`, runs the homepage check
   via `_uc_ensure_carrefour_login()`.
4. Navigates to `/app/orders`. If Carrefour step-up auths and bounces to
   `/login/phone` (homepage cookies say "logged in" but `/app/*` demands
   a fresh credential exchange), clears localStorage / sessionStorage /
   cookies on the Carrefour origin and calls `_uc_carrefour_email_login`
   directly — skipping the misleading homepage shortcut.
5. Extracts orders via a single shared JS IIFE
   (`_CARREFOUR_EXTRACT_JS_IIFE`). The same IIFE is used by both the live
   UC path and the (currently disabled) Playwright path so the parsing
   stays in one place.

The IIFE captures: `order_ref`, `status`, `date_text`, `total_text`,
`items_text`, `detail_url`, and (added 2026-05-25) `delivery_type` —
one of `'now'` / `'scheduled'` / `''` based on the "NOW Delivery" /
"Scheduled Delivery" card label.

The `orders.delivery_type` column was added the same day via
`db_setup.py` (idempotent `ALTER TABLE`). Newly-scraped orders write
the value directly; pre-existing rows get backfilled in place on re-scrape
when the column is NULL.
