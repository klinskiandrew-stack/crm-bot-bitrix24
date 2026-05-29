from aiogram import Router, types, F, Bot
from aiogram.enums import ParseMode
from aiogram.filters import Command
from datetime import date, timedelta
import structlog
from config import settings
from db.repositories import audit as audit_repo, settings as settings_repo
from ai.router import router as model_router
from bot.keyboards.admin import get_admin_main_menu
from b24.client import Bitrix24Client
from reports.builder import build_daily_report, build_period_report

logger = structlog.get_logger()

router = Router()


def is_admin(user_context: dict = None) -> bool:
    """Check if user is admin."""
    if not user_context:
        return False
    return user_context.get("is_admin", False) or user_context["telegram_id"] == settings.admin_telegram_id


@router.message(Command("admin"))
async def admin_command(message: types.Message, user_context: dict = None):
    """Open admin panel."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен. Это команда только для администратора.")
        return

    default_model = await settings_repo.get("default_model") or "claude-sonnet-4-6"
    routing_mode = await settings_repo.get("routing_mode") or "auto"

    response = f"""🔧 АДМИН-ПАНЕЛЬ

Текущая конфигурация:
📌 Модель: {default_model}
🔄 Маршрутизация: {routing_mode}

Выберите действие:
"""

    await message.answer(response, reply_markup=get_admin_main_menu())
    logger.info("Admin panel opened", admin_id=user_context["telegram_id"])


@router.message(Command("set_model"))
async def set_model_command(message: types.Message, user_context: dict = None):
    """Set AI model."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "Использование: /set_model <sonnet|haiku|opus>\n\n"
            "Примеры:\n"
            "/set_model sonnet\n"
            "/set_model haiku\n"
            "/set_model opus"
        )
        return

    model_name = args[1].lower()

    model_mapping = {
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5",
        "opus": "claude-opus-4-7"
    }

    if model_name not in model_mapping:
        await message.answer(f"Неизвестная модель: {model_name}")
        return

    full_model_name = model_mapping[model_name]
    await model_router.set_default_model(full_model_name)
    await settings_repo.set("default_model", full_model_name)

    await message.answer(f"✅ Модель изменена на: {model_name.upper()}")
    logger.info("Model changed", admin_id=user_context["telegram_id"], model=full_model_name)


@router.message(Command("stats"))
async def stats_command(message: types.Message, user_context: dict = None):
    """Show usage statistics."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    try:
        stats = await audit_repo.get_stats(days=7)

        response = "📊 СТАТИСТИКА (последние 7 дней)\n\n"
        response += f"Всего запросов: {stats.get('total_requests', 0)}\n"
        response += f"Ошибок: {stats.get('error_count', 0)}\n"
        response += f"Входных токенов: {stats.get('total_input_tokens', 0)}\n"
        response += f"Выходных токенов: {stats.get('total_output_tokens', 0)}\n"
        response += f"Потрачено credits: {stats.get('total_credits', 0):.2f}\n\n"

        if stats.get('model_distribution'):
            response += "Распределение по моделям:\n"
            for model, count in stats['model_distribution'].items():
                response += f"  {model}: {count}\n"

        await message.answer(response)
        logger.info("Stats shown", admin_id=user_context["telegram_id"])

    except Exception as e:
        logger.error("Error getting stats", error=str(e))
        await message.answer(f"Ошибка при получении статистики: {str(e)}")


@router.message(Command("report_now"))
async def report_now_command(message: types.Message, user_context: dict = None):
    """Build and post a report on demand.

    /report_now              — daily for yesterday into REPORTS_CHAT_ID
    /report_now daily        — same as above
    /report_now weekly       — previous Mon-Sun week
    /report_now monthly      — previous calendar month
    /report_now here         — daily, but reply into THIS chat (for testing)
    """
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    args = (message.text or "").split()
    kind = args[1].lower() if len(args) > 1 else "daily"

    target_chat = message.chat.id if "here" in args else settings.reports_chat_id
    if not target_chat:
        await message.answer("REPORTS_CHAT_ID не настроен. Используй `/report_now here` для проверки в этом чате.")
        return

    await message.answer(f"⏳ Собираю отчёт ({kind})...")

    b24 = Bitrix24Client()
    try:
        if kind in ("daily", "here"):
            text = await build_daily_report(b24, date.today() - timedelta(days=1))
        elif kind == "weekly":
            today = date.today()
            offset = (today.weekday() + 7) if today.weekday() == 0 else today.weekday()
            last_mon = today - timedelta(days=offset)
            last_sun = last_mon + timedelta(days=6)
            text = await build_period_report(b24, last_mon, last_sun, "Еженедельный отчёт")
        elif kind == "monthly":
            today = date.today()
            first_this = today.replace(day=1)
            last_prev = first_this - timedelta(days=1)
            first_prev = last_prev.replace(day=1)
            text = await build_period_report(b24, first_prev, last_prev, "Ежемесячный отчёт")
        else:
            await message.answer("Использование: /report_now [daily|weekly|monthly] [here]")
            return
    finally:
        if b24._session and not b24._session.closed:
            await b24._session.close()

    # Send (chunked if needed)
    for i in range(0, len(text), 4000):
        await message.bot.send_message(target_chat, text[i:i + 4000], parse_mode=ParseMode.HTML)

    if target_chat != message.chat.id:
        await message.answer(f"✅ Отчёт отправлен в чат {target_chat}")


@router.message(Command("errors"))
async def errors_command(message: types.Message, user_context: dict = None):
    """Show recent errors."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    try:
        errors = await audit_repo.get_recent_errors(limit=5)

        if not errors:
            await message.answer("❌ Нет ошибок в последних записях.")
            return

        response = "❌ ПОСЛЕДНИЕ ОШИБКИ\n\n"
        for err in errors:
            response += f"ID: {err['id']}\n"
            response += f"Пользователь: {err['telegram_id']}\n"
            response += f"Вопрос: {err['question'][:100]}...\n"
            response += f"Ошибка: {err['error'][:100]}...\n"
            response += f"Время: {err['created_at']}\n\n"

        await message.answer(response)
        logger.info("Errors shown", admin_id=user_context["telegram_id"])

    except Exception as e:
        logger.error("Error getting errors", error=str(e))
        await message.answer(f"Ошибка: {str(e)}")


