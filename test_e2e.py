"""
End-to-end test: Two cart creation flows with vendor builds.
Test 1: Suggested tab -> Ready Suggested -> Build
Test 2: Chat-driven cart -> Build
"""
import asyncio
import json
import websockets
import sys
import sqlite3

DB_PATH = "autostock.db"

def cleanup_lists():
    """Mark any active lists as done so we get a fresh start."""
    conn = sqlite3.connect(DB_PATH)
    changed = conn.execute(
        "UPDATE shopping_lists SET status = 'done' WHERE status IN ('draft', 'confirmed')"
    ).rowcount
    conn.commit()
    conn.close()
    if changed:
        print(f"  Cleaned up {changed} stale lists")


async def collect_messages(ws, timeout=300):
    """Collect all WS messages until cart_complete or timeout."""
    messages = []
    try:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(msg)
            messages.append(data)
            msg_type = data.get("type", "")

            if msg_type == "status":
                print(f"  Status: {data.get('text', '')[:100]}")
            elif msg_type == "state_change":
                state = data.get("state", "")
                print(f"  State -> {state}")
                if state == "idle" and any(m.get("type") == "cart_complete" for m in messages):
                    break
            elif msg_type == "cart_progress":
                step = data.get("step", "")
                detail = {k: v for k, v in data.items() if k not in ("type", "step")}
                print(f"  Progress: {step} {json.dumps(detail)[:120]}")
            elif msg_type == "cart_complete":
                success = data.get("success", False)
                message = data.get("message", "")
                tag = "SUCCESS" if success else "FAILED"
                print(f"  Cart Complete [{tag}]: {message[:150]}")
            elif msg_type == "chat_response":
                print(f"  Chat: {data.get('text', '')[:120]}")
            elif msg_type == "list_data":
                vendors = data.get("vendors", [])
                item_count = sum(
                    len(item)
                    for v in vendors
                    for cat in v.get("categories", [])
                    for item in [cat.get("items", [])]
                )
                print(f"  List data: {item_count} items")
            elif msg_type == "toast":
                print(f"  Toast: {data.get('text', '')}")
            elif msg_type == "view_switch":
                print(f"  View switch -> {data.get('view', '')}")
    except asyncio.TimeoutError:
        print(f"  TIMEOUT after {timeout}s")
    return messages


async def test_suggested_build():
    """Test 1: Connect, receive suggestions, trigger Ready Suggested build."""
    print("=" * 60)
    print("TEST 1: Suggested Tab -> Ready Suggested -> Build")
    print("=" * 60)

    uri = "ws://localhost:8000/ws"
    async with websockets.connect(uri) as ws:
        # Phase 1: Get suggestions
        print("\n[Phase 1] Receiving suggestions...")
        suggestion_list_id = None
        items = []

        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=60)
            data = json.loads(msg)

            if data.get("type") == "list_data":
                suggestion_list_id = data.get("suggestionListId") or data.get("listId")
                for v in data.get("vendors", []):
                    store = v.get("name", "")
                    for cat in v.get("categories", []):
                        for item in cat.get("items", []):
                            items.append(f"{item.get('name','')} ({store})")
                            status = item.get("status", "")
                            flag = f" [{status}]" if status != "matched" else ""
                            print(f"    {item.get('name','')} ({store}){flag}")
                break
            elif data.get("type") == "status":
                print(f"  Status: {data.get('text','')[:80]}")
            elif data.get("type") == "chat_response":
                print(f"  Chat: {data.get('text','')[:80]}")

        print(f"\n  List ID: {suggestion_list_id}, Items: {len(items)}")

        if not suggestion_list_id:
            print("  ERROR: No suggestion list")
            return None

        # Phase 2: Trigger build
        print("\n[Phase 2] Sending 'Ready Suggested'...")
        await ws.send(json.dumps({
            "type": "action",
            "action": "order_action",
            "command": "ready",
            "view": "suggestions",
            "listId": suggestion_list_id
        }))

        # Phase 3: Collect build results
        print("\n[Phase 3] Building carts at vendors...")
        messages = await collect_messages(ws, timeout=300)

        # Extract results
        complete_msg = next((m for m in messages if m.get("type") == "cart_complete"), None)
        if complete_msg:
            return {
                "success": complete_msg.get("success", False),
                "message": complete_msg.get("message", ""),
                "items": len(items),
            }
        return {"success": False, "message": "No cart_complete received", "items": len(items)}


