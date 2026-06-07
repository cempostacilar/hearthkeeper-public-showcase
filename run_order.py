"""
AutoRestock - Order Orchestrator

The top-level script that runs the full end-to-end flow:
  1. Generate shopping list (what's due for restocking?)
  2. Interactive review (approve/skip/modify items)
  3. Build real carts on store websites
  4. Report results

This is the "brain" that ties all the pieces together.

Usage:
  python3 run_order.py                    # full flow for next order date (Tue/Fri)
  python3 run_order.py --date 2026-02-25  # specific date
  python3 run_order.py --dry-run          # preview only, no cart building
  python3 run_order.py --skip-review      # auto-approve all items
  python3 run_order.py --show             # show most recent list without generating new one

IMPORTANT: Close Edge before running (Playwright needs exclusive profile access).
"""

import asyncio
import sys
from datetime import datetime

from db_setup import create_database
from list_generate import list_generate, list_get, list_update_item, list_add_item, list_finalize
from cart_builder import build_cart, print_cart_report


# --------------------------------------------
# Pretty printing
# --------------------------------------------

def _print_header(text):
    """Print a section header."""
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def _print_shopping_list(result):
    """Pretty-print a generated shopping list grouped by store."""
    _print_header(f"Shopping List for {result['day_name']} {result['target_date']}")

    if not result["items"]:
        print("\n  Nothing due for restocking! You're all stocked up.")
        return

    for store_name, items in result["by_store"].items():
        store_total = sum(i.get("estimated_price", 0) or 0 for i in items)
        print(f"\n  {store_name} ({len(items)} items, ~{store_total:.0f} AED)")
        print(f"  {'-'*50}")

        for i, item in enumerate(items, 1):
            qty = item.get("quantity", 1)
            price = item.get("estimated_price", 0) or 0
            category = item.get("category", "")
            notes = item.get("notes", "")

            # Truncate notes for display
            note_short = ""
            if notes:
                note_short = f" -- {notes[:40]}..." if len(notes) > 40 else f" -- {notes}"

            status_icon = "[OK]" if item.get("status") == "approved" else "[ ]"
            print(f"    {status_icon} {i:2d}. {item['item_name']:<20s} x{qty:<4g} ~{price:>6.2f} AED  ({category}){note_short}")

    # Summary
    summary = result.get("summary", {})
    print(f"\n  {'-'*50}")
    print(f"  TOTAL: {summary.get('total_items', 0)} items, ~{summary.get('total_estimated', 0):.2f} AED")

    for store_name, store_info in summary.get("stores", {}).items():
        print(f"    {store_name}: {store_info['count']} items, ~{store_info['estimated']:.2f} AED")


def _print_minimum_order_warnings(result):
    """Flag stores with very few items (minimum order logic from PRD)."""
    warnings = []
    for store_name, items in result["by_store"].items():
        count = len(items)
        total = sum(i.get("estimated_price", 0) or 0 for i in items)

        if count <= 2 or total < 50:
            warnings.append(
                f"  !! {store_name} only has {count} item(s) (~{total:.0f} AED). "
                f"Worth ordering separately, or move to another store?"
            )

    if warnings:
        print("\n  MINIMUM ORDER WARNINGS:")
        for w in warnings:
            print(w)


# --------------------------------------------
# Interactive review
# --------------------------------------------

def _interactive_review(list_id):
    """
    Let the user review and modify the shopping list interactively.

    Commands:
      Enter        = approve all remaining items
      <number>     = toggle skip/approve for that item
      q<number>    = change quantity (e.g., q3 2 = set item 3 to qty 2)
      add <name>   = add a new item
      done         = finish review
    """
    shopping_list = list_get(list_id)
    if not shopping_list:
        print("  ERROR: Could not load list.")
        return False

    items = shopping_list["items"]
    if not items:
        print("  No items to review.")
        return False

    print(f"\n  REVIEW MODE")
    print(f"  Commands:")
    print(f"    Enter       = approve all and continue")
    print(f"    <number>    = skip that item (toggle)")
    print(f"    q<num> <qty> = change quantity (e.g., 'q3 2' sets item 3 qty to 2)")
    print(f"    add <name>  = add an item")
    print(f"    done        = finish review")
    print()

    # Number items across all stores
    flat_items = []
    for store_name in sorted(shopping_list["by_store"].keys()):
        for item in shopping_list["by_store"][store_name]:
            flat_items.append(item)

    while True:
        # Display current state
        idx = 1
        for store_name in sorted(shopping_list["by_store"].keys()):
            store_items = [fi for fi in flat_items if fi["store_name"] == store_name]
            print(f"  {store_name}:")
            for item in store_items:
                skipped = item.get("user_action") == "skipped"
                mark = "[SKIP]" if skipped else "[ OK ]"
                qty = item.get("quantity", 1)
                price = item.get("estimated_price", 0) or 0
                print(f"    {mark} {idx:2d}. {item['item_name']:<20s} x{qty:<4g} ~{price:>6.2f} AED")
                idx += 1
            print()

        try:
            user_input = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Review cancelled.")
            return False

        if not user_input or user_input.lower() == "done":
            # Approve all non-skipped items
            for item in flat_items:
                if item.get("user_action") != "skipped":
                    list_update_item(item["list_item_id"], "approved")
            print("  All non-skipped items approved.")
            break

        elif user_input.lower().startswith("add "):
            item_name = user_input[4:].strip()
            if item_name:
                result = list_add_item(list_id, item_name)
                if result:
                    print(f"  Added: {item_name}")
                    # Refresh the list
                    shopping_list = list_get(list_id)
                    flat_items = []
                    for sn in sorted(shopping_list["by_store"].keys()):
                        for item in shopping_list["by_store"][sn]:
                            flat_items.append(item)
                else:
                    print(f"  Could not add '{item_name}' (not found in items table)")

        elif user_input.lower().startswith("q") and len(user_input) > 1:
            # Change quantity: q3 2 = set item 3 to qty 2
            parts = user_input[1:].split()
            if len(parts) >= 2:
                try:
                    item_num = int(parts[0])
                    new_qty = float(parts[1])
                    if 1 <= item_num <= len(flat_items):
                        target = flat_items[item_num - 1]
                        list_update_item(target["list_item_id"], "approved", quantity=new_qty)
                        target["quantity"] = new_qty
                        target["estimated_price"] = (target.get("price_each", 0) or 0) * new_qty
                        print(f"  Updated {target['item_name']} qty to {new_qty}")
                    else:
                        print(f"  Invalid item number: {item_num}")
                except ValueError:
                    print(f"  Usage: q<item#> <qty>  (e.g., 'q3 2')")

        else:
            # Toggle skip for an item number
            try:
                item_num = int(user_input)
                if 1 <= item_num <= len(flat_items):
                    target = flat_items[item_num - 1]
                    current = target.get("user_action", "")
                    if current == "skipped":
                        list_update_item(target["list_item_id"], "approved")
                        target["user_action"] = "approved"
                        print(f"  {target['item_name']} -> APPROVED")
                    else:
                        list_update_item(target["list_item_id"], "skipped")
                        target["user_action"] = "skipped"
                        print(f"  {target['item_name']} -> SKIPPED")
                else:
                    print(f"  Invalid item number: {item_num}")
            except ValueError:
                print(f"  Unknown command: {user_input}")

    return True


