"""
Hearthkeeper - Agent Tool Definitions & Wrappers

Maps 15 Claude tool_use definitions to existing Python functions.
Two parts:
  1. TOOL_DEFINITIONS — JSON schemas passed to Claude API's `tools` param
  2. execute_tool() — dispatcher that calls the right Python function

Tools are thin wrappers. All business logic stays in the existing modules.

Domains:
  - List Operations (6 tools)
  - Product Intelligence (4 tools)
  - Preference Learning (2 tools)
  - Order Lifecycle (2 tools)
  - Household Knowledge (1 tool)

Usage:
    from agent_tools import TOOL_DEFINITIONS, execute_tool
    response = await client.messages.create(..., tools=TOOL_DEFINITIONS)
    result = await execute_tool(tool_name, tool_input, session)
"""

import json
import sqlite3
import logging
from pathlib import Path

DB_PATH = Path(__file__).parent / "autostock.db"
logger = logging.getLogger("agent_tools")


# ============================================================
# HELPER: DB connection factory
# ============================================================

def _get_conn():
    """Get a DB connection with row_factory set."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _item_lookup(item_name, conn):
    """
    Look up an item in the items table by name.
    Returns dict with id, name, preferred_store_id, category or None.
    """
    from message_handler import match_item_to_db
    result = match_item_to_db(item_name, conn)
    return result  # {"item_id": int, "name": str, "store_id": int, "store_name": str} or None


def _ensure_list(session, conn=None):
    """
    Ensure a cart (shopping list) exists on the session for adding items.
    If no cart_list_id, creates a new cart and sets session.cart_list_id + list_id.
    Returns the cart list_id (never adds to suggestions).

    Uses session.target_date instead of today (fixes reconnect bug where
    chat-created lists would vanish because they had the wrong target_date).
    """
    # If cart exists, use it
    cart_id = getattr(session, "cart_list_id", None)
    if cart_id:
        return cart_id

    # Two-entity mode: suggestion exists but no cart — must create a new cart
    # (don't fall through to list_id which may point to the suggestion list)
    suggestion_id = getattr(session, "suggestion_list_id", None)
    if not suggestion_id:
        # Legacy mode (no dual-list): use list_id if available
        list_id = getattr(session, "list_id", None)
        if list_id:
            return list_id

    # No cart — create one
    target = getattr(session, "target_date", None)
    if not target:
        from datetime import date as _date
        target = _date.today().isoformat()

    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO shopping_lists
            (name, target_date, status, notes, list_type, source)
            VALUES (?, ?, 'draft', 'User-curated order', 'cart', 'chat')
        """, ("Your Cart", target))
        list_id = cur.lastrowid
        conn.commit()
        session.list_id = list_id
        session.cart_list_id = list_id
        session.target_date = target
        logger.info(f"Auto-created cart #{list_id} for session")
        return list_id
    finally:
        if own_conn:
            conn.close()


