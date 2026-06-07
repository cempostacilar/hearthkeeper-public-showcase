"""
Hearthkeeper - Feedback Manager

Orchestrator for the WhatsApp household feedback lifecycle.
Manages feedback rounds, routes messages, creates feedback items,
handles approvals/rejections, and tracks timeouts.

State machine:
  Round:  pending → collecting → complete / timeout / cancelled
  Member: pending → notified → replied → done / no_reply
"""

import sqlite3
from datetime import datetime, timedelta

from config import (
    WHATSAPP_ENABLED,
    WHATSAPP_FEEDBACK_TIMEOUT_MIN,
    WHATSAPP_FOLLOWUP_MIN,
)
from db_setup import DB_PATH
from whatsapp_client import wa_client
from whatsapp_agent import (
    process_message,
    format_cart_message,
    format_confirm_add,
    format_confirm_remove,
    format_not_found,
    format_round_done,
    format_no_active_round,
    format_round_closed,
    format_non_text,
    auto_pick_best,
)
from frontend_bridge import build_frontend_list
from app_logging import get_logger, log_event

logger = get_logger("feedback_manager")


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _extract_recommended_option(matched):
    """Extract the recommended catalog option from a curate_basket result."""
    for opt in matched.get("options", []):
        if opt.get("store_id") == matched.get("recommended_store_id"):
            return opt
    return matched["options"][0] if matched.get("options") else None


