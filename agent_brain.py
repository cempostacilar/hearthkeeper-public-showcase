"""
AutoRestock - Agent Brain

The central state machine that drives the entire restock flow.
This is the "brain" -- it decides what to do next based on:
  - Where we are in the order cycle
  - What the user said
  - What went wrong

States:
  IDLE          -> waiting for next order cycle
  CHECKIN       -> sent check-in message, waiting for user reply
  REVIEWING     -> processing user reply, updating list
  BUILDING      -> building carts on store websites
  HANDOFF       -> reporting results, handling failed items
  AWAITING_ALT  -> waiting for user to pick alternatives for failed items
  POST_ORDER    -> scraping actual order, running gap analysis
  DONE          -> cycle complete

The agent talks through whatever channel is available:
  Phase 1 (now):  CLI / terminal
  Phase 2:        Web UI (FastAPI)
  Phase 3:        WhatsApp

Usage:
  python3 agent_brain.py                    # run one full cycle
  python3 agent_brain.py --dry-run          # preview without cart building
  python3 agent_brain.py --date 2026-02-25  # specific date
  python3 agent_brain.py --status           # show current agent state
"""

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta

from db_setup import DB_PATH, create_database
from list_generate import list_generate, list_get, list_update_item, list_add_item, list_finalize
from message_handler import parse_message, match_item_to_db, Intent
from cart_analysis import save_cart_snapshot, analyze_gap, apply_learnings, ensure_tables as ensure_analysis_tables


# ============================================================
# AGENT STATES
# ============================================================

class State:
    IDLE = "idle"
    CHECKIN = "checkin"
    REVIEWING = "reviewing"
    BUILDING = "building"
    HANDOFF = "handoff"
    AWAITING_ALT = "awaiting_alt"
    POST_ORDER = "post_order"
    DONE = "done"


# ============================================================
# AGENT SESSION
# ============================================================

class AgentSession:
    """
    Holds the state for one order cycle.
    Persisted to DB so we can resume after restarts.

    Two-entity model:
      suggestion_list_id = AI's predicted shopping list (auto-generated, refreshes over time)
      cart_list_id       = User's working order (explicitly created via chat or button)
      list_id            = ACTIVE list (backward compat — points to cart if exists, else suggestion)
    """
    def __init__(self):
        self.state = State.IDLE
        self.list_id = None                   # Active list (backward compat)
        self.suggestion_list_id = None        # AI prediction list
        self.cart_list_id = None              # User's working order
        self.target_date = None
        self.cart_report = None
        self.failed_items = []       # items needing user decision
        self.current_failed_idx = 0  # which failed item we're asking about
        self.messages_sent = []      # log of what we said
        self.messages_received = []  # log of what user said
        self.pending_suggestions = None  # catalog search results awaiting user approval
        self.dry_run = False
        self.started_at = None
        self.completed_at = None

    @property
    def active_list_id(self):
        """The list that chat/tools should target: cart if exists, else suggestion."""
        return self.cart_list_id or self.suggestion_list_id

    def to_dict(self):
        return {
            "state": self.state,
            "list_id": self.list_id,
            "suggestion_list_id": self.suggestion_list_id,
            "cart_list_id": self.cart_list_id,
            "target_date": self.target_date,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "failed_items_count": len(self.failed_items),
            "current_failed_idx": self.current_failed_idx,
            "messages_sent": len(self.messages_sent),
            "messages_received": len(self.messages_received),
        }


# ============================================================
# AGENT BRAIN
# ============================================================

