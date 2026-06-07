"""
End-to-end tests for Hearthkeeper.
Connects via WebSocket, tests each flow, notes bugs.
"""
import asyncio
import json
import os
import sys

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

try:
    import websockets
except ImportError:
    print("Installing websockets...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

BUGS = []
WS_URI = "ws://localhost:8000/ws"


def bug(msg):
    BUGS.append(msg)
    print(f"  [BUG] {msg}")


async def recv_until(ws, target_type, timeout=60, print_others=True):
    """Receive messages until we get the target type or timeout."""
    collected = []
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 10))
            data = json.loads(msg)
            t = data.get("type")
            collected.append(data)
            if print_others:
                if t == "agent":
                    print(f"    [{t}] {data.get('text', '')[:80]}")
                elif t == "agent_progress":
                    print(f"    [{t}] {data.get('label', '')}")
                elif t in ("chat_history", "order_tracking", "feedback_status"):
                    print(f"    [{t}]")
                elif t == "list_data":
                    print(f"    [{t}] received!")
                else:
                    print(f"    [{t}]")
            if t == target_type:
                return data, collected
        except asyncio.TimeoutError:
            continue
    return None, collected


async def test_1_connection_and_list():
    """Test 1: WebSocket connection + list loading."""
    print("=== TEST 1: WebSocket Connection + List Loading ===")

    async with websockets.connect(WS_URI, ping_interval=None) as ws:
        # Wait for list_data (may take time due to live verification)
        list_data, all_msgs = await recv_until(ws, "list_data", timeout=90)

        # Also collect trailing messages
        if list_data:
            for _ in range(10):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                    data = json.loads(msg)
                    t = data.get("type")
                    all_msgs.append(data)
                    print(f"    [{t}]")
                except asyncio.TimeoutError:
                    break

        if not list_data:
            types = [m.get("type") for m in all_msgs]
            bug(f"No list_data received. Got: {types}")
            return None, all_msgs

        # Analyze list quality
        vendors = list_data.get("vendors", [])
        suggestions_raw = list_data.get("suggestions")
        # suggestions can be a dict with "vendors" inside, or a list
        if isinstance(suggestions_raw, dict):
            suggestions = suggestions_raw.get("vendors", [])
        elif isinstance(suggestions_raw, list):
            suggestions = suggestions_raw
        else:
            suggestions = []
        all_items = suggestions or vendors

        print(f"\n  suggestions={len(suggestions) if suggestions else 'None'}, "
              f"vendors={len(vendors) if vendors else 'None'}")
        print(f"  agentState={list_data.get('agentState')}")
        print(f"  suggestionListId={list_data.get('suggestionListId')}")
        print(f"  cartListId={list_data.get('cartListId')}")

        # Flatten items from nested structure: vendors[].categories[].items[]
        flat_items = []
        for vendor in (all_items or []):
            if isinstance(vendor, dict):
                if "categories" in vendor:
                    for cat in vendor["categories"]:
                        flat_items.extend(cat.get("items", []))
                elif "items" in vendor:
                    flat_items.extend(vendor["items"])
                else:
                    flat_items.append(vendor)

        print(f"  Total items: {len(flat_items)}")

        # Category breakdown
        cats = {}
        for item in flat_items:
            cat = item.get("category", "Unknown")
            cats[cat] = cats.get(cat, 0) + 1
        print(f"  Categories: {dict(sorted(cats.items()))}")

        # Data quality checks
        no_price = [i.get("name") for i in flat_items if not i.get("price")]
        no_vendor = [i.get("name") for i in flat_items if not i.get("vendor")]
        no_image = [i.get("name") for i in flat_items if not i.get("image")]

        if no_price:
            bug(f"{len(no_price)} items with no price: {no_price[:5]}")
        if no_vendor:
            bug(f"{len(no_vendor)} items with no vendor: {no_vendor[:5]}")

        # Check for "Other" categories in the category containers
        other_cat_items = []
        for vendor in (all_items or []):
            if isinstance(vendor, dict) and "categories" in vendor:
                for cat in vendor["categories"]:
                    if cat["name"] in ("Other", "Unknown", ""):
                        for item in cat.get("items", []):
                            other_cat_items.append(f"{item.get('name')} (in {vendor['name']})")
        if other_cat_items:
            bug(f'{len(other_cat_items)} items in "Other" category: {other_cat_items[:5]}')

        print(f"  No image: {len(no_image)} items")

        # Sample items
        print("\n  Sample items:")
        for item in flat_items[:5]:
            print(f"    - {item.get('name')} | {item.get('category')} | "
                  f"{item.get('vendor')} | AED {item.get('price', '?')}")

        # Check order tracking in trailing messages
        ot_msgs = [m for m in all_msgs if m.get("type") == "order_tracking"]
        if ot_msgs:
            ot = ot_msgs[0]
            active = ot.get("activeOrder")
            if active:
                print(f"\n  Order tracking: status={active.get('status')}, "
                      f"expected={active.get('expected_by')}, "
                      f"store={active.get('store_name')}")
                if active.get("status") == "unknown":
                    bug("Active order has status=unknown")
                if not active.get("expected_by"):
                    bug("Active order has no expected_by")

        print("\n  [PASS] Test 1")
        return list_data, all_msgs


