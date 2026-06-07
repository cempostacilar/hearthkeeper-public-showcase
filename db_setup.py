"""
AutoRestock - Database Setup (Slice 1)

Creates the SQLite database with all tables needed for the MVP.
Safe to run multiple times — uses "IF NOT EXISTS" so it won't break anything.

Tables:
  stores              - Carrefour, Kibsons, etc.
  items               - Our product catalog (things we buy)
  orders              - Past orders (date, store, total)
  order_items         - What was in each order (links orders to items)
  restock_rules       - How often each item needs restocking
  shopping_lists      - Generated shopping lists
  list_items          - Items on each shopping list
  product_catalog     - Deep product knowledge base (rich, scraped once, updated rarely)
  item_alternatives   - Pre-scored alternatives per item per store (the ranking brain)
  agent_log           - Everything the agent does (for debugging + learning)
"""

import sqlite3
import os

# Database lives in the project folder, right next to the code
DB_PATH = os.path.join(os.path.dirname(__file__), "autostock.db")

# Schema version — persisted in the SQLite file via PRAGMA user_version.
# Bump this when you add a numbered migration inside create_database().
# Migration log:
#   1 -> initial baseline (idempotent CREATE TABLE / ALTER TABLE statements)
#   2 -> drop product_cache (2026-05-27 cleanup, Wave 4):
#        Predecessor of product_catalog. Never written to, no code
#        references (only the CREATE TABLE block in this file).
SCHEMA_VERSION = 3


