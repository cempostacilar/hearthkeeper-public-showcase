"""
AutoRestock - Message Handler

Parses user replies (from WhatsApp, web UI, or CLI) into structured actions.
This is the "ears" of the agent -- it understands what the user means.

Supported intents:
  - approve_all:  "looks good", "ok", "go ahead", "all good"
  - add_item:     "add olive oil", "also need lamb chops"
  - skip_item:    "skip salmon", "remove the eggs", "no cheese"
  - change_qty:   "2 packs of eggs", "make it 3 for the mince"
  - pick_alt:     "1", "option 2", "the first one"
  - nothing:      "nothing", "nah", "that's it"
  - show_list:    "show me", "what's on the list", "list"
  - cancel:       "cancel", "stop", "never mind"
  - question:     "how much is...", "what about...", "is there..."
  - unknown:      couldn't parse

Usage:
  from message_handler import parse_message, Intent
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional


# ============================================================
# INTENTS
# ============================================================

class Intent:
    APPROVE_ALL = "approve_all"
    ADD_ITEM = "add_item"
    SKIP_ITEM = "skip_item"
    CHANGE_QTY = "change_qty"
    PICK_ALT = "pick_alt"
    NOTHING = "nothing"
    SHOW_LIST = "show_list"
    CANCEL = "cancel"
    DELAY = "delay"
    QUESTION = "question"
    RESET = "reset"
    PREFERENCE = "preference"  # "I prefer Wagyu", "only organic", "no more Kraft"
    UNKNOWN = "unknown"


@dataclass
class ParsedAction:
    """A single parsed action from a user message."""
    intent: str
    item_name: Optional[str] = None
    quantity: Optional[float] = None
    choice_number: Optional[int] = None
    store_hint: Optional[str] = None
    raw_text: str = ""
    confidence: float = 0.8
    pref_type: Optional[str] = None    # brand_prefer, brand_avoid, attribute_require, etc.
    pref_value: Optional[str] = None   # the preference value ("Wagyu", "organic", etc.)


@dataclass
class ParsedMessage:
    """Full parsed result from a user message. Can contain multiple actions."""
    actions: List[ParsedAction] = field(default_factory=list)
    raw_text: str = ""

    @property
    def primary_intent(self):
        return self.actions[0].intent if self.actions else Intent.UNKNOWN

    @property
    def is_positive(self):
        return self.primary_intent in (Intent.APPROVE_ALL, Intent.NOTHING)

    def has_intent(self, intent):
        return any(a.intent == intent for a in self.actions)


# ============================================================
# PATTERNS
# ============================================================

# Approval patterns -- user agrees with the list
APPROVE_PATTERNS = [
    r"^(?:looks?\s+)?good$",
    r"^(?:all\s+)?good$",
    r"^ok(?:ay)?$",
    r"^go\s+ahead$",
    r"^(?:yes|yeah|yep|yup|sure|perfect|great|fine|approved?)$",
    r"^let'?s?\s+go$",
    r"^do\s+it$",
    r"^proceed$",
    r"^confirm(?:ed)?$",
    r"^that'?s?\s+(?:fine|good|great|perfect|it)$",
    r"^no\s+changes?$",
]

# Nothing extra -- user has no additions
NOTHING_PATTERNS = [
    r"^nothing$",
    r"^nah$",
    r"^no(?:pe|thing)?$",
    r"^that'?s?\s+(?:it|all)$",
    r"^i'?m?\s+good$",
    r"^all\s+set$",
    r"^no\s+(?:thanks|need)$",
    r"^don'?t?\s+(?:need|want)\s+anything",
]

# Add item -- user wants to add something
ADD_PATTERNS = [
    r"(?:add|also\s+(?:need|add|get)|(?:we\s+)?need|(?:can\s+you\s+)?get|throw\s+in|include|put\s+in|grab)\s+(.+)",
    r"(?:we'?re?\s+(?:out|low)\s+(?:of|on))\s+(.+)",
    r"(?:almost\s+out\s+of|running\s+low\s+on)\s+(.+)",
    r"(?:don'?t?\s+forget)\s+(.+)",
    r"(?:we\s+(?:also\s+)?need|let'?s?\s+(?:also\s+)?get)\s+(.+)",
]

# Reduce quantity -- "remove 1 pasta", "take off 1 cheese" → reduce, not skip
# Must come BEFORE skip patterns since "remove 1 pasta" matches both
REDUCE_PATTERNS = [
    r"(?:remove|drop|take\s+off|take\s+out|cut)\s+(\d+)\s+(?:of\s+)?(?:the\s+)?(.+)",
    r"(?:lose|ditch)\s+(\d+)\s+(?:of\s+)?(?:the\s+)?(.+)",
    r"(\d+)\s+(?:less|fewer)\s+(?:of\s+)?(?:the\s+)?(.+)",
]

# Skip/remove -- user wants to remove something entirely
SKIP_PATTERNS = [
    r"(?:skip|remove|drop|no|cancel|lose|ditch|cut)\s+(?:the\s+)?(.+)",
    r"(?:take\s+(?:off|out)|leave\s+out|hold\s+(?:off\s+on|the))\s+(?:the\s+)?(.+)",
    r"(?:we\s+(?:still\s+)?have|(?:i\s+)?(?:already\s+)?(?:have|got|bought)|got\s+enough)\s+(?:the\s+)?(.+)",
    r"(?:i?\s*don'?t?\s+(?:need|want|think\s+we\s+need))\s+(?:the\s+)?(.+)",
    r"(?:too\s+much|enough)\s+(.+)",
    r"(?:not?\s+(?:this\s+time|today|now))\s+(?:for\s+)?(?:the\s+)?(.+)",
]

# Change quantity
QTY_PATTERNS = [
    r"(\d+)\s+(?:packs?|boxes?|cartons?|bags?|bottles?|pcs|pieces?|units?|x)?\s*(?:of\s+)?(.+)",
    r"(?:make\s+it|change\s+(?:it\s+)?to|set\s+(?:it\s+)?to)\s+(\d+)\s+(?:for\s+)?(?:the\s+)?(.+)",
    r"(?:change|update|set)\s+(?:the\s+)?(.+?)\s+to\s+(\d+)",
    r"(?:only|just)\s+(\d+)\s+(?:of\s+)?(?:the\s+)?(.+)",
    r"(.+?)(?:\s+x\s*|\s*x\s+)(\d+)",
]

# Pick alternative (during 80/20 handoff)
PICK_PATTERNS = [
    r"^(\d)$",
    r"^(?:option|choice|number|pick|#)\s*(\d+)$",
    r"^(?:the\s+)?(?:first|second|third|1st|2nd|3rd)\s+(?:one)?$",
    r"^(?:go\s+with|take|pick)\s+(?:option\s+)?(\d+)$",
]

# Preference statements -- "I prefer Wagyu", "only organic eggs", "no more Kraft"
PREFERENCE_PATTERNS = [
    # "I prefer X" / "prefer X for Y"
    (r"(?:i\s+)?prefer\s+(.+?)(?:\s+for\s+(.+))?$", "brand_prefer"),
    # "only organic X" / "must be organic"
    (r"(?:only|must\s+(?:be|have)|always)\s+(organic|fresh|free[\s-]?range|grass[\s-]?fed|unsalted|extra\s+virgin)\s*(.+)?$", "attribute_require"),
    # "no more X" / "never X brand" / "never get X" / "stop buying X"
    (r"(?:no\s+more|never(?:\s+(?:get|buy))?|stop\s+(?:buying|getting))\s+(.+)$", "brand_avoid"),
    # "switch to X" / "change to X for Y"
    (r"(?:switch|change)\s+(?:to\s+)?(.+?)\s+(?:for|instead\s+of)\s+(.+)$", "brand_prefer"),
    # "always get X" / "must have X"
    (r"(?:always\s+(?:get|buy|have)|must\s+(?:get|buy|have))\s+(.+)$", "product_prefer"),
    # "X brand is better" / "I like X brand"
    (r"(?:i\s+)?(?:like|love)\s+(.+?)\s+(?:brand|better|more)$", "brand_prefer"),
    # "get X from kibsons/carrefour"
    (r"(?:get|buy)\s+(.+?)\s+from\s+(kibsons|carrefour)$", "store_prefer"),
]

# Show list
SHOW_PATTERNS = [
    r"(?:show|what'?s?\s+on)\s+(?:me\s+)?(?:the\s+)?(?:list|cart|order)",
    r"^(?:list|cart)$",
    r"(?:let\s+me\s+see|can\s+I\s+see)",
    r"^show\s+me",
]

# Cancel
CANCEL_PATTERNS = [
    r"^(?:cancel|stop|abort|quit|never\s*mind|forget\s+it)$",
]

# Delay -- user wants to postpone
DELAY_PATTERNS = [
    r"^(?:delay|later|tomorrow|not\s+now|not\s+today)$",
    r"^(?:remind\s+me|check\s+back)\s+(?:later|tomorrow)$",
    r"^(?:push|move)\s+(?:it\s+)?(?:to\s+)?(?:later|tomorrow)$",
    r"^(?:maybe\s+)?(?:later|tomorrow)$",
]

# Reset -- user wants to restore original suggestion
RESET_PATTERNS = [
    r"^reset$",
    r"^reset\s+(?:list|cart|order|everything|all|to\s+original)$",
    r"^(?:restore|undo|revert)(?:\s+(?:all|everything|changes?))?$",
    r"^(?:go\s+)?back\s+to\s+original$",
    r"^start\s+over$",
]

# Question
QUESTION_PATTERNS = [
    r"^(?:how\s+much|what|which|where|when|why|is\s+there|do\s+(?:you|they)|can\s+(?:you|we))\b",
    r"\?$",
    r"^(?:that'?s?\s+(?:too|not|way\s+too)|seems?\s+(?:like|too))\s+",
    r"^why\s+",
]

# Store hints within text
STORE_HINTS = {
    "kibsons": "Kibsons",
    "carrefour": "Carrefour UAE",
    "amazon": "Amazon",
    "from kibsons": "Kibsons",
    "from carrefour": "Carrefour UAE",
    "on kibsons": "Kibsons",
    "on carrefour": "Carrefour UAE",
}


# ============================================================
# PARSER
# ============================================================

def parse_message(text: str) -> ParsedMessage:
    """
    Parse a user message into structured actions.

    Handles compound messages like:
      "add olive oil and skip the salmon"
      "2 packs of eggs, also need lamb chops from kibsons"

    Returns ParsedMessage with one or more actions.
    """
    if not text or not text.strip():
        return ParsedMessage(actions=[ParsedAction(intent=Intent.UNKNOWN)], raw_text="")

    raw = text.strip()
    clean = raw.lower().strip().rstrip('.!').strip()

    result = ParsedMessage(raw_text=raw)

    # Check simple single-intent patterns first
    # Approve
    for pattern in APPROVE_PATTERNS:
        if re.match(pattern, clean, re.IGNORECASE):
            result.actions.append(ParsedAction(
                intent=Intent.APPROVE_ALL, raw_text=raw, confidence=0.95
            ))
            return result

    # Nothing
    for pattern in NOTHING_PATTERNS:
        if re.match(pattern, clean, re.IGNORECASE):
            result.actions.append(ParsedAction(
                intent=Intent.NOTHING, raw_text=raw, confidence=0.95
            ))
            return result

    # Cancel
    for pattern in CANCEL_PATTERNS:
        if re.match(pattern, clean, re.IGNORECASE):
            result.actions.append(ParsedAction(
                intent=Intent.CANCEL, raw_text=raw, confidence=0.95
            ))
            return result

    # Delay
    for pattern in DELAY_PATTERNS:
        if re.match(pattern, clean, re.IGNORECASE):
            result.actions.append(ParsedAction(
                intent=Intent.DELAY, raw_text=raw, confidence=0.9
            ))
            return result

    # Reset
    for pattern in RESET_PATTERNS:
        if re.match(pattern, clean, re.IGNORECASE):
            result.actions.append(ParsedAction(
                intent=Intent.RESET, raw_text=raw, confidence=0.95
            ))
            return result

    # Show list
    for pattern in SHOW_PATTERNS:
        if re.search(pattern, clean, re.IGNORECASE):
            result.actions.append(ParsedAction(
                intent=Intent.SHOW_LIST, raw_text=raw, confidence=0.9
            ))
            return result

    # Question (check before compound parser to avoid false add_item matches)
    for pattern in QUESTION_PATTERNS:
        if re.search(pattern, clean, re.IGNORECASE):
            result.actions.append(ParsedAction(
                intent=Intent.QUESTION, raw_text=raw, confidence=0.7
            ))
            return result

    # Pick alternative (number responses during handoff)
    for pattern in PICK_PATTERNS:
        m = re.match(pattern, clean, re.IGNORECASE)
        if m:
            # Extract choice number
            num = None
            for group in m.groups():
                if group and group.isdigit():
                    num = int(group)
                    break
            if num is None:
                # Handle "first", "second", "third"
                ordinal_map = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}
                for word in clean.split():
                    if word in ordinal_map:
                        num = ordinal_map[word]
                        break
            if num:
                result.actions.append(ParsedAction(
                    intent=Intent.PICK_ALT, choice_number=num,
                    raw_text=raw, confidence=0.9
                ))
                return result

    # Compound messages: split by "and", ",", "also", "plus"
    # "add olive oil and skip salmon"  ->  ["add olive oil", "skip salmon"]
    segments = _split_compound(clean)

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        action = _parse_segment(segment, raw)
        if action:
            result.actions.append(action)

    # If nothing matched, check if it's a question
    if not result.actions:
        for pattern in QUESTION_PATTERNS:
            if re.search(pattern, clean, re.IGNORECASE):
                result.actions.append(ParsedAction(
                    intent=Intent.QUESTION, raw_text=raw, confidence=0.7
                ))
                return result

    # Still nothing? Unknown
    if not result.actions:
        result.actions.append(ParsedAction(
            intent=Intent.UNKNOWN, raw_text=raw, confidence=0.3
        ))

    return result


def _split_compound(text):
    """Split compound messages into individual segments."""
    # Replace connectors with a delimiter
    split_text = re.sub(r'\b(?:and|also|plus)\b', '|||', text)
    split_text = re.sub(r',\s+', '|||', split_text)

    segments = [s.strip() for s in split_text.split('|||') if s.strip()]
    return segments if segments else [text]


def _parse_segment(segment, raw_text=""):
    """Parse a single segment into an action."""
    # Detect store hint and clean it from the segment
    store_hint = None
    # Check longest hints first (e.g., "from kibsons" before "kibsons")
    sorted_hints = sorted(STORE_HINTS.items(), key=lambda x: -len(x[0]))
    for hint_text, store_name in sorted_hints:
        if hint_text in segment:
            store_hint = store_name
            segment = segment.replace(hint_text, "").strip()
            break
    # Also catch "if kibsons has..." patterns
    if not store_hint:
        if_store = re.search(r'if\s+(kibsons|carrefour)\s+has', segment, re.IGNORECASE)
        if if_store:
            store_key = if_store.group(1).lower()
            store_hint = STORE_HINTS.get(store_key)
            segment = segment[:if_store.start()].strip()

    # Reduce quantity -- "remove 1 pasta" → reduce by 1, not full skip
    for pattern in REDUCE_PATTERNS:
        m = re.match(pattern, segment, re.IGNORECASE)
        if m:
            qty = None
            item = None
            for g in m.groups():
                if g and re.match(r'^\d+$', g.strip()):
                    qty = float(g)
                elif g:
                    item = _clean_item_name(g)
            if qty and item:
                return ParsedAction(
                    intent=Intent.CHANGE_QTY, item_name=item, quantity=-qty,
                    store_hint=store_hint, raw_text=raw_text, confidence=0.85
                )

    # Preference statements -- checked before skip since "no more Kraft" overlaps with "no X"
    for pattern, pref_type in PREFERENCE_PATTERNS:
        m = re.match(pattern, segment, re.IGNORECASE)
        if m:
            groups = [g for g in m.groups() if g]
            if groups:
                pref_value = _clean_item_name(groups[0]) if groups else ""
                pref_item = _clean_item_name(groups[1]) if len(groups) > 1 else ""
                if pref_value:
                    return ParsedAction(
                        intent=Intent.PREFERENCE,
                        item_name=pref_item or pref_value,
                        raw_text=raw_text,
                        confidence=0.85,
                        store_hint=store_hint,
                        pref_type=pref_type,
                        pref_value=pref_value,
                    )

    # Skip/remove (no number = full skip)
    for pattern in SKIP_PATTERNS:
        m = re.match(pattern, segment, re.IGNORECASE)
        if m:
            # Grab the last non-None capturing group (item name)
            item_name = None
            for g in reversed(m.groups()):
                if g:
                    item_name = _clean_item_name(g)
                    break
            if item_name:
                return ParsedAction(
                    intent=Intent.SKIP_ITEM, item_name=item_name,
                    store_hint=store_hint, raw_text=raw_text, confidence=0.85
                )

    # Quantity change (check before add, since "2 packs of eggs" has a qty)
    for pattern in QTY_PATTERNS:
        m = re.match(pattern, segment, re.IGNORECASE)
        if m:
            groups = m.groups()
            # Figure out which group is qty and which is item name
            qty = None
            item = None
            for g in groups:
                if g and re.match(r'^\d+$', g.strip()):
                    qty = float(g)
                elif g:
                    item = _clean_item_name(g)

            if qty and item:
                return ParsedAction(
                    intent=Intent.CHANGE_QTY, item_name=item, quantity=qty,
                    store_hint=store_hint, raw_text=raw_text, confidence=0.85
                )
            elif qty and not item:
                # Just a number with context -- might be qty change
                return ParsedAction(
                    intent=Intent.CHANGE_QTY, quantity=qty,
                    store_hint=store_hint, raw_text=raw_text, confidence=0.6
                )

    # Add item
    for pattern in ADD_PATTERNS:
        m = re.match(pattern, segment, re.IGNORECASE)
        if m:
            item_name = _clean_item_name(m.group(1))
            if item_name:
                # Check if there's an embedded quantity
                qty_m = re.match(r'^(\d+)\s+(?:packs?\s+(?:of\s+)?)?(.+)', item_name)
                if qty_m:
                    return ParsedAction(
                        intent=Intent.ADD_ITEM,
                        item_name=_clean_item_name(qty_m.group(2)),
                        quantity=float(qty_m.group(1)),
                        store_hint=store_hint, raw_text=raw_text, confidence=0.85
                    )
                return ParsedAction(
                    intent=Intent.ADD_ITEM, item_name=item_name,
                    store_hint=store_hint, raw_text=raw_text, confidence=0.85
                )

    # If segment looks like just an item name (during add context)
    # This catches replies like just "olive oil" when the agent asked what to add
    item_name = _clean_item_name(segment)
    if item_name and len(item_name.split()) <= 5:
        return ParsedAction(
            intent=Intent.ADD_ITEM, item_name=item_name,
            store_hint=store_hint, raw_text=raw_text, confidence=0.5
        )

    return None


def _clean_item_name(text):
    """Clean up an extracted item name."""
    if not text:
        return ""
    text = text.strip()
    # Remove leading articles
    text = re.sub(r'^(?:the|some|a|an)\s+', '', text, flags=re.IGNORECASE)
    # Remove trailing filler words
    text = re.sub(r'\s+(?:please|pls|too|as\s+well|this\s+time|today|now|right\s+now|for\s+now|anymore|though)$', '', text, flags=re.IGNORECASE)
    # Remove "if" clauses: "lamb chops if kibsons has them"
    text = re.sub(r'\s+if\s+.+$', '', text, flags=re.IGNORECASE)
    # Remove parenthetical: "lamb chops (the australian ones)"
    text = re.sub(r'\s*\(.+?\)\s*', ' ', text)
    return text.strip()


# ============================================================
# MATCH ITEM TO DB
# ============================================================

def match_item_to_db(item_name, conn):
    """
    Try to match a user-mentioned item name to an item in the DB.

    Uses fuzzy matching: checks if key words from the user's text
    appear in item names or search terms.

    Returns:
        {"item_id": int, "name": str, "store_id": int, "store_name": str} or None
    """
    if not item_name:
        return None

    cur = conn.cursor()
    cur.execute("""
        SELECT i.id, i.name, i.category, i.search_terms,
               s.id AS store_id, s.name AS store_name
        FROM items i
        LEFT JOIN stores s ON i.preferred_store_id = s.id
    """)
    items = [dict(row) for row in cur.fetchall()]

    query_words = set(w.lower() for w in item_name.split() if len(w) > 2)
    if not query_words:
        query_words = {item_name.lower()}

    best_match = None
    best_score = 0

    for item in items:
        score = 0
        name_lower = item["name"].lower()
        name_words = set(w for w in name_lower.split() if len(w) > 2)

        # Exact name match
        if item_name.lower() == name_lower:
            score += 100

        # Word overlap
        overlap = query_words & name_words
        score += len(overlap) * 20

        # Substring match
        if item_name.lower() in name_lower or name_lower in item_name.lower():
            score += 30

        # Check search terms
        if item.get("search_terms"):
            try:
                terms = json.loads(item["search_terms"]) if isinstance(item["search_terms"], str) else item["search_terms"]
                for store, term_list in terms.items():
                    for term in term_list:
                        if item_name.lower() in term.lower() or any(w in term.lower() for w in query_words):
                            score += 15
                            break
            except (json.JSONDecodeError, TypeError):
                pass

        if score > best_score and score >= 15:
            best_score = score
            best_match = item

    if best_match:
        return {
            "item_id": best_match["id"],
            "name": best_match["name"],
            "store_id": best_match.get("store_id"),
            "store_name": best_match.get("store_name"),
        }
    return None


# ============================================================
# CLI DEMO
# ============================================================

import json

if __name__ == "__main__":
    test_messages = [
        # Approvals
        "Looks good",
        "ok",
        "Go ahead",
        "Yes",
        "That's fine",

        # Nothing extra
        "Nothing",
        "Nah",
        "That's it",

        # Add items
        "Add olive oil",
        "Also need lamb chops from kibsons",
        "We're almost out of eggs",
        "Get some salmon",
        "Add 2 packs of cheese",

        # Skip items
        "Skip salmon",
        "Remove the eggs",
        "No cheese this time",
        "We still have bread",

        # Compound
        "Add olive oil and skip the salmon",
        "2 packs of eggs, also need lamb chops from kibsons",
        "Skip bread and add pasta",

        # Qty changes
        "3 packs of mince",
        "Make it 2 for the eggs",

        # Pick alternatives (during handoff)
        "1",
        "Option 2",
        "The first one",
        "Go with 3",

        # Questions
        "How much is the salmon?",
        "What about organic options?",

        # Show
        "Show me the list",
        "What's on the cart?",

        # Cancel
        "Cancel",
        "Never mind",

        # Edge cases
        "yeah add olive oil if carrefour has the bertolli one",
        "skip salmon, we got it from the fish market",
    ]


# ============================================================
# LLM ROUTING
# ============================================================

def needs_llm(parsed: ParsedMessage, threshold: float = 0.8) -> bool:
    """
    Should this message be routed to the LLM instead of using regex?

    Returns True if:
      - Primary intent is UNKNOWN (regex couldn't parse)
      - Primary intent is QUESTION (LLM can answer naturally)
      - Confidence is below threshold (ambiguous)
      - Message is long (>12 words, likely conversational)
    """
    if not parsed.actions:
        return True

    primary = parsed.actions[0]

    if primary.intent == Intent.UNKNOWN:
        return True

    if primary.intent == Intent.QUESTION:
        return True

    if primary.confidence < threshold:
        return True

    if len(parsed.raw_text.split()) > 12:
        return True

    return False

    print("=" * 60)
    print("  MESSAGE PARSER DEMO")
    print("=" * 60)

    for msg in test_messages:
        parsed = parse_message(msg)
        actions_str = []
        for a in parsed.actions:
            parts = [a.intent]
            if a.item_name:
                parts.append(f'item="{a.item_name}"')
            if a.quantity:
                parts.append(f"qty={a.quantity}")
            if a.choice_number:
                parts.append(f"choice={a.choice_number}")
            if a.store_hint:
                parts.append(f"store={a.store_hint}")
            parts.append(f"conf={a.confidence:.0%}")
            actions_str.append(" | ".join(parts))

        print(f"\n  \"{msg}\"")
        for a_str in actions_str:
            print(f"    -> {a_str}")
