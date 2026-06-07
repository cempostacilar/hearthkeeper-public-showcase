"""Force the Ally to regenerate its active Scheduled list right now.

Hits /api/diag/regenerate-suggestions with the HMAC signature derived from
GITHUB_WEBHOOK_SECRET in .env (same key the GitHub webhook uses).

Why this script exists:
  - The 2026-05-27 list_generate tightening (require times_ordered >= 3 and
    last_ordered within 2x cadence) only affects NEWLY GENERATED lists.
    Existing draft lists that were built before the fix keep their old
    inflated item set until something regenerates them.
  - The nightly scheduler will regenerate eventually, but if you want the
    bloat gone now without waiting, run this.
  - Auth is HMAC over the request body so no plaintext secret is ever
    transmitted; matches the existing /webhook/deploy and other diag-trigger
    endpoints.

Usage:
    python3 scripts/regenerate_scheduled.py
        # regenerates against next_order_date() (default)

    python3 scripts/regenerate_scheduled.py --target 2026-06-30
        # regenerates for a specific target date

    python3 scripts/regenerate_scheduled.py --host http://localhost:8000
        # hit a local instance instead of the public Ally
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.request
from pathlib import Path


def load_dotenv(path: Path) -> dict:
    """Cheap .env reader -- no python-dotenv dep, no surprises."""
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="https://hearthkeeper.cc",
                    help="Server base URL (default: %(default)s)")
    ap.add_argument("--target",
                    help="YYYY-MM-DD target date; default = server's "
                         "next_order_date()")
    args = ap.parse_args()

    env_path = Path(__file__).resolve().parent.parent / ".env"
    env = load_dotenv(env_path)
    secret = env.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        print(f"ERROR: GITHUB_WEBHOOK_SECRET not found in {env_path}",
              file=sys.stderr)
        sys.exit(2)

    payload = {}
    if args.target:
        payload["target_date"] = args.target
    body = json.dumps(payload).encode("utf-8") if payload else b""

    sig = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()

    url = args.host.rstrip("/") + "/api/diag/regenerate-suggestions"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    print(f"POST {url}")
    if payload:
        print(f"  body: {payload}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(f"  status: {resp.status}")
            print(f"  body  : {resp.read().decode('utf-8')[:1200]}")
    except urllib.error.HTTPError as e:
        print(f"  status: {e.code}")
        print(f"  body  : {e.read().decode('utf-8')[:1200]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
