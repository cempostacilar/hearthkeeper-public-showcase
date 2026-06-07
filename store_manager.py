"""
Hearthkeeper - Store Manager Agent

The core agent loop that turns user messages into tool-assisted responses.
Think of this as the grocery store manager's brain — takes in a message,
reasons about it, calls tools (check list, score products, search catalog),
and returns a natural, actionable response.

Architecture:
  1. Build system prompt (static context cached + dynamic per turn)
  2. Call Claude API with tool definitions
  3. If Claude returns tool_use blocks → execute them locally
  4. Feed tool results back to Claude
  5. Repeat until Claude returns text only (or hits max rounds)
  6. Return final text response to user

Usage:
    from store_manager import run_agent_turn, AgentTurnResult
    result = await run_agent_turn(user_text, session, on_progress=callback)
"""

import json
import time
import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, Awaitable

from config import (
    ANTHROPIC_API_KEY,
    AGENT_MODEL as _AGENT_MODEL,
    AGENT_MAX_TOKENS as _AGENT_MAX_TOKENS,
    AGENT_MAX_TOOL_ROUNDS as _AGENT_MAX_TOOL_ROUNDS,
    AGENT_TIMEOUT_MS as _AGENT_TIMEOUT_MS,
)

logger = logging.getLogger("store_manager")

DB_PATH = Path(__file__).parent / "autostock.db"

# ── Agent Configuration (from config.py / .env) ──
AGENT_MODEL = _AGENT_MODEL
AGENT_MAX_TOKENS = _AGENT_MAX_TOKENS
AGENT_MAX_TOOL_ROUNDS = _AGENT_MAX_TOOL_ROUNDS
AGENT_TIMEOUT_MS = _AGENT_TIMEOUT_MS

# Retry settings for transient API errors (529 overloaded, rate limits)
API_MAX_RETRIES = 3
API_RETRY_DELAYS = [2.0, 4.0, 8.0]  # seconds — exponential backoff


# ============================================================
# PHASE-BASED PROGRESS (replaces dual thinking + tool_activity)
# ============================================================
#
# Maps tool names → (phase_key, user-facing label).
# Debounce rule: don't emit a new phase if <1.5s since last one.
# This collapses the 5 rapid label swaps into 2-3 smooth transitions.

PHASE_MAP = {
    "batch_curate":           ("curating",  "Finding the best products..."),
    "search_products":        ("curating",  "Browsing the aisles..."),
    "add_item_to_list":       ("updating",  "Updating your list..."),
    "remove_item_from_list":  ("updating",  "Updating your list..."),
    "change_item_quantity":   ("updating",  "Adjusting quantities..."),
    "get_alternatives":       ("curating",  "Hunting for alternatives..."),
    "approve_list":           ("updating",  "Locking it in!"),
    "reset_list":             ("updating",  "Wiping the slate clean..."),
    "explain_recommendation": ("thinking",  "Let me break it down..."),
    "record_preference":      ("updating",  "Got it, noted..."),
    "get_preferences":        ("thinking",  "Checking your preferences..."),
    "get_shopping_list":      ("thinking",  "Pulling up your list..."),
    "get_order_status":       ("thinking",  "Tracking your order..."),
    "cancel_order":           ("updating",  "Scrapping this one..."),
    "delay_order":            ("updating",  "Pushing it to later..."),
    "get_household_info":     ("thinking",  "Checking the playbook..."),
}

# Thinking labels per round (fallback when no tool is active)
_ROUND_LABELS = [
    "Let me think...",
    "Putting it all together...",
    "Bear with me, almost there...",
]

PROGRESS_DEBOUNCE_S = 1.5  # Don't emit new phase within this window


# ============================================================
# RESULT DATACLASS
# ============================================================

@dataclass
class AgentTurnResult:
    """Result from one agent turn (possibly multi-round with tool calls)."""
    text: str                          # Final response text for the user
    tool_calls: list = field(default_factory=list)  # Tools called: [{name, input, output}]
    rounds: int = 0                    # How many API round-trips
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    hit_limit: bool = False            # True if we hit MAX_TOOL_ROUNDS
    error: Optional[str] = None        # Error message if something went wrong