# --------------------------------------------
# Main orchestrator
# --------------------------------------------

async def run_order(target_date=None, dry_run=False, skip_review=False, show_only=False):
    """
    Run the full order flow:
      1. Generate (or load) shopping list
      2. Review and approve items
      3. Build carts on store websites
      4. Report results
    """
    create_database()

    # -- Step 1: Generate shopping list --
    if show_only:
        _print_header("MOST RECENT SHOPPING LIST")
        result = list_get()
        if not result:
            print("  No shopping lists found. Run without --show to generate one.")
            return
        _print_shopping_list(result)
        return

    _print_header("AUTORESTOCK ORDER FLOW")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if dry_run:
        print(f"  Mode: DRY RUN (no cart building)")

    print("\n  Step 1: Generating shopping list...")
    result = list_generate(target_date=target_date)

    if not result or not result.get("items"):
        print("  Nothing due for restocking! You're all stocked up.")
        return

    _print_shopping_list(result)
    _print_minimum_order_warnings(result)

    list_id = result["list_id"]

    # -- Step 2: Review --
    if skip_review:
        print("\n  Step 2: Auto-approving all items (--skip-review)")
        for item in result["items"]:
            list_update_item(item["list_item_id"], "approved")
    else:
        print("\n  Step 2: Review your shopping list")
        reviewed = _interactive_review(list_id)
        if not reviewed:
            print("  Order cancelled.")
            return

    # -- Step 3: Finalize --
    print("\n  Step 3: Finalizing list...")
    list_finalize(list_id)

    # Reload to see final state
    final_list = list_get(list_id)
    approved_count = sum(
        1 for i in final_list["items"]
        if i.get("user_action") == "approved" or i.get("status") == "approved"
    )
    skipped_count = sum(
        1 for i in final_list["items"]
        if i.get("user_action") == "skipped" or i.get("status") == "skipped"
    )
    print(f"  Finalized: {approved_count} approved, {skipped_count} skipped")

    if approved_count == 0:
        print("  No items approved. Nothing to build.")
        return

    if dry_run:
        print("\n  Step 4: [DRY RUN] Checking product availability...")
        report = await build_cart(list_id, dry_run=True)
        print_cart_report(report)
        print("\n  Dry run complete. No items were added to any cart.")
        return

    # -- Step 4: Build carts --
    print("\n  Step 4: Building carts...")
    print("  IMPORTANT: Close Edge browser now if it's open!")

    try:
        confirm = input("  Press Enter to start cart building (or 'q' to quit): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return

    if confirm.lower() == 'q':
        print("  Cart building cancelled. Your list is saved (id: {list_id}).")
        print("  Run again later with: python3 cart_builder.py --list-id {list_id}")
        return

    report = await build_cart(list_id)
    print_cart_report(report)

    # -- Step 5: Handoff --
    _print_header("READY FOR CHECKOUT")
    print("  Your carts are built! Here's what to do:")
    print()

    for store_name, sr in report.get("stores", {}).items():
        if sr["items_added"] > 0:
            print(f"  {store_name}:")
            print(f"    -> Open the browser and review your cart")
            print(f"    -> {sr['items_added']} items, ~{sr['estimated_total']:.0f} AED")
            if sr["items_failed"] > 0:
                print(f"    -> {sr['items_failed']} item(s) could not be added (see report above)")
            print()

    print("  The browser is still open. Review your carts and place the orders yourself.")
    print("  The agent NEVER clicks 'Place Order' -- that's your job!")
    print()


# --------------------------------------------
# CLI
# --------------------------------------------

async def main():
    target_date = None
    dry_run = False
    skip_review = False
    show_only = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--date" and i + 1 < len(args):
            target_date = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        elif args[i] == "--skip-review":
            skip_review = True
            i += 1
        elif args[i] == "--show":
            show_only = True
            i += 1
        else:
            i += 1

    await run_order(
        target_date=target_date,
        dry_run=dry_run,
        skip_review=skip_review,
        show_only=show_only,
    )


if __name__ == "__main__":
    asyncio.run(main())
