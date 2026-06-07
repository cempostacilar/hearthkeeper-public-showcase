# Hearthkeeper — Architecture

The single source of truth for "how it all works." Read top-to-bottom once;
come back to a section later. This file supersedes the dated daily-summary
docs. **If a PR changes architecture, update THIS file** (not a new dated doc).

Hearthkeeper runs an autonomous grocery-restock loop for one household in
Dubai. It predicts what the household needs, lets the curator (Cem) review it,
builds carts on the store websites, tracks the resulting orders, and learns
from delivery feedback — closing the loop back into the next prediction.

---

## 1. Big picture

Three surfaces, one backend process, one database.

```
   ┌─────────────────────┐        ┌──────────────────────────────┐
   │ Curator app (React)  │  WS    │  FastAPI  (app.py)           │
   │  hearthkeeper.cc/    │◄──────►│   • WebSocket chat + actions │
   └─────────────────────┘        │   • REST + diag endpoints     │
   ┌─────────────────────┐        │   • 12 async scheduler tasks  │
   │ Admin dashboard      │  HTTP  │   • deploy webhook            │
   │  /admin (static)     │◄──────►│                              │
   └─────────────────────┘        └───────────────┬──────────────┘
   ┌─────────────────────┐  HTTP                   │
   │ WhatsApp sidecar     │◄───────────────────────┤  reads/writes
   │  Node, whatsapp-web  │   /send /restart        ▼
   │  (Sara / Max chat) │        ┌──────────────────────────────┐
   └─────────────────────┘        │  autostock.db  (SQLite, 39 t) │
                                   └───────────────┬──────────────┘
                                                   │ Playwright / UC Chrome
                                          ┌────────┴─────────┐
                                          │  Carrefour UAE   │
                                          │  Kibsons         │
                                          └──────────────────┘
```

- **Curator app** — React (`frontend/`), served by FastAPI at `/`. Cem reviews
  the predicted "Scheduled" list, edits it, builds a "Cart," sends to family.
- **WhatsApp sidecar** — a Node process (`whatsapp-service/`, whatsapp-web.js)
  on `localhost:3001`. Sara/Max reply on WhatsApp to skip/swap/add/rate.
  **All outbound WhatsApp is currently fail-closed (see §8).**
- **Admin dashboard** — static HTML at `/admin`: health, orders, observability.

Everything except the WhatsApp sidecar and the browser automation runs
in-process in the single FastAPI app. State is one SQLite file, `autostock.db`.

### The closed loop (the whole system in one diagram)

```
  consumption habits + order/delivery history + external purchases
                              │
                              ▼
        cadence_calibration → restock_rules (when + how much)
                              │
                  list_generate (inventory model)
                              │   honors: household_rules, preference_state,
                              ▼            stop_suggesting, household_profile
        list_quality_validator (pick the right product per item)
                              │
                              ▼
        SCHEDULED list ──► curator reviews/edits ──► CART
                              │
                              ▼
        cart_builder (UC Chrome / Playwright) → carts on store sites
                              │   ⟶ USER places the order (agent never checks out)
                              ▼
        order_scraper poller → orders / order_items / order_status_log
                              │   ⟶ order-stage notifications (gated)
                              ▼
        delivered → ratings (app or WhatsApp)
                              │
            ┌─────────────────┴──────────────────┐
            ▼                                     ▼
   household_rules (LLM-parsed)          preference_signals → preference_state
   (avoid/prefer brand, attribute,        (per-item learned brand/qty/timing/…)
    cheaper, variety, check_flag)
            └─────────────────┬──────────────────┘
                              ▼
                back into list_generate + scoring  ⟲  (loop closes)
```

---

## 2. Data model (SQLite, 39 tables)

`db_setup.py` owns the schema. Every table/column is created idempotently
(`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE … ADD COLUMN` inside a
`try/except OperationalError`), so re-running `create_database()` is always
safe. Versioned migrations gate on `PRAGMA user_version` (currently **3**).

### The 6 buckets

| Bucket | Tables (key ones) |
|---|---|
| **Catalog** | `product_catalog` (~70k scraped products), `item_alternatives` (pre-scored picks per item×store), `stores` |
| **Household model** | `items` (master product list built from order history), `restock_rules` (per-item cadence/qty prediction, 1:1 with items), `household_members`, `household_preferences` (global fallbacks) |
| **List state** | `shopping_lists` (the two-list model), `list_items`, `suggestion_snapshots` (frozen AI baseline for "reset to original"), `cart_attempts`, `item_resolution_attempts` |
| **Order history** | `orders`, `order_items`, `order_status_log`, `order_ratings`, `order_item_ratings`, `external_purchases` |
| **Preferences / feedback** | `household_rules`, `user_item_prefs`, `preference_signals` (immutable event log), `preference_state` (aggregated current prefs), `skip_events` |
| **WhatsApp / chat** | `whatsapp_messages`, `feedback_rounds`, `feedback_member_status`, `feedback_items`, `chat_messages`, `nudge_queue` |
| **Ops** | `app_events`, `app_state`, `agent_turns`, `llm_usage`, `llm_log`, `agent_log` |

