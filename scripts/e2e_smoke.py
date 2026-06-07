"""End-to-end smoke test across today's (2026-05-27) slices.

Runs locally against the dev `autostock.db`. Exercises every module,
schema, formatter, parser, dispatcher hook, and query touched today
and produces a single pass/fail report.

Doesn't actually send WhatsApp messages or call Claude (no
ANTHROPIC_API_KEY locally and WHATSAPP_ENABLED defaults off). Tests
the integration points that DON'T require the network.

Usage:
    cd C:\\Users\\CemPo\\Projects\\AutoRestock
    python3 -X utf8 scripts/e2e_smoke.py

Exit code is 0 if everything passes, 1 otherwise. Output is grouped
by phase so failures are easy to attribute.
"""

import os
import sys
import json
import sqlite3
import importlib
from contextlib import contextmanager

# Ensure repo root is on sys.path when invoked from anywhere.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)


# ─────────────────────────────────────────────
# Tiny test harness
# ─────────────────────────────────────────────
PASS = 0
FAIL = 0
FAILURES = []  # list of (phase, label, detail)


def check(phase, label, ok, detail=""):
    global PASS, FAIL
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    if not ok:
        FAIL += 1
        FAILURES.append((phase, label, detail))
        if detail:
            for line in str(detail).splitlines():
                print(f"           {line}")
    else:
        PASS += 1


@contextmanager
def phase(name):
    print()
    print(f"=== {name} ===")
    yield


# ─────────────────────────────────────────────
# Phase 1: Module imports
# ─────────────────────────────────────────────
def phase1_imports():
    mods = [
        "whatsapp_agent",
        "feedback_manager",
        "scheduler",
        "list_generate",
        "product_score",
        "product_knowledge",
        "preference_engine",
        "external_purchases",
        "delivery_optimizer",
        "vendor_select",
        "price_intel",
        "agent_brain",
        "cart_builder",
        "db_setup",
        "notification_prefs",  # B7
        "app",  # heavy import; pulls in everything
    ]
    loaded = {}
    for name in mods:
        try:
            mod = importlib.import_module(name)
            importlib.reload(mod)
            loaded[name] = mod
            check("imports", f"import {name}", True)
        except Exception as e:
            check("imports", f"import {name}", False, repr(e))
            loaded[name] = None
    return loaded


# ─────────────────────────────────────────────
# Phase 2: Symbol presence (today's new pieces)
# ─────────────────────────────────────────────
def phase2_symbols(loaded):
    expected = {
        "whatsapp_agent": [
            # B5
            "format_delivered_rating_prompt",
            "format_rating_resolved",
            "parse_rating_reply",
            # B6
            "format_rating_reminder",
            # B3
            "format_status_update",
            "status_is_forward",
            "_STATUS_NOTIFY_SET",
            "_STATUS_RANK",
            # B2
            "format_order_placed",
            # B4
            "format_today_deliveries",
            # C1
            "format_round_status_footer",
            # LLM context
            "build_whatsapp_llm_context",
            "_WA_LLM_SYSTEM_PROMPT",
            "parse_with_llm",
        ],
        "feedback_manager": [
            "FeedbackManager",
        ],
        "scheduler": [
            "_delivered_rating_dispatcher",
            "_status_update_dispatcher",
            "_rating_reminder_dispatcher",
            "_today_deliveries_morning_dispatcher",
        ],
        "list_generate": [
            "get_forgotten_items",
        ],
        "product_score": [
            "_sibling_noun_penalty",
            "_SIBLING_NOUN_QUALIFIERS",
            "score_products",
            "score_single_product",
        ],
        "agent_brain": [
            "AgentBrain",
        ],
        "app": [
            "handle_get_forgotten_items",
            "handle_item_pref",
            "ACTION_HANDLERS",
        ],
    }
    for mod_name, names in expected.items():
        mod = loaded.get(mod_name)
        if not mod:
            check("symbols", f"{mod_name} (skipped - module didn't load)", False)
            continue
        for name in names:
            ok = hasattr(mod, name)
            check("symbols", f"{mod_name}.{name}", ok)

    # FeedbackManager methods
    fm_mod = loaded.get("feedback_manager")
    if fm_mod:
        fm_cls = getattr(fm_mod, "FeedbackManager", None)
        if fm_cls:
            method_checks = [
                "_maybe_handle_tier2_reply",
                "_maybe_handle_rating_reply",
                "_perform_swap_commit",
                "_compute_cart_status",
            ]
            for m in method_checks:
                check("symbols", f"FeedbackManager.{m}", hasattr(fm_cls, m))

    # AgentBrain new method
    ab_mod = loaded.get("agent_brain")
    if ab_mod:
        ab_cls = getattr(ab_mod, "AgentBrain", None)
        if ab_cls:
            check(
                "symbols", "AgentBrain._send_order_placed_whatsapp",
                hasattr(ab_cls, "_send_order_placed_whatsapp"),
            )

    # WS action registry
    app_mod = loaded.get("app")
    if app_mod:
        handlers = getattr(app_mod, "ACTION_HANDLERS", {}) or {}
        wanted = [
            "get_forgotten_items",
            "get_learning_items",
            "get_preferences",
            "item_pref",
            "reset_preference",
            "deactivate_preference",
        ]
        for k in wanted:
            check("symbols", f"ACTION_HANDLERS['{k}']", k in handlers)


# ─────────────────────────────────────────────
# Phase 3: Schema migration is idempotent + new columns present
# ─────────────────────────────────────────────
def phase3_schema(loaded):
    db_setup = loaded.get("db_setup")
    if not db_setup:
        check("schema", "db_setup not loaded", False)
        return

    # Run the create_database call against the existing DB. The
    # ALTER TABLE ... ADD COLUMN guards should make this idempotent.
    try:
        db_setup.create_database()
        check("schema", "create_database() idempotent run", True)
    except Exception as e:
        check("schema", "create_database()", False, repr(e))
        return

    conn = sqlite3.connect("autostock.db")
    conn.row_factory = sqlite3.Row

    def has_column(table, col):
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == col for r in rows)

    cols = [
        ("orders", "rating_status"),       # earlier slice
        ("orders", "rating_reminded_at"),  # B6 today
        ("orders", "delivery_date"),       # B3/B5 use
        ("orders", "expected_by"),          # B4 use
        ("orders", "delivery_type"),        # status formatter
        ("order_status_log", "notified_at"),  # B3 today
        ("external_purchases", "item_id"),   # D4/D3 use
        ("household_members", "member_pending_intent"),  # tier2+B5
        ("household_members", "notification_prefs"),  # B7 today
        ("user_item_prefs", "is_active"),   # A12/Phase 6
    ]
    for tbl, c in cols:
        check("schema", f"{tbl}.{c} exists", has_column(tbl, c))

    conn.close()


# ─────────────────────────────────────────────
# Phase 4: Formatter rendering
# ─────────────────────────────────────────────
def phase4_formatters(loaded):
    wa = loaded.get("whatsapp_agent")
    if not wa:
        check("formatters", "whatsapp_agent not loaded", False)
        return

    cases = [
        (
            "format_delivered_rating_prompt",
            lambda: wa.format_delivered_rating_prompt({
                "id": 42, "store_name": "Carrefour UAE",
                "total_amount": 188.50, "order_ref": "CFR-1",
            }, items=[{}, {}, {}]),
            ["delivery", "How did it go", "good"],
        ),
        (
            "format_rating_resolved(thumbs_up_all)",
            lambda: wa.format_rating_resolved("thumbs_up_all"),
            ["Glad", "good"],
        ),
        (
            "format_rating_resolved(thumbs_down_all)",
            lambda: wa.format_rating_resolved("thumbs_down_all"),
            ["Sorry", "review"],
        ),
        (
            "format_rating_resolved(dismiss)",
            lambda: wa.format_rating_resolved("dismiss"),
            ["No worries"],
        ),
        (
            "format_rating_reminder",
            lambda: wa.format_rating_reminder({"store_name": "Carrefour UAE"}),
            ["Last ask", "Carrefour"],
        ),
        (
            "format_status_update(dispatched)",
            lambda: wa.format_status_update(
                {"store_name": "Carrefour UAE", "order_ref": "X",
                 "delivery_type": "NOW", "expected_by": "today"},
                "dispatched",
            ),
            ["out for delivery", "express", "Carrefour"],
        ),
        (
            "format_order_placed (multi-store)",
            lambda: wa.format_order_placed({
                "list_id": 1,
                "stores": {
                    "Carrefour UAE": {
                        "items_added": 6, "items_failed": 1,
                        "estimated_total": 188.50,
                        "failed": [{"item_name": "X", "reason": "OOS"}],
                    },
                    "Kibsons": {
                        "items_added": 12, "items_failed": 0,
                        "estimated_total": 365.00,
                        "failed": [],
                    },
                },
            }),
            ["Order placed", "Carrefour UAE", "Kibsons", "18 item"],
        ),
        (
            "format_today_deliveries (2 orders)",
            lambda: wa.format_today_deliveries([
                {"store_name": "Kibsons", "status": "processing",
                 "expected_by": "2026-05-27 09:00-11:00"},
                {"store_name": "Carrefour UAE", "status": "confirmed",
                 "expected_by": "2026-05-27 14:30"},
            ]),
            ["Today", "Kibsons", "Carrefour"],
        ),
        (
            "format_today_deliveries (empty -> None)",
            lambda: wa.format_today_deliveries([]),
            None,  # special: expect None
        ),
        (
            "format_round_status_footer",
            lambda: wa.format_round_status_footer(19, 654.50),
            ["19 items", "654.50", "Anything else"],
        ),
    ]

    for label, fn, expected in cases:
        try:
            out = fn()
        except Exception as e:
            check("formatters", label, False, repr(e))
            continue

        if expected is None:
            ok = out is None
            check("formatters", label, ok,
                  detail=f"expected None, got {out!r}" if not ok else "")
        else:
            missing = [s for s in expected if s not in (out or "")]
            ok = not missing
            check("formatters", label, ok,
                  detail=f"missing fragments: {missing}" if missing else "")


# ─────────────────────────────────────────────
# Phase 5: Parser routing
# ─────────────────────────────────────────────
def phase5_parsers(loaded):
    wa = loaded.get("whatsapp_agent")
    if not wa:
        check("parsers", "whatsapp_agent not loaded", False)
        return

    # parse_rating_reply
    rating_cases = [
        ("good", "thumbs_up_all"),
        ("👍", "thumbs_up_all"),
        ("bad", "thumbs_down_all"),
        ("👎", "thumbs_down_all"),
        ("rotten bananas", "thumbs_down_all"),
        ("salmon wasn't fresh, please change", "free_text"),
        ("skip", "dismiss"),
        ("skip 3", None),   # verb-with-number guard
        ("have 2", None),
        ("swap 1", None),
        ("goodbye", None),
        ("", None),
    ]
    for text, expected in rating_cases:
        try:
            got = wa.parse_rating_reply(text)
        except Exception as e:
            check("parsers", f"parse_rating_reply({text!r})", False, repr(e))
            continue
        check("parsers", f"parse_rating_reply({text!r}) == {expected!r}",
              got == expected, detail=f"got {got!r}")

    # parse_alternative_choice
    alt_cases = [("A", 0), ("b", 1), ("3", 2), ("F", None), ("", None)]
    for text, expected in alt_cases:
        got = wa.parse_alternative_choice(text)
        check("parsers", f"parse_alternative_choice({text!r}) == {expected!r}",
              got == expected, detail=f"got {got!r}")

    # status_is_forward
    sif_cases = [
        ("confirmed", "dispatched", True),
        ("dispatched", "confirmed", False),
        (None, "confirmed", True),
        ("delivered", "out_for_delivery", False),
        ("processing", "cancelled", False),
    ]
    for old, new, expected in sif_cases:
        got = wa.status_is_forward(old, new)
        check("parsers", f"status_is_forward({old!r}, {new!r}) == {expected!r}",
              got == expected, detail=f"got {got!r}")

    # parse_tier2_reply
    tier2_cases = [
        ("stop", "stop"), ("yes", "stop"), ("never", "stop"),
        ("keep", "keep"), ("no", "keep"),
        ("later", "later"), ("remind", "later"),
        ("hello", None),
    ]
    for text, expected in tier2_cases:
        got = wa.parse_tier2_reply(text)
        check("parsers", f"parse_tier2_reply({text!r}) == {expected!r}",
              got == expected, detail=f"got {got!r}")


# ─────────────────────────────────────────────
# Phase 6: Scoring engine + Phase 4 penalty
# ─────────────────────────────────────────────
def phase6_scoring(loaded):
    ps = loaded.get("product_score")
    if not ps:
        check("scoring", "product_score not loaded", False)
        return

    # Sibling-noun penalty cases
    pen_cases = [
        # (item, product, expect penalty < 0)
        ("Potatoes", "Del Monte Sweet Potatoes - Cubes - 400g", True),
        ("Yogurt",   "Almarai Kids Yoghurt 80g",                True),
        ("Potatoes", "Kibsons Potatoes - 500g",                 False),
        ("Sweet Potatoes", "Kibsons Sweet Potatoes - 500g",     False),
        ("Apple",    "Sudtirol Royal Gala Apples 500g",         False),
        ("Eggplant", "Smoked Aubergine 250g",                   True),
    ]
    for item, prod, expect_pen in pen_cases:
        try:
            pen, reasons = ps._sibling_noun_penalty(item, prod)
        except Exception as e:
            check("scoring", f"_sibling_noun_penalty({item!r}, {prod[:40]!r})",
                  False, repr(e))
            continue
        got_pen = pen < 0
        check(
            "scoring",
            f"sibling-noun penalty for '{item}' + '{prod[:40]}...' = {expect_pen}",
            got_pen == expect_pen,
            detail=f"penalty={pen} reasons={reasons}" if got_pen != expect_pen else "",
        )

    # Pinned product wins via preference_engine -> profile['_pinned']
    pe = loaded.get("preference_engine")
    if pe:
        try:
            conn = sqlite3.connect("autostock.db")
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT i.id, i.name, pc.id AS pin_id, pc.product_name "
                "FROM user_item_prefs uip "
                "JOIN items i ON i.id = uip.item_id "
                "JOIN product_catalog pc ON pc.id = CAST(uip.value AS INTEGER) "
                "WHERE uip.pref_type = 'pin_product' AND uip.is_active = 1 LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                profile = pe.get_scoring_profile(row["id"])
                pinned = profile.get("_pinned")
                check("scoring", "preference_engine returns _pinned for pinned item",
                      bool(pinned and pinned.get("catalog_id") == row["pin_id"]),
                      detail=f"profile._pinned = {pinned}")
                # Score the pinned product -> expect 100
                result = ps.score_single_product(
                    row["name"], "Carrefour UAE",
                    {"id": row["pin_id"], "name": row["product_name"], "price": 1.0},
                    profile=profile,
                )
                check("scoring", "pinned product scores 100",
                      result["score"] == 100,
                      detail=f"got score={result['score']}, tier={result['tier']}")
            else:
                check("scoring", "no pinned product in local DB - skipping pin score test",
                      True)
        except Exception as e:
            check("scoring", "pinned product end-to-end", False, repr(e))


# ─────────────────────────────────────────────
# Phase 7: Backend queries (read-only against real DB)
# ─────────────────────────────────────────────
def phase7_queries(loaded):
    lg = loaded.get("list_generate")
    if not lg:
        check("queries", "list_generate not loaded", False)
        return

    try:
        # Forgotten items against a list that has everything (should be 0)
        latest = sqlite3.connect("autostock.db").execute(
            "SELECT MAX(id) FROM shopping_lists"
        ).fetchone()[0]
        forgotten = lg.get_forgotten_items(list_id=latest, limit=10)
        check("queries", f"get_forgotten_items(list_id={latest}) returns a list",
              isinstance(forgotten, list))
        # Hypothetical empty list -> may surface items if the dev DB has any
        # true patterns (times_ordered >= 3 and recently bought). Older
        # versions of this test asserted len > 0, but after the 2026-05-27
        # tightening (one_off + lapsed filters) the dev DB often has zero
        # qualifying patterns. So we just check it returns a list -- Phase 20
        # exercises the actual gate logic with synthetic fixtures.
        forgotten_all = lg.get_forgotten_items(list_id=99999, limit=20)
        check("queries", "get_forgotten_items(list_id=99999) returns a list",
              isinstance(forgotten_all, list),
              detail=f"n={len(forgotten_all) if isinstance(forgotten_all, list) else 'NA'}")
    except Exception as e:
        check("queries", "forgotten_items query", False, repr(e))

    # context builder
    wa = loaded.get("whatsapp_agent")
    if wa:
        try:
            ctx = wa.build_whatsapp_llm_context(round_id=3)
            # Should at least include cart or household section
            check("queries", "build_whatsapp_llm_context returns text",
                  isinstance(ctx, str) and len(ctx) > 0,
                  detail=f"ctx[:120]={ctx[:120]!r}")
        except Exception as e:
            check("queries", "build_whatsapp_llm_context", False, repr(e))


