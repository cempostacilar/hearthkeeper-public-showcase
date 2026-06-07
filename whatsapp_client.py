"""
Hearthkeeper - WhatsApp Client

Thin Python HTTP client for the Node.js WhatsApp sidecar service.
All sends/receives are logged to the whatsapp_messages table.
"""

import asyncio
import sqlite3

import httpx

from config import WHATSAPP_SERVICE_URL, WHATSAPP_ENABLED
from db_setup import DB_PATH
from app_logging import get_logger

logger = get_logger("whatsapp_client")

# Retry config
MAX_RETRIES = 2
RETRY_DELAY_S = 2
SEND_TIMEOUT_S = 15


def _normalize_phone(phone_raw) -> str:
    """Canonical E.164 for allowlist comparison: drop @c.us/@lid suffix,
    strip spaces/dashes, ensure a leading '+'. Empty string for falsy input."""
    if not phone_raw:
        return ""
    p = str(phone_raw).split("@")[0]
    p = "".join(p.split()).replace("-", "")
    if not p.startswith("+"):
        p = "+" + p
    return p


def _runtime_allowed_phones() -> set:
    """Phones approved at runtime via the app (household_members.allow_whatsapp
    = 1). Additive to config.WHATSAPP_ALLOWLIST so a recipient (e.g. the test
    number) can be approved from the authenticated app without editing the Ally
    .env or putting any number in the repo. Fail-safe: returns empty on any
    error (incl. the column not existing yet) so the gate stays fail-closed."""
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT phone FROM household_members "
                "WHERE COALESCE(allow_whatsapp, 0) = 1 AND COALESCE(phone, '') != ''"
            ).fetchall()
        finally:
            conn.close()
        return {_normalize_phone(r[0]) for r in rows if r and r[0]}
    except Exception:
        return set()