### The two-list model (critical)

`shopping_lists.list_type` separates the two surfaces the user toggles between:

- **Scheduled** = `list_type='suggestion'` (or `NULL`) — the AI's predicted list.
  Regenerated each cycle; stale drafts for other target dates are superseded.
- **Cart** = `list_type='cart'` — the user's working order, built from the
  Scheduled list, mutable until the order is placed.

`list_items` rows hang off a list; `status` ∈ `pending/approved/skipped`,
`added_by` ∈ `system/user`, `original_qty` snapshots the AI suggestion.
Which list a chat edit hits is decided by `session.active_view` (see §5).

### Key relationships

```
items 1─1 restock_rules        items 1─N item_alternatives ─N product_catalog
shopping_lists 1─N list_items ─N cart_attempts
orders 1─N order_items 1─N order_item_ratings ;  orders 1─1 order_ratings
orders 1─N order_status_log
feedback_rounds 1─N feedback_member_status / feedback_items / whatsapp_messages
preference_signals (events) ─► preference_state (aggregate, unique item×dimension×key)
order_ratings ─► household_rules (LLM-parsed)  ;  order_ratings ─► preference_signals
```

### Who writes what

| Table | Primary writer(s) |
|---|---|
| `restock_rules` | `cadence_calibration`, `list_generate`, `pattern_detector`, `skip_tracking` |
| `list_items` | `list_generate`, `list_quality_validator`, `agent_tools`, app actions |
| `product_catalog` / `item_alternatives` | `scrape_*` (nightly), `product_knowledge` |
| `orders` / `order_items` / `order_status_log` | `order_scraper` |
| `order_ratings` / `order_item_ratings` | `app._save_rating` |
| `household_rules` | `household_rules.capture_feedback` (+ `add_household_rule` from chat) |
| `preference_signals` → `preference_state` | `preference_engine.emit_signal` / `process_pending_signals` |
| `chat_messages` | `app._save_chat_message` |
| `whatsapp_messages` | `whatsapp_client` (send + inbound webhook) |

---

## 3. Runtime, hosting & deploy

### Processes

| Process | What | How it starts |
|---|---|---|
| FastAPI app | `app.py` via uvicorn on `:8000`; serves React, WS, REST, runs the scheduler in-loop | NSSM service `hearthkeeper-app` |
| WhatsApp sidecar | `whatsapp-service/index.js` (Express + whatsapp-web.js) on `:3001`; persists auth in `.wwebjs_auth/` | NSSM service `hearthkeeper-whatsapp` |
| Watchdog | `watchdog.py` polls `/api/health` and **self-heals**: restarts the app if HTTP is down OR the scheduler heartbeat is stale (process up but background work dead), and pings the test number (gated WhatsApp via `/api/diag/watchdog-alert`) when it intervenes. A "scheduler never started" reading alerts without restart-looping. | Windows Task Scheduler, every ~5 min |
| Browser automation | UC Chrome (Carrefour) / Playwright (Kibsons), launched on demand by `cart_builder` / `order_scraper` | in-process, guarded by `app_state.cart_build_active` |

Host: a Windows "ROG Ally" at home. Public access via a **Cloudflare tunnel**
→ `hearthkeeper.cc`. (Cloudflare returns error 1010 to non-browser User-Agents,
so diag scripts must send a browser UA.)

### Deploy pipeline (git → live)

```
git push main ─► GitHub webhook ─► POST /webhook/deploy (HMAC X-Hub-Signature-256)
   ─► returns 200 immediately, spawns a thread:  git pull --ff-only
   ─► nssm restart hearthkeeper-app  ─► poll /api/health for the new commit SHA
```

`/api/health` returns `{status, commit}` (short SHA) — the canonical "is the
new build live" check. `_perform_deploy_sync` runs off the event loop so the
webhook meets GitHub's 10s timeout.

### Auth model

- **Session** (curator app): single household passcode → signed cookie
  `hk_session = <issued_ts>.<hmac_sha256(SESSION_SECRET, issued_ts)>`,
  verified by `_verify_session_token`, max age 30 days. A middleware requires it
  on every path **except** `_AUTH_EXEMPT` (login, `/api/health`, the webhooks,
  and the HMAC-gated diag triggers).
- **Webhooks**: WhatsApp inbound and GitHub deploy each verify a shared-secret
  HMAC (`WEBHOOK_SECRET`, `GITHUB_WEBHOOK_SECRET`). Empty secret ⇒ reject all.
- **Diag triggers** (e.g. Carrefour re-scrape) accept HMAC **or** a session
  cookie.

### The scheduler tasks (`scheduler.start_scheduler`)

`start_scheduler()` is launched by the `@app.on_event("startup")` hook in
app.py (a smoke test guards that this call is never removed — the Wave-5a
cleanup once deleted it and silently killed all background work for ~4 days).
Each task runs as its own `asyncio.create_task` with an isolated
`try/except/log/sleep` loop, so one crashing doesn't take down the others.