async def test_chat_build():
    """Test 2: Connect, send chat message to create cart, then build."""
    print("\n" + "=" * 60)
    print("TEST 2: Chat-driven Cart -> Build")
    print("=" * 60)

    # Clean up lists from test 1
    cleanup_lists()

    uri = "ws://localhost:8000/ws"
    async with websockets.connect(uri) as ws:
        # Phase 1: Get initial suggestions (wait for list_data)
        print("\n[Phase 1] Connecting and waiting for initial state...")
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=60)
            data = json.loads(msg)
            if data.get("type") == "list_data":
                sid = data.get("suggestionListId") or data.get("listId")
                print(f"  Got initial suggestions (list {sid})")
                break
            elif data.get("type") in ("status", "chat_response"):
                print(f"  {data.get('type')}: {data.get('text','')[:80]}")

        # Phase 2: Send chat message to add items
        print("\n[Phase 2] Sending chat: 'I need eggs, milk, and bananas from Kibsons'...")
        await ws.send(json.dumps({
            "text": "I need eggs, milk, and bananas from Kibsons"
        }))

        # Wait for cart creation and response
        cart_list_id = None
        chat_items = []
        print("  Waiting for agent response...")
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=120)
                data = json.loads(msg)
                msg_type = data.get("type", "")

                if msg_type == "chat_response":
                    print(f"  Agent: {data.get('text','')[:150]}")
                elif msg_type == "list_data":
                    cart_list_id = data.get("cartListId")
                    if cart_list_id:
                        for v in data.get("cartVendors", data.get("vendors", [])):
                            store = v.get("name", "")
                            for cat in v.get("categories", []):
                                for item in cat.get("items", []):
                                    chat_items.append(f"{item.get('name','')} ({store})")
                        print(f"  Cart created: list {cart_list_id}, {len(chat_items)} items")
                        for it in chat_items:
                            print(f"    {it}")
                elif msg_type == "toast":
                    print(f"  Toast: {data.get('text', '')}")
                elif msg_type == "view_switch":
                    print(f"  View switch -> {data.get('view', '')}")
                elif msg_type == "status":
                    text = data.get("text", "")
                    print(f"  Status: {text[:80]}")
                    # If status shows agent is done thinking, break
                    if "still working" not in text.lower():
                        pass

                # Break when we have a cart with items and chat response
                if cart_list_id and chat_items:
                    # Wait a bit more for any final messages
                    try:
                        extra = await asyncio.wait_for(ws.recv(), timeout=5)
                        extra_data = json.loads(extra)
                        if extra_data.get("type") == "chat_response":
                            print(f"  Agent: {extra_data.get('text','')[:150]}")
                    except asyncio.TimeoutError:
                        pass
                    break
            except asyncio.TimeoutError:
                print("  TIMEOUT waiting for agent response")
                break

        if not cart_list_id:
            print("  ERROR: No cart created")
            return {"success": False, "message": "No cart created", "items": 0}

        # Phase 3: Trigger build for cart
        print(f"\n[Phase 3] Sending 'Ready Custom' for cart {cart_list_id}...")
        await ws.send(json.dumps({
            "type": "action",
            "action": "order_action",
            "command": "ready",
            "view": "cart",
            "listId": cart_list_id
        }))

        # Phase 4: Collect build results
        print("\n[Phase 4] Building carts at vendors...")
        messages = await collect_messages(ws, timeout=300)

        complete_msg = next((m for m in messages if m.get("type") == "cart_complete"), None)
        if complete_msg:
            return {
                "success": complete_msg.get("success", False),
                "message": complete_msg.get("message", ""),
                "items": len(chat_items),
            }
        return {"success": False, "message": "No cart_complete received", "items": len(chat_items)}


async def main():
    # Clean state before tests
    print("Cleaning up DB state...")
    cleanup_lists()

    # Test 1
    result1 = await test_suggested_build()
    print(f"\nTest 1 result: {result1}")

    # Test 2
    result2 = await test_chat_build()
    print(f"\nTest 2 result: {result2}")

    # Summary
    print("\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    if result1:
        tag1 = "PASS" if result1.get("success") else "FAIL"
        print(f"  Test 1 (Suggested): [{tag1}] {result1.get('message','')[:100]}")
    if result2:
        tag2 = "PASS" if result2.get("success") else "FAIL"
        print(f"  Test 2 (Chat):      [{tag2}] {result2.get('message','')[:100]}")


if __name__ == "__main__":
    asyncio.run(main())
