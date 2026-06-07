"""Send the current draft scheduled list to a household member on WhatsApp.

Looks up the member by name in `household_members`, then POSTs the
rendered list to /api/diag/send-test-list with HMAC auth. The endpoint
renders the v2 cart message format including clickable product URLs
(format_cart_message uses include_links=True by default), so the
recipient gets a tappable list.

No PII in this script. The recipient's phone number is read from the
live DB at run time, not embedded in source or chat.

Usage:
    python3 scripts/send_scheduled_to_member.py Sara
        # sends current draft list to Sara

    python3 scripts/send_scheduled_to_member.py "Sara" --list-id 1109
        # sends a specific list

    python3 scripts/send_scheduled_to_member.py "Sara" --regen
        # regenerates the scheduled list FIRST (so the new inventory
        # model has a chance to apply), then sends the fresh result

    python3 scripts/send_scheduled_to_member.py "Sara" --host http://localhost:8000
        # hit the local instance instead of public Ally

Exit codes:
  0  sent OK
  1  send failed / network error / unauth
  2  config error (.env missing GITHUB_WEBHOOK_SECRET, member not found)
"""
import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def find_member(db_path: Path, name_query: str):
    """Return (member_id, name, display_name, phone) for the first
    member whose name or display_name matches name_query (case-insensitive).
    Returns None if no match.
    """
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT id, name, display_name, phone
               FROM household_members
               WHERE is_active = 1
                 AND (LOWER(name) = LOWER(?) OR LOWER(display_name) = LOWER(?))
               ORDER BY role DESC, id ASC
               LIMIT 1""",
            (name_query, name_query),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def hmac_post(host: str, path: str, secret: str, payload: dict, timeout: int = 60):
    """POST to host+path with X-Hub-Signature-256 = sha256(secret, body).

    Returns (status_code, body_text).
    """
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        host.rstrip("/") + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="Household member name (e.g. Sara)")
    ap.add_argument("--host", default="https://hearthkeeper.cc",
                    help="Server base URL (default: %(default)s)")
    ap.add_argument("--db", default=str(REPO_ROOT / "autostock.db"),
                    help="SQLite DB path (default: %(default)s)")
    ap.add_argument("--list-id", type=int,
                    help="Explicit list_id; omit to use the latest draft")
    ap.add_argument("--regen", action="store_true",
                    help="Force regenerate the scheduled list before sending")
    ap.add_argument("--target-day",
                    help="Override target day name (e.g. Tuesday); "
                         "defaults to the list's target_date weekday")
    ap.add_argument("--deliveries",
                    help='Optional delivery summary string '
                         '(e.g. "Tuesday 7-11am · Wed 9-11am")')
    args = ap.parse_args()

    env = load_dotenv(REPO_ROOT / ".env")
    secret = env.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        print(f"ERROR: GITHUB_WEBHOOK_SECRET not found in {REPO_ROOT / '.env'}",
              file=sys.stderr)
        return 2

    member = find_member(Path(args.db), args.name)
    if not member:
        print(f"ERROR: no active household_member matching '{args.name}'.",
              file=sys.stderr)
        print(f"  Tried: name == '{args.name}' and display_name == '{args.name}' "
              f"(case-insensitive).", file=sys.stderr)
        return 2

    phone = member["phone"]
    member_name = member["display_name"] or member["name"]
    masked = phone[:3] + "***" + phone[-3:] if len(phone) > 6 else "***"
    print(f"Recipient: {member_name} (phone {masked})")

    if args.regen:
        print("Step 1/2: regenerating scheduled list ...")
        st, txt = hmac_post(
            args.host, "/api/diag/regenerate-suggestions", secret,
            payload={},
        )
        if st != 200:
            print(f"  regen failed: status={st} body={txt[:200]}", file=sys.stderr)
            return 1
        # Try to pull the new list_id out of the response so we send
        # the freshly-built one (not whatever lands as "latest draft"
        # in a race).
        try:
            data = json.loads(txt)
            if isinstance(data, dict) and data.get("list_id"):
                args.list_id = int(data["list_id"])
                print(f"  regen ok -> new list_id={args.list_id}")
            else:
                print(f"  regen ok (no list_id in response, will use latest draft)")
        except Exception:
            print(f"  regen ok (couldn't parse response, will use latest draft)")

    payload = {
        "phone": phone,
        "member_name": member_name,
    }
    if args.list_id:
        payload["list_id"] = args.list_id
    if args.target_day:
        payload["target_day"] = args.target_day
    if args.deliveries:
        payload["deliveries"] = args.deliveries

    print(f"Step 2/2: sending list (list_id={args.list_id or 'latest'}) ...")
    st, txt = hmac_post(
        args.host, "/api/diag/send-test-list", secret, payload=payload
    )
    if st == 200:
        try:
            data = json.loads(txt)
        except Exception:
            data = {}
        wa_id = data.get("wa_message_id") or data.get("sent_message_id") or "?"
        msg_len = data.get("rendered_length") or len(data.get("message") or "")
        print(f"  OK: WA msg id={wa_id}  length={msg_len} chars")
        return 0
    print(f"  FAILED: status={st} body={txt[:400]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
