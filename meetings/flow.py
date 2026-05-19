"""Meetings module business logic: voting polls + direct creation.

All times user-facing in MSK. Stored in DB:
  - meeting_polls.meeting_date: 'YYYY-MM-DD' (MSK calendar day)
  - scheduled_meetings.start_at: ISO UTC
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from config import settings
from db.repositories import (
    chat_members as chat_members_repo,
    meeting_polls as polls_repo,
    scheduled_meetings as meetings_repo,
)
from meetings.intent import MeetingIntent
from meetings.zoom_client import ZoomError, zoom_client

logger = structlog.get_logger()

_MSK = ZoneInfo("Europe/Moscow")

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MONTH_RU_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


# =========================================================================
# Date helpers
# =========================================================================

def _now_msk() -> datetime:
    return datetime.now(_MSK)


def _today_msk() -> date:
    return _now_msk().date()


def _fmt_date_ru(d: date) -> str:
    return f"{_WEEKDAY_RU[d.weekday()]}, {d.day} {_MONTH_RU_GEN[d.month - 1]}"


def _msk_dt_to_utc_iso(d: date, hour: int, minute: int = 0) -> str:
    dt_msk = datetime(d.year, d.month, d.day, hour, minute, tzinfo=_MSK)
    return dt_msk.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()


def _msk_dt_iso_naive(d: date, hour: int, minute: int = 0) -> str:
    """Zoom expects start_time as naive ISO + timezone field."""
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{hour:02d}:{minute:02d}:00"


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _slot_hours_for(d: date, duration_min: int) -> List[int]:
    """Available slot start hours on day `d` given working hours 10..18.

    For today, drop hours that have already passed (use current MSK hour;
    if minute>=10 we already lost that hour, but with 1h step let's keep
    it strict — hour must be >= now.hour + (1 if minute > 0 else 0)).
    """
    start_h = settings.meetings_work_start_hour
    end_h = settings.meetings_work_end_hour
    # Last possible start hour = end_h - ceil(duration/60)
    last_start = end_h - max(1, (duration_min + 59) // 60)
    hours = list(range(start_h, last_start + 1))

    if d == _today_msk():
        now = _now_msk()
        cutoff = now.hour + (1 if now.minute > 0 else 0)
        hours = [h for h in hours if h >= cutoff]

    return hours


# =========================================================================
# Result types
# =========================================================================

@dataclass
class FlowResult:
    text: str
    keyboard: Optional[InlineKeyboardMarkup] = None
    parse_mode: Optional[str] = ParseMode.HTML


# =========================================================================
# Sanity validation for an intent (returns error msg or None)
# =========================================================================

def validate_intent(intent: MeetingIntent) -> Optional[str]:
    try:
        d = date.fromisoformat(intent.meeting_date)
    except ValueError:
        return "Не понял дату. Попробуй ещё раз."

    if d < _today_msk():
        return f"Дата {_fmt_date_ru(d)} уже прошла. Назначь на будущее."

    if _is_weekend(d):
        return f"{_fmt_date_ru(d)} — выходной. Выбери будний день."

    if intent.meeting_time:
        hh, mm = intent.meeting_time.split(":")
        hh, mm = int(hh), int(mm)
        if not (settings.meetings_work_start_hour <= hh < settings.meetings_work_end_hour):
            return (
                f"Время {intent.meeting_time} вне рабочих часов "
                f"({settings.meetings_work_start_hour}:00–{settings.meetings_work_end_hour}:00 МСК)."
            )
        if d == _today_msk():
            now = _now_msk()
            if datetime(d.year, d.month, d.day, hh, mm, tzinfo=_MSK) <= now + timedelta(minutes=5):
                return "Это время уже наступило. Выбери позже."

    return None


# =========================================================================
# Module 1: Voting poll
# =========================================================================

async def start_voting_poll(
    bot: Bot,
    chat_id: int,
    initiator_id: int,
    intent: MeetingIntent,
) -> Tuple[Optional[int], FlowResult]:
    """Create a poll row, return (poll_id, FlowResult with participant picker)."""
    err = validate_intent(intent)
    if err:
        return None, FlowResult(text=html.escape(err))

    members = await chat_members_repo.list_for_chat(chat_id)
    # Exclude initiator from optional picks — they are auto-included.
    # Actually for fairness: keep initiator as preselected, allow toggle.
    if not members:
        return None, FlowResult(
            text=(
                "В этом чате ещё нет известных участников (никто не писал кроме тебя). "
                "Попроси коллег что-нибудь написать в чат и попробуй снова."
            )
        )

    deadline = datetime.utcnow() + timedelta(minutes=settings.meetings_poll_timeout_min)
    poll_id = await polls_repo.create(
        chat_id=chat_id,
        initiator_id=initiator_id,
        meeting_date=intent.meeting_date,
        duration_min=intent.duration_min,
        topic=intent.topic,
        deadline_at=deadline.isoformat(),
    )

    # Pre-select the initiator.
    initiator_member = next((m for m in members if m["user_id"] == initiator_id), None)
    if initiator_member:
        await polls_repo.add_participant(
            poll_id, initiator_id, initiator_member.get("full_name")
        )

    text = _build_picker_text(intent, members, await polls_repo.list_participants(poll_id))
    kb = _build_picker_keyboard(poll_id, members, {initiator_id} if initiator_member else set())
    return poll_id, FlowResult(text=text, keyboard=kb)


def _build_picker_text(
    intent: MeetingIntent,
    members: List[dict],
    selected: List[dict],
) -> str:
    d = date.fromisoformat(intent.meeting_date)
    selected_names = ", ".join(html.escape(p["full_name"] or f"id{p['user_id']}") for p in selected) or "—"
    topic_line = f"\nТема: <i>{html.escape(intent.topic)}</i>" if intent.topic else ""
    return (
        f"📅 <b>Согласуем созвон</b>\n"
        f"Дата: {_fmt_date_ru(d)}\n"
        f"Длительность: {intent.duration_min} мин"
        f"{topic_line}\n\n"
        f"<b>Кто участвует?</b> Выбери из списка ниже.\n"
        f"Выбраны: {selected_names}"
    )


def _build_picker_keyboard(
    poll_id: int,
    members: List[dict],
    selected_ids: set,
) -> InlineKeyboardMarkup:
    rows = []
    for m in members[:20]:  # safety cap
        mark = "✅" if m["user_id"] in selected_ids else "⬜"
        name = (m.get("full_name") or m.get("username") or f"id{m['user_id']}")[:40]
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {name}",
                callback_data=f"mtp:{poll_id}:t:{m['user_id']}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="✔ Готово — начать голосование",
            callback_data=f"mtp:{poll_id}:done",
        )
    ])
    rows.append([
        InlineKeyboardButton(text="✖ Отменить", callback_data=f"mtp:{poll_id}:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---- voting board ----

async def build_voting_board(poll_id: int) -> Optional[FlowResult]:
    poll = await polls_repo.get(poll_id)
    if not poll:
        return None
    participants = await polls_repo.list_participants(poll_id)
    if not participants:
        return None
    votes = await polls_repo.get_all_votes(poll_id)
    d = date.fromisoformat(poll["meeting_date"])
    hours = _slot_hours_for(d, poll["duration_min"])

    if not hours:
        return FlowResult(
            text=(
                f"На {_fmt_date_ru(d)} уже не осталось рабочих слотов "
                f"({settings.meetings_work_start_hour}:00–{settings.meetings_work_end_hour}:00 МСК)."
            )
        )

    # Build counters {hour: {'yes': set(user_id), 'no': set(user_id)}}
    counters: dict = {h: {"yes": set(), "no": set()} for h in hours}
    for v in votes:
        h = v["slot_hour"]
        if h in counters and v["vote"] in ("yes", "no"):
            counters[h][v["vote"]].add(v["user_id"])

    n_part = len(participants)

    # Build text
    topic_line = f"\nТема: <i>{html.escape(poll['topic'])}</i>" if poll["topic"] else ""
    parts_names = ", ".join(html.escape(p["full_name"] or f"id{p['user_id']}") for p in participants)
    deadline_dt = datetime.fromisoformat(poll["deadline_at"])
    deadline_msk = deadline_dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(_MSK)
    lines = [
        f"🗳 <b>Голосование за слот</b>",
        f"Дата: {_fmt_date_ru(d)} · {poll['duration_min']} мин",
        f"Участники: {parts_names}",
    ]
    if poll["topic"]:
        lines.append(f"Тема: <i>{html.escape(poll['topic'])}</i>")
    lines.append("")
    lines.append("Нажми на час — переключится твой голос: ⬜ → ✅ → ❌ → ⬜")
    lines.append(f"Опрос закроется автоматически в {deadline_msk.strftime('%H:%M')} МСК.")
    text = "\n".join(lines)

    # Build keyboard: each row = one hour with counters
    rows = []
    for h in hours:
        yes = len(counters[h]["yes"])
        no = len(counters[h]["no"])
        label = f"{h:02d}:00  ✅{yes}  ❌{no}"
        if yes == n_part and no == 0:
            label = f"⭐ {label}"
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"mtv:{poll_id}:{h}",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="🔒 Завершить голосование", callback_data=f"mtv:{poll_id}:done"),
        InlineKeyboardButton(text="✖ Отменить", callback_data=f"mtv:{poll_id}:cancel"),
    ])

    return FlowResult(text=text, keyboard=InlineKeyboardMarkup(inline_keyboard=rows))


# =========================================================================
# Finalize: find best slot or ask initiator
# =========================================================================

@dataclass
class FinalizeOutcome:
    chosen_hour: Optional[int]      # slot to schedule (if auto-found)
    need_manual_pick: bool           # if True — DM initiator with manual buttons
    text: Optional[str]              # already-formatted message (for cancellation/no-slots)


async def _summarize_for_manual(poll: dict, participants: List[dict], votes: List[dict]) -> str:
    d = date.fromisoformat(poll["meeting_date"])
    hours = _slot_hours_for(d, poll["duration_min"])

    counters: dict = {h: {"yes": set(), "no": set()} for h in hours}
    names_by_id = {p["user_id"]: (p["full_name"] or f"id{p['user_id']}") for p in participants}
    for v in votes:
        h = v["slot_hour"]
        if h in counters and v["vote"] in ("yes", "no"):
            counters[h][v["vote"]].add(v["user_id"])

    lines = [
        "🤔 <b>Полного согласия нет</b>",
        f"Дата: {_fmt_date_ru(d)} · {poll['duration_min']} мин",
        "",
    ]
    for h in hours:
        yes_n = ", ".join(html.escape(names_by_id[u]) for u in counters[h]["yes"]) or "—"
        no_n = ", ".join(html.escape(names_by_id[u]) for u in counters[h]["no"]) or "—"
        lines.append(f"<b>{h:02d}:00</b> · могут: {yes_n} · не могут: {no_n}")
    lines.append("")
    lines.append("Выбери слот, на который ставим встречу:")
    return "\n".join(lines)


def _build_manual_keyboard(poll_id: int, hours: List[int]) -> InlineKeyboardMarkup:
    rows = []
    # Pack 2 buttons per row
    row = []
    for h in hours:
        row.append(InlineKeyboardButton(
            text=f"{h:02d}:00",
            callback_data=f"mtm:{poll_id}:{h}",
        ))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✖ Отменить", callback_data=f"mtm:{poll_id}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def evaluate_finalize(poll_id: int) -> FinalizeOutcome:
    """Decide what to do when a poll is being closed.

    Logic:
      - If any slot has all participants ✅ and zero ❌ → pick earliest.
      - Else → return need_manual_pick=True with summary text.
    """
    poll = await polls_repo.get(poll_id)
    if not poll:
        return FinalizeOutcome(None, False, "Опрос не найден.")
    participants = await polls_repo.list_participants(poll_id)
    if not participants:
        return FinalizeOutcome(None, False, "Нет участников.")
    votes = await polls_repo.get_all_votes(poll_id)
    d = date.fromisoformat(poll["meeting_date"])
    hours = _slot_hours_for(d, poll["duration_min"])
    if not hours:
        return FinalizeOutcome(None, False, "Нет доступных слотов на эту дату.")

    counters = {h: {"yes": set(), "no": set()} for h in hours}
    for v in votes:
        if v["slot_hour"] in counters and v["vote"] in ("yes", "no"):
            counters[v["slot_hour"]][v["vote"]].add(v["user_id"])

    n_part = len(participants)
    for h in sorted(hours):  # earliest first
        if len(counters[h]["yes"]) == n_part and len(counters[h]["no"]) == 0:
            return FinalizeOutcome(chosen_hour=h, need_manual_pick=False, text=None)

    text = await _summarize_for_manual(poll, participants, votes)
    return FinalizeOutcome(chosen_hour=None, need_manual_pick=True, text=text)


def manual_pick_keyboard_for(poll_id: int, hours: List[int]) -> InlineKeyboardMarkup:
    return _build_manual_keyboard(poll_id, hours)


async def manual_pick_hours(poll_id: int) -> List[int]:
    poll = await polls_repo.get(poll_id)
    if not poll:
        return []
    d = date.fromisoformat(poll["meeting_date"])
    return _slot_hours_for(d, poll["duration_min"])


# =========================================================================
# Schedule the actual Zoom meeting + announce
# =========================================================================

async def schedule_meeting(
    chat_id: int,
    initiator_id: Optional[int],
    meeting_date: str,
    hour: int,
    minute: int,
    duration_min: int,
    topic: str,
    participants: List[dict],
) -> Tuple[Optional[int], FlowResult]:
    """Create Zoom meeting, save to DB, return (meeting_id, announcement text)."""
    d = date.fromisoformat(meeting_date)
    start_iso_naive = _msk_dt_iso_naive(d, hour, minute)
    start_utc_iso = _msk_dt_to_utc_iso(d, hour, minute)

    final_topic = topic or "Созвон команды Growzone"

    try:
        zoom = await zoom_client.create_meeting(
            topic=final_topic,
            start_at_iso=start_iso_naive,
            duration_min=duration_min,
            timezone=settings.meetings_timezone,
        )
    except ZoomError as e:
        logger.error("Failed to create Zoom meeting", error=str(e))
        return None, FlowResult(
            text=(
                "⚠️ Не удалось создать встречу в Zoom: "
                f"<code>{html.escape(str(e))}</code>"
            )
        )

    join_url = zoom.get("join_url", "")
    start_url = zoom.get("start_url", "")
    meeting_id_zoom = str(zoom.get("id", ""))

    db_id = await meetings_repo.create(
        chat_id=chat_id,
        initiator_id=initiator_id,
        topic=final_topic,
        start_at_utc_iso=start_utc_iso,
        duration_min=duration_min,
        zoom_meeting_id=meeting_id_zoom,
        zoom_join_url=join_url,
        zoom_start_url=start_url,
        participants=participants,
    )

    names = ", ".join(html.escape(p.get("full_name") or f"id{p['user_id']}") for p in participants)
    text = (
        f"✅ <b>Созвон назначен</b>\n"
        f"🕐 {_fmt_date_ru(d)}, {hour:02d}:{minute:02d} МСК\n"
        f"⏱ Длительность: {duration_min} мин\n"
        f"👥 {names or '—'}\n"
        f"📝 Тема: {html.escape(final_topic)}\n\n"
        f"🔗 <a href=\"{html.escape(join_url, quote=True)}\">Подключиться к Zoom</a>\n"
        f"Напомню за {settings.meetings_reminder_min_before} минут."
    )
    return db_id, FlowResult(text=text)


# =========================================================================
# Reminder formatting (called by scheduler)
# =========================================================================

def format_reminder(meeting_row: dict) -> str:
    start_utc = datetime.fromisoformat(meeting_row["start_at"])
    start_msk = start_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(_MSK)
    participants = []
    try:
        participants = json.loads(meeting_row.get("participants_json") or "[]")
    except json.JSONDecodeError:
        pass
    names = ", ".join(html.escape(p.get("full_name") or f"id{p['user_id']}") for p in participants) or "—"
    topic = html.escape(meeting_row.get("topic") or "Созвон")
    join_url = meeting_row.get("zoom_join_url") or ""
    return (
        f"⏰ <b>Через {settings.meetings_reminder_min_before} минут созвон</b>\n"
        f"🕐 {start_msk.strftime('%H:%M')} МСК — {topic}\n"
        f"👥 {names}\n"
        f"🔗 <a href=\"{html.escape(join_url, quote=True)}\">Подключиться к Zoom</a>"
    )
