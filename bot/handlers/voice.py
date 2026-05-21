"""Voice-command handler — transcribe a voice message and answer it.

A Telegram voice message (or video-note «кружок») is run through the
local Whisper STT; the recognised text is then fed into the SAME
orchestrator pipeline as a typed question (bot.handlers.group.process_
question), so a voice query behaves exactly like a written one.

Scope:
- private chats — any voice message is treated as a question;
- group chats — only when the voice is a REPLY to the bot's own
  message. A voice message can't carry an @mention, so a reply is the
  only way to address the bot by voice in a group.

STT is serialised with the lead-reports pipeline through
stt.transcribe_lock — two Whisper runs at once would OOM the box.
"""

import asyncio
import os
import tempfile

import structlog
from aiogram import F, Router, types

from config import settings
from lead_reports import stt
from bot.handlers.group import (
    _bot_identity, _ensure_bot_identity, process_question,
)

logger = structlog.get_logger()

router = Router()

# A voice note longer than this is almost certainly not a command —
# refuse it rather than tie up Whisper for minutes.
_MAX_VOICE_SEC = 600


def _is_reply_to_bot(message: types.Message) -> bool:
    """True if this message is a reply to one authored by the bot."""
    replied = message.reply_to_message
    return bool(
        replied
        and replied.from_user
        and replied.from_user.id == _bot_identity["id"]
    )


async def _transcribe(message: types.Message, file_id: str, suffix: str) -> str:
    """Download the Telegram audio file and run Whisper on it.

    Serialised via stt.transcribe_lock; the model is unloaded afterwards
    to free ~1.2GB on this RAM-tight box (reload costs only ~9s).
    """
    tmp_path = None
    try:
        tg_file = await message.bot.get_file(file_id)
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="voicecmd_")
        os.close(fd)
        await message.bot.download_file(tg_file.file_path, tmp_path)

        async with stt.transcribe_lock:
            try:
                return await asyncio.to_thread(stt.transcribe, tmp_path)
            finally:
                stt.unload()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as e:
                logger.debug("Voice temp cleanup failed", path=tmp_path, error=str(e))


@router.message(F.voice | F.video_note)
async def handle_voice(message: types.Message, user_context: dict = None):
    """Transcribe a voice / video-note message and answer it as a question."""
    if not user_context or not settings.voice_commands_enabled:
        return

    chat_type = message.chat.type

    # Scope gate — private always, groups only on a reply to the bot.
    if chat_type == "private":
        if not user_context.get("allow_private", True):
            await message.reply(
                "Я общаюсь только в рабочих чатах. Задавайте вопросы там."
            )
            return
    elif chat_type in ("group", "supergroup"):
        await _ensure_bot_identity(message.bot)
        if not _is_reply_to_bot(message):
            return  # voice not addressed to the bot
    else:
        return

    media = message.voice or message.video_note
    if media is None:
        return
    if (media.duration or 0) > _MAX_VOICE_SEC:
        await message.reply("🎙 Голосовое слишком длинное — принимаю до 10 минут.")
        return
    suffix = ".mp4" if message.video_note else ".oga"

    status = None
    try:
        status = await message.reply("🎙 Распознаю голос…")
    except Exception as e:
        logger.warning("Voice status message failed", error=str(e))

    try:
        text = await _transcribe(message, media.file_id, suffix)
    except Exception as e:
        logger.error("Voice transcription failed", error=str(e),
                     chat_id=message.chat.id, user_id=message.from_user.id)
        await _say(status, message, "⚠️ Не удалось распознать голосовое. Попробуйте ещё раз.")
        return

    text = (text or "").strip()
    if not text:
        await _say(status, message, "🎙 Не расслышал — повторите, пожалуйста, чуть чётче.")
        return

    logger.info(
        "Voice command transcribed",
        chars=len(text), chat_id=message.chat.id, user_id=message.from_user.id,
    )

    # Show what was heard so the user can verify the recognition.
    await _say(status, message, f"🎙 Распознал: «{text}»")

    # Hand the recognised text to the normal question pipeline — it posts
    # its own progress message and final answer as separate replies.
    await process_question(message, text, user_context)


async def _say(status, message: types.Message, text: str) -> None:
    """Edit the status message if we have one, else send a fresh reply."""
    if status is not None:
        try:
            await status.edit_text(text)
            return
        except Exception:
            pass
    try:
        await message.reply(text)
    except Exception as e:
        logger.warning("Voice reply failed", error=str(e))