def _summarize_tool_actions(tool_call_log: list) -> str:
    """Build a short positive/negative confirmation from the tools a turn ran.

    Used as a fallback when the model finishes a turn with NO text of its
    own -- so the user always gets an explicit "here's what I did" instead
    of a hung spinner or a generic non-sequitur. Returns "" when nothing
    mutating happened (caller then uses a neutral prompt).
    """
    added, removed, changed, other = [], [], [], []
    for c in tool_call_log or []:
        name = c.get("name", "")
        inp = c.get("input") or {}
        out = c.get("output")
        # A tool that returned a failure STATUS (not just an "error" key) must not
        # be reported as success — otherwise "remove eggs" gets a "Done, removed
        # eggs" confirmation even when the tool returned not_found and nothing
        # changed (the false positive the user hit).
        status = out.get("status") if isinstance(out, dict) else None
        ok = (isinstance(out, dict)
              and not out.get("error")
              and status not in ("not_found", "needs_input", "error", "failed", "no_op"))
        label = inp.get("item_name") or inp.get("name") or inp.get("query") or ""
        if name == "add_item_to_list" and ok:
            added.append(label or "item")
        elif name == "remove_item_from_list" and ok:
            removed.append(label or "item")
        elif name in ("change_item_quantity", "batch_curate") and ok:
            changed.append(label or "")
        elif name == "approve_list" and ok:
            other.append("approved the list")
        elif name == "reset_list" and ok:
            other.append("reset the list")
        elif name == "record_preference" and ok:
            other.append("saved your preference")
        elif name in ("cancel_order", "delay_order") and ok:
            other.append(name.replace("_", " ") + "ed")
    parts = []
    if added:
        parts.append("added " + ", ".join(dict.fromkeys(added)))
    if removed:
        parts.append("removed " + ", ".join(dict.fromkeys(removed)))
    if changed:
        parts.append("updated quantities")
    parts.extend(dict.fromkeys(other))
    if not parts:
        return ""
    sentence = "; ".join(parts)
    return "Done — " + sentence[0].upper() + sentence[1:] + "."


# ============================================================
# CONVERSATION HISTORY MANAGER
# ============================================================

class ConversationHistory:
    """
    Manages the message history for one session.
    Keeps a sliding window of recent messages to stay within token budget.
    """
    MAX_MESSAGES = 20  # Keep last 20 messages (10 turns)

    def __init__(self):
        self.messages = []

    def add_user_message(self, text):
        self.messages.append({"role": "user", "content": text})
        self._trim()

    def add_assistant_message(self, content):
        """Content can be str or list of content blocks."""
        if isinstance(content, str):
            self.messages.append({"role": "assistant", "content": content})
        else:
            self.messages.append({"role": "assistant", "content": content})
        self._trim()

    def add_tool_results(self, tool_results):
        """Add tool results as a user message (Claude API format)."""
        self.messages.append({"role": "user", "content": tool_results})
        self._trim()

    def get_messages(self):
        return list(self.messages)

    def _trim(self):
        """Keep conversation within budget."""
        if len(self.messages) > self.MAX_MESSAGES:
            # Always keep the first message (initial user request)
            # and trim from the middle
            self.messages = self.messages[:1] + self.messages[-(self.MAX_MESSAGES - 1):]

    def clear(self):
        self.messages = []


# ============================================================
# SYSTEM PROMPT BUILDER
# ============================================================

def _build_system_prompt(session):
    """
    Build the complete system prompt from agent_context.
    Combines static (household/habits/prefs) + dynamic (list/messages/state).
    """
    from agent_context import build_system_prompt
    return build_system_prompt(session)


# ============================================================
# CORE AGENT LOOP
# ============================================================

