"""Clean up the duplicate list_items rows that accumulated before
list_add_item learned to dedup. Run on the Ally to fix the bloat
on the active scheduled list.

Strategy per (list_id, item_id) group with > 1 row:
  1. Pick the "winner" row: prefer non-skipped + most recent.
     - if any row has status != 'skipped' AND user_action != 'skipped',
       pick the highest id among those (most recent active).
     - else (every row is skipped) pick the highest id.
  2. Sum the quantities of all NON-skipped rows into the winner.
  3. Mark the losers status='skipped', user_action='skipped' so they
     don't reappear but the history stays auditable.
  4. Never DELETE -- skip-marking preserves audit trail.

Usage:
    python3 scripts/dedupe_list_items.py            # dry-run, prints what it WOULD do
    python3 scripts/dedupe_list_items.py --apply    # actually does the cleanup

Idempotent: re-running after --apply finds nothing to do.
"""

import argparse
import sqlite3
import sys
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually run the cleanup. Without this flag, "
                         "prints a preview without touching the DB.")
    ap.add_argument("--db", default="autostock.db",
                    help="Path to the SQLite DB (default: autostock.db)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Find every (list_id, item_id) with more than one row, restricting
    # to lists that are still "live" (draft / pending / suggested).
    # Done deals don't need cleanup -- they're historical.
    live_lists = [r[0] for r in conn.execute(
        """SELECT id FROM shopping_lists
           WHERE status IN ('draft', 'pending', 'in_review', 'suggested')
             AND COALESCE(dismissed_at, '') = ''"""
    ).fetchall()]
    if not live_lists:
        print("No live lists. Nothing to do.")
        return

    placeholders = ",".join("?" * len(live_lists))
    dups = conn.execute(
        f"""SELECT list_id, item_id, COUNT(*) AS n
            FROM list_items
            WHERE list_id IN ({placeholders}) AND item_id IS NOT NULL
            GROUP BY list_id, item_id
            HAVING COUNT(*) > 1
            ORDER BY list_id, item_id""",
        live_lists,
    ).fetchall()

    if not dups:
        print(f"No duplicates in {len(live_lists)} live list(s). Clean.")
        return

    print(f"Found {len(dups)} (list_id, item_id) groups with dups across {len(live_lists)} live list(s):\n")
    total_to_skip = 0
    plan = []
    for dup in dups:
        rows = conn.execute(
            """SELECT id, status, user_action, quantity, estimated_price,
                      added_by, created_at, item_name_text
               FROM list_items
               WHERE list_id = ? AND item_id = ?
               ORDER BY id""",
            (dup["list_id"], dup["item_id"]),
        ).fetchall()
        # Partition active vs skipped
        active = [r for r in rows if (r["status"] or "") != "skipped"
                                   and (r["user_action"] or "") != "skipped"]
        all_skipped = [r for r in rows if r not in active]

        # Pick winner
        if active:
            winner = max(active, key=lambda r: r["id"])
            losers = [r for r in rows if r["id"] != winner["id"]]
            # Sum active quantities into winner
            qty_sum = sum((r["quantity"] or 0) for r in active)
        else:
            # Every row is skipped -- pick the latest, keep status=skipped
            winner = max(rows, key=lambda r: r["id"])
            losers = [r for r in rows if r["id"] != winner["id"]]
            qty_sum = winner["quantity"] or 0

        total_to_skip += sum(1 for l in losers if (l["status"] or "") != "skipped")
        plan.append({
            "list_id": dup["list_id"],
            "item_id": dup["item_id"],
            "winner": winner["id"],
            "winner_qty_after": qty_sum,
            "losers": [(l["id"], l["status"], l["user_action"]) for l in losers],
        })

        print(f"  list={dup['list_id']} item_id={dup['item_id']} -> winner=li{winner['id']} "
              f"(qty {qty_sum}), {len(losers)} losers will be skipped")
        for l in losers:
            print(f"     li{l['id']:5} status={l['status']!s:10} ua={l['user_action']!s:10} "
                  f"qty={l['quantity']} added_by={l['added_by']} name={l['item_name_text'] or '?':30}")

    print()
    print(f"Summary: {len(plan)} groups, would skip {total_to_skip} duplicate active rows.")

    if not args.apply:
        print("\nDry run. Pass --apply to commit.")
        return

    print("\nApplying...")
    cur = conn.cursor()
    for entry in plan:
        # Set winner quantity
        cur.execute(
            "UPDATE list_items SET quantity = ? WHERE id = ?",
            (entry["winner_qty_after"], entry["winner"]),
        )
        # Mark losers skipped if not already
        for lid, _st, _ua in entry["losers"]:
            cur.execute(
                """UPDATE list_items
                   SET status = 'skipped', user_action = 'skipped'
                   WHERE id = ? AND (
                     COALESCE(status,'') != 'skipped'
                     OR COALESCE(user_action,'') != 'skipped'
                   )""",
                (lid,),
            )
    conn.commit()
    print(f"Applied. Re-running:")

    # Show the post-state per live list
    for lid in live_lists:
        n = conn.execute(
            "SELECT COUNT(*) FROM list_items "
            "WHERE list_id = ? AND COALESCE(status,'') != 'skipped'",
            (lid,),
        ).fetchone()[0]
        print(f"  list {lid}: now {n} active items")


if __name__ == "__main__":
    main()
