"""
Hearthkeeper - LLM Handler

Calls Claude API for complex/ambiguous user messages.
Returns a structured ParsedMessage (same format as regex message_handler)
plus a natural language response for the user.

Architecture:
  - message_handler.py handles simple regex intents (instant, free)
  - This module handles everything else (async, paid)
  - Fallback: if API fails, regex result is used

Usage:
  from llm_handler import llm_parse_message
  result = await llm_parse_message(user_text, context)
"""

import json
import time
from dataclasses import dataclass
from typing import Optional

from message_handler import ParsedMessage, ParsedAction, Intent
from config import ANTHROPIC_API_KEY, LLM_MODEL, LLM_MAX_TOKENS


@dataclass
class LLMResult:
    """Result from the LLM handler."""
    parsed: ParsedMessage            # Structured result (same format as regex parser)
    response_text: str               # Natural language for the user
    model_used: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """You are Hearthkeeper, a friendly grocery restock assistant for a family in Dubai, UAE.
You help manage a weekly shopping list across stores like Carrefour UAE and Kibsons.

Your personality: helpful, casual, concise. Like texting a smart assistant.

CURRENT AGENT STATE: {state}
States explained:
- checkin: Presenting the predicted shopping list for user review
- reviewing: User is making modifications (add, skip, qty changes, preferences)
- building: Building carts on store websites (user should wait)
- done: Order cycle complete

SHOPPING LIST:
{list_summary}

RECENT CONVERSATION:
{recent_messages}

Respond with a JSON object (no markdown wrapping, just raw JSON):
{{
  "actions": [
    {{
      "intent": "<one of: approve_all, add_item, skip_item, change_qty, pick_alt, preference, nothing, show_list, cancel, delay, question, reset, log_purchase, unknown>",
      "item_name": "<item name if applicable, else null>",
      "quantity": <number if applicable, else null>,
      "choice_number": <number if picking alternative, else null>,
      "store_hint": "<Kibsons or Carrefour UAE if mentioned, else null>",
      "pref_type": "<for preference intent: brand_prefer, brand_avoid, attribute_require, attribute_prefer, attribute_avoid, store_prefer — else null>",
      "pref_value": "<for preference intent: the value like 'organic', 'Wagyu', 'Kraft' — else null>",
      "confidence": <0.0 to 1.0>
    }}
  ],
  "response": "<your natural language response to the user>"
}}

INTENT GUIDE:
- approve_all: User likes the list ("looks good", "perfect", "ok", "go ahead"). Finalizes the list.
- add_item: User wants to add an item ("add yogurt", "also get bananas", "we need olive oil").
- skip_item: User wants to remove an item from the list ("skip salmon", "remove the cheese", "no eggs this week").
- change_qty: User wants to change quantity ("2 packs of eggs", "double the milk", "less bread").
- pick_alt: User picks a numbered alternative ("option 2", "the second one"). Set choice_number.
- preference: User states a brand/product preference or requirement. Use this for:
  * Brand preferences: "I prefer Wagyu", "only buy Jenan eggs" → pref_type: brand_prefer, pref_value: brand name
  * Brand avoidance: "no more Kraft", "never get President" → pref_type: brand_avoid, pref_value: brand name
  * Attribute requirements: "only organic eggs", "must be free range" → pref_type: attribute_require, pref_value: attribute
  * Attribute preferences: "prefer fresh over frozen" → pref_type: attribute_prefer, pref_value: attribute
  * Attribute avoidance: "nothing frozen", "no caged eggs" → pref_type: attribute_avoid, pref_value: attribute
  * Store preferences: "get eggs from Kibsons" → pref_type: store_prefer, pref_value: store name
- nothing: User is just chatting, saying thanks, or not making a list change.
- show_list: User wants to see the current list.
- cancel: User wants to STOP the order entirely (doesn't want to order this week).
- delay: User wants to push the order to later.
- reset: User wants to regenerate/rebuild the list from scratch but KEEP ordering. "start over", "redo it" = reset, NOT cancel.
- question: User asks about the list, products, prices, or anything informational.
- log_purchase: User reports buying something offline ("I bought milk from the store").
- unknown: You genuinely can't tell what the user means. Ask for clarification.

Rules:
- Keep responses SHORT (1-3 sentences). This is a chat, not an essay.
- For compound requests ("add X and skip Y"), return multiple actions.
- Match item names flexibly: "eggs" matches "Eggs", "olive oil" matches "Olive Oil".
- If the user is just chatting or saying thanks, set intent to "nothing" and respond naturally.
- If you're unsure what the user means, ask for clarification and set intent to "unknown".
- When the user gives feedback about recommendations (e.g. "that brand is bad"), set intent to "preference" with brand_avoid.
- AED is the currency. Always refer to prices in AED.
- If the user says something positive about the list ("looks good", "this is fine", "perfect"), that's "approve_all" not "nothing".
- Your response text should confirm the action naturally. Example: if user says "skip the salmon", your response could be "Got it, removing salmon from the list."
- For add_item, KEEP quality/attribute modifiers in item_name: "add organic eggs" → item_name: "organic eggs". "fresh salmon" → item_name: "fresh salmon". Only strip filler words like "some", "a", "the". Also create a separate preference action for the attribute so the system learns it for future orders.
"""