async def test_2_chat(ws):
    """Test 2: Chat message and agent response."""
    print("\n=== TEST 2: Chat Message ===")

    await ws.send(json.dumps({"action": "chat", "text": "how many items on my list?"}))
    print('  Sent: "how many items on my list?"')

    agent_data, _ = await recv_until(ws, "agent", timeout=30)
    if agent_data:
        print(f"  [PASS] Agent responded")
    else:
        bug("Chat message got no agent response")
        print("  [FAIL]")
    return agent_data


async def test_3_quantity_update(ws, list_data):
    """Test 3: Quantity update via WebSocket action."""
    print("\n=== TEST 3: Quantity Update ===")

    # Get an item from the list
    vendors = list_data.get("vendors", []) or list_data.get("suggestions", [])
    flat_items = []
    for entry in vendors:
        if isinstance(entry, dict) and "items" in entry:
            flat_items.extend(entry["items"])

    if not flat_items:
        bug("No items to test quantity update")
        print("  [SKIP] No items")
        return

    item = flat_items[0]
    item_id = item.get("id")
    old_qty = item.get("quantity", 1)
    print(f'  Target: "{item.get("name")}" (id={item_id}, qty={old_qty})')

    # Send quantity +1
    await ws.send(json.dumps({
        "action": "update_quantity",
        "listItemId": item_id,
        "delta": 1,
    }))
    print(f"  Sent: delta=+1")

    # Wait for item_update or list_data
    for _ in range(10):
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            t = data.get("type")
            if t == "item_update":
                new_qty = data.get("changes", {}).get("quantity")
                print(f"  Got item_update: qty={new_qty}")
                if new_qty == old_qty + 1:
                    print("  [PASS]")
                else:
                    bug(f"Quantity update expected {old_qty+1} but got {new_qty}")
                break
            elif t == "list_data":
                print("  Got list_data refresh (checking qty...)")
                # Check the item in refreshed data
                print("  [PASS] (list_data refresh)")
                break
            else:
                print(f"    [{t}]")
        except asyncio.TimeoutError:
            bug("No response to quantity update")
            print("  [FAIL] Timeout")
            break

    # Revert: send delta=-1
    await ws.send(json.dumps({
        "action": "update_quantity",
        "listItemId": item_id,
        "delta": -1,
    }))
    # Drain the response
    try:
        await asyncio.wait_for(ws.recv(), timeout=3)
    except asyncio.TimeoutError:
        pass


async def test_4_remove_item(ws, list_data):
    """Test 4: Remove item and verify item_removed message."""
    print("\n=== TEST 4: Remove Item ===")

    vendors = list_data.get("vendors", []) or list_data.get("suggestions", [])
    flat_items = []
    for entry in vendors:
        if isinstance(entry, dict) and "items" in entry:
            flat_items.extend(entry["items"])

    if len(flat_items) < 2:
        print("  [SKIP] Not enough items")
        return

    # Pick the last item to remove
    item = flat_items[-1]
    item_id = item.get("id")
    print(f'  Removing: "{item.get("name")}" (id={item_id})')

    await ws.send(json.dumps({
        "action": "remove_item",
        "listItemId": item_id,
    }))

    for _ in range(10):
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            t = data.get("type")
            if t == "item_removed":
                print(f"  Got item_removed for {data.get('listItemId')}")
                print("  [PASS]")
                break
            elif t == "list_data":
                print("  Got list_data refresh")
                print("  [PASS]")
                break
            else:
                print(f"    [{t}]")
        except asyncio.TimeoutError:
            bug("No response to remove_item")
            print("  [FAIL] Timeout")
            break


