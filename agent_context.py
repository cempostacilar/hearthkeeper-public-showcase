"""
Hearthkeeper - Agent Context Builder

Builds the system prompt and dynamic context for the Store Manager agent.
Two layers:
  - Static context: household profile, habits, preferences (loaded once per session)
  - Dynamic context: current list, recent messages, pending issues (per turn)

Usage:
    from agent_context import build_system_prompt, build_dynamic_context
    system = build_system_prompt(session)
    # Pass as 'system' parameter to Claude API
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "autostock.db"


# ============================================================
# STATIC CONTEXT (loaded once per session, cached)
# ============================================================

_static_cache = None


def _build_habits_summary():
    """Summarize consumption_habits.HABITS for the system prompt."""
    try:
        from consumption_habits import HABITS
    except ImportError:
        return "Consumption habits not available."

    lines = []
    for h in HABITS:
        name = h.get("name", "?")
        freq = h.get("frequency", "?")
        qty = h.get("quantity", "?")
        notes = h.get("notes", "")
        line = f"- {name}: {freq}, qty {qty}"
        if notes:
            line += f" ({notes})"
        lines.append(line)
    return "\n".join(lines) if lines else "No habits defined."


def _build_preferences_summary():
    """Summarize learned preferences from preference_engine for the system prompt."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
    except Exception:
        return "Preference data not available."

    try:
        from preference_engine import get_preference_explanation

        rows = conn.execute("""
            SELECT DISTINCT i.id, i.name
            FROM items i
            ORDER BY i.name
        """).fetchall()

        lines = []
        for row in rows:
            try:
                explanations = get_preference_explanation(row["id"], conn)
                if explanations:
                    lines.append(f"- {row['name']}: {'; '.join(explanations[:3])}")
            except Exception:
                pass

        conn.close()
        return "\n".join(lines) if lines else "No learned preferences yet."
    except (ImportError, Exception):
        conn.close()
        return "Preference engine not available."