| Task | Cadence | Owns |
|---|---|---|
| `_scheduler_heartbeat` | ~60 s | stamps `app_state.scheduler_last_beat` for the `/api/health` liveness check (watchdog self-heal) |
| `_nudge_scheduler` | ~30 min | check-in nudges (Mon/Thu eve, Wed/Sat morning) |
| `_order_status_poller` | **adaptive** (waking hrs): ~10 min out-for-delivery · ~22 min placed · hourly idle/overnight | scrape store order pages; detect status changes |
| `_nightly_catalog_scraper` | 04:00 | refresh `product_catalog` (sets the browser lock) |
| `_nightly_pattern_detector` | 05:00 | drop-pattern escalation ladder (pure SQL) |
| `_nightly_carrefour_order_detail_backfill` | 05:30 | re-scrape orders with `items=[]` |
| `_feedback_timeout_checker` | ~5 min | feedback-round deadlines **+ follow-up re-pings** |
| `_nightly_whatsapp_restart` | 03:30 | preemptive sidecar restart |
| `_whatsapp_liveness_probe` | ~2 min | restart sidecar after 3 failures; **circuit breaker** after N failed restarts (stop thrashing, escalate) |
| `_delivered_rating_dispatcher` | ~10 min | delivered → rating prompt (B5) |
| `_status_update_dispatcher` | ~10 min | out-for-delivery notification (B3) |
| `_rating_reminder_dispatcher` | hourly | 48h "still want to rate?" (B6) |
| `_today_deliveries_morning_dispatcher` | 07:30 | "arriving today" summary (B4) |

Plus ad-hoc **post-cart checks** scheduled at **+15/+30/+60 min** after a cart
build, to catch the just-placed order quickly.

### Reliability & health visibility

The home-box failure modes that hurt uptime were systemic: recovery that didn't
remediate, no circuit breaker, and **silent failures whose only alarm channel
(WhatsApp) was usually the broken thing**. Three mechanisms address that:

- **Subsystem health ledger** (`subsystem_health.py` → `GET /api/health/subsystems`
  → `/admin/health`, both auth-exempt). Derives each background subsystem's status
  (ok / degraded / down) from the events the loops **already emit** — last success
  vs its expected cadence, plus any recent crash — with live probes for the
  scheduler beat and the WhatsApp connection. No per-loop rewiring; **staleness is
  the safety net** (a loop that silently dies, or whose success event stops
  arriving, goes stale and shows up even with no error logged). Dispatchers are
  "error-only" (they only act when there's something to send, so cadence-staleness
  doesn't apply). `/admin/health` is the WhatsApp-independent place to glance at.
- **WhatsApp self-heal that remediates.** The sidecar (`whatsapp-service/index.js`)
  `destroy()`s the client on SIGTERM/SIGINT and in `/restart` (so a restart kills
  the Chromium child instead of orphaning it), and `clearStaleBrowser()` on startup
  removes a stale `SingletonLock` + kills any orphaned Chromium scoped to our
  session dir. This breaks the "browser already running" crash-loop at the source.
- **Circuit breaker** in `_whatsapp_liveness_probe`: after `MAX_RESTART_ATTEMPTS`
  restarts that don't restore the link, it stops thrashing, logs `wa_circuit_open`
  (surfaced by the ledger), and only re-tries every 6h; a good probe re-arms it.

**Gotcha — `app_events` columns are `event` + `timestamp` (UTC), not
`event_type`/`created_at`.** And any bulk write keyed on catalog rows must batch
under SQLite's 999-variable limit (a single `IN (?, …)` over tens of thousands of
URLs silently aborted the nightly scrape).

### Config knobs that gate behavior (`config.py`, from env)

| Knob | Default | Gates |
|---|---|---|
| `WHATSAPP_ENABLED` | false | all WhatsApp dispatchers/sends (early-return when off) |
| `WHATSAPP_ALLOWLIST` | empty | **recipient allowlist** — empty + enforced = block all (see §8) |
| `WHATSAPP_ALLOWLIST_ENFORCED` | true | fail-closed enforcement of the allowlist |
| `ORDER_NOTIFS_TEST_ONLY` | true | route order-stage notifs to the driver only (per-feature) |
| `AGENT_ENABLED` | true | use the Store Manager LLM agent for chat |
| `AGENT_MODEL` / `AGENT_MAX_TOKENS` / `AGENT_MAX_TOOL_ROUNDS` | Sonnet / 1024 / 7 | the chat agent's model + loop bounds |
| `AGENT_DAILY_TOKEN_BUDGET` | 500k | daily token cap (LLM calls throw when exceeded) |
| `SESSION_SECRET` / `WEBHOOK_SECRET` / `GITHUB_WEBHOOK_SECRET` | "" | auth (empty ⇒ that surface refuses) |

---

## 4. Hot path 1 — Predict the Scheduled list

Runs on a schedule and whenever the curator opens the app. Entry:
`list_generate.list_generate(target_date)`.

