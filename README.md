# Hearthkeeper — an autonomous grocery‑restock agent

> A self‑hosted agent that learns a household's grocery habits, builds the weekly
> shop across multiple UAE vendors, and lets the family steer it from their phone.
> Runs unattended on a home server.
>
> **This is a sanitized public mirror.** Personal data (order history, addresses,
> phone numbers) and live credentials are removed; the engineering is intact.

---

## What it does

Most "smart shopping list" apps are glorified checklists. Hearthkeeper tries to be
the thing a good household manager actually is: it **predicts** what you're about
to run out of, **picks the right product** at the right vendor for the right price,
**asks the family** when it's unsure, and **gets better every week** from what they
actually buy and say.

- **Predicts** restock timing per item from consumption history (not fixed dates).
- **Curates** each item to a real product across **Carrefour UAE, Kibsons & Amazon**
  — matching by quality, brand, size, origin and price, not just name.
- **Builds the cart** via resilient browser automation, handling out‑of‑stock,
  cross‑vendor fallback and delivery‑fee thresholds.
- **Talks to the family** over WhatsApp and an in‑app chat: skip, swap, "have it",
  "never this brand" — all of which feed the learning loop.
- **Learns** durable preferences (brand / organic / grass‑fed / size / price band)
  and converges on what the household genuinely accepts.

## Engineering highlights

| Area | What's interesting |
|---|---|
| **Preference‑learning engine** | A signal pipeline (`emit_signal` → `preference_state`) turns every action — an order, a swap, a skip, a WhatsApp edit — into durable, confidence‑weighted preferences across timing / brand / product / store / attribute / inclusion dimensions. One unified read model the scheduled‑list builder and the chat curator both honor. |
| **Price intelligence** | Learns each item's **accepted price band** per *type* and *size* from real purchase history, models **what drives price** (organic premium, country‑of‑origin, brand), and flags bad data / fake deals with a plausibility check. "2 vs 9 AED an egg" is judged against what the household actually pays. |
| **X‑vs‑Y comparison learning** | When a user swaps product X for Y, it diffs the two across a small fixed set of grocery variables (brand / organic / price) and learns *why* — the cleanest preference signal there is. |
| **Multi‑vendor browser automation** | Playwright + undetected‑chrome drivers that survive bot detection, step‑up auth, residential proxies and flaky carts; every attempt is persisted for observability. |
| **Matcher correctness** | A layered gate (category → form‑modifier → required‑noun → foreign‑produce‑noun → ml‑volume) that stops "Banana" matching banana *shallots*, banana *milk* drinks or baby‑food purées. |
| **Self‑healing ops** | GitHub‑webhook deploy to a home server, a scheduler liveness heartbeat + watchdog self‑heal, a subsystem health ledger, and a graceful WhatsApp sidecar with a circuit breaker. |
| **Safety‑first agent design** | A fail‑closed recipient allow‑list at the WhatsApp send chokepoint, session‑HMAC auth, origin‑validated WebSockets, and a hard LLM budget cap. |

## Architecture

```
        WhatsApp  ─┐                          ┌─ Carrefour ─┐
   In‑app chat ────┤   FastAPI + WebSocket    │   Kibsons   │  Playwright /
   React (Vite) ───┤   (single Python app) ───┤   Amazon    │  undetected‑chrome
                   │                          └─────────────┘
                   ▼
        ┌─────────────────────────────────────────────┐
        │  Preference engine · Price intelligence ·    │
        │  List validator · Curator · Cart builder     │
        └─────────────────────────────────────────────┘
                   ▼
              SQLite  (consumption model, orders, learned prefs)
```

Single FastAPI process serves the React frontend, the WebSocket API and the agent;
SQLite holds the consumption model, order history and learned preferences; a
Node WhatsApp sidecar bridges the family chat. Deployed via a GitHub webhook that
pulls + rebuilds + restarts on a home server behind a Cloudflare Tunnel.

## Tech stack

**Backend** Python · FastAPI · SQLite · Playwright / Selenium (undetected‑chrome) ·
Anthropic API (bounded LLM use)
**Frontend** React · Vite · Tailwind CSS
**Ops** GitHub‑webhook CD · NSSM (Windows service) · Cloudflare Tunnel · a Node
WhatsApp sidecar

## A note on scope

This was built as a real system for one household, iterated over ~190 commits with
an end‑to‑end smoke suite (570+ checks) gating each deploy. The public mirror keeps
the code and architecture; the household's data, credentials and live endpoints are
intentionally stripped.
