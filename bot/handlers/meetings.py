"""Telegram handlers for the meetings module.

Callback data conventions:
  mtp:<poll_id>:t:<user_id>  — toggle participant in picker
  mtp:<poll_id>:done         — finish picker → start voting
  mtp:<poll_id>:cancel       — cancel poll (picker stage)
  mtv:<poll_id>:<hour>       — toggle vote on slot
  mtv:<poll_id>:done         — finalize voting
  mtv:<poll_id>:cancel       — cancel poll (voting stage)
  mtm:<poll_id>:<hour>       — manual pick (initiator DM)
  mtm:<poll_id>:cancel       — manual cancel
"""

from __future__ import annotations

import html
import structlog
from aiogram import F, Router, types
from aiogram.enums import ParseMode

from config import settings
from db.repositories import (
    meeting_polls as polls_repo,
)
from meetings import flow
from meetings.zoom_client import zoom_client

logger = structlog.get_logger()

router = Router()


# =========================================================================
# Helpers
# =========================================================================

async def _safe_edit(message: types.Message, text: str, kb=None, parse_mode=ParseMode.HTML):
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode=parse_mode, disable_web_page_preview=True)
    except Exception as e:
        logger.debug("edit failed", error=str(e))


async def _refresh_voting_board(bot, chat_id: int, message_id: int, poll_id: int):
    res = await flow.build_voting_board(poll_id)
    if not res:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=res.text,
            reply_markup=res.keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.debug("refresh voting board failed", error=str(e))


def _is_initiator(user_id: int, poll: dict) -> bool:
    return poll and poll.get("initiator_id") == user_id


# =========================================================================
# Picker callbacks (mtp:*)
# =========================================================================