# ============================================================
# RICH CONTEXT BUILDER
# ============================================================

def build_rich_list_context(agent):
    """
    Build a detailed text summary of the current list for LLM context.
    Includes item names, quantities, brands, prices, stores, and user preferences.

    Falls back to basic format if data sources aren't available.
    """
    if not agent.session.list_id:
        return "No list loaded"

    try:
        from list_generate import list_get
        shopping_list = list_get(agent.session.list_id)
        if not shopping_list:
            return "Empty list"

        items = shopping_list.get("items", [])
        if not items:
            return "Empty list"

        # Try to load preference explanations for each item
        pref_fn = None
        try:
            from preference_engine import get_preference_explanation
            pref_fn = get_preference_explanation
        except ImportError:
            pass

        lines = []
        for i in items[:25]:  # Cap at 25 items for token budget
            if i.get("user_action") == "skipped":
                continue

            name = i.get("item_name", "?")
            qty = i.get("quantity", 1)
            store = i.get("store_name", "?")
            price = i.get("estimated_price") or i.get("price_each")
            item_id = i.get("item_id")

            # Base line: name, qty, store, price
            line = f"- {name} x{qty} ({store})"
            if price:
                line += f" — AED {price:.2f}"

            # Add brand/product info if available
            brand = i.get("brand")
            if brand:
                line += f", brand: {brand}"

            # Add preference summary if available
            if pref_fn and item_id:
                try:
                    prefs = pref_fn(item_id)
                    if prefs:
                        pref_text = "; ".join(prefs[:3])  # Max 3 prefs per item
                        line += f"\n  Prefs: {pref_text}"
                except Exception:
                    pass

            lines.append(line)

        return "\n".join(lines) if lines else "Empty list"

    except Exception:
        return "Error loading list"


# ============================================================
# LLM PARSE
# ============================================================