class WhatsAppClient:
    """Async HTTP client for the WhatsApp sidecar service."""

    def __init__(self, base_url: str = None):
        self.base_url = (base_url or WHATSAPP_SERVICE_URL).rstrip("/")

    # ── Public API ──

    async def send_message(
        self, phone: str, text: str, round_id: int = None, member_id: int = None
    ) -> str | None:
        """
        Send a text message via WhatsApp.
        Returns the WhatsApp message ID on success, None on failure.
        Logs to whatsapp_messages regardless of outcome.
        """
        if not WHATSAPP_ENABLED:
            logger.warning("WhatsApp disabled — skipping send to %s", phone)
            return None

        # ── GLOBAL fail-closed recipient gate ──
        # Single source of truth: nothing leaves this process to a number that
        # isn't explicitly allowlisted (config.WHATSAPP_ALLOWLIST, from the
        # Ally .env). Fail-closed — empty allowlist + enforcement on = block
        # everything. Catches EVERY caller (feedback rounds, order
        # notifications, schedulers, admin endpoint), so no automated flow can
        # message a household member without prior approval. Blocked attempts
        # are logged (whatsapp_messages + app_events) so we can see what WOULD
        # have been sent.
        from config import WHATSAPP_ALLOWLIST, WHATSAPP_ALLOWLIST_ENFORCED
        # Allowed = the .env allowlist UNION runtime-approved members. Both are
        # explicit opt-ins; default state (neither set) still blocks everything.
        allowed = set(WHATSAPP_ALLOWLIST) | _runtime_allowed_phones()
        if WHATSAPP_ALLOWLIST_ENFORCED and _normalize_phone(phone) not in allowed:
            masked = (phone[:3] + "***" + phone[-2:]) if phone and len(phone) > 6 else "***"
            logger.warning("WhatsApp send BLOCKED — %s not on allowlist", masked)
            try:
                from app_logging import log_event
                log_event("whatsapp_send_blocked", source="whatsapp_client",
                          detail=f"to={masked} chars={len(text or '')}", level="warning")
            except Exception:
                pass
            self._log_message(
                round_id=round_id, member_id=member_id, direction="outbound",
                wa_message_id=None, content=text, status="blocked",
                error_detail="recipient not allowlisted (.env or member flag)",
            )
            return None

        wa_message_id = None
        error_detail = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S) as client:
                    resp = await client.post(
                        f"{self.base_url}/send",
                        json={"phone": phone, "message": text},
                    )

                if resp.status_code == 200:
                    data = resp.json()
                    wa_message_id = data.get("messageId")
                    self._log_message(
                        round_id=round_id,
                        member_id=member_id,
                        direction="outbound",
                        wa_message_id=wa_message_id,
                        content=text,
                        status="sent",
                    )
                    logger.info("Sent to %s (msgId=%s)", phone, wa_message_id)
                    return wa_message_id

                elif resp.status_code == 503:
                    error_detail = "WhatsApp not connected"
                    logger.warning("WhatsApp not connected (attempt %d/%d)", attempt, MAX_RETRIES)
                else:
                    error_detail = resp.text
                    logger.error("Send failed %d: %s", resp.status_code, resp.text)

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                error_detail = str(e)
                logger.error("Connection error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY_S)

        # All retries exhausted — log failure
        self._log_message(
            round_id=round_id,
            member_id=member_id,
            direction="outbound",
            wa_message_id=None,
            content=text,
            status="failed",
            error_detail=error_detail,
        )
        return None

    async def is_connected(self) -> bool:
        """Check if the WhatsApp sidecar is connected."""
        if not WHATSAPP_ENABLED:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.base_url}/status")
            return resp.status_code == 200 and resp.json().get("connected", False)
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    async def request_restart(self, reason: str = "manual") -> bool:
        """Ask the sidecar to exit so NSSM can bring it back up.

        Authenticates with the same WEBHOOK_SECRET the sidecar uses
        to verify inbound webhook posts. The sidecar responds 200
        immediately, then process.exit(0)'s after a 500ms beat;
        NSSM AppExit=Restart brings it back.

        Returns True if the sidecar acknowledged (200) — the caller
        should NOT assume the new process is up yet; allow ~10-15s
        for NSSM + WhatsApp Web reconnect.

        Used by:
          * scheduler._nightly_whatsapp_restart   (3:30 AM preemptive)
          * scheduler._whatsapp_liveness_probe    (after N failures)
          * /api/diag/restart-whatsapp            (manual)
        """
        if not WHATSAPP_ENABLED:
            return False
        # Imported lazily so importing this module doesn't require
        # config to be fully populated.
        from config import GITHUB_WEBHOOK_SECRET  # reused as the WA secret on this host
        import os as _os
        secret = (
            _os.environ.get("WHATSAPP_WEBHOOK_SECRET")
            or _os.environ.get("WEBHOOK_SECRET")
            or GITHUB_WEBHOOK_SECRET
            or ""
        )
        if not secret:
            logger.warning("request_restart: no WEBHOOK_SECRET available — skipping")
            return False
        try:
            async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S) as client:
                resp = await client.post(
                    f"{self.base_url}/restart",
                    headers={"X-Webhook-Secret": secret},
                    json={"reason": reason},
                )
            if resp.status_code == 200:
                logger.info("Sidecar accepted restart (reason=%s)", reason)
                return True
            logger.warning(
                "Sidecar /restart returned %d: %s", resp.status_code, resp.text[:200]
            )
            return False
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            # If the sidecar is so dead it can't even accept /restart,
            # the only fallback would be `nssm restart` from a process
            # that has permission. We log and let the caller decide.
            logger.warning("request_restart connect/timeout: %s", e)
            return False

    async def get_status(self) -> dict:
        """Get full status from the sidecar (connected, error, hasQR)."""
        if not WHATSAPP_ENABLED:
            return {"connected": False, "error": "WhatsApp disabled", "hasQR": False}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.base_url}/status")
            if resp.status_code == 200:
                return resp.json()
            return {"connected": False, "error": f"HTTP {resp.status_code}", "hasQR": False}
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            return {"connected": False, "error": str(e), "hasQR": False}

    def log_incoming(
        self, round_id: int, member_id: int, wa_message_id: str, content: str
    ) -> bool:
        """
        Log an incoming WhatsApp message. Returns False if duplicate (already logged).
        """
        return self._log_message(
            round_id=round_id,
            member_id=member_id,
            direction="inbound",
            wa_message_id=wa_message_id,
            content=content,
            status="received",
        )

    # ── DB Logging ──

    def _log_message(
        self,
        round_id: int | None,
        member_id: int | None,
        direction: str,
        wa_message_id: str | None,
        content: str,
        status: str,
        error_detail: str | None = None,
    ) -> bool:
        """Insert into whatsapp_messages. Returns False on duplicate."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                """INSERT INTO whatsapp_messages
                   (round_id, member_id, direction, wa_message_id, content, status, error_detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (round_id, member_id, direction, wa_message_id, content, status, error_detail),
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            # Duplicate wa_message_id — already logged
            logger.debug("Duplicate message ignored: %s", wa_message_id)
            return False
        except Exception as e:
            logger.error("Failed to log message: %s", e)
            return False

    # ── Member Lookup ──

    @staticmethod
    def get_member_by_phone(phone_raw: str) -> dict | None:
        """
        Look up a household member by WhatsApp ID or E.164 phone.
        phone_raw: "+971XXXXXXXXX@c.us" or "+971XXXXXXXXX" or
                   "WA_LID_REDACTED@lid" (privacy-preserving LID format).

        For LID senders, falls back to a wa_lid column lookup since
        the raw LID number does NOT correspond to a phone number --
        it's an opaque per-contact hash.
        """
        is_lid = "@lid" in phone_raw
        ident = phone_raw.replace("@c.us", "").replace("@lid", "")

        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row

            if is_lid:
                # Inbound came in as LID (e.g. 'WA_LID_REDACTED@lid').
                # The numeric prefix is the LID; match against the
                # wa_lid column. No phone fallback because LID and
                # phone are unrelated namespaces.
                row = conn.execute(
                    "SELECT * FROM household_members "
                    "WHERE wa_lid = ? AND is_active = 1",
                    (ident,),
                ).fetchone()
                if row:
                    conn.close()
                    return dict(row)
                # Some clients still send @c.us for the same contact
                # in other paths; if no LID match, log + give up.
                logger.info(
                    "WhatsApp LID sender unrecognised (no wa_lid match): %s",
                    phone_raw,
                )
                conn.close()
                return None

            # Standard phone-format sender (@c.us or bare digits).
            phone = ident
            if not phone.startswith("+"):
                phone = "+" + phone

            row = conn.execute(
                "SELECT * FROM household_members WHERE phone = ? AND is_active = 1",
                (phone,),
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.error("Member lookup failed: %s", e)
            return None


# Module-level singleton
wa_client = WhatsAppClient()