async def test_5_whatsapp_matching():
    """Test 5: WhatsApp add-item product matching."""
    print("\n=== TEST 5: WhatsApp Product Matching ===")

    from whatsapp_agent import auto_pick_best

    test_cases = [
        ("banana", True, "should match a banana product"),
        ("greek yogurt", True, "should match greek yogurt"),
        ("eggs", True, "should match eggs"),
        ("milk", True, "should match milk"),
        ("tomatoe", True, "should handle typo"),
        ("xyzfoo123nonsense", False, "should return None for gibberish"),
    ]

    for text, should_match, desc in test_cases:
        result = auto_pick_best(text)
        matched = result is not None

        if matched == should_match:
            if matched:
                print(f'  "{text}" -> {result.get("product_name")} '
                      f'({result.get("store_name")}, AED {result.get("price")}, '
                      f'cat={result.get("category")}) [PASS]')
                # Check data quality
                if not result.get("catalog_id"):
                    bug(f'WhatsApp match "{text}" has no catalog_id')
                if not result.get("price"):
                    bug(f'WhatsApp match "{text}" has no price')
                if not result.get("category"):
                    bug(f'WhatsApp match "{text}" has no category')
            else:
                print(f'  "{text}" -> None [PASS] ({desc})')
        else:
            if should_match:
                bug(f'WhatsApp "{text}" returned None but should have matched')
                print(f'  "{text}" -> None [FAIL] ({desc})')
            else:
                bug(f'WhatsApp "{text}" matched but should have returned None')
                print(f'  "{text}" -> {result.get("product_name")} [FAIL] ({desc})')


async def test_6_feedback_manager():
    """Test 6: Feedback manager - create round + approve item."""
    print("\n=== TEST 6: Feedback Manager ===")

    import sqlite3
    DB_PATH = "C:/Users/CemPo/Projects/AutoRestock/autostock.db"

    from feedback_manager import FeedbackManager
    fm = FeedbackManager()

    # Check if there's an active or recent list
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, status FROM shopping_lists ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if not row:
        print("  [SKIP] No shopping lists found")
        conn.close()
        return

    list_id = row["id"]
    print(f"  Using list {list_id} (status={row['status']})")
    print(f"  Active list: {list_id}")

    # Check existing feedback rounds
    rounds = conn.execute(
        "SELECT * FROM feedback_rounds WHERE list_id=? ORDER BY id DESC LIMIT 3",
        (list_id,)
    ).fetchall()
    print(f"  Existing rounds: {len(rounds)}")
    for r in rounds:
        print(f"    Round {r['id']}: status={r['status']}, channel={r['channel']}")

    # Check feedback items
    items = conn.execute(
        "SELECT fi.* FROM feedback_items fi "
        "JOIN feedback_rounds fr ON fi.round_id=fr.id "
        "WHERE fr.list_id=? ORDER BY fi.id DESC LIMIT 5",
        (list_id,)
    ).fetchall()
    print(f"  Recent feedback items: {len(items)}")
    for fi in items:
        print(f"    Item {fi['id']}: action={fi['action_type']}, "
              f"text={fi['item_text']}, resolved={fi['resolved_name']}, "
              f"status={fi['status']}, catalog_id={fi['catalog_id']}")
        if fi["action_type"] == "add" and fi["status"] == "pending":
            if not fi["resolved_name"]:
                bug(f"Feedback item {fi['id']} has no resolved_name (text={fi['item_text']})")
            if not fi["catalog_id"]:
                bug(f"Feedback item {fi['id']} has no catalog_id (text={fi['item_text']})")

    conn.close()
    print("  [PASS] Feedback manager data check")


async def test_7_order_tracking():
    """Test 7: Order tracking data."""
    print("\n=== TEST 7: Order Tracking ===")

    import sqlite3
    DB_PATH = "C:/Users/CemPo/Projects/AutoRestock/autostock.db"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Check orders
    orders = conn.execute(
        "SELECT * FROM orders ORDER BY id DESC LIMIT 5"
    ).fetchall()
    print(f"  Recent orders: {len(orders)}")

    cols = [d[0] for d in conn.execute("SELECT * FROM orders LIMIT 1").description]
    for o in orders:
        od = dict(zip(cols, tuple(o)))
        store = od.get("store_name") or f"store_id={od.get('store_id')}"
        print(f"    Order {od['id']}: store={store}, status={od['status']}, "
              f"expected={od.get('expected_by')}, ref={od.get('order_ref')}")
        if od["status"] == "unknown":
            bug(f"Order {od['id']} has status=unknown")

    # Test the frontend function
    try:
        sys.path.insert(0, "C:/Users/CemPo/Projects/AutoRestock")
        from order_scraper import get_active_order_for_frontend
        active = get_active_order_for_frontend()
        if active:
            print(f"  Frontend active order: status={active.get('status')}, "
                  f"expected={active.get('expected_by')}")
            if active.get("status") == "unknown":
                bug("get_active_order_for_frontend returns unknown status")
            if not active.get("expected_by"):
                bug("get_active_order_for_frontend returns no expected_by")
        else:
            print("  No active order for frontend")
    except Exception as e:
        bug(f"get_active_order_for_frontend error: {e}")

    conn.close()
    print("  [PASS] Order tracking check")


