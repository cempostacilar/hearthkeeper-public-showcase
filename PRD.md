# Hearthkeeper MVP - Product Requirements Document

## Problem Statement
Restocking day-to-day groceries in a household is repetitive, time-consuming, and error-prone. By the time you notice something is missing, it's already a problem causing friction. The task itself isn't complex — it's just tedious: checking what's low, browsing stores, comparing options, building carts, and doing it all over again every few days.

## Solution
An AI agent that learns your household's consumption patterns from order history, predicts when items need restocking, checks in with you conversationally, builds carts on the right stores, and hands you a one-click-away purchase.

## MVP Scope

### Users
- **Driver** (Cem) — primary operator, receives all check-ins, reviews and places orders
- **Supporter** role (e.g., wife) — designed for but NOT built in MVP. Future: provides feedback, approves lists

### Stores (MVP)
| Store | Categories | Notes |
|-------|-----------|-------|
| **Kibsons** | Dairy, fresh (fruits & veg), meats | Organic eggs if Carrefour doesn't have them |
| **Carrefour UAE** | Everything else | Default store for most items |

Amazon UAE deferred to post-MVP.

### Order Frequency
- **2 orders per week**: Friday and Tuesday
- User-configurable in the future

---

## Core Flows

### Flow 1: Order History Seeding (One-time + Periodic)
**Goal:** Build the consumption baseline from real purchase data.

1. Agent opens store website (Carrefour, Kibsons) via browser automation
2. Navigates to order history section
3. Scrapes all available past orders:
   - Date, items, quantities, prices, brands, sizes, product URLs
   - Store and category info
4. Data stored locally in SQLite
5. Periodic re-scrape to capture orders placed outside the system

**Additional data source:** Order confirmation emails can also be scraped/parsed to capture order data — useful as a fallback or cross-reference when website scraping misses something.

**Key rule:** Any order placed directly by the user (not through the system) should be detected on next scrape and factored into the baseline.

### Flow 2: Baseline & Prediction
**Goal:** Predict what the household needs and when.

1. Analyze order history to calculate per-item consumption rate
   - E.g., "6 eggs every 5 days", "1kg chicken breast every 7 days"
2. Factor in:
   - Average quantity per order
   - Time between orders for each item
   - Preferred brands/variants (organic, free-range, country of origin)
   - Preferred store per item
3. Generate a restock schedule per item
4. Confidence improves over 3-4 weeks of data collection
5. Seasonal patterns (Ramadan, Eid, summer) — designed for, not built in MVP

### Flow 3: Check-in (Conversational)
**Goal:** Quick, non-intrusive check-in before each order.

**Channel:** Web UI at hearthkeeper.cc (future: WhatsApp)

**Tone:** Personal assistant, human-like. NOT a checklist.

**Two interaction models:**

**A. Suggestions Tab (AI-driven):**
- AI generates predicted items based on consumption model
- User reviews in the React frontend
- Can adjust quantities, skip items, or approve the whole list
- One-tap "Ready Suggested" to trigger cart building

**B. Cart Tab (Chat-driven):**
- User tells the Store Manager agent what they need
- "I need eggs, milk, and bananas from Kibsons"
- Agent finds products, creates cart, confirms with details
- One-tap "Ready Custom" to trigger cart building

### Flow 4: Cart Preparation
**Goal:** Build real carts on store websites, ready for one-click purchase.

**Dual-browser architecture:**
| Store | Browser | Reason |
|-------|---------|--------|
| Carrefour UAE | UC Chrome (undetected-chromedriver) | Bypasses Akamai Bot Manager |
| Kibsons | Playwright (Chromium) | No bot detection issues |

**Build flow:**
1. For each item, find the best product from the knowledge base
2. Navigate to the product page on the store website
3. Check availability — if out of stock, try next ranked alternative
4. Add to cart and set correct quantity
5. Verify the item was added (quantity widget appears)
6. Move to next item
7. Report results: items added, items failed, estimated totals

**Key technical details:**
- UC Chrome uses a separate profile (`~/.hearthkeeper-carrefour/`) with persistent login
- Stale Chrome processes are auto-killed before each build (PowerShell cleanup)
- Carrefour's "Add to Cart" text is inside a child `<div>` — requires XPath `contains(., ...)` not `contains(text(), ...)`
- In-cart detection via `button[role="combobox"]` (quantity dropdown widget)
- OOS detection via case-insensitive XPath with `translate()`