async def llm_parse_message(
    user_text: str,
    state: str = "",
    list_summary: str = "",
    recent_messages: list = None,
) -> Optional[LLMResult]:
    """
    Parse a user message using Claude API.

    Returns LLMResult with structured ParsedMessage + natural text,
    or None on failure (caller should fall back to regex).
    """
    if not ANTHROPIC_API_KEY:
        try:
            from app_logging import log_event
            log_event("llm_skip", source="llm_handler",
                      detail="ANTHROPIC_API_KEY not set — all LLM queries will use fallback", level="warning")
        except Exception:
            pass
        return None

    try:
        import anthropic
    except ImportError:
        try:
            from app_logging import log_event
            log_event("llm_skip", source="llm_handler",
                      detail="anthropic package not installed", level="warning")
        except Exception:
            pass
        return None

    # Budget gate -- estimate cost before the call. Worst-case
    # max_tokens output + a generous input estimate. If the cap
    # is reached, log and fall back to regex (return None).
    #
    # The (ok, reason) tuple lets us pinpoint which cap fired
    # (daily / monthly / db_error) -- that string is included in
    # the event detail so /api/diag/recent-errors shows exactly
    # why an LLM call was skipped, not just "skipped".
    try:
        from llm_budget import can_spend, estimate_cost
        worst_case_input = LLM_MAX_TOKENS * 3  # rough heuristic: prompt is ~3x output
        est = estimate_cost(LLM_MODEL, worst_case_input, LLM_MAX_TOKENS)
        ok, reason = can_spend(est)
        if not ok:
            from app_logging import log_event
            log_event(
                "llm_budget_exceeded", source="llm_handler",
                detail=(
                    f"intent=whatsapp_parse model={LLM_MODEL} "
                    f"est=${est:.4f} reason={reason} -- falling back to regex"
                ),
                level="warning",
            )
            return None
    except Exception:
        # Budget module unavailable -- proceed but skip the gate.
        pass

    # Build context
    recent_text = ""
    if recent_messages:
        for msg in recent_messages[-5:]:
            role = msg.get("role", "?")
            text = msg.get("text", "")[:200]
            recent_text += f"  {role}: {text}\n"

    system = SYSTEM_PROMPT.format(
        state=state or "unknown",
        list_summary=list_summary or "No list loaded yet",
        recent_messages=recent_text or "  (no previous messages)",
    )

    start = time.time()

    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )

        latency_ms = int((time.time() - start) * 1000)
        content = response.content[0].text
        # Record actual spend AFTER the call so the next can_spend()
        # call sees this one.
        try:
            from llm_budget import record_call as _record
            _record(
                provider="anthropic",
                model=LLM_MODEL,
                intent="whatsapp_parse",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        except Exception:
            pass

        # Extract JSON — handle if model wraps in markdown code block
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        data = json.loads(content.strip())

        # Build ParsedMessage from LLM response
        parsed = ParsedMessage(raw_text=user_text)
        for action_data in data.get("actions", []):
            intent_str = action_data.get("intent", Intent.UNKNOWN)
            # Validate intent is one we know
            known = {Intent.APPROVE_ALL, Intent.ADD_ITEM, Intent.SKIP_ITEM,
                     Intent.CHANGE_QTY, Intent.PICK_ALT, Intent.NOTHING,
                     Intent.SHOW_LIST, Intent.CANCEL, Intent.DELAY,
                     Intent.QUESTION, Intent.UNKNOWN, Intent.PREFERENCE,
                     "reset", "log_purchase"}
            if intent_str not in known:
                intent_str = Intent.UNKNOWN

            parsed.actions.append(ParsedAction(
                intent=intent_str,
                item_name=action_data.get("item_name"),
                quantity=action_data.get("quantity"),
                choice_number=action_data.get("choice_number"),
                store_hint=action_data.get("store_hint"),
                raw_text=user_text,
                confidence=action_data.get("confidence", 0.85),
                pref_type=action_data.get("pref_type"),
                pref_value=action_data.get("pref_value"),
            ))

        # Ensure at least one action
        if not parsed.actions:
            parsed.actions.append(ParsedAction(
                intent=Intent.UNKNOWN, raw_text=user_text, confidence=0.5
            ))

        return LLMResult(
            parsed=parsed,
            response_text=data.get("response", ""),
            model_used=LLM_MODEL,
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
        )

    except json.JSONDecodeError as e:
        try:
            from app_logging import log_event
            log_event("llm_error", source="llm_handler",
                      detail=f"JSON parse failed: {e}", level="error")
        except Exception:
            pass
        return None
    except Exception as e:
        error_type = type(e).__name__
        try:
            from app_logging import log_event
            log_event("llm_error", source="llm_handler",
                      detail=f"{error_type}: {str(e)[:150]}", level="error")
        except Exception:
            pass
        return None


# ============================================================
# RATING FEEDBACK LLM
# ============================================================

RATING_FEEDBACK_PROMPT = """You are Hearthkeeper, a grocery restock assistant for a family in Dubai, UAE.
The user just rated a delivered order. Based on their feedback, respond conversationally confirming what you'll do differently next time.

Rules:
- Keep it SHORT: 1-2 sentences max.
- Be warm and specific — reference their actual feedback.
- If they mention a brand or product issue, confirm you'll adjust.
- If they want organic/fresh/different items, confirm you'll prioritize that.
- AED is the currency.
- Don't be formal — this is a chat."""


async def llm_process_rating_feedback(
    feedback_text: str,
    item_feedback: list = None,
) -> Optional[str]:
    """
    Process rating feedback text through the LLM for a conversational response.

    Args:
        feedback_text: User's free text about how to improve
        item_feedback: [{orderItemId, feedback, name}] for context

    Returns:
        Natural language response string, or None on failure.
    """
    if not ANTHROPIC_API_KEY:
        return None

    try:
        import anthropic
    except ImportError:
        return None

    # Budget gate (same pattern as llm_parse_message above)
    try:
        from llm_budget import can_spend, estimate_cost
        est = estimate_cost(LLM_MODEL, 600, 200)
        ok, reason = can_spend(est)
        if not ok:
            from app_logging import log_event
            log_event(
                "llm_budget_exceeded", source="llm_handler",
                detail=(
                    f"intent=rating_feedback model={LLM_MODEL} "
                    f"est=${est:.4f} reason={reason} -- skipping"
                ),
                level="warning",
            )
            return None
    except Exception:
        pass

    # Build context about per-item feedback
    context_lines = []
    if item_feedback:
        thumbs_down = [i.get("name", "an item") for i in item_feedback if i.get("feedback") == "thumbs_down"]
        thumbs_up = [i.get("name", "an item") for i in item_feedback if i.get("feedback") == "thumbs_up"]
        if thumbs_down:
            context_lines.append(f"Items they didn't like: {', '.join(thumbs_down[:5])}")
        if thumbs_up:
            context_lines.append(f"Items they liked: {', '.join(thumbs_up[:5])}")

    user_msg = feedback_text
    if context_lines:
        user_msg = "\n".join(context_lines) + f"\n\nUser's feedback: {feedback_text}"

    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=LLM_MODEL,
            max_tokens=200,
            system=RATING_FEEDBACK_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        try:
            from llm_budget import record_call as _record
            _record(
                provider="anthropic",
                model=LLM_MODEL,
                intent="rating_feedback",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        except Exception:
            pass
        return response.content[0].text.strip()
    except Exception as e:
        try:
            from app_logging import log_event
            log_event("llm_rating_error", source="llm_handler",
                      detail=f"Rating feedback LLM failed: {e}", level="error")
        except Exception:
            pass
        return None