def build_static_context():
    """
    Build the static portion of the system prompt.
    Loaded once per session, cached for reuse across turns.

    Returns: str (the static context block)
    """
    global _static_cache
    if _static_cache is not None:
        return _static_cache

    # Household profile
    try:
        from household_profile import (
            HOUSEHOLD, ORGANIC_TIERS, PRODUCT_TYPE_GATES, PORTION_RULES,
        )
        h = HOUSEHOLD
        household_block = (
            f"Family of {h.get('adults', 2) + h.get('children', 1)} "
            f"({h.get('adults', 2)} adults, {h.get('children', 1)} child age {h.get('child_age', 4)}) "
            f"in {h.get('location', 'Dubai, UAE')}.\n"
            f"Orders {h.get('orders_per_week', 2)}x/week, ~{h.get('days_per_order', 3.5)} days per order.\n"
            f"Currency: AED."
        )

        # Organic tiers
        organic_lines = []
        for tier_name, tier_data in ORGANIC_TIERS.items():
            items_str = ", ".join(tier_data["items"][:10])
            if len(tier_data["items"]) > 10:
                items_str += f" (+{len(tier_data['items']) - 10} more)"
            label = tier_name.replace("_", " ").title()
            organic_lines.append(f"  {label}: {items_str}")
        organic_block = "\n".join(organic_lines)

        # Product type gates
        gate_lines = []
        for gate_key, gate_data in PRODUCT_TYPE_GATES.items():
            gate_lines.append(f"  - {gate_key.title()}: {gate_data.get('directive', '')}")
        gates_block = "\n".join(gate_lines)

        # Key portion rules
        portion_lines = []
        key_items = ["meat", "kefir", "eggs", "spinach", "striploin", "yogurt", "milk"]
        for item_key in key_items:
            if item_key in PORTION_RULES:
                r = PORTION_RULES[item_key]
                portion_lines.append(f"  - {item_key.title()}: {r.get('note', '')}")
        portions_block = "\n".join(portion_lines)

    except ImportError:
        household_block = "Household profile not available."
        organic_block = ""
        gates_block = ""
        portions_block = ""

    # Build the full static context
    habits_block = _build_habits_summary()
    prefs_block = _build_preferences_summary()

    static = f"""You are Hearthkeeper, the personal grocery manager for a family in Dubai, UAE.
Think of yourself as a store manager who has served this household for years.
You know their pantry, preferences, schedule, and what each store carries.

== HOUSEHOLD ==
{household_block}
Stores: Kibsons (organic/fresh, MOV 75 AED), Carrefour UAE (general, MOV 100 AED)

== ORGANIC POLICY ==
{organic_block}

== HARD PRODUCT RULES (non-negotiable) ==
{gates_block}

== PORTION SIZING ==
{portions_block}

== CONSUMPTION HABITS ==
{habits_block}

== LEARNED PREFERENCES ==
{prefs_block}

== COMMUNICATION STYLE ==
- Short, casual (1-3 sentences). This is texting, not email.
- After adding items, ALWAYS confirm with specifics: product name, store, and price.
  Example: "Added organic eggs from Kibsons (34.49 AED) and full cream milk from Carrefour (12.50 AED)"
- When something can't be found, explain what you searched and suggest alternatives.
- When something is out of stock, explain what you found instead and why.
- Never say "I'll remember that" without calling record_preference.
- Never guess prices or availability — always use a tool to check.
- AED for prices. Round in conversation.
- Never respond with vague messages like "Done!" or "I've updated your list" — be specific.

== DECISION RULES ==
- 3+ items → use batch_curate tool (compares across stores)
- 1-2 items → use add_item_to_list tool
- User states a preference → ALWAYS call the right tool so it persists:
    • "stop suggesting X" / "never X again" → stop_suggesting_item
    • "buy X less/more often" / "we don't eat much X" → set_item_frequency
    • "we (don't) like brand B" for a category → set_brand_rule
    • per-item brand/store/attribute/origin → record_preference
- User asks "why" about a product → use explain_recommendation tool
- Unknown/unfamiliar items → use search_products tool first
- If item is a broad category ("meat", "fish") → ask which specific item they mean
- "looks good", "ok", "go ahead" → call approve_list tool
- "where's my order" / "did it arrive" → use get_order_status tool
- When user wants to swap a product (different brand/store) → remove old + add new
- When user asks about "last order" → use get_order_status tool (shows recent history)

== WHAT NOT TO DO ==
- Never place orders. Build carts, user places orders.
- Never make up product names, prices, or availability.
- Never ignore product type gates (the hard rules above).
- Never silently substitute — always explain when you can't fulfill a directive.

== TRUTHFULNESS (critical) ==
- Only claim you DID something (added, removed, skipped, updated, blocked,
  "I'll remember", "won't suggest it again") if a tool call THIS TURN
  returned a success status (e.g. status "added"/"recorded"/"removed").
- If you did not call a tool, or it returned needs_input / not_found /
  error, say so plainly — do NOT pretend it worked.
- State cadence, last-ordered, prices, counts and quantities ONLY using
  values returned by tools this turn. Never invent figures like "almost
  daily" or a quantity you didn't read back from a tool result.
"""

    _static_cache = static
    return static



# ============================================================
# DYNAMIC CONTEXT (built per turn)
# ============================================================