```
1. next order date (next Tue/Fri, ~3.5-day cadence) ─ _next_order_date
2. sync_restock_dates_from_deliveries  ─ anchor the consumption clock to the
   DELIVERY date (forward-only; Carrefour order_date is unreliable)
3. refresh_observed_cadences (cadence_calibration) ─ recompute avg_days_between
   + next_restock_date from real order/external-purchase history; FORWARD-ONLY
   on last_ordered (never regress to a stale captured date).
   STAPLE FLOOR: basics whose name contains a staple word (eggs, bread, milk,
   kefir, yogurt, bananas — whole-word, so "Eggplant"/"Buttermilk" are excluded)
   are capped per-item at STAPLE_MAX_CADENCE (bread/milk/bananas 2d,
   kefir/eggs/yogurt 3d; multi-word names take the tightest cap). Incomplete
   capture (Carrefour line-item gap) otherwise inflates their observed gap to
   13-33d and they fall off the list as "stocked"; the household buys them
   ~every order.
4. _apply_delivery_sequence / backfill_last_ordered_from_lists ─ Tier-0/Tier-1
   anchoring; returns the in_flight set (placed-but-not-delivered → suppressed)
5. hard pre-filters: times_ordered≥3, within rotation window, confidence≥0.3,
   NOT stop_suggesting, valid cadence
6. _inventory_suggest_quantity ─ THE CORE MODEL:
      daily_use   = avg_qty / cadence
      stock@target= max(0, last_order_qty - daily_use × days_since_last
                              - daily_use × days_to_target)
      if stock covers the window → skip ("stocked:Nd")
      else order ceil(shortfall × daily_use)
7. validate_list_quality → per item: derive_quality_bar + score_candidate to
   pick the actual product (and switch store if a better match exists)
8. write list_items; supersede stale drafts; prune system items no longer due
9. frontend_bridge.build_frontend_list → the JSON the React app renders
```

### Live availability check (`live_verify`)

Keeps the Scheduled list showing IN-STOCK items. `live_verify_list_availability`
resolves the product each item currently displays (pending suggestion rows via
`frontend_bridge._get_product_info`; their `item_name` falls back to `items.name`
so the generic search can run), live-searches each store, and writes fresh
availability + price back to `product_catalog`. It does **not** edit `list_items`
— once the catalog is fresh, the render-time OOS auto-swap (with a note) and the
cart resolver pick the best available alternative on their own.

- **Triggers:** primarily the **background `scheduler._availability_refresh_loop`**
  (~every 20 min, skipped while a browser is busy) — the "async, keep it fresh"
  path so the user never waits at app-open; plus the start of `build_cart` (so we
  only attempt to add in-stock products). The old connect-time verify is gated
  OFF by default (`AVAILABILITY_VERIFY_ON_CONNECT`) — launching a browser inside
  the WS connect path was risky.