async def run_agent_turn(
    user_text: str,
    session,
    on_progress: Optional[Callable[[str, str], Awaitable[None]]] = None,
    # Legacy dual-callback support (ignored if on_progress is provided)
    on_thinking: Optional[Callable[[int, str], Awaitable[None]]] = None,
    on_tool_activity: Optional[Callable[[str, str], Awaitable[None]]] = None,
) -> AgentTurnResult:
    """
    Run one complete agent turn: user message → (tool calls)* → response.

    Args:
        user_text: The user's message
        session: AgentSession (has list_id, state, messages, conversation_history)
        on_progress: async callback(phase, label) — single debounced progress indicator
        on_thinking: (legacy) async callback(round_number, label)
        on_tool_activity: (legacy) async callback(tool_name, summary)

    Returns:
        AgentTurnResult with text, tool history, usage stats
    """
    # Ensure conversation history exists on session
    if not hasattr(session, "conversation_history"):
        session.conversation_history = ConversationHistory()

    # Check API key
    if not ANTHROPIC_API_KEY:
        logger.warning("No ANTHROPIC_API_KEY — cannot run agent")
        return AgentTurnResult(
            text="Sorry, I can't process that right now (API not configured).",
            error="no_api_key",
        )

    # Import Anthropic
    try:
        import anthropic
    except ImportError:
        logger.error("anthropic package not installed")
        return AgentTurnResult(
            text="Sorry, something went wrong on my end.",
            error="anthropic_not_installed",
        )

    # Import tools
    from agent_tools import TOOL_DEFINITIONS, execute_tool, get_tool_label

    # Build system prompt
    system_prompt = _build_system_prompt(session)

    # Add user message to conversation history
    session.conversation_history.add_user_message(user_text)
    messages = session.conversation_history.get_messages()

    # Track stats
    start_time = time.time()
    total_input_tokens = 0
    total_output_tokens = 0
    tool_call_log = []

    # Create client
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    # Debounced progress emitter — collapses rapid phase changes
    last_progress_time = 0.0
    last_progress_phase = None

    async def _emit_progress(phase: str, label: str, force: bool = False):
        nonlocal last_progress_time, last_progress_phase
        now = time.time()
        # Skip if same phase or within debounce window (unless forced)
        if not force and phase == last_progress_phase:
            return
        if not force and (now - last_progress_time) < PROGRESS_DEBOUNCE_S:
            return
        last_progress_time = now
        last_progress_phase = phase
        if on_progress:
            try:
                await on_progress(phase, label)
            except Exception:
                pass
        elif on_thinking:
            # Legacy fallback
            try:
                await on_thinking(0, label)
            except Exception:
                pass

    try:
        for round_num in range(AGENT_MAX_TOOL_ROUNDS):
            # Send thinking indicator for this round
            round_label = _ROUND_LABELS[min(round_num, len(_ROUND_LABELS) - 1)]
            await _emit_progress("thinking", round_label, force=(round_num == 0))

            # Call Claude API (with retry for transient errors)
            response = None
            for attempt in range(API_MAX_RETRIES + 1):
                try:
                    response = await client.messages.create(
                        model=AGENT_MODEL,
                        max_tokens=AGENT_MAX_TOKENS,
                        system=system_prompt,
                        messages=messages,
                        tools=TOOL_DEFINITIONS,
                    )
                    break  # Success — exit retry loop
                except (anthropic._exceptions.OverloadedError, anthropic.RateLimitError) as retry_err:
                    if attempt >= API_MAX_RETRIES:
                        raise  # Out of retries — propagate to outer handler
                    delay = API_RETRY_DELAYS[min(attempt, len(API_RETRY_DELAYS) - 1)]
                    logger.warning(
                        f"API {type(retry_err).__name__} (attempt {attempt + 1}/{API_MAX_RETRIES + 1}), "
                        f"retrying in {delay}s..."
                    )
                    # Let user know we're retrying
                    await _emit_progress("retrying", f"Service busy, retrying in {int(delay)}s...", force=True)
                    await asyncio.sleep(delay)

            # Track tokens
            if hasattr(response, "usage"):
                total_input_tokens += getattr(response.usage, "input_tokens", 0)
                total_output_tokens += getattr(response.usage, "output_tokens", 0)

            # Separate tool_use blocks from text blocks
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            # If no tool calls, we're done — extract final text
            if not tool_uses:
                final_text = "\n".join(b.text for b in text_blocks).strip()

                # Guarantee a terminal response: if the model produced no
                # text, synthesize a confirmation from whatever tools ran
                # this turn (so an action always gets a positive/negative
                # reply, never a hung spinner or a generic non-sequitur).
                if not final_text:
                    final_text = _summarize_tool_actions(tool_call_log) or (
                        "Got it — but I'm not sure what to change. Try e.g. "
                        "\"add 2 milk\", \"skip the apples\", or tap an item."
                    )

                # Add assistant response to conversation history
                session.conversation_history.add_assistant_message(final_text)

                latency = int((time.time() - start_time) * 1000)
                return AgentTurnResult(
                    text=final_text,
                    tool_calls=tool_call_log,
                    rounds=round_num + 1,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    latency_ms=latency,
                    model=AGENT_MODEL,
                )

            # Has tool calls — execute them
            # First, add assistant message with content blocks to messages
            # (Claude API requires this for the tool result turn)
            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
            messages.append({"role": "assistant", "content": assistant_content})

            # Execute each tool
            tool_results = []
            for tu in tool_uses:
                # Send progress indicator for this tool
                phase_key, phase_label = PHASE_MAP.get(
                    tu.name, ("thinking", get_tool_label(tu.name)))
                await _emit_progress(phase_key, phase_label)

                # Execute the tool
                logger.info(f"Round {round_num}: calling {tu.name}")
                try:
                    result = await execute_tool(tu.name, tu.input, session)
                except Exception as e:
                    logger.exception(f"Tool {tu.name} raised exception")
                    result = {"error": f"Tool failed: {str(e)}"}

                # Log it
                tool_call_log.append({
                    "name": tu.name,
                    "input": tu.input,
                    "output": result,
                    "round": round_num,
                })

                # Format tool result for Claude
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result, default=str),
                })

            # Add tool results as user message (Claude API format)
            messages.append({"role": "user", "content": tool_results})

        # Hit max rounds without finishing
        latency = int((time.time() - start_time) * 1000)

        # Try to get whatever text we have
        partial_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                partial_text += block.text

        if not partial_text:
            partial_text = "That took longer than expected. Your items should be on the list — take a look and let me know if anything needs adjusting."

        session.conversation_history.add_assistant_message(partial_text)

        return AgentTurnResult(
            text=partial_text,
            tool_calls=tool_call_log,
            rounds=AGENT_MAX_TOOL_ROUNDS,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            latency_ms=latency,
            model=AGENT_MODEL,
            hit_limit=True,
        )

    except anthropic.APIConnectionError as e:
        logger.error(f"API connection error: {e}")
        return AgentTurnResult(
            text="Having trouble reaching the AI service. Try again in a moment.",
            error=f"api_connection: {e}",
            latency_ms=int((time.time() - start_time) * 1000),
        )
    except anthropic._exceptions.OverloadedError as e:
        logger.error(f"API overloaded after {API_MAX_RETRIES} retries: {e}")
        return AgentTurnResult(
            text="The AI service is overloaded right now. I'll try again — just send your message again in a minute.",
            error=f"overloaded_after_retries: {e}",
            latency_ms=int((time.time() - start_time) * 1000),
        )
    except anthropic.RateLimitError as e:
        logger.error(f"Rate limited after retries: {e}")
        return AgentTurnResult(
            text="I'm getting rate limited right now. Give me a minute and try again.",
            error=f"rate_limit_after_retries: {e}",
            latency_ms=int((time.time() - start_time) * 1000),
        )
    except anthropic.APIStatusError as e:
        logger.error(f"API error: {e.status_code} {e.message}")
        return AgentTurnResult(
            text="Something went wrong on my end. Try again?",
            error=f"api_error_{e.status_code}: {e.message}",
            latency_ms=int((time.time() - start_time) * 1000),
        )
    except Exception as e:
        logger.exception(f"Unexpected error in agent turn: {e}")
        return AgentTurnResult(
            text="Sorry, something unexpected happened. Try again?",
            error=f"unexpected: {str(e)}",
            latency_ms=int((time.time() - start_time) * 1000),
        )


