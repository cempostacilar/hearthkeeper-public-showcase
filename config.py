"""
Hearthkeeper - Configuration

Loads settings from .env file and environment variables.
API keys stay local, never committed to git.
"""

import os
from pathlib import Path

# Load .env file if it exists (simple loader, no dependencies)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            # Override empty values, don't clobber real env vars
            if value and not os.environ.get(key):
                os.environ[key] = value

# ── LLM Settings ──
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1024"))

# Regex confidence threshold — messages below this go to the LLM
LLM_CONFIDENCE_THRESHOLD = float(os.environ.get("LLM_CONFIDENCE_THRESHOLD", "0.8"))

# ── Store Manager Agent Settings ──
AGENT_ENABLED = os.environ.get("AGENT_ENABLED", "true").lower() == "true"
AGENT_MODEL = os.environ.get("AGENT_MODEL", LLM_MODEL)
AGENT_MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "1024"))
AGENT_MAX_TOOL_ROUNDS = int(os.environ.get("AGENT_MAX_TOOL_ROUNDS", "7"))
AGENT_TIMEOUT_MS = int(os.environ.get("AGENT_TIMEOUT_MS", "15000"))
AGENT_DAILY_TOKEN_BUDGET = int(os.environ.get("AGENT_DAILY_TOKEN_BUDGET", "500000"))

# ── WhatsApp Feedback Settings ──
WHATSAPP_ENABLED = os.environ.get("WHATSAPP_ENABLED", "false").lower() == "true"
WHATSAPP_SERVICE_URL = os.environ.get("WHATSAPP_SERVICE_URL", "http://localhost:3001")
WHATSAPP_FEEDBACK_TIMEOUT_MIN = int(os.environ.get("WHATSAPP_FEEDBACK_TIMEOUT_MIN", "60"))
WHATSAPP_FOLLOWUP_MIN = int(os.environ.get("WHATSAPP_FOLLOWUP_MIN", "30"))

# ── Order notifications (placed / on-the-way / delivered) ──
# VERIFY GATE: while True (default), every order-stage notification is
# routed to the curator (role='driver') ONLY -- so the wording can be
# approved before it reaches the rest of the household. Flip to "false"
# in the Ally .env once approved to go live to Sara + Max.
ORDER_NOTIFS_TEST_ONLY = os.environ.get("ORDER_NOTIFS_TEST_ONLY", "true").lower() == "true"

# ── GLOBAL outbound WhatsApp safety gate (fail-closed allowlist) ──
# Hard rule: never message anyone who isn't explicitly approved. ORDER_NOTIFS_
# TEST_ONLY above only covers order-stage notifications; other paths (feedback
# rounds, which send to ALL active members) bypassed it. This is the single
# source of truth, enforced at the send chokepoint (whatsapp_client.
# send_message), so NO feature or automated dispatcher can reach a real number
# on its own.
#
# Numbers come from the Ally .env (never committed): comma-separated E.164,
# e.g.  WHATSAPP_ALLOWLIST="+9........,+9........"
# Fail-closed: with enforcement ON and an EMPTY allowlist, every send is
# blocked. To go live to the whole household later, set
# WHATSAPP_ALLOWLIST_ENFORCED=false in the Ally .env.
WHATSAPP_ALLOWLIST_ENFORCED = os.environ.get("WHATSAPP_ALLOWLIST_ENFORCED", "true").lower() == "true"


def _parse_phone_allowlist(raw):
    nums = set()
    for part in (raw or "").replace(";", ",").split(","):
        p = "".join(part.split())  # strip all internal/edge whitespace
        if not p:
            continue
        if not p.startswith("+"):
            p = "+" + p
        nums.add(p)
    return nums


WHATSAPP_ALLOWLIST = _parse_phone_allowlist(os.environ.get("WHATSAPP_ALLOWLIST", ""))

# ── Webhook Security ──
# Shared secret the WhatsApp sidecar must send (X-Webhook-Secret header) on
# POST /webhook/whatsapp. Empty value => the webhook rejects every request.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# ── App Login (S1) ──
# Household passcode for the in-app login. Empty value => login always fails.
APP_PASSCODE = os.environ.get("APP_PASSCODE", "")
# Secret used to sign the session cookie. Empty value => sessions can't be issued.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")

# ── GitHub auto-deploy webhook ──
# Shared secret configured in the GitHub repo Settings > Webhooks page. The
# webhook handler at POST /webhook/deploy verifies the HMAC-SHA256 signature
# in the X-Hub-Signature-256 header. Empty value => the endpoint is disabled.
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
# Service name to restart after a successful pull (NSSM service on Windows).
DEPLOY_RESTART_SERVICE = os.environ.get("DEPLOY_RESTART_SERVICE", "hearthkeeper-app")
# Branch the deploy webhook will accept pushes for. Pushes to other branches
# are acknowledged but ignored. Defaults to the branch the Ally currently has
# checked out (resolved lazily at request time if this is empty).
DEPLOY_BRANCH = os.environ.get("DEPLOY_BRANCH", "")
