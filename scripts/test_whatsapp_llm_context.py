"""Drive /api/diag/simulate-reply on the deployed Ally with messages
that bypass Tier-1 and hit the real Claude LLM. Reports what the LLM
produced and how the dispatcher routed each.

Why this script: the WhatsApp LLM fallback now ships a context-aware
prompt that includes the user's current numbered cart, household
preferences, stop-suggesting items, pinned products, and pending swap
state. This driver feeds the LLM 6 natural-language messages designed
to slip past Tier-1 regex and verifies the LLM produces dispatcher-
shaped actions grounded in the cart.

USAGE
    # On the Ally (loopback, no Cloudflare gate):
    python3 scripts/test_whatsapp_llm_context.py

    # Or against the public URL from your home network:
    HEARTHKEEPER_URL=https://hearthkeeper.cc python3 scripts/...

    # Optional overrides (defaults loaded from local DB / env)
    SIMULATE_REPLY_PHONE=+971XXXX...
    GITHUB_WEBHOOK_SECRET=...

The harness intercepts every outbound send -- nothing is actually
delivered to WhatsApp.
"""

import hmac, hashlib, json, os, sys
import urllib.request, urllib.error
import sqlite3


def get_phone() -> str:
    """Resolve the phone for the simulate-reply call.

    Priority:
      1. SIMULATE_REPLY_PHONE env var
      2. local autostock.db -> household_members.name = 'Cem'
    Raises SystemExit if neither is available.
    """
    phone = os.environ.get("SIMULATE_REPLY_PHONE")
    if phone:
        return phone
    try:
        conn = sqlite3.connect("autostock.db")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT phone FROM household_members WHERE name = 'Cem' LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row["phone"]:
            return row["phone"]
    except Exception as e:
        print(f"WARN: couldn't read autostock.db for phone lookup: {e}")
    print(
        "FATAL: no phone available. Set SIMULATE_REPLY_PHONE=+971... "
        "in env or run from the AutoRestock directory where autostock.db lives."
    )
    raise SystemExit(1)


def get_secret() -> str:
    """Resolve the webhook secret. Must be set via env."""
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        print(
            "FATAL: GITHUB_WEBHOOK_SECRET not set. Set it from the Ally's "
            "secrets file or as an env var before running this script."
        )
        raise SystemExit(1)
    return secret


URL = os.environ.get("HEARTHKEEPER_URL", "http://localhost:8000")
URL = URL.rstrip("/") + "/api/diag/simulate-reply"

# Test messages picked to BYPASS Tier-1:
#   - no add/get/need/buy/etc. start verb
#   - no skip/remove/stop/never/drop/cancel/swap anywhere
#   - no don't/dont, no "take out", no "no more"
#   - not pure confirm/done/undo patterns
# So they fall through to the Tier-3 LLM, where the new context block
# should let it produce a correct dispatcher action.
SCENARIOS = [
    {
        "name": "have-it via natural phrasing",
        "text": "I picked up the bread on my way home",
        "expect_hint": "have_it ref_number matching whichever cart slot is bread",
    },
    {
        "name": "have-it ambiguous referent",
        "text": "wife already grabbed the eggs this morning",
        "expect_hint": "have_it ref_number for eggs",
    },
    {
        "name": "swap via natural phrasing",
        "text": "any other options you can show me for the pasta?",
        "expect_hint": "swap_request ref_number for pasta",
    },
    {
        "name": "confirm via natural phrasing",
        "text": "yeah this all looks fine to me",
        "expect_hint": "confirm (may also be caught by Tier-1)",
    },
    {
        "name": "compound: have + add",
        "text": "we already have eggs but could you find me almond milk too",
        "expect_hint": "have_it for eggs + add for almond milk",
    },
    {
        "name": "out-of-scope question",
        "text": "are these prices accurate compared to last week?",
        "expect_hint": "unknown (no actionable intent)",
    },
]


def hmac_sign(body_bytes: bytes, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), body_bytes, hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def call(text: str, phone: str, secret: str, reset: bool = False):
    body = json.dumps({
        "phone": phone,
        "text": text,
        "reset_list_items_first": reset,
    }).encode("utf-8")
    sig = hmac_sign(body, secret)
    req = urllib.request.Request(
        URL, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"network error: {e}"


def render(idx: int, sc: dict, status, body, also_resets: bool = False):
    note = " (also resets list state)" if also_resets else ""
    print(f"--- Scenario {idx}: {sc['name']}{note} ---")
    print(f"  text: {sc['text']!r}")
    print(f"  expect: {sc['expect_hint']}")
    print(f"  http: {status}")
    try:
        data = json.loads(body)
        out = data.get("captured_outbound") or []
        for o in out:
            t = (o.get("text") or "")[:240].replace("\n", " | ")
            print(f"  outbound -> phone={o.get('phone','?')[-4:]}: {t}")
        ar = data.get("action_result")
        if ar:
            print(f"  action_result: {json.dumps(ar)[:400]}")
        diff = data.get("list_item_changes") or []
        if diff:
            print(f"  list_item_changes: {json.dumps(diff)[:400]}")
    except Exception:
        print(f"  raw body: {body[:600]}")
    print()


def main():
    phone = get_phone()
    secret = get_secret()
    print(f">> driving {URL}")
    print(f">> phone tail = {phone[-4:]}  (full not echoed)")
    print()

    # First call resets list state so each scenario starts clean.
    status, body = call(
        SCENARIOS[0]["text"], phone=phone, secret=secret, reset=True,
    )
    render(1, SCENARIOS[0], status, body, also_resets=True)

    for i, sc in enumerate(SCENARIOS[1:], start=2):
        status, body = call(
            sc["text"], phone=phone, secret=secret, reset=False,
        )
        render(i, sc, status, body)


if __name__ == "__main__":
    main()
