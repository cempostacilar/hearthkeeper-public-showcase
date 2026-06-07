"""LLM evaluator: does country of origin help a shopper decide
about THIS item?

Replaces hand-curated rule tables with a one-shot per-item LLM verdict,
cached in `items.origin_signal_llm`. The formatter prefers this verdict
when present, falling back to the rule tables when NULL.

Cost: a single classification call per item, ~150 tokens prompt +
~80 tokens response, using claude-3-5-haiku. At Haiku pricing
($0.80 input / $4 output per 1M tokens) this is roughly $0.0004 per
item -- ~$0.01 to evaluate a typical 25-item household catalog. Cached
forever once written.

Triggers for re-evaluation (future, not yet wired):
  - items.category changes (e.g. moved Produce -> Pantry)
  - items.notes substantially changes
  - manual force via /api/admin/evaluate-origin-relevance?force=1
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Optional

import anthropic

from config import ANTHROPIC_API_KEY
from db_setup import DB_PATH


# Cheap classification model. Haiku is plenty for a single-bit answer
# with a one-sentence reason; using Sonnet would be wasteful.
_LLM_MODEL = "claude-haiku-4-5"

_PROMPT = """You are evaluating one grocery item to decide whether \
displaying its country of origin helps a shopper make a meaningful \
decision. The shopper is in the UAE, shops Carrefour UAE and Kibsons, \
and reviews a weekly order list on WhatsApp.

Principle: show origin when origin REVEALS SOMETHING about the product \
the shopper would act on -- provenance, farming practice, freshness, \
terroir, ethical sourcing. Hide it when the product is processed or \
manufactured and origin is just "where the nearest plant happens to be".

Examples (calibration):
  Watermelon            -> SHOW (variety, ripeness, season differ by country)
  Vinegar (white)       -> HIDE (processed, country is just plant location)
  Olive oil             -> SHOW (terroir matters, even for big labels)
  Pepsi / Coca-Cola     -> HIDE (global brand, identical recipe)
  Beef mince            -> SHOW (Australia vs Brazil matters for many)
  Salt, sugar, flour    -> HIDE (commodity, country is uninformative)
  Cheese                -> SHOW (most cheeses are origin-defined)
  Yogurt (mainstream)   -> SHOW (local vs imported is decision-relevant)
  Honey                 -> SHOW (provenance varies a lot)
  Tea, coffee           -> SHOW (single-origin is a real signal)

Item to evaluate:
  Name:     {name}
  Category: {category}
  Notes:    {notes}

Respond with strict JSON only, no prose:
{{"show_origin": true|false, "reasoning": "one sentence"}}"""


async def _evaluate_one(client: anthropic.AsyncAnthropic, item: dict) -> Optional[dict]:
    prompt = _PROMPT.format(
        name=item.get("name", "?"),
        category=item.get("category") or "Unknown",
        notes=(item.get("notes") or "")[:300],
    )
    try:
        resp = await client.messages.create(
            model=_LLM_MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip markdown fences if Haiku decides to wrap the JSON
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        data = json.loads(text)
        # Record cost so the LLM budget tracker stays consistent
        try:
            from llm_budget import record_call as _record
            _record(
                provider="anthropic",
                model=_LLM_MODEL,
                intent="item_origin_eval",
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            )
        except Exception:
            pass
        return {
            "show_origin": bool(data.get("show_origin")),
            "reasoning": str(data.get("reasoning", ""))[:300],
        }
    except (json.JSONDecodeError, KeyError, IndexError, anthropic.APIError) as e:
        return {"show_origin": None, "reasoning": f"parse/api error: {str(e)[:150]}"}


async def evaluate_items(
    item_ids: Optional[list[int]] = None,
    *,
    force: bool = False,
    only_missing: bool = True,
    limit: int = 50,
) -> dict:
    """Batch-evaluate items.

    Args:
        item_ids: explicit subset to evaluate; None = all items.
        force: re-evaluate even if origin_signal_llm is already set.
        only_missing: when item_ids is None, only pick items where the
            flag is NULL. (When force=True this becomes a no-op.)
        limit: cap how many items get evaluated per run.

    Returns: { evaluated: N, results: [ {item_id, name, show, reasoning} ] }
    """
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not set", "evaluated": 0, "results": []}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Pick the candidate set
    if item_ids:
        placeholders = ",".join("?" * len(item_ids))
        rows = cur.execute(
            f"SELECT id, name, category, notes, origin_signal_llm "
            f"FROM items WHERE id IN ({placeholders})",
            item_ids,
        ).fetchall()
    else:
        where = ""
        if only_missing and not force:
            where = "WHERE origin_signal_llm IS NULL"
        rows = cur.execute(
            f"SELECT id, name, category, notes, origin_signal_llm "
            f"FROM items {where} ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    candidates = []
    for r in rows:
        d = dict(r)
        if d.get("origin_signal_llm") is not None and not force:
            continue
        candidates.append(d)
    candidates = candidates[:limit]

    if not candidates:
        conn.close()
        return {"evaluated": 0, "results": [], "note": "nothing to evaluate"}

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    # Evaluate in parallel; cap concurrency at 5 to be polite + avoid
    # rate limits with a long list.
    sem = asyncio.Semaphore(5)

    async def go(item):
        async with sem:
            verdict = await _evaluate_one(client, item)
            return item, verdict

    pairs = await asyncio.gather(*(go(c) for c in candidates))

    # Persist
    results = []
    for item, verdict in pairs:
        if verdict is None or verdict.get("show_origin") is None:
            results.append({
                "item_id": item["id"],
                "name": item["name"],
                "show": None,
                "reasoning": verdict.get("reasoning", "no verdict") if verdict else "no verdict",
            })
            continue
        show = 1 if verdict["show_origin"] else 0
        cur.execute(
            """UPDATE items
               SET origin_signal_llm = ?,
                   origin_signal_reasoning = ?,
                   origin_signal_at = datetime('now')
               WHERE id = ?""",
            (show, verdict["reasoning"], item["id"]),
        )
        results.append({
            "item_id": item["id"],
            "name": item["name"],
            "show": bool(show),
            "reasoning": verdict["reasoning"],
        })

    conn.commit()
    conn.close()
    return {"evaluated": len(results), "results": results}


def evaluate_items_sync(**kwargs) -> dict:
    """Sync wrapper for use in non-async contexts (admin endpoint, CLI)."""
    return asyncio.run(evaluate_items(**kwargs))