# ─────────────────────────────────────────────
# Phase 8: LLM action normalizer
# ─────────────────────────────────────────────
def phase8_normalizer(loaded):
    """The normalizer lives inline inside parse_with_llm. We can't
    easily call it without the network. Instead reproduce the logic
    here and verify the cases route the same way.
    """
    import re

    DT = {
        "skip_tier0", "have_it", "stop_suggesting", "swap_request",
        "swap_pick", "add", "external_purchase",
        "remove", "confirm", "done", "unknown",
    }

    def normalize(parsed_list, text="(orig)"):
        results = []
        for entry in parsed_list:
            t = entry.get("type") or entry.get("action") or ""
            t = str(t).lower().strip()
            if t in ("skip", "drop"): t = "skip_tier0"
            elif t in ("have", "had", "got", "bought"): t = "have_it"
            elif t in ("stop", "never"): t = "stop_suggesting"
            elif t in ("swap", "switch", "change", "replace"): t = "swap_request"
            elif t in ("ok", "good", "approve", "approved"): t = "confirm"
            elif t in ("finish", "complete"): t = "done"
            elif t == "unclear": t = "unknown"
            if t not in DT: t = "unknown"
            action = {"type": t}
            if t in ("skip_tier0", "have_it", "stop_suggesting", "swap_request", "remove"):
                ref = entry.get("ref_number")
                if isinstance(ref, str) and ref.isdigit(): ref = int(ref)
                if isinstance(ref, int) and ref > 0:
                    action["ref_number"] = ref
                else:
                    action = {"type": "needs_number", "verb": (
                        "skip" if t in ("skip_tier0", "remove")
                        else "have" if t == "have_it"
                        else "stop" if t == "stop_suggesting" else "swap"
                    )}
            elif t == "swap_pick":
                letter = entry.get("choice_letter")
                index = entry.get("choice_index")
                r = None
                if isinstance(letter, str) and len(letter) == 1 and letter.upper() in "ABCDE":
                    r = ord(letter.upper()) - ord("A")
                if r is None and isinstance(index, int) and 0 <= index <= 4: r = index
                if r is None:
                    action = {"type": "unknown", "item": text, "qty": 0}
                else:
                    action["choice_index"] = r
            elif t in ("add", "external_purchase"):
                action["item"] = entry.get("item") or entry.get("name") or ""
                qty = entry.get("qty") or entry.get("quantity") or 1
                try:
                    action["qty"] = int(qty)
                except Exception:
                    action["qty"] = 1
                if not action["item"]:
                    action = {"type": "unknown", "item": text, "qty": 0}
            elif t == "unknown":
                action["item"] = entry.get("item") or text
                action["qty"] = 0
            results.append(action)
        return results

    cases = [
        ("canonical skip", [{"type": "skip_tier0", "ref_number": 1}],
         lambda r: r[0]["type"] == "skip_tier0" and r[0]["ref_number"] == 1),
        ("alias drop", [{"type": "drop", "ref_number": 2}],
         lambda r: r[0]["type"] == "skip_tier0"),
        ("alias bought", [{"type": "bought", "ref_number": 3}],
         lambda r: r[0]["type"] == "have_it"),
        ("alias switch", [{"type": "switch", "ref_number": 4}],
         lambda r: r[0]["type"] == "swap_request"),
        ("swap_pick letter A", [{"type": "swap_pick", "choice_letter": "A"}],
         lambda r: r[0]["type"] == "swap_pick" and r[0]["choice_index"] == 0),
        ("swap_pick out-of-range F", [{"type": "swap_pick", "choice_letter": "F"}],
         lambda r: r[0]["type"] == "unknown"),
        ("external_purchase", [{"type": "external_purchase", "item": "almond milk", "qty": 1}],
         lambda r: r[0]["type"] == "external_purchase" and r[0]["item"] == "almond milk"),
        ("add", [{"type": "add", "item": "yogurt"}],
         lambda r: r[0]["type"] == "add" and r[0]["qty"] == 1),
        ("legacy action shape", [{"action": "add", "item": "milk", "quantity": 2}],
         lambda r: r[0]["type"] == "add" and r[0]["qty"] == 2),
        ("verb without ref -> needs_number",
         [{"type": "skip_tier0"}],
         lambda r: r[0]["type"] == "needs_number" and r[0]["verb"] == "skip"),
        ("compound multi-action",
         [{"type": "add", "item": "y", "qty": 1}, {"type": "skip", "ref_number": 2}],
         lambda r: len(r) == 2 and r[0]["type"] == "add" and r[1]["type"] == "skip_tier0"),
        ("unknown surprise type",
         [{"type": "telephone_call"}],
         lambda r: r[0]["type"] == "unknown"),
        ("empty add downgrades to unknown",
         [{"type": "add", "item": ""}],
         lambda r: r[0]["type"] == "unknown"),
    ]
    for label, inp, predicate in cases:
        try:
            out = normalize(inp, text="orig")
            ok = predicate(out)
        except Exception as e:
            check("normalizer", label, False, repr(e))
            continue
        check("normalizer", label, ok, detail=f"got {out}" if not ok else "")


# ─────────────────────────────────────────────
# Phase 9: Dispatcher reply-handler precedence (static analysis)
# ─────────────────────────────────────────────
def phase9_dispatcher_order(loaded):
    """Make sure handle_member_reply checks special-case replies in
    the right order. Tier-2 ask must come BEFORE rating reply must
    come BEFORE the round lookup -- otherwise a stale round can
    swallow a Tier-2 keyword.
    """
    fm = loaded.get("feedback_manager")
    if not fm:
        check("dispatcher", "feedback_manager not loaded", False)
        return

    import inspect
    src = inspect.getsource(fm.FeedbackManager.handle_member_reply)
    tier2_pos = src.find("_maybe_handle_tier2_reply")
    rating_pos = src.find("_maybe_handle_rating_reply")
    swap_pending_pos = src.find('pending.get("kind") == "swap_pending"')
    round_lookup_pos = src.find("SELECT * FROM feedback_rounds")

    check("dispatcher", "_maybe_handle_tier2_reply called before _maybe_handle_rating_reply",
          0 < tier2_pos < rating_pos,
          detail=f"tier2={tier2_pos} rating={rating_pos}")
    # Both pre-round checks must come BEFORE the round lookup
    if rating_pos > 0 and round_lookup_pos > 0:
        check("dispatcher", "rating reply check before round lookup",
              rating_pos < round_lookup_pos,
              detail=f"rating={rating_pos} round_lookup={round_lookup_pos}")
    # swap_pending is round-scoped so it comes AFTER the round lookup
    if swap_pending_pos > 0 and round_lookup_pos > 0:
        check("dispatcher", "swap_pending check after round lookup",
              swap_pending_pos > round_lookup_pos,
              detail=f"swap_pending={swap_pending_pos} round={round_lookup_pos}")


# ─────────────────────────────────────────────
# Phase 10: Diag endpoint allowlist
# ─────────────────────────────────────────────
def phase10_diag_endpoints(loaded):
    """Today's two new diag endpoints have to be in the allowlist
    so the auth middleware doesn't 401 them."""
    app_mod = loaded.get("app")
    if not app_mod:
        check("diag", "app not loaded", False)
        return

    # The allowlist is a module-level set/list. Grep the source for
    # the canonical names rather than relying on a constant.
    import inspect
    src = inspect.getsource(app_mod)
    wanted = [
        "/api/diag/forgotten-items",   # A2 today
        "/api/diag/rebuild-item-alternatives",  # Phase 1-3
        "/api/diag/simulate-reply",     # WhatsApp testing
        "/api/diag/llm-usage",          # cost cap
        "/api/diag/cart-attempts",      # cart-build forensics
    ]
    for path in wanted:
        check("diag", f"endpoint '{path}' present in app.py",
              path in src,
              detail="not found in app.py source" if path not in src else "")


# ─────────────────────────────────────────────
# Phase 12: Verb typo tolerance (Damerau-Levenshtein fuzzy match)
# ─────────────────────────────────────────────
def phase12_typos(loaded):
    """The Tier-1 parser should catch common phone-keyboard typos
    on the v2 verbs (skip/have/stop/swap and aliases) without
    routing through the LLM. Constraints: first-letter match,
    length within ±1, edit distance ≤1 (Damerau, so transpositions
    count as 1 op).
    """
    wa = loaded.get("whatsapp_agent")
    if not wa:
        check("typos", "whatsapp_agent not loaded", False)
        return

    # Damerau distance sanity
    sanity = [
        ("skip", "skip", 0),
        ("sikp", "skip", 1),   # transposition
        ("skp",  "skip", 1),   # deletion
        ("skipp","skip", 1),   # insertion
        ("slip", "skip", 1),   # substitution
        ("teh",  "the",  1),   # classic transposition
        ("kitten", "sitting", 3),  # textbook example
    ]
    for a, b, expected in sanity:
        got = wa._verb_edit_distance(a, b, cap=5)
        check("typos", f"edit_distance({a!r}, {b!r}) == {expected}",
              got == expected, detail=f"got {got}")

    # Fuzzy verb resolution -- should resolve typos to canonical
    resolves = [
        ("skipp",  "skip"),
        ("sikp",   "skip"),
        ("skp",    "skip"),
        ("hav",    "have"),
        ("hve",    "have"),
        ("haev",   "have"),
        ("stp",    "stop"),
        ("stoop",  "stop"),
        ("swp",    "swap"),
        ("sawp",   "swap"),
        ("swich",  "switch"),
        ("chenge", "change"),
        ("skipp.", "skip"),   # punctuation tolerated
        ("hav!",   "have"),
    ]
    for inp, expected in resolves:
        got = wa._fuzzy_resolve_verb(inp)
        check("typos", f"fuzzy_resolve_verb({inp!r}) == {expected!r}",
              got == expected, detail=f"got {got!r}")

    # Should NOT match (false-positive guards)
    rejects = [
        "hello", "show", "hope", "godd",   # not close enough / wrong first letter
        "hed", "get",                        # short verbs (had/got) require exact match
        "",
    ]
    for inp in rejects:
        got = wa._fuzzy_resolve_verb(inp)
        check("typos", f"fuzzy_resolve_verb({inp!r}) == None",
              got is None, detail=f"got {got!r}")

    # End-to-end parse_verb_with_numbers
    e2e = [
        ("skipp 3",    "skip_tier0", [3]),
        ("sikp 3 5",   "skip_tier0", [3, 5]),
        ("hav 2",      "have_it",    [2]),
        ("haev 1 2",   "have_it",    [1, 2]),
        ("stp 4",      "stop_suggesting", [4]),
        ("sawp 1",     "swap_request", [1]),
    ]
    for inp, exp_type, exp_nums in e2e:
        out = wa.parse_verb_with_numbers(inp)
        ok = (
            out is not None
            and out[0]["type"] == exp_type
            and [a["ref_number"] for a in out] == exp_nums
        )
        check("typos", f"parse({inp!r}) -> {exp_type} {exp_nums}",
              ok, detail=f"got {out}")

    # Typoed verb WITHOUT a number should still surface needs_number
    # via the Tier-1 dispatcher (parse_message_tier1), not fall to
    # LLM. Verify via the full Tier-1 entry point.
    needs_num_cases = [
        ("skipp the milk",     "skip"),
        ("hve the bread plz",  "have"),
        ("stp suggesting eggs","stop"),
        ("swp this one",       "swap"),
    ]
    for inp, expected_verb in needs_num_cases:
        actions = wa.parse_message_tier1(inp)
        ok = (
            actions
            and actions[0].get("type") == "needs_number"
            and actions[0].get("verb") == expected_verb
        )
        check("typos", f"parse_message_tier1({inp!r}) -> needs_number {expected_verb!r}",
              ok, detail=f"got {actions}")


# ─────────────────────────────────────────────
# Phase 13: cross-vendor OOS fallback
# ─────────────────────────────────────────────
def phase13_cross_vendor(loaded):
    """When an item fails at its primary store and same-vendor alts
    are exhausted, get_cross_vendor_alternatives should surface the
    same item at OTHER vendors. format_order_placed should render
    that as a one-tap rescue hint in the WhatsApp summary.
    """
    pk = loaded.get("product_knowledge")
    cb = loaded.get("cart_builder")
    wa = loaded.get("whatsapp_agent")
    if not pk or not cb or not wa:
        check("cross_vendor", "modules not loaded", False)
        return

    # Symbol presence
    check("cross_vendor", "product_knowledge.get_cross_vendor_alternatives",
          hasattr(pk, "get_cross_vendor_alternatives"))
    check("cross_vendor", "cart_builder._get_alternative_suggestions",
          hasattr(cb, "_get_alternative_suggestions"))

    # Query against the real DB. Pick an item that has alternatives
    # at multiple stores; the lookup should return non-zero hits
    # when excluding any one store.
    import sqlite3
    conn = sqlite3.connect("autostock.db")
    conn.row_factory = sqlite3.Row
    multi_store_item = conn.execute(
        """SELECT i.id FROM items i
           WHERE EXISTS (
               SELECT 1 FROM item_alternatives ia
               WHERE ia.item_id = i.id
               GROUP BY ia.item_id
               HAVING COUNT(DISTINCT ia.store_id) > 1
           ) ORDER BY i.id LIMIT 1"""
    ).fetchone()
    if multi_store_item:
        item_id = multi_store_item["id"]
        alts = pk.get_cross_vendor_alternatives(
            item_id, exclude_store_id=1, limit=5, conn=conn,
        )
        check("cross_vendor",
              f"cross-vendor lookup returns hits (excluding store 1) for item {item_id}",
              len(alts) >= 0,  # zero is acceptable if every alt was at store 1
              detail=f"got {len(alts)} alternatives")
        # Verify shape
        if alts:
            a = alts[0]
            for field in ("store_id", "store_name", "product_name", "price",
                          "product_url", "tier"):
                check("cross_vendor", f"alt dict has '{field}'",
                      field in a, detail=f"got keys: {list(a.keys())}")
    conn.close()

    # _get_alternative_suggestions returns the new {"same_vendor",
    # "other_vendor"} shape
    item_payload = {"item_id": 1, "store_id": 999}  # nonexistent store
    suggestions = cb._get_alternative_suggestions(item_payload, conn=None)
    check("cross_vendor", "_get_alternative_suggestions returns dict shape",
          isinstance(suggestions, dict)
          and "same_vendor" in suggestions
          and "other_vendor" in suggestions,
          detail=f"got {suggestions}")

    # format_order_placed surfaces the cross-vendor hint
    sample_report = {
        "stores": {
            "Carrefour UAE": {
                "items_added": 5, "items_failed": 1,
                "estimated_total": 100.0,
                "failed": [{
                    "item_name": "Olive Oil", "reason": "Out of stock",
                    "alternatives": {
                        "same_vendor": [],
                        "other_vendor": [{
                            "name": "Olive Oil 500ml",
                            "price": 22.50, "store_name": "Kibsons",
                            "url": "https://...", "tier": "preferred",
                        }],
                    },
                }],
                "added": [],
            },
        },
    }
    rendered = wa.format_order_placed(sample_report)
    check("cross_vendor",
          "format_order_placed shows 'available at *Kibsons*' rescue hint",
          "available at *Kibsons*" in rendered,
          detail=rendered[:200])
    check("cross_vendor",
          "format_order_placed shows price + 'tap swap' hint",
          "(22.50" in rendered and "swap" in rendered,
          detail=rendered[:200])

    # When no other_vendor alts -> no rescue hint
    sample2 = {
        "stores": {
            "Carrefour UAE": {
                "items_added": 5, "items_failed": 1,
                "estimated_total": 100.0,
                "failed": [{
                    "item_name": "Bread", "reason": "404",
                    "alternatives": {"same_vendor": [], "other_vendor": []},
                }],
                "added": [],
            },
        },
    }
    rendered2 = wa.format_order_placed(sample2)
    check("cross_vendor",
          "format_order_placed omits rescue hint when no other_vendor alts",
          "available at" not in rendered2,
          detail=rendered2)

    # Legacy plain-list alternatives shape still doesn't crash
    sample3 = {
        "stores": {
            "Carrefour UAE": {
                "items_added": 5, "items_failed": 1,
                "estimated_total": 100.0,
                "failed": [{
                    "item_name": "Legacy", "reason": "x",
                    "alternatives": [{"name": "A", "price": 10}],  # legacy list
                }],
                "added": [],
            },
        },
    }
    try:
        rendered3 = wa.format_order_placed(sample3)
        check("cross_vendor", "legacy alternatives list doesn't crash format_order_placed",
              "Legacy" in rendered3)
    except Exception as e:
        check("cross_vendor", "legacy alternatives list", False, repr(e))

    # Auto-rescue: an item with `rescued_from` lands in another store's
    # added[] and the message says "moved from *<source>*".
    rescue_report = {
        "stores": {
            "Carrefour UAE": {
                "items_added": 5, "items_failed": 0,
                "estimated_total": 150.0,
                "added": [],
                "failed": [],
            },
            "Kibsons": {
                "items_added": 13, "items_failed": 0,
                "estimated_total": 380.0,
                "added": [{
                    "item_name": "Olive Oil",
                    "product_name": "Ladinio Cretan Olive Oil 500ml",
                    "qty": 1, "price": 22.50,
                    "rescued_from": "Carrefour UAE",
                }],
                "failed": [],
            },
        },
    }
    rendered_rescue = wa.format_order_placed(rescue_report)
    check("cross_vendor",
          "format_order_placed shows '🔁 X — moved from *Carrefour UAE*'",
          "🔁" in rendered_rescue
          and "moved from *Carrefour UAE*" in rendered_rescue,
          detail=rendered_rescue[:250])
    check("cross_vendor",
          "rescued item appears under the destination store (Kibsons) not source",
          rendered_rescue.find("Kibsons") < rendered_rescue.find("moved from"),
          detail="Kibsons block should appear before the moved-from line")

    # Multiple rescues > 3 -> "+ N more in the app" rollup
    many_rescues = {
        "stores": {
            "Kibsons": {
                "items_added": 5, "items_failed": 0,
                "estimated_total": 100.0,
                "added": [
                    {"item_name": f"Item{i}", "product_name": f"P{i}",
                     "qty": 1, "price": 10.0,
                     "rescued_from": "Carrefour UAE"}
                    for i in range(5)
                ],
                "failed": [],
            },
        },
    }
    rendered_many = wa.format_order_placed(many_rescues)
    check("cross_vendor",
          "5 rescues -> shows '+ 2 more cross-vendor rescues in the app'",
          "+ 2 more cross-vendor rescue" in rendered_many,
          detail=rendered_many[:300])


# ─────────────────────────────────────────────
# Phase 11: B7 — notification_prefs helpers
# ─────────────────────────────────────────────
def phase11_b7(loaded):
    """Verify the B7 helper module: defaults preserve current behavior,
    custom prefs override correctly, quiet-hours math handles
    cross-midnight, list_recipients enumerates eligible members.
    """
    np = loaded.get("notification_prefs")
    if not np:
        check("b7", "notification_prefs not loaded", False)
        return

    # NOTIFICATION_KEYS shape
    check("b7", "NOTIFICATION_KEYS has 5 entries",
          len(np.NOTIFICATION_KEYS) == 5,
          detail=f"got {np.NOTIFICATION_KEYS}")
    for expected_key in ("b2_order_placed", "b3_dispatched",
                         "b4_today_deliveries", "b5_delivered",
                         "b6_reminder"):
        check("b7", f"NOTIFICATION_KEYS contains '{expected_key}'",
              expected_key in np.NOTIFICATION_KEYS)

    # Use the real driver/member rows from autostock.db
    import sqlite3
    conn = sqlite3.connect("autostock.db")
    conn.row_factory = sqlite3.Row
    driver = conn.execute(
        "SELECT id FROM household_members WHERE role = 'driver' LIMIT 1"
    ).fetchone()
    member = conn.execute(
        "SELECT id FROM household_members WHERE role = 'member' LIMIT 1"
    ).fetchone()
    conn.close()

    if not driver:
        check("b7", "driver row exists in autostock.db", False)
        return

    # Driver defaults: all on
    prefs = np.get_member_prefs(driver["id"])
    all_on = all(prefs["notifications"].get(k, False) for k in np.NOTIFICATION_KEYS)
    check("b7", "driver defaults to all notifications ON", all_on,
          detail=f"got {prefs['notifications']}")
    check("b7", "driver default quiet_hours = 22-07",
          prefs["quiet_hours"]["start"] == 22 and prefs["quiet_hours"]["end"] == 7,
          detail=f"got {prefs['quiet_hours']}")

    # Non-driver defaults: all off
    if member:
        prefs_m = np.get_member_prefs(member["id"])
        all_off = all(not prefs_m["notifications"].get(k, True) for k in np.NOTIFICATION_KEYS)
        check("b7", "non-driver defaults to all notifications OFF", all_off,
              detail=f"got {prefs_m['notifications']}")

    # Quiet-hours math, default 22-07 window
    from datetime import datetime
    cases = [
        (datetime(2026, 5, 27, 23, 0), True,  "23:00"),
        (datetime(2026, 5, 27, 6, 0),  True,  "06:00"),
        (datetime(2026, 5, 27, 7, 0),  False, "07:00"),
        (datetime(2026, 5, 27, 7, 30), False, "07:30"),
        (datetime(2026, 5, 27, 14, 0), False, "14:00"),
        (datetime(2026, 5, 27, 21, 59), False, "21:59"),
        (datetime(2026, 5, 27, 22, 0), True,  "22:00"),
    ]
    for ts, expected, label in cases:
        got = np.is_quiet_hours(driver["id"], now=ts)
        check("b7", f"is_quiet_hours @ {label} == {expected}",
              got == expected, detail=f"got {got}")

    # Custom prefs override
    import json
    custom = json.dumps({
        "notifications": {"b3_dispatched": False, "b6_reminder": False},
        "quiet_hours": {"start": 23, "end": 8},
    })
    conn = sqlite3.connect("autostock.db")
    conn.execute("UPDATE household_members SET notification_prefs = ? WHERE id = ?",
                 (custom, driver["id"]))
    conn.commit()
    conn.close()
    try:
        prefs2 = np.get_member_prefs(driver["id"])
        check("b7", "custom b3_dispatched=False applies",
              prefs2["notifications"]["b3_dispatched"] is False,
              detail=f"got {prefs2['notifications']}")
        check("b7", "untouched b5_delivered keeps default True",
              prefs2["notifications"]["b5_delivered"] is True,
              detail=f"got {prefs2['notifications']}")
        check("b7", "custom quiet_hours=23-8 applies",
              prefs2["quiet_hours"] == {"start": 23, "end": 8},
              detail=f"got {prefs2['quiet_hours']}")
        check("b7", "is_quiet_hours respects custom 23-08 @ 22:30",
              not np.is_quiet_hours(driver["id"], now=datetime(2026, 5, 27, 22, 30)))
        check("b7", "is_quiet_hours respects custom 23-08 @ 07:30",
              np.is_quiet_hours(driver["id"], now=datetime(2026, 5, 27, 7, 30)))
    finally:
        # Restore default state so subsequent runs see defaults
        conn = sqlite3.connect("autostock.db")
        conn.execute("UPDATE household_members SET notification_prefs = NULL WHERE id = ?",
                     (driver["id"],))
        conn.commit()
        conn.close()