@router.callback_query(F.data.startswith("mtp:"))
async def handle_picker(query: types.CallbackQuery, user_context: dict = None):
    parts = query.data.split(":")
    poll_id = int(parts[1])
    action = parts[2]
    user_id = query.from_user.id

    poll = await polls_repo.get(poll_id)
    if not poll or poll["status"] not in ("picking_participants",):
        await query.answer("Этот опрос уже неактивен.", show_alert=False)
        return

    if not _is_initiator(user_id, poll):
        await query.answer("Только инициатор настраивает участников.", show_alert=False)
        return

    if action == "cancel":
        await polls_repo.update(poll_id, status="cancelled")
        await _safe_edit(query.message, "✖ Опрос отменён.", kb=None)
        await query.answer("Отменено")
        return

    if action == "done":
        participants = await polls_repo.list_participants(poll_id)
        if len(participants) < 2:
            await query.answer("Выбери минимум 2 участника", show_alert=True)
            return
        await polls_repo.update(poll_id, status="voting")

        board = await flow.build_voting_board(poll_id)
        if not board:
            await query.answer("Не удалось построить голосование", show_alert=True)
            return
        try:
            await query.message.edit_text(
                board.text,
                reply_markup=board.keyboard,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            await polls_repo.update(poll_id, message_id=query.message.message_id)
        except Exception as e:
            logger.warning("Failed to edit picker into voting board", error=str(e))
        await query.answer()
        return

    if action == "t":
        target_id = int(parts[3])
        current = await polls_repo.list_participants(poll_id)
        current_ids = {p["user_id"] for p in current}
        from db.repositories import chat_members as chat_members_repo
        if target_id in current_ids:
            await polls_repo.remove_participant(poll_id, target_id)
        else:
            members = await chat_members_repo.list_for_chat(poll["chat_id"])
            member = next((m for m in members if m["user_id"] == target_id), None)
            name = (member.get("full_name") if member else None) or f"id{target_id}"
            await polls_repo.add_participant(poll_id, target_id, name)

        # Rebuild keyboard
        members = await chat_members_repo.list_for_chat(poll["chat_id"])
        selected = await polls_repo.list_participants(poll_id)
        selected_ids = {p["user_id"] for p in selected}
        # Reuse private builders via flow
        from meetings.flow import _build_picker_keyboard, _build_picker_text
        from meetings.intent import MeetingIntent
        intent = MeetingIntent(
            kind="vote",
            meeting_date=poll["meeting_date"],
            meeting_time=None,
            duration_min=poll["duration_min"],
            topic=poll["topic"] or "",
        )
        new_text = _build_picker_text(intent, members, selected)
        new_kb = _build_picker_keyboard(poll_id, members, selected_ids)
        await _safe_edit(query.message, new_text, kb=new_kb)
        await query.answer()
        return


# =========================================================================
# Voting callbacks (mtv:*)
# =========================================================================

@router.callback_query(F.data.startswith("mtv:"))
async def handle_voting(query: types.CallbackQuery, user_context: dict = None):
    parts = query.data.split(":")
    poll_id = int(parts[1])
    action = parts[2]
    user_id = query.from_user.id

    poll = await polls_repo.get(poll_id)
    if not poll or poll["status"] != "voting":
        await query.answer("Опрос уже неактивен.", show_alert=False)
        return

    if action == "cancel":
        if not _is_initiator(user_id, poll):
            await query.answer("Отменить может только инициатор.", show_alert=False)
            return
        await polls_repo.update(poll_id, status="cancelled")
        await _safe_edit(query.message, "✖ Опрос отменён.", kb=None)
        await query.answer("Отменено")
        return

    if action == "done":
        if not _is_initiator(user_id, poll):
            await query.answer("Завершить опрос может только инициатор.", show_alert=False)
            return
        await _finalize_poll(query.bot, poll_id, manual_trigger_message=query.message)
        await query.answer()
        return

    # Slot toggle: action is hour
    try:
        hour = int(action)
    except ValueError:
        await query.answer("Bad data")
        return

    # Only listed participants may vote
    participants = await polls_repo.list_participants(poll_id)
    if user_id not in {p["user_id"] for p in participants}:
        await query.answer("Ты не в списке участников этого опроса.", show_alert=False)
        return

    current = await polls_repo.get_vote(poll_id, hour, user_id)
    # Cycle: None -> 'yes' -> 'no' -> None
    if current is None:
        new_vote = "yes"
    elif current == "yes":
        new_vote = "no"
    else:
        new_vote = None

    await polls_repo.set_vote(poll_id, hour, user_id, new_vote)

    # Refresh board
    await _refresh_voting_board(query.bot, poll["chat_id"], query.message.message_id, poll_id)

    # Check if everyone has voted on at least one slot — auto-close
    all_voted = await _all_participants_voted(poll_id)
    if all_voted:
        await _finalize_poll(query.bot, poll_id, manual_trigger_message=None)

    await query.answer()


# =========================================================================
# Manual pick callbacks (mtm:*) — initiator DM after no-consensus
# =========================================================================

@router.callback_query(F.data.startswith("mtm:"))
async def handle_manual_pick(query: types.CallbackQuery, user_context: dict = None):
    parts = query.data.split(":")
    poll_id = int(parts[1])
    action = parts[2]
    user_id = query.from_user.id

    poll = await polls_repo.get(poll_id)
    if not poll or poll["status"] != "manual_pick":
        await query.answer("Опрос уже неактивен.", show_alert=False)
        return

    if not _is_initiator(user_id, poll):
        await query.answer("Это решение принимает инициатор.", show_alert=False)
        return

    if action == "cancel":
        await polls_repo.update(poll_id, status="cancelled")
        await _safe_edit(query.message, "✖ Опрос отменён.", kb=None)
        try:
            await query.bot.send_message(poll["chat_id"], "✖ Опрос созвона отменён инициатором.")
        except Exception:
            pass
        await query.answer("Отменено")
        return

    try:
        hour = int(action)
    except ValueError:
        await query.answer("Bad data")
        return

    await _create_meeting_for_poll(query.bot, poll_id, hour, manual_message=query.message)
    await query.answer()


# =========================================================================
# Finalization helpers
# =========================================================================

async def _all_participants_voted(poll_id: int) -> bool:
    """True if every listed participant has at least one vote (yes or no)."""
    participants = await polls_repo.list_participants(poll_id)
    if not participants:
        return False
    votes = await polls_repo.get_all_votes(poll_id)
    voters = {v["user_id"] for v in votes}
    return all(p["user_id"] in voters for p in participants)


async def _finalize_poll(bot, poll_id: int, manual_trigger_message=None):
    """Close the voting stage and either auto-schedule or DM initiator."""
    poll = await polls_repo.get(poll_id)
    if not poll or poll["status"] not in ("voting", "picking_participants"):
        return

    outcome = await flow.evaluate_finalize(poll_id)

    if outcome.chosen_hour is not None:
        await _create_meeting_for_poll(bot, poll_id, outcome.chosen_hour, manual_message=None)
        return

    if outcome.text and outcome.chosen_hour is None and not outcome.need_manual_pick:
        # Nothing to schedule (no slots / empty etc.)
        await polls_repo.update(poll_id, status="cancelled")
        try:
            await bot.send_message(poll["chat_id"], outcome.text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    # need_manual_pick — switch status and DM initiator with manual buttons
    await polls_repo.update(poll_id, status="manual_pick")
    hours = await flow.manual_pick_hours(poll_id)
    kb = flow.manual_pick_keyboard_for(poll_id, hours)
    try:
        await bot.send_message(
            poll["initiator_id"],
            outcome.text or "Выбери слот вручную:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        # Initiator hasn't started the bot in DM — fall back to chat with @-mention
        logger.warning("Could not DM initiator for manual pick — using chat fallback", error=str(e))
        try:
            await bot.send_message(
                poll["chat_id"],
                outcome.text or "Выбери слот вручную:",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e2:
            logger.error("Manual pick fallback also failed", error=str(e2))


async def _create_meeting_for_poll(bot, poll_id: int, hour: int, manual_message=None):
    poll = await polls_repo.get(poll_id)
    if not poll:
        return
    if poll["status"] == "finalized":
        return
    participants = await polls_repo.list_participants(poll_id)

    db_id, res = await flow.schedule_meeting(
        chat_id=poll["chat_id"],
        initiator_id=poll["initiator_id"],
        meeting_date=poll["meeting_date"],
        hour=hour,
        minute=0,
        duration_min=poll["duration_min"],
        topic=poll["topic"] or "",
        participants=[{"user_id": p["user_id"], "full_name": p["full_name"]} for p in participants],
    )

    if db_id is None:
        # Zoom failed; announce error in chat
        try:
            await bot.send_message(poll["chat_id"], res.text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    await polls_repo.update(
        poll_id,
        status="finalized",
        chosen_hour=hour,
        zoom_meeting_id=db_id,
        finalized_at="CURRENT_TIMESTAMP",  # will be overwritten by NOW in next update if needed
    )
    # Replace CURRENT_TIMESTAMP literal — pass an explicit ISO instead
    from datetime import datetime
    await polls_repo.update(poll_id, finalized_at=datetime.utcnow().isoformat())

    # Edit the original poll message in chat to a "finalized" notice (if we know it)
    try:
        if poll.get("message_id"):
            await bot.edit_message_text(
                chat_id=poll["chat_id"],
                message_id=poll["message_id"],
                text="✅ Голосование завершено. Слот выбран.",
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        pass

    # Edit the manual-pick message if any
    if manual_message is not None:
        try:
            await manual_message.edit_text("✅ Слот выбран.", parse_mode=ParseMode.HTML)
        except Exception:
            pass

    # Announce in chat
    try:
        await bot.send_message(
            poll["chat_id"],
            res.text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("Failed to announce meeting", error=str(e))