def _active_suggestion_list_id(conn):
    """The current scheduled (AI suggestion) list id, or None.

    Matches the app's active-suggestion query: newest non-dismissed
    suggestion-typed draft. Resolved fresh from the DB rather than trusting
    session.list_id, which may point at the cart or a stale list.
    """
    row = conn.execute(
        """SELECT id FROM shopping_lists
           WHERE (list_type = 'suggestion' OR list_type IS NULL)
             AND status IN ('draft', 'confirmed', 'suggested', 'in_review')
             AND COALESCE(dismissed_at, '') = ''
           ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    return (row["id"] if row else None)


def _render_list_block(conn, list_id, title):
    """Render one list as a context block, or a one-line 'empty/none' note.

    Used for BOTH the scheduled list and the cart so the chat agent can
    reason about either without guessing which one session.list_id points
    at (the gap that left it blind to the cart).
    """
    if not list_id:
        return f"== {title} ==\n(none)"
    try:
        from list_generate import list_get
        shopping_list = list_get(list_id, conn)
        if not shopping_list:
            return f"== {title} ==\n(none)"
        items = [
            it for it in shopping_list.get("items", [])
            if it.get("user_action") != "skipped" and it.get("status") != "skipped"
        ]
        if not items:
            return f"== {title} (#{list_id}) ==\nEmpty."
        lines = [f"== {title} (#{list_id}) — {len(items)} items =="]
        for item in items[:30]:
            name = item.get("item_name", "?")
            qty = item.get("quantity", 1)
            store = item.get("store_name", "?")
            price = item.get("estimated_price") or item.get("price_each")
            status = item.get("status", "")
            product = item.get("product_name", "")
            line = f"- {name} x{qty}"
            if product and product != name:
                line += f" → {product}"
            line += f" ({store})"
            if price:
                line += f" AED {price:.2f}"
            if status and status not in ("approved", "matched"):
                line += f" [{status}]"
            lines.append(line)
        if len(items) > 30:
            lines.append(f"  …and {len(items) - 30} more")
        return "\n".join(lines)
    except Exception:
        return f"== {title} ==\n(could not load)"


def build_dynamic_context(session):
    """
    Build the dynamic portion that changes per turn.

    Args:
        session: AgentSession with list_id, cart_list_id, state, messages.

    Returns: str (the dynamic context block)

    Shows BOTH the scheduled (AI suggestion) list and the cart (the user's
    working order), each resolved fresh from the DB. Previously only
    session.list_id was injected, so the agent was blind to the cart and
    could see a stale scheduled id.
    """
    parts = []

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        sched_id = _active_suggestion_list_id(conn)
        cart_id = getattr(session, "cart_list_id", None)
        parts.append(_render_list_block(conn, sched_id, "SCHEDULED LIST (AI suggestions)"))
        parts.append(_render_list_block(conn, cart_id, "CART (working order)"))
        # If the user is actively viewing some other list, surface it too so
        # "this list" references resolve.
        viewing = getattr(session, "list_id", None)
        if viewing and viewing not in (sched_id, cart_id):
            parts.append(_render_list_block(conn, viewing, "CURRENTLY VIEWING"))
        # Which tab the user is looking at — edits apply HERE.
        av = (getattr(session, "active_view", None) or "scheduled").lower()
        av_label = "CART" if av == "cart" else "SCHEDULED LIST"
        parts.append(
            f"== ACTIVE VIEW ==\nThe user is viewing the {av_label}. "
            f"add/remove/skip/quantity commands apply to THIS list — act on it "
            f"directly. Do NOT refuse to remove or skip something that is on the "
            f"{av_label} by saying it must be 'added first'; just skip/remove it there."
        )
    finally:
        conn.close()

    # Recent conversation
    recent = getattr(session, "messages_sent", []) or []
    if recent:
        msg_lines = ["== RECENT CONVERSATION =="]
        for msg in recent[-10:]:
            role = msg.get("role", "?")
            text = msg.get("text", "")[:200]
            msg_lines.append(f"  {role}: {text}")
        parts.append("\n".join(msg_lines))

    # Agent state
    state = getattr(session, "state", "unknown")
    parts.append(f"\n== AGENT STATE ==\nCurrent: {state}")

    return "\n\n".join(parts)


# ============================================================
# FULL SYSTEM PROMPT (static + dynamic)
# ============================================================

def build_system_prompt(session):
    """
    Build the complete system prompt for one Claude API turn.

    Combines:
    - Static context (household, habits, preferences) — cached per session
    - Dynamic context (list, messages, state) — fresh per turn

    Returns: str
    """
    static = build_static_context()
    dynamic = build_dynamic_context(session)
    return f"{static}\n\n{dynamic}"