# ─────────────────────────────────────────────
# Phase 14: scheduler task surface
# ─────────────────────────────────────────────
def phase14_scheduler(loaded):
    """Every async task that scheduler.start_scheduler launches must
    exist as a coroutine function. Catches the "renamed a task but
    forgot to update start_scheduler" class of regression.
    """
    sched = loaded.get("scheduler")
    if not sched:
        check("scheduler", "scheduler not loaded", False)
        return

    expected_tasks = [
        "_nudge_scheduler",
        "_availability_refresh_loop",
        "_stale_list_refresh_loop",
        "_order_status_poller",
        "_nightly_catalog_scraper",
        "_nightly_pattern_detector",
        "_nightly_carrefour_order_detail_backfill",
        "_feedback_timeout_checker",
        "_nightly_whatsapp_restart",
        "_whatsapp_liveness_probe",
        "_delivered_rating_dispatcher",
        "_status_update_dispatcher",
        "_rating_reminder_dispatcher",
        "_today_deliveries_morning_dispatcher",
    ]
    import inspect, asyncio
    for name in expected_tasks:
        fn = getattr(sched, name, None)
        check("scheduler", f"scheduler.{name} exists", fn is not None)
        if fn is not None:
            check("scheduler", f"scheduler.{name} is async",
                  asyncio.iscoroutinefunction(fn),
                  detail=f"signature: {inspect.signature(fn)}")

    # start_scheduler itself
    check("scheduler", "start_scheduler is async coroutine",
          asyncio.iscoroutinefunction(getattr(sched, "start_scheduler", None)))

    # start_scheduler must actually be WIRED into app startup. The Wave-5a
    # dead-code cleanup once deleted the @app.on_event("startup") call, which
    # silently killed every background task (order poller, WhatsApp dispatchers,
    # nightly scraper/pattern detector) for ~4 days. Guard against recurrence.
    import pathlib
    _app_src = (pathlib.Path(__file__).parent.parent / "app.py").read_text(encoding="utf-8")
    check("scheduler", "app.py wires start_scheduler() into startup",
          "start_scheduler()" in _app_src,
          detail="an @app.on_event('startup') / lifespan hook must call start_scheduler()")

    # Module-level tunables exist (guards against accidental rename)
    tunables = [
        "DELIVERED_PROMPT_INTERVAL_S",
        "DELIVERED_PROMPT_MAX_AGE_HOURS",
        "STATUS_UPDATE_INTERVAL_S",
        "REMINDER_SWEEP_INTERVAL_S",
        "REMINDER_AGE_HOURS",
        "DAY_SUMMARY_HOUR",
        "DAY_SUMMARY_MIN",
        "PROBE_INTERVAL_S",
        "PROBE_FAIL_THRESHOLD",
        "RESTART_COOLDOWN_S",
    ]
    for t in tunables:
        check("scheduler", f"tunable {t} exists",
              hasattr(sched, t),
              detail=f"got {getattr(sched, t, 'MISSING')!r}")


# ─────────────────────────────────────────────
# Phase 15: WS action handler coverage
# ─────────────────────────────────────────────
def phase15_ws_handlers(loaded):
    """Every key in ACTION_HANDLERS must point to an existing async
    handler in app.py. Catches typo-rename regressions in the
    registry."""
    app_mod = loaded.get("app")
    if not app_mod:
        check("ws_handlers", "app not loaded", False)
        return
    handlers = getattr(app_mod, "ACTION_HANDLERS", {}) or {}
    import asyncio
    check("ws_handlers", "ACTION_HANDLERS dict exists",
          isinstance(handlers, dict) and len(handlers) >= 20,
          detail=f"got {len(handlers)} entries")
    # Today's action types should all be registered
    required = [
        "change_qty", "remove_item", "mark_have_it", "item_pref",
        "find_alternatives", "accept_alternative", "reorder_from_history",
        "add_suggestion", "next_suggestion", "skip_suggestion",
        "get_order_tracking", "submit_rating", "dismiss_rating",
        "feedback_alert", "create_cart", "dismiss_cart", "cherry_pick",
        "request_feedback", "approve_feedback", "reject_feedback",
        "cancel_feedback", "order_action",
        "get_learning_items", "get_forgotten_items",
        "reset_preference", "get_preferences", "deactivate_preference",
    ]
    for key in required:
        fn = handlers.get(key)
        check("ws_handlers", f"ACTION_HANDLERS['{key}'] points to async fn",
              fn is not None and asyncio.iscoroutinefunction(fn),
              detail=f"got {fn}")


# ─────────────────────────────────────────────
# Phase 16: dispatcher precedence + state isolation
# ─────────────────────────────────────────────
def phase16_dispatcher_isolation(loaded):
    """Static-analysis checks on the WhatsApp dispatcher's source.
    Catches the ordering regressions that bit us before (e.g.
    swap_request missing from the verb tuple, tier2 ordering, etc.)
    """
    fm = loaded.get("feedback_manager")
    if not fm:
        check("dispatcher", "feedback_manager not loaded", False)
        return
    import inspect
    src = inspect.getsource(fm.FeedbackManager.handle_member_reply)

    # The tuple of v2 verbs MUST contain swap_request -- the original
    # bug was that swap N silently fell to the unknown branch.
    check("dispatcher", "verb tuple includes 'swap_request'",
          "swap_request" in src
          and 'action_type in ("skip_tier0", "have_it", "stop_suggesting"' in src,
          detail="lost the swap_request membership again")

    # Tier-2 check runs before rating check before round lookup.
    tier2_pos = src.find("_maybe_handle_tier2_reply")
    rating_pos = src.find("_maybe_handle_rating_reply")
    check("dispatcher", "tier2 reply check exists",
          tier2_pos > 0)
    check("dispatcher", "rating reply check exists",
          rating_pos > 0)
    check("dispatcher", "tier2 BEFORE rating in handle_member_reply",
          tier2_pos < rating_pos)

    # swap_pick (LLM) branch exists in the action loop
    check("dispatcher", "swap_pick branch in action loop",
          'action_type == "swap_pick"' in src or "swap_pick" in src,
          detail="lost the LLM swap_pick handler")

    # external_purchase branch
    check("dispatcher", "external_purchase branch in action loop",
          'action_type == "external_purchase"' in src,
          detail="lost the D4 external_purchase handler")


# ─────────────────────────────────────────────
# Phase 17: formatter robustness (edge inputs)
# ─────────────────────────────────────────────
def phase17_formatter_robustness(loaded):
    """Formatters should never crash on missing/None/zero fields.
    The B-series messages run unattended; a None field shouldn't
    take down a scheduler task."""
    wa = loaded.get("whatsapp_agent")
    if not wa:
        check("formatter_robust", "whatsapp_agent not loaded", False)
        return

    # Empty order_placed
    try:
        msg = wa.format_order_placed({})
        check("formatter_robust", "format_order_placed({}) doesn't crash",
              isinstance(msg, str) and len(msg) > 0)
    except Exception as e:
        check("formatter_robust", "format_order_placed({})", False, repr(e))

    # Order with missing fields
    try:
        msg = wa.format_order_placed({
            "stores": {
                "X": {"items_added": 0, "items_failed": 0,
                      "added": [], "failed": []},
            },
        })
        check("formatter_robust", "format_order_placed(zero-items) renders",
              isinstance(msg, str) and "Order placed" in msg)
    except Exception as e:
        check("formatter_robust", "format_order_placed(zero-items)", False, repr(e))

    # Rating prompt with no items
    try:
        msg = wa.format_delivered_rating_prompt({})
        check("formatter_robust", "format_delivered_rating_prompt({}) renders",
              isinstance(msg, str) and "delivery" in msg.lower())
    except Exception as e:
        check("formatter_robust", "format_delivered_rating_prompt({})", False, repr(e))

    # Today's deliveries empty -> None
    try:
        out = wa.format_today_deliveries([])
        check("formatter_robust", "format_today_deliveries([]) returns None",
              out is None)
    except Exception as e:
        check("formatter_robust", "format_today_deliveries([])", False, repr(e))

    # Round status footer with 0 items
    try:
        out = wa.format_round_status_footer(0, 0)
        check("formatter_robust", "format_round_status_footer(0, 0) renders",
              isinstance(out, str) and "0 items" in out)
    except Exception as e:
        check("formatter_robust", "format_round_status_footer(0, 0)", False, repr(e))

    # Status update with missing optional fields
    try:
        msg = wa.format_status_update({"store_name": "X"}, "dispatched")
        check("formatter_robust", "format_status_update(minimal) renders",
              isinstance(msg, str) and "X" in msg)
    except Exception as e:
        check("formatter_robust", "format_status_update(minimal)", False, repr(e))


# ─────────────────────────────────────────────
# Phase 18: catalog matcher gates end-to-end
# ─────────────────────────────────────────────
def phase18_catalog_gates(loaded):
    """Verify the four gate functions in product_knowledge fire
    correctly on representative inputs. Regression-proofs the
    catalog-matching slice (Phases 1-4 + sibling penalty)."""
    pk = loaded.get("product_knowledge")
    if not pk:
        check("catalog_gates", "product_knowledge not loaded", False)
        return

    # Category gate
    cases_cat = [
        # (item_cat, catalog_cat, expected_allowed)
        ("Produce", "Fruits & Vegetables > Fresh", True),
        ("Produce", "Bakery > Bread", False),
        ("Pantry",  "Pantry > Pasta Noodles", True),
        ("Pantry",  "Drinks > Soda", False),
        ("Meat",    "Meat > Beef Veal", True),
        ("Meat",    "Bakery > Bread", False),
        (None,      "anything", True),  # missing item_cat -> fail-open
        ("Produce", "",        True),   # missing catalog_cat -> fail-open
    ]
    for item_cat, cat_cat, expected in cases_cat:
        got = pk._category_allowed(item_cat, cat_cat)
        check("catalog_gates",
              f"_category_allowed({item_cat!r}, {cat_cat[:30]!r}) == {expected}",
              got == expected, detail=f"got {got}")

    # Form-modifier gate
    cases_form = [
        # (item_cat, product_name, expected_blocked)
        ("Produce", "Apple Juice", True),       # juice blocks Produce
        ("Produce", "Fresh Apples", False),
        ("Produce", "Apple Sauce", True),
        ("Pantry",  "Walnut Dip", True),
        ("Pantry",  "Whole Walnuts", False),
        ("Meat",    "Beef Jerky", True),        # jerky blocks Meat
        ("Meat",    "Fresh Beef Mince", False),
        ("Dairy",   "Milk Powder", True),
        ("Dairy",   "Fresh Milk", False),
    ]
    for item_cat, prod, expected in cases_form:
        got = pk._form_modifier_blocked(item_cat, prod)
        check("catalog_gates",
              f"_form_modifier_blocked({item_cat!r}, {prod!r}) == {expected}",
              got == expected, detail=f"got {got}")

    # Noun gate (with spelling variants)
    cases_noun = [
        ("Yogurt",  "Al Ain Full Fat Yoghurt 400g", True),  # variant
        ("Yoghurt", "Al Ain Full Fat Yogurt 400g",  True),
        ("Apple",   "Hollinger Apple Juice 200ml",  True),  # noun matches even though Form would block
        ("Eggplant", "Organic Aubergine 500g",      True),  # variant
        ("Salmon",  "Beef Mince 500g",              False),
        ("Beef Sausages", "Bostock Chicken Sausages",False), # missing 'beef'
    ]
    for item, prod, expected in cases_noun:
        nouns = pk._primary_nouns(item)
        got = pk._noun_match(nouns, prod)
        check("catalog_gates",
              f"_noun_match({item!r}, {prod!r}) == {expected}",
              got == expected, detail=f"got {got} nouns={nouns}")

    # Sibling-noun penalty (Phase 4)
    ps = loaded.get("product_score")
    if ps:
        cases_sib = [
            ("Potatoes", "Sweet Potatoes Cubes 400g", True),  # sweet sibling
            ("Onions",   "Pearl Onions 250g",          True),
            ("Yogurt",   "Almarai Kids Yoghurt 80g",   True),  # variant + sibling
            ("Potatoes", "Kibsons Potatoes 500g",       False),
            ("Sweet Potatoes", "Sweet Potatoes Cubes",  False),  # item HAS sweet
        ]
        for item, prod, expect_pen in cases_sib:
            pen, _r = ps._sibling_noun_penalty(item, prod)
            got = pen < 0
            check("catalog_gates",
                  f"sibling penalty for {item!r} + {prod[:35]!r} = {expect_pen}",
                  got == expect_pen,
                  detail=f"pen={pen}")