# ============================================================
# USAGE TRACKING
# ============================================================

def log_agent_turn(session, user_text, result: AgentTurnResult):
    """
    Save agent turn metrics to the agent_turns table.
    Call this after every agent turn for cost tracking.
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_turns (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT,
                list_id         INTEGER,
                user_text       TEXT,
                agent_state     TEXT,
                tools_called    TEXT,
                tool_rounds     INTEGER,
                response_text   TEXT,
                input_tokens    INTEGER,
                output_tokens   INTEGER,
                latency_ms      INTEGER,
                model           TEXT,
                fallback_used   INTEGER DEFAULT 0,
                error           TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        """)

        tools_called = json.dumps([tc["name"] for tc in result.tool_calls])

        conn.execute("""
            INSERT INTO agent_turns
            (session_id, list_id, user_text, agent_state, tools_called,
             tool_rounds, response_text, input_tokens, output_tokens,
             latency_ms, model, fallback_used, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(id(session)),
            getattr(session, "list_id", None),
            user_text[:500],
            getattr(session, "state", "unknown"),
            tools_called,
            result.rounds,
            result.text[:1000],
            result.input_tokens,
            result.output_tokens,
            result.latency_ms,
            result.model,
            1 if result.error else 0,
            result.error,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to log agent turn: {e}")


# ============================================================
# DAILY USAGE CHECK
# ============================================================