@router.message(Command("leads"))
async def leads_command(message: types.Message, user_context: dict = None):
    """Show the most recent collected lead reports (sphere ИТМ chat)."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    from lead_reports.lead_db import get_recent

    try:
        leads = await get_recent(limit=10)
        if not leads:
            await message.answer("📭 Лидов пока не собрано.")
            return

        lines = ["📋 ПОСЛЕДНИЕ ЛИДЫ (sphere ИТМ)\n"]
        for ld in leads:
            comment = (ld.get("comment") or "").replace("\n", " ")
            if len(comment) > 90:
                comment = comment[:90] + "…"
            lines.append(
                f"• {ld.get('call_datetime') or '—'} | "
                f"{ld.get('company') or '—'} | {ld.get('phone') or '—'}\n"
                f"  {ld.get('fio') or '—'} · {comment or '—'}"
            )
        await message.answer("\n".join(lines))
        logger.info("Leads shown", admin_id=user_context["telegram_id"])
    except Exception as e:
        logger.error("Error getting leads", error=str(e))
        await message.answer(f"Ошибка: {str(e)}")


@router.message(Command("leads_stats"))
async def leads_stats_command(message: types.Message, user_context: dict = None):
    """Show aggregate stats for collected lead reports."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    from lead_reports.lead_db import get_stats

    try:
        stats = await get_stats()
        by_status = stats.get("by_status", {})
        status_lines = "\n".join(
            f"  • {k}: {v}" for k, v in by_status.items()
        ) or "  —"
        await message.answer(
            "📊 СТАТИСТИКА ЛИДОВ (sphere ИТМ)\n\n"
            f"Всего собрано: {stats.get('total', 0)}\n"
            f"По статусам:\n{status_lines}\n\n"
            f"Последний звонок: {stats.get('last_call_datetime') or '—'}"
        )
        logger.info("Leads stats shown", admin_id=user_context["telegram_id"])
    except Exception as e:
        logger.error("Error getting leads stats", error=str(e))
        await message.answer(f"Ошибка: {str(e)}")