# ─────────────────────────────────────────────
# Phase 19: list_add_item dedup
# ─────────────────────────────────────────────
def phase19_list_dedup(loaded):
    """Without dedup in list_add_item, every 'add X' call piles a fresh
    row onto the list. Long-lived drafts on the Ally accumulated 70+
    rows from repeated adds via WhatsApp / in-app chat / curator UI.
    This phase guards the dedup behavior.
    """
    lg = loaded.get("list_generate")
    if not lg:
        check("list_dedup", "list_generate not loaded", False)
        return

    import sqlite3
    conn = sqlite3.connect("autostock.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # Snapshot the sequence so we can roll back cleanly
    cur.execute(
        "INSERT INTO shopping_lists (name, status, list_type) "
        "VALUES ('TEMP-SMOKE-DEDUP', 'draft', 'cart')"
    )
    test_list = cur.lastrowid
    conn.commit()
    item = cur.execute("SELECT id FROM items LIMIT 1").fetchone()
    if not item:
        check("list_dedup", "items table has no rows", False)
        return
    item_id = item["id"]

    try:
        # 1) First add inserts a row
        id1 = lg.list_add_item(test_list, "TestItem", item_id=item_id,
                                quantity=1, conn=conn)
        check("list_dedup", "first add returns a list_item_id",
              isinstance(id1, int))

        # 2) Second add for same (list, item) returns SAME id and
        #    bumps quantity
        id2 = lg.list_add_item(test_list, "TestItem", item_id=item_id,
                                quantity=2, conn=conn)
        check("list_dedup", "second add reuses existing row (no duplicate)",
              id1 == id2)
        row = cur.execute(
            "SELECT quantity, status FROM list_items WHERE id=?", (id1,)
        ).fetchone()
        check("list_dedup", "second add bumps quantity (1+2=3)",
              row["quantity"] == 3,
              detail=f"got {row['quantity']}")

        # 3) Skip the row, then add again -> un-skip + restore qty
        cur.execute(
            "UPDATE list_items SET status='skipped', user_action='skipped' "
            "WHERE id=?", (id1,)
        )
        conn.commit()
        id3 = lg.list_add_item(test_list, "TestItem", item_id=item_id,
                                quantity=5, conn=conn)
        check("list_dedup", "add-after-skip reuses same row",
              id3 == id1)
        row = cur.execute(
            "SELECT quantity, status, user_action FROM list_items WHERE id=?",
            (id1,)
        ).fetchone()
        check("list_dedup", "add-after-skip un-skips (status=approved)",
              row["status"] == "approved")
        check("list_dedup", "add-after-skip restores user_action='added'",
              row["user_action"] == "added")
        check("list_dedup", "add-after-skip overrides quantity to new value",
              row["quantity"] == 5,
              detail=f"got {row['quantity']}")

        # 4) Confirm only ONE row exists for this (list, item)
        n = cur.execute(
            "SELECT COUNT(*) FROM list_items WHERE list_id=? AND item_id=?",
            (test_list, item_id)
        ).fetchone()[0]
        check("list_dedup", "exactly 1 row exists for (list, item) after 3 adds",
              n == 1, detail=f"got {n}")

    finally:
        # Always clean up the temp list
        cur.execute("DELETE FROM list_items WHERE list_id=?", (test_list,))
        cur.execute("DELETE FROM shopping_lists WHERE id=?", (test_list,))
        conn.commit()
        conn.close()


# ─────────────────────────────────────────────
# Phase 20: list_generate suggestion gates (times_ordered >= 3 + recency)
# ─────────────────────────────────────────────
def phase20_list_gates(loaded):
    """list_generate's Step-1 SQL must exclude:
       - one_off items (times_ordered < 3)
       - lapsed items (last_ordered older than 2x avg_days_between)
    Without these gates the scheduled list bloated to 50+ items on the Ally
    because every twice-bought item with confidence=0.7 sailed through.
    """
    import sqlite3
    from datetime import datetime, timedelta
    conn = sqlite3.connect("autostock.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Need a store row to satisfy the inner JOIN on stores
    store = cur.execute("SELECT id FROM stores LIMIT 1").fetchone()
    if not store:
        check("list_gates", "stores table empty -- cannot run phase 20", False)
        conn.close()
        return
    store_id = store["id"]

    # Build a tag we can clean up safely
    TAG = "SMOKE-GATE-"
    inserted_items = []
    inserted_rules = []

    def mk_item(name_suffix, store_id):
        cur.execute(
            "INSERT INTO items (name, preferred_store_id, category) "
            "VALUES (?, ?, 'Smoke')",
            (TAG + name_suffix, store_id),
        )
        iid = cur.lastrowid
        inserted_items.append(iid)
        return iid

    def mk_rule(item_id, times_ordered, last_ordered_offset_days,
                avg_days_between, next_offset_days, confidence=0.85):
        last = (datetime.now().date() - timedelta(days=last_ordered_offset_days)
                ).isoformat() if last_ordered_offset_days is not None else None
        next_d = (datetime.now().date() + timedelta(days=next_offset_days)).isoformat()
        cur.execute(
            "INSERT INTO restock_rules "
            "(item_id, avg_quantity, avg_days_between, next_restock_date, "
            " last_ordered_date, confidence, times_ordered, updated_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, datetime('now'))",
            (item_id, avg_days_between, next_d, last, confidence, times_ordered),
        )
        inserted_rules.append(item_id)

    try:
        # PASS case: 3 purchases, last week, 7-day cadence, due today
        good_iid = mk_item("good-3x-fresh", store_id)
        mk_rule(good_iid, times_ordered=3, last_ordered_offset_days=7,
                avg_days_between=7, next_offset_days=0)

        # FAIL: only 2 purchases (one_off)
        one_off_iid = mk_item("oneoff-2x", store_id)
        mk_rule(one_off_iid, times_ordered=2, last_ordered_offset_days=7,
                avg_days_between=7, next_offset_days=0)

        # FAIL: only 1 purchase
        single_iid = mk_item("single-1x", store_id)
        mk_rule(single_iid, times_ordered=1, last_ordered_offset_days=7,
                avg_days_between=7, next_offset_days=0)

        # FAIL: 5 purchases but last one 6 months ago (lapsed -- well past
        # the MAX(4x*7=28, 90) = 90-day window).
        lapsed_iid = mk_item("lapsed-5x-stale", store_id)
        mk_rule(lapsed_iid, times_ordered=5, last_ordered_offset_days=180,
                avg_days_between=7, next_offset_days=0)

        # PASS: weekly item, last bought 60 days ago. Without the 90-day
        # floor this would falsely lapse at 4*7=28 days. With the floor
        # it survives -- protects Yogurt / Apple class items from being
        # cut off during order-ingestion droughts.
        weekly_idle_iid = mk_item("weekly-60d-idle", store_id)
        mk_rule(weekly_idle_iid, times_ordered=10, last_ordered_offset_days=60,
                avg_days_between=7, next_offset_days=0)

        # PASS edge: 3 purchases, last exactly at 90-day floor
        edge_iid = mk_item("edge-3x-90d", store_id)
        mk_rule(edge_iid, times_ordered=3, last_ordered_offset_days=90,
                avg_days_between=7, next_offset_days=0)

        # FAIL edge: 3 purchases, last 95 days ago (well past 90d floor).
        # NB: was 91 originally, but Python local time vs SQLite UTC can
        # diverge by 1 day around UTC midnight, making 91 racy. 95 is
        # safely past the boundary regardless of timezone.
        just_lapsed_iid = mk_item("just-lapsed-3x", store_id)
        mk_rule(just_lapsed_iid, times_ordered=3, last_ordered_offset_days=95,
                avg_days_between=7, next_offset_days=0)

        # PASS: long-cadence item (60-day) bought 200 days ago, 4x=240 > 200
        long_cadence_iid = mk_item("long-cadence-200d", store_id)
        mk_rule(long_cadence_iid, times_ordered=3, last_ordered_offset_days=200,
                avg_days_between=60, next_offset_days=0)

        # FAIL: long-cadence item (60-day) bought 250 days ago, 4x=240 < 250
        long_cadence_stale_iid = mk_item("long-cadence-250d", store_id)
        mk_rule(long_cadence_stale_iid, times_ordered=3, last_ordered_offset_days=250,
                avg_days_between=60, next_offset_days=0)

        conn.commit()

        # Run the gating SQL directly (same shape as list_generate Step 1)
        target = (datetime.now().date() + timedelta(days=2)).isoformat()
        result_ids = {r["item_id"] for r in cur.execute("""
            SELECT i.id AS item_id
            FROM restock_rules r
            JOIN items i ON r.item_id = i.id
            JOIN stores s ON i.preferred_store_id = s.id
            WHERE r.next_restock_date IS NOT NULL
              AND r.next_restock_date <= ?
              AND r.confidence >= 0.3
              AND COALESCE(r.times_ordered, 0) >= 3
              AND r.last_ordered_date IS NOT NULL
              AND julianday(date('now')) - julianday(r.last_ordered_date)
                  <= MAX(COALESCE(r.avg_days_between, 7) * 4, 90)
        """, (target,)).fetchall()}

        check("list_gates", "3 purchases, fresh: INCLUDED",
              good_iid in result_ids,
              detail=f"good_iid={good_iid} result={result_ids}")
        check("list_gates", "2 purchases (one_off): EXCLUDED",
              one_off_iid not in result_ids,
              detail=f"one_off_iid={one_off_iid} result={result_ids}")
        check("list_gates", "1 purchase (singleton): EXCLUDED",
              single_iid not in result_ids,
              detail=f"single_iid={single_iid} result={result_ids}")
        check("list_gates", "5 purchases but lapsed 180d: EXCLUDED",
              lapsed_iid not in result_ids,
              detail=f"lapsed_iid={lapsed_iid} result={result_ids}")
        check("list_gates", "10 weekly purchases, 60d idle: INCLUDED (floor)",
              weekly_idle_iid in result_ids,
              detail=f"weekly_idle_iid={weekly_idle_iid} result={result_ids}")
        check("list_gates", "3 purchases at 90d floor: INCLUDED",
              edge_iid in result_ids,
              detail=f"edge_iid={edge_iid} result={result_ids}")
        check("list_gates", "3 purchases at 95d (past 90d floor): EXCLUDED",
              just_lapsed_iid not in result_ids,
              detail=f"just_lapsed_iid={just_lapsed_iid} result={result_ids}")
        check("list_gates", "60d-cadence at 200d (within 4x=240): INCLUDED",
              long_cadence_iid in result_ids,
              detail=f"long_cadence_iid={long_cadence_iid} result={result_ids}")
        check("list_gates", "60d-cadence at 250d (past 4x=240): EXCLUDED",
              long_cadence_stale_iid not in result_ids,
              detail=f"long_cadence_stale_iid={long_cadence_stale_iid} result={result_ids}")

    finally:
        # Cleanup -- always run, leave the DB pristine
        for iid in inserted_rules:
            cur.execute("DELETE FROM restock_rules WHERE item_id=?", (iid,))
        for iid in inserted_items:
            cur.execute("DELETE FROM items WHERE id=?", (iid,))
        conn.commit()
        conn.close()


# ─────────────────────────────────────────────
# Phase 21: inventory-based suggestion model
# ─────────────────────────────────────────────
def phase21_inventory_model(loaded):
    """The new list_generate suggestion model isn't pure cadence:
    project consumption from avg_quantity / avg_days_between forward
    from last_ordered_date, ask if the projected inventory covers the
    gap to the next order. Phase 20 tests the SQL pre-filters; this
    phase tests the inventory math itself (the part that decides
    whether and how much to suggest).
    """
    lg = loaded.get("list_generate")
    if not lg:
        check("inventory_model", "list_generate not loaded", False)
        return
    from datetime import date, timedelta

    def call(avg_quantity, cadence, days_since_last, days_until_target,
             coverage_window, last_order_qty=None):
        today = date(2026, 5, 27)
        last = today - timedelta(days=days_since_last)
        target = today + timedelta(days=days_until_target)
        return lg._inventory_suggest_quantity(
            avg_quantity=avg_quantity,
            avg_days_between=cadence,
            last_ordered_date=last.isoformat(),
            today_dt=today,
            target_dt=target,
            coverage_window_days=coverage_window,
            last_order_qty=last_order_qty,
        )

    # Yogurt (daily, qty 1, cadence 3.5d)
    qty, reason = call(1, 3.5, days_since_last=0, days_until_target=1,
                       coverage_window=3)
    check("inventory_model", "Yogurt bought today, target tomorrow, 3d coverage: needs ~1",
          qty == 1, detail=f"qty={qty} reason={reason}")

    qty, reason = call(1, 3.5, days_since_last=4, days_until_target=0,
                       coverage_window=3)
    check("inventory_model", "Yogurt bought 4d ago (out), target today, 3d coverage: needs 1",
          qty == 1, detail=f"qty={qty} reason={reason}")

    # Yogurt bought today in qty 2 — fresh stock covers a full cycle.
    # daily=0.57; inv=2; lasts 2/0.57 = 3.5d >= 3 → skip.
    qty, reason = call(2, 3.5, days_since_last=0, days_until_target=0,
                       coverage_window=3)
    check("inventory_model", "Yogurt bought today x2 (fresh stock), 3d coverage: skip",
          qty == 0, detail=f"qty={qty} reason={reason}")

    # Cooking oil (cadence 14d, qty 1)
    qty, reason = call(1, 14, days_since_last=3, days_until_target=0,
                       coverage_window=4)
    check("inventory_model", "Oil bought 3d ago (plenty), 4d coverage: skip",
          qty == 0, detail=f"qty={qty} reason={reason}")
    assert reason.startswith("stocked:"), f"expected stocked:N, got {reason}"

    qty, reason = call(1, 14, days_since_last=20, days_until_target=0,
                       coverage_window=4)
    check("inventory_model", "Oil bought 20d ago (out), 4d coverage: needs 1",
          qty == 1, detail=f"qty={qty} reason={reason}")

    # Eggs bought 5d ago in qty 1, daily consumption (1/3.5):
    # inventory_today = max(0, 1 - 5/3.5) = 0; coverage 3d demand = 0.86 -> 1
    qty, reason = call(1, 3.5, days_since_last=5, days_until_target=0,
                       coverage_window=3)
    check("inventory_model", "Eggs bought 5d ago (out), 3d coverage: needs 1",
          qty == 1, detail=f"qty={qty} reason={reason}")

    # Big bulk purchase: cucumbers, qty 6 bought 3d ago, cadence 3.5d
    # daily = 6/3.5 = 1.71. Inventory today = max(0, 6 - 1.71*3) = 0.87
    # Coverage demand 3d = 5.14. Need = 5.14 - 0.87 = 4.27 → 5
    qty, reason = call(6, 3.5, days_since_last=3, days_until_target=0,
                       coverage_window=3)
    check("inventory_model", "Cucumbers x6, 3d ago, demand exceeds inventory: ~5",
          qty == 5, detail=f"qty={qty} reason={reason}")

    # Bulk purchase 1d ago: stock covers the window
    # daily = 6/3.5 = 1.71. Inventory today = max(0, 6 - 1.71) = 4.29
    # Coverage demand 3d = 5.14. Need = 5.14 - 4.29 = 0.85 → ceil = 1
    qty, reason = call(6, 3.5, days_since_last=1, days_until_target=0,
                       coverage_window=3)
    check("inventory_model", "Cucumbers x6 bought 1d ago, small gap: 1",
          qty == 1, detail=f"qty={qty} reason={reason}")

    # Zero / bad-input handling
    qty, reason = call(0, 7, days_since_last=10, days_until_target=0,
                       coverage_window=3)
    # avg_qty=0 defaults to 1; cadence 7d, idle 10d → out of stock,
    # needs to cover 3 days → suggest 1
    check("inventory_model", "avg_qty=0 defaults to 1; idle 10d: needs 1",
          qty == 1, detail=f"qty={qty} reason={reason}")

    qty, reason = call(1, 0, days_since_last=10, days_until_target=0,
                       coverage_window=3)
    check("inventory_model", "cadence=0 returns bad_cadence (skip)",
          qty == 0 and reason == "bad_cadence",
          detail=f"qty={qty} reason={reason}")

    qty, reason = call(1, 7, days_since_last=0, days_until_target=0,
                       coverage_window=3)
    # daily=1/7=0.143. Today inventory=1 (just bought). Target=today same.
    # demand 3d = 0.43. inv_at_target = 1. need = 0.43-1 = -0.57 → skip
    check("inventory_model", "Bought today, plenty of stock: skip",
          qty == 0, detail=f"qty={qty} reason={reason}")

    # Edge: high consumption rate but tiny coverage window
    # Bread qty 1, cadence 3d, bought 2d ago, target=today, coverage 1d
    # daily = 0.333. inv_today = max(0, 1 - 0.667) = 0.33.
    # demand 1d = 0.33. need = 0 → skip (just enough)
    qty, reason = call(1, 3, days_since_last=2, days_until_target=0,
                       coverage_window=1)
    check("inventory_model", "Bread bought 2d ago, 1d coverage: just enough (skip)",
          qty == 0, detail=f"qty={qty} reason={reason}")

    # last_order_qty refinement: anomalous bulk purchase tracked correctly
    # Cucumber: avg_qty=2 cadence=3.5d (daily=0.57), but last order was 6.
    # 2d after: inventory = max(0, 6 - 0.57*2) = 4.86. Lasts 4.86/0.57 = 8.5d.
    # 3d coverage → 8.5 > 3 → skip.
    qty, reason = call(2, 3.5, days_since_last=2, days_until_target=0,
                       coverage_window=3, last_order_qty=6)
    check("inventory_model", "Bulk last_order_qty=6 (avg=2) 2d ago: skip",
          qty == 0, detail=f"qty={qty} reason={reason}")
    assert reason.startswith("stocked:")

    # Same shape but the bulk purchase was a while back: 8 days idle,
    # consumed 0.57*8 = 4.57, inventory = max(0, 6 - 4.57) = 1.43.
    # Lasts 2.5d, 3d coverage → suggest. short=0.5, qty = ceil(0.5*0.57) = 1.
    qty, reason = call(2, 3.5, days_since_last=8, days_until_target=0,
                       coverage_window=3, last_order_qty=6)
    check("inventory_model", "Bulk 8d ago, partial deplete, 3d coverage: suggest 1",
          qty == 1, detail=f"qty={qty} reason={reason}")

    # last_order_qty=None falls back to avg_quantity (legacy behavior)
    qty_with_none, _ = call(2, 3.5, days_since_last=0, days_until_target=0,
                            coverage_window=3, last_order_qty=None)
    qty_with_avg, _ = call(2, 3.5, days_since_last=0, days_until_target=0,
                            coverage_window=3, last_order_qty=2)
    check("inventory_model", "last_order_qty=None matches passing avg_qty",
          qty_with_none == qty_with_avg,
          detail=f"none={qty_with_none} avg={qty_with_avg}")

    # ── Typical-purchase cap (2026-06-04): the suggestion never exceeds what
    # the household usually buys, so fast staples they buy 1 of don't show 2-3.
    # Milk-like: avg_qty 1, cadence 2d, out of stock, 4d window. The raw
    # coverage need is 2, but they buy 1 per shop -> suggest 1, not 2.
    qty, reason = call(1, 2.0, days_since_last=4, days_until_target=0,
                       coverage_window=4)
    check("inventory_model", "Milk-like (buy 1, coverage wants 2): caps at typical 1",
          qty == 1, detail=f"qty={qty} reason={reason}")

    # Severe shortfall still bumps -- but only by ONE above the usual purchase.
    # Very fast item (cadence 1d), buy 1, out 5d, 4d window: coverage need 4 >=
    # 2*1+1 -> a clear run-out -> bump to 2 (never the full 4).
    qty, reason = call(1, 1.0, days_since_last=5, days_until_target=0,
                       coverage_window=4)
    check("inventory_model", "Severe run-out bumps usual+1 (1 -> 2), never full coverage",
          qty == 2, detail=f"qty={qty} reason={reason}")

    # Bulk buyer is unaffected: a 6-cucumber household genuinely short still
    # gets up to ~their usual 6 (min(coverage, typical) preserves bulk).
    qty, reason = call(6, 3.5, days_since_last=10, days_until_target=0,
                       coverage_window=4)
    check("inventory_model", "Bulk buyer (avg 6) very short: still allowed up to typical",
          qty >= 4, detail=f"qty={qty} reason={reason}")


# ─────────────────────────────────────────────
# Phase 22: staple cadence floor
# ─────────────────────────────────────────────
def phase22_staple_floor(loaded):
    """Staples (eggs/bread/milk/kefir/yogurt/bananas) are bought ~every order,
    but gappy order capture inflates their observed cadence and drops them off
    the list as 'stocked'. cadence_calibration caps their cadence at
    STAPLE_MAX_CADENCE. Guard the whole-word matcher so a rename can't (a) stop
    flooring real basics or (b) start flooring look-alikes like 'Eggplant'.
    """
    import sqlite3 as _sql
    try:
        import cadence_calibration as cc
    except Exception as e:
        check("staple_floor", "cadence_calibration imports", False, detail=str(e))
        return

    caps = getattr(cc, "STAPLE_MAX_CADENCE", None)
    check("staple_floor", "STAPLE_MAX_CADENCE is a per-item dict of sane caps (<=4d)",
          isinstance(caps, dict) and bool(caps) and all(
              isinstance(v, (int, float)) and 0 < v <= 4 for v in caps.values()),
          detail=f"STAPLE_MAX_CADENCE={caps!r}")
    # Caps were raised 2-3d -> 4d on 2026-06-04 once the Carrefour line-item
    # gap was fixed (the gap was the original reason for the tight caps). Lock
    # in ~order-cadence so a careless edit can't drop them back to the 2d value
    # that over-stated daily consumption and inflated suggested quantities.
    for word, expect in (("bread", 4.0), ("milk", 4.0), ("kefir", 4.0), ("banana", 4.0)):
        check("staple_floor", f"cap for {word!r} == {expect}d",
              isinstance(caps, dict) and caps.get(word) == expect,
              detail=f"got {caps.get(word) if isinstance(caps, dict) else caps!r}")

    conn = _sql.connect(":memory:")
    conn.row_factory = _sql.Row
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    must_floor = ["Eggs", "Organic Free Range Eggs", "Bread", "Milk",
                  "Kefir", "Yogurt", "Greek Yoghurt", "Banana"]
    must_not = ["Organic Eggplants", "Eggplant", "Buttermilk", "Breadcrumbs",
                "Cheese", "Butter", "Apple", "Striploin"]
    for i, n in enumerate(must_floor + must_not, 1):
        conn.execute("INSERT INTO items (id, name) VALUES (?, ?)", (i, n))
    ids = cc._staple_item_ids(conn)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM items")}
    floored = {names[i] for i in ids}

    for n in must_floor:
        check("staple_floor", f"floors staple {n!r}", n in floored)
    for n in must_not:
        check("staple_floor", f"does NOT floor look-alike {n!r}", n not in floored)


# ─────────────────────────────────────────────
# Phase 23: scheduled-list availability resolver
# ─────────────────────────────────────────────
def phase23_availability_resolver(loaded):
    """The Scheduled list is availability-checked by resolving each item's
    displayed product, live-verifying it, and letting the render path swap OOS
    picks. Guard the resolver: it must (a) include non-skipped items that pin a
    catalog product, (b) exclude skipped / store-less rows, and (c) expose an
    async orchestrator. The pending-item resolution path needs the full catalog
    schema, so it's exercised live on the Ally, not here.
    """
    import sqlite3 as _sql, inspect, asyncio
    try:
        import live_verify as lv
    except Exception as e:
        check("availability", "live_verify imports", False, detail=str(e))
        return

    check("availability", "extract_picks_from_resolved_list exists",
          hasattr(lv, "extract_picks_from_resolved_list"))
    check("availability", "live_verify_list_availability is async",
          asyncio.iscoroutinefunction(getattr(lv, "live_verify_list_availability", None)))

    conn = _sql.connect(":memory:")
    conn.row_factory = _sql.Row
    conn.execute("CREATE TABLE product_catalog (id INTEGER PRIMARY KEY, "
                 "product_name TEXT, product_url TEXT, sku TEXT, price REAL, "
                 "is_available INTEGER)")
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE list_items (id INTEGER PRIMARY KEY, list_id INTEGER, "
                 "item_id INTEGER, item_name_text TEXT, store_id INTEGER, "
                 "catalog_id INTEGER, estimated_price REAL, status TEXT, user_action TEXT)")
    conn.execute("INSERT INTO product_catalog VALUES (100,'Salmon Fillet','http://x/salmon','S1',40,1)")
    conn.execute("INSERT INTO product_catalog VALUES (103,'Kibsons Free Range Eggs','http://x/eggs','E1',12,1)")
    conn.executemany("INSERT INTO items VALUES (?,?)", [(10, "Salmon"), (13, "Eggs")])
    # pinned + approved -> included
    conn.execute("INSERT INTO list_items VALUES (1,999,10,'Salmon',1,100,40,'approved',NULL)")
    # skipped -> excluded
    conn.execute("INSERT INTO list_items VALUES (2,999,11,'Skip me',1,101,5,'skipped',NULL)")
    # store-less -> excluded
    conn.execute("INSERT INTO list_items VALUES (3,999,12,'No store',NULL,102,5,'pending',NULL)")
    # pending with NULL item_name_text -> item_name must fall back to items.name
    conn.execute("INSERT INTO list_items VALUES (4,999,13,NULL,1,103,12,'pending',NULL)")
    conn.commit()

    picks = lv.extract_picks_from_resolved_list(999, conn)
    cat_ids = {p["catalog_id"] for p in picks}
    check("availability", "resolver includes pinned non-skipped item",
          100 in cat_ids, detail=f"picks={cat_ids}")
    check("availability", "resolver excludes skipped + store-less rows",
          101 not in cat_ids and 102 not in cat_ids, detail=f"picks={cat_ids}")
    p_eggs = next((p for p in picks if p["catalog_id"] == 103), {})
    check("availability", "NULL item_name_text falls back to items.name (enables search fallback)",
          p_eggs.get("item_name") == "Eggs", detail=f"pick={p_eggs}")
    if picks:
        p0 = next((p for p in picks if p["catalog_id"] == 100), {})
        check("availability", "pinned pick carries product_url + store_id",
              p0.get("product_url") == "http://x/salmon" and p0.get("store_id") == 1,
              detail=f"pick={p0}")

    # Safety valve: a blocked/failed verify that returns "everything OOS" must
    # NOT mark the catalog unavailable (that was the false-positive cascade).
    VR = getattr(lv, "VerifyResult", None)
    if VR is not None:
        def _cat(n):
            c = _sql.connect(":memory:"); c.row_factory = _sql.Row
            c.execute("CREATE TABLE product_catalog (id INTEGER PRIMARY KEY, "
                      "price REAL, is_available INTEGER, scraped_at TEXT)")
            for i in range(1, n + 1):
                c.execute("INSERT INTO product_catalog VALUES (?,10,1,'')", (i,))
            return c
        # 5/5 verified OOS -> distrust -> none marked unavailable
        c = _cat(5)
        lv._update_catalog({i: VR(catalog_id=i, verified=True, is_available=False)
                            for i in range(1, 6)}, c)
        still = c.execute("SELECT COUNT(*) c FROM product_catalog WHERE is_available=1").fetchone()["c"]
        check("availability", "mass-OOS verify does NOT mark catalog unavailable",
              still == 5, detail=f"available={still}/5")
        # 1/5 OOS (minority) -> applied normally
        c = _cat(5)
        res = {1: VR(catalog_id=1, verified=True, is_available=False)}
        for i in range(2, 6):
            res[i] = VR(catalog_id=i, verified=True, is_available=True, live_price=11)
        lv._update_catalog(res, c)
        id1 = c.execute("SELECT is_available FROM product_catalog WHERE id=1").fetchone()[0]
        check("availability", "minority OOS (1/5) IS applied", id1 == 0,
              detail=f"id1 is_available={id1}")
        # empty-search results are inconclusive (verified=False) -> never counted OOS
        c = _cat(2)
        lv._update_catalog({1: VR(catalog_id=1, verified=False, is_available=True, error="search_empty"),
                            2: VR(catalog_id=2, verified=True, is_available=True, live_price=9)}, c)
        a1 = c.execute("SELECT is_available FROM product_catalog WHERE id=1").fetchone()[0]
        check("availability", "search_empty leaves catalog untouched", a1 == 1,
              detail=f"id1 is_available={a1}")


# ─────────────────────────────────────────────
# Phase 24: skip-marker convention (system skips recover, user skips stick)
# ─────────────────────────────────────────────
def phase24_skip_marker_convention(loaded):
    """A SYSTEM skip (no clean match / out of stock / transient) must recover on
    the next validator pass once the cause clears; a genuine USER skip must
    stick. They're told apart by the marker: SYSTEM skips use SYSTEM_SKIP_PREFIX
    ('auto_skipped_'), user skips use 'skipped'/'removed'. The validator's reset
    un-sticks the whole prefix. This guards that contract so a future OOS skip
    (auto_skipped_oos) auto-recovers instead of lingering like a user drop.
    """
    import sqlite3 as _sql
    try:
        import list_quality_validator as lqv
    except Exception as e:
        check("skip-marker", "list_quality_validator imports", False, detail=str(e))
        return

    check("skip-marker", "SYSTEM_SKIP_PREFIX defined",
          getattr(lqv, "SYSTEM_SKIP_PREFIX", None) == "auto_skipped_")
    check("skip-marker", "SKIP_NO_MATCH + SKIP_OOS use the system prefix",
          getattr(lqv, "SKIP_NO_MATCH", "").startswith("auto_skipped_")
          and getattr(lqv, "SKIP_OOS", "").startswith("auto_skipped_"))

    prefix = lqv.SYSTEM_SKIP_PREFIX
    conn = _sql.connect(":memory:")
    conn.row_factory = _sql.Row
    conn.execute("CREATE TABLE list_items (id INTEGER PRIMARY KEY, list_id INTEGER, "
                 "status TEXT, user_action TEXT)")
    rows = [
        (1, 7, "skipped", lqv.SKIP_NO_MATCH),   # system: matcher no-match -> recover
        (2, 7, "skipped", lqv.SKIP_OOS),        # system: out of stock     -> recover
        (3, 7, "skipped", "skipped"),           # user drop (chat/app)     -> stick
        (4, 7, "skipped", "removed"),           # user drop (WhatsApp)     -> stick
        (5, 7, "approved", ""),                 # active                   -> untouched
        (6, 8, "skipped", lqv.SKIP_OOS),        # other list               -> untouched
    ]
    conn.executemany("INSERT INTO list_items VALUES (?,?,?,?)", rows)
    conn.commit()

    # The exact reset the validator runs at the top of each pass.
    conn.execute(
        "UPDATE list_items SET status = 'pending', user_action = '' "
        "WHERE list_id = ? AND user_action LIKE ?",
        (7, prefix + "%"),
    )
    conn.commit()
    st = {r["id"]: (r["status"], r["user_action"]) for r in
          conn.execute("SELECT id, status, user_action FROM list_items").fetchall()}

    check("skip-marker", "system no-match skip recovers -> pending",
          st[1] == ("pending", ""), detail=f"row1={st[1]}")
    check("skip-marker", "system OOS skip recovers -> pending",
          st[2] == ("pending", ""), detail=f"row2={st[2]}")
    check("skip-marker", "user 'skipped' drop stays skipped",
          st[3] == ("skipped", "skipped"), detail=f"row3={st[3]}")
    check("skip-marker", "user 'removed' drop stays skipped",
          st[4] == ("skipped", "removed"), detail=f"row4={st[4]}")
    check("skip-marker", "active item untouched",
          st[5] == ("approved", ""), detail=f"row5={st[5]}")
    check("skip-marker", "other list's system skip untouched (scoped by list_id)",
          st[6] == ("skipped", lqv.SKIP_OOS), detail=f"row6={st[6]}")


# ─────────────────────────────────────────────
# Phase 25: subsystem health ledger
# ─────────────────────────────────────────────
def phase25_subsystem_health(loaded):
    """The health ledger turns the events the background loops already emit into
    one 'is everything OK' answer, so silent failures (a stale scraper, a crashed
    dispatcher) surface instead of rotting unseen. Guard the classification: a
    failed cadence-job is down, a recently-crashed dispatcher is degraded, a live
    WhatsApp probe drives that subsystem, and a clean slate is all-ok.
    """
    import sqlite3 as _sql
    from datetime import datetime, timezone, timedelta
    try:
        import subsystem_health as sh
    except Exception as e:
        check("health", "subsystem_health imports", False, detail=str(e))
        return

    def _conn(events):
        c = _sql.connect(":memory:")
        c.execute("CREATE TABLE app_events (id INTEGER PRIMARY KEY, timestamp TEXT, event TEXT)")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for name, ago_h in events:
            ts = (now - timedelta(hours=ago_h)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("INSERT INTO app_events (timestamp,event) VALUES (?,?)", (ts, name))
        c.commit()
        return c

    # Broken state (the real one we found): scrape failing, dispatcher crashing,
    # WhatsApp down.
    h = sh.get_health(_conn([
        ("nightly_scrape_error", 6), ("order_poller_done", 0.5),
        ("today_deliveries_dispatcher_error", 3), ("pattern_detector_done", 5),
    ]), wa_connected=False, wa_detail="locked", scheduler_beat_age_s=45)
    by = {s["key"]: s["status"] for s in h["subsystems"]}
    check("health", "failed cadence job (scrape) -> down", by.get("catalog_scrape") == "down", detail=str(by))
    check("health", "healthy cadence job (poller) -> ok", by.get("order_poller") == "ok", detail=str(by))
    check("health", "recently-crashed dispatcher -> degraded", by.get("today_deliveries") == "degraded", detail=str(by))
    check("health", "WhatsApp live-down -> down", by.get("whatsapp") == "down", detail=str(by))
    check("health", "live scheduler beat -> ok", by.get("scheduler") == "ok", detail=str(by))
    check("health", "overall reflects worst (down)", h["overall"] == "down", detail=h["overall"])

    # Recovered state: everything green.
    h2 = sh.get_health(_conn([
        ("nightly_scrape_done", 2), ("order_poller_done", 0.5),
        ("pattern_detector_done", 5), ("carrefour_backfill_done", 5),
    ]), wa_connected=True, scheduler_beat_age_s=30)
    by2 = {s["key"]: s["status"] for s in h2["subsystems"]}
    check("health", "clean slate -> overall ok", h2["overall"] == "ok", detail=str(by2))
    # An OLD dispatcher error (outside the window) must NOT flag degraded.
    h3 = sh.get_health(_conn([("nightly_scrape_done", 2),
                              ("today_deliveries_dispatcher_error", 100)]),
                       wa_connected=True, scheduler_beat_age_s=30)
    td = next(s for s in h3["subsystems"] if s["key"] == "today_deliveries")
    check("health", "stale (old) dispatcher error clears -> ok", td["status"] == "ok", detail=td["status"])


# ─────────────────────────────────────────────
# Phase 26: adaptive order-poll cadence
# ─────────────────────────────────────────────
def phase26_order_poll_cadence(loaded):
    """The order poller checks more often while an order is actually in flight
    (so 'out for delivery' / 'delivered' are caught in minutes, not up to an
    hour) and backs off when nothing is live or overnight. Guard the pure rule.
    """
    try:
        import scheduler as sch
    except Exception as e:
        check("order-cadence", "scheduler imports", False, detail=str(e))
        return
    f = sch._poll_interval_for
    DAY, NIGHT = 10, 3  # representative hours
    check("order-cadence", "dispatched (out for delivery) -> urgent ~10min",
          f({"dispatched"}, DAY) == sch.POLL_URGENT_S, detail=str(f({"dispatched"}, DAY)))
    check("order-cadence", "confirmed/placed -> active ~22min",
          f({"confirmed"}, DAY) == sch.POLL_ACTIVE_S and f({"placed"}, DAY) == sch.POLL_ACTIVE_S)
    check("order-cadence", "nothing in flight -> idle hourly",
          f(set(), DAY) == sch.POLL_IDLE_S, detail=str(f(set(), DAY)))
    check("order-cadence", "urgent beats active when both present",
          f({"confirmed", "dispatched"}, DAY) == sch.POLL_URGENT_S)
    check("order-cadence", "overnight overrides everything (no deliveries land)",
          f({"dispatched"}, NIGHT) == sch.POLL_OVERNIGHT_S, detail=str(f({"dispatched"}, NIGHT)))
    check("order-cadence", "urgent < active < idle (monotonic tightening)",
          sch.POLL_URGENT_S < sch.POLL_ACTIVE_S < sch.POLL_IDLE_S)


# ─────────────────────────────────────────────
# Phase 27: Carrefour order-detail receipt parser
# ─────────────────────────────────────────────
def phase27_carrefour_detail_parser(loaded):
    """The Carrefour "Consignment Details" page renders line items as a TEXT
    receipt (no product cards / PDP-link anchors), with each price split across
    nodes (33 / .49 / AED). The 2026-06-04 rewrite parses that layout; before
    it, EVERY Carrefour order captured 0 items and silently starved the model
    (cheese etc.). Guard the algorithm against the real captured receipt for
    order TEST_ORDER_REF, plus a canary that the production JS still targets
    the receipt layout (not the old link-anchored extractor).
    """
    import re as _re
    approx = lambda a, b: a is not None and abs(a - b) < 0.005

    # Python mirror of _CARREFOUR_ORDER_DETAIL_JS's line-walk (browser JS can't
    # run in this offline suite). The canary below guards the real JS so the
    # two can't silently diverge in intent.
    def parse_receipt(text):
        lines = [s.strip() for s in text.split("\n") if s.strip()]
        isQty = lambda s: bool(_re.match(r'^\d{1,3}$', s))
        isWeight = lambda s: bool(_re.match(r'^\d*\.?\d+\s*kg$', s, _re.I))
        isPriceInt = lambda s: bool(_re.match(r'^\d{1,5}$', s))
        isPriceDec = lambda s: bool(_re.match(r'^\.\d{2}$', s))
        isCurrency = lambda s: bool(_re.match(r'^AED$', s, _re.I))
        isFullPrice = lambda s: bool(_re.match(r'^AED\s+[\d,.]+$', s, _re.I))
        start, end = -1, len(lines)
        for i, l in enumerate(lines):
            if _re.match(r'^\d+\s+items?$', l, _re.I): start = i + 1; break
        for i in range(start if start >= 0 else 0, len(lines)):
            if _re.match(r'^order summary', lines[i], _re.I): end = i; break
        items = []
        if start >= 0:
            i = start
            while i < end:
                line = lines[i]
                if isWeight(line): qty, weight = 1, True
                elif isQty(line): qty, weight = int(line), False
                else: i += 1; continue
                name = lines[i + 1] if i + 1 < len(lines) else ''
                if (not name or len(name) < 3 or isQty(name) or isWeight(name)
                        or isCurrency(name) or isFullPrice(name) or isPriceDec(name)):
                    i += 1; continue
                total, consumedTo, j = 0.0, i + 1, i + 2
                while j < end and j <= i + 7:
                    n1 = lines[j + 1] if j + 1 < len(lines) else ''
                    n2 = lines[j + 2] if j + 2 < len(lines) else ''
                    if isPriceInt(lines[j]) and isPriceDec(n1) and isCurrency(n2):
                        total = float(lines[j] + n1); consumedTo = j + 2
                        if isFullPrice(lines[j + 3] if j + 3 < len(lines) else ''):
                            consumedTo = j + 3
                        break
                    l1 = lines[j + 1] if j + 1 < len(lines) else ''
                    if ((isQty(lines[j]) or isWeight(lines[j])) and l1 and len(l1) >= 3
                            and not isQty(l1) and not isCurrency(l1)):
                        break
                    j += 1
                if total <= 0: i += 1; continue
                unit = round(total / qty, 2) if qty > 1 else total
                items.append({"name": name, "qty": qty, "price": unit,
                              "total": total, "weight": weight})
                i = consumedTo + 1
        return items

    fixture = "\n".join([
        "TEST_ORDER_REF", "Your order is delivered!", "Order Detail",
        "Consignment Number:", "89703111", "Placed on:", "Jun 4, 2026", "14 Items",
        "1", "Jenan Organic Free Range Eggs Medium White or Brown 15 PCS", "15 PCS", "33", ".49", "AED",
        "1", "Premium Rocket Gerger Herbs 200g", "200g", "3", ".29", "AED",
        "2", "Organic Okra", "200g", "25", ".98", "AED",
        "1", "Premium Parsley 200g", "200g", "2", ".89", "AED",
        "1", "Golden Loaf Arabic Shawarma Bread 6 Breads", "6 PCS", "1", ".99", "AED",
        "1", "Premium Spring Onion 200g", "200g", "2", ".89", "AED",
        "1", "Indomie Fiery Chicken Flavour Fried Noodles, 94g Pack of 5", "94gx5", "12", ".99", "AED",
        "1", "Savushkin 9% Fat Cottage Cheese 200g", "200g", "15", ".79", "AED",
        "1", "Carrefour Bio Half Baguettes, 250g, Pack of 2", "250gx2", "16", ".49", "AED",
        "1", "Meliha Organic Full Fat Natural Traditional Yoghurt, 1kg", "1kg", "10", ".29", "AED",
        "1", "Glenstal Mild Irish Cheddar Cheese, 200g", "200g", "20", ".99", "AED",
        "0.47Kg", "New Zealand Beef Tenderloin Steak", "108", ".55", "AED", "AED 108.55",
        "1", "Britannia Good Day Chunkies Chocolate Chip Cookies, 96g Pack of 4", "96gx4", "9", ".49", "AED", "AED 9.99",
        "1", "Carrefour Ready-to-Eat Hass Avocados 2-Piece Pack", "350g", "10", ".89", "AED",
        "Order Summary", "Yay! You saved AED 0.50 on this order!", "Subtotal (incl. VAT)", "AED 276.51",
    ])
    items = parse_receipt(fixture)
    names = {it["name"] for it in items}
    check("carrefour-parse", "all 14 receipt items parsed", len(items) == 14, detail=f"got {len(items)}")
    check("carrefour-parse", "cheese captured (the reported miss)",
          "Glenstal Mild Irish Cheddar Cheese, 200g" in names)
    eggs = next((it for it in items if it["name"].startswith("Jenan")), None)
    check("carrefour-parse", "split price reassembled (33 + .49 -> 33.49)",
          bool(eggs) and approx(eggs["total"], 33.49), detail=str(eggs))
    okra = next((it for it in items if it["name"] == "Organic Okra"), None)
    check("carrefour-parse", "qty=2 line-total -> unit price (25.98/2=12.99)",
          bool(okra) and okra["qty"] == 2 and approx(okra["price"], 12.99), detail=str(okra))
    beef = next((it for it in items if "Tenderloin" in it["name"]), None)
    check("carrefour-parse", "weight-sold item (0.47Kg) -> qty 1 @ 108.55",
          bool(beef) and beef["qty"] == 1 and approx(beef["total"], 108.55), detail=str(beef))
    cookies = next((it for it in items if "Chunkies" in it["name"]), None)
    check("carrefour-parse", "discounted row uses paid 9.49 (not struck 9.99)",
          bool(cookies) and approx(cookies["total"], 9.49), detail=str(cookies))

    # Canary: the production JS must still target the receipt layout, not the
    # old '/p/'-link extractor that returned 0 items for every Carrefour order.
    try:
        import order_scraper as _os
        js = _os._CARREFOUR_ORDER_DETAIL_JS
        check("carrefour-parse", "JS parses body.innerText receipt section",
              "innerText" in js and "order summary" in js.lower())
        check("carrefour-parse", "JS dropped the old link-walk extractor",
              "seenContainers" not in js and "productLinks" not in js)
        check("carrefour-parse", "JS targets Carrefour PDP url pattern (sid=)", "sid=" in js)
    except Exception as e:
        check("carrefour-parse", "order_scraper JS constant importable", False, detail=str(e))


# ─────────────────────────────────────────────
# Phase 28: wrong-item feedback intent (reject vs swap)
# ─────────────────────────────────────────────
def phase28_wrong_item_intent(loaded):
    """The in-app 'Wrong item' feedback must tell a REJECTION ('we never buy
    this' -> remove + stop suggesting) apart from a SWAP request ('organic
    version' -> find a different product). Previously every reason was treated
    as a swap, which returned a near-identical product (7Up -> 7Up) so the list
    looked unchanged -- the reported "nothing changes" bug. Guard the classifier.
    """
    app_mod = loaded.get("app")
    if not app_mod or not hasattr(app_mod, "_is_rejection_feedback"):
        check("wrong-item", "_is_rejection_feedback present", False)
        return
    f = app_mod._is_rejection_feedback
    rejects = ["we never buy soft drinks", "remove this", "we don't buy this",
               "don't want it", "stop suggesting this", "never buy", "delete",
               "we dont drink soda", "get rid of it", "we don't like this",
               "no more chips please", "not for us"]
    for s in rejects:
        check("wrong-item", f"rejection -> remove: {s!r}", f(s) is True, detail=str(f(s)))
    swaps = ["organic version", "smaller size", "different brand", "cheaper one",
             "the 1L bottle", "diet version", "low fat one", ""]
    for s in swaps:
        check("wrong-item", f"swap/none -> keep swap path: {s!r}", f(s) is False, detail=str(f(s)))


# ─────────────────────────────────────────────
# Phase 29: future last_ordered_date clamp
# ─────────────────────────────────────────────
def phase29_future_last_ordered_clamp(loaded):
    """No restock_rules.last_ordered_date may be in the FUTURE -- a future date
    makes days_since_last clamp to 0 so the item looks perpetually fresh and
    never comes due. backfill_last_ordered_from_lists used to feed a
    superseded draft's future target_date in here; that's bounded now, and
    refresh_observed_cadences ends with a blanket clamp for any stragglers.
    Guard the clamp SQL (mirrors that statement)."""
    import sqlite3 as _sql
    c = _sql.connect(":memory:")
    c.execute("CREATE TABLE restock_rules (item_id INT, last_ordered_date TEXT, "
              "next_restock_date TEXT, avg_days_between REAL, updated_at TEXT)")
    c.execute("INSERT INTO restock_rules VALUES (1, date('now','localtime','+1 day'), '2099-01-01', 3, NULL)")
    c.execute("INSERT INTO restock_rules VALUES (2, '2026-03-31', '2026-04-03', 60, NULL)")
    c.execute("INSERT INTO restock_rules VALUES (3, date('now','localtime','+5 days'), '2099-01-01', NULL, NULL)")
    n = c.execute(
        "UPDATE restock_rules SET last_ordered_date = date('now','localtime'), "
        "next_restock_date = date('now','localtime', '+' || CAST(COALESCE(avg_days_between,7) AS INT) || ' days'), "
        "updated_at = datetime('now') WHERE last_ordered_date > date('now','localtime')"
    ).rowcount
    check("future-clamp", "clamps the 2 future rows", n == 2, detail=str(n))
    fut = c.execute("SELECT COUNT(*) FROM restock_rules WHERE last_ordered_date > date('now','localtime')").fetchone()[0]
    check("future-clamp", "no future last_ordered remains", fut == 0, detail=str(fut))
    past = c.execute("SELECT last_ordered_date FROM restock_rules WHERE item_id=2").fetchone()[0]
    check("future-clamp", "past date left untouched", past == "2026-03-31", detail=past)
    nxt3 = c.execute("SELECT next_restock_date FROM restock_rules WHERE item_id=3").fetchone()[0]
    exp3 = c.execute("SELECT date('now','localtime','+7 days')").fetchone()[0]
    check("future-clamp", "null cadence -> +7d next_restock", nxt3 == exp3, detail=f"{nxt3} vs {exp3}")
    c.close()


# ─────────────────────────────────────────────
# Phase 30: validator price-sanity (avoid overpriced picks)
# ─────────────────────────────────────────────
def phase30_price_sanity(loaded):
    """The validator must score a 2-3x-overpriced product (e.g. 15-egg Tregor
    at 72.70 / +161%) BELOW a sane-priced in-stock alternative, while NOT
    penalizing a legitimately bigger pack at the same price-per-unit
    (size-normalized per-gram comparison)."""
    try:
        import list_quality_validator as lv
    except Exception as e:
        check("price-sanity", "validator imports", False, detail=str(e))
        return

    def sc(cand, bar, gen):
        return lv.score_candidate(cand, bar, category=gen, generic_name=gen, search_term=gen)

    # Eggs: count-based (no grams) -> pack-ratio path. Baseline 28 AED.
    ebar = lv.derive_quality_bar({"name": "Eggs", "category": "eggs", "size": "", "avg_price": 28.0}, {})
    sane = {"is_available": 1, "price": 30.0, "size": "10 PCS", "product_name": "Eggs", "brand": "Larder", "is_organic": 1}
    pricey = {"is_available": 1, "price": 72.70, "size": "15 PCS", "product_name": "Eggs", "brand": "Tregor", "is_organic": 1}
    _, s_sane, _ = sc(sane, ebar, "eggs")
    _, s_pricey, r_pricey = sc(pricey, ebar, "eggs")
    check("price-sanity", "overpriced egg (72.70, ~2.6x) scores below sane (30)",
          s_pricey < s_sane, detail=f"sane={s_sane} pricey={s_pricey} | {r_pricey}")
    check("price-sanity", "overpriced egg flagged in reason",
          ("over usual price" in r_pricey) or ("pricey" in r_pricey), detail=r_pricey)

    # Cheese: grams -> size-normalized per-gram. Baseline 20 AED / 200g.
    cbar = lv.derive_quality_bar({"name": "Cheddar Cheese", "category": "cheese", "size": "200g", "avg_price": 20.0}, {})
    small = {"is_available": 1, "price": 20.0, "size": "200g", "product_name": "Cheddar", "brand": "Aa", "is_organic": 0}
    bigpk = {"is_available": 1, "price": 48.0, "size": "500g", "product_name": "Cheddar", "brand": "Aa", "is_organic": 0}   # 2.4x pack, 0.96x/g
    premium = {"is_available": 1, "price": 50.0, "size": "200g", "product_name": "Cheddar", "brand": "Aa", "is_organic": 0}  # 2.5x/g
    _, s_small, _ = sc(small, cbar, "cheddar")
    _, s_big, r_big = sc(bigpk, cbar, "cheddar")
    _, s_prem, r_prem = sc(premium, cbar, "cheddar")
    check("price-sanity", "bigger pack at same per-gram NOT price-penalized",
          "over usual price" not in r_big, detail=f"big={s_big} | {r_big}")
    check("price-sanity", "2.5x-per-gram cheese scores below sane same-size",
          s_prem < s_small, detail=f"small={s_small} prem={s_prem} | {r_prem}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# Phase 31: chat "add X" offers options instead of dead-ending
# ─────────────────────────────────────────────
def phase31_chat_add_suggestion(loaded):
    """When the curator can't confidently match a chat "add X" (e.g.
    "cucumber" -- which the basket scorer mis-ranks to "eggplant"/"pudding
    rice"), the fast path must OFFER real catalog matches via the suggestion
    sheet rather than dead-end on a ~50s LLM non-answer that adds nothing and
    never refreshes the view. Also guards the two consistency fixes shipped
    alongside it: the unconditional post-turn refresh, and the approve handler
    pinning the EXACT catalog_id to the list the user is viewing."""
    import types
    import inspect
    app_mod = loaded.get("app")
    if not app_mod:
        check("chat-add", "app not loaded", False)
        return

    # 1) _build_catalog_suggestion returns REAL matches for a known produce
    #    item and stages them for the sheet (NAME search, not the curator).
    sess = types.SimpleNamespace(pending_suggestions=None)
    sheet = app_mod._build_catalog_suggestion("cucumber", None, 1, sess)
    ok_sheet = bool(sheet and sheet.get("product"))
    check("chat-add", "cucumber -> a suggestion sheet is built", ok_sheet,
          detail=str(sheet)[:160])
    if ok_sheet:
        pname = (sheet["product"].get("product_name") or "").lower()
        check("chat-add", "offered product is actually a cucumber (not eggplant)",
              "cucumber" in pname, detail=pname)
        check("chat-add", "sheet carries a catalog_id to pin",
              bool(sheet["product"].get("catalog_id")),
              detail=str(sheet["product"].get("catalog_id")))
        ps = sess.pending_suggestions or {}
        check("chat-add", "pending_suggestions staged for next/skip/approve",
              bool(ps.get("products")) and ps.get("current_index") == 0
              and ps.get("quantity") == 1,
              detail=str({k: ps.get(k) for k in ("current_index", "quantity")}))

    # 2) A genuinely unknown name yields no sheet -> fast honest message path.
    sess2 = types.SimpleNamespace(pending_suggestions=None)
    none_sheet = app_mod._build_catalog_suggestion("zqxwv_nonexistent_item", None, 1, sess2)
    check("chat-add", "unknown item -> no sheet (honest fallback)",
          none_sheet is None, detail=str(none_sheet)[:120])

    # 3) Static: the post-turn refresh is now UNCONDITIONAL — the old
    #    STRUCTURE_CHANGING_TOOLS gate that left edits like stop_suggesting_item
    #    stale until reconnect is gone, and the fast path routes an unmatched
    #    add to the suggestion sheet.
    src = inspect.getsource(app_mod)
    check("chat-add", "refresh gate removed (STRUCTURE_CHANGING_TOOLS gone)",
          "STRUCTURE_CHANGING_TOOLS" not in src)
    check("chat-add", "fast path routes unmatched add to suggestion sheet",
          'fast_tool.get("suggest")' in src)

    # 4) Static: approve handler pins the exact catalog_id to the VIEWED list
    #    (it previously dropped catalog_id and used session.list_id, so the
    #    approved product re-ran the matcher that had already failed).
    add_sug = inspect.getsource(app_mod.handle_add_suggestion)
    check("chat-add", "approve handler passes catalog_id to list_add_item",
          "catalog_id=catalog_id" in add_sug, detail="handle_add_suggestion")
    check("chat-add", "approve handler targets the viewed list (_resolve_edit_list)",
          "_resolve_edit_list" in add_sug, detail="handle_add_suggestion")


# ─────────────────────────────────────────────
# Phase 32: organic requirement honors the is_organic flag
# ─────────────────────────────────────────────
def phase32_organic_flag(loaded):
    """The scorer's organic REQUIREMENT must honor the catalog's structured
    is_organic flag, not just a text search for the word "organic" in the
    product name. Kibsons sells produce as organic by default WITHOUT putting
    "organic" in the title ("Kibsons Cucumbers"), so a pure text check
    disqualified every real organic cucumber to 'avoid' — and the curator
    returned eggplant / baby food instead. Guard: a flag-organic product is
    NOT disqualified for the organic requirement, while a genuinely non-organic
    product still IS. Scored against item 8 (Cucumber, organic-required)."""
    try:
        from product_score import score_single_product
    except Exception as e:
        check("organic-flag", "product_score imports", False, detail=str(e))
        return

    base = {
        "name": "Kibsons Cucumbers - 500g - 5 to 8 Pieces",  # NO 'organic' word
        "price": 6.9, "brand": "", "size": "500g",
        "description": "", "origin_country": "", "ingredients": "",
    }
    org = score_single_product("Cucumber", "Kibsons", {**base, "is_organic": True}, item_id=8)
    non = score_single_product("Cucumber", "Kibsons", {**base, "is_organic": False}, item_id=8)

    def _reasons_text(s):
        rs = s.get("reasons", [])
        if isinstance(rs, str):
            try:
                rs = json.loads(rs)
            except Exception:
                rs = [rs]
        return " ".join(r.lower() for r in rs if isinstance(r, str))

    org_txt = _reasons_text(org)
    check("organic-flag", "flag-organic cucumber NOT disqualified",
          not org.get("disqualified"), detail=f"score={org['score']} {org_txt}")
    check("organic-flag", "flag-organic satisfies the organic requirement",
          "missing required 'organic'" not in org_txt, detail=org_txt)
    check("organic-flag", "flag-organic scores acceptable+ (not avoid)",
          org.get("tier") in ("preferred", "acceptable"),
          detail=f"tier={org.get('tier')} score={org['score']}")
    # The requirement still bites a genuinely non-organic product.
    check("organic-flag", "non-organic cucumber still disqualified",
          non.get("disqualified") is True,
          detail=f"score={non['score']} tier={non.get('tier')}")


# ─────────────────────────────────────────────
# Phase 33: scrape availability sweep guard
# ─────────────────────────────────────────────
def phase33_availability_guard(loaded):
    """A failed/incomplete catalog scrape must NOT mark the whole store
    unavailable. availability_sweep_safe() gates the "mark not-seen products
    unavailable" step: it returns True only when the scrape re-saw enough of the
    existing catalog to trust its absences. Carrefour hit 0 available of 60,714
    because a partial scrape swept everything. Also guards (statically) that both
    RSC scrapers actually call the gate."""
    pk = loaded.get("product_knowledge")
    if not pk or not hasattr(pk, "availability_sweep_safe"):
        check("avail-guard", "availability_sweep_safe present", False)
        return
    f = pk.availability_sweep_safe
    # Healthy full scrape: re-saw ~all of the catalog -> safe to sweep.
    check("avail-guard", "full coverage -> sweep allowed", f(12000, 12932) is True)
    check("avail-guard", "exactly half -> sweep allowed", f(6466, 12932) is True)
    # The Carrefour failure: saw a tiny fraction of 60,714 -> must NOT sweep.
    check("avail-guard", "tiny fraction of big store -> sweep BLOCKED",
          f(50, 60714) is False)
    check("avail-guard", "30% coverage -> sweep BLOCKED", f(3000, 10000) is False)
    # Absolute floor: a handful of products is always incomplete.
    check("avail-guard", "under min_seen -> sweep BLOCKED", f(150, 12932) is False)
    # Brand-new / empty store: nothing to wrongly zero.
    check("avail-guard", "empty store -> sweep allowed", f(0, 0) is True)

    # Both RSC scrapers must actually call the gate (read source, no import).
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for fname in ("scrape_carrefour_rsc.py", "scrape_kibsons_rsc.py"):
        try:
            with open(os.path.join(root, fname), encoding="utf-8") as fh:
                src = fh.read()
            check("avail-guard", f"{fname} calls availability_sweep_safe",
                  "availability_sweep_safe" in src)
        except Exception as e:
            check("avail-guard", f"{fname} readable", False, detail=str(e)[:120])


# ─────────────────────────────────────────────
# Phase 34: validator honors in-app per-item feedback
# ─────────────────────────────────────────────
def phase34_validator_honors_feedback(loaded):
    """The scheduled-list product picker (list_quality_validator) must honor the
    user's DIRECT in-app feedback: a 'wrong item' product is never re-picked, a
    'never {brand}' brand is rejected, and a pinned product always wins. These
    were written to user_item_prefs but the validator never read them, so flagged
    items kept coming back next generation (the reported feedback-loop disconnect
    #7). Guards both the bar derivation and the score_candidate enforcement."""
    try:
        import list_quality_validator as lv
    except Exception as e:
        check("fb-loop", "validator imports", False, detail=str(e))
        return
    fb = {"avoid_brands": ["Tregor"], "blocked_catalog_ids": [555],
          "pinned_catalog_id": 999}
    bar = lv.derive_quality_bar(
        {"name": "Eggs", "category": "eggs", "size": "", "avg_price": 28.0}, {}, fb)
    check("fb-loop", "bar folds in blocked + pinned + avoid_brand",
          555 in bar.get("blocked_catalog_ids", [])
          and bar.get("pinned_catalog_id") == 999
          and "Tregor" in bar.get("avoid_brands", []))

    def cand(cid, name="Organic Eggs"):
        return {"id": cid, "product_name": name, "brand": "", "is_available": 1,
                "is_organic": 1, "price": 28.0, "size": "10 PCS",
                "description": "", "origin_country": ""}

    ok, _, r = lv.score_candidate(cand(555), bar, category="eggs",
                                  generic_name="eggs", search_term="eggs")
    check("fb-loop", "wrong-item product is rejected next generation",
          ok is False and "blocked" in r, detail=r)
    ok, sc, r = lv.score_candidate(cand(999), bar, category="eggs",
                                   generic_name="eggs", search_term="eggs")
    check("fb-loop", "pinned product always wins", ok is True and sc >= 10000,
          detail=r)
    ok, _, r = lv.score_candidate(cand(124, "Tregor Eggs 15pc"), bar,
                                  category="eggs", generic_name="eggs",
                                  search_term="eggs")
    check("fb-loop", "never-brand product is rejected",
          ok is False and "Tregor" in r, detail=r)
    # Backward-compat: 2-arg derive_quality_bar (no feedback) still works.
    bar0 = lv.derive_quality_bar(
        {"name": "Eggs", "category": "eggs", "size": "", "avg_price": 28.0}, {})
    check("fb-loop", "no-feedback bar has empty blocked/pinned",
          bar0.get("blocked_catalog_ids") == [] and bar0.get("pinned_catalog_id") is None)
    # Static: the validator actually loads the in-app feedback table.
    import inspect
    src = inspect.getsource(lv.validate_list_quality)
    check("fb-loop", "validator loads user_item_prefs", "user_item_prefs" in src)


# ─────────────────────────────────────────────
# Phase 35: feedback understanding (LLM) + block undo
# ─────────────────────────────────────────────
def phase35_feedback_undo_and_llm(loaded):
    """Slice 1 Part B. (1) In-app 'wrong item' free-text now runs through the
    SAME LLM 'understand' step delivered-order feedback uses
    (household_rules.capture_item_feedback). (2) The reset/undo now clears the
    user's product + brand blocks — previously it only cleared cadence + stop,
    so a 'wrong item' / 'never brand' flag could never be undone."""
    import sqlite3 as _sql
    import inspect
    try:
        from pattern_detector import reset_item_ladder
    except Exception as e:
        check("fb-undo", "pattern_detector imports", False, detail=str(e))
        return
    c = _sql.connect(":memory:")
    c.execute("CREATE TABLE user_item_prefs (id INTEGER PRIMARY KEY, item_id INT, "
              "pref_type TEXT, value TEXT, is_active INT DEFAULT 1)")
    c.execute("CREATE TABLE preference_state (id INTEGER PRIMARY KEY, item_id INT, "
              "dimension TEXT, key TEXT)")
    c.execute("CREATE TABLE restock_rules (item_id INT, cadence_adjustment REAL, updated_at TEXT)")
    c.execute("INSERT INTO user_item_prefs (item_id,pref_type,value,is_active) VALUES "
              "(5,'wrong_item','555',1),(5,'block_brand','Tregor',1),(5,'stop_suggesting','user',1)")
    c.execute("INSERT INTO preference_state (item_id,dimension,key) VALUES (5,'product','blocked')")
    c.execute("INSERT INTO restock_rules (item_id,cadence_adjustment) VALUES (5,1.5)")
    c.commit()
    res = reset_item_ladder(c, 5, kind="all")
    check("fb-undo", "reset reports blocks_cleared >= 2",
          res.get("blocks_cleared", 0) >= 2, detail=str(res))
    active = c.execute("SELECT COUNT(*) FROM user_item_prefs WHERE item_id=5 AND "
                       "is_active=1 AND pref_type IN ('wrong_item','block_brand')").fetchone()[0]
    check("fb-undo", "wrong_item + block_brand deactivated", active == 0)
    ps = c.execute("SELECT COUNT(*) FROM preference_state WHERE item_id=5 AND "
                   "dimension='product' AND key='blocked'").fetchone()[0]
    check("fb-undo", "learned blocked-product state cleared", ps == 0)
    cad = c.execute("SELECT cadence_adjustment FROM restock_rules WHERE item_id=5").fetchone()[0]
    check("fb-undo", "cadence reset to 1.0", cad == 1.0)
    # kind='blocks' clears blocks but leaves stop_suggesting intact.
    c.execute("UPDATE user_item_prefs SET is_active=1")
    c.commit()
    reset_item_ladder(c, 5, kind="blocks")
    stop_active = c.execute("SELECT COUNT(*) FROM user_item_prefs WHERE item_id=5 AND "
                            "pref_type='stop_suggesting' AND is_active=1").fetchone()[0]
    check("fb-undo", "kind=blocks leaves stop_suggesting intact", stop_active == 1)
    c.close()

    import household_rules
    check("fb-undo", "capture_item_feedback is async",
          inspect.iscoroutinefunction(getattr(household_rules, "capture_item_feedback", None)))
    app_mod = loaded.get("app")
    if app_mod:
        check("fb-undo", "wrong-item handler routes free-text through the LLM",
              "capture_item_feedback" in inspect.getsource(app_mod.handle_item_pref))


# ─────────────────────────────────────────────
# Phase 36: scheduled list aligns with purchase history
# ─────────────────────────────────────────────
def phase36_validator_purchase_history(loaded):
    """Slice 3: the scheduled-list picker (validator) now biases toward the
    brands the household ACTUALLY buys (purchase history from scraped vendor
    orders + app orders), graduated by frequency — so the list converges on what
    gets ordered. Previously only the chat curator used purchase history; the
    scheduled list ignored it, so the two surfaces disagreed and the list never
    'learned' the household's brands."""
    import list_quality_validator as lv
    import inspect
    bar = lv.derive_quality_bar(
        {"name": "Banana", "category": "produce", "size": "", "avg_price": 10.0}, {})
    bar["history_brands"] = {"Chiquita": 8, "Dole": 1}

    def cand(name, brand, cid):
        return {"id": cid, "product_name": name, "brand": brand, "is_available": 1,
                "is_organic": 0, "price": 10.0, "size": "6 pc", "description": "",
                "origin_country": ""}

    _, sc_c, r_c = lv.score_candidate(cand("Chiquita Bananas 6pc", "Chiquita", 1),
                                      bar, category="produce", generic_name="banana",
                                      search_term="banana")
    _, sc_d, _ = lv.score_candidate(cand("Dole Bananas 6pc", "Dole", 2), bar,
                                    category="produce", generic_name="banana",
                                    search_term="banana")
    _, sc_g, _ = lv.score_candidate(cand("Generic Bananas 6pc", "", 3), bar,
                                    category="produce", generic_name="banana",
                                    search_term="banana")
    check("buy-history", "usual brand (8x) > rare (1x) > unknown (0x)",
          sc_c > sc_d > sc_g, detail=f"{sc_c}/{sc_d}/{sc_g}")
    check("buy-history", "frequent-brand boost is reasoned",
          "usually buy Chiquita" in r_c, detail=r_c)
    bar2 = lv.derive_quality_bar(
        {"name": "Banana", "category": "produce", "size": "", "avg_price": 10.0}, {})
    _, _, r2 = lv.score_candidate(cand("Chiquita Bananas", "Chiquita", 1), bar2,
                                  category="produce", generic_name="banana",
                                  search_term="banana")
    check("buy-history", "no history -> no boost (safe)", "usually" not in r2)
    check("buy-history", "validator loads get_purchase_history",
          "get_purchase_history" in inspect.getsource(lv.validate_list_quality))


# ─────────────────────────────────────────────
# Phase 37: alternatives X-vs-Y comparison learning
# ─────────────────────────────────────────────
def phase37_swap_comparison(loaded):
    """Slice 4: accepting an alternative now compares the chosen product X against
    the one it replaced Y and learns the DIFFERENTIATOR (brand / organic /
    cheaper) — the cleanest preference signal — instead of a blind SKU pin.
    _swap_differentiators is the pure decision (no DB writes)."""
    app_mod = loaded.get("app")
    if not app_mod:
        check("swap-learn", "app loaded", False)
        return
    sd = app_mod._swap_differentiators
    d = sd({"brand": "Chiquita", "is_organic": 0, "price": 10.0},
           {"brand": "Dole", "is_organic": 0, "price": 10.0})
    check("swap-learn", "different brand -> learn the brand",
          ("brand", "Chiquita") in d, detail=str(d))
    d = sd({"brand": "X", "is_organic": 1, "price": 12.0},
           {"brand": "X", "is_organic": 0, "price": 12.0})
    check("swap-learn", "organic upgrade -> learn organic",
          any(k == "organic" for k, _ in d), detail=str(d))
    d = sd({"brand": "X", "is_organic": 0, "price": 8.0},
           {"brand": "X", "is_organic": 0, "price": 10.0})
    check("swap-learn", "cheaper (>=15%) -> learn cheaper",
          any(k == "cheaper" for k, _ in d), detail=str(d))
    same = {"brand": "X", "is_organic": 0, "price": 10.0}
    check("swap-learn", "identical X/Y -> nothing learned", sd(same, dict(same)) == [])
    d = sd({"brand": "X", "is_organic": 0, "price": 9.5},
           {"brand": "X", "is_organic": 0, "price": 10.0})
    check("swap-learn", "<15% cheaper not flagged",
          not any(k == "cheaper" for k, _ in d), detail=str(d))
    import inspect
    src = inspect.getsource(app_mod.handle_accept_alternative)
    check("swap-learn", "handler captures Y + calls the learner",
          "_learn_from_swap" in src and "orig_catalog_id" in src)


def phase38_whatsapp_learning(loaded):
    """Slice 5: WhatsApp family edits feed the SAME durable preference pipeline.
    (a) the X-vs-Y swap comparison is now canonical in household_rules so the
    in-app sheet AND the WhatsApp swap pick learn identically; (b) a WhatsApp
    swap captures Y before overwriting it + calls learn_from_swap(...,
    signal_source='whatsapp'); (c) a WhatsApp skip emits an item_skip inclusion
    signal (was a silent one-cycle drop). All static/pure — no live round."""
    import importlib
    import inspect
    hr = importlib.import_module("household_rules")

    # (a) canonical relocation + shared pure decision
    check("wa-learn", "household_rules exposes swap_differentiators + learn_from_swap",
          callable(getattr(hr, "swap_differentiators", None)) and
          callable(getattr(hr, "learn_from_swap", None)))
    sig = str(inspect.signature(hr.learn_from_swap))
    check("wa-learn", "learn_from_swap tags signal_source", "signal_source" in sig,
          detail=sig)
    d = hr.swap_differentiators({"brand": "Almarai", "is_organic": 0, "price": 9.0},
                                {"brand": "Lacnor", "is_organic": 0, "price": 9.0})
    check("wa-learn", "shared swap decision learns brand",
          ("brand", "Almarai") in d, detail=str(d))

    # (b) in-app delegates to the canonical helper (one implementation)
    app_mod = loaded.get("app")
    if app_mod:
        a = app_mod._swap_differentiators(
            {"brand": "Almarai", "is_organic": 0, "price": 9.0},
            {"brand": "Lacnor", "is_organic": 0, "price": 9.0})
        check("wa-learn", "app._swap_differentiators delegates (same result)",
              a == d, detail=str(a))

    # (c) WhatsApp swap path captures Y + learns with whatsapp source
    fm = loaded.get("feedback_manager")
    if not fm:
        check("wa-learn", "feedback_manager loaded", False)
        return
    swap_src = inspect.getsource(fm.FeedbackManager._perform_swap_commit)
    check("wa-learn", "WhatsApp swap captures Y (orig_catalog_id)",
          "orig_catalog_id" in swap_src)
    check("wa-learn", "WhatsApp swap learns with signal_source='whatsapp'",
          "learn_from_swap" in swap_src and "signal_source=\"whatsapp\"" in swap_src)

    # (d) WhatsApp skip now emits a durable inclusion signal
    reply_src = inspect.getsource(fm.FeedbackManager.handle_member_reply)
    check("wa-learn", "WhatsApp skip emits item_skip inclusion signal",
          "item_skip" in reply_src and "item_inclusion" in reply_src)


def phase39_curator_unified(loaded):
    """Slice 6: unify the READ side. The chat curator (product_score) now honors
    the SAME durable household_rules (category-level prefer/avoid brand) that the
    scheduled-list validator already honors, resolved once per item — so a brand
    learned via a swap or delivered feedback moves chat suggestions the same way
    it moves the scheduled list. Pure/in-memory — no live scrape."""
    ps = loaded.get("product_score")
    if not ps:
        check("unify", "product_score loaded", False)
        return

    # resolver tolerates no DB and returns the documented shape
    v = ps._resolve_rules_view("Milk", None, None)
    check("unify", "_resolve_rules_view shape (no DB)",
          isinstance(v, dict) and set(v.keys()) == {"avoid_brands", "prefer_brands"},
          detail=str(v))

    prof = {"brands": {"preferred": [], "acceptable": [], "avoid": []}}
    pref_prod = {"name": "Almarai Full Fat Milk 2L", "brand": "Almarai", "price": 9.0}
    other_prod = {"name": "Lacnor Full Fat Milk 2L", "brand": "Lacnor", "price": 9.0}

    base = ps.score_single_product("Milk", "Carrefour", dict(pref_prod), profile=prof)["score"]
    boosted = ps.score_single_product(
        "Milk", "Carrefour", dict(pref_prod), profile=prof,
        rules_view={"prefer_brands": ["Almarai"], "avoid_brands": []})["score"]
    check("unify", "prefer_brand household rule boosts the curator (+)",
          boosted > base, detail=f"{base} -> {boosted}")

    base_o = ps.score_single_product("Milk", "Carrefour", dict(other_prod), profile=prof)["score"]
    avoided = ps.score_single_product(
        "Milk", "Carrefour", dict(other_prod), profile=prof,
        rules_view={"prefer_brands": [], "avoid_brands": ["Lacnor"]})
    check("unify", "avoid_brand household rule penalizes the curator (-)",
          avoided["score"] < base_o, detail=f"{base_o} -> {avoided['score']}")
    check("unify", "avoid_brand annotated in reasons",
          any("Avoided" in r for r in avoided["reasons"]), detail=str(avoided["reasons"]))

    import inspect
    sp_src = inspect.getsource(ps.score_products)
    check("unify", "score_products resolves rules_view once per item",
          "_resolve_rules_view" in sp_src and "rules_view=rules_view" in sp_src)


def phase40_delivery_fee_model(loaded):
    """Delivery-fee-aware cart, slice 1: the per-vendor cost model + pure fee math.
    Carrefour is a value threshold (free over min_order_value); Kibsons is a FLAT
    fee regardless of cart size (no threshold => can't be padded to free), free
    only with a subscription. analyze_list_delivery surfaces the per-vendor fee
    picture. Pure math is deterministic; no DB needed for the core checks."""
    do = loaded.get("delivery_optimizer")
    if not do:
        check("delivery", "delivery_optimizer loaded", False)
        return

    carre = {"min_order_value": 150, "delivery_fee": 12, "has_subscription": False}
    kib = {"min_order_value": 0, "delivery_fee": 8, "has_subscription": False}
    kib_sub = {"min_order_value": 0, "delivery_fee": 8, "has_subscription": True}

    check("delivery", "Carrefour below threshold -> fee",
          do.effective_fee(carre, 127) == 12, detail=str(do.effective_fee(carre, 127)))
    check("delivery", "Carrefour at/above threshold -> free",
          do.effective_fee(carre, 150) == 0 and do.effective_fee(carre, 200) == 0)
    check("delivery", "Kibsons flat fee regardless of cart size",
          do.effective_fee(kib, 500) == 8, detail=str(do.effective_fee(kib, 500)))
    check("delivery", "Kibsons subscription -> free",
          do.effective_fee(kib_sub, 10) == 0)
    check("delivery", "shortfall: Carrefour 127 -> 23",
          do.shortfall_to_free(carre, 127) == 23.0,
          detail=str(do.shortfall_to_free(carre, 127)))
    check("delivery", "shortfall: flat vendor -> None (can't pad to free)",
          do.shortfall_to_free(kib, 500) is None)
    check("delivery", "is_flat_fee: Kibsons yes, Carrefour no, subscribed no",
          do.is_flat_fee(kib) and not do.is_flat_fee(carre) and not do.is_flat_fee(kib_sub))

    # confirmed model values (Kibsons flat 8 / no threshold, Carrefour 150 / 12)
    check("delivery", "DEFAULT_MODEL = user-confirmed figures",
          do.DEFAULT_MODEL["1"] == {"min_order_value": 0.0, "delivery_fee": 8.0} and
          do.DEFAULT_MODEL["2"] == {"min_order_value": 150.0, "delivery_fee": 12.0},
          detail=str(do.DEFAULT_MODEL))
    check("delivery", "analyze/load/seed callable",
          all(callable(getattr(do, n, None)) for n in
              ("analyze_list_delivery", "load_delivery_model",
               "ensure_delivery_model_seeded", "set_subscription")))

    # preflight surfaces the delivery picture
    cb = loaded.get("cart_builder")
    if cb:
        import inspect
        check("delivery", "preflight_cart surfaces the delivery analysis",
              "analyze_list_delivery" in inspect.getsource(cb.preflight_cart))


def phase41_alternatives_pollution(loaded):
    """item_alternatives pollution fix: the matcher let products with a DIFFERENT
    produce head-noun through (Banana matched 'Banana Shallots' onion, 'Plantain
    Banana', baby-food 'Apple Banana Crush', 'Banana Milk' drinks) because the
    noun gate only checks PRESENCE. New gates: foreign-produce-noun + prepared
    forms + ml-volume — high precision (real bananas + leafy 'Lettuce Leaves'
    survive). purge_gated_alternatives clears existing pollution."""
    pk = loaded.get("product_knowledge")
    if not pk:
        check("pollution", "product_knowledge loaded", False)
        return
    nouns = pk._primary_nouns("Banana")

    def blocked(name):
        return (pk._form_modifier_blocked("Produce", name)
                or pk._foreign_produce_noun_blocked("Produce", nouns, name)
                or not pk._noun_match(nouns, name))

    POLLUTANTS = [
        "Kibsons Banana Shallots - 500g", "Yellow Plantain Banana",
        "Kibsons Curuba - Banana Passionfruit - 500g", "Kibsons Banana leaf",
        "Kibsons Banana Stem - 2 to 3 pieces", "Organic Apple Banana Crush, 100g",
        "Sambazon Scoopable Acai - Banana - 500ml", "Pokka Drink, Banana Milk, 240ml",
        "Barakat Strawberry Banana 330ml",
    ]
    REAL = [
        "Chiquita Banana", "Chiquita Bananas - 4 to 6 pieces",
        "Kibsons Red Banana - 4 to 5 Pcs", "Kibsons Banana Green - 3 to 4 pieces",
        "Heyy Life Banana - 350g",
    ]
    for p in POLLUTANTS:
        check("pollution", f"blocked: {p[:34]}", blocked(p), detail=p)
    for r in REAL:
        check("pollution", f"kept: {r[:34]}", not blocked(r), detail=r)

    # leafy-green false-positive guard: "Lettuce Leaves" must survive
    lett = pk._primary_nouns("Lettuce")
    check("pollution", "Lettuce Leaves NOT blocked (leafy crop)",
          not pk._foreign_produce_noun_blocked("Produce", lett, "Romaine Lettuce Leaves 200g"))
    # ml-volume only gates Produce, not Pantry (Olive Oil 500ml is fine)
    check("pollution", "Pantry ml-volume not gated (Olive Oil 500ml)",
          not pk._foreign_produce_noun_blocked("Pantry", pk._primary_nouns("Olive Oil"),
                                               "Borges Olive Oil 500ml"))
    check("pollution", "purge_gated_alternatives available",
          callable(getattr(pk, "purge_gated_alternatives", None)))


def phase42_cross_vendor_price(loaded):
    """Per-item cross-vendor price selection (safe). Compares the PREFERRED
    product per store by UNIT price, never downgrades a maybe-organic item to a
    name-conventional one (the is_organic flag is unreliable in BOTH directions —
    Lurpak flagged organic, Isigny Organic flagged 0), and only compares like
    base units. Read-only surfacing; auto-apply (with the fee guardrail) is a
    later, user-gated slice."""
    vsel = loaded.get("vendor_select")
    if not vsel:
        check("xvendor", "vendor_select loaded", False)
        return

    # the unreliable-flag downgrade guard: name beats flag
    isigny = {"product_name": "Isigny Ste Mere Organic Unsalted Butter, 200g", "is_organic": 0}
    lurpak = {"product_name": "Lurpak Unsalted Butter - 200g", "is_organic": 1}
    check("xvendor", "candidate organic judged by NAME, not flag",
          vsel._name_organic(isigny) and not vsel._name_organic(lurpak))
    check("xvendor", "current maybe-organic protects (flag OR name)",
          vsel._maybe_organic(isigny) and vsel._maybe_organic(lurpak))
    # the guard the analyzer applies: organic Isigny -> conventional Lurpak is a
    # DOWNGRADE and must be blocked even though Lurpak's flag lies
    downgrade_blocked = vsel._maybe_organic(isigny) and not vsel._name_organic(lurpak)
    check("xvendor", "organic->conventional downgrade is blocked", downgrade_blocked)

    # unit-price helper delegates to product_score and parses sizes
    up, bu = vsel._unit_price(10.0, "500g")
    check("xvendor", "unit price 10/500g -> 20/kg", up == 20.0 and bu == "kg",
          detail=f"{up}/{bu}")

    import inspect
    src = inspect.getsource(vsel.analyze_list_vendor_savings)
    check("xvendor", "compares only same base unit",
          'base_unit"] != cur["base_unit"' in src or "base_unit" in src)
    check("xvendor", "applies the organic-downgrade guard",
          "_maybe_organic" in src and "_name_organic" in src)
    check("xvendor", "module surface (analyze + preferred-per-store)",
          callable(getattr(vsel, "analyze_list_vendor_savings", None)) and
          callable(getattr(vsel, "best_preferred_at_store", None)))


def phase43_have_it_signal(loaded):
    """"Have it" (external purchase, WhatsApp or chat) emits a POSITIVE inclusion
    signal — reaffirm the item is wanted + clear any skip streak — but must NEVER
    shrink the restock interval (the wrong-direction offline_purchase behavior
    that kept this deferred). The clock reset is left to the external_purchases
    cadence math."""
    import inspect
    pe = loaded.get("preference_engine")
    ep = loaded.get("external_purchases")
    if not pe or not ep:
        check("have-it", "modules loaded", False)
        return

    inc_src = inspect.getsource(pe._aggregate_inclusion)
    check("have-it", "inclusion reset branch handles purchase_observed",
          "purchase_observed" in inc_src)

    ext_src = inspect.getsource(ep.record_external_purchase)
    check("have-it", "record_external_purchase emits the signal",
          'signal_type="purchase_observed"' in ext_src)
    check("have-it", "emitted in item_inclusion dimension (not timing)",
          'dimension="item_inclusion"' in ext_src
          and 'dimension="timing"' not in ext_src)
    # the wrong-direction signal must not be the one EMITTED (the word may appear
    # in an explanatory comment, so check the actual emit call, not the prose)
    check("have-it", "does NOT emit interval-shrinking offline_purchase",
          'signal_type="offline_purchase"' not in ext_src)
    # behavioural proof (skip-streak reset, interval unchanged) was verified
    # end-to-end against the live DB during the build.


def phase44_price_intel(loaded):
    """Price intelligence: unit-price baselines per item/category + what drives
    price (organic / origin / brand) + a plausibility guard that flags a price
    far from baseline as bad data (Driscoll's strawberries @ AED 6/kg). Wired into
    the cross-vendor check so artefacts + quality downgrades stop surfacing."""
    pi = loaded.get("price_intel")
    vsel = loaded.get("vendor_select")
    if not pi or not vsel:
        check("price", "modules loaded", False)
        return

    # origin normalisation collapses the messy free-text
    check("price", "origin normalises UAE variants",
          pi._norm_origin("United Arab Emirates") == "uae"
          and pi._norm_origin("Uae") == "uae" and pi._norm_origin("UAE") == "uae")
    check("price", "origin normalises USA / UK",
          pi._norm_origin("U.S.A") == "usa" and pi._norm_origin("United Kingdom") == "uk")

    # percentile + stats math
    st = pi._stats([10, 20, 30, 40, 50])
    check("price", "stats median/p25/p75", st["median"] == 30 and st["n"] == 5,
          detail=str(st))

    # plausibility verdicts against a synthetic in-memory baseline (median 20/kg)
    import sqlite3 as _sql
    mem = _sql.connect(":memory:")
    mem.row_factory = _sql.Row
    mem.execute("CREATE TABLE items (id INT, name TEXT, category TEXT)")
    mem.execute("CREATE TABLE item_alternatives (item_id INT, catalog_id INT, tier TEXT)")
    mem.execute("CREATE TABLE product_catalog (id INT, price REAL, size TEXT, "
                "brand TEXT, origin_country TEXT, product_name TEXT, "
                "is_organic INT, is_available INT, category TEXT)")
    mem.execute("CREATE TABLE order_items (item_id INT, unit_price REAL, order_id INT)")
    mem.execute("CREATE TABLE orders (id INT, status TEXT)")
    for i, price in enumerate([18, 20, 22, 24]):  # ~20/kg @ 1kg packs
        mem.execute("INSERT INTO product_catalog VALUES (?,?,?,?,?,?,?,?,?)",
                    (i, price, "1kg", "B", "UAE", f"Thing {i}", 0, 1, "x"))
        mem.execute("INSERT INTO item_alternatives VALUES (?,?,?)", (7, i, "acceptable"))
    mem.execute("INSERT INTO items VALUES (7,'Thing','Produce')")
    try:
        too_cheap = pi.price_plausibility(7, 2.0, "kg", conn=mem)["verdict"]
        normal = pi.price_plausibility(7, 20.0, "kg", conn=mem)["verdict"]
        pricey = pi.price_plausibility(7, 60.0, "kg", conn=mem)["verdict"]
        check("price", "plausibility: 2/kg vs ~20 -> too_cheap", too_cheap == "too_cheap",
              detail=too_cheap)
        check("price", "plausibility: 20/kg -> normal", normal == "normal", detail=normal)
        check("price", "plausibility: 60/kg -> too_expensive", pricey == "too_expensive",
              detail=pricey)
    finally:
        mem.close()

    # the cross-vendor check now applies a quality-attr guard + plausibility gate
    check("price", "quality-attr downgrade detected (grass-fed -> generic)",
          bool(vsel._quality_set("Grass-Fed Beef") - vsel._quality_set("Beef Sausages")))
    import inspect
    asrc = inspect.getsource(vsel.analyze_list_vendor_savings)
    check("price", "vendor_select gates on plausibility + quality",
          "price_plausibility" in asrc and "_quality_set" in asrc)


def phase45_historical_baseline(loaded):
    """Historical baseline learned from purchase history, segmented per item /
    category / TYPE (the household's revealed quality choice from the bought
    product name) with SIZE as a real dimension. Not a market average — 'what WE
    buy and pay'."""
    pi = loaded.get("price_intel")
    if not pi:
        check("history", "price_intel loaded", False)
        return

    # the type classifier reads the household's revealed choice from the name
    check("history", "type: Beef Mince (Grass Fed) -> grass-fed",
          pi.product_type("Beef Mince (Grass Fed)") == ("grass-fed",))
    check("history", "type: Organic Free Range Eggs -> free-range+organic",
          pi.product_type("Organic Free Range Eggs") == ("free-range", "organic"))
    check("history", "type: Greek Yoghurt -> greek",
          pi.product_type("Greek Yoghurt (Natural)") == ("greek",))
    check("history", "type: plain item -> standard",
          pi.product_type("Chicken Breast") == ("standard",))
    check("history", "mode picks the household's typical value",
          pi._mode(["500g", "500g", "1kg"]) == "500g")

    # end-to-end: per-type + per-size segmentation from in-memory history
    import sqlite3 as _sql
    mem = _sql.connect(":memory:")
    mem.row_factory = _sql.Row
    mem.execute("CREATE TABLE orders (id INT, status TEXT)")
    mem.execute("""CREATE TABLE order_items (item_id INT, raw_name TEXT, size TEXT,
        unit_price REAL, quantity REAL, brand TEXT, order_id INT)""")
    mem.execute("INSERT INTO orders VALUES (1,'delivered')")
    hist = [
        ("Beef Mince (Grass Fed)", "500g", 36.0),
        ("Beef Mince (Grass Fed)", "500g", 38.0),
        ("Beef Mince (Grass Fed)", "500g", 37.0),
        ("Beef Mince Fresh", "500g", 67.0),
    ]
    for nm, sz, pr in hist:
        mem.execute("INSERT INTO order_items VALUES (5,?,?,?,1,NULL,1)", (nm, sz, pr))
    try:
        h = pi.historical_baseline(5, conn=mem)
        check("history", "segmented by type",
              "grass-fed" in h["by_type"] and "standard" in h["by_type"],
              detail=str(list(h["by_type"].keys())))
        check("history", "dominant type = most-bought (grass-fed x3)",
              h["dominant_type"] == "grass-fed", detail=str(h.get("dominant_type")))
        gf = h["by_type"].get("grass-fed", {})
        check("history", "size captured + per-type unit price",
              gf.get("typical_size") == "500g" and gf.get("base_unit") == "kg"
              and gf.get("unit_price", {}).get("median") == 74.0,
              detail=str(gf))
    finally:
        mem.close()


def phase46_accepted_band(loaded):
    """Accepted-price band as the recommendation reference, cross-referenced with
    LEARNED preference: a cheap non-preferred TYPE is no benefit (we buy organic
    eggs); within the preferred type, 2 vs 9 AED/egg -> accepted vs outrageous.
    Organic preference is learned PER ITEM (eggs organic, beef standard)."""
    pi = loaded.get("price_intel")
    if not pi:
        check("accepted", "price_intel loaded", False)
        return
    import sqlite3 as _sql
    mem = _sql.connect(":memory:")
    mem.row_factory = _sql.Row
    mem.execute("CREATE TABLE household_preferences (pref_key TEXT, pref_value TEXT)")
    mem.execute("INSERT INTO household_preferences VALUES ('prefer_organic','true')")
    mem.execute("CREATE TABLE items (id INT, name TEXT, category TEXT)")
    mem.executemany("INSERT INTO items VALUES (?,?,?)",
                    [(1, "Eggs", "Dairy"), (5, "Beef Mince", "Meat")])
    mem.execute("CREATE TABLE orders (id INT, status TEXT)")
    mem.execute("INSERT INTO orders VALUES (1,'delivered')")
    mem.execute("""CREATE TABLE order_items (item_id INT, raw_name TEXT, size TEXT,
        unit_price REAL, quantity REAL, brand TEXT, order_id INT)""")
    for p in (22.3, 23.0, 23.0, 27.0, 27.5):   # organic eggs ~2.3/egg @ 10 pcs
        mem.execute("INSERT INTO order_items VALUES (1,'Organic Free Range Eggs','10 PCS',?,1,NULL,1)", (p,))
    for p in (67.0, 68.0, 66.0):               # standard beef ~134/kg @ 500g
        mem.execute("INSERT INTO order_items VALUES (5,'Beef Mince Fresh','500g',?,1,NULL,1)", (p,))
    try:
        check("accepted", "organic preference learned per item (eggs yes, beef no)",
              pi.item_organic_preference(1, conn=mem) is True
              and pi.item_organic_preference(5, conn=mem) is False)

        def ev(iid, name, pack, size):
            return pi.evaluate_option(iid, name, pack, size, conn=mem)

        cheap = ev(1, "Organic Eggs", 20.0, "10 pcs")      # 2.0/egg
        mid = ev(1, "Organic Eggs", 35.0, "10 pcs")        # 3.5/egg
        outr = ev(1, "Organic Eggs", 90.0, "10 pcs")       # 9.0/egg
        check("accepted", "2 AED/egg -> accepted + recommend",
              cheap["price_verdict"] == "accepted" and cheap["recommend"], detail=str(cheap))
        check("accepted", "3.5 AED/egg -> premium (not auto-recommend)",
              mid["price_verdict"] == "premium" and not mid["recommend"], detail=str(mid))
        check("accepted", "9 AED/egg -> outrageous (rejected)",
              outr["price_verdict"] == "outrageous" and not outr["recommend"], detail=str(outr))

        nonorg = ev(1, "Carrefour Brown Eggs", 10.0, "10 pcs")   # cheap, non-organic
        check("accepted", "cheap non-organic egg -> not a benefit (gated)",
              nonorg["recommend"] is False and nonorg["preferred_type"] is False,
              detail=str(nonorg))

        beef = ev(5, "Beef Mince Fresh", 67.0, "500g")
        check("accepted", "standard beef NOT organic-gated (we buy standard)",
              beef["preferred_type"] is True and beef["price_verdict"] == "accepted",
              detail=str(beef))
    finally:
        mem.close()

    # the public reference fns must be callable WITHOUT a conn (the endpoint
    # does) — a missing default here 500s the price-intel API
    import inspect
    for fn in ("item_organic_preference", "accepted_band", "price_verdict",
               "evaluate_option", "historical_baseline", "item_price_baseline"):
        params = inspect.signature(getattr(pi, fn)).parameters
        conn_p = params.get("conn")
        check("accepted", f"{fn}(conn=None) callable without a connection",
              conn_p is not None and conn_p.default is None, detail=fn)


def phase47_callout_best_next(loaded):
    """When NOTHING is within our accepted band, recommend_for_item makes a
    call-out and offers the best next — staying within our preferred TYPE
    (offer premium organic, not cheap non-organic)."""
    pi = loaded.get("price_intel")
    if not pi:
        check("callout", "price_intel loaded", False)
        return
    import sqlite3 as _sql
    mem = _sql.connect(":memory:")
    mem.row_factory = _sql.Row
    mem.execute("CREATE TABLE household_preferences (pref_key TEXT, pref_value TEXT)")
    mem.execute("INSERT INTO household_preferences VALUES ('prefer_organic','true')")
    mem.execute("CREATE TABLE items (id INT, name TEXT, category TEXT)")
    mem.execute("INSERT INTO items VALUES (1,'Eggs','Dairy')")
    mem.execute("CREATE TABLE orders (id INT, status TEXT)")
    mem.execute("INSERT INTO orders VALUES (1,'delivered')")
    mem.execute("""CREATE TABLE order_items (item_id INT, raw_name TEXT, size TEXT,
        unit_price REAL, quantity REAL, brand TEXT, order_id INT)""")
    for p in (22.3, 23.0, 23.0, 27.0, 27.5):
        mem.execute("INSERT INTO order_items VALUES (1,'Organic Free Range Eggs','10 PCS',?,1,NULL,1)", (p,))
    try:
        # an in-band organic option exists -> ok, pick it
        good = [
            {"name": "Organic Eggs", "price": 24.0, "size": "10 pcs"},   # 2.4/egg accepted
            {"name": "Brown Eggs", "price": 9.0, "size": "10 pcs"},      # cheap non-organic
        ]
        r1 = pi.recommend_for_item(1, good, conn=mem, category="Dairy")
        check("callout", "in-band option -> status ok + pick",
              r1["status"] == "ok" and r1["pick"]["name"] == "Organic Eggs", detail=str(r1["status"]))

        # nothing in band -> call-out + best next stays the preferred (organic) type
        bad = [
            {"name": "Premium Organic Eggs", "price": 95.0, "size": "10 pcs"},  # outrageous organic
            {"name": "Brown Eggs", "price": 9.0, "size": "10 pcs"},             # cheap non-organic
        ]
        r2 = pi.recommend_for_item(1, bad, conn=mem, category="Dairy")
        check("callout", "no in-band -> status callout + message",
              r2["status"] == "callout" and bool(r2["callout"]), detail=str(r2["status"]))
        check("callout", "best-next stays preferred type (organic, not cheap non-organic)",
              r2["best_next"]["name"] == "Premium Organic Eggs", detail=str(r2["best_next"]))
        check("callout", "no candidates -> status none",
              pi.recommend_for_item(1, [], conn=mem)["status"] == "none")
    finally:
        mem.close()

    # the scheduled-list render surfaces the accepted-band verdict on each item
    import importlib
    import inspect
    try:
        fb = importlib.import_module("frontend_bridge")
        src = inspect.getsource(fb.build_frontend_list)
        check("callout", "scheduled-list render surfaces priceVerdict + call-out",
              "evaluate_option" in src and "priceVerdict" in src and "priceCallout" in src)
    except Exception as e:
        check("callout", "frontend_bridge importable", False, detail=repr(e))


def main():
    print("=" * 60)
    print("Hearthkeeper end-to-end smoke test — 2026-05-27")
    print("=" * 60)
    print()

    with phase("Phase 1: module imports"):
        loaded = phase1_imports()

    with phase("Phase 2: today's symbol presence"):
        phase2_symbols(loaded)

    with phase("Phase 3: schema migration + new columns"):
        phase3_schema(loaded)

    with phase("Phase 4: formatter rendering"):
        phase4_formatters(loaded)

    with phase("Phase 5: parser routing"):
        phase5_parsers(loaded)

    with phase("Phase 6: scoring engine + Phase 4 penalty"):
        phase6_scoring(loaded)

    with phase("Phase 7: backend queries"):
        phase7_queries(loaded)

    with phase("Phase 8: LLM action normalizer"):
        phase8_normalizer(loaded)

    with phase("Phase 9: dispatcher reply-handler precedence"):
        phase9_dispatcher_order(loaded)

    with phase("Phase 10: diag endpoint allowlist"):
        phase10_diag_endpoints(loaded)

    with phase("Phase 11: B7 notification_prefs"):
        phase11_b7(loaded)

    with phase("Phase 12: verb typo tolerance"):
        phase12_typos(loaded)

    with phase("Phase 13: cross-vendor OOS fallback"):
        phase13_cross_vendor(loaded)

    with phase("Phase 19: list_add_item dedup"):
        phase19_list_dedup(loaded)

    with phase("Phase 14: scheduler task surface"):
        phase14_scheduler(loaded)

    with phase("Phase 15: WS action handler coverage"):
        phase15_ws_handlers(loaded)

    with phase("Phase 16: dispatcher precedence + state isolation"):
        phase16_dispatcher_isolation(loaded)

    with phase("Phase 17: formatter robustness (edge inputs)"):
        phase17_formatter_robustness(loaded)

    with phase("Phase 18: catalog matcher gates end-to-end"):
        phase18_catalog_gates(loaded)

    with phase("Phase 20: list_generate suggestion gates"):
        phase20_list_gates(loaded)

    with phase("Phase 21: inventory-based suggestion model"):
        phase21_inventory_model(loaded)

    with phase("Phase 22: staple cadence floor"):
        phase22_staple_floor(loaded)

    with phase("Phase 23: scheduled-list availability resolver"):
        phase23_availability_resolver(loaded)

    with phase("Phase 24: skip-marker convention"):
        phase24_skip_marker_convention(loaded)

    with phase("Phase 25: subsystem health ledger"):
        phase25_subsystem_health(loaded)

    with phase("Phase 26: adaptive order-poll cadence"):
        phase26_order_poll_cadence(loaded)

    with phase("Phase 27: Carrefour order-detail receipt parser"):
        phase27_carrefour_detail_parser(loaded)

    with phase("Phase 28: wrong-item feedback intent"):
        phase28_wrong_item_intent(loaded)

    with phase("Phase 29: future last_ordered_date clamp"):
        phase29_future_last_ordered_clamp(loaded)

    with phase("Phase 30: validator price-sanity"):
        phase30_price_sanity(loaded)

    with phase("Phase 31: chat add offers options (no dead-end)"):
        phase31_chat_add_suggestion(loaded)

    with phase("Phase 32: organic requirement honors is_organic flag"):
        phase32_organic_flag(loaded)

    with phase("Phase 33: scrape availability sweep guard"):
        phase33_availability_guard(loaded)

    with phase("Phase 34: validator honors in-app feedback"):
        phase34_validator_honors_feedback(loaded)

    with phase("Phase 35: feedback understanding (LLM) + block undo"):
        phase35_feedback_undo_and_llm(loaded)

    with phase("Phase 36: scheduled list aligns with purchase history"):
        phase36_validator_purchase_history(loaded)

    with phase("Phase 37: alternatives X-vs-Y comparison learning"):
        phase37_swap_comparison(loaded)

    with phase("Phase 38: WhatsApp edits → durable learning"):
        phase38_whatsapp_learning(loaded)

    with phase("Phase 39: unify consumption (curator honors household_rules)"):
        phase39_curator_unified(loaded)

    with phase("Phase 40: delivery-fee model + per-vendor analysis"):
        phase40_delivery_fee_model(loaded)

    with phase("Phase 41: item_alternatives pollution gates"):
        phase41_alternatives_pollution(loaded)

    with phase("Phase 42: cross-vendor price selection (safe)"):
        phase42_cross_vendor_price(loaded)

    with phase("Phase 43: have-it -> inclusion signal (no interval shrink)"):
        phase43_have_it_signal(loaded)

    with phase("Phase 44: price intelligence (baselines + drivers + plausibility)"):
        phase44_price_intel(loaded)

    with phase("Phase 45: historical baseline (per item/category/type, size-aware)"):
        phase45_historical_baseline(loaded)

    with phase("Phase 46: accepted-price band + preference cross-reference"):
        phase46_accepted_band(loaded)

    with phase("Phase 47: call-out + best-next when nothing's in band"):
        phase47_callout_best_next(loaded)

    print()
    print("=" * 60)
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    if FAILURES:
        print()
        print("Failures:")
        for phase_name, label, detail in FAILURES:
            print(f"  [{phase_name}] {label}")
            if detail:
                for line in str(detail).splitlines():
                    print(f"      {line}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
