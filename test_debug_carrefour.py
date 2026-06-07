"""Quick test: just the suggested build to debug Carrefour button failures."""
import asyncio, json, websockets, sqlite3

async def main():
    conn = sqlite3.connect("autostock.db")
    conn.execute("UPDATE shopping_lists SET status='done' WHERE status IN ('draft','confirmed')")
    conn.commit()
    conn.close()

    uri = "ws://localhost:8000/ws"
    async with websockets.connect(uri) as ws:
        # Get suggestions
        sid = None
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if data.get("type") == "list_data":
                sid = data.get("suggestionListId") or data.get("listId")
                items = sum(len(cat.get("items",[])) for v in data.get("vendors",[]) for cat in v.get("categories",[]))
                print(f"Got {items} suggestions (list {sid})")
                break

        # Send ready
        await ws.send(json.dumps({
            "type": "action", "action": "order_action",
            "command": "ready", "view": "suggestions", "listId": sid
        }))
        print("Sent Ready Suggested, watching build...")

        while True:
            try:
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=300))
                t = data.get("type","")
                if t == "cart_progress":
                    step = data.get("step","")
                    if step == "item_ok":
                        print(f"  OK  {data.get('item','')} -> {data.get('product','')} ({data.get('price',0)} AED) [{data.get('store','')}]")
                    elif step == "item_fail":
                        print(f"  FAIL {data.get('item','')}: {data.get('reason','')[:100]} [{data.get('store','')}]")
                    elif step == "done":
                        print(f"  Done: {data.get('total_added',0)} added, {data.get('total_failed',0)} failed")
                    elif step in ("starting", "login_check", "verify"):
                        print(f"  [{step}] {data.get('message', data.get('status',''))}")
                elif t == "cart_complete":
                    tag = "SUCCESS" if data.get("success") else "FAILED"
                    print(f"RESULT: [{tag}]")
                elif t == "state_change" and data.get("state") == "idle":
                    break
            except asyncio.TimeoutError:
                print("TIMEOUT")
                break

asyncio.run(main())