def _active_scheduled_list_id(conn):
    """Newest open AI-suggestion list (the Scheduled tab the user sees)."""
    row = conn.execute(
        """SELECT id FROM shopping_lists
           WHERE (list_type = 'suggestion' OR list_type IS NULL)
             AND status IN ('draft','confirmed','suggested','in_review')
             AND COALESCE(dismissed_at,'') = ''
           ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    return (row["id"] if row else None) if row else None


def _resolve_edit_list(session, conn=None):
    """The list chat edits should apply to — the one the user is VIEWING.

    Previously every edit targeted the cart, so 'remove the cola' / 'skip
    salmon' were refused (cola/salmon live on the Scheduled list) and adds
    landed in a tab the user wasn't looking at. Now we honor
    session.active_view: 'cart' -> the cart; otherwise (default 'scheduled')
    -> the active suggestion list. Falls back to the cart if no scheduled
    list exists.
    """
    view = (getattr(session, "active_view", None) or "scheduled").lower()
    if view == "cart":
        return _ensure_list(session, conn=conn)
    own = conn is None
    if own:
        conn = _get_conn()
    try:
        # ALWAYS target the live active Scheduled list (the one the user is
        # viewing), NOT the cached session.suggestion_list_id — that cache is set
        # once at connect and goes stale when a newer list is generated, so edits
        # were landing on an old list while the UI showed the new one (chat
        # "confirmed" but nothing changed). Fall back to the cache only if no
        # active suggestion list exists.
        sid = _active_scheduled_list_id(conn) or getattr(session, "suggestion_list_id", None)
        if sid:
            session.suggestion_list_id = sid
            session.list_id = sid
            return sid
    finally:
        if own:
            conn.close()
    return _ensure_list(session, conn=conn)


# ============================================================
# TOOL DEFINITIONS (Claude API schema)
# ============================================================

TOOL_DEFINITIONS = [
    # ── Domain 1: List Operations ──
    {
        "name": "get_shopping_list",
        "description": (
            "Get the current shopping list with all items, stores, prices, and statuses. "
            "Call this when the user asks 'show me the list', 'what's on the list', or "
            "when you need to reference list contents before making changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "integer",
                    "description": "Specific list ID. If omitted, uses the current session's list."
                }
            },
            "required": []
        }
    },
    {
        "name": "add_item_to_list",
        "description": (
            "Add 1-2 items to the shopping list. Matches the item name to our product database, "
            "finds the best product across stores (using preferences, organic rules, sizing), "
            "and adds it. Use batch_curate instead when adding 3+ items at once. "
            "IMPORTANT: When the user mentions a specific brand (e.g. 'balade', 'al ain', 'lurpak'), "
            "always pass it as brand_hint so the curator can pick the right product."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "Item to add, e.g. 'organic eggs', 'butter', 'salmon fillet'"
                },
                "quantity": {
                    "type": "number",
                    "description": "How many to order. Defaults to 1.",
                    "default": 1
                },
                "store_hint": {
                    "type": "string",
                    "description": "Optional store preference, e.g. 'Kibsons' or 'Carrefour'"
                },
                "brand_hint": {
                    "type": "string",
                    "description": "Brand name if the user mentioned one, e.g. 'balade', 'al ain', 'lurpak', 'president'"
                }
            },
            "required": ["item_name"]
        }
    },
    {
        "name": "remove_item_from_list",
        "description": (
            "Remove/skip an item from the shopping list. "
            "Use when the user says 'skip salmon', 'remove the eggs', 'no cheese this time'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "Item to remove, e.g. 'salmon', 'eggs'"
                }
            },
            "required": ["item_name"]
        }
    },
    {
        "name": "change_item_quantity",
        "description": (
            "Change the quantity of an item already on the list. "
            "Use when the user says '2 packs of eggs', 'double the milk', 'make it 3'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "Item to change, e.g. 'eggs', 'milk'"
                },
                "quantity": {
                    "type": "number",
                    "description": "New quantity"
                }
            },
            "required": ["item_name", "quantity"]
        }
    },
    {
        "name": "approve_list",
        "description": (
            "Approve the current shopping list and move to cart building. "
            "Call when the user says 'looks good', 'ok', 'go ahead', 'perfect', 'approved'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "reset_list",
        "description": (
            "Reset the shopping list back to the original AI suggestion. "
            "Use when the user says 'start over', 'reset', 'redo the list'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },

    # ── Domain 2: Product Intelligence ──
    {
        "name": "batch_curate",
        "description": (
            "Smart batch curation for 3+ items. Takes item names and finds the best product "
            "for each across all stores, considering preferences, organic rules, portion sizing, "
            "and minimum order values. Returns matched products with scores and reasons. "
            "Items that can't be matched (like generic 'meat') return suggestions to ask about."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of item names, e.g. ['eggs', 'milk', 'banana', 'spinach']"
                }
            },
            "required": ["item_names"]
        }
    },
    {
        "name": "search_products",
        "description": (
            "Search the product catalog for items. Use when you need to find specific products, "
            "explore what's available, or when the user asks about an unfamiliar item. "
            "Returns products with name, brand, price, size, organic status, and store."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'organic butter', 'lentil pasta', 'wagyu'"
                },
                "store": {
                    "type": "string",
                    "description": "Optional: restrict to 'Kibsons' or 'Carrefour UAE'"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 10)",
                    "default": 10
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "explain_recommendation",
        "description": (
            "Explain why a specific product was recommended for an item. "
            "Shows the scoring breakdown: preference matches, organic tier, size fit, "
            "origin quality, brand history, and any compromises made. "
            "Use when the user asks 'why this brand?', 'why not organic?', 'why Carrefour?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "The item to explain, e.g. 'eggs', 'milk'"
                }
            },
            "required": ["item_name"]
        }
    },
    {
        "name": "get_alternatives",
        "description": (
            "Get ranked alternative products for an item. Shows what else is available "
            "across stores with scores and reasons. Use when the user wants to see options "
            "or isn't happy with the current pick."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "Item to find alternatives for, e.g. 'milk', 'eggs'"
                },
                "store": {
                    "type": "string",
                    "description": "Optional: restrict to one store"
                }
            },
            "required": ["item_name"]
        }
    },

    # ── Domain 3: Preference Learning ──
    {
        "name": "record_preference",
        "description": (
            "Record a user preference so the system learns. ALWAYS call this when the user "
            "states a preference like 'I prefer organic', 'never Kraft', 'get eggs from Kibsons', "
            "'only plain yogurt', 'I like Australian beef'. The preference is stored permanently "
            "and affects all future scoring."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "Which item this preference is about, e.g. 'eggs', 'yogurt'. Can be empty for global preferences."
                },
                "preference_type": {
                    "type": "string",
                    "enum": ["brand", "store", "attribute", "product", "size", "origin"],
                    "description": (
                        "What kind of preference: "
                        "'brand' for 'I prefer Al Rawabi', 'never Kraft'; "
                        "'store' for 'get eggs from Kibsons'; "
                        "'attribute' for 'only organic', 'no preservatives'; "
                        "'product' for 'I want this specific product'; "
                        "'size' for 'always 500ml kefir'; "
                        "'origin' for 'I like Australian beef'"
                    )
                },
                "action": {
                    "type": "string",
                    "enum": ["prefer", "avoid", "require", "block"],
                    "description": (
                        "'prefer' = bonus points, 'avoid' = penalty, "
                        "'require' = must have (hard gate), 'block' = never pick (hard reject)"
                    )
                },
                "value": {
                    "type": "string",
                    "description": "The preference value, e.g. 'organic', 'Kraft', 'Kibsons', 'Australian', '500ml'"
                }
            },
            "required": ["preference_type", "action", "value"]
        }
    },
    {
        "name": "get_preferences",
        "description": (
            "Get the current known preferences for an item. Shows what the system has learned: "
            "brand preferences, organic requirements, size targets, store preferences, etc. "
            "Use when the user asks 'what do you know about my egg preferences?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "Item to check, e.g. 'eggs', 'milk'"
                }
            },
            "required": ["item_name"]
        }
    },

    {
        "name": "stop_suggesting_item",
        "description": (
            "Permanently stop suggesting an item. Call this when the user says "
            "'stop suggesting X', 'never suggest X', 'we don't want X anymore', "
            "'remove X for good'. This writes a durable preference so X won't "
            "appear on future scheduled lists. Different from removing X from the "
            "current list (which is one-time)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {"type": "string", "description": "The item to stop suggesting, e.g. 'striploin', 'soft drink'."}
            },
            "required": ["item_name"]
        }
    },
    {
        "name": "set_item_frequency",
        "description": (
            "Make the system suggest an item MORE or LESS often. Call this for "
            "'buy beef less often', 'we go through milk faster', 'suggest eggs "
            "more often', 'we don't eat much X'. Adjusts how frequently X is "
            "auto-suggested. Durable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {"type": "string", "description": "The item, e.g. 'beef mince'."},
                "direction": {"type": "string", "enum": ["less", "more"], "description": "'less' = suggest less often, 'more' = more often."}
            },
            "required": ["item_name", "direction"]
        }
    },
    {
        "name": "set_brand_rule",
        "description": (
            "Record a durable brand preference for a whole CATEGORY. Call this for "
            "'we don't like Pepsi, we prefer Coca-Cola' (avoid Pepsi + prefer "
            "Coca-Cola in soft drinks), 'no Angus beef' (avoid Angus in meat). "
            "Applies to all future picks in that category, not just one item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string", "description": "Brand name, e.g. 'Pepsi', 'Coca-Cola', 'Angus'."},
                "category": {"type": "string", "description": "Category in the user's words, e.g. 'soft drink', 'meat', 'pasta'."},
                "action": {"type": "string", "enum": ["avoid", "prefer"], "description": "'avoid' = never pick this brand here; 'prefer' = favour it."}
            },
            "required": ["brand", "category", "action"]
        }
    },

    # ── Domain 4: Order Lifecycle ──
    {
        "name": "get_order_status",
        "description": (
            "Check the status of recent orders. Shows active orders being delivered, "
            "delivered orders awaiting rating, and recent order history. "
            "Use when the user asks 'where's my order?', 'did it arrive?', "
            "'what did we order last time?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "cancel_order",
        "description": (
            "Cancel the current order flow. List is preserved but no carts are built. "
            "Use when user says 'cancel', 'stop', 'never mind'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "delay_order",
        "description": (
            "Delay the current order. Keeps the list but pauses the flow. "
            "Use when user says 'later', 'tomorrow', 'not now'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },

    # ── Domain 5: Household Knowledge ──
    {
        "name": "get_household_info",
        "description": (
            "Look up household rules and preferences for an item. Returns organic tier, "
            "portion sizing, product type gates, and origin sensitivity. "
            "Use when the user asks 'why organic eggs?', 'what are my rules for pasta?', "
            "or when you need to explain a decision."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "Item to look up, e.g. 'eggs', 'pasta', 'yogurt'"
                }
            },
            "required": ["item_name"]
        }
    },
]


# ============================================================
# TOOL IMPLEMENTATIONS
# ============================================================

def _tool_get_shopping_list(params, session):
    """Get current shopping list with product details."""
    from list_generate import list_get
    from frontend_bridge import build_frontend_list

    list_id = params.get("list_id") or getattr(session, "list_id", None)
    if not list_id:
        return {"error": "No active shopping list. Start by telling me what you need."}

    conn = _get_conn()
    try:
        raw_list = list_get(list_id, conn)
        if not raw_list:
            return {"error": f"List #{list_id} not found."}

        # Build enriched frontend view for rich item details
        frontend = build_frontend_list(list_id, conn)

        # Build a concise summary for the LLM
        items_summary = []
        total = 0
        for item in raw_list.get("items", []):
            if item.get("user_action") == "skipped":
                continue
            name = item.get("item_name", "?")
            qty = item.get("quantity", 1)
            store = item.get("store_name", "?")
            price = item.get("estimated_price") or item.get("price_each")
            product = item.get("product_name", "")
            status = item.get("status", "")

            entry = {"name": name, "quantity": qty, "store": store}
            if product and product != name:
                entry["product"] = product
            if price:
                entry["price_aed"] = round(price, 2)
                total += price * qty
            if status and status not in ("approved", "matched"):
                entry["status"] = status
            entry["list_item_id"] = item.get("id")
            items_summary.append(entry)

        return {
            "list_id": list_id,
            "target_date": raw_list.get("target_date"),
            "item_count": len(items_summary),
            "estimated_total_aed": round(total, 2),
            "items": items_summary,
        }
    finally:
        conn.close()


def _tool_add_item_to_list(params, session):
    """Add a single item to the list with smart matching."""
    from list_generate import list_add_item
    from basket_curator import curate_basket

    # Target the list the user is VIEWING (scheduled or cart), not always cart.
    list_id = _resolve_edit_list(session)

    item_name = params["item_name"]
    quantity = params.get("quantity", 1)
    store_hint = params.get("store_hint")
    brand_hint = params.get("brand_hint")

    # Build curator query: prepend brand so qualifier matching can boost the right product
    # e.g. brand_hint="balade", item_name="yogurt" → curator_query="balade yogurt"
    curator_query = item_name
    if brand_hint:
        brand_lower = brand_hint.strip().lower()
        # Only prepend if brand isn't already in item_name
        if brand_lower not in item_name.lower():
            curator_query = f"{brand_hint.strip()} {item_name}"

    conn = _get_conn()
    try:
        # Use curator for single-item curation (gets scoring, matching, preference checks)
        result = curate_basket([curator_query], list_id=list_id)

        items = result.get("items", [])
        unmatched = result.get("unmatched", [])

        # Handle unmatched / needs_input items
        # unmatched is a list of item name strings (not dicts)
        if unmatched:
            um = unmatched[0]
            um_name = um if isinstance(um, str) else um.get("input_name", item_name)
            return {
                "status": "needs_input",
                "item_name": um_name,
                "reason": "Could not match to a product",
                "did_you_mean": [],
                "message": f"I couldn't find a match for '{um_name}'. Can you be more specific?"
            }

        if not items:
            return {
                "status": "not_found",
                "item_name": item_name,
                "message": f"No products found for '{item_name}' in any store."
            }

        # Found a match — extract recommended option details
        matched = items[0]
        rec_store_id = matched.get("recommended_store_id")
        rec_store_name = matched.get("recommended_store", "")

        # Get catalog_id and details from the recommended option
        rec_opt = {}
        for opt in matched.get("options", []):
            if opt.get("store_id") == rec_store_id:
                rec_opt = opt
                break
        if not rec_opt and matched.get("options"):
            rec_opt = matched["options"][0]

        store_id = rec_store_id
        store_name = rec_store_name

        # Override store ONLY if the user asked for one AND the curator actually
        # found this item there. NEVER relabel one store's product as another
        # store — that was storing a Carrefour product under a "Kibsons" label
        # (correct store_id, but catalog_id/URL/price still Carrefour's).
        if store_hint:
            store_row = conn.execute(
                "SELECT id, name FROM stores WHERE LOWER(name) LIKE ?",
                (f"%{store_hint.lower()}%",)
            ).fetchone()
            if store_row:
                hinted_opt = next(
                    (o for o in matched.get("options", [])
                     if o.get("store_id") == store_row["id"]),
                    None,
                )
                if hinted_opt:
                    rec_opt = hinted_opt
                    store_id = store_row["id"]
                    store_name = store_row["name"]
                else:
                    return {
                        "status": "not_found",
                        "item_name": item_name,
                        "store": store_row["name"],
                        "message": f"I couldn't find {item_name} at {store_row['name']}.",
                    }

        # Derive EVERY stored field from the catalog row the chosen option points
        # at, so store_id, store_name, product, catalog_id, URL and price can
        # never disagree. This is the authoritative source of truth.
        catalog_id = rec_opt.get("catalog_id")
        price_aed = matched.get("recommended_price")
        product_name = matched.get("recommended_product", "")
        is_organic = rec_opt.get("is_organic", False)
        category_override = rec_opt.get("category")
        if catalog_id:
            cat = conn.execute(
                """SELECT pc.product_name, pc.price, pc.store_id, pc.is_organic,
                          pc.category, s.name AS store_name
                   FROM product_catalog pc JOIN stores s ON s.id = pc.store_id
                   WHERE pc.id = ?""",
                (catalog_id,),
            ).fetchone()
            if cat:
                store_id = cat["store_id"]
                store_name = cat["store_name"]
                price_aed = cat["price"]
                product_name = cat["product_name"]
                is_organic = bool(cat["is_organic"])
                category_override = cat["category"]

        list_item_id = list_add_item(
            list_id=list_id,
            item_name=matched.get("input_name", item_name),
            store_id=store_id,
            store_name=store_name,
            item_id=matched.get("matched_item_id"),
            quantity=quantity,
            estimated_price=price_aed,
            catalog_id=catalog_id,
            category_override=category_override,
            status="approved",
            conn=conn,
        )

        # Transition to reviewing state after adding items
        if getattr(session, "state", None) not in ("reviewing", "building"):
            session.state = "reviewing"

        sel_reason = matched.get("selection_reason", {})
        return {
            "status": "added",
            "list_item_id": list_item_id,
            "item_name": matched.get("input_name", item_name),
            "product_name": product_name,
            "store": store_name,
            "price_aed": price_aed,
            "quantity": quantity,
            "selection_reason": sel_reason.get("summary", "") if isinstance(sel_reason, dict) else str(sel_reason),
            "organic": is_organic,
        }
    finally:
        conn.close()


def _tool_remove_item_from_list(params, session):
    """Remove/skip an item from the list by name matching."""
    from list_generate import list_get, list_update_item

    list_id = _resolve_edit_list(session)  # the list the user is viewing
    if not list_id:
        return {"error": "No active list."}

    item_name = params["item_name"].lower().strip()
    conn = _get_conn()
    try:
        shopping_list = list_get(list_id, conn)
        if not shopping_list:
            return {"error": f"List #{list_id} not found."}

        # Find the item by fuzzy name match
        best_match = None
        best_score = 0
        for item in shopping_list.get("items", []):
            if item.get("user_action") == "skipped":
                continue
            db_name = (item.get("item_name") or "").lower()
            # Exact match
            if db_name == item_name:
                best_match = item
                best_score = 100
                break
            # Substring match
            if item_name in db_name or db_name in item_name:
                score = 80
                if score > best_score:
                    best_match = item
                    best_score = score

        if not best_match:
            return {
                "status": "not_found",
                "item_name": item_name,
                "message": f"'{item_name}' is not on the current list."
            }

        _li_id = best_match.get("list_item_id") or best_match.get("id")
        list_update_item(_li_id, "skip", conn=conn)

        return {
            "status": "removed",
            "item_name": best_match.get("item_name", item_name),
            "list_item_id": _li_id,
        }
    finally:
        conn.close()


def _tool_change_item_quantity(params, session):
    """Change quantity of a list item."""
    from list_generate import list_get, list_update_item

    list_id = _resolve_edit_list(session)  # the list the user is viewing
    if not list_id:
        return {"error": "No active list."}

    item_name = params["item_name"].lower().strip()
    new_qty = params["quantity"]
    conn = _get_conn()
    try:
        shopping_list = list_get(list_id, conn)
        if not shopping_list:
            return {"error": f"List #{list_id} not found."}

        # Find the item
        best_match = None
        for item in shopping_list.get("items", []):
            if item.get("user_action") == "skipped":
                continue
            db_name = (item.get("item_name") or "").lower()
            if db_name == item_name or item_name in db_name or db_name in item_name:
                best_match = item
                if db_name == item_name:
                    break

        if not best_match:
            return {
                "status": "not_found",
                "item_name": item_name,
                "message": f"'{item_name}' is not on the current list."
            }

        old_qty = best_match.get("quantity", 1)
        _li_id = best_match.get("list_item_id") or best_match.get("id")
        list_update_item(_li_id, "modify", quantity=new_qty, conn=conn)

        return {
            "status": "updated",
            "item_name": best_match.get("item_name", item_name),
            "old_quantity": old_qty,
            "new_quantity": new_qty,
            "list_item_id": _li_id,
        }
    finally:
        conn.close()


def _tool_approve_list(params, session):
    """Approve the list and transition to cart building."""
    from list_generate import list_finalize

    list_id = getattr(session, "list_id", None)
    if not list_id:
        return {"error": "No active list to approve."}

    conn = _get_conn()
    try:
        result = list_finalize(list_id, conn)
        # Update session state
        session.state = "building"

        return {
            "status": "approved",
            "list_id": list_id,
            "approved_items": result.get("approved", 0),
            "skipped_items": result.get("skipped", 0),
            "message": "List approved! Starting cart building.",
        }
    finally:
        conn.close()


def _tool_reset_list(params, session):
    """Reset list to original AI suggestion."""
    from list_generate import list_reset_to_original

    list_id = getattr(session, "list_id", None)
    if not list_id:
        return {"error": "No active list to reset."}

    conn = _get_conn()
    try:
        result = list_reset_to_original(list_id, conn)
        session.state = "reviewing"

        return {
            "status": "reset",
            "restored_items": result.get("restored", 0),
            "removed_user_adds": result.get("removed_user_adds", 0),
            "message": "List reset to original suggestion. Review and let me know what changes you want.",
        }
    finally:
        conn.close()


def _tool_batch_curate(params, session):
    """Batch curate multiple items across stores."""
    from basket_curator import curate_basket

    # Auto-create list if needed
    list_id = _ensure_list(session)
    item_names = params["item_names"]

    result = curate_basket(item_names, list_id=list_id)

    # Helper: extract details from the recommended option in options array
    def _get_recommended_option(item):
        rec_store = item.get("recommended_store_id")
        for opt in item.get("options", []):
            if opt.get("store_id") == rec_store:
                return opt
        # Fallback: first option if available
        opts = item.get("options", [])
        return opts[0] if opts else {}

    # Separate matched vs unmatched items
    unmatched_names = set(result.get("unmatched", []))

    # Format matched items for LLM consumption
    matched = []
    for item in result.get("items", []):
        if item.get("input_name") in unmatched_names:
            continue  # handled below in needs_input
        rec_opt = _get_recommended_option(item)
        sel_reason = item.get("selection_reason", {})
        entry = {
            "item_name": item.get("input_name", "?"),
            "product_name": item.get("recommended_product", ""),
            "store": item.get("recommended_store", ""),
            "price_aed": item.get("recommended_price"),
            "quantity": item.get("recommended_qty", 1),
            "score": rec_opt.get("score"),
            "organic": rec_opt.get("is_organic", False),
            "selection_reason": sel_reason.get("summary", "") if isinstance(sel_reason, dict) else str(sel_reason),
        }
        if sel_reason and isinstance(sel_reason, dict):
            entry["directive_met"] = sel_reason.get("directive_met")
            if sel_reason.get("compromises"):
                entry["compromises"] = sel_reason["compromises"]
        matched.append(entry)

    # Unmatched / needs_input items — pull full data from items list
    needs_input = []
    for item in result.get("items", []):
        if item.get("input_name") not in unmatched_names:
            continue
        fc = item.get("failure_context") or {}
        # did_you_mean can be on the item directly (generic terms) or in failure_context
        dym = item.get("did_you_mean") or fc.get("did_you_mean") or []
        # Simplify suggestions for LLM consumption
        suggestions = []
        for s in dym[:6]:
            if isinstance(s, dict):
                suggestions.append(s.get("name", str(s)))
            else:
                suggestions.append(str(s))
        needs_input.append({
            "item_name": item.get("input_name", "?"),
            "reason": fc.get("reason_human") or fc.get("reason") or "Could not match",
            "is_generic": item.get("is_generic", False),
            "did_you_mean": suggestions,
        })

    # Add matched items to the list (skip unmatched/needs_input)
    if matched:
        from list_generate import list_add_item
        conn = _get_conn()
        try:
            for item in result.get("items", []):
                if item.get("input_name") in unmatched_names:
                    continue
                rec_opt = _get_recommended_option(item)
                list_add_item(
                    list_id=list_id,
                    item_name=item.get("input_name", ""),
                    store_id=item.get("recommended_store_id"),
                    store_name=item.get("recommended_store", ""),
                    item_id=item.get("matched_item_id"),
                    quantity=item.get("recommended_qty", 1),
                    estimated_price=item.get("recommended_price"),
                    catalog_id=rec_opt.get("catalog_id"),
                    category_override=rec_opt.get("category"),
                    status="approved",
                    conn=conn,
                )
        finally:
            conn.close()

    # Transition to reviewing state
    if getattr(session, "state", None) not in ("reviewing", "building"):
        session.state = "reviewing"

    # Store curation result on session for live verification in app.py
    session._batch_curation_result = result

    return {
        "matched_count": len(matched),
        "needs_input_count": len(needs_input),
        "items": matched,
        "needs_input": needs_input,
        "vendor_split": result.get("vendor_split", {}),
        "estimated_total_aed": result.get("total"),
    }


def _tool_search_products(params, session):
    """Search the product catalog."""
    from product_knowledge import search_catalog

    query = params["query"]
    store_name = params.get("store")
    limit = params.get("limit", 10)

    conn = _get_conn()
    try:
        # Resolve store name to ID
        store_id = None
        if store_name:
            row = conn.execute(
                "SELECT id FROM stores WHERE LOWER(name) LIKE ?",
                (f"%{store_name.lower()}%",)
            ).fetchone()
            if row:
                store_id = row["id"]

        results = search_catalog(query, store_id=store_id, limit=limit, conn=conn)

        # Simplify for LLM
        products = []
        for p in results:
            entry = {
                "product_name": p.get("product_name") or p.get("name", "?"),
                "brand": p.get("brand", ""),
                "price_aed": p.get("price"),
                "size": p.get("size", ""),
                "store": p.get("store_name", ""),
                "organic": p.get("is_organic", False),
            }
            if p.get("origin_country"):
                entry["origin"] = p["origin_country"]
            products.append(entry)

        return {
            "query": query,
            "result_count": len(products),
            "products": products,
        }
    finally:
        conn.close()


def _tool_explain_recommendation(params, session):
    """Explain why a product was chosen for an item."""
    from product_score import score_single_product, get_purchase_history
    from preference_engine import get_preference_explanation, get_scoring_profile
    from list_generate import list_get

    item_name = params["item_name"]
    list_id = getattr(session, "list_id", None)

    conn = _get_conn()
    try:
        # Find the item on the current list
        current_product = None
        if list_id:
            shopping_list = list_get(list_id, conn)
            if shopping_list:
                for item in shopping_list.get("items", []):
                    if item.get("user_action") == "skipped":
                        continue
                    if item_name.lower() in (item.get("item_name") or "").lower():
                        current_product = item
                        break

        # Get item from DB for preference lookup
        item_match = _item_lookup(item_name, conn)
        item_id = item_match["item_id"] if item_match else None

        explanation = {
            "item_name": item_name,
        }

        # Current selection on list
        if current_product:
            explanation["current_pick"] = {
                "product": current_product.get("product_name", ""),
                "store": current_product.get("store_name", ""),
                "price_aed": current_product.get("estimated_price"),
            }

        # Preference explanation
        if item_id:
            pref_lines = get_preference_explanation(item_id, conn)
            if pref_lines:
                explanation["preferences"] = pref_lines

        # Scoring breakdown (if we have the product details)
        if current_product and current_product.get("catalog_id"):
            catalog = conn.execute(
                "SELECT * FROM product_catalog WHERE id = ?",
                (current_product["catalog_id"],)
            ).fetchone()
            if catalog:
                product_dict = dict(catalog)
                store_name = current_product.get("store_name", "")
                history = get_purchase_history(item_name, conn)
                score_result = score_single_product(
                    item_name, store_name, product_dict,
                    purchase_history=history, item_id=item_id
                )
                explanation["score"] = score_result.get("score")
                explanation["tier"] = score_result.get("tier")
                explanation["reasons"] = score_result.get("reasons", [])
                if score_result.get("compromises"):
                    explanation["compromises"] = score_result["compromises"]

        # Household intelligence
        try:
            from household_profile import get_household_intelligence
            intel = get_household_intelligence(item_name)
            if intel:
                explanation["household_rules"] = {
                    "organic_tier": intel.get("organic_tier"),
                    "size_rule": intel.get("portion_rule", {}).get("note") if intel.get("portion_rule") else None,
                    "type_gate": intel.get("type_gate", {}).get("directive") if intel.get("type_gate") else None,
                }
        except ImportError:
            pass

        return explanation
    finally:
        conn.close()


def _tool_get_alternatives(params, session):
    """Get ranked alternatives for an item across stores."""
    from product_knowledge import get_ranked_alternatives, search_catalog

    item_name = params["item_name"]
    store_name = params.get("store")

    conn = _get_conn()
    try:
        item_match = _item_lookup(item_name, conn)
        alternatives = []

        if item_match:
            item_id = item_match["item_id"]

            # Get stores to search
            stores = conn.execute(
                "SELECT id, name FROM stores WHERE is_active = 1"
            ).fetchall()

            if store_name:
                stores = [s for s in stores if store_name.lower() in s["name"].lower()]

            for store in stores:
                alts = get_ranked_alternatives(item_id, store["id"], limit=5, conn=conn)
                for alt in alts:
                    alternatives.append({
                        "product_name": alt.get("product_name") or alt.get("name", "?"),
                        "brand": alt.get("brand", ""),
                        "price_aed": alt.get("price"),
                        "size": alt.get("size", ""),
                        "store": store["name"],
                        "score": alt.get("score"),
                        "tier": alt.get("tier", ""),
                        "organic": alt.get("is_organic", False),
                    })

        # If no pre-scored alternatives, search catalog directly
        if not alternatives:
            results = search_catalog(item_name, limit=10, conn=conn)
            for r in results:
                alternatives.append({
                    "product_name": r.get("product_name") or r.get("name", "?"),
                    "brand": r.get("brand", ""),
                    "price_aed": r.get("price"),
                    "size": r.get("size", ""),
                    "store": r.get("store_name", ""),
                    "organic": r.get("is_organic", False),
                })

        # Sort by score desc, then price asc
        alternatives.sort(key=lambda a: (-a.get("score", 0), a.get("price_aed", 999)))

        return {
            "item_name": item_name,
            "alternatives_count": len(alternatives),
            "alternatives": alternatives[:10],  # Cap at 10 for token budget
        }
    finally:
        conn.close()


def _tool_record_preference(params, session):
    """Record a user preference via the preference engine."""
    from preference_engine import emit_signal

    item_name = params.get("item_name", "")
    pref_type = params["preference_type"]
    action = params["action"]
    value = params["value"]

    conn = _get_conn()
    try:
        # Look up item_id if item_name provided
        item_id = None
        if item_name:
            item_match = _item_lookup(item_name, conn)
            if item_match:
                item_id = item_match["item_id"]

        # Map action + type to signal_type
        signal_type_map = {
            ("brand", "prefer"): "prefer_brand",
            ("brand", "avoid"): "avoid_brand",
            ("brand", "block"): "block_brand",
            ("brand", "require"): "prefer_brand",
            ("store", "prefer"): "prefer_store",
            ("store", "avoid"): "avoid_store",
            ("store", "require"): "prefer_store",
            ("attribute", "prefer"): "prefer_attribute",
            ("attribute", "avoid"): "avoid_attribute",
            ("attribute", "require"): "require_attribute",
            ("attribute", "block"): "block_attribute",
            ("product", "prefer"): "pin_product",
            ("product", "block"): "wrong_item",
            ("size", "prefer"): "size_preference",
            ("size", "require"): "size_preference",
            ("origin", "prefer"): "prefer_origin",
            ("origin", "avoid"): "avoid_origin",
        }

        signal_type = signal_type_map.get((pref_type, action), f"{action}_{pref_type}")

        # Map preference_type to dimension
        dimension_map = {
            "brand": "brand",
            "store": "store",
            "attribute": "attribute",
            "product": "product",
            "size": "quantity",
            "origin": "attribute",
        }
        dimension = dimension_map.get(pref_type, "attribute")

        # Strength: blocks/requires are stronger than preferences
        strength_map = {
            "block": 1.0,
            "require": 0.95,
            "avoid": 0.8,
            "prefer": 0.7,
        }
        strength = strength_map.get(action, 0.7)

        emit_signal(
            item_id=item_id,
            signal_type=signal_type,
            signal_source="chat",
            dimension=dimension,
            old_value=None,
            new_value=value,
            strength=strength,
            context={"action": action, "raw_item_name": item_name, "pref_type": pref_type},
            conn=conn,
        )

        return {
            "status": "recorded",
            "item_name": item_name or "(global)",
            "preference": f"{action} {pref_type}: {value}",
            "message": f"Got it — I'll {'always' if action == 'require' else 'never' if action == 'block' else action} {value} for {item_name or 'all items'}.",
        }
    finally:
        conn.close()


def _tool_stop_suggesting(params, session):
    """Durably stop suggesting an item (writes user_item_prefs stop_suggesting).

    This is what makes 'stop suggesting striploin' actually stick — it was
    previously only narrated. Returns a clear recorded/not_found so the
    agent can only confirm a real write.
    """
    item_name = (params.get("item_name") or "").strip()
    if not item_name:
        return {"status": "error", "message": "No item given."}
    conn = _get_conn()
    try:
        match = _item_lookup(item_name, conn)
        if not match:
            return {"status": "not_found", "item_name": item_name,
                    "message": f"I don't track an item called '{item_name}', so I can't stop suggesting it."}
        item_id = match["item_id"]
        # De-dup: reactivate or insert. value 'user:chat' (not auto:*) so the
        # E6 auto-reset on re-approve won't silently undo a deliberate stop.
        existing = conn.execute(
            "SELECT id FROM user_item_prefs WHERE item_id=? AND pref_type='stop_suggesting'",
            (item_id,)).fetchone()
        if existing:
            conn.execute("UPDATE user_item_prefs SET is_active=1, value='user:chat' WHERE id=?",
                         (existing["id"] if hasattr(existing, "__getitem__") else existing[0],))
        else:
            conn.execute(
                "INSERT INTO user_item_prefs (item_id, pref_type, value, is_active) "
                "VALUES (?, 'stop_suggesting', 'user:chat', 1)", (item_id,))
        conn.commit()
        return {"status": "recorded", "item_name": match.get("name") or item_name,
                "message": f"Won't suggest {match.get('name') or item_name} on future lists."}
    finally:
        conn.close()


def _tool_set_item_frequency(params, session):
    """Make an item suggested MORE or LESS often by scaling its cadence
    (restock_rules.avg_days_between). 'less' multiplies the gap (×1.6),
    'more' shrinks it (÷1.6). Durable; recomputes next_restock_date.
    """
    item_name = (params.get("item_name") or "").strip()
    direction = (params.get("direction") or "").strip().lower()
    if not item_name or direction not in ("less", "more"):
        return {"status": "error", "message": "Need item_name and direction (less/more)."}
    conn = _get_conn()
    try:
        match = _item_lookup(item_name, conn)
        if not match:
            return {"status": "not_found", "item_name": item_name,
                    "message": f"I don't track '{item_name}', so I can't change how often it's suggested."}
        item_id = match["item_id"]
        rule = conn.execute(
            "SELECT avg_days_between, last_ordered_date FROM restock_rules WHERE item_id=?",
            (item_id,)).fetchone()
        if not rule or not rule["avg_days_between"]:
            return {"status": "not_found", "item_name": match.get("name") or item_name,
                    "message": f"No cadence on record for {item_name} yet."}
        cur_cad = float(rule["avg_days_between"])
        factor = 1.6 if direction == "less" else (1 / 1.6)
        new_cad = round(max(1.0, min(120.0, cur_cad * factor)), 2)
        from datetime import datetime as _dt, timedelta as _td
        next_date = None
        last = (rule["last_ordered_date"] or "")[:10]
        if last and len(last) == 10:
            try:
                next_date = (_dt.strptime(last, "%Y-%m-%d") + _td(days=int(round(new_cad)))).strftime("%Y-%m-%d")
            except ValueError:
                next_date = None
        if next_date:
            conn.execute("UPDATE restock_rules SET avg_days_between=?, next_restock_date=?, updated_at=datetime('now') WHERE item_id=?",
                         (new_cad, next_date, item_id))
        else:
            conn.execute("UPDATE restock_rules SET avg_days_between=?, updated_at=datetime('now') WHERE item_id=?",
                         (new_cad, item_id))
        conn.commit()
        return {"status": "recorded", "item_name": match.get("name") or item_name,
                "direction": direction, "old_cadence_days": cur_cad, "new_cadence_days": new_cad,
                "message": f"Will suggest {match.get('name') or item_name} {direction} often "
                           f"(about every {int(round(new_cad))} days now)."}
    finally:
        conn.close()


def _tool_set_brand_rule(params, session):
    """Record a durable category-level brand rule (avoid/prefer) in
    household_rules. Powers 'we don't like Pepsi, we like cola'.
    """
    brand = (params.get("brand") or "").strip()
    category = (params.get("category") or "").strip()
    action = (params.get("action") or "").strip().lower()
    if not brand or not category or action not in ("avoid", "prefer"):
        return {"status": "error", "message": "Need brand, category and action (avoid/prefer)."}
    try:
        from household_rules import add_household_rule, RULE_AVOID_BRAND, RULE_PREFER_BRAND
        rule_type = RULE_AVOID_BRAND if action == "avoid" else RULE_PREFER_BRAND
        added = add_household_rule(rule_type, category, brand=brand, source="chat")
        return {"status": "recorded", "brand": brand, "category": category, "action": action,
                "newly_added": added,
                "message": f"Noted — {action} {brand} in {category} from now on."}
    except Exception as e:
        return {"status": "error", "message": str(e)[:150]}


def _tool_get_preferences(params, session):
    """Get known preferences for an item."""
    from preference_engine import get_preference_explanation, get_scoring_profile

    item_name = params["item_name"]

    conn = _get_conn()
    try:
        item_match = _item_lookup(item_name, conn)
        if not item_match:
            return {
                "item_name": item_name,
                "status": "not_found",
                "message": f"'{item_name}' not found in our items database."
            }

        item_id = item_match["item_id"]

        # Human-readable explanations
        explanations = get_preference_explanation(item_id, conn)

        # Scoring profile (structured)
        profile = get_scoring_profile(item_id, conn)

        # Household intelligence
        household_rules = {}
        try:
            from household_profile import get_household_intelligence
            intel = get_household_intelligence(item_name)
            if intel:
                household_rules = {
                    "organic_tier": intel.get("organic_tier"),
                    "portion_note": intel.get("portion_rule", {}).get("note") if intel.get("portion_rule") else None,
                    "type_gate": intel.get("type_gate", {}).get("directive") if intel.get("type_gate") else None,
                    "origin_sensitivity": intel.get("origin_sensitivity"),
                }
        except ImportError:
            pass

        return {
            "item_name": item_name,
            "explanations": explanations,
            "profile_summary": {
                "require": profile.get("require", []),
                "prefer": profile.get("prefer", []),
                "avoid": profile.get("avoid", []),
                "brands": profile.get("brands", {}),
                "size_target": profile.get("size_target"),
            },
            "household_rules": household_rules,
        }
    finally:
        conn.close()


def _tool_get_order_status(params, session):
    """Get recent order status and history."""
    result = {}

    try:
        from order_scraper import (
            get_active_order_for_frontend,
            get_delivered_unrated_order,
        )

        active = get_active_order_for_frontend()
        delivered = get_delivered_unrated_order()

        if active:
            result["active_order"] = {
                "store": active.get("store", ""),
                "status": active.get("status", ""),
                "eta": active.get("eta", ""),
                "items_count": active.get("items_count", 0),
                "total": active.get("total"),
            }

        if delivered:
            result["recently_delivered"] = {
                "store": delivered.get("store", ""),
                "delivered_at": delivered.get("delivered_at", ""),
                "items_count": delivered.get("items_count", 0),
                "total": delivered.get("total"),
                "needs_rating": True,
            }
    except ImportError:
        pass

    # Also get recent order history from DB
    try:
        conn = _get_conn()
        rows = conn.execute("""
            SELECT o.id, o.store_id, s.name as store_name,
                   o.order_date, o.status, o.total_amount,
                   COUNT(oi.id) as item_count
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.id
            LEFT JOIN order_items oi ON o.id = oi.order_id
            GROUP BY o.id
            ORDER BY o.order_date DESC
            LIMIT 5
        """).fetchall()
        conn.close()

        if rows:
            result["recent_orders"] = [
                {
                    "order_id": r["id"],
                    "store": r["store_name"] or "Unknown",
                    "date": r["order_date"],
                    "status": r["status"],
                    "total_aed": r["total_amount"],
                    "items": r["item_count"],
                }
                for r in rows
            ]
    except Exception:
        pass

    if not result:
        return {"message": "No recent orders found."}
    return result


def _tool_cancel_order(params, session):
    """Cancel the current order flow."""
    session.state = "done"
    return {
        "status": "cancelled",
        "message": "Order cancelled. Your list is saved — just let me know when you're ready to order.",
    }


def _tool_delay_order(params, session):
    """Delay the current order."""
    session.state = "idle"
    return {
        "status": "delayed",
        "message": "No rush. I'll keep the list ready — just say when.",
    }


def _tool_get_household_info(params, session):
    """Get household rules for an item."""
    item_name = params["item_name"]

    result = {"item_name": item_name}

    try:
        from household_profile import (
            get_organic_tier, get_portion_rule, get_product_type_gate,
            get_origin_sensitivity, get_household_intelligence,
        )

        intel = get_household_intelligence(item_name)
        if intel:
            result["organic_tier"] = intel.get("organic_tier", "unknown")
            result["organic_directive"] = intel.get("organic_directive", "")

            portion = intel.get("portion_rule")
            if portion:
                result["portion_sizing"] = portion

            gate = intel.get("type_gate")
            if gate:
                result["product_type_gate"] = {
                    "directive": gate.get("directive", ""),
                    "require": gate.get("require_type", []),
                    "reject": gate.get("reject_type", []),
                }

            result["origin_sensitivity"] = intel.get("origin_sensitivity", "unknown")

    except ImportError:
        result["error"] = "Household profile not available."

    return result


# ============================================================
# TOOL REGISTRY & DISPATCHER
# ============================================================

TOOL_REGISTRY = {
    "get_shopping_list": _tool_get_shopping_list,
    "add_item_to_list": _tool_add_item_to_list,
    "remove_item_from_list": _tool_remove_item_from_list,
    "change_item_quantity": _tool_change_item_quantity,
    "approve_list": _tool_approve_list,
    "reset_list": _tool_reset_list,
    "batch_curate": _tool_batch_curate,
    "search_products": _tool_search_products,
    "explain_recommendation": _tool_explain_recommendation,
    "get_alternatives": _tool_get_alternatives,
    "record_preference": _tool_record_preference,
    "stop_suggesting_item": _tool_stop_suggesting,
    "set_item_frequency": _tool_set_item_frequency,
    "set_brand_rule": _tool_set_brand_rule,
    "get_preferences": _tool_get_preferences,
    "get_order_status": _tool_get_order_status,
    "cancel_order": _tool_cancel_order,
    "delay_order": _tool_delay_order,
    "get_household_info": _tool_get_household_info,
}


async def execute_tool(tool_name, tool_input, session):
    """
    Execute a tool by name with given input.

    Args:
        tool_name: One of the 16 tool names
        tool_input: Dict of parameters from Claude's tool_use block
        session: AgentSession with list_id, state, etc.

    Returns:
        dict: Tool result (JSON-serializable) to send back to Claude
    """
    handler = TOOL_REGISTRY.get(tool_name)
    if not handler:
        logger.error(f"Unknown tool: {tool_name}")
        return {"error": f"Unknown tool: {tool_name}"}

    try:
        logger.info(f"Executing tool: {tool_name} with input: {json.dumps(tool_input)[:200]}")
        result = handler(tool_input, session)
        logger.info(f"Tool {tool_name} completed: {json.dumps(result)[:200]}")
        return result
    except Exception as e:
        logger.exception(f"Tool {tool_name} failed: {e}")
        try:
            from app_logging import log_event as _le
            _le("agent_tool_error", source="agent_tools", level="error",
                detail=f"{tool_name}: {type(e).__name__}: {str(e)[:160]}")
        except Exception:
            pass
        return {"error": f"Tool failed: {str(e)}"}


# ============================================================
# TOOL NAME → FRIENDLY LABEL (for frontend thinking indicators)
# ============================================================

TOOL_LABELS = {
    "get_shopping_list": "Pulling up your list...",
    "add_item_to_list": "Tossing it in the basket...",
    "remove_item_from_list": "Taking that out...",
    "change_item_quantity": "Adjusting the quantity...",
    "approve_list": "Locking it in!",
    "reset_list": "Wiping the slate clean...",
    "batch_curate": "Scouting the best picks for you...",
    "search_products": "Browsing the aisles...",
    "explain_recommendation": "Let me break it down...",
    "get_alternatives": "Hunting for alternatives...",
    "record_preference": "Got it, I'll remember that...",
    "stop_suggesting_item": "Noting to stop suggesting that...",
    "set_item_frequency": "Adjusting how often I suggest that...",
    "set_brand_rule": "Saving your brand preference...",
    "get_preferences": "Checking what you like...",
    "get_order_status": "Tracking your order...",
    "cancel_order": "Scrapping this one...",
    "delay_order": "Pushing it to later...",
    "get_household_info": "Checking the household playbook...",
}


def get_tool_label(tool_name):
    """Get a user-friendly label for a tool (shown in thinking indicator)."""
    return TOOL_LABELS.get(tool_name, "Working...")