class AgentBrain:
    """
    The central orchestrator. Call run_cycle() for a full order flow,
    or step through manually with process_input() for conversational mode.
    """

    def __init__(self, dry_run=False):
        self.session = AgentSession()
        self.session.dry_run = dry_run
        self.conn = None
        self._output_callback = None  # function to send messages to user

    def set_output(self, callback):
        """Set the function used to send messages to the user."""
        self._output_callback = callback

    def say(self, message):
        """Send a message to the user through whatever channel is active."""
        self.session.messages_sent.append({
            "text": message,
            "time": datetime.now().isoformat(),
        })
        if self._output_callback:
            self._output_callback(message)
        else:
            print(message)

    def _get_conn(self):
        if not self.conn:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
        return self.conn

    def _get_product_name(self, item_id, store_id):
        """Look up the actual product name from the knowledge base for an item."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT pc.product_name, pc.brand, pc.price, pc.size, ia.tier
            FROM item_alternatives ia
            JOIN product_catalog pc ON ia.catalog_id = pc.id
            WHERE ia.item_id = ? AND ia.store_id = ?
            ORDER BY ia.score DESC
            LIMIT 1
        """, (item_id, store_id))
        row = cur.fetchone()
        if row:
            return {
                "product_name": row["product_name"],
                "brand": row["brand"],
                "price": row["price"],
                "size": row["size"],
                "tier": row["tier"],
            }
        return None

    def _match_list_item(self, query, shopping_list):
        """
        Find the best matching item on the current list for a user query.
        Matches against item names, product names, and brands.
        Skips already-skipped items.

        Returns (item_dict, score) or (None, 0).
        """
        best = None
        best_score = 0
        q = query.lower()
        query_words = set(w for w in q.split() if len(w) > 2)
        if not query_words:
            query_words = {q}

        for item in shopping_list.get("items", []):
            if item.get("user_action") == "skipped" or item.get("status") == "skipped":
                continue

            score = 0
            iname = item["item_name"].lower()

            # Check generic item name (e.g., "Kefir", "Cheese")
            if q == iname:
                score += 100
            elif q in iname or iname in q:
                score += 50
            item_words = set(w for w in iname.split() if len(w) > 2)
            score += len(query_words & item_words) * 20

            # Also check actual product name (e.g., "Biotiful Kefir Blossom Honey")
            product = self._get_product_name(item["item_id"], item["store_id"])
            if product:
                pname = product["product_name"].lower()
                if q in pname:
                    score += 40
                pname_words = set(w for w in pname.split() if len(w) > 2)
                score += len(query_words & pname_words) * 15

                # Check brand too
                brand = (product.get("brand") or "").lower()
                if brand and q in brand:
                    score += 30

            if score > best_score:
                best_score = score
                best = item

        return (best, best_score) if best and best_score >= 15 else (None, 0)

    def _close_conn(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    # ────────────────────────────────────────────
    # PREFERENCE LEARNING SIGNALS
    # ────────────────────────────────────────────

    def _emit_signal(self, item_id, signal_type, dimension,
                     old_value=None, new_value=None, context=None):
        """Emit a preference signal from chat interaction. Fails silently."""
        if not item_id:
            return
        try:
            from preference_engine import emit_signal
            emit_signal(
                item_id=item_id,
                signal_type=signal_type,
                signal_source="chat",
                dimension=dimension,
                old_value=old_value,
                new_value=new_value,
                context=context,
            )
        except Exception as e:
            print(f"[agent_brain] Could not emit preference signal: {e}")

    # ────────────────────────────────────────────
    # DIRECT ITEM UPDATES (from frontend UI)
    # ────────────────────────────────────────────

    def update_item_direct(self, list_item_id, action, quantity=None):
        """
        Direct item update from frontend UI, bypasses NLP parsing.
        Used for quantity +/- buttons and remove button.

        Args:
            list_item_id: ID from list_items table
            action: "change_qty" or "remove"
            quantity: absolute target quantity (for change_qty)
        """
        if action == "change_qty" and quantity is not None:
            if quantity <= 0:
                list_update_item(list_item_id, "skipped")
            else:
                list_update_item(list_item_id, "approved", quantity=quantity)
            return True
        elif action == "remove":
            list_update_item(list_item_id, "skipped")
            return True
        return False

    # ────────────────────────────────────────────
    # LIST REUSE (avoid duplicates on reconnect)
    # ────────────────────────────────────────────

    def _find_existing_list(self, target_date):
        """
        Check if there's already an active list for this target date.
        Returns list_get() result if found, None otherwise.

        "Active" means: status is 'draft' or 'confirmed' (not 'done' or 'cancelled').
        If found, we reuse it instead of creating yet another duplicate.

        IMPORTANT: If there have been deliveries AFTER the list was created,
        the list is stale and should be regenerated. This prevents showing items
        that were just delivered.
        """
        import sqlite3
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row

            # Get ALL active lists for this target date (not just one)
            rows = conn.execute("""
                SELECT id, created_at FROM shopping_lists
                WHERE target_date = ? AND status IN ('draft', 'confirmed')
                ORDER BY id DESC
            """, (target_date,)).fetchall()

            if not rows:
                conn.close()
                return None

            # Invalidate ALL stale lists (delivery arrived after creation)
            # and deduplicate: keep only the most recent valid list
            valid_row = None
            for row in rows:
                delivery_after = conn.execute("""
                    SELECT 1 FROM orders
                    WHERE delivery_date IS NOT NULL
                      AND delivery_date > ?
                    LIMIT 1
                """, (row["created_at"],)).fetchone()

                if delivery_after:
                    conn.execute(
                        "UPDATE shopping_lists SET status = 'done', notes = 'Invalidated: delivery received after list was created' WHERE id = ?",
                        (row["id"],)
                    )
                    print(f"  List {row['id']} invalidated: delivery arrived after it was created")
                elif valid_row is None:
                    valid_row = row  # Keep the most recent valid one
                else:
                    # Duplicate draft — mark older ones as done
                    conn.execute(
                        "UPDATE shopping_lists SET status = 'done', notes = 'Deduplicated: newer draft exists' WHERE id = ?",
                        (row["id"],)
                    )

            conn.commit()
            conn.close()

            if valid_row:
                existing = list_get(valid_row["id"])
                if existing and existing.get("items"):
                    # Filter out skipped items for the summary
                    active_items = [
                        i for i in existing["items"]
                        if i.get("status") != "skipped" and i.get("user_action") != "skipped"
                    ]
                    if active_items:
                        return existing

            return None
        except Exception as e:
            print(f"  Warning: could not check for existing list: {e}")
            return None

    # ────────────────────────────────────────────
    # SESSION RESTORE (reconnect persistence)
    # ────────────────────────────────────────────

    def restore_session(self):
        """
        Restore session from DB on WebSocket reconnect.
        Looks for the most recent active suggestion and cart lists.
        Returns True if a session was restored, False if starting fresh.
        """
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row

            # Find most recent active suggestion
            suggestion = conn.execute("""
                SELECT id, target_date, status, agent_state, agent_state_data
                FROM shopping_lists
                WHERE list_type = 'suggestion'
                  AND status IN ('draft', 'confirmed')
                  AND dismissed_at IS NULL
                ORDER BY updated_at DESC LIMIT 1
            """).fetchone()

            # Find most recent active cart
            cart = conn.execute("""
                SELECT id, target_date, status, agent_state, agent_state_data
                FROM shopping_lists
                WHERE list_type = 'cart'
                  AND status IN ('draft', 'confirmed')
                  AND dismissed_at IS NULL
                ORDER BY updated_at DESC LIMIT 1
            """).fetchone()

            conn.close()

            restored = False

            if suggestion:
                self.session.suggestion_list_id = suggestion["id"]
                self.session.list_id = suggestion["id"]
                self.session.target_date = suggestion["target_date"]
                restored = True
                print(f"  Restored suggestion list {suggestion['id']}")

                # Restore agent state if saved
                if suggestion["agent_state"]:
                    self.session.state = suggestion["agent_state"]
                    if suggestion["agent_state_data"]:
                        try:
                            data = json.loads(suggestion["agent_state_data"])
                            if data.get("failed_items"):
                                self.session.failed_items = data["failed_items"]
                            if data.get("cart_report"):
                                self.session.cart_report = data["cart_report"]
                        except (json.JSONDecodeError, TypeError):
                            pass

            if cart:
                self.session.cart_list_id = cart["id"]
                self.session.list_id = cart["id"]  # Cart takes priority
                restored = True
                print(f"  Restored cart list {cart['id']}")

                # Mid-build crash recovery
                if cart["agent_state"] == "building":
                    self.session.state = State.REVIEWING
                    print("  Cart was mid-build — recovered to reviewing state")
                elif cart["agent_state"]:
                    self.session.state = cart["agent_state"]

            return restored

        except Exception as e:
            print(f"  Warning: session restore failed: {e}")
            return False

    def persist_state(self):
        """
        Save current agent state to DB for reconnect persistence.
        Called after every agent turn and UI action.
        """
        try:
            # Determine which list to save state on
            target_id = self.session.cart_list_id or self.session.suggestion_list_id
            if not target_id:
                return

            # Build state data JSON
            state_data = {}
            if self.session.failed_items:
                state_data["failed_items"] = self.session.failed_items
            if self.session.cart_report:
                state_data["cart_report"] = self.session.cart_report

            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                UPDATE shopping_lists
                SET agent_state = ?, agent_state_data = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (
                self.session.state,
                json.dumps(state_data, default=str) if state_data else None,
                target_id,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"  Warning: state persistence failed: {e}")

    # ────────────────────────────────────────────
    # STEP 1: CHECK-IN
    # ────────────────────────────────────────────

    def _ensure_fresh_suggestion_list(self, target_date=None):
        """
        Make sure there's a fresh draft suggestion list, even after a
        restore_session restored the user's cart but no suggestion list.

        "Fresh" means:
          - list_type='suggestion' AND status='draft' (or 'confirmed').
          - created_at >= the most recent delivery date (so a delivery
            invalidates any list created before it).

        If no fresh list exists AND we're past the 24h delivery cooldown
        AND there are DUE restock_rules, this calls list_generate() and
        attaches the resulting list_id to the session.

        Idempotent: safe to call every time start() runs. If a fresh
        list already exists, just attaches it to the session.

        Why this method exists:
          AgentBrain.start() used to early-return after restore_session()
          set the cart_list_id. The suggestion list never got
          regenerated, even when restock_rules was screaming that 20
          items were overdue. See docs/scheduled_list_stale_2026_05_26.md
          for the full diagnosis.
        """
        # Already restored a suggestion list — nothing to do.
        if self.session.suggestion_list_id:
            return

        # Don't auto-generate within the post-delivery cooldown window.
        # Lets the user enjoy a freshly-arrived order for a bit before
        # we put a new list in their face.
        if self._recent_delivery_cooldown(hours=24):
            return

        resolved_date = target_date or next_order_date()

        # Look for any existing draft suggestion newer than the most
        # recent delivery. Deliveries invalidate prior lists.
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            last_delivery_row = conn.execute(
                "SELECT MAX(delivery_date) AS d FROM orders WHERE delivery_date IS NOT NULL"
            ).fetchone()
            last_delivery = last_delivery_row["d"] if last_delivery_row else None

            if last_delivery:
                existing = conn.execute(
                    """SELECT id FROM shopping_lists
                       WHERE list_type = 'suggestion'
                         AND status IN ('draft', 'confirmed')
                         AND dismissed_at IS NULL
                         AND created_at >= ?
                       ORDER BY id DESC LIMIT 1""",
                    (last_delivery,),
                ).fetchone()
            else:
                existing = conn.execute(
                    """SELECT id FROM shopping_lists
                       WHERE list_type = 'suggestion'
                         AND status IN ('draft', 'confirmed')
                         AND dismissed_at IS NULL
                       ORDER BY id DESC LIMIT 1"""
                ).fetchone()
            conn.close()
        except Exception as e:
            print(f"  [ensure_fresh] check failed: {e}")
            return

        if existing:
            self.session.suggestion_list_id = existing["id"]
            print(f"  [ensure_fresh] reusing existing suggestion list {existing['id']}")
            return

        # No fresh suggestion list -- generate one. list_generate calls
        # refresh_observed_cadences() first so it reads the corrected
        # cadence numbers, not the hardcoded HABITS ones.
        print(f"  [ensure_fresh] no fresh suggestion list; generating for {resolved_date}")
        try:
            result = list_generate(target_date=resolved_date)
            if not result or not result.get("items"):
                print(f"  [ensure_fresh] list_generate returned no items (last delivery: {last_delivery})")
                return

            new_id = result["list_id"]
            self.session.suggestion_list_id = new_id

            # Mark this list as a suggestion in the DB (so future
            # restore_session calls find it).
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "UPDATE shopping_lists SET list_type = 'suggestion', source = 'auto' WHERE id = ?",
                    (new_id,),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

            print(f"  [ensure_fresh] generated suggestion list {new_id} with {len(result.get('items', []))} items")
        except Exception as e:
            print(f"  [ensure_fresh] generation failed: {e}")

    def _recent_delivery_cooldown(self, hours=24):
        """
        Check if a delivery arrived recently (within `hours` hours).
        If so, the user probably doesn't need a new order right away.
        Returns True if we should show "All Caught Up" instead of a list.
        """
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT delivery_date FROM orders
                WHERE delivery_date IS NOT NULL
                ORDER BY delivery_date DESC LIMIT 1
            """).fetchone()
            conn.close()

            if row and row["delivery_date"]:
                last = datetime.strptime(row["delivery_date"], "%Y-%m-%d %H:%M:%S")
                hours_since = (datetime.now() - last).total_seconds() / 3600
                if hours_since < hours:
                    print(f"  Delivery cooldown: {hours_since:.1f}h since last delivery (< {hours}h threshold)")
                    return True
        except Exception as e:
            print(f"  Delivery cooldown check error: {e}")
        return False

    def start_checkin(self, target_date=None):
        """
        Generate the shopping list and send the check-in message.
        Transitions: IDLE -> CHECKIN

        First tries to restore a previous session (reconnect persistence).
        Then reuses an existing list for the same target date if one exists
        (avoids creating duplicate lists on page refresh / reconnect).

        If a delivery arrived recently (< 24h), don't auto-suggest an order.
        Show "All Caught Up" and let the user create an order when ready.
        """
        create_database()
        ensure_analysis_tables()
        self.session.started_at = datetime.now().isoformat()

        # Try to restore a previous session first
        if self.restore_session():
            # Session restored — set state to CHECKIN if it was IDLE
            if self.session.state == State.IDLE:
                self.session.state = State.CHECKIN
            # Even after restore, make sure a fresh suggestion list exists.
            # Previously: if restore_session picked up a draft CART, we'd
            # return here and never call list_generate -- the suggestion
            # list could sit stale for months. Now we always check + regen
            # when the existing draft suggestion is missing or stale.
            try:
                self._ensure_fresh_suggestion_list(target_date)
            except Exception as e:
                print(f"  [start] _ensure_fresh_suggestion_list failed: {e}")
            # Skip check-in message — frontend will get the lists via send_list_data
            return

        # Don't auto-suggest orders right after a delivery
        if self._recent_delivery_cooldown(hours=24):
            self.say("Your last delivery just arrived! Everything's stocked up. When you're ready for the next order, just tap Create Order.")
            self.session.state = State.DONE
            return

        # Check for an existing active list for this target date
        resolved_date = target_date or next_order_date()
        existing = self._find_existing_list(resolved_date)

        if existing:
            # Reuse the existing list instead of generating a new one
            result = existing
            print(f"  Reusing existing list {result['list_id']} for {resolved_date}")

            # Backfill suggestion snapshots if missing (for lists created before Phase 1)
            try:
                from list_generate import _backfill_snapshots
                _backfill_snapshots(result["list_id"], result.get("items", []))
            except Exception as e:
                print(f"  Warning: snapshot backfill failed: {e}")

            # Filter out skipped items for accurate summary
            active_items = [
                i for i in result.get("items", [])
                if i.get("status") != "skipped" and i.get("user_action") != "skipped"
            ]
            result["items"] = active_items

            # Rebuild by_store and summary from active items only
            by_store = {}
            total_est = 0.0
            for item in active_items:
                store = item.get("store_name", "Unknown")
                by_store.setdefault(store, []).append(item)
                total_est += (item.get("estimated_price") or 0)
            result["by_store"] = by_store
            result["summary"] = {
                "total_items": len(active_items),
                "total_estimated": round(total_est, 2),
                "stores": {
                    s: {"count": len(items), "estimated": round(sum(i.get("estimated_price", 0) or 0 for i in items), 2)}
                    for s, items in by_store.items()
                },
            }
        else:
            # Generate a new list
            result = list_generate(target_date=target_date)

        self.session.target_date = result["target_date"]

        if not result or not result.get("items"):
            self.say(
                f"Hey, checked for {result['day_name']} {result['target_date']} -- "
                f"nothing's due for restock. You're all stocked up!"
            )
            self.session.state = State.DONE
            return

        self.session.list_id = result["list_id"]
        self.session.suggestion_list_id = result["list_id"]

        # Mark this list as a suggestion in the DB
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "UPDATE shopping_lists SET list_type = 'suggestion', source = 'auto' WHERE id = ?",
                (result["list_id"],)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

        # Pre-check availability and write substitution notes
        try:
            from product_knowledge import pre_check_availability
            avail = pre_check_availability(result["items"])
            subs_count = 0
            unavail_count = 0
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            for item in result["items"]:
                check = avail.get(item["item_id"])
                if not check or check["status"] == "available":
                    continue
                if check["note"]:
                    cur.execute("""
                        UPDATE list_items SET substitution_note = ?
                        WHERE list_id = ? AND item_id = ?
                    """, (check["note"], result["list_id"], item["item_id"]))
                    if check["status"] == "substituted":
                        subs_count += 1
                    elif check["status"] == "unavailable":
                        unavail_count += 1
            conn.commit()
            conn.close()
            if subs_count or unavail_count:
                print(f"  Pre-availability: {subs_count} substituted, {unavail_count} unavailable")
        except Exception as e:
            print(f"  Warning: pre-availability check failed: {e}")

        # Build the check-in message
        total_items = result["summary"]["total_items"]
        total_est = result["summary"]["total_estimated"]
        day_name = result["day_name"]
        date = result["target_date"]

        # Store breakdown
        store_lines = []
        for store_name, info in result["summary"]["stores"].items():
            store_lines.append(f"  {store_name}: {info['count']} items (~{info['estimated']:.0f} AED)")

        # Item list grouped by store, with real product names
        item_lines = []
        for store_name in sorted(result["by_store"].keys()):
            store_items = result["by_store"][store_name]
            store_total = sum(i.get("estimated_price", 0) or 0 for i in store_items)
            item_lines.append(f"\n{store_name} ({len(store_items)} items, ~{store_total:.0f} AED):")
            for i, item in enumerate(store_items, 1):
                qty = item["quantity"]
                price = item.get("estimated_price") or 0
                qty_str = f"x{int(qty)}" if qty > 1 else ""
                price_str = f"~{price:.0f}" if price else ""

                # Look up actual product from knowledge base
                product = self._get_product_name(item["item_id"], item["store_id"])
                if product:
                    display = product["product_name"]
                    if product["tier"] == "preferred":
                        tier_mark = ""
                    elif product["tier"] == "acceptable":
                        tier_mark = " [~]"
                    else:
                        tier_mark = " [?]"
                else:
                    display = item["item_name"]
                    tier_mark = " [no match]"

                # Get scoring reasons for this product
                reason_hint = ""
                rec = self._get_recommendation_reasons(item["item_id"], item["store_id"])
                if rec and rec["reasons"]:
                    # Show top 2 positive reasons as a compact hint
                    pos_reasons = [r for r in rec["reasons"] if "+" in r or "match" in r.lower()][:2]
                    if pos_reasons:
                        reason_hint = f" ({', '.join(r.split('(')[0].strip() for r in pos_reasons)})"

                item_lines.append(f"  {i}. {display} {qty_str} {price_str} AED{tier_mark}{reason_hint}")

        msg = (
            f"Hey, putting together your {day_name} order ({date}).\n"
            f"\n"
            f"{total_items} items, ~{total_est:.0f} AED total:\n"
            + "\n".join(store_lines) + "\n"
            + "\n".join(item_lines)
        )

        msg += "\n\nAnything to add, change, or skip?"

        self.say(msg)
        self.session.state = State.CHECKIN

    # ────────────────────────────────────────────
    # USER-CURATED ORDER (from "All Caught Up")
    # ────────────────────────────────────────────

    def start_user_order(self, silent=False):
        """
        Create a cart (user-curated order).
        Called when user sends a message from the DONE / "All Caught Up" state.
        Transitions to REVIEWING so the existing add/skip/approve flow works.

        Args:
            silent: If True, create the cart but don't send the prompt message
                    (used when user already included items in their message).
        """
        from datetime import date as _date
        target = self.session.target_date or _date.today().isoformat()

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO shopping_lists
                (name, target_date, status, notes, list_type, source)
                VALUES (?, ?, 'draft', 'User-curated order', 'cart', 'chat')
            """, ("Your Cart", target))
            list_id = cur.lastrowid
            conn.commit()

            self.session.list_id = list_id
            self.session.cart_list_id = list_id
            self.session.target_date = target
            self.session.state = State.REVIEWING

            self._log("user_order_start", detail=f"Created cart {list_id}")
            if not silent:
                self.say(
                    "Let's build your order! Tell me what you need — "
                    "e.g. \"bananas, rice, chicken, olive oil\".\n\n"
                "I'll find the best options based on your preferences."
            )
        finally:
            conn.close()

    # ────────────────────────────────────────────
    # STEP 2: PROCESS USER REPLY
    # ────────────────────────────────────────────

    def process_input(self, user_text, parsed=None, llm_response=None):
        """
        Process a user message based on current state.

        Args:
            user_text: Raw user text
            parsed: Pre-parsed ParsedMessage (if already parsed by app.py)
            llm_response: Natural language from LLM (for conversational replies)

        Returns True if the agent has more to say / needs more input.
        Returns False if the cycle is complete or waiting for async work.
        """
        self.session.messages_received.append({
            "text": user_text,
            "time": datetime.now().isoformat(),
        })

        if parsed is None:
            parsed = parse_message(user_text)

        # Store LLM response for state handlers to use
        self._llm_response = llm_response

        state = self.session.state

        # ── CHECKIN state: waiting for user response to check-in ──
        if state == State.CHECKIN:
            return self._handle_checkin_reply(parsed)

        # ── REVIEWING state: processing modifications ──
        elif state == State.REVIEWING:
            return self._handle_review_reply(parsed)

        # ── AWAITING_ALT state: user picking alternatives for failed items ──
        elif state == State.AWAITING_ALT:
            return self._handle_alt_reply(parsed)

        # ── Other states ──
        elif state == State.IDLE:
            self.say("No active order cycle. Starting a new one...")
            self.start_checkin()
            return True

        elif state == State.DONE:
            # User wants to create an order — start user-curated flow
            self.start_user_order()
            # If user also included items in their message, process them now
            has_items = any(
                a.intent in (Intent.ADD_ITEM, Intent.SKIP_ITEM, Intent.CHANGE_QTY)
                for a in parsed.actions
            )
            if has_items:
                self._process_modifications(parsed)
            return True

        else:
            self.say(f"I'm currently in '{state}' mode. Hold tight...")
            return False

    def _handle_checkin_reply(self, parsed):
        """Handle the user's reply to the check-in message."""
        primary = parsed.primary_intent

        if primary in (Intent.APPROVE_ALL, Intent.NOTHING):
            # User is happy with the list -- approve all and proceed to build
            self._approve_all_items()
            self.say("Got it, keeping the list as is. Building carts...")
            self.session.state = State.BUILDING
            return False  # signal to start cart building

        elif primary == Intent.CANCEL:
            self.say("Order cancelled. Let me know when you want to try again.")
            self.session.state = State.DONE
            return False

        elif primary == Intent.RESET:
            # User wants to restore the original AI suggestion
            from list_generate import list_reset_to_original
            result = list_reset_to_original(self.session.list_id)
            if "error" in result:
                self.say("Can't reset — no original suggestion saved for this list.")
            else:
                restored = result.get("restored", 0)
                removed = result.get("removed_user_adds", 0)
                parts = [f"Restored {restored} items to my original suggestion."]
                if removed > 0:
                    parts.append(f"Removed {removed} item(s) you had added.")
                self.say(" ".join(parts))
            return True

        elif primary == "log_purchase":
            # User bought something off-app
            item_name = parsed.actions[0].item_name if parsed.actions else None
            if item_name:
                self.say(f"Noted! You bought {item_name} outside the app. I'll adjust my predictions.")
                # TODO: update consumption model
            else:
                self.say("Got it, what did you buy?")
            return True

        elif primary == Intent.DELAY:
            self.say("Got it, I'll check back tomorrow. Your list is saved.")
            self.session.state = State.IDLE
            return False

        elif primary == Intent.SHOW_LIST:
            self._show_list()
            return True

        elif primary == Intent.QUESTION:
            if self._llm_response:
                self.say(self._llm_response)
                return True
            # Check if user is asking "why" about a product
            raw_lower = parsed.raw_text.lower().strip()
            if raw_lower.startswith("why "):
                return self._explain_recommendation(raw_lower[4:].strip())
            self.say("I can look that up -- but let me know your changes first so I can finalize the list.")
            return True

        else:
            # Process all actions (add, skip, qty changes)
            return self._process_modifications(parsed)

    def _handle_review_reply(self, parsed):
        """Handle follow-up modifications after initial check-in."""
        primary = parsed.primary_intent

        if primary in (Intent.APPROVE_ALL, Intent.NOTHING):
            self._approve_all_items()
            self.say("List finalized. Building carts now...")
            self.session.state = State.BUILDING
            return False

        elif primary == Intent.CANCEL:
            self.say("Order cancelled.")
            self.session.state = State.DONE
            return False

        elif primary == Intent.RESET:
            self.say("Starting fresh! Regenerating your list...")
            self.start_checkin(target_date=self.session.target_date)
            return True

        elif primary == "log_purchase":
            item_name = parsed.actions[0].item_name if parsed.actions else None
            if item_name:
                self.say(f"Noted! You bought {item_name} outside the app. I'll adjust my predictions.")
            else:
                self.say("Got it, what did you buy?")
            return True

        elif primary == Intent.SHOW_LIST:
            self._show_list()
            return True

        elif primary == Intent.QUESTION:
            if self._llm_response:
                self.say(self._llm_response)
                return True
            self.say("Let me know your changes, or say 'ok' to finalize.")
            return True

        else:
            return self._process_modifications(parsed)

    def _handle_alt_reply(self, parsed):
        """Handle user picking alternatives for failed items."""
        primary = parsed.primary_intent

        if primary == Intent.PICK_ALT:
            choice = parsed.actions[0].choice_number
            return self._apply_alt_choice(choice)

        elif primary in (Intent.SKIP_ITEM, Intent.NOTHING):
            # Skip this failed item
            self.say("Skipping that one.")
            self.session.current_failed_idx += 1
            return self._present_next_failed()

        elif primary == Intent.APPROVE_ALL:
            # Skip remaining failed items
            self.say("Skipping the rest. Carts are ready!")
            self.session.state = State.DONE
            self._print_final_summary()
            return False

        else:
            self.say("Pick a number (1, 2, 3...) or say 'skip'.")
            return True

    # ────────────────────────────────────────────
    # MODIFICATION PROCESSING
    # ────────────────────────────────────────────

    def _process_modifications(self, parsed):
        """
        Process add/skip/qty actions from user message.

        Returns True (always continues reviewing).
        Sets self._pending_suggestion if _add_item returns a suggestion dict,
        so the WebSocket layer can send it to the frontend.

        When 3+ ADD_ITEM actions are detected, routes through the batch
        curator (basket_curator.py) for cross-vendor price comparison and
        MOV optimization instead of adding items one-by-one.
        """
        conn = self._get_conn()
        responses = []
        self._pending_suggestion = None  # Reset per call
        self._batch_curation_result = None  # Reset per call

        # ── Batch detection: 3+ add items → use curator ──
        add_actions = [a for a in parsed.actions if a.intent == Intent.ADD_ITEM and a.item_name]
        other_actions = [a for a in parsed.actions if a.intent != Intent.ADD_ITEM]

        # Apply preference modifiers to add actions before curation
        # e.g., if LLM created ADD_ITEM("apples") + PREFERENCE("organic" for "apples"),
        # prepend "organic" to the item name so curator searches for "organic apples"
        _QUALITY_MODIFIERS = {"organic", "fresh", "free range", "grass fed", "unsalted",
                              "extra virgin", "whole grain", "sugar free", "gluten free",
                              "low fat", "full fat", "raw", "smoked", "frozen"}
        pref_actions = [a for a in other_actions if a.intent == Intent.PREFERENCE]
        for pref in pref_actions:
            pref_val = (getattr(pref, "pref_value", None) or "").lower().strip()
            pref_item = (pref.item_name or "").lower().strip()
            if pref_val in _QUALITY_MODIFIERS:
                for add_a in add_actions:
                    name_lower = (add_a.item_name or "").lower()
                    # Match if pref targets this item, or no specific target (global)
                    if (not pref_item or pref_item in name_lower or name_lower in pref_item):
                        if pref_val not in name_lower:
                            add_a.item_name = f"{pref_val} {add_a.item_name}"

        if len(add_actions) >= 3:
            # Batch mode: curate entire basket at once
            batch_result = self._batch_curate(add_actions)
            if batch_result:
                responses.append(batch_result)
                # Process any non-add actions (skip, qty change) normally
                for action in other_actions:
                    if action.intent == Intent.SKIP_ITEM:
                        responses.append(self._skip_item(action, conn))
                    elif action.intent == Intent.CHANGE_QTY:
                        responses.append(self._change_qty(action, conn))
                    elif action.intent == Intent.PREFERENCE:
                        responses.append(self._handle_preference(action, conn))

                self.session.state = State.REVIEWING
                text_responses = [r for r in responses if isinstance(r, str)]
                if text_responses:
                    self.say("\n".join(text_responses))
                return True

        # ── Normal mode: process actions one by one ──
        for action in parsed.actions:
            if action.intent == Intent.ADD_ITEM:
                result = self._add_item(action, conn)
                if isinstance(result, dict) and result.get("suggestion"):
                    # Catalog search found suggestions — store for WebSocket layer
                    self._pending_suggestion = result
                    responses.append(f"Searching for '{result['item_name']}' in our catalog...")
                else:
                    responses.append(result)

            elif action.intent == Intent.SKIP_ITEM:
                result = self._skip_item(action, conn)
                responses.append(result)

            elif action.intent == Intent.CHANGE_QTY:
                result = self._change_qty(action, conn)
                responses.append(result)

            elif action.intent == Intent.PREFERENCE:
                result = self._handle_preference(action, conn)
                responses.append(result)

            elif action.intent == Intent.UNKNOWN and action.confidence < 0.5:
                self._log("unknown_action", detail=f"Low-confidence action: {action.raw_text}")
                responses.append(f"Didn't catch that: \"{action.raw_text}\"")

        if responses:
            if self._llm_response and not self._pending_suggestion:
                # LLM crafted a natural response — use it instead of hardcoded confirmations
                self.say(self._llm_response)
            elif self._pending_suggestion and self._llm_response:
                # LLM response + suggestion: say the LLM response, WebSocket sends the suggestion
                self.say(self._llm_response)
            else:
                # Fallback: regex path, use built confirmations
                text_responses = [r for r in responses if isinstance(r, str)]
                if text_responses:
                    text_responses.append("\nAnything else, or good to go?")
                    self.say("\n".join(text_responses))
            self.session.state = State.REVIEWING
            return True
        else:
            if self._llm_response:
                self.say(self._llm_response)
            else:
                self._log("no_actions_parsed", detail=f"No actions parsed from: {parsed.raw_text[:100]}")
                self.say("I'm not sure what to do with that. Try 'add [item]', 'skip [item]', or 'ok' to proceed.")
            return True

    def _add_item(self, action, conn):
        """
        Add an item to the shopping list.

        If item is known (in items table), auto-add immediately.
        If unknown, search the product catalog and return a suggestion dict
        for the WebSocket layer to send to the frontend as a bottom sheet.
        """
        item_name = action.item_name
        if not item_name:
            self._log("add_item_empty", detail="No item name provided")
            return "Add what? (e.g., 'add olive oil')"

        # Step 1: Try to match to a known item in the items table
        match = match_item_to_db(item_name, conn)
        if match:
            result = list_add_item(
                self.session.list_id,
                match["name"],
                store_name=action.store_hint,
                quantity=action.quantity or 1,
            )
            if result:
                qty_str = f" x{int(action.quantity)}" if action.quantity and action.quantity > 1 else ""
                store = action.store_hint or match.get("store_name", "")
                # Emit preference signal: user explicitly added this item
                self._emit_signal(match.get("item_id") or match.get("id"), "item_add", "item_inclusion",
                                  new_value={"action": "add", "raw_text": item_name},
                                  context={"list_id": self.session.list_id})
                self._log("add_item_ok", detail=f"Added known item: {match['name']}")
                return f"Added: {match['name']}{qty_str} ({store})"
            else:
                self._log("add_item_fail", detail=f"list_add_item failed for known item: {item_name}")
                return f"Could not add '{item_name}' to the list"

        # Step 2: Unknown item — search the product catalog for suggestions
        self._log("add_item_catalog_search", detail=f"Item '{item_name}' not in items table, searching catalog")
        try:
            from product_knowledge import search_catalog
            catalog_results = search_catalog(item_name, limit=5)

            if catalog_results:
                # Store suggestions in session for the WebSocket layer to send
                self.session.pending_suggestions = {
                    "item_name": item_name,
                    "query": item_name,
                    "products": catalog_results,
                    "current_index": 0,
                    "quantity": action.quantity or 1,
                    "store_hint": action.store_hint,
                }
                top = catalog_results[0]
                self._log("add_item_suggestions_found",
                          detail=f"Found {len(catalog_results)} catalog matches for '{item_name}', top: {top['product_name']}")
                # Return a dict (not string) — caller detects this and triggers the suggestion flow
                return {
                    "suggestion": True,
                    "item_name": item_name,
                    "product": top,
                    "has_more": len(catalog_results) > 1,
                    "total": len(catalog_results),
                }
            else:
                self._log("add_item_no_catalog_match",
                          detail=f"No catalog matches for '{item_name}'", level="warning")
                return f"I couldn't find '{item_name}' in our product catalog. Try a different name or add it manually next time you shop."

        except Exception as e:
            self._log("add_item_catalog_error",
                      detail=f"Catalog search failed for '{item_name}': {type(e).__name__}: {str(e)[:150]}",
                      level="error")
            return f"I had trouble searching for '{item_name}'. Try again or use a simpler name."

    def _batch_curate(self, add_actions):
        """
        Batch-curate multiple items using the basket curator.

        Matches items across all stores, compares prices, respects preferences,
        and optimizes the vendor split considering minimum order values.

        Returns a summary string, or None if curation failed.
        """
        item_names = [a.item_name for a in add_actions]
        self._log("batch_curate_start",
                  detail=f"Curating {len(item_names)} items: {', '.join(item_names[:10])}")

        try:
            from basket_curator import curate_basket

            result = curate_basket(item_names, list_id=self.session.list_id)
            self._batch_curation_result = result  # Store for WebSocket layer

            # Add items to the shopping list (matched AND unmatched)
            added_count = 0
            unmatched_count = 0
            for item in result.get("items", []):
                if item["status"] == "unmatched":
                    # Still add to list so frontend can show failure context + suggestions
                    import json as _json
                    fc = item.get("failure_context")
                    list_add_item(
                        self.session.list_id,
                        item["input_name"],
                        store_id=None,  # no store for unmatched
                        status="needs_input",
                        failure_context=_json.dumps(fc) if fc else None,
                    )
                    unmatched_count += 1
                    continue

                # Use real product name when available (not raw "carrot")
                display_name = item.get("recommended_product") or item["matched_name"]

                # Get catalog_id + category from the best option for the recommended store
                catalog_id = None
                category_override = None
                for opt in item.get("options", []):
                    if opt.get("store_id") == item.get("recommended_store_id"):
                        catalog_id = opt.get("catalog_id")
                        # Extract top-level category from product hierarchy
                        # e.g., "Fresh Food > Dairy & Eggs > Milk" -> "Dairy & Eggs"
                        raw_cat = opt.get("category", "")
                        if raw_cat:
                            parts = [p.strip() for p in raw_cat.split(">")]
                            category_override = parts[1] if len(parts) >= 2 else parts[0]
                        break

                list_item_id = list_add_item(
                    self.session.list_id,
                    display_name,
                    store_id=item.get("recommended_store_id"),
                    item_id=item.get("matched_item_id"),
                    estimated_price=item.get("recommended_price"),
                    quantity=1,
                    catalog_id=catalog_id,
                    category_override=category_override,
                )
                if list_item_id:
                    added_count += 1
                    # Emit preference signal for each added item
                    if item.get("matched_item_id"):
                        self._emit_signal(
                            item["matched_item_id"], "item_add", "item_inclusion",
                            new_value={"action": "batch_add", "raw_text": item["input_name"]},
                            context={"list_id": self.session.list_id, "batch": True},
                        )

            self._log("batch_curate_done",
                      detail=f"Added {added_count}/{len(item_names)} items"
                             f" ({unmatched_count} unmatched)")

            # Build the user-facing summary
            return result.get("summary", f"Added {added_count} items.")

        except Exception as e:
            self._log("batch_curate_error",
                      detail=f"Batch curation failed: {type(e).__name__}: {str(e)[:200]}",
                      level="error")
            # Fallback: try adding items one by one
            self._log("batch_curate_fallback", detail="Falling back to single-item add")
            conn = self._get_conn()
            fallback_responses = []
            for action in add_actions:
                result = self._add_item(action, conn)
                if isinstance(result, str):
                    fallback_responses.append(result)
            return "\n".join(fallback_responses) if fallback_responses else None

    def _log(self, event, detail="", level="info"):
        """Convenience wrapper for logging agent events."""
        try:
            from app_logging import log_event
            log_event(event, source="agent_brain", detail=detail, level=level)
        except Exception:
            pass

    def _skip_item(self, action, conn):
        """Skip/remove an item from the shopping list."""
        item_name = action.item_name
        if not item_name:
            return "Skip what? (e.g., 'skip salmon')"

        shopping_list = list_get(self.session.list_id)
        if not shopping_list:
            return "Could not load the list"

        best, score = self._match_list_item(item_name, shopping_list)
        if best:
            list_update_item(best["list_item_id"], "skipped")
            # Emit preference signal: user skipped this item
            self._emit_signal(best.get("item_id"), "item_skip", "item_inclusion",
                              new_value={"action": "skip", "raw_text": item_name},
                              context={"list_id": self.session.list_id})
            return f"Removed: {best['item_name']} ✕"
        else:
            return f"Can't find '{item_name}' on the list"

    def _change_qty(self, action, conn):
        """Change quantity for an item. Negative qty = reduce by that amount."""
        item_name = action.item_name
        qty = action.quantity
        if not item_name or qty is None:
            return "Change what? (e.g., '2 packs of eggs')"

        shopping_list = list_get(self.session.list_id)
        if not shopping_list:
            return "Could not load the list"

        best, score = self._match_list_item(item_name, shopping_list)
        if best:
            current_qty = best.get("quantity", 1)
            if qty < 0:
                # Negative qty = reduce by that amount (e.g., "remove 1 pasta")
                new_qty = current_qty + qty  # qty is negative
                if new_qty <= 0:
                    # Reducing to 0 or below = skip entirely
                    list_update_item(best["list_item_id"], "skipped")
                    self._emit_signal(best.get("item_id"), "item_skip", "item_inclusion",
                                      new_value={"action": "skip"},
                                      context={"list_id": self.session.list_id})
                    return f"Removed: {best['item_name']} (was x{int(current_qty)})"
                else:
                    list_update_item(best["list_item_id"], "approved", quantity=new_qty)
                    self._emit_signal(best.get("item_id"), "qty_change", "quantity",
                                      old_value={"qty": current_qty},
                                      new_value={"qty": new_qty},
                                      context={"list_id": self.session.list_id})
                    return f"Reduced: {best['item_name']} x{int(current_qty)} → x{int(new_qty)}"
            else:
                list_update_item(best["list_item_id"], "approved", quantity=qty)
                self._emit_signal(best.get("item_id"), "qty_change", "quantity",
                                  old_value={"qty": current_qty},
                                  new_value={"qty": qty},
                                  context={"list_id": self.session.list_id})
                return f"Updated: {best['item_name']} → x{int(qty)}"
        else:
            return f"Can't find '{item_name}' on the list"

    def _handle_preference(self, action, conn):
        """Handle a preference statement like 'I prefer Wagyu' or 'no more Kraft'."""
        pref_type = getattr(action, "pref_type", None)
        pref_value = getattr(action, "pref_value", None) or action.item_name
        item_name = action.item_name or ""

        if not pref_value:
            return "Got it, but I didn't catch the preference."

        # Map pref_type to signal_type and dimension
        PREF_MAP = {
            "brand_prefer":     ("brand_pref_stated",     "brand"),
            "brand_avoid":      ("brand_avoid_stated",    "brand"),
            "attribute_require": ("attribute_pref_stated", "attribute"),
            "product_prefer":   ("product_pref_stated",   "product"),
            "store_prefer":     ("store_pref_stated",     "store"),
        }
        signal_type, dimension = PREF_MAP.get(pref_type, ("preference_stated", "attribute"))

        # Try to match to a DB item
        match = match_item_to_db(item_name, conn)

        # For brand/attribute prefs without a clear item match, try matching pref_value
        if not match and pref_value != item_name:
            match = match_item_to_db(pref_value, conn)

        # Build signal payload
        new_value = {"pref_type": pref_type, "value": pref_value}
        if action.store_hint:
            new_value["store"] = action.store_hint

        if match:
            self._emit_signal(
                match["item_id"], signal_type, dimension,
                new_value=new_value,
                context={"raw_text": action.raw_text, "list_id": self.session.list_id},
            )
            # Also apply immediate effect if it's a strong preference
            if pref_type == "brand_avoid":
                return f"Noted: avoid {pref_value} for {match['name']}. I'll remember that."
            elif pref_type == "brand_prefer":
                return f"Noted: prefer {pref_value} for {match['name']}. I'll prioritize it next time."
            elif pref_type == "attribute_require":
                return f"Noted: {pref_value} required for {match['name']}. Updated your preferences."
            elif pref_type == "store_prefer":
                store = action.store_hint or pref_value
                return f"Noted: get {match['name']} from {store}."
            else:
                return f"Noted: preference for {pref_value} ({match['name']})."
        else:
            # No DB match — still acknowledge, but we can't emit a targeted signal yet
            # Try to emit a generic signal if there's a list context
            if pref_type == "brand_avoid":
                return f"Noted: avoid {pref_value}. I'll keep that in mind for future lists."
            elif pref_type == "brand_prefer":
                return f"Noted: prefer {pref_value}. I'll prioritize it when I can."
            elif pref_type == "attribute_require":
                return f"Noted: {pref_value} is important to you."
            else:
                return f"Got it, I'll remember that about {pref_value}."

    def _approve_all_items(self):
        """Approve all non-skipped items on the list."""
        shopping_list = list_get(self.session.list_id)
        if not shopping_list:
            return
        for item in shopping_list["items"]:
            if item.get("user_action") != "skipped" and item.get("status") != "skipped":
                list_update_item(item["list_item_id"], "approved")
        list_finalize(self.session.list_id)

    def _show_list(self):
        """Show the current shopping list with real product names."""
        shopping_list = list_get(self.session.list_id)
        if not shopping_list:
            self.say("No list loaded.")
            return

        lines = [f"Shopping list ({shopping_list['target_date']}):"]
        for store_name in sorted(shopping_list.get("by_store", {}).keys()):
            items = shopping_list["by_store"][store_name]
            store_total = sum(i.get("estimated_price", 0) or 0 for i in items)
            lines.append(f"\n{store_name} ({len(items)} items, ~{store_total:.0f} AED):")
            for i, item in enumerate(items, 1):
                qty = item.get("quantity", 1)
                price = item.get("estimated_price", 0) or 0
                status = "SKIP" if item.get("user_action") == "skipped" else "OK"

                # Look up actual product
                product = self._get_product_name(item["item_id"], item["store_id"])
                if product:
                    display = product["product_name"]
                else:
                    display = item["item_name"]

                qty_str = f"x{int(qty)}" if qty > 1 else ""
                lines.append(f"  [{status}] {i}. {display} {qty_str} ~{price:.0f} AED")

        self.say("\n".join(lines))

    def _explain_recommendation(self, query):
        """Explain why a specific product was recommended."""
        shopping_list = list_get(self.session.list_id)
        if not shopping_list:
            self.say("No list to check.")
            return True

        best, score = self._match_list_item(query, shopping_list)
        if not best:
            self.say(f"Can't find '{query}' on the list.")
            return True

        rec = self._get_recommendation_reasons(best["item_id"], best["store_id"])
        if rec:
            lines = [f"Why {rec['product_name']}? (score: {rec['score']}/100, tier: {rec['tier']})"]
            for reason in rec["reasons"]:
                lines.append(f"  • {reason}")
            self.say("\n".join(lines))
        else:
            self.say(f"No scoring data available for {best['item_name']}.")
        return True

    def _show_compact_list(self):
        """Show a brief summary of the updated list (active items only)."""
        shopping_list = list_get(self.session.list_id)
        if not shopping_list:
            return

        active = [i for i in shopping_list["items"]
                  if i.get("user_action") != "skipped" and i.get("status") != "skipped"]
        skipped = [i for i in shopping_list["items"]
                   if i.get("user_action") == "skipped" or i.get("status") == "skipped"]
        total = sum(i.get("estimated_price", 0) or 0 for i in active)

        lines = [f"\nUpdated list ({len(active)} items, ~{total:.0f} AED):"]
        for item in active:
            qty = item.get("quantity", 1)
            qty_str = f" x{int(qty)}" if qty > 1 else ""
            product = self._get_product_name(item["item_id"], item["store_id"])
            display = product["product_name"] if product else item["item_name"]
            lines.append(f"  ✓ {display}{qty_str}")

        if skipped:
            lines.append(f"  ({len(skipped)} skipped)")

        self.say("\n".join(lines))

    def _get_recommendation_reasons(self, item_id, store_id):
        """Get the scoring reasons for why a product was recommended."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT ia.reasons, ia.score, ia.tier, pc.product_name
            FROM item_alternatives ia
            JOIN product_catalog pc ON ia.catalog_id = pc.id
            WHERE ia.item_id = ? AND ia.store_id = ?
            ORDER BY ia.score DESC
            LIMIT 1
        """, (item_id, store_id))
        row = cur.fetchone()
        if row and row["reasons"]:
            import json as _json
            try:
                reasons = _json.loads(row["reasons"])
                return {
                    "product_name": row["product_name"],
                    "score": row["score"],
                    "tier": row["tier"],
                    "reasons": reasons,
                }
            except (ValueError, TypeError):
                pass
        return None

    # ────────────────────────────────────────────
    # STEP 3: BUILD CARTS
    # ────────────────────────────────────────────

    async def build_carts(self, progress_cb=None):
        """
        Build real carts on store websites.
        Transitions: BUILDING -> HANDOFF

        Args:
            progress_cb: async callback(step, **kwargs) forwarded to cart_builder
                         for real-time progress updates over WebSocket.
        """
        from cart_builder import build_cart

        self.say("Launching browser and building carts...")

        try:
            report = await build_cart(
                self.session.list_id,
                dry_run=self.session.dry_run,
                progress_cb=progress_cb,
            )
        except Exception as e:
            # Sanitize error — never show browser debug logs to user
            err_str = str(e).lower()
            if "browser" in err_str or "playwright" in err_str or "target" in err_str:
                self.say("Could not open the browser. Make sure Edge is closed and try again.")
            elif "timeout" in err_str or "network" in err_str:
                self.say("Cart building timed out. The store website may be slow. Try again in a few minutes.")
            else:
                # Truncate and strip technical details
                clean_err = str(e).split("\n")[0][:120]
                self.say(f"Cart building hit an issue: {clean_err}")
            self.session.state = State.DONE
            raise  # Re-raise so app.py can handle and report correctly

        self.session.cart_report = report

        # ── Pre-build availability gate tripped ──
        # The browser was NOT launched. Surface the unavailable items +
        # alternatives and stop, so the user swaps/skips and re-runs build
        # (preflight re-checks) -- rather than building a partial cart.
        if report.get("error") == "availability_check":
            self._present_preflight_problems(report.get("preflight", {}))
            self.session.state = State.REVIEWING
            return

        if "error" in report:
            self.say(f"Error: {report['error']}")
            self.session.state = State.DONE
            return

        # Save cart snapshot for gap analysis
        if not self.session.dry_run:
            try:
                save_cart_snapshot(report)
            except Exception as e:
                print(f"  Warning: could not save cart snapshot: {e}")

            # Create expected order entries so the status checker knows to look
            self._create_expected_orders(report)

            # Fast follow-up order checks at +15/30/60 min so a freshly-placed
            # order (real ref, status, line items) is reconciled with the
            # placeholder quickly instead of waiting for the hourly poller.
            try:
                from scheduler import schedule_post_cart_checks
                schedule_post_cart_checks()
            except Exception as e:
                print(f"  Warning: post-cart order checks not scheduled: {e}")

            # B2 -- WhatsApp the curator a one-shot summary of what
            # was just submitted. Best-effort; failures here can't
            # roll back the actual cart build that just succeeded,
            # so we log and move on.
            try:
                await self._send_order_placed_whatsapp(report)
            except Exception as e:
                # Use the agent's logger via the say() debug channel
                # so a missing wa_client / sidecar doesn't show up
                # as an exception in the curator's app session.
                print(f"  Warning: B2 order-placed WA send failed: {e}")

        # Build the report message
        self._send_cart_report(report)

        # Check for failed items that need user attention
        all_failed = []
        for store_name, sr in report.get("stores", {}).items():
            for failed in sr.get("failed", []):
                failed["store_name"] = store_name
                all_failed.append(failed)

        if all_failed:
            self.session.failed_items = all_failed
            self.session.current_failed_idx = 0
            self.session.state = State.HANDOFF
            self._present_next_failed()
        else:
            self.session.state = State.DONE
            self._print_final_summary()

    def _create_expected_orders(self, report):
        """Create placeholder orders with status='expected' so the status checker
        knows we're waiting for real orders from these stores."""
        conn = self._get_conn()
        cur = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")

        for store_name, sr in report.get("stores", {}).items():
            if sr.get("items_added", 0) == 0:
                continue  # No items added to this store's cart

            # Get store_id
            cur.execute("SELECT id FROM stores WHERE name = ?", (store_name,))
            row = cur.fetchone()
            if not row:
                continue
            store_id = row["id"]

            # Check if we already have an expected order for this store today
            cur.execute("""
                SELECT id FROM orders
                WHERE store_id = ? AND order_date = ? AND status = 'expected'
            """, (store_id, today))
            if cur.fetchone():
                continue  # Already expecting an order

            cur.execute("""
                INSERT INTO orders (store_id, order_date, status, source)
                VALUES (?, ?, 'expected', 'system')
            """, (store_id, today))
            order_id = cur.lastrowid
            print(f"  Created expected order #{order_id} for {store_name}")

        conn.commit()

    async def _send_order_placed_whatsapp(self, report):
        """B2 — send a one-line WhatsApp summary to the curator
        after a successful cart_build.

        No-op when WHATSAPP_ENABLED is off, no driver phone exists,
        the report had 0 items added across all stores, the driver
        opted out of b2_order_placed via notification_prefs, or
        the driver's quiet-hours window is active. Failures are
        swallowed; the in-app cart report still tells the curator
        what happened.
        """
        try:
            from config import WHATSAPP_ENABLED
        except Exception:
            return
        if not WHATSAPP_ENABLED:
            return

        # Skip the WA if nothing actually got added -- the in-app
        # error toast already covers the no-items-added case.
        stores_data = report.get("stores", {}) or {}
        total_added = sum(
            sr.get("items_added", 0) or 0 for sr in stores_data.values()
        )
        if total_added <= 0:
            return

        # Resolve driver (id + phone).
        conn = self._get_conn()
        try:
            driver = conn.execute(
                """SELECT id, name, phone, member_pending_intent
                   FROM household_members
                   WHERE role = 'driver' AND COALESCE(phone, '') != ''
                   LIMIT 1"""
            ).fetchone()
        finally:
            conn.close()
        if not driver or not driver["phone"]:
            return

        # B7 -- per-member opt-out + quiet hours. Defaults preserve
        # the driver-gets-all, 22-07 quiet behavior.
        try:
            from notification_prefs import should_notify, is_quiet_hours
            if not should_notify(driver["id"], "b2_order_placed"):
                return
            if is_quiet_hours(driver["id"]):
                return
        except Exception as e:
            # Fail-open: if the prefs module errors, we'd rather
            # send than silently swallow. The in-app log will catch
            # it for follow-up.
            print(f"  B2 notification_prefs lookup failed: {e}")

        # Build + send.
        try:
            from whatsapp_agent import format_order_placed
            from whatsapp_client import wa_client
            msg = format_order_placed(report)
            await wa_client.send_message(driver["phone"], msg)
        except Exception as e:
            print(f"  B2 order-placed send error: {e}")
            return

        # Best-effort event log so /api/diag/recent-errors shows the
        # successful send -- helps debug "did the user actually get
        # the message?" without grepping logs.
        try:
            from app_logging import log_event
            log_event(
                "b2_order_placed_sent", source="agent_brain",
                detail=f"list_id={report.get('list_id')} items={total_added}",
            )
        except Exception:
            pass

    def _send_cart_report(self, report):
        """Format and send the cart building report."""
        lines = ["Carts ready!\n"]

        for store_name, sr in report.get("stores", {}).items():
            added = sr["items_added"]
            requested = sr["items_requested"]
            total = sr["estimated_total"]
            lines.append(f"  {store_name}: {added}/{requested} items (~{total:.0f} AED)")

            if sr.get("items_failed", 0) > 0:
                lines.append(f"    {sr['items_failed']} item(s) need attention")

        self.say("\n".join(lines))

    def _present_preflight_problems(self, pf):
        """Tell the curator which items aren't in stock BEFORE building, with
        same- and cross-vendor alternatives, so they can swap or skip and
        re-run build. No browser was launched."""
        problems = pf.get("problems", []) or []
        n = len(problems)
        verb = "aren't" if n != 1 else "isn't"
        lines = [
            f"Hold on — {n} item{'s' if n != 1 else ''} {verb} in stock right "
            f"now, so I paused before building the cart. Swap or skip "
            f"{'each' if n != 1 else 'it'}, then tap build again:"
        ]
        for p in problems:
            lines.append(
                f"\n• {p.get('item_name', '?')} ({p.get('store', '')}) — out of stock"
            )
            alts = p.get("alternatives", {}) or {}
            same = (alts.get("same_vendor") or [])[:2]
            other = (alts.get("other_vendor") or [])[:2]
            for a in same:
                price = a.get("price")
                lines.append(
                    f"    ↳ {(a.get('name') or '?')[:42]}"
                    + (f" — {price:.0f} AED" if price else "")
                )
            for a in other:
                price = a.get("price")
                st = a.get("store_name") or "other store"
                lines.append(
                    f"    ↳ {st}: {(a.get('name') or '?')[:42]}"
                    + (f" — {price:.0f} AED" if price else "")
                )
            if not same and not other:
                lines.append("    ↳ no alternative found — you may need to skip it")
        self.say("\n".join(lines))

    def _present_next_failed(self):
        """Present the next failed item with alternatives for user decision."""
        idx = self.session.current_failed_idx
        failed = self.session.failed_items

        if idx >= len(failed):
            # All failed items handled
            self.session.state = State.DONE
            self._print_final_summary()
            return False

        item = failed[idx]
        lines = [
            f"\nCouldn't find a good option for: {item['item_name']} ({item.get('store_name', '')})",
            f"  Reason: {item.get('reason', '?')}",
        ]

        alts = item.get("alternatives", [])
        if alts:
            lines.append("  Alternatives:")
            for i, alt in enumerate(alts[:3], 1):
                name = alt.get("name", "?")[:45]
                price = alt.get("price", 0)
                tier = alt.get("tier", "?")
                lines.append(f"    {i}. {name} -- {price} AED [{tier}]")
            lines.append(f"    {len(alts[:3]) + 1}. Skip")
            lines.append("\nPick a number:")
        else:
            lines.append("  No alternatives found. Skipping.")
            self.session.current_failed_idx += 1
            self.say("\n".join(lines))
            return self._present_next_failed()

        self.session.state = State.AWAITING_ALT
        self.say("\n".join(lines))
        return True

    def _apply_alt_choice(self, choice):
        """Apply user's alternative choice for a failed item."""
        idx = self.session.current_failed_idx
        failed = self.session.failed_items

        if idx >= len(failed):
            return False

        item = failed[idx]
        alts = item.get("alternatives", [])

        if choice <= len(alts) and choice >= 1:
            chosen = alts[choice - 1]
            self.say(f"Added: {chosen.get('name', '?')[:45]} -- {chosen.get('price', 0)} AED")
            # TODO: actually add to cart via browser
        else:
            self.say("Skipping that one.")

        self.session.current_failed_idx += 1
        return self._present_next_failed()

    def _print_final_summary(self):
        """Print the final summary when the cycle is complete."""
        report = self.session.cart_report
        if not report:
            self.say("Order cycle complete.")
            return

        lines = ["\n--- ORDER READY ---"]
        for store_name, sr in report.get("stores", {}).items():
            if sr["items_added"] > 0:
                lines.append(f"  {store_name}: {sr['items_added']} items, ~{sr['estimated_total']:.0f} AED")
                lines.append(f"    -> Open the browser and review your cart")

        if self.session.dry_run:
            lines.append("\n[DRY RUN] No items were actually added to carts.")
        else:
            lines.append("\nBrowser is still open. Review and place orders yourself!")

        self.say("\n".join(lines))

    # ────────────────────────────────────────────
    # STEP 4: POST-ORDER CHECK
    # ────────────────────────────────────────────

    async def post_order_check(self):
        """
        After the user places the order, scrape what was actually bought,
        check status of pending orders, and run gap analysis.

        This runs separately from the main order flow -- either triggered
        manually or by the scheduler.
        """
        from order_scraper import check_and_scrape_orders

        self.say("Checking for new orders and order statuses...")
        self.session.state = State.POST_ORDER

        try:
            result = await check_and_scrape_orders()
        except Exception as e:
            self.say(f"Order scraping failed: {e}")
            self.session.state = State.DONE
            return

        # Report status changes
        status_changes = result.get("status_changes", [])
        if status_changes:
            for change in status_changes:
                self.say(
                    f"Order #{change['order_id']}: "
                    f"{change['old']} -> {change['new']}"
                )

        new_orders = result.get("new_orders", [])
        if not new_orders and not status_changes:
            self.say("No new orders or status changes found.")
            self.session.state = State.DONE
            return

        if not new_orders:
            self.session.state = State.DONE
            return

        # Run gap analysis for each new order
        conn = self._get_conn()
        for order_data in new_orders:
            self.say(f"Found new order: {order_data['store_name']} #{order_data['order_ref']}")

            # Try to match to a cart snapshot
            cur = conn.cursor()
            cur.execute("""
                SELECT cs.id as snapshot_id
                FROM cart_snapshots cs
                JOIN stores s ON cs.store_id = s.id
                WHERE s.name = ?
                ORDER BY cs.built_at DESC
                LIMIT 1
            """, (order_data["store_name"],))
            snap = cur.fetchone()

            if snap:
                # Find the order_id we just saved
                cur.execute("""
                    SELECT o.id FROM orders o
                    JOIN stores s ON o.store_id = s.id
                    WHERE o.order_ref = ? AND s.name = ?
                """, (order_data["order_ref"], order_data["store_name"]))
                order_row = cur.fetchone()

                if order_row:
                    gap_result = analyze_gap(snap["snapshot_id"], order_row["id"], conn=conn)
                    if "error" not in gap_result:
                        summary = gap_result["summary"]
                        self.say(
                            f"Gap analysis: {summary['match_rate']:.0%} match rate -- "
                            f"{summary['matched']} matched, "
                            f"{summary['removed']} removed, "
                            f"{summary['added']} added"
                        )
                        # Apply learnings via preference engine signals
                        signals = apply_learnings(gap_result, conn=conn)
                        if signals:
                            self.say(f"Learned from {len(signals)} gap(s):")
                            for sig in signals:
                                self.say(f"  {sig['item_name']}: {sig['reason']}")
            else:
                self.say(f"  No cart snapshot to compare against.")

        self._close_conn()
        self.session.state = State.DONE

    # ────────────────────────────────────────────
    # FULL CYCLE RUNNER
    # ────────────────────────────────────────────

    async def run_cycle(self, target_date=None):
        """
        Run one complete order cycle: check-in -> review -> build -> report.

        For CLI mode -- handles input/output directly in the terminal.
        """
        # Step 1: Check-in
        self.start_checkin(target_date=target_date)

        if self.session.state == State.DONE:
            return  # nothing to restock

        # Step 2: Interactive review loop
        while self.session.state in (State.CHECKIN, State.REVIEWING):
            try:
                user_input = input("\n  You: ").strip()
            except (EOFError, KeyboardInterrupt):
                self.say("\nOrder cancelled.")
                self.session.state = State.DONE
                return

            if not user_input:
                # Empty enter = approve all
                user_input = "ok"

            needs_more = self.process_input(user_input)

            if self.session.state == State.BUILDING:
                break

        # Step 3: Build carts
        if self.session.state == State.BUILDING:
            if not self.session.dry_run:
                self.say("\nIMPORTANT: Close Edge browser now if it's open!")
                try:
                    confirm = input("  Press Enter to build carts (or 'q' to quit): ").strip()
                except (EOFError, KeyboardInterrupt):
                    self.say("\nCancelled.")
                    return
                if confirm.lower() == 'q':
                    self.say("Cancelled. Your list is saved.")
                    return

            await self.build_carts()

        # Step 4: Handle alternatives
        while self.session.state == State.AWAITING_ALT:
            try:
                user_input = input("\n  You: ").strip()
            except (EOFError, KeyboardInterrupt):
                self.say("\nSkipping remaining items.")
                break

            self.process_input(user_input)

        self.session.completed_at = datetime.now().isoformat()
        self._close_conn()