@router.message(Command("debug"))
async def debug_command(message: types.Message, user_context: dict = None):
    """On-demand failure digest for the last 24h."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return
    from reports.error_digest import build_error_digest
    try:
        text = await build_error_digest(hours=24)
        await message.answer(text, parse_mode=ParseMode.HTML)
        logger.info("Debug digest shown", admin_id=user_context["telegram_id"])
    except Exception as e:
        logger.error("Debug digest failed", error=str(e))
        await message.answer(f"Ошибка: {e}")


@router.message(Command("diagnose"))
async def diagnose_command(message: types.Message, user_context: dict = None):
    """On-demand AI debug review for the last 24h (same as the 08:30 cron)."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return
    from reports.error_digest import build_error_diagnosis
    placeholder = await message.answer("🛠 Собираю разбор — audit_log + journalctl → DeepSeek…")
    try:
        text = await build_error_diagnosis(hours=24)
        await placeholder.edit_text(text, parse_mode=ParseMode.HTML)
        logger.info("Diagnose run", admin_id=user_context["telegram_id"], chars=len(text))
    except Exception as e:
        logger.error("Diagnose failed", error=str(e))
        await placeholder.edit_text(f"Ошибка: {e}")


@router.message(Command("morning_now"))
async def morning_now_command(message: types.Message, user_context: dict = None):
    """Запустить ежедневный утренний combined-отчёт ВНЕ расписания.

    Использует тот же send_manager_daily который cron вызывает в 09:00:
    сначала активность за вчера → РОП-чат, потом growth-блок (через
    DeepSeek по сделкам с активностью за 48ч).
    """
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return
    from reports.manager_daily import send_manager_daily
    # При вызове из ЛИЧНОГО чата (chat.type == 'private') отчёт шлём
    # тестово в эту же личку — чтобы РОП-чат не засорять. В обычной
    # ситуации (cron в 09:00) override_chat_id=0 и идёт в РОП-чат.
    is_private = message.chat.type == "private"
    target_chat_id = message.chat.id if is_private else 0
    target_desc = "в эту личку (тест)" if is_private else "в РОП-чат"
    placeholder = await message.answer(
        f"🌅 Запускаю утренний отчёт, пришлю {target_desc}. "
        "Manager-блок придёт сразу, growth-блок через 1-2 минуты "
        "(DeepSeek работает)…"
    )
    try:
        # bot.send_message внутри send_manager_daily использует тот же
        # объект Bot который сейчас работает → правильная сессия + прокси.
        await send_manager_daily(message.bot, override_chat_id=target_chat_id)
        await placeholder.edit_text(f"✅ Утренний отчёт отправлен {target_desc}")
        logger.info("Morning-now run",
                    admin_id=user_context["telegram_id"],
                    target_chat_id=target_chat_id or settings.manager_daily_chat_id)
    except Exception as e:
        logger.error("Morning-now failed", error=str(e))
        await placeholder.edit_text(f"❌ Ошибка: {e}")


@router.message(Command("transcribe_pending"))
async def transcribe_pending_command(message: types.Message, user_context: dict = None):
    """Transcribe all not-yet-processed lead recordings (backfill)."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    import asyncio
    from lead_reports.pipeline import transcribe_pending

    await message.answer(
        "🎧 Запускаю транскрибацию накопленных звонков в фоне.\n"
        "Это может занять время (~1-3 мин на звонок). "
        "Прогресс смотри через /leads_stats."
    )

    async def _run():
        try:
            res = await transcribe_pending(limit=200)
            await message.answer(
                f"✅ Транскрибация завершена.\n"
                f"Обработано: {res['processed']}, "
                f"успешно: {res['ok']}, ошибок: {res['failed']}"
            )
        except Exception as e:
            logger.error("transcribe_pending command failed", error=str(e))
            await message.answer(f"⚠️ Ошибка транскрибации: {e}")

    asyncio.create_task(_run())
    logger.info("Transcription backfill triggered", admin_id=user_context["telegram_id"])


@router.message(Command("export_leads_sheet"))
async def export_leads_sheet_command(message: types.Message, user_context: dict = None):
    """Export transcribed lead calls to the Google Sheet."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    from lead_reports.sheets_exporter import export_pending, is_enabled

    if not is_enabled():
        await message.answer(
            "⚠️ Экспорт не настроен: задай LEAD_REPORTS_SHEET_ID в .env "
            "и дай сервисному аккаунту доступ к таблице."
        )
        return

    await message.answer("📤 Выгружаю расшифровки в Google-таблицу...")
    try:
        res = await export_pending(limit=1000)
        if res.get("error"):
            await message.answer(f"⚠️ Ошибка экспорта: {res['error']}")
        else:
            await message.answer(f"✅ Выгружено строк: {res.get('exported', 0)}")
        logger.info("Sheet export triggered", admin_id=user_context["telegram_id"])
    except Exception as e:
        logger.error("export_leads_sheet command failed", error=str(e))
        await message.answer(f"⚠️ Ошибка: {e}")
