"""
E2E Round 2: Three test scenarios to stress different flows.
Test 3: Suggested tab build (consistency / repeatability)
Test 4: Chat multi-store request ("cheese and salmon from Carrefour, bread and butter from Kibsons")
Test 5: Chat single-item request ("add olive oil from Kibsons")
"""
import asyncio
import json
import websockets
import sqlite3
import time

DB_PATH = "autostock.db"


def cleanup_lists():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute(
        "UPDATE shopping_lists SET status = 'done' WHERE status IN ('draft', 'confirmed')"
    ).rowcount
    conn.commit()
    conn.close()
    if n:
        print(f"  Cleaned up {n} stale lists")


async def wait_for_list_data(ws, timeout=60):
    """Wait for initial list_data message. Returns (suggestion_list_id, items_list)."""
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
        data = json.loads(msg)
        if data.get("type") == "list_data":
            sid = data.get("suggestionListId") or data.get("listId")
            items = []
            for v in data.get("vendors", []):
                store = v.get("name", "")
                for cat in v.get("categories", []):
                    for item in cat.get("items", []):
                        status = item.get("status", "")
                        flag = f" [{status}]" if status != "matched" else ""
                        items.append(f"{item.get('name','')} ({store}){flag}")
            return sid, items, data
        elif data.get("type") in ("status", "chat_response"):
            print(f"  {data.get('type')}: {data.get('text','')[:80]}")


async def send_ready(ws, view, list_id):
    """Send the Ready action for a given view/list."""
    await ws.send(json.dumps({
        "type": "action",
        "action": "order_action",
        "command": "ready",
        "view": view,
        "listId": list_id
    }))


async def collect_build(ws, timeout=300):
    """Collect build progress until cart_complete. Returns (success, summary, progress_items)."""
    progress = []
    success = False
    summary = ""
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(msg)
            t = data.get("type", "")

            if t == "cart_progress":
                step = data.get("step", "")
                if step == "item_ok":
                    item = data.get("item", "")
                    product = data.get("product", "")
                    price = data.get("price", 0)
                    store = data.get("store", "")
                    progress.append({"item": item, "ok": True, "product": product, "price": price, "store": store})
                    print(f"    OK  {item} -> {product} ({price} AED) [{store}]")
                elif step == "item_fail":
                    item = data.get("item", "")
                    reason = data.get("reason", "")[:80]
                    store = data.get("store", "")
                    progress.append({"item": item, "ok": False, "reason": reason, "store": store})
                    print(f"    FAIL {item}: {reason} [{store}]")
                elif step == "done":
                    added = data.get("total_added", 0)
                    failed = data.get("total_failed", 0)
                    summary = data.get("summary", "")
                    print(f"    Done: {added} added, {failed} failed")
                elif step in ("starting", "login_check", "verify"):
                    msg_text = data.get("message", data.get("status", ""))
                    print(f"    [{step}] {msg_text}")
            elif t == "cart_complete":
                success = data.get("success", False)
                summary = data.get("message", "")
                tag = "SUCCESS" if success else "FAILED"
                print(f"  Cart Complete [{tag}]")
            elif t == "state_change" and data.get("state") == "idle":
                break
            elif t == "list_data":
                pass  # updated list during build
        except asyncio.TimeoutError:
            print("  TIMEOUT waiting for build")
            break
    return success, summary, progress


async def send_chat_and_wait_for_cart(ws, text, timeout=120):
    """Send a chat message and wait for cart creation. Returns (cart_list_id, items)."""
    await ws.send(json.dumps({"text": text}))
    print(f"  Sent: '{text}'")

    cart_list_id = None
    chat_items = []
    agent_responses = []

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            remaining = max(1, deadline - time.time())
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            data = json.loads(msg)
            t = data.get("type", "")

            if t == "chat_response":
                resp = data.get("text", "")
                agent_responses.append(resp)
                print(f"  Agent: {resp[:150]}")
            elif t == "list_data":
                cid = data.get("cartListId")
                if cid:
                    cart_list_id = cid
                    chat_items = []
                    for v in data.get("cartVendors", data.get("vendors", [])):
                        store = v.get("name", "")
                        for cat in v.get("categories", []):
                            for item in cat.get("items", []):
                                chat_items.append(f"{item.get('name','')} ({store})")
                    print(f"  Cart {cid}: {len(chat_items)} items")
                    for it in chat_items:
                        print(f"    {it}")
            elif t == "toast":
                print(f"  Toast: {data.get('text','')}")

            # If we have a cart with items AND got an agent response, we're done
            if cart_list_id and chat_items and agent_responses:
                # Drain any remaining quick messages
                try:
                    while True:
                        extra = await asyncio.wait_for(ws.recv(), timeout=3)
                        edata = json.loads(extra)
                        if edata.get("type") == "chat_response":
                            print(f"  Agent: {edata.get('text','')[:150]}")
                        elif edata.get("type") == "list_data":
                            # Update cart items if we get a newer list_data
                            cid2 = edata.get("cartListId")
                            if cid2:
                                cart_list_id = cid2
                                chat_items = []
                                for v in edata.get("cartVendors", edata.get("vendors", [])):
                                    store = v.get("name", "")
                                    for cat in v.get("categories", []):
                                        for item in cat.get("items", []):
                                            chat_items.append(f"{item.get('name','')} ({store})")
                                print(f"  Updated cart {cid2}: {len(chat_items)} items")
                except asyncio.TimeoutError:
                    pass
                break
        except asyncio.TimeoutError:
            break

    return cart_list_id, chat_items