### Flow 5: The 80/20 Handoff
**Goal:** Don't let problem items block the whole order.

1. Agent prepares cart with everything it can find (the 80%)
2. For problem items (out of stock, no good match, no organic option):
   - Reports in the build summary with per-store breakdown
   - Shows which items need attention
3. User can address problem items via chat or skip them

### Flow 6: Order Review & Checkout
**Goal:** User reviews and places order themselves.

1. Agent reports cart is ready with per-store summary and estimated totals
2. User opens store website, reviews cart
3. User places order themselves
4. Agent NEVER clicks "Place Order"
5. After order window, agent can scrape to confirm what was ordered

### Flow 7: Suggestion Pipeline (Review & Reset)
**Goal:** Let the user review, modify, and reset the AI-generated shopping list before cart building.

1. AI generates a suggestion list based on prediction engine + restock rules
2. A **suggestion snapshot** is frozen — captures original quantities, prices, and product picks
3. **Pre-availability check** runs before showing the list:
   - For each item, checks if the preferred product is available
   - If unavailable, substitutes with next best alternative
   - Writes substitution note (e.g., "Instead of X (out of stock) -> Y")
4. User reviews in the React frontend:
   - Can change quantities, skip/remove items, add new items via chat
   - Substituted items show orange badge with explanation
5. **Diff tracking** compares current list against original snapshot
6. User can tap **Reset** to restore original AI suggestion
7. When satisfied, user taps **Ready** to approve and proceed to cart building

### Flow 8: Store Manager Agent (Chat-Driven)
**Goal:** Natural language grocery management via conversational AI.

**Architecture:** Claude-powered agent with 16 tools

**Capabilities:**
- "I need eggs and milk from Kibsons" → searches catalog, creates cart
- "Add olive oil" → finds best match, adds to cart
- "What's the cheapest butter?" → compares options across stores
- "Remove the pasta" → removes from cart
- Multi-store requests in a single message
- Automatic product matching against household preferences

**Agent loop:** User message → tool selection → execution → confirmation response

### Minimum Order Logic
- Before finalizing, agent checks if a store's cart is "worth it"
- If a store only has 1-2 items / low total → flag to user
- User decides whether to consolidate to another store

---

## Product Information Requirements
When presenting products (in alternatives, summaries, or check-ins), include:
- Product image
- Title / description
- Price
- Weight / size / quantity
- Brand
- Origin / country (especially for meat, dairy)
- Quality tier (organic, free-range, conventional)
- Availability status
- Store

---

## Preferences & Learning

### What the system learns over time:
- Per-item consumption rate and frequency
- Brand preferences (e.g., Al Marai milk, not Nada)
- Quality preferences (e.g., organic eggs, Australian meat)
- Store preferences per category
- Price sensitivity per category
- Items that get skipped often (maybe remove from baseline)
- Items that get manually added often (maybe add to baseline)
- Seasonal patterns (future)

### Preference hierarchy for product matching:
1. Exact match (same brand, same variant, same store)
2. Same brand, different size/quantity
3. Same quality tier, different brand
4. Different quality tier -> flag to user, don't auto-substitute

### Product Scoring (100-point system)
Each product is scored against a preference profile:
- **Name relevance** (+15 / -20) — prevents cross-category errors
- **Required attributes** (+10 / -40) — deal-breakers like "organic"
- **Preferred keywords** (+8 each) — nice-to-haves
- **Avoid keywords** (-15 each) — unwanted attributes
- **Brand matching** (+20 / -20) — per household preference
- **Variety matching** (+12) — produce types
- **Unit price** (+/-5) — normalized per kg/L/piece
- **Purchase history** (+3 to +15) — previously ordered products

Tiers: preferred (75+), acceptable (55-74), fallback (35-54), avoid (<35)

---

## Technical Constraints

### Browser Automation
- **Carrefour:** UC Chrome (undetected-chromedriver) — required to bypass Akamai Bot Manager
- **Kibsons:** Playwright (Chromium) — no bot detection issues
- Both use persistent browser profiles for login persistence
- Agent handles: search, navigation, scraping, cart building
- Agent does NOT handle: payment, OTP, "Place Order" click

### Data & Privacy
- All data stored locally in SQLite
- No cloud services for data storage
- Order history, preferences, consumption patterns — all local
- Claude API used only for Store Manager agent (text-only, no personal data sent)

