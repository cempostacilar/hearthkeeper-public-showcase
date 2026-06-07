"""Cart-build success report. Reads the live cart_attempts data and
prints a per-build breakdown of success/fail rates, top failure
reasons, and where the catalog/scraper pipeline is hurting most.

Run on the Ally (where the live DB lives):

    python3 scripts/cart_success_report.py

Or against the deployed Ally over HTTP from your home network:

    HEARTHKEEPER_URL=http://localhost:8000 python3 scripts/cart_success_report.py

The HTTP path uses GET /api/diag/cart-attempts (read-only, no auth)
and the local-DB path queries autostock.db directly. Output is the
same either way.
"""

import json
import os
import sqlite3
import sys
import urllib.request


def from_local_db(limit_builds=5):
    """Read cart_attempts directly. Faster than the HTTP path."""
    conn = sqlite3.connect("autostock.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM cart_attempts")
    total = cur.fetchone()[0]
    if total == 0:
        print("No cart_attempts rows yet -- nothing to analyze.")
        return

    print(f"Total attempts in DB: {total}\n")

    # Most-recent builds
    builds = cur.execute(
        """SELECT list_id, MIN(created_at) AS first_attempt,
                  COUNT(*) AS attempts,
                  SUM(success) AS succ,
                  COUNT(DISTINCT list_item_id) AS items_touched
           FROM cart_attempts
           GROUP BY list_id
           ORDER BY first_attempt DESC LIMIT ?""",
        (limit_builds,),
    ).fetchall()

    for b in builds:
        list_id = b["list_id"]
        rate = (b["succ"] / b["attempts"] * 100) if b["attempts"] else 0
        print("=" * 60)
        print(f"list_id={list_id}  first attempt: {b['first_attempt']}")
        print("=" * 60)
        print(
            f"  Items touched: {b['items_touched']}  "
            f"Attempts: {b['attempts']}  "
            f"Successes: {b['succ']}  "
            f"Failures: {b['attempts'] - b['succ']}  "
            f"({rate:.0f}%)"
        )
        # Per-store
        print()
        print("  Per store:")
        for s in cur.execute(
            """SELECT s.name AS store,
                      COUNT(DISTINCT ca.list_item_id) AS items,
                      COUNT(*) AS attempts,
                      SUM(success) AS succ
               FROM cart_attempts ca
               JOIN stores s ON s.id = ca.store_id
               WHERE ca.list_id = ?
               GROUP BY s.id ORDER BY attempts DESC""",
            (list_id,),
        ):
            srate = (s["succ"] / s["attempts"] * 100) if s["attempts"] else 0
            print(
                f"    {s['store']:14} items={s['items']:3}  "
                f"attempts={s['attempts']:3}  succ={s['succ']:3}  "
                f"({srate:.0f}%)"
            )
        # Top failure reasons
        print()
        print("  Top failure reasons:")
        for r in cur.execute(
            """SELECT reason_class, COUNT(*) AS n
               FROM cart_attempts
               WHERE list_id = ? AND success = 0
               GROUP BY reason_class ORDER BY n DESC LIMIT 6""",
            (list_id,),
        ):
            print(f"    {r['reason_class']:18} x{r['n']}")
        # Sample failure details
        print()
        print("  Sample failures (max 5):")
        for r in cur.execute(
            """SELECT ca.reason_class, ca.reason,
                      i.name AS item_name,
                      s.name AS store
               FROM cart_attempts ca
               JOIN stores s ON s.id = ca.store_id
               LEFT JOIN items i ON i.id = ca.item_id
               WHERE ca.list_id = ? AND ca.success = 0
               ORDER BY ca.created_at DESC LIMIT 5""",
            (list_id,),
        ):
            short = (r["reason"] or "").split("\n")[0][:80]
            print(
                f"    [{r['reason_class']:14}] {(r['item_name'] or '?'):20} "
                f"({r['store']}): {short}"
            )
        print()


def from_http(url_base):
    """Fall back to the diag endpoint. Requires the Ally to be
    reachable from wherever this runs."""
    url = url_base.rstrip("/") + "/api/diag/cart-attempts"
    print(f"Fetching {url}")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"HTTP error: {e}")
        return
    summary = data.get("summary", {})
    print(f"Summary: {json.dumps(summary, indent=2)}\n")
    attempts = data.get("attempts", []) or []
    print(f"Total attempts in payload: {len(attempts)}")
    # Group by list_id and present
    by_list = {}
    for a in attempts:
        lid = a.get("list_id")
        if lid is None:
            continue
        by_list.setdefault(lid, []).append(a)
    for lid in sorted(by_list.keys(), reverse=True)[:5]:
        rows = by_list[lid]
        succ = sum(1 for r in rows if r.get("success"))
        rate = (succ / len(rows) * 100) if rows else 0
        print()
        print(f"list_id={lid}  attempts={len(rows)}  succ={succ}  ({rate:.0f}%)")
        # First few failures
        failures = [r for r in rows if not r.get("success")][:5]
        for f in failures:
            reason = (f.get("reason") or "").split("\n")[0][:80]
            print(
                f"  [{f.get('reason_class','?'):14}] "
                f"item_id={f.get('item_id','?')}  "
                f"store_id={f.get('store_id','?')}  "
                f"reason: {reason}"
            )


def main():
    url = os.environ.get("HEARTHKEEPER_URL")
    if url:
        from_http(url)
    else:
        # Local DB path
        if not os.path.exists("autostock.db"):
            print(
                "autostock.db not found. Run from the repo root, or set "
                "HEARTHKEEPER_URL=http://localhost:8000 to use the HTTP diag."
            )
            sys.exit(1)
        from_local_db()


if __name__ == "__main__":
    main()