# ─────────────────────────────────────────
# TEST 3: Suggested tab build (consistency)
# ─────────────────────────────────────────
async def test3_suggested():
    print("=" * 60)
    print("TEST 3: Suggested Tab -> Ready Suggested (consistency)")
    print("=" * 60)

    uri = "ws://localhost:8000/ws"
    async with websockets.connect(uri) as ws:
        sid, items, _ = await wait_for_list_data(ws)
        print(f"  List {sid}: {len(items)} items")
        for it in items:
            print(f"    {it}")

        print(f"\n  Sending Ready Suggested...")
        await send_ready(ws, "suggestions", sid)

        success, summary, progress = await collect_build(ws)
        ok = sum(1 for p in progress if p["ok"])
        fail = sum(1 for p in progress if not p["ok"])
        print(f"\n  RESULT: {ok}/{ok+fail} items | {'PASS' if success else 'FAIL'}")
        if fail:
            print("  Failures:")
            for p in progress:
                if not p["ok"]:
                    print(f"    - {p['item']}: {p['reason']}")
        return {"test": "3-suggested", "success": success, "ok": ok, "fail": fail, "progress": progress}


# ─────────────────────────────────────────
# TEST 4: Chat multi-store
# ─────────────────────────────────────────
async def test4_chat_multistore():
    print("\n" + "=" * 60)
    print("TEST 4: Chat Multi-Store (Carrefour + Kibsons)")
    print("=" * 60)
    cleanup_lists()

    uri = "ws://localhost:8000/ws"
    async with websockets.connect(uri) as ws:
        # Get initial state
        sid, _, _ = await wait_for_list_data(ws)
        print(f"  Initial suggestions: list {sid}")

        # Send multi-store chat
        cart_id, cart_items = await send_chat_and_wait_for_cart(
            ws,
            "I need cheese and salmon from Carrefour, and bread and butter from Kibsons"
        )

        if not cart_id:
            print("  ERROR: No cart created")
            return {"test": "4-multistore", "success": False, "ok": 0, "fail": 0, "error": "no cart"}

        print(f"\n  Building cart {cart_id}...")
        await send_ready(ws, "cart", cart_id)

        success, summary, progress = await collect_build(ws)
        ok = sum(1 for p in progress if p["ok"])
        fail = sum(1 for p in progress if not p["ok"])
        print(f"\n  RESULT: {ok}/{ok+fail} items | {'PASS' if success else 'FAIL'}")
        if fail:
            print("  Failures:")
            for p in progress:
                if not p["ok"]:
                    print(f"    - {p['item']} [{p['store']}]: {p['reason']}")
        return {"test": "4-multistore", "success": success, "ok": ok, "fail": fail, "progress": progress}


# ─────────────────────────────────────────
# TEST 5: Chat single item
# ─────────────────────────────────────────
async def test5_chat_single():
    print("\n" + "=" * 60)
    print("TEST 5: Chat Single Item (olive oil from Kibsons)")
    print("=" * 60)
    cleanup_lists()

    uri = "ws://localhost:8000/ws"
    async with websockets.connect(uri) as ws:
        sid, _, _ = await wait_for_list_data(ws)
        print(f"  Initial suggestions: list {sid}")

        cart_id, cart_items = await send_chat_and_wait_for_cart(
            ws,
            "add olive oil from Kibsons to my cart"
        )

        if not cart_id:
            print("  ERROR: No cart created")
            return {"test": "5-single", "success": False, "ok": 0, "fail": 0, "error": "no cart"}

        print(f"\n  Building cart {cart_id}...")
        await send_ready(ws, "cart", cart_id)

        success, summary, progress = await collect_build(ws)
        ok = sum(1 for p in progress if p["ok"])
        fail = sum(1 for p in progress if not p["ok"])
        print(f"\n  RESULT: {ok}/{ok+fail} items | {'PASS' if success else 'FAIL'}")
        if fail:
            print("  Failures:")
            for p in progress:
                if not p["ok"]:
                    print(f"    - {p['item']} [{p['store']}]: {p['reason']}")
        return {"test": "5-single", "success": success, "ok": ok, "fail": fail, "progress": progress}


async def main():
    print("Cleaning DB state...\n")
    cleanup_lists()

    r3 = await test3_suggested()
    r4 = await test4_chat_multistore()
    r5 = await test5_chat_single()

    print("\n" + "=" * 60)
    print("ROUND 2 SUMMARY")
    print("=" * 60)
    for r in [r3, r4, r5]:
        tag = "PASS" if r["success"] else "FAIL"
        err = f" ({r.get('error','')})" if r.get("error") else ""
        print(f"  {r['test']:20s} [{tag}] {r['ok']}/{r['ok']+r['fail']} items{err}")

    total_ok = sum(r["ok"] for r in [r3, r4, r5])
    total_all = sum(r["ok"] + r["fail"] for r in [r3, r4, r5])
    print(f"\n  TOTAL: {total_ok}/{total_all} items ({100*total_ok/total_all:.0f}%)" if total_all else "")


if __name__ == "__main__":
    asyncio.run(main())