# ============================================================
# SCHEDULER HELPER
# ============================================================

def next_order_date():
    """Next Tuesday or Friday from today."""
    today = datetime.now().date()
    for d in range(0, 8):
        candidate = today + timedelta(days=d)
        if candidate.weekday() in (1, 4):  # Tue=1, Fri=4
            return candidate.strftime("%Y-%m-%d")
    return (today + timedelta(days=3)).strftime("%Y-%m-%d")


def should_checkin_today():
    """
    Should the agent send a check-in today?

    Check-in happens the day BEFORE an order day.
    Order days: Tue, Fri
    Check-in days: Mon (for Tue), Thu (for Fri)
    """
    today = datetime.now().date()
    return today.weekday() in (0, 3)  # Mon=0, Thu=3


def should_check_orders_today():
    """
    Should the agent check for completed orders today?

    Check on the day after order day to allow time for delivery.
    Order days: Tue, Fri
    Check days: Wed (for Tue orders), Sat (for Fri orders)
    """
    today = datetime.now().date()
    return today.weekday() in (2, 5)  # Wed=2, Sat=5


# ============================================================
# CLI
# ============================================================

async def main():
    target_date = None
    dry_run = False
    status_only = False
    post_order = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--date" and i + 1 < len(args):
            target_date = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        elif args[i] == "--status":
            status_only = True
            i += 1
        elif args[i] == "--post-order":
            post_order = True
            i += 1
        else:
            i += 1

    if status_only:
        print(f"\n  Agent Status")
        print(f"  {'='*40}")
        print(f"  Next order date: {next_order_date()}")
        print(f"  Should check-in today: {should_checkin_today()}")
        print(f"  Should check orders today: {should_check_orders_today()}")
        print(f"  Today: {datetime.now().strftime('%A %Y-%m-%d')}")
        print(f"  {'='*40}")
        return

    agent = AgentBrain(dry_run=dry_run)

    if post_order:
        await agent.post_order_check()
    else:
        await agent.run_cycle(target_date=target_date)


if __name__ == "__main__":
    asyncio.run(main())