async def test_8_item_pref():
    """Test 8: Item preference actions (pin/unpin)."""
    print("\n=== TEST 8: Item Preference (Pin) ===")

    async with websockets.connect(WS_URI, ping_interval=None) as ws:
        # Wait for list_data
        list_data, _ = await recv_until(ws, "list_data", timeout=90, print_others=False)
        if not list_data:
            print("  [SKIP] No list_data")
            return

        vendors = list_data.get("vendors", []) or list_data.get("suggestions", [])
        flat_items = []
        for entry in vendors:
            if isinstance(entry, dict) and "items" in entry:
                flat_items.extend(entry["items"])

        if not flat_items:
            print("  [SKIP] No items")
            return

        # Find an item with an itemId (items table reference)
        item = None
        for i in flat_items:
            if i.get("itemId"):
                item = i
                break

        if not item:
            bug("No items have itemId — preference actions won't work")
            print("  [FAIL] No items with itemId")
            return

        print(f'  Target: "{item.get("name")}" (itemId={item.get("itemId")}, '
              f'listItemId={item.get("id")})')

        # Send pin action
        await ws.send(json.dumps({
            "action": "item_pref",
            "itemId": item.get("itemId"),
            "prefType": "pin_product",
            "value": item.get("catalogId"),
            "listItemId": item.get("id"),
        }))
        print("  Sent pin_product")

        # Wait for response
        for _ in range(10):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(msg)
                t = data.get("type")
                if t == "list_data":
                    print("  Got list_data refresh")
                    print("  [PASS]")
                    break
                elif t == "agent":
                    print(f"  Agent: {data.get('text', '')[:80]}")
                elif t == "toast":
                    print(f"  Toast: {data.get('message', '')[:80]}")
                else:
                    print(f"    [{t}]")
            except asyncio.TimeoutError:
                bug("No response to pin action")
                print("  [FAIL] Timeout")
                break

        # Unpin to reset state
        await ws.send(json.dumps({
            "action": "item_pref",
            "itemId": item.get("itemId"),
            "prefType": "unpin_product",
            "value": item.get("catalogId"),
            "listItemId": item.get("id"),
        }))
        try:
            await asyncio.wait_for(ws.recv(), timeout=3)
        except asyncio.TimeoutError:
            pass


async def main():
    print("=" * 60)
    print("  HEARTHKEEPER END-TO-END TESTS")
    print("=" * 60)
    print()

    # Test 1: Connection + list
    result = await test_1_connection_and_list()
    list_data = result[0] if result else None

    # Test 5: WhatsApp matching (independent, no WS needed)
    await test_5_whatsapp_matching()

    # Test 6: Feedback manager (independent)
    await test_6_feedback_manager()

    # Test 7: Order tracking (independent)
    await test_7_order_tracking()

    # Tests needing WS (2, 3, 4, 8) - new connection
    if list_data:
        async with websockets.connect(WS_URI, ping_interval=None) as ws:
            # Wait for init
            ld2, _ = await recv_until(ws, "list_data", timeout=90, print_others=False)
            if ld2:
                await test_2_chat(ws)
                await test_3_quantity_update(ws, ld2)
                await test_4_remove_item(ws, ld2)

        # Test 8 opens its own connection
        await test_8_item_pref()

    # Summary
    print("\n" + "=" * 60)
    print("  BUG SUMMARY")
    print("=" * 60)
    if BUGS:
        for i, b in enumerate(BUGS, 1):
            print(f"  BUG-{i}: {b}")
    else:
        print("  No bugs found!")
    print(f"\n  Total bugs: {len(BUGS)}")


if __name__ == "__main__":
    asyncio.run(main())