- **Fail-safe:** a search that returns NOTHING is *inconclusive*, never OOS;
  `_update_catalog` distrusts a verdict where ≥80% of verified items are OOS (a
  blocked session can't flag the whole list). `/api/admin/restore-recent-oos`
  undoes a bad run.
- **Store coverage:** Kibsons works (RSC search, generic-name query). **Carrefour
  does not** — its search is client-rendered behind Akamai bot detection, so a
  Playwright/Edge fetch gets a product-less shell (the cart builder/scraper use
  UC Chrome + category RSC for this reason). Carrefour items are safely skipped
  (inconclusive → untouched). Verifying them needs a category-RSC or UC-Chrome
  path — open follow-up.

### Picking the product (`list_quality_validator`)

`derive_quality_bar(item, household_prefs)` builds the criteria; `score_candidate`
scores each catalog option. Honored inputs:

- **Catalog gates** (`product_knowledge` + `score_candidate`): category match ·
  form-modifier block (no "Apple Juice" for "Apple") · required-noun match ·
  sibling-noun penalty ("Sweet Potatoes" for "Potatoes") · foreign-noun reject
  (≥2 unexplained nouns → wrong product). NB the universal allow-list holds only
  DESCRIPTORS, never product NOUNS — "yoghurt"/"yogurt" were wrongly in it, so
  "Banana Yoghurt" passed for a "Banana" item; the yogurt↔yoghurt spelling
  tolerance now lives in `_SPELLING_EQUIV`, applied only when yogurt is expected.
  The candidate query is category-aware (`category LIKE '%<item category>%'`) but
  the term loop re-pulls WITHOUT that filter when nothing meets the bar — store
  taxonomies ("fruits"/"vegetables") don't contain the household word "Produce",
  which otherwise hid the only correct product. **Candidate retrieval MUST order
  before `LIMIT 20`** (`ORDER BY is_available DESC, LENGTH(product_name) ASC`): a
  broad term like "Garlic" matches dozens of rows (garlic oil, garlic spice,
  garlic cheese...), and an unordered slice excluded the real "Kibsons Garlic",
  so every surfaced candidate was a flavoured reject and the staple was skipped.
- **Organic** — strongly preferred for produce (+20, so an organic option
  decisively beats a non-organic one), but **non-strict by default**: a good
  non-organic option is accepted (−12, "no organic in stock") so a staple isn't
  skipped when the store has no organic version ("organic or good"). Strict only
  when the item note says "only organic" (`organic_strict`, e.g. eggs → skip
  rather than buy non-organic). An "Organic **or** good quality" note
  (`_ORGANIC_OPTIONAL`) makes organic a pure soft boost.
- **Price sanity** — a candidate priced >3× the item's usual `avg_price` gets a
  soft −10 (no reject; pack sizes vary), deprioritizing an overpriced mismatch.
- **household_rules** (from delivery feedback): `avoid_brand` (hard reject),
  `prefer_brand` (+25), **`prefer_attribute`** (origin/grade like wagyu/NZ/
  grass-fed, +12; also added to the "expected words" so the desired grade isn't
  mis-flagged as a wrong product), **`prefer_cheaper`** (stronger price weight).
- **preference_state** (learned) via `get_scoring_profile` — per-item brand /
  product / size / store preferences.
- **Pack size** — `derive_quality_bar` sets `target_grams` from `items.size`
  (seeded from `consumption_habits.typical_size`: milk 2L, yogurt 950g), with a
  per-staple fallback (`_FALLBACK_TARGET_GRAMS`) if size was never seeded.
  `_size_grams` parses g/kg/ml/L. The score band: <0.45× target −14 ("too
  small"), 0.45–1.7× +8 ("right size"), >3× −5 ("oversized"). This is what stops
  a single-serving 200ml milk / 150g yogurt winning on a low per-pack price (the
  qty model is unit-based and pack-size-blind, so without this it just buys more
  small packs). **Caveat:** the validator's item query MUST select `i.size` —
  it's read as `item_row["size"]`; if the SELECT omits it, `target_grams` is
  None and the whole band silently no-ops.
- **Cost efficiency** — `per_unit_price(generic, category, product)` divides
  price by the real unit count (`PRICEY_RULES`, e.g. eggs >3 AED/egg via
  `_pack_count`). Over the ceiling → `score_candidate` −12 and `build_frontend_list`
  sets `isPricey`/`pricedPerUnit` → an amber "Pricey" badge in the app. Flags an
  expensive pack even when it's the only option; whole-word matched so
  "Eggplant" never trips the egg rule.

**Upstream:** orders+deliveries, household_rules, preference_state,
household_profile, product_catalog.
**Downstream:** the Scheduled list the curator sees; the input to cart build.

**Gotchas:** Carrefour `order_items` gaps make captured order dates unreliable →
delivery-anchoring (steps 2–4) compensates; in-flight suppression stops
re-ordering a just-placed item; forward-only rules stop the consumption clock
drifting backward (this was the historical cause of scheduled-list "bloat").

---

## 5. Hot path 2 — In-app chat (the agent)

The curator chats in the app over a WebSocket. The frontend sends
`{text, view}` (view = the tab they're looking at). Three routing tiers:

```
WS frame {text, view} ─► session.active_view = scheduled|cart
   │
   ├─ external-purchase shortcut ("just bought milk") → record + done
   │
   ├─ 1) _try_fast_tool_command  (deterministic, ~0.4s, NO LLM)
   │     clean single "add/remove/skip/set-qty X" → dispatch straight to the
   │     SAME view-aware agent_tools the agent would call. Confirms ONLY from
   │     the real tool status. Anything ambiguous/multi-item/question → None
   │
   ├─ 2) agent_router.route_message → "fast" for approve/cancel/show/etc.
   │     → agent_brain.process_input (legacy regex state machine)
   │
   └─ 3) agent path → store_manager.run_agent_turn  (LLM + tools)
```

**Why three tiers:** tier 1 makes mechanical commands instant (a connect-time
live price-check used to run *inline* before the receive loop, stalling the
first command ~20s — now backgrounded). Tier 3 handles real reasoning.

### The Store Manager agent (`store_manager` + `agent_tools` + `agent_context`)

A Claude (Sonnet) tool-calling loop (bounded by `AGENT_MAX_TOOL_ROUNDS`,
default 7). **19 tools**:

- *List*: `get_shopping_list`, `add_item_to_list`, `remove_item_from_list`,
  `change_item_quantity`, `approve_list`, `reset_list`
- *Products*: `batch_curate`, `search_products`, `explain_recommendation`,
  `get_alternatives`
- *Preferences*: `record_preference`, `get_preferences`, `stop_suggesting_item`,
  `set_item_frequency`, `set_brand_rule`
- *Orders*: `get_order_status`, `cancel_order`, `delay_order`
- *Household*: `get_household_info`

`agent_context` injects **static** context (household profile, organic policy,
portion rules, habits, learned prefs, a **TRUTHFULNESS rule**) and **dynamic**
context (both lists rendered, an **ACTIVE VIEW** block, recent messages). The
truthfulness rule is load-bearing: the agent may claim "done/added/removed"
**only** if a tool returned success this turn, and may quote cadence/qty/price
**only** from tool results.

### View-aware edits

`agent_tools._resolve_edit_list(session)` targets the list the user is viewing:
`active_view='cart'` → the cart; otherwise → the **live** active Scheduled list
(`_active_scheduled_list_id`, re-queried every edit — NOT the cached
`session.suggestion_list_id`, which goes stale when a newer list is generated and
once caused chat to "confirm" edits on an old list while the UI showed the new
one). Every add/remove/change tool routes through it (fast path and agent path
alike), and confirmations come only from a real tool result with a success
status — a `not_found`/`needs_input` tool can't report "Done".

### Chat persistence

`_save_chat_message` writes every user + assistant line; `_load_chat_history`
loads the most recent N **globally** (one household = one conversation) —
deliberately NOT filtered by `list_id`, because the edit tools reassign
`session.list_id` mid-turn and the old per-list filter orphaned every edit
confirmation. The per-connect "Hearthkeeper agent ready" banner is `save=False`
and filtered at load so history stays clean.

### Frontend WS contract (`useWebSocket.js`)

- **Outbound**: `{text, view}` (chat), `{type:"action", action, …}` (UI
  buttons), `{type:"feedback", msg_id, vote}`.
- **Inbound**: `agent`, `user`, `list_data` (vendors + the dual
  suggestion/cart payload), `item_update`, `item_removed`, `chat_history`,
  `order_tracking`, `agent_progress`, `state_change`, `cart_progress`,
  `cart_complete`, `toast`, `feedback_status`, `preferences`, `learning_items`.

**Authority:** `store_manager` (LLM) is the chat brain; `agent_brain` is the
legacy per-session state machine, now used only for the regex fast path
(approve/cancel/show).

---

## 6. Hot path 3 — Cart build → order placement → tracking

### Build (`cart_builder.build_cart`)

```
approved list → live availability refresh (verify_picks → fresh is_available,
   before any cart browser launches) → group by store → preflight_cart (resolve
   each item in-stock, no browser if it can be answered from KB) → launch the
   store's browser → per item: pick product (KB → pinned → catalog search) → add
       (a store add-helper returning None is treated as a clean not_found, never
       a crash) on OOS/404: same-vendor alternatives → cross-vendor rescue (add
       at the other store if its browser is live) → else fail[] with a
       cross-vendor suggestion for a manual swap
   → snapshot pre-existing cart items → verify cart → report
```

Every attempt is logged to `cart_attempts` (`attempt_n`, `reason_class` ∈
`ok/oos/not_found/no_kb/add_failed/login/other`) for forensics.

**Triggering a build:** the in-app "ready cart" action approves all items, then
builds. `/api/admin/build-cart` (session-authed) does the same in the BACKGROUND
— holds `cart_build_active`, records the report in `app_state.last_cart_build`
(poll `/api/admin/build-cart-status`), takes an optional `approve_all` — so a
test cart can be built + WhatsApp'd without the chat UI. It must be backgrounded:
an inline build would exceed Cloudflare's request timeout.

### Browsers

| Store | Engine | Login | Notes |
|---|---|---|---|
| Carrefour UAE | **undetected-chromedriver** (Selenium) | `_uc_ensure_carrefour_login` / `_uc_carrefour_email_login` | Playwright is bot-detected here; `/app/*` routes need step-up re-login |
| Kibsons | **Playwright** | `ensure_logged_in` | subscription-default PDPs need the "one-time" selector |

A single in-memory lock, `app_state.cart_build_active`, serializes the browser
between cart build, the order poller, and the nightly scraper (they share
single-process Chrome profiles). If a build crashes with the lock set, the
poller is blocked until the app restarts.

### The safety boundary (non-negotiable)

**The agent builds carts; the user places the order.** There is no
checkout/place-order code path anywhere. The build stops at a verified cart;
Cem reviews it in the browser and completes the purchase.

### Tracking (`order_scraper`)

`check_and_scrape_orders` (per store) scrapes the order-history pages, inserts
new `orders` + `order_items`, and appends every status change to
`order_status_log`. Status flows roughly `confirmed → processing → dispatched →
delivered`. `delivery_date` (arrival) is tracked separately from `order_date`
(placement); Carrefour cards often show arrival, so the scraper routes
accordingly. `enrich_new_order_items` links raw line-items back to `items`,
feeding the cadence model.

**Known gap:** Carrefour order-detail pages sometimes load empty (an expired
invoice URL / auth redirect yields `chrome-error://…`), so an order saves with
`items=[]`. `_nightly_carrefour_order_detail_backfill` + the on-demand
`backfill_carrefour_order_items` retry it, but if the page won't load the items
stay missing (the order view then shows no line items).

### Order-stage notifications (`order_notifications` + dispatchers)

Placed / out-for-delivery / today / delivered+rating / 48h-reminder
(historically "B2–B6"). Idempotent: each stamps a `notif_*_at` /
`order_status_log.notified_at` **before** sending so a transient failure never
double-sends. **Every send is gated** by `ORDER_NOTIFS_TEST_ONLY` (per-feature)
**and** the global allowlist + quiet hours (§8).

**Upstream:** the approved cart list. **Downstream:** `orders` →
`cadence_calibration` → the next Scheduled list; and the rating prompt that
starts hot path 4.

---

## 7. Hot path 4 — Delivered → feedback → learning (the loop closer)

When an order is delivered, the household rates it (in-app rating sheet, or a
WhatsApp reply). That feedback becomes durable, structured knowledge.

```
1. handle_submit_rating (app) ─ {order_id, per-item thumbs, free-text note}
2. _save_rating ─ order_ratings + order_item_ratings; orders.rating_status=finalized
3. _emit_rating_signals ─ thumbs → preference_signals
      👍 → purchase_observed(brand/product) + item_approve(inclusion)
      👎 → wrong_item(product) + avoid(brand)
4. capture_feedback (household_rules) ─ Haiku parses the free-text note into
   ALL the durable rules it implies (see rule types below) and stores them
5. next cycle: list_generate + list_quality_validator honor the rules;
   preference_state feeds product scoring
```

### household_rules — the durable rule types

| rule_type | meaning | honored by |
|---|---|---|
| `avoid_brand_in_category` | never this brand in this category | validator hard-rejects |
| `prefer_brand_in_category` | favor this brand here | validator +25 |
| `prefer_attribute_in_category` | favor an origin/grade/quality (`value`, e.g. *wagyu, new zealand, australia*) | validator +12, exempt from foreign-word penalty |
| `prefer_cheaper_in_category` | pick the cheaper comparable option | validator weights price harder |
| `prefer_variety` | don't suggest N× the same product | list_generate caps single-product qty |
| `check_flag` | actionable non-preference ("verify the tomato was organic & tag it") | captured + surfaced, doesn't affect scoring |

Category is the household's own word, matched case-insensitively against the
item's category path **and** name. The parser captures *every* rule a note
implies (a "no Angus, prefer wagyu/NZ, pick cheaper, check the tomato" note
yields four rules). `add_household_rule` is the deterministic path used by chat
(`set_brand_rule`).

---

## 8. WhatsApp subsystem

### Outbound — one chokepoint, fail-closed

**Every** outbound message goes through `whatsapp_client.send_message`. After
the `WHATSAPP_ENABLED` check it applies a **global fail-closed allowlist**:

```
if WHATSAPP_ALLOWLIST_ENFORCED and normalize(phone) not in WHATSAPP_ALLOWLIST:
      block → audit-log (whatsapp_messages status='blocked' + app_events) → return None
```

`WHATSAPP_ALLOWLIST` is E.164 numbers from the Ally `.env` only (**never in the
repo**). A second, additive opt-in is the per-member `household_members.
allow_whatsapp` flag (settable from the authenticated app via
`POST /api/admin/whatsapp-allow`), used to approve e.g. the test number
without editing `.env`. The gate allows a recipient on **either** list.
**Both empty + enforced = block everything** — the safe default.
This exists because an open "Ask family" feedback round + the 5-minute
follow-up dispatcher auto-re-pinged a real member (a 17:46 message reached Sara)
against the test-number-only rule. The per-feature `ORDER_NOTIFS_TEST_ONLY`
flag did **not** cover feedback rounds, so this single chokepoint gate is the
real enforcement. **Current state: allowlist empty ⇒ all WhatsApp is blocked
until a number is added to the Ally `.env`.** To go fully live, set
`WHATSAPP_ALLOWLIST_ENFORCED=false`.

### Inbound — webhook → tiered parse → dispatch

```
sidecar ─► POST /webhook/whatsapp (HMAC) ─► get_member_by_phone (phone or @lid)
   ─► feedback_manager.handle_member_reply:
        • Tier-2 ask reply?  (stop/keep/later)        _maybe_handle_tier2_reply
        • rating reply?      (👍/👎/free text/dismiss)  _maybe_handle_rating_reply
        • else active round → whatsapp_agent parse:
              Tier-1 regex (verbs + numbers, typo-tolerant)
              → Tier-3 LLM (parse_with_llm, grounded in the numbered cart +
                household prefs + pending swap) only if Tier-1 returns unknown
        • dispatch actions: skip / have_it / stop_suggesting / swap_request /
              swap_pick / add / remove / confirm / external_purchase / unknown
```

**Out-of-round capture:** a text reply that arrives with NO active feedback round
is still `log_incoming`'d (`round_id=NULL`) before the "no active round" reply, so
replies to an admin/builder question over WhatsApp land in `whatsapp_messages` and
are readable via `/api/admin/whatsapp-messages` — instead of being dropped. The
test member's `wa_lid` is mapped so LID-addressed inbound resolves to them.

### Feedback rounds

`initiate_feedback(list_id)` cancels any open round, creates a new one, and
sends the cart to **all active members** (driver excluded), tracking each in
`feedback_member_status`. `_feedback_timeout_checker` (every ~5 min) sends
follow-up re-pings to non-repliers and closes the round at its deadline. (This
auto-re-ping is exactly what the §8 gate now contains.)

---

## 9. The preference signal pipeline

Every preference-bearing action — app buttons, chat tools, gap analysis (habit
vs actual), ratings, external purchases — funnels into one place:

```
emit_signal(item, type, dimension, old, new, strength)  ─► preference_signals
                                                             (immutable log)
process_pending_signals()  ─► aggregate per dimension (timing/quantity/brand/
                                product/store/attribute/inclusion)
                            ─► preference_state (unique item×dimension×key,
                                with confidence + seed-decay over time)
get_scoring_profile(item)  ─► the dict product scoring + the quality validator read
```

Strong signals (pin product, block brand, wrong item) process immediately;
the rest batch. `preference_state` — not any hardcoded dict — is what the
scorer reads, so the system genuinely learns per item.

---

## 10. Cross-cutting concerns

- **LLM budget** (`llm_budget`): every Anthropic call is recorded to
  `llm_usage` with token + USD cost; a daily/monthly cap blocks calls when
  exceeded (chat agent, feedback parser, WhatsApp LLM tier all respect it).
- **Observability**: `GET /api/diag/observability` + `/admin` Visibility tab —
  cart success, notification counts, LLM spend, errors by source, scheduler
  heartbeats. Plus read-only diag endpoints: `/api/diag/feedback-state`
  (ratings + rules), `/api/diag/whatsapp-debug`, `/api/diag/list-state`,
  `/api/diag/recent-errors`.
- **Events** (`app_events` via `log_event`): the audit trail for everything
  (deploys, blocked sends, rating capture, agent tool errors).
- **Safety boundaries**: (1) agent never places orders; (2) WhatsApp is
  fail-closed to an explicit allowlist; (3) sensitive values (phone numbers,
  secrets) live only in the Ally `.env`, never in the repo.

---

## 11. Module index

| Concern | Module(s) |
|---|---|
| HTTP + WS + diag + deploy | `app.py` |
| Schema | `db_setup.py` |
| Config | `config.py` |
| Background tasks | `scheduler.py`, `watchdog.py` |
| Prediction / list gen | `list_generate.py`, `cadence_calibration.py`, `consumption_habits.py`, `household_profile.py` |
| Product matching / scoring | `list_quality_validator.py`, `product_knowledge.py`, `product_score.py`, `product_grading.py`, `basket_curator.py`, `item_origin_evaluator.py` |
| Preferences / learning | `preference_engine.py`, `household_rules.py`, `skip_tracking.py`, `pattern_detector.py`, `external_purchases.py` |
| Chat agent | `store_manager.py`, `agent_tools.py`, `agent_context.py`, `agent_router.py`, `message_handler.py`, `agent_brain.py` (legacy), `llm_handler.py` |
| Frontend bridge | `frontend_bridge.py`, `frontend/` (React) |
| Cart build / browsers | `cart_builder.py`, `browser_common.py` |
| Order scraping / tracking | `order_scraper.py`, `scrape_carrefour*.py`, `scrape_kibsons*.py` |
| WhatsApp | `whatsapp_client.py`, `feedback_manager.py`, `whatsapp_agent.py`, `notification_prefs.py`, `order_notifications.py`, `whatsapp-service/` (Node) |
| Cost / ops | `llm_budget.py`, `app_logging.py`, `app_state.py` |

---

## 12. Known gotchas

- **`cart_build_active` lock leak** — a crashed build leaves the lock set,
  blocking the order poller until restart.
- **Carrefour `items=[]`** — order-detail pages can load empty (expired URL /
  auth redirect → `chrome-error://`); the order saves with no line items.
- **`WHATSAPP_ENABLED=false` (dev)** — every dispatcher early-returns; nothing
  sends. And even when enabled, the **allowlist blocks all** until configured.
- **`ANTHROPIC_API_KEY` absent (dev)** — LLM parsers return "unknown"/empty;
  you can verify prompt shape but not model behavior. Run LLM checks on the Ally.
- **Cloudflare 1010** — diag endpoints need a browser User-Agent.
- **Forward-only cadence rules** — `last_ordered`/`next_restock` never regress;
  this is what prevents Scheduled-list bloat. Don't "fix" it to overwrite.
- **Idempotent migrations** — never assume a `DEFAULT`-seeding `UPDATE` re-runs;
  it only runs on the first `ALTER`.
- **Skip-marker convention (`list_items.user_action`)** — a skip is either
  SYSTEM-derived (recovers once the cause clears) or a USER decision (sticks).
  SYSTEM skips MUST use `SYSTEM_SKIP_PREFIX = "auto_skipped_"` (`auto_skipped_no_match`,
  `auto_skipped_oos`); `validate_list_quality` un-sticks the whole prefix to
  `pending` at the top of every pass. USER skips use `"skipped"` (chat/app) or
  `"removed"` (WhatsApp/feedback) and are never reset. **Any new automatic skip
  (e.g. an OOS-on-load check) must use the prefix** — using bare `"skipped"`
  makes a transiently-OOS staple indistinguishable from a user drop and it
  lingers forever. (Today OOS flows through `product_catalog.is_available=0` →
  validator no-match → recovers; no current path stuck-skips an item.)

---

## 13. Test infrastructure

- `scripts/e2e_smoke.py` — **344 cases**, no network, runs in seconds. The
  safety net for every refactor. Must pass before any commit touching a hot
  path; add a phase whenever you ship a new dispatcher/parser/scorer/tool.
- `scripts/cart_success_report.py` — Ally-side `cart_attempts` forensics.
- `scripts/test_whatsapp_llm_context.py` — live LLM verification (Ally only).
