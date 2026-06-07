"""
Hearthkeeper - Agent Router

Decides whether a user message takes the fast path (regex) or agent path (LLM).
~40% of messages are simple enough for regex (approve, cancel, show list).
~60% need LLM reasoning (add items, questions, preferences, ambiguous).

Usage:
    from agent_router import route_message
    path, parsed = route_message(user_text, state)
    if path == "fast":
        handle_fast_path(parsed, session)
    else:
        result = await run_agent_turn(user_text, session, ...)
"""

from message_handler import parse_message, Intent

# Intents that can be handled without LLM (instant, free)
FAST_PATH_INTENTS = {
    Intent.APPROVE_ALL,   # "ok", "looks good", "go ahead"
    Intent.CANCEL,        # "cancel", "stop"
    Intent.DELAY,         # "later", "tomorrow"
    Intent.RESET,         # "reset", "start over"
    Intent.SHOW_LIST,     # "show list", "what's on the list"
    Intent.NOTHING,       # "thanks", "ok", "that's it"
}

# Minimum confidence to trust the regex parse
MIN_CONFIDENCE = 0.85


def route_message(user_text, state="unknown"):
    """
    Route a user message to fast path or agent path.

    Args:
        user_text: The raw user message
        state: Current agent state (idle, checkin, reviewing, etc.)

    Returns:
        tuple: ("fast", ParsedMessage) or ("agent", None)
    """
    parsed = parse_message(user_text)

    if not parsed or not parsed.actions:
        return ("agent", None)

    primary = parsed.actions[0]

    # Fast path: single, high-confidence, unambiguous intent
    if (primary.intent in FAST_PATH_INTENTS
            and primary.confidence >= MIN_CONFIDENCE
            and len(parsed.actions) == 1):
        return ("fast", parsed)

    # Single add/skip/qty with very high confidence can also be fast
    # BUT only for skip and qty change — add needs curation (agent path)
    if (primary.intent == Intent.SKIP_ITEM
            and primary.confidence >= 0.9
            and len(parsed.actions) == 1
            and primary.item_name):
        return ("fast", parsed)

    if (primary.intent == Intent.CHANGE_QTY
            and primary.confidence >= 0.9
            and len(parsed.actions) == 1
            and primary.item_name
            and primary.quantity is not None):
        return ("fast", parsed)

    # Pick alternative (numbered choice) — fast when confident
    if (primary.intent == Intent.PICK_ALT
            and primary.confidence >= 0.9
            and len(parsed.actions) == 1):
        return ("fast", parsed)

    # Everything else → agent
    return ("agent", None)