def create_database():
    """Create all tables. Safe to call multiple times."""

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # --- STORES ---
    # Simple lookup table: which stores do we use?
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stores (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            website_url TEXT,
            categories  TEXT,
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # --- ITEMS (Product Catalog) ---
    # One row per unique product. Built from order history scraping.
    # Think of this as your master list of "things we buy".
    # search_terms = JSON: {"Carrefour UAE": ["exact name 1", "exact name 2"], "Kibsons": [...]}
    #   These are the actual product names to type into the store search bar.
    #   Ordered by preference — first match wins.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            brand           TEXT,
            size            TEXT,
            category        TEXT,
            subcategory     TEXT,
            preferred_store_id INTEGER REFERENCES stores(id),
            avg_price       REAL,
            quality_tier    TEXT,
            origin_country  TEXT,
            product_url     TEXT,
            image_url       TEXT,
            notes           TEXT,
            search_terms    TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        )
    """)

    # --- ORDERS ---
    # Each row = one order placed on a store (e.g., "Carrefour order on Feb 15")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id        INTEGER NOT NULL REFERENCES stores(id),
            order_date      TEXT NOT NULL,
            order_ref       TEXT,
            total_amount    REAL,
            currency        TEXT DEFAULT 'AED',
            status          TEXT DEFAULT 'completed',
            source          TEXT DEFAULT 'scraped',
            scraped_at      TEXT DEFAULT (datetime('now')),
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)

    # --- ORDER ITEMS ---
    # Links orders to items. "In order #5, I bought 2x eggs at 15 AED each"
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    INTEGER NOT NULL REFERENCES orders(id),
            item_id     INTEGER REFERENCES items(id),
            raw_name    TEXT NOT NULL,
            quantity    REAL NOT NULL DEFAULT 1,
            unit_price  REAL,
            total_price REAL,
            brand       TEXT,
            size        TEXT,
            product_url TEXT
        )
    """)

    # --- RESTOCK RULES ---
    # Per-item prediction: "eggs need restocking every 5 days, usually buy 2 packs"
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS restock_rules (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id             INTEGER NOT NULL UNIQUE REFERENCES items(id),
            avg_days_between    REAL,
            avg_quantity        REAL,
            last_ordered_date   TEXT,
            next_restock_date   TEXT,
            confidence          REAL DEFAULT 0.0,
            times_ordered       INTEGER DEFAULT 0,
            updated_at          TEXT DEFAULT (datetime('now'))
        )
    """)

    # --- SHOPPING LISTS ---
    # Generated lists: "Tuesday Feb 25 order"
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shopping_lists (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            target_date TEXT,
            status      TEXT DEFAULT 'draft',
            notes       TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # --- LIST ITEMS ---
    # What's on each shopping list, with user actions (approved, skipped, etc.)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS list_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id         INTEGER NOT NULL REFERENCES shopping_lists(id),
            item_id         INTEGER REFERENCES items(id),
            store_id        INTEGER REFERENCES stores(id),
            quantity        REAL NOT NULL DEFAULT 1,
            estimated_price REAL,
            status          TEXT DEFAULT 'pending',
            user_action     TEXT,
            user_note       TEXT,
            added_by        TEXT DEFAULT 'system',
            created_at      TEXT DEFAULT (datetime('now')),
            item_name_text  TEXT
        )
    """)

    # --- SUGGESTION SNAPSHOTS ---
    # Frozen copy of what the AI originally suggested (never modified after creation).
    # Used to diff against user changes and enable "reset to original".
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS suggestion_snapshots (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id               INTEGER NOT NULL REFERENCES shopping_lists(id),
            item_id               INTEGER REFERENCES items(id),
            store_id              INTEGER REFERENCES stores(id),
            original_qty          REAL NOT NULL DEFAULT 1,
            original_price        REAL,
            original_catalog_id   INTEGER REFERENCES product_catalog(id),
            original_product_name TEXT,
            created_at            TEXT DEFAULT (datetime('now'))
        )
    """)

    # --- SHOPPING LIST EXTENSIONS ---
    # Support for two-entity model (Suggestions + Cart) and persistence.
    # list_type: 'suggestion' (AI prediction) or 'cart' (user's working order)
    # source: how the list was created (auto/chat/reorder)
    # has_user_edits: flipped to 1 on first user modification
    # agent_state: persisted agent state (reviewing/building/etc) for reconnect
    # agent_state_data: JSON blob (failed_items, cart_report, etc.)
    # dismissed_at: soft-delete timestamp (NULL = active)
    for col_def in [
        "list_type TEXT DEFAULT 'suggestion'",
        "source TEXT DEFAULT 'auto'",
        "has_user_edits INTEGER DEFAULT 0",
        "agent_state TEXT",
        "agent_state_data TEXT",
        "dismissed_at TEXT",
    ]:
        try:
            cursor.execute(f"ALTER TABLE shopping_lists ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Add columns to list_items for tracking original state, substitutions, and catalog links
    for col_def in [
        "original_qty REAL",
        "substitution_note TEXT",
        "catalog_id INTEGER REFERENCES product_catalog(id)",
        "failure_context TEXT",       # JSON: reason, reason_human, did_you_mean, stores_checked
        "category_override TEXT",     # category from product catalog (not items table)
    ]:
        try:
            cursor.execute(f"ALTER TABLE list_items ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # --- LLM-evaluated origin-relevance flag (2026-05-27) ---
    # When the formatter renders an item to WhatsApp, it asks
    # "should I show country of origin for this item?" -- show for
    # produce (watermelon's origin tells you something), hide for
    # processed goods (vinegar's origin doesn't). Hand-curated rules
    # cover ~95% of cases but miss edge cases (Italian pasta? Specialty
    # Greek olives? Mexican vanilla?). This LLM verdict per item closes
    # the long tail. Evaluated once, cached forever (until item changes).
    for col_def in [
        "origin_signal_llm INTEGER",     # 1=show, 0=hide, NULL=not yet eval'd
        "origin_signal_reasoning TEXT",  # one-sentence LLM rationale, audit
        "origin_signal_at TEXT",         # when evaluated
    ]:
        try:
            cursor.execute(f"ALTER TABLE items ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # already exists

    # --- PRODUCT CACHE ---
    # --- DROPPED: product_cache (Schema migration v2, 2026-05-27) ---
    # product_cache was an early-design "quick price/availability" table.
    # Superseded by product_catalog (which carries every field we need)
    # and never written to. Removed in the cleanup pass. The DROP is
    # idempotent: if the column is gone, nothing happens. If it still
    # exists on an old deployment, the row count was zero so dropping
    # loses nothing.
    try:
        cursor.execute("DROP TABLE IF EXISTS product_cache")
    except sqlite3.OperationalError:
        pass

    # --- PRODUCT CATALOG ---
    # The deep product knowledge base. Scraped ONCE per product, updated rarely.
    # Contains everything we know about a product: description, ingredients,
    # nutrition, origin — the stuff that tells us WHY it's good or bad.
    #
    # One row = one unique product on one store.
    # Discovered by searching for habit items, but stored permanently.
    #
    # Scraping strategy:
    #   Phase 1 (initial): For each habit item, search store, scrape top 15-20 results DEEP
    #   Phase 2 (order time): Quick availability check on preferred + top 2 alternatives
    #   Phase 3 (on-demand): If preferred unavailable, re-scrape alternatives for that item only
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_catalog (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id        INTEGER NOT NULL REFERENCES stores(id),
            product_name    TEXT NOT NULL,
            product_url     TEXT,
            brand           TEXT,
            price           REAL,
            size            TEXT,
            category        TEXT,
            description     TEXT,
            ingredients     TEXT,
            nutrition_info  TEXT,
            origin_country  TEXT,
            image_url       TEXT,
            is_organic      INTEGER DEFAULT 0,
            is_available    INTEGER DEFAULT 1,
            scraped_at      TEXT DEFAULT (datetime('now')),
            detail_scraped  INTEGER DEFAULT 0,
            UNIQUE(store_id, product_url)
        )
    """)

    # --- ITEM ALTERNATIVES ---
    # Pre-computed rankings: "For Eggs on Carrefour, here are the top alternatives"
    # Built during initial discovery scrape, refreshed periodically.
    #
    # Each row links a habit item to a discovered catalog product with a score.
    # When the preferred product is unavailable, we instantly know the next best option.
    #
    # rank=1 is the preferred pick, rank=2 is first alternative, etc.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS item_alternatives (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id         INTEGER NOT NULL REFERENCES items(id),
            store_id        INTEGER NOT NULL REFERENCES stores(id),
            catalog_id      INTEGER NOT NULL REFERENCES product_catalog(id),
            rank            INTEGER NOT NULL DEFAULT 99,
            score           REAL DEFAULT 0,
            tier            TEXT DEFAULT 'unknown',
            reasons         TEXT,
            is_preferred    INTEGER DEFAULT 0,
            last_scored_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(item_id, store_id, catalog_id)
        )
    """)

    # --- AGENT LOG ---
    # Everything the agent does. For debugging and learning.
    # "At 9:01am, sent WhatsApp check-in. At 9:05am, user replied 'add olive oil'."
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            details     TEXT,
            outcome     TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # --- LLM LOG ---
    # Every LLM interaction: what was asked, what the model returned, user feedback
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS llm_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_text           TEXT NOT NULL,
            agent_state         TEXT,
            model               TEXT,
            parsed_intent       TEXT,
            parsed_actions      TEXT,
            response_text       TEXT,
            prompt_tokens       INTEGER,
            completion_tokens   INTEGER,
            latency_ms          INTEGER,
            user_feedback       TEXT,
            feedback_at         TEXT,
            created_at          TEXT DEFAULT (datetime('now'))
        )
    """)

    # --- NUDGE QUEUE ---
    # Messages the agent wants to send but nobody was connected to receive
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nudge_queue (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            text        TEXT NOT NULL,
            priority    TEXT DEFAULT 'normal',
            delivered_at TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # --- USER ITEM PREFERENCES ---
    # Per-item feedback: blocked brands, pinned products, store switches, wrong-item flags.
    # These override the scoring engine.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_item_prefs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     INTEGER NOT NULL,
            pref_type   TEXT NOT NULL,
            value       TEXT,
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items(id)
        )
    """)
    # pref_type values:
    #   block_brand   — value = brand name (e.g. "Kraft"). Score → -100
    #   pin_product   — value = catalog_id. Bypass scoring, always pick this product.
    #   wrong_item    — value = catalog_id. Penalize this product -50.
    #   prefer_store  — value = store_id. Override preferred_store_id for this item.

    # Household-level rules captured from delivered-order feedback. These are
    # CATEGORY-scoped (not item-scoped), so they don't fit user_item_prefs
    # (item_id NOT NULL). Dedicated table per the "query shape differs" rule.
    #   rule_type = 'avoid_brand_in_category' — brand + category (e.g. no
    #                'Angus' in 'meat'). The quality validator hard-rejects.
    #   rule_type = 'prefer_variety'          — category (e.g. 'pasta'):
    #                diversify instead of N× the same product.
    # `category` is the user's own word; matched case-insensitively against
    # an item's category path AND its name (so "pasta" hits "Pantry > Pasta").
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS household_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_type   TEXT NOT NULL,
            category    TEXT,
            brand       TEXT,
            value       TEXT,
            source      TEXT,
            note        TEXT,
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --- CHAT MESSAGES ---
    # Persistent conversation log — survives page refreshes and reconnects.
    # One flat table for now; can add user_id later.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id     INTEGER,
            role        TEXT NOT NULL,
            text        TEXT NOT NULL,
            metadata    TEXT,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # role: 'user', 'assistant', 'system'
    # metadata: JSON — nudge flag, priority, cart progress, etc.

    # --- ORDER STATUS LOG ---
    # Tracks status transitions for each order over time.
    # e.g., order #42: "confirmed" -> "processing" -> "out_for_delivery" -> "delivered"
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_status_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    INTEGER NOT NULL REFERENCES orders(id),
            old_status  TEXT,
            new_status  TEXT NOT NULL,
            checked_at  TEXT DEFAULT (datetime('now')),
            source      TEXT DEFAULT 'scraper'
        )
    """)
    # source: 'scraper', 'manual', 'browser_extension', 'api'

    # Add columns to orders for status tracking
    for col_def in [
        "last_status_check TEXT",
        "delivery_date TEXT",
        "expected_by TEXT",
        # Carrefour-specific: "NOW" (~30-90 min express) vs "scheduled"
        # (next-day slotted). Empty for stores that don't differentiate
        # (Kibsons) or for older Carrefour orders pre-extraction.
        "delivery_type TEXT",
    ]:
        try:
            cursor.execute(f"ALTER TABLE orders ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Household order-notification tracking (placed / on-the-way / delivered).
    # Per-order, per-stage timestamp so the hourly poller never re-sends. On
    # FIRST add we stamp every existing order as already-notified, so turning
    # the feature on doesn't retro-blast the household for historical orders.
    for col_def in [
        "notif_placed_at TEXT",
        "notif_otw_at TEXT",
        "notif_delivered_at TEXT",
    ]:
        colname = col_def.split()[0]
        try:
            cursor.execute(f"ALTER TABLE orders ADD COLUMN {col_def}")
            cursor.execute(f"UPDATE orders SET {colname} = datetime('now')")
        except sqlite3.OperationalError:
            pass  # Column already exists -- do NOT re-stamp (would mute new orders)

    # B3 -- track whether we've already sent a WhatsApp notification
    # for a given status transition. Without this column the
    # status-update dispatcher would re-send on every sweep until
    # the next transition happened.
    try:
        cursor.execute("ALTER TABLE order_status_log ADD COLUMN notified_at TEXT")
    except sqlite3.OperationalError:
        pass

    # B6 -- one-shot 48h reminder for unanswered rating prompts.
    # If a delivered-order rating ask sits in rating_status='pending'
    # for 48+ hours, the reminder dispatcher sends ONE gentle nudge
    # and stamps rating_reminded_at so we never re-fire.
    try:
        cursor.execute("ALTER TABLE orders ADD COLUMN rating_reminded_at TEXT")
    except sqlite3.OperationalError:
        pass

    # --- EXTERNAL PURCHASES ---
    # Phantom order rows for items the household bought outside our
    # tracked channels (mall, Talabat, neighbor brought it, etc).
    # The cadence calibrator unions these with order_items so the
    # predictor knows "you already have milk this week" even though
    # we never saw it in a Carrefour/Kibsons order.
    # See docs/invisible_shopping_feedback_2026_05_26.md.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS external_purchases (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id       INTEGER REFERENCES items(id),
            raw_name      TEXT NOT NULL,
            purchase_date TEXT NOT NULL,
            quantity      REAL DEFAULT 1,
            source        TEXT,
            member_id     INTEGER REFERENCES household_members(id),
            confidence    REAL DEFAULT 0.8,
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)
    # source: 'chat' (in-app), 'whatsapp' (reply from reviewer),
    #         'receipt_ocr' (future), 'pattern' (future inference).
    try:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_external_purchases_item_date "
            "ON external_purchases(item_id, purchase_date)"
        )
    except sqlite3.OperationalError:
        pass

    # household_members.wa_lid: store WhatsApp's "Linked Identifier" -
    # WhatsApp's new privacy-preserving sender ID. Inbound messages
    # sometimes arrive with `WA_LID_REDACTED@lid` instead of the
    # phone-formatted `+971XXXXXXXXX@c.us`. Without this column the
    # webhook couldn't resolve the sender back to a household_member
    # and would silently drop the message.
    try:
        cursor.execute("ALTER TABLE household_members ADD COLUMN wa_lid TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # household_members.allow_whatsapp: explicit per-member opt-in for outbound
    # WhatsApp. The send gate (whatsapp_client.send_message) permits a recipient
    # only if it's on config.WHATSAPP_ALLOWLIST (.env) OR a member here has
    # allow_whatsapp=1. Default 0 keeps the system fail-closed; flipping it for
    # the test member lets the test number receive messages without editing the
    # Ally .env or putting any number in the repo.
    try:
        cursor.execute("ALTER TABLE household_members ADD COLUMN allow_whatsapp INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists

    # household_members.member_pending_intent: per-member multi-turn state
    # that survives across feedback rounds. feedback_member_status.pending_intent
    # only persists while a round is "collecting"; the Tier-2 ask (E4) is
    # sent outside any round (nightly at 5 AM), so the reply may arrive
    # hours or days later without an active round in flight.
    #
    # JSON shape: {"type": "tier2_ask", "item_id": int, "item_name": str,
    #              "asked_at": ISO-8601 string, "drop_count": int}
    # Cleared when the user replies (stop/keep/later) or when we expire it.
    try:
        cursor.execute("ALTER TABLE household_members ADD COLUMN member_pending_intent TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # B7 -- per-member notification preferences. JSON blob so we can
    # extend the shape without piling on columns.
    # Shape (all keys optional, defaults applied at read time):
    #   {
    #     "notifications": {
    #       "b2_order_placed":     true|false,
    #       "b3_dispatched":       true|false,
    #       "b4_today_deliveries": true|false,
    #       "b5_delivered":        true|false,
    #       "b6_reminder":         true|false
    #     },
    #     "quiet_hours": {"start": 22, "end": 7}
    #   }
    # NULL/missing means "use defaults" -- which preserves the
    # current behaviour (driver gets all notifications, 22-07 quiet).
    try:
        cursor.execute("ALTER TABLE household_members ADD COLUMN notification_prefs TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # --- CART ATTEMPTS ---
    # Per-item, per-attempt log of every add-to-cart try. Lets us
    # answer "WHY did this item fail?" after the fact (previously the
    # store_report dict was thrown away when build_cart returned).
    # Tier 0 = primary KB pick. Tier 1+ = OOS/404 fallback alternatives.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cart_attempts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id       INTEGER REFERENCES shopping_lists(id),
            list_item_id  INTEGER REFERENCES list_items(id),
            item_id       INTEGER REFERENCES items(id),
            store_id      INTEGER REFERENCES stores(id),
            attempt_n     INTEGER NOT NULL DEFAULT 0,
            url           TEXT,
            success       INTEGER NOT NULL DEFAULT 0,
            reason        TEXT,
            reason_class  TEXT,   -- 'ok' | 'oos' | 'not_found' | 'no_kb' | 'add_failed' | 'other'
            duration_ms   INTEGER,
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)
    try:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_cart_attempts_list_id "
            "ON cart_attempts(list_id, created_at)"
        )
    except sqlite3.OperationalError:
        pass

    # orders.detail_url -- store the exact href Carrefour gives us
    # on the order summary page so the backfill scraper can revisit
    # it without guessing a URL format. Carrefour uses
    # /mafuae/en/app/orders/{ref}/... but the exact suffix varies;
    # persisting the original href avoids re-parsing surprises.
    # Kibsons orders also benefit -- same pattern.
    try:
        cursor.execute("ALTER TABLE orders ADD COLUMN detail_url TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # --- LLM USAGE ---
    # One row per Anthropic call, with token counts + estimated cost in
    # USD. Used by llm_budget.py to enforce a daily + monthly cap so
    # natural-language fallbacks can be turned on without runaway spend.
    # intent tags the call site so we can see what's burning the budget
    # (whatsapp_parse, rating_feedback, agent_chat, etc.).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            provider      TEXT NOT NULL DEFAULT 'anthropic',
            model         TEXT NOT NULL,
            intent        TEXT,
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd      REAL NOT NULL DEFAULT 0,
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)
    # Index for the fast "today's total" and "MTD total" queries the
    # budget guard runs on every call.
    try:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_usage_created_at ON llm_usage(created_at)"
        )
    except sqlite3.OperationalError:
        pass

    # --- SKIP EVENTS ---
    # Immutable log of "I don't want this on the list right now" actions
    # by the curator (app) or reviewer (WhatsApp). Drives Tier-0 push of
    # next_restock_date, pattern detection (Tier 1+2), and trust signals.
    # See docs/invisible_shopping_feedback_2026_05_26.md.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS skip_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id      INTEGER REFERENCES items(id),
            list_id      INTEGER REFERENCES shopping_lists(id),
            list_item_id INTEGER REFERENCES list_items(id),
            source       TEXT NOT NULL,
            reason       TEXT,
            member_id    INTEGER REFERENCES household_members(id),
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    # source: 'curator_drop' | 'reviewer_drop' | 'reviewer_have_it'
    #         'reviewer_wrong' | 'pattern_detected' | 'reactivation'
    # reason: optional structured reason ('have_it', 'wrong_pick',
    #         'this_week_only', 'stop_suggesting')
    try:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_skip_events_item "
            "ON skip_events(item_id, created_at)"
        )
    except sqlite3.OperationalError:
        pass

    # cadence_adjustment: multiplicative on top of avg_days_between.
    # Starts at 1.0; pattern detector bumps to 1.25 / 1.5 / etc when
    # the same item is repeatedly skipped. Reset to 1.0 on reactivation.
    try:
        cursor.execute(
            "ALTER TABLE restock_rules ADD COLUMN cadence_adjustment REAL DEFAULT 1.0"
        )
    except sqlite3.OperationalError:
        pass  # already exists

    # --- PREFERENCE SIGNALS ---
    # Immutable event log of every user action that implies a preference.
    # All input channels (app UI, chat, gap analysis, offline) write here.
    # The preference engine aggregates these into preference_state.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS preference_signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id         INTEGER REFERENCES items(id),
            signal_type     TEXT NOT NULL,
            signal_source   TEXT NOT NULL,
            dimension       TEXT NOT NULL,
            old_value       TEXT,
            new_value       TEXT,
            strength        REAL DEFAULT 0.5,
            context         TEXT,
            is_processed    INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    # signal_type: qty_change, item_skip, item_add, item_approve,
    #   pin_product, block_brand, wrong_item, prefer_store,
    #   brand_pref_stated, attribute_pref_stated,
    #   gap_matched, gap_removed, gap_added, gap_qty_changed, gap_substituted,
    #   purchase_observed, offline_purchase, seed_baseline
    # signal_source: app_ui, chat, gap_analysis, offline, seed
    # dimension: timing, quantity, brand, product, store, attribute, item_inclusion

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_unprocessed
        ON preference_signals(is_processed, created_at)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_item
        ON preference_signals(item_id, dimension)
    """)

    # --- PREFERENCE STATE ---
    # Aggregated current preferences. One row per (item_id, dimension, key).
    # The scoring engine reads from this table, not from hardcoded dicts.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS preference_state (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id           INTEGER NOT NULL REFERENCES items(id),
            dimension         TEXT NOT NULL,
            key               TEXT NOT NULL,
            value             TEXT NOT NULL,
            confidence        REAL DEFAULT 0.0,
            observation_count INTEGER DEFAULT 0,
            source_type       TEXT DEFAULT 'seed',
            last_signal_at    TEXT,
            updated_at        TEXT DEFAULT (datetime('now')),
            UNIQUE(item_id, dimension, key)
        )
    """)
    # dimension: timing, quantity, brand, product, store, attribute, item_inclusion
    # key examples: avg_days_between, avg_quantity, preferred, avoid, require,
    #   prefer, pinned, blocked, price_range, flexibility, active
    # source_type: seed (from initial setup), learned (from real data), mixed (blend)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_prefstate_item
        ON preference_state(item_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_prefstate_lookup
        ON preference_state(item_id, dimension)
    """)

    # Add source_type column to restock_rules
    try:
        cursor.execute("ALTER TABLE restock_rules ADD COLUMN source_type TEXT DEFAULT 'seed'")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # --- ORDER RATINGS ---
    # Per-order ratings submitted after delivery.
    # Links to order_item_ratings for per-item feedback.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_ratings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id        INTEGER NOT NULL UNIQUE REFERENCES orders(id),
            overall_text    TEXT,
            llm_response    TEXT,
            status          TEXT DEFAULT 'pending',
            created_at      TEXT DEFAULT (datetime('now')),
            finalized_at    TEXT
        )
    """)
    # status: pending (started but not finalized), finalized, dismissed

    # --- ORDER ITEM RATINGS ---
    # Per-item feedback within an order rating (thumbs up/down).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_item_ratings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            rating_id       INTEGER NOT NULL REFERENCES order_ratings(id),
            order_item_id   INTEGER NOT NULL REFERENCES order_items(id),
            item_id         INTEGER REFERENCES items(id),
            feedback        TEXT
        )
    """)
    # feedback: thumbs_up, thumbs_down, or NULL (no feedback given)

    # Add rating_status column to orders
    try:
        cursor.execute("ALTER TABLE orders ADD COLUMN rating_status TEXT DEFAULT 'none'")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # rating_status: none, pending, finalized, dismissed

    # --- RSC scraper columns for product_catalog ---
    # Rich structured data from Kibsons RSC (React Server Components) payloads.
    # These fields come directly from the API, no heuristic parsing needed.
    for col_def in [
        "sku TEXT",                     # Kibsons stockCode (e.g. "EGGOETROR6PCA1")
        "unit TEXT",                    # Price unit: "tray", "pack", "kg"
        "regular_price REAL",           # Full price before any discount
        "sale_price REAL DEFAULT 0",    # Promo price (0 if no sale)
        "transport_route TEXT",         # "BY AIR", "BY SEA", "LOCAL" etc.
        "popularity_score REAL DEFAULT 0",  # Kibsons pl_sku_popularity_score
        "is_fresh INTEGER DEFAULT 0",   # Fresh produce flag
        "badges TEXT",                  # JSON: ["Organic","Eco-Friendly"]
        "featured_tags TEXT",           # JSON: ["Organic","Ramadan"]
        "search_keywords TEXT",         # Kibsons search keywords string
        "external_id TEXT",             # Kibsons internal product id
    ]:
        try:
            cursor.execute(f"ALTER TABLE product_catalog ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Index for SKU lookups
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_catalog_store_sku
        ON product_catalog(store_id, sku)
    """)

    # --- HOUSEHOLD PREFERENCES ---
    # Global preferences that apply to ALL items (not per-item).
    # Used by the Book Keeper as a fallback when scoring catalog-only items
    # that don't have per-item preference profiles.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS household_preferences (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            pref_key    TEXT NOT NULL UNIQUE,
            pref_value  TEXT NOT NULL,
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # Seed household preferences (only if table is empty — don't overwrite user changes)
    existing = cursor.execute("SELECT COUNT(*) FROM household_preferences").fetchone()[0]
    if existing == 0:
        household_defaults = [
            ("prefer_organic", "true"),
            ("prefer_keywords", "organic,free range,grass fed"),
            ("avoid_keywords", ""),
            ("prefer_local", "false"),
        ]
        cursor.executemany(
            "INSERT INTO household_preferences (pref_key, pref_value) VALUES (?, ?)",
            household_defaults,
        )

    # ── WHATSAPP FEEDBACK SYSTEM ──

    # --- HOUSEHOLD MEMBERS ---
    # People who can provide feedback on grocery carts via WhatsApp.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS household_members (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            display_name TEXT,
            phone        TEXT NOT NULL UNIQUE,
            role         TEXT DEFAULT 'member',
            is_active    INTEGER DEFAULT 1,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    # role: "driver" (Cem), "member" (Sara, Max)

    # Seed household members (only if empty)
    existing_members = cursor.execute("SELECT COUNT(*) FROM household_members").fetchone()[0]
    if existing_members == 0:
        cursor.executemany(
            "INSERT INTO household_members (name, display_name, phone, role) VALUES (?, ?, ?, ?)",
            [
                ("Cem", "Cem", "+971XXXXXXXXX", "driver"),
                ("Sara", "Sara", "+971XXXXXXXXX", "member"),
                ("Max", "Max", "+971XXXXXXXXX", "member"),
            ],
        )

    # --- FEEDBACK ROUNDS ---
    # One per feedback request cycle. Cem taps "Request Feedback" → new round.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback_rounds (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id         INTEGER NOT NULL REFERENCES shopping_lists(id),
            status          TEXT DEFAULT 'pending',
            initiated_at    TEXT DEFAULT (datetime('now')),
            deadline_at     TEXT,
            completed_at    TEXT,
            followup_count  INTEGER DEFAULT 0,
            notes           TEXT
        )
    """)
    # status: pending → collecting → complete/timeout/cancelled

    # feedback_member_status.pending_intent: per-member multi-turn state
    # (e.g. "I asked the reviewer to pick A/B/C among 3 alternatives,
    # waiting for their reply"). JSON: {kind, list_item_id, item_id,
    # ref_number, options: [{catalog_id, name, ...}]}. Cleared on
    # next valid action.
    # Lives on feedback_member_status (not the round) since two members
    # in the same round can have independent pending swaps.

    # --- FEEDBACK MEMBER STATUS ---
    # Per-member tracking within a round.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback_member_status (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id        INTEGER NOT NULL REFERENCES feedback_rounds(id),
            member_id       INTEGER NOT NULL REFERENCES household_members(id),
            status          TEXT DEFAULT 'pending',
            notified_at     TEXT,
            replied_at      TEXT,
            followup_sent   INTEGER DEFAULT 0,
            UNIQUE(round_id, member_id)
        )
    """)
    # status: pending → notified → replied → done/no_reply

    # Idempotent migration for pending_intent (multi-turn swap state).
    try:
        cursor.execute("ALTER TABLE feedback_member_status ADD COLUMN pending_intent TEXT")
    except sqlite3.OperationalError:
        pass  # already exists

    # --- FEEDBACK ITEMS ---
    # Individual add/remove suggestions from household members.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id        INTEGER NOT NULL REFERENCES feedback_rounds(id),
            member_id       INTEGER NOT NULL REFERENCES household_members(id),
            action_type     TEXT NOT NULL,
            item_text       TEXT NOT NULL,
            resolved_name   TEXT,
            catalog_id      INTEGER REFERENCES product_catalog(id),
            item_id         INTEGER REFERENCES items(id),
            store_id        INTEGER REFERENCES stores(id),
            quantity        INTEGER DEFAULT 1,
            price           REAL,
            status          TEXT DEFAULT 'pending',
            list_item_id    INTEGER REFERENCES list_items(id),
            created_at      TEXT DEFAULT (datetime('now')),
            resolved_at     TEXT
        )
    """)
    # action_type: "add" or "remove"
    # status: pending → approved/rejected/clarifying/unresolved

    # --- WHATSAPP MESSAGES ---
    # Full audit log of all WhatsApp messages sent and received.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_messages (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id          INTEGER REFERENCES feedback_rounds(id),
            member_id         INTEGER REFERENCES household_members(id),
            direction         TEXT NOT NULL,
            wa_message_id     TEXT UNIQUE,
            message_type      TEXT DEFAULT 'text',
            content           TEXT NOT NULL,
            status            TEXT DEFAULT 'sent',
            error_detail      TEXT,
            created_at        TEXT DEFAULT (datetime('now')),
            status_updated_at TEXT
        )
    """)
    # direction: "outbound" / "inbound"
    # status: sent/delivered/read/failed/received

    # Add feedback_item_id to list_items (links approved feedback to its source)
    try:
        cursor.execute("ALTER TABLE list_items ADD COLUMN feedback_item_id INTEGER REFERENCES feedback_items(id)")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # --- ITEM RESOLUTION ATTEMPTS (2026-05-27) ---
    # Every time the quality validator agent evaluates an item-to-product
    # match it writes a row here. Lets subsequent list-generation cycles
    # *read history* and shortcut decisions (e.g. "organic was OOS at
    # Carrefour the last 3 cycles in a row -> start with Kibsons this
    # cycle, don't waste the round-trip"). Also gives operators an
    # auditable trail of "why did the agent pick Kibsons for cucumber
    # this week instead of Carrefour".
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS item_resolution_attempts (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id                  INTEGER NOT NULL REFERENCES items(id),
            list_id                  INTEGER REFERENCES shopping_lists(id),
            list_item_id             INTEGER REFERENCES list_items(id),
            attempted_at             TEXT DEFAULT (datetime('now')),
            quality_bar              TEXT,         -- JSON: criteria the agent required
            store_id_tried           INTEGER REFERENCES stores(id),
            candidates_total         INTEGER DEFAULT 0,
            candidates_meeting_bar   INTEGER DEFAULT 0,
            candidates_in_stock      INTEGER DEFAULT 0,
            outcome                  TEXT,         -- 'matched' | 'switched_store' |
                                                   -- 'no_in_stock' | 'no_match' |
                                                   -- 'flagged' | 'scrape_queued'
            resolved_catalog_id      INTEGER REFERENCES product_catalog(id),
            resolved_store_id        INTEGER REFERENCES stores(id),
            notes                    TEXT          -- human-readable reason
        )
    """)
    try:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_resolution_attempts_item "
            "ON item_resolution_attempts(item_id, attempted_at DESC)"
        )
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_resolution_attempts_list "
            "ON item_resolution_attempts(list_id)"
        )
    except sqlite3.OperationalError:
        pass

    # --- Schema version ---
    # The idempotent CREATE TABLE / ALTER TABLE statements above always bring
    # any database (fresh or existing) up to the current baseline schema.
    # Stamp the version so future schema changes can be applied as numbered,
    # version-gated migrations rather than more bare ALTER attempts, e.g.:
    #     if db_version < 2:
    #         cursor.execute("ALTER TABLE ...")
    # then bump SCHEMA_VERSION at the top of this file.
    cursor.execute("PRAGMA user_version")
    db_version = cursor.fetchone()[0]
    if db_version != SCHEMA_VERSION:
        cursor.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        if db_version:
            print(f"Schema version upgraded: {db_version} -> {SCHEMA_VERSION}")

    # Seed the user-confirmed delivery cost model once (idempotent; guarded by a
    # flag so it never clobbers later Settings edits). Lazy import — by the time
    # this runs db_setup is fully loaded, so there's no import cycle.
    try:
        from delivery_optimizer import ensure_delivery_model_seeded
        if ensure_delivery_model_seeded(conn):
            print("Seeded delivery cost model (Kibsons flat 8, Carrefour 150/12)")
    except Exception as e:
        print(f"delivery model seed skipped: {e}")

    conn.commit()
    conn.close()
    print(f"Database created at: {DB_PATH}")
    return DB_PATH


if __name__ == "__main__":
    create_database()