class FeedbackManager:
    """Manages the full feedback lifecycle for WhatsApp household feedback."""

    # ── Initiate a Feedback Round ──

    async def initiate_feedback(self, list_id: int, timeout_minutes: int = None) -> int | None:
        """
        Start a new feedback round. Sends the cart to all active members.
        Returns round_id, or None if WhatsApp is unavailable.
        """
        if not WHATSAPP_ENABLED:
            logger.warning("WhatsApp disabled — cannot initiate feedback")
            return None

        timeout = timeout_minutes or WHATSAPP_FEEDBACK_TIMEOUT_MIN
        conn = _get_conn()

        try:
            # Cancel any existing active round for this list
            conn.execute(
                """UPDATE feedback_rounds
                   SET status = 'cancelled', completed_at = datetime('now')
                   WHERE list_id = ? AND status IN ('pending', 'collecting')""",
                (list_id,),
            )

            # Create the new round
            deadline = datetime.utcnow() + timedelta(minutes=timeout)
            cur = conn.execute(
                """INSERT INTO feedback_rounds (list_id, status, deadline_at)
                   VALUES (?, 'collecting', ?)""",
                (list_id, deadline.isoformat()),
            )
            round_id = cur.lastrowid

            # Get active members (exclude driver — Cem doesn't need to message himself)
            members = conn.execute(
                "SELECT * FROM household_members WHERE role = 'member' AND is_active = 1"
            ).fetchall()

            conn.commit()
        except Exception as e:
            logger.error("Failed to create feedback round: %s", e)
            conn.close()
            return None

        # Build the cart message
        try:
            list_data = build_frontend_list(list_id)
        except Exception as e:
            logger.error("Failed to build list data for feedback: %s", e)
            conn.close()
            return round_id

        # Determine target day from list
        target_day = None
        try:
            row = conn.execute(
                "SELECT target_date FROM shopping_lists WHERE id = ?", (list_id,)
            ).fetchone()
            if row and row["target_date"]:
                dt = datetime.fromisoformat(row["target_date"])
                target_day = dt.strftime("%A")  # "Friday", "Saturday", etc.
        except Exception:
            pass

        # Send to each member
        for member in members:
            msg = format_cart_message(list_data, member["name"], target_day)
            wa_msg_id = await wa_client.send_message(
                member["phone"], msg,
                round_id=round_id, member_id=member["id"],
            )

            status = "notified" if wa_msg_id else "pending"
            try:
                conn.execute(
                    """INSERT INTO feedback_member_status
                       (round_id, member_id, status, notified_at)
                       VALUES (?, ?, ?, ?)""",
                    (round_id, member["id"], status,
                     datetime.utcnow().isoformat() if wa_msg_id else None),
                )
                conn.commit()
            except Exception as e:
                logger.error("Failed to insert member status: %s", e)

        conn.close()
        log_event("feedback_initiated", source="feedback_manager",
                  detail=f"round={round_id}, list={list_id}, members={len(members)}")
        return round_id

    # ── Handle Incoming Replies ──

    async def handle_member_reply(
        self, member: dict, text: str, message_id: str = None
    ) -> dict:
        """
        Process a WhatsApp message from a household member.
        Returns a result dict with actions taken.
        """
        conn = _get_conn()

        # E4 — Tier-2 ask reply check (BEFORE the round lookup).
        # household_members.member_pending_intent is set by the nightly
        # pattern detector when it sends a "want me to stop suggesting X?"
        # message. That message arrives outside any feedback round, so
        # the round-scoped pending_intent on feedback_member_status can't
        # carry the state -- we use a member-level column instead.
        # If the reply is a recognized Tier-2 keyword (stop/keep/later),
        # we handle it here and return. Anything else (e.g. "skip 3")
        # falls through to the normal round-scoped path; the tier2_ask
        # stays pending until they answer it or it ages out.
        tier2_result = await self._maybe_handle_tier2_reply(member, text, conn)
        if tier2_result is not None:
            conn.close()
            return tier2_result

        # B5 — Delivered-order rating reply check (BEFORE the round
        # lookup). Same shape as the Tier-2 case: scheduler set
        # member_pending_intent to {"type":"rating_pending","order_id":...}
        # when the delivered prompt went out. The reply could be a quick
        # "good"/"bad"/emoji, or substantive free text ("rotten bananas").
        # Anything we can't recognize as a rating reply falls through to
        # the normal round-scoped path.
        rating_result = await self._maybe_handle_rating_reply(member, text, conn)
        if rating_result is not None:
            conn.close()
            return rating_result

        # Find active round + per-member state. pending_intent carries
        # multi-turn state (e.g. "waiting for A/B/C choice after swap").
        round_row = conn.execute(
            """SELECT fr.*, fms.status AS member_status, fms.id AS fms_id,
                      fms.pending_intent
               FROM feedback_rounds fr
               JOIN feedback_member_status fms ON fms.round_id = fr.id
               WHERE fms.member_id = ? AND fr.status = 'collecting'
               ORDER BY fr.initiated_at DESC LIMIT 1""",
            (member["id"],),
        ).fetchone()

        if not round_row:
            # Capture the message even with no active round, so an out-of-round
            # reply (e.g. answering an admin question over WhatsApp) is stored in
            # whatsapp_messages and readable, instead of being silently dropped.
            try:
                wa_client.log_incoming(
                    round_id=None, member_id=member["id"],
                    wa_message_id=message_id, content=text,
                )
            except Exception:
                pass

            # No active round — check if there's a recently closed one
            closed = conn.execute(
                """SELECT status FROM feedback_rounds
                   WHERE id IN (SELECT round_id FROM feedback_member_status WHERE member_id = ?)
                   ORDER BY initiated_at DESC LIMIT 1""",
                (member["id"],),
            ).fetchone()

            conn.close()

            if closed and closed["status"] in ("complete", "timeout"):
                reply = format_round_closed()
            else:
                # Neutral acknowledgement instead of a "no active list" bounce:
                # the message was just captured above and is readable, so a reply
                # to an out-of-round question isn't dismissed with a misleading
                # "nothing to review" message.
                reply = "Got it \U0001f44d — your message is noted."

            await wa_client.send_message(
                member["phone"], reply,
                member_id=member["id"],
            )
            return {"action": "no_round", "reply": reply}

        round_id = round_row["id"]
        list_id = round_row["list_id"]

        # Update member status to "replied" if first reply
        if round_row["member_status"] == "notified":
            conn.execute(
                """UPDATE feedback_member_status
                   SET status = 'replied', replied_at = datetime('now')
                   WHERE id = ?""",
                (round_row["fms_id"],),
            )
            conn.commit()

        # Log the incoming message with round context
        wa_client.log_incoming(
            round_id=round_id,
            member_id=member["id"],
            wa_message_id=message_id,
            content=text,
        )

        # Parse the message through the 3-tier pipeline. Pass the
        # member's phone so the Tier-3 LLM can pull pending swap state
        # and other phone-keyed context when the regex parser can't
        # parse the reply.
        actions = await process_message(
            text, round_id=round_id, phone=member.get("phone"),
        )

        added_items = []
        removed_items = []
        reply_parts = []
        # v2 verb-with-numbers handlers collect items so we can send
        # ONE affirmation per verb at the end (matches "Skipped X, Y, Z").
        skip_confirms = []     # list of (name, ref_number) tuples
        have_confirms = []     # list of (name, ref_number) tuples
        out_of_range_nums = [] # numbers that didn't resolve

        # Multi-turn state: if the member previously asked to swap an
        # item and we showed them A/B/C options, their next reply
        # might be a pick ("A", "B", "2"). Resolve that BEFORE the
        # normal parser so a bare "A" doesn't fall through to unknown.
        pending = None
        try:
            pi = round_row["pending_intent"]
            if pi:
                import json as _json
                pending = _json.loads(pi) if isinstance(pi, str) else pi
        except Exception:
            pending = None

        if pending and pending.get("kind") == "swap_pending":
            from whatsapp_agent import parse_alternative_choice
            choice = parse_alternative_choice(text)
            options = pending.get("options") or []
            if choice is not None and 0 <= choice < len(options):
                # Bare-letter / bare-number pick (A/B/C/1/2/3). Same
                # commit logic as the LLM-driven swap_pick path -- see
                # _perform_swap_commit for the shared implementation.
                # On success: clear pending so the next message doesn't
                # re-resolve to the same swap context.
                swap_committed, ack, _picked = self._perform_swap_commit(
                    pending, choice,
                )
                if swap_committed:
                    conn.execute(
                        "UPDATE feedback_member_status SET pending_intent = NULL WHERE id = ?",
                        (round_row["fms_id"],),
                    )
                    conn.commit()
                    # C1 footer -- running total + next-step prompt.
                    # Only on success; the failure ack already nudges
                    # the user to retry or skip.
                    try:
                        from whatsapp_agent import format_round_status_footer
                        count, total = self._compute_cart_status(conn, list_id)
                        if count > 0:
                            ack = ack + "\n\n" + format_round_status_footer(count, total)
                    except Exception as e:
                        logger.debug("swap footer compute failed: %s", e)
                await wa_client.send_message(
                    member["phone"], ack,
                    round_id=round_id, member_id=member["id"],
                )
                conn.close()
                return {
                    "action": "swap_accepted" if swap_committed else "swap_failed",
                    "reply": ack,
                }
            # Else: not a valid choice -- fall through to normal parser
            # (user might've abandoned the swap and started a new command,
            # OR the LLM may produce a swap_pick action later for free-
            # text like "the cheaper one" -- handled in the action loop).

        # Pre-resolve every number reference against the ORIGINAL list
        # state before we mutate anything. Without this, "skip 1 2 3"
        # on a 3-item list ran into a race: after step 1 skipped item
        # #1, step 2's lookup of "#2" already saw a shrunken visible
        # list (because _resolve_number_ref calls build_frontend_list
        # which queries live state). The user's #3 then resolved as
        # out-of-range even though it was valid at the time they typed.
        # Fix: take one snapshot upfront, look every number up against
        # it; the per-action loop just uses the pre-resolved target.
        pre_resolved_targets = {}
        for _action in actions:
            _ref = _action.get("ref_number")
            if not _ref or _ref in pre_resolved_targets:
                continue
            _target = self._resolve_number_ref(conn, list_id, _ref)
            pre_resolved_targets[_ref] = _target  # may be None (out of range)

        for action in actions:
            action_type = action.get("type")

            # LLM-emitted swap_pick: the user replied to an open
            # swap with free text like "the cheaper one" and Claude
            # resolved the choice against the PENDING STATE block in
            # its prompt. Routes to the same commit path as the
            # bare-letter Tier-1 handler at the top of this method.
            #
            # We do this BEFORE the verb-with-numbers block so a
            # swap_pick can't get confused with a regular swap_request.
            if action_type == "swap_pick":
                if not (pending and pending.get("kind") == "swap_pending"):
                    # LLM claimed swap_pick but there's no pending
                    # swap on the round -- treat as unknown so user
                    # sees the help text.
                    reply_parts.append(
                        "I don't see an open swap to pick from. "
                        "Reply *swap N* on a list item to start one."
                    )
                    log_event(
                        "wa_swap_pick_no_pending", source="feedback_manager",
                        detail=f"member={member.get('name')} text={(text or '')[:80]}",
                        level="warning",
                    )
                    continue
                choice_idx = action.get("choice_index")
                if not isinstance(choice_idx, int):
                    reply_parts.append(
                        "I couldn't tell which option you meant. "
                        "Reply *A*, *B*, or *C*."
                    )
                    continue
                swap_committed, ack, _picked = self._perform_swap_commit(
                    pending, choice_idx,
                )
                if swap_committed:
                    conn.execute(
                        "UPDATE feedback_member_status SET pending_intent = NULL WHERE id = ?",
                        (round_row["fms_id"],),
                    )
                    conn.commit()
                    # C1 footer for LLM-driven swap picks too.
                    try:
                        from whatsapp_agent import format_round_status_footer
                        count, total = self._compute_cart_status(conn, list_id)
                        if count > 0:
                            ack = ack + "\n\n" + format_round_status_footer(count, total)
                    except Exception as e:
                        logger.debug("swap_pick footer compute failed: %s", e)
                await wa_client.send_message(
                    member["phone"], ack,
                    round_id=round_id, member_id=member["id"],
                )
                # Don't append to reply_parts -- ack was already sent.
                continue

            # v2 verb-with-numbers actions (skip 1 3 5, have 2, stop 4,
            # swap 4). All share the "resolve number → list_item →
            # backend action" pattern; the backend action differs per
            # verb.
            #
            # Bug fix 2026-05-26: swap_request was previously missing
            # from this tuple, so every "swap N" message silently
            # fell through to the unknown-intent handler.
            if action_type in ("skip_tier0", "have_it", "stop_suggesting",
                               "swap_request"):
                ref_number = action.get("ref_number")
                # Pull from the upfront snapshot so chained skips don't
                # invalidate each other's references.
                target = pre_resolved_targets.get(ref_number) if ref_number else None
                if not target:
                    out_of_range_nums.append(ref_number)
                    continue
                list_item_id = target.get("list_item_id")
                item_name = target.get("generic_name") or target.get("item_name_text") or f"#{ref_number}"

                if action_type == "skip_tier0":
                    try:
                        from skip_tracking import apply_tier_zero_drop
                        # Mark the list_item skipped + log skip_event + push
                        # next_restock past the next cycle.
                        try:
                            from list_generate import list_update_item
                            list_update_item(int(list_item_id), "skip")
                        except Exception:
                            pass
                        apply_tier_zero_drop(
                            list_item_id=int(list_item_id),
                            source="reviewer_drop",
                            reason="this_week_only",
                            member_id=member["id"],
                        )
                        # A WhatsApp skip is a durable inclusion signal, not
                        # just a one-cycle drop. Feed the learning engine so
                        # repeated family skips accumulate a skip_streak (the
                        # pattern detector stretches cadence / eventually
                        # auto-stops) — mirrors the in-app skip emit so both
                        # surfaces learn identically.
                        skip_item_id = target.get("item_id")
                        if skip_item_id:
                            try:
                                from preference_engine import emit_signal
                                emit_signal(
                                    item_id=int(skip_item_id),
                                    signal_type="item_skip",
                                    signal_source="whatsapp",
                                    dimension="item_inclusion",
                                    new_value={"action": "skip"},
                                    context={"reason": "this_week_only",
                                             "ref": ref_number},
                                )
                            except Exception as e:
                                logger.debug("skip signal emit failed: %s", e)
                        skip_confirms.append((item_name, ref_number))
                    except Exception as e:
                        logger.warning("skip_tier0 dispatch failed: %s", e)
                        reply_parts.append(f"Couldn't skip #{ref_number}, try again.")

                elif action_type == "have_it":
                    try:
                        from external_purchases import record_external_purchase
                        # record_external_purchase has a side-effect that
                        # also marks any active list_item as skipped, so
                        # the visible list updates AND the cadence math
                        # advances (since the row goes into
                        # external_purchases for future cadence calc).
                        record_external_purchase(
                            raw_name=item_name,
                            source="whatsapp",
                            member_id=member["id"],
                        )
                        have_confirms.append((item_name, ref_number))
                    except Exception as e:
                        logger.warning("have_it dispatch failed: %s", e)
                        reply_parts.append(f"Couldn't mark #{ref_number} as have, try again.")

                elif action_type == "stop_suggesting":
                    # 2026-05-26: was previously deferred ("coming soon"
                    # placeholder). Now wired through. We:
                    #   1. Drop the item from this cycle (Tier-0 skip),
                    #      so the user gets immediate satisfaction.
                    #   2. Insert a user_item_prefs(stop_suggesting,
                    #      value='user:wa_command') row so list_generate
                    #      excludes it from future cycles. value='user:*'
                    #      so E6's auto-reset (which only deactivates
                    #      auto:* rows) doesn't undo this on an accidental
                    #      re-approve.
                    #   3. Reuse the auto-stop "tier" reporting so this
                    #      shows up in the A12 panel as a user stop.
                    underlying_item_id = target.get("item_id")
                    try:
                        # 1. Tier-0 drop for this cycle
                        try:
                            from list_generate import list_update_item
                            from skip_tracking import apply_tier_zero_drop
                            list_update_item(int(list_item_id), "skip")
                            apply_tier_zero_drop(
                                list_item_id=int(list_item_id),
                                source="reviewer_drop",
                                reason="user_stopped_suggesting",
                                member_id=member["id"],
                            )
                        except Exception as e:
                            logger.warning("stop_suggesting cycle-drop failed: %s", e)

                        # 2. Persist the stop preference (sticky)
                        if underlying_item_id:
                            existing = conn.execute(
                                """SELECT id, is_active FROM user_item_prefs
                                   WHERE item_id = ? AND pref_type = 'stop_suggesting'
                                   LIMIT 1""",
                                (underlying_item_id,),
                            ).fetchone()
                            if existing:
                                conn.execute(
                                    """UPDATE user_item_prefs
                                       SET is_active = 1, value = 'user:wa_command'
                                       WHERE id = ?""",
                                    (existing["id"],),
                                )
                            else:
                                conn.execute(
                                    """INSERT INTO user_item_prefs
                                           (item_id, pref_type, value, is_active)
                                       VALUES (?, 'stop_suggesting', 'user:wa_command', 1)""",
                                    (underlying_item_id,),
                                )
                            conn.commit()
                        reply_parts.append(
                            f"✓ Stopped suggesting *{item_name}* (#{ref_number}). "
                            f"Approve it on any future list to bring it back."
                        )
                    except Exception as e:
                        logger.warning("stop_suggesting dispatch failed: %s", e)
                        reply_parts.append(
                            f"Couldn't stop suggesting #{ref_number}, try again."
                        )

                elif action_type == "swap_request":
                    # Surface 3 alternatives over WhatsApp; user picks
                    # A/B/C in their next message. Pending state stored
                    # on feedback_member_status.pending_intent so the
                    # multi-turn flow survives across messages.
                    try:
                        from frontend_bridge import find_alternatives_for_item
                        alts = find_alternatives_for_item(
                            item_id=target.get("item_id"),
                            store_id=target.get("store_id"),
                            current_catalog_id=None,
                            scope="same_vendor",
                            conn=conn,
                            limit=3,
                        )
                    except Exception as e:
                        logger.warning("find_alternatives_for_item failed: %s", e)
                        alts = []

                    from whatsapp_agent import format_swap_options
                    swap_msg = format_swap_options(item_name, ref_number, alts)

                    # Persist pending state so reviewer's next reply
                    # ("A" / "B" / "C") gets routed to the swap-accept path.
                    if alts:
                        import json as _json
                        pending_payload = {
                            "kind": "swap_pending",
                            "list_item_id": list_item_id,
                            "item_id": target.get("item_id"),
                            "store_id": target.get("store_id"),
                            "ref_number": ref_number,
                            "generic_name": item_name,
                            "scope": "same_vendor",
                            "options": [
                                {
                                    "catalog_id": a.get("catalogId"),
                                    "name": a.get("name"),
                                }
                                for a in alts[:5]
                            ],
                        }
                        conn.execute(
                            "UPDATE feedback_member_status SET pending_intent = ? WHERE id = ?",
                            (_json.dumps(pending_payload), round_row["fms_id"]),
                        )
                        conn.commit()

                    # Send the options reply standalone (don't batch
                    # with other affirmations -- it's a question, not
                    # an acknowledgement).
                    try:
                        await wa_client.send_message(
                            member["phone"], swap_msg,
                            round_id=round_id, member_id=member["id"],
                        )
                    except Exception as e:
                        logger.warning("swap options send failed: %s", e)
                continue  # don't fall through to add/remove handling

            # "undo" command -- not yet built. Honest message: tell the
            # user what they CAN do instead. Removed the "Reply 'undo'"
            # hint from confirmation messages too so we're not promising
            # what we don't deliver.
            if action_type == "undo":
                reply_parts.append(
                    "Undo isn't built yet. To bring an item back, reply "
                    "*add <name>* (e.g. `add yogurt`) -- it'll re-resolve "
                    "to a real product. Or wait for the next list and it'll "
                    "come around naturally."
                )
                continue

            if action_type == "confirm":
                # "No changes" / thumbs up — mark as done
                conn.execute(
                    "UPDATE feedback_member_status SET status = 'done' WHERE id = ?",
                    (round_row["fms_id"],),
                )
                conn.commit()
                reply = format_round_done(member["name"])
                await wa_client.send_message(
                    member["phone"], reply,
                    round_id=round_id, member_id=member["id"],
                )
                conn.close()
                self._check_round_complete(round_id)
                return {"action": "confirm", "reply": reply}

            elif action_type == "done":
                conn.execute(
                    "UPDATE feedback_member_status SET status = 'done' WHERE id = ?",
                    (round_row["fms_id"],),
                )
                conn.commit()
                reply = format_round_done(member["name"])
                await wa_client.send_message(
                    member["phone"], reply,
                    round_id=round_id, member_id=member["id"],
                )
                conn.close()
                self._check_round_complete(round_id)
                return {"action": "done", "reply": reply}

            elif action_type == "add":
                item_text = action.get("item", "")
                qty = action.get("qty", 1)
                resolved = action.get("resolved")

                if resolved:
                    # Good match — create feedback item with full product details
                    fi_id = self._create_feedback_item(
                        conn, round_id, member["id"], "add", item_text,
                        resolved_name=resolved.get("product_name"),
                        catalog_id=resolved.get("catalog_id"),
                        store_id=resolved.get("store_id"),
                        item_id=resolved.get("item_id"),
                        quantity=qty,
                        price=resolved.get("price"),
                    )
                    added_items.append({
                        "id": fi_id,
                        "item_text": item_text,
                        "resolved_name": resolved.get("product_name"),
                        "price": resolved.get("price"),
                    })
                else:
                    # No match — create unresolved feedback item
                    fi_id = self._create_feedback_item(
                        conn, round_id, member["id"], "add", item_text,
                        quantity=qty, status="unresolved",
                    )
                    reply_parts.append(format_not_found(item_text))

            elif action_type == "remove":
                item_text = action.get("item")
                ref_number = action.get("ref_number")

                if ref_number:
                    # Number reference — look up from the numbered list
                    target = self._resolve_number_ref(conn, list_id, ref_number)
                    if target:
                        fi_id = self._create_feedback_item(
                            conn, round_id, member["id"], "remove",
                            target["item_name_text"] or f"Item #{ref_number}",
                            item_id=target.get("item_id"),
                            store_id=target.get("store_id"),
                        )
                        removed_items.append({
                            "id": fi_id,
                            "item_text": target["item_name_text"] or f"Item #{ref_number}",
                            "resolved_name": target["item_name_text"],
                        })
                    else:
                        reply_parts.append(f"I couldn't find item #{ref_number} on the list.")
                elif item_text:
                    # Text-based remove — try to match against current list
                    target = self._find_in_list(conn, list_id, item_text)
                    if target:
                        fi_id = self._create_feedback_item(
                            conn, round_id, member["id"], "remove", item_text,
                            resolved_name=target.get("item_name_text"),
                            item_id=target.get("item_id"),
                            store_id=target.get("store_id"),
                        )
                        removed_items.append({
                            "id": fi_id,
                            "item_text": item_text,
                            "resolved_name": target["item_name_text"],
                        })
                    else:
                        reply_parts.append(
                            f"{item_text} isn't on the current list, so nothing to remove."
                        )

            elif action_type == "needs_number":
                # User typed a command verb (skip/remove/stop/swap/have)
                # without a number. Render a verb-specific hint so they
                # know they're SO CLOSE to the right command -- don't
                # fall through to generic help or silent fuzzy-add.
                from whatsapp_agent import format_needs_number
                verb = action.get("verb", "skip")
                reply_parts.append(format_needs_number(verb))
                log_event(
                    "wa_needs_number", source="feedback_manager",
                    detail=f"member={member.get('name')} verb={verb} text={(text or '')[:80]}",
                )

            elif action_type == "budget_skipped":
                # The LLM fallback was gated out because we hit the
                # daily / monthly cap. Surface this AS the cause so
                # the user (and we) know to differentiate "couldn't
                # parse" from "no AI budget left".
                from whatsapp_agent import format_budget_exceeded
                reason = action.get("reason", "daily_cap")
                reply_parts.append(format_budget_exceeded(reason))
                log_event(
                    "wa_budget_skipped_reply",
                    source="feedback_manager",
                    detail=f"member={member.get('name')} reason={reason} text={(text or '')[:80]}",
                    level="warning",
                )

            elif action_type == "external_purchase":
                # D4 -- LLM detected the user reporting an item they
                # already bought OUTSIDE the cart (gas-station, Lulu,
                # mall trip). Same backend handler the in-app chat
                # uses; sets source='whatsapp' so cadence stats can
                # attribute the signal.
                item_text = (action.get("item") or "").strip()
                qty = action.get("qty") or 1
                if not item_text:
                    continue
                try:
                    from external_purchases import record_external_purchase
                    rec = record_external_purchase(
                        raw_name=item_text,
                        quantity=qty,
                        source="whatsapp",
                        member_id=member["id"],
                    )
                except Exception as e:
                    logger.warning("external_purchase dispatch failed: %s", e)
                    reply_parts.append(
                        f"Couldn't log that purchase: {str(e)[:60]}. "
                        f"Try again or use *have N* on the list."
                    )
                    continue
                if rec and rec.get("item_id"):
                    resolved = rec.get("resolved_name") or item_text
                    pdate = rec.get("purchase_date") or "today"
                    skipped = rec.get("skipped_list_items") or []
                    extra = (
                        " I've also dropped it from the current list."
                        if skipped else ""
                    )
                    reply_parts.append(
                        f"✓ Logged *{resolved}* as bought on {pdate}.{extra} "
                        f"Won't suggest until next natural cycle."
                    )
                    log_event(
                        "wa_external_purchase", source="feedback_manager",
                        detail=(
                            f"member={member.get('name')} item={resolved} "
                            f"qty={qty} skipped_li={skipped}"
                        ),
                    )
                elif rec:
                    # Recorded raw_name but couldn't resolve to a
                    # tracked item -- still saved so the curator can
                    # see it on the diag, but cadence won't move.
                    reply_parts.append(
                        f"Noted that you bought *{item_text}*, but it's "
                        f"not on your tracked items list -- it won't "
                        f"affect future suggestions until you add it as "
                        f"a habit."
                    )
                    log_event(
                        "wa_external_purchase_unresolved",
                        source="feedback_manager",
                        detail=f"member={member.get('name')} raw={item_text[:60]}",
                    )

            elif action_type == "unknown":
                # v2 help text: matches the Quick Replies block we send
                # with format_cart_message. Single source of vocabulary.
                reply_parts.append(
                    "I'm not sure what you mean. Try:\n"
                    "  *skip 3* — drop item #3 this cycle\n"
                    "  *have 3* — already have it (skip + advance cadence)\n"
                    "  *swap 3* — show me 3 alternatives\n"
                    "  *more 5* / *less 5* — change quantity\n"
                    "  *add yogurt* — request a new item\n"
                    "  *good* — looks fine, send to stores\n\n"
                    "_Multiple items? Chain numbers: 'skip 3 5 7'._"
                )

        # Build reply.
        # v2 verb confirmations go FIRST so the user sees what
        # actually happened before any "couldn't add" / "I'm not sure"
        # noise from secondary actions.
        v2_parts = []
        if skip_confirms:
            from whatsapp_agent import format_skip_confirm
            v2_parts.append(format_skip_confirm(skip_confirms))
        if have_confirms:
            from whatsapp_agent import format_have_it_confirm
            v2_parts.append(format_have_it_confirm(have_confirms))
        if out_of_range_nums:
            from whatsapp_agent import format_out_of_range
            max_n = self._count_numbered_items(conn, list_id)
            # One line per out-of-range number rather than spamming N messages
            for n in out_of_range_nums:
                if n is not None:
                    v2_parts.append(format_out_of_range(n, max_n))

        for part in reversed(v2_parts):
            reply_parts.insert(0, part)

        if added_items:
            reply_parts.insert(0, format_confirm_add(added_items))
        if removed_items:
            insert_idx = sum(1 for p in v2_parts) + (1 if added_items else 0)
            reply_parts.insert(insert_idx, format_confirm_remove(removed_items))

        reply = "\n\n".join(reply_parts) if reply_parts else format_confirm_add(added_items)

        # C1: append a single cart-status footer after the bundled
        # confirmations so the reviewer always sees the running total
        # + an explicit "Anything else?" prompt -- instead of every
        # formatter pasting its own variant. Only attach when an
        # action actually mutated the cart; pure-error replies (e.g.
        # "I'm not sure what you mean") shouldn't pretend a change
        # was made.
        any_cart_change = bool(
            skip_confirms or have_confirms or added_items or removed_items
        )
        if reply and any_cart_change:
            try:
                from whatsapp_agent import format_round_status_footer
                count, total = self._compute_cart_status(conn, list_id)
                if count > 0:
                    reply = reply + "\n\n" + format_round_status_footer(count, total)
            except Exception as e:
                logger.debug("round_status_footer compute failed: %s", e)

        if reply:
            await wa_client.send_message(
                member["phone"], reply,
                round_id=round_id, member_id=member["id"],
            )

        conn.commit()
        conn.close()

        return {
            "action": "items",
            "added": added_items,
            "removed": removed_items,
            "reply": reply,
        }

    # ── Approve / Reject Feedback Items ──

    def approve_feedback_item(self, feedback_item_id: int, list_id: int) -> int | None:
        """
        Approve a feedback item — adds it to the shopping list.
        Returns the new list_item_id, or None on failure.
        """
        conn = _get_conn()
        fi = conn.execute(
            "SELECT * FROM feedback_items WHERE id = ?", (feedback_item_id,)
        ).fetchone()

        if not fi:
            conn.close()
            return None

        if fi["action_type"] == "add":
            # Get member name for added_by
            member = conn.execute(
                "SELECT name FROM household_members WHERE id = ?", (fi["member_id"],)
            ).fetchone()
            added_by = member["name"].lower() if member else "feedback"

            # Resolve product details — use stored data or retry via curator
            resolved_name = fi["resolved_name"]
            catalog_id = fi["catalog_id"]
            store_id = fi["store_id"]
            item_id = fi["item_id"]
            price = fi["price"]
            category = None

            # If unresolved or missing catalog_id, retry matching via curator
            if not catalog_id:
                try:
                    from basket_curator import curate_basket
                    curator_result = curate_basket([fi["item_text"]])
                    curator_items = curator_result.get("items", [])
                    if curator_items and curator_items[0].get("status") not in ("unmatched", "needs_input"):
                        matched = curator_items[0]
                        rec_opt = _extract_recommended_option(matched)
                        if rec_opt:
                            resolved_name = matched.get("recommended_product") or rec_opt.get("product_name")
                            catalog_id = rec_opt.get("catalog_id")
                            store_id = matched.get("recommended_store_id") or rec_opt.get("store_id")
                            item_id = matched.get("matched_item_id")
                            price = matched.get("recommended_price") or rec_opt.get("price")
                            category = rec_opt.get("category")
                            logger.info("Curator resolved '%s' → '%s' (catalog=%s)",
                                        fi["item_text"], resolved_name, catalog_id)
                except Exception as e:
                    logger.warning("Curator retry failed for '%s': %s", fi["item_text"], e)

            # Try to resolve item_id from catalog product name when missing
            if not item_id and catalog_id:
                cat_row = conn.execute(
                    "SELECT product_name FROM product_catalog WHERE id = ?", (catalog_id,)
                ).fetchone()
                if cat_row:
                    # Fuzzy match: look for an items entry whose name matches the catalog product
                    item_row = conn.execute(
                        """SELECT id FROM items
                           WHERE LOWER(name) = LOWER(?)
                           LIMIT 1""",
                        (cat_row["product_name"],)
                    ).fetchone()
                    if item_row:
                        item_id = item_row["id"]
                        logger.info("Resolved item_id=%d from catalog product '%s'",
                                    item_id, cat_row["product_name"])

            # Get category from catalog if we have catalog_id but no category yet
            if catalog_id and not category:
                cat_row = conn.execute(
                    "SELECT category FROM product_catalog WHERE id = ?", (catalog_id,)
                ).fetchone()
                category = cat_row["category"] if cat_row else None

            from list_generate import list_add_item
            list_item_id = list_add_item(
                list_id,
                resolved_name or fi["item_text"],
                store_id=store_id,
                item_id=item_id,
                quantity=fi["quantity"] or 1,
                estimated_price=price,
                catalog_id=catalog_id,
                category_override=category,
                status="approved",
            )

            if list_item_id:
                # Update added_by to member name (list_add_item hardcodes 'user')
                conn.execute(
                    "UPDATE list_items SET added_by = ?, feedback_item_id = ? WHERE id = ?",
                    (added_by, feedback_item_id, list_item_id),
                )
                # Mark feedback item as approved
                conn.execute(
                    """UPDATE feedback_items
                       SET status = 'approved', list_item_id = ?, resolved_at = datetime('now')
                       WHERE id = ?""",
                    (list_item_id, feedback_item_id),
                )
                conn.commit()
                conn.close()

                log_event("feedback_approved", source="feedback_manager",
                          detail=f"fi={feedback_item_id} → li={list_item_id}")
                return list_item_id

        elif fi["action_type"] == "remove":
            # Find and remove the item from the list
            target_item_id = fi["item_id"]
            if target_item_id:
                conn.execute(
                    """UPDATE list_items SET status = 'skipped', user_action = 'removed'
                       WHERE list_id = ? AND item_id = ? AND status != 'skipped'""",
                    (list_id, target_item_id),
                )
            # Mark feedback item as approved
            conn.execute(
                """UPDATE feedback_items
                   SET status = 'approved', resolved_at = datetime('now')
                   WHERE id = ?""",
                (feedback_item_id,),
            )
            conn.commit()
            conn.close()

            log_event("feedback_approved", source="feedback_manager",
                      detail=f"fi={feedback_item_id} (remove)")
            return -1  # Indicates removal, not a new list_item

        conn.close()
        return None

    def reject_feedback_item(self, feedback_item_id: int):
        """Reject a feedback suggestion."""
        conn = _get_conn()
        conn.execute(
            """UPDATE feedback_items
               SET status = 'rejected', resolved_at = datetime('now')
               WHERE id = ?""",
            (feedback_item_id,),
        )
        conn.commit()
        conn.close()
        log_event("feedback_rejected", source="feedback_manager",
                  detail=f"fi={feedback_item_id}")

    # ── Round Management ──

    def cancel_round(self, round_id: int):
        """Cancel a feedback round."""
        conn = _get_conn()
        conn.execute(
            """UPDATE feedback_rounds
               SET status = 'cancelled', completed_at = datetime('now')
               WHERE id = ?""",
            (round_id,),
        )
        conn.commit()
        conn.close()
        log_event("feedback_cancelled", source="feedback_manager",
                  detail=f"round={round_id}")

    async def check_timeouts(self):
        """
        Background task: check for expired rounds and send follow-ups.
        Called every 5 minutes by the scheduler.
        """
        conn = _get_conn()
        now = datetime.utcnow()

        # Find active rounds
        rounds = conn.execute(
            "SELECT * FROM feedback_rounds WHERE status = 'collecting'"
        ).fetchall()

        for rnd in rounds:
            deadline = datetime.fromisoformat(rnd["deadline_at"]) if rnd["deadline_at"] else None
            if not deadline:
                continue

            # Check if round expired
            if now >= deadline:
                conn.execute(
                    """UPDATE feedback_rounds
                       SET status = 'timeout', completed_at = datetime('now')
                       WHERE id = ?""",
                    (rnd["id"],),
                )
                # Mark non-replied members as no_reply
                conn.execute(
                    """UPDATE feedback_member_status
                       SET status = 'no_reply'
                       WHERE round_id = ? AND status IN ('pending', 'notified')""",
                    (rnd["id"],),
                )
                conn.commit()
                log_event("feedback_timeout", source="feedback_manager",
                          detail=f"round={rnd['id']}")
                continue

            # Check if follow-up needed (30 min before deadline)
            followup_threshold = deadline - timedelta(minutes=WHATSAPP_FOLLOWUP_MIN)
            if now >= followup_threshold and rnd["followup_count"] == 0:
                # Send follow-up to non-replied members
                pending = conn.execute(
                    """SELECT fms.*, hm.name, hm.phone
                       FROM feedback_member_status fms
                       JOIN household_members hm ON hm.id = fms.member_id
                       WHERE fms.round_id = ? AND fms.status IN ('notified')
                         AND fms.followup_sent = 0""",
                    (rnd["id"],),
                ).fetchall()

                remaining_min = int((deadline - now).total_seconds() / 60)
                for member in pending:
                    msg = (
                        f"Hey {member['name']}, the grocery list closes in "
                        f"{remaining_min} minutes. Any changes?\n"
                        f"Reply 'no changes' if all good."
                    )
                    await wa_client.send_message(
                        member["phone"], msg,
                        round_id=rnd["id"], member_id=member["member_id"],
                    )
                    conn.execute(
                        "UPDATE feedback_member_status SET followup_sent = 1 WHERE id = ?",
                        (member["id"],),
                    )

                conn.execute(
                    "UPDATE feedback_rounds SET followup_count = followup_count + 1 WHERE id = ?",
                    (rnd["id"],),
                )
                conn.commit()
                log_event("feedback_followup", source="feedback_manager",
                          detail=f"round={rnd['id']}, sent to {len(pending)} members")

        conn.close()

    # ── Query Methods ──

    def get_round_status(self, round_id: int = None, list_id: int = None) -> dict | None:
        """
        Get the current status of a feedback round.
        Look up by round_id or list_id (latest active round).
        """
        conn = _get_conn()

        if round_id:
            rnd = conn.execute(
                "SELECT * FROM feedback_rounds WHERE id = ?", (round_id,)
            ).fetchone()
        elif list_id:
            rnd = conn.execute(
                """SELECT * FROM feedback_rounds
                   WHERE list_id = ?
                   ORDER BY initiated_at DESC LIMIT 1""",
                (list_id,),
            ).fetchone()
        else:
            conn.close()
            return None

        if not rnd:
            conn.close()
            return None

        # Get member statuses
        members = conn.execute(
            """SELECT fms.*, hm.name, hm.display_name
               FROM feedback_member_status fms
               JOIN household_members hm ON hm.id = fms.member_id
               WHERE fms.round_id = ?""",
            (rnd["id"],),
        ).fetchall()

        # Count pending feedback items
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM feedback_items WHERE round_id = ? AND status = 'pending'",
            (rnd["id"],),
        ).fetchone()[0]

        conn.close()

        # Calculate remaining time
        remaining_min = None
        if rnd["deadline_at"] and rnd["status"] == "collecting":
            deadline = datetime.fromisoformat(rnd["deadline_at"])
            remaining = (deadline - datetime.utcnow()).total_seconds()
            remaining_min = max(0, int(remaining / 60))

        return {
            "roundId": rnd["id"],
            "listId": rnd["list_id"],
            "status": rnd["status"],
            "remainingMin": remaining_min,
            "pendingCount": pending_count,
            "members": [
                {
                    "name": m["display_name"] or m["name"],
                    "status": m["status"],
                    "itemCount": self._member_item_count(rnd["id"], m["member_id"]),
                }
                for m in members
            ],
        }

    def get_feedback_items(self, round_id: int = None, list_id: int = None) -> list[dict]:
        """Get feedback items for a round, for the frontend."""
        conn = _get_conn()

        if not round_id and list_id:
            rnd = conn.execute(
                """SELECT id FROM feedback_rounds
                   WHERE list_id = ?
                   ORDER BY initiated_at DESC LIMIT 1""",
                (list_id,),
            ).fetchone()
            round_id = rnd["id"] if rnd else None

        if not round_id:
            conn.close()
            return []

        items = conn.execute(
            """SELECT fi.*, hm.name AS member_name, hm.display_name,
                      pc.image_url, pc.product_name AS catalog_name
               FROM feedback_items fi
               JOIN household_members hm ON hm.id = fi.member_id
               LEFT JOIN product_catalog pc ON pc.id = fi.catalog_id
               WHERE fi.round_id = ?
               ORDER BY fi.created_at DESC""",
            (round_id,),
        ).fetchall()

        conn.close()

        return [
            {
                "id": item["id"],
                "memberName": item["display_name"] or item["member_name"],
                "actionType": item["action_type"],
                "itemText": item["item_text"],
                "resolvedName": item["resolved_name"] or item["catalog_name"],
                "image": item["image_url"],
                "price": item["price"],
                "quantity": item["quantity"],
                "status": item["status"],
                "storeId": item["store_id"],
            }
            for item in items
        ]

    # ── Private Helpers ──

    def _create_feedback_item(
        self, conn, round_id, member_id, action_type, item_text,
        resolved_name=None, catalog_id=None, item_id=None, store_id=None,
        quantity=1, price=None, status="pending",
    ) -> int:
        """Insert a feedback item and return its ID."""
        cur = conn.execute(
            """INSERT INTO feedback_items
               (round_id, member_id, action_type, item_text, resolved_name,
                catalog_id, item_id, store_id, quantity, price, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (round_id, member_id, action_type, item_text, resolved_name,
             catalog_id, item_id, store_id, quantity, price, status),
        )
        conn.commit()
        return cur.lastrowid

    def _check_round_complete(self, round_id: int):
        """Check if all members are done and mark round complete if so."""
        conn = _get_conn()
        pending = conn.execute(
            """SELECT COUNT(*) FROM feedback_member_status
               WHERE round_id = ? AND status NOT IN ('done', 'no_reply')""",
            (round_id,),
        ).fetchone()[0]

        if pending == 0:
            conn.execute(
                """UPDATE feedback_rounds
                   SET status = 'complete', completed_at = datetime('now')
                   WHERE id = ?""",
                (round_id,),
            )
            conn.commit()
            log_event("feedback_complete", source="feedback_manager",
                      detail=f"round={round_id} — all members responded")

        conn.close()

    def _member_item_count(self, round_id: int, member_id: int) -> int:
        """Count feedback items from a specific member in a round."""
        conn = _get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM feedback_items WHERE round_id = ? AND member_id = ?",
            (round_id, member_id),
        ).fetchone()[0]
        conn.close()
        return count

    def _perform_swap_commit(self, pending: dict, choice_index: int):
        """Execute a swap_pending pick. Used by BOTH the bare-letter
        Tier-1 path (top of handle_member_reply) and the LLM-emitted
        swap_pick action (free-text pick like "the cheaper one"
        resolved by Claude against the listed A/B/C options).

        Returns:
          (swap_committed: bool, ack_text: str, picked_option: dict)
          where ack_text is ready to send to the user verbatim. Caller
          is responsible for clearing pending_intent on success and
          for sending the ack.

        Implementation mirrors the original inline block at the top
        of handle_member_reply; extracted here so both code paths
        stay consistent.
        """
        from whatsapp_agent import format_swap_accepted
        options = pending.get("options") or []
        if not (0 <= choice_index < len(options)):
            return False, "Couldn't find that option to swap to.", None
        picked = options[choice_index]
        catalog_id = picked.get("catalog_id") or picked.get("catalogId")
        list_item_id = pending.get("list_item_id")
        generic = (
            pending.get("generic_name")
            or pending.get("name")
            or "the item"
        )
        ref_number = pending.get("ref_number")

        swap_committed = False
        swap_error = None
        swap_item_id = None
        swap_chosen = None
        swap_original = None
        swap_category = None
        try:
            with _get_conn() as swap_conn:
                swap_conn.row_factory = sqlite3.Row
                prod = swap_conn.execute(
                    "SELECT product_name, price, store_id, brand, is_organic, "
                    "       origin_country FROM product_catalog WHERE id = ?",
                    (catalog_id,),
                ).fetchone()
                li = swap_conn.execute(
                    "SELECT li.item_id, li.store_id, li.catalog_id AS orig_catalog_id, "
                    "       i.category AS category "
                    "FROM list_items li LEFT JOIN items i ON i.id = li.item_id "
                    "WHERE li.id = ?",
                    (list_item_id,),
                ).fetchone()
                if not prod:
                    swap_error = "catalog product not found"
                elif not li:
                    swap_error = "list item not found"
                else:
                    # Capture X (chosen) + Y (original) BEFORE the UPDATE
                    # overwrites list_items.catalog_id, so we can learn WHY
                    # the family swapped (brand / organic / cheaper) — the
                    # same X-vs-Y comparison the in-app alternatives sheet
                    # runs. WhatsApp edits are a strong preference signal.
                    swap_item_id = li["item_id"]
                    swap_category = li["category"]
                    swap_chosen = dict(prod)
                    orig_cat_id = li["orig_catalog_id"]
                    if orig_cat_id and orig_cat_id != catalog_id:
                        y = swap_conn.execute(
                            "SELECT product_name, price, store_id, brand, is_organic, "
                            "       origin_country FROM product_catalog WHERE id = ?",
                            (orig_cat_id,),
                        ).fetchone()
                        if y:
                            swap_original = dict(y)
                    scope = pending.get("scope", "same_vendor")
                    new_store_id = (
                        prod["store_id"] if scope == "other_vendors"
                        else li["store_id"]
                    )
                    swap_conn.execute(
                        """UPDATE list_items SET catalog_id=?, store_id=?,
                               estimated_price=?, item_name_text=?,
                               status='approved', user_action='approved'
                           WHERE id=?""",
                        (catalog_id, new_store_id, prod["price"],
                         prod["product_name"], list_item_id),
                    )
                    # Pin the choice so the curator learns this preference
                    if li["item_id"]:
                        swap_conn.execute(
                            """INSERT INTO user_item_prefs (item_id, pref_type, value, is_active)
                               VALUES (?, 'pin_product', ?, 1)""",
                            (li["item_id"], str(catalog_id)),
                        )
                    swap_conn.commit()
                    swap_committed = True
        except Exception as e:
            logger.warning("swap acceptance failed: %s", e)
            swap_error = str(e)[:120]

        # Generalize the swap into DURABLE preferences both surfaces honor
        # (household_rule for the validator + preference_state signal for the
        # curator). Done after the swap connection closes — learn_from_swap
        # opens its own. The SKU pin above locks the exact choice; this learns
        # the reason behind it so future suggestions move the right direction.
        if swap_committed and swap_item_id and swap_original is not None:
            try:
                from household_rules import learn_from_swap
                learned = learn_from_swap(
                    swap_item_id, swap_chosen, swap_original,
                    swap_category, signal_source="whatsapp",
                )
                if learned:
                    log_event(
                        "wa_swap_learned", source="feedback_manager",
                        detail=f"item={swap_item_id} {', '.join(learned)}",
                    )
            except Exception as e:
                logger.warning("swap learning failed: %s", e)

        if swap_committed:
            ack = format_swap_accepted(
                generic, picked.get("name", "your pick"), ref_number,
            )
            log_event(
                "wa_swap_accepted", source="feedback_manager",
                detail=f"item={generic} #{ref_number} catalog={catalog_id}",
            )
        else:
            ack = (
                f"⚠️ Couldn't complete the swap for *{generic}* "
                f"(#{ref_number}). "
                f"{('Reason: ' + swap_error + '. ') if swap_error else ''}"
                f"Try again, pick a different letter, or "
                f"reply 'skip {ref_number}' to drop it."
            )
            log_event(
                "wa_swap_failed", source="feedback_manager",
                detail=f"item={generic} #{ref_number} reason={swap_error}",
                level="warning",
            )
        return swap_committed, ack, picked

    def _resolve_number_ref(self, conn, list_id: int, ref_number: int) -> dict | None:
        """
        Resolve a number reference (e.g., "remove 3") to a list item.

        IMPORTANT: must use the same item ordering as format_cart_message,
        otherwise "skip 3" refers to a different item than the reviewer
        sees on their phone.

        Uses build_frontend_list() which is the single source of truth
        for the WhatsApp v2 numbering (vendors sorted Carrefour ->
        Kibsons -> others; items per vendor in catalog order).
        """
        try:
            from frontend_bridge import build_frontend_list
            data = build_frontend_list(list_id, conn=conn)
        except Exception as e:
            logger.warning("build_frontend_list failed in _resolve_number_ref: %s", e)
            return None

        n = 0
        for vendor in data.get("vendors", []):
            for category in vendor.get("categories", []):
                for item in category.get("items", []):
                    if item.get("quantity", 1) <= 0:
                        continue
                    n += 1
                    if n == ref_number:
                        return {
                            "id": item.get("id"),                  # list_item_id
                            "list_item_id": item.get("id"),
                            "item_id": item.get("itemId"),
                            "store_id": item.get("storeId"),
                            "item_name_text": item.get("genericName") or item.get("name"),
                            "name": item.get("name"),
                            "generic_name": item.get("genericName"),
                        }
        return None

    async def _maybe_handle_tier2_reply(self, member: dict, text: str, conn) -> dict | None:
        """E4 — Handle a reply to a Tier-2 "want me to stop?" ask.

        Returns a result dict if the reply was a Tier-2 keyword AND the
        member has a pending tier2_ask. Returns None to let the caller
        fall through to the normal round-scoped handler.

        Three decisions:
          stop  -> INSERT user_item_prefs(stop_suggesting, value='user:tier2')
                   for the item, clear member_pending_intent. Future
                   suggestion lists will exclude it.
          keep  -> reset restock_rules.cadence_adjustment to 1.0 for
                   the item, clear member_pending_intent. The drop
                   counter effectively resets; the next dropped event
                   starts the ladder over from scratch.
          later -> clear member_pending_intent + delete tier2_asked
                   pref so we'll re-ask next time the pattern fires.
        """
        try:
            pi = conn.execute(
                "SELECT member_pending_intent FROM household_members WHERE id = ?",
                (member["id"],),
            ).fetchone()
        except Exception:
            return None
        if not pi or not pi["member_pending_intent"]:
            return None
        try:
            import json as _json
            pending = _json.loads(pi["member_pending_intent"])
        except Exception:
            return None
        if not isinstance(pending, dict) or pending.get("type") != "tier2_ask":
            return None

        from whatsapp_agent import parse_tier2_reply, format_tier2_resolved
        decision = parse_tier2_reply(text)
        if decision is None:
            # Not a Tier-2 keyword -- let the regular handler take it.
            # The tier2_ask stays pending so a follow-up keyword reply
            # still works.
            return None

        item_id = pending.get("item_id")
        item_name = pending.get("item_name") or f"item#{item_id}"

        try:
            if decision == "stop":
                # Insert a user-confirmed stop. Use value='user:tier2' so
                # list_update_item's E6 reset (which only deactivates
                # value LIKE 'auto:%') won't undo this on a future
                # accidental approve.
                existing = conn.execute(
                    """SELECT id, is_active, value FROM user_item_prefs
                       WHERE item_id = ? AND pref_type = 'stop_suggesting'
                       LIMIT 1""",
                    (item_id,),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE user_item_prefs
                           SET is_active = 1, value = 'user:tier2'
                           WHERE id = ?""",
                        (existing["id"],),
                    )
                else:
                    conn.execute(
                        """INSERT INTO user_item_prefs
                               (item_id, pref_type, value, is_active)
                           VALUES (?, 'stop_suggesting', 'user:tier2', 1)""",
                        (item_id,),
                    )
            elif decision == "keep":
                # Reset the soft-nudge cadence; clear any auto-stop.
                conn.execute(
                    """UPDATE restock_rules
                       SET cadence_adjustment = 1.0,
                           updated_at = datetime('now')
                       WHERE item_id = ? AND cadence_adjustment > 1.0""",
                    (item_id,),
                )
                conn.execute(
                    """UPDATE user_item_prefs
                       SET is_active = 0
                       WHERE item_id = ?
                         AND pref_type = 'stop_suggesting'
                         AND is_active = 1
                         AND value LIKE 'auto:%'""",
                    (item_id,),
                )
                # Also clear the tier2_asked marker so the next time
                # the pattern fires we can ask again from a clean slate.
                conn.execute(
                    """UPDATE user_item_prefs
                       SET is_active = 0
                       WHERE item_id = ?
                         AND pref_type = 'tier2_asked'""",
                    (item_id,),
                )
            elif decision == "later":
                # Clear the tier2_asked marker so we ask again next time.
                # cadence_adjustment stays where the pattern detector
                # put it (1.5 for firmer tier).
                conn.execute(
                    """UPDATE user_item_prefs
                       SET is_active = 0
                       WHERE item_id = ?
                         AND pref_type = 'tier2_asked'""",
                    (item_id,),
                )

            # Clear the pending intent so the next reply isn't routed
            # back here.
            conn.execute(
                "UPDATE household_members SET member_pending_intent = NULL WHERE id = ?",
                (member["id"],),
            )
            conn.commit()
        except Exception as e:
            logger.warning("tier2 reply persistence failed: %s", e)
            # Don't acknowledge if we couldn't persist -- leave the
            # pending intent so the user can try again.
            return None

        ack = format_tier2_resolved(item_name, decision)
        try:
            await wa_client.send_message(
                member["phone"], ack,
                member_id=member["id"],
            )
        except Exception as e:
            logger.warning("tier2 ack send failed: %s", e)
        log_event(
            "tier2_reply",
            source="feedback_manager",
            detail=f"member={member.get('name')} item={item_name} "
                   f"decision={decision}",
        )
        return {"action": "tier2_reply", "decision": decision,
                "item_id": item_id, "item_name": item_name, "reply": ack}

    async def _maybe_handle_rating_reply(self, member: dict, text: str, conn) -> dict | None:
        """B5 — Handle a reply to a delivered-order rating prompt.

        Returns a result dict if the reply was a rating-style answer AND
        the member has a pending rating_ask. Returns None to let the
        caller fall through to the normal round-scoped handler.

        Three decisions:
          thumbs_up_all   -> mark every order_item with feedback='thumbs_up'
                             via order_ratings + order_item_ratings rows.
          thumbs_down_all -> same but feedback='thumbs_down'. Ack invites
                             the user to elaborate (the LLM rating handler
                             will parse follow-up free text).
          free_text       -> save the message verbatim into
                             order_ratings.overall_text and run the LLM
                             through it for a conversational reply.
          dismiss         -> mark orders.rating_status='dismissed', no
                             rating rows written.

        Either way, clears member_pending_intent on success so we don't
        re-fire on the next unrelated message.
        """
        try:
            pi_row = conn.execute(
                "SELECT member_pending_intent FROM household_members WHERE id = ?",
                (member["id"],),
            ).fetchone()
        except Exception:
            return None
        if not pi_row or not pi_row["member_pending_intent"]:
            return None
        try:
            import json as _json
            pending = _json.loads(pi_row["member_pending_intent"])
        except Exception:
            return None
        if not isinstance(pending, dict) or pending.get("type") != "rating_pending":
            return None

        from whatsapp_agent import parse_rating_reply, format_rating_resolved
        decision = parse_rating_reply(text)
        if decision is None:
            # Not recognized as a rating reply -- leave the pending
            # state in place so a follow-up reply still routes here.
            return None

        order_id = pending.get("order_id")
        if not order_id:
            logger.warning("rating_pending intent missing order_id; clearing")
            conn.execute(
                "UPDATE household_members SET member_pending_intent = NULL WHERE id = ?",
                (member["id"],),
            )
            conn.commit()
            return None

        # Persist the rating + clear pending. Wrap in a try so a DB
        # failure doesn't leave the member stuck mid-flow.
        try:
            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if decision == "dismiss":
                conn.execute(
                    "UPDATE orders SET rating_status = 'dismissed' WHERE id = ?",
                    (order_id,),
                )
            else:
                # Insert or update the order_ratings row.
                overall_text = text if decision == "free_text" else None
                conn.execute(
                    """
                    INSERT INTO order_ratings (order_id, overall_text, status, finalized_at)
                    VALUES (?, ?, 'finalized', ?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        overall_text = excluded.overall_text,
                        status = 'finalized',
                        finalized_at = excluded.finalized_at
                    """,
                    (order_id, overall_text, now_iso),
                )
                rating_row = conn.execute(
                    "SELECT id FROM order_ratings WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                rating_id = rating_row["id"] if rating_row else None

                # Per-item rows for the thumbs cases. free_text saves
                # the overall_text only; the LLM extracts per-item
                # complaints later (separate slice).
                if rating_id and decision in ("thumbs_up_all", "thumbs_down_all"):
                    feedback_val = (
                        "thumbs_up" if decision == "thumbs_up_all"
                        else "thumbs_down"
                    )
                    items_rows = conn.execute(
                        "SELECT id, item_id FROM order_items WHERE order_id = ?",
                        (order_id,),
                    ).fetchall()
                    for ir in items_rows:
                        conn.execute(
                            """
                            INSERT INTO order_item_ratings
                                (rating_id, order_item_id, item_id, feedback)
                            VALUES (?, ?, ?, ?)
                            """,
                            (rating_id, ir["id"], ir["item_id"], feedback_val),
                        )

                conn.execute(
                    "UPDATE orders SET rating_status = 'finalized' WHERE id = ?",
                    (order_id,),
                )

            conn.execute(
                "UPDATE household_members SET member_pending_intent = NULL WHERE id = ?",
                (member["id"],),
            )
            conn.commit()
        except Exception as e:
            logger.error("rating reply save failed: %s", e, exc_info=True)
            log_event(
                "rating_save_error", source="feedback_manager",
                detail=f"order={order_id} decision={decision} err={str(e)[:160]}",
                level="error",
            )
            return None

        ack = format_rating_resolved(decision)

        # For free-text feedback, also kick off the LLM for a
        # conversational follow-up. Best-effort; failure doesn't block
        # the canonical save above.
        if decision == "free_text":
            try:
                from llm_handler import llm_process_rating_feedback
                llm_reply = await llm_process_rating_feedback(text)
                if llm_reply:
                    ack = llm_reply
            except Exception as e:
                logger.debug("rating LLM follow-up skipped: %s", e)

        try:
            from whatsapp_client import wa_client
            await wa_client.send_message(
                member["phone"], ack, member_id=member["id"],
            )
        except Exception as e:
            logger.warning("rating ack send failed: %s", e)

        log_event(
            "rating_reply", source="feedback_manager",
            detail=(
                f"member={member.get('name')} order={order_id} "
                f"decision={decision}"
            ),
        )
        return {"action": "rating_reply", "decision": decision,
                "order_id": order_id, "reply": ack}

    def _count_numbered_items(self, conn, list_id: int) -> int:
        """How many items are visible (and therefore number-referenceable)
        in the list_data we sent over WhatsApp."""
        try:
            from frontend_bridge import build_frontend_list
            data = build_frontend_list(list_id, conn=conn)
        except Exception:
            return 0
        n = 0
        for vendor in data.get("vendors", []):
            for category in vendor.get("categories", []):
                for item in category.get("items", []):
                    if item.get("quantity", 1) > 0:
                        n += 1
        return n

    def _compute_cart_status(self, conn, list_id: int) -> tuple[int, float]:
        """Return (visible_item_count, running_total) for the cart.

        Used by handle_member_reply to append a single status footer
        ("Cart now: N items · TOTAL د.إ. Anything else?") after each
        confirmed action -- gives the reviewer the same kind of running
        total they get on the in-app cart view.

        Both values are computed from build_frontend_list so the
        numbering and totals match what format_cart_message originally
        sent. Returns (0, 0.0) if anything blows up; the caller treats
        that as "skip the footer".
        """
        try:
            from frontend_bridge import build_frontend_list
            data = build_frontend_list(list_id, conn=conn)
        except Exception:
            return 0, 0.0
        count = 0
        total = 0.0
        for vendor in data.get("vendors", []):
            for category in vendor.get("categories", []):
                for item in category.get("items", []):
                    qty = item.get("quantity", 1) or 1
                    if qty <= 0:
                        continue
                    count += 1
                    price = item.get("price")
                    if isinstance(price, (int, float)):
                        total += price * qty
        return count, round(total, 2)

    def _find_in_list(self, conn, list_id: int, item_text: str) -> dict | None:
        """Find an item in the list by name (fuzzy)."""
        items = conn.execute(
            """SELECT * FROM list_items
               WHERE list_id = ? AND status != 'skipped'""",
            (list_id,),
        ).fetchall()

        item_lower = item_text.lower()
        for item in items:
            name = (item["item_name_text"] or "").lower()
            if item_lower in name or name in item_lower:
                return dict(item)

        return None


# Module-level singleton
feedback_manager = FeedbackManager()