### Human-in-the-Loop (Non-negotiable)
- Agent never places an order
- Agent never makes a payment
- Every cart requires user review before checkout

---

## Implementation Status (as of 2026-02-27)

### Completed
| Component | Status | Details |
|-----------|--------|---------|
| **Database schema** | Done | 17+ tables in SQLite |
| **Order history scraper** | Done | `order_scraper.py` — scrapes Carrefour + Kibsons order history |
| **RSC product scrapers** | Done | `scrape_carrefour_rsc.py` (15,719 products in 3 min), `scrape_kibsons_rsc.py` (8,700+ products) |
| **Product scoring engine** | Done | `product_score.py` — 100-point scoring with grading tags |
| **Product grading** | Done | `product_grading.py` — quality tags ([ORG], [FR], [GF], etc.) |
| **Product knowledge base** | Done | `product_knowledge.py` — 3-phase discovery with pre-availability check |
| **Basket curator** | Done | `basket_curator.py` — cross-vendor optimization |
| **Store Manager agent** | Done | `store_manager.py` — 16-tool Claude-powered conversational agent |
| **Agent router + tools** | Done | `agent_router.py`, `agent_tools.py`, `agent_context.py` |
| **Catalog intelligence** | Done | `catalog_intelligence.py` — catalog analysis and insights |
| **Household profile** | Done | `household_profile.py` — consumption pattern modeling |
| **Web server** | Done | `app.py` — FastAPI + WebSocket, two-entity data model, toast notifications |
| **Cart builder** | Done | `cart_builder.py` — UC Chrome (Carrefour) + Playwright (Kibsons), stale process cleanup, OOS fallback |
| **React frontend** | Done | Two-tab layout (Suggestions + Cart), SkuCard with qty controls, StickyActionBar, ChatPanel, VendorSection |
| **Frontend data bridge** | Done | `frontend_bridge.py` — DB to React JSON with diff tracking |
| **Suggestion snapshots** | Done | Frozen original suggestions for diff/reset |
| **Pre-availability check** | Done | Auto-substitution with human-readable notes |
| **Structured logging** | Done | `app_logging.py` — rotating log file, event tracking |

### E2E Test Results (Feb 27, 2026)
| Test | Result | Items |
|------|--------|-------|
| Suggested build | PASS | 10/12 |
| Chat build (Kibsons) | PASS | 3/3 |
| Suggested repeat | PASS | 10/12 |
| Chat multi-store | PASS | 3/3 |
| Chat single item | PASS | 1/1 |
| **Total** | **PASS** | **27/31 (87%)** |

Failures: Cucumber and Pepper (knowledge base gaps — no catalog match).

### Error Handling & Fallbacks
| Scenario | Handling |
|----------|----------|
| **Stale Chrome processes** | `_kill_stale_uc_chrome()` kills zombies via PowerShell before launch |
| **UC Chrome launch fails** | Falls back to Playwright (may fail on Akamai-protected stores) |
| **Product out of stock** | Marks unavailable in DB, tries next ranked alternative |
| **LLM unavailable** | Regex intent detection handles most commands |
| **Pre-availability check fails** | Logged as warning, list shown without substitution notes |
| **WebSocket disconnects** | Auto-reconnect with exponential backoff |
| **Scraper partial failure** | `--retry-failed` mode re-scrapes only failed items |

### What's Next
1. **UI/UX refinements** — tab independence (stop auto-switching), compact SkuCard, in-cart indicators
2. **Price accuracy** — compare against last ordered price, not avg_price across sizes
3. **OOS auto-suggest** — automatically suggest alternatives from item_alternatives
4. **Scheduler** — auto check-in Mon/Thu at 8 AM
5. **WhatsApp integration** — send check-ins and cart summaries via WhatsApp
6. **Learning loop** — feed order outcomes back into restock_rules
7. **Full inventory scrape** — catalog all 24K+ products with enriched metadata

---

## Out of MVP Scope (Designed for, not built)
- Amazon UAE as a vendor
- Mobile app
- WhatsApp Business API (using browser automation instead)
- Fridge cam / visual inventory
- TV screen time monitor
- Seasonal pattern detection
- "Guests coming over" overrides
- Fully autonomous agent (always-on Jetson device)
- Multi-user household tracking
