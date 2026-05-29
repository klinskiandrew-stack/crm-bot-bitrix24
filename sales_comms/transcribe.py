"""Whisper-worker для расшифровки звонков из deal_communications.

Тащит из БД pending-записи (call с audio_url='disk:{file_id}'), скачивает
MP3 через Bitrix Disk, прогоняет через faster-whisper, записывает text
и проставляет status='done'. Ошибки фиксирует с error_message, чтобы
повторный прогон не зацикливался на одной и той же битой записи.

Один прогон обрабатывает не больше N звонков (default 3), потому что
Whisper жуёт ~realtime (5-минутный звонок = 5 минут CPU). При cron'е
каждые 5 минут это даёт ~36 звонков в час — за сутки 800+, бэкфилл
активных сделок переваривается за пару дней.

RAM-страховка: модель сидит в памяти 1.2 ГБ. После прогона батча НЕ
выгружаем (повторная загрузка ~9с) — но если бот ещё используется
voice-командами, они шарят тот же _model через lead_reports/stt.py.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

import structlog

from b24.client import Bitrix24Client
from lead_reports import stt   # переиспользуем глобальный Whisper-singleton
from sales_comms.db import (
    mark_transcription_done,
    mark_transcription_failed,
    pending_transcriptions,
)

logger = structlog.get_logger()


def _parse_audio_url(url: str) -> Optional[int]:
    """audio_url хранится как 'disk:{file_id}'. Возвращает int file_id."""
    if not url or not url.startswith("disk:"):
        return None
    try:
        return int(url.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


async def _download_to_tmp(client: Bitrix24Client, file_id: int) -> Optional[Path]:
    """Скачать файл из Bitrix Disk во временный mp3 на диске."""
    body = await client.download_disk_file(file_id)
    if not body:
        return None
    # mp3-расширение для большинства телефонных записей; faster-whisper
    # читает через ffmpeg и сам определит формат.
    fd, path_str = tempfile.mkstemp(prefix=f"call-{file_id}-", suffix=".mp3")
    path = Path(path_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


async def transcribe_one(client: Bitrix24Client, comm: dict) -> bool:
    """Обработать одну pending-запись. Возвращает True на успех."""
    comm_id = comm["id"]
    audio_url = comm.get("audio_url")
    file_id = _parse_audio_url(audio_url or "")
    if file_id is None:
        await mark_transcription_failed(comm_id, f"audio_url={audio_url!r} не парсится")
        return False

    tmp_path: Optional[Path] = None
    try:
        tmp_path = await _download_to_tmp(client, file_id)
        if tmp_path is None:
            await mark_transcription_failed(comm_id, "disk.file.get не отдал содержимое")
            return False

        async with stt.transcribe_lock:
            # blocking → off the event loop
            text = await asyncio.to_thread(stt.transcribe, str(tmp_path))

        if not text or not text.strip():
            await mark_transcription_failed(comm_id, "пустой транскрипт (тишина или очень короткий звонок)")
            return False

        await mark_transcription_done(comm_id, text.strip())
        logger.info(
            "Sales-comms call transcribed",
            comm_id=comm_id,
            file_id=file_id,
            chars=len(text),
            duration_sec=comm.get("duration_sec"),
        )
        return True
    except Exception as e:
        logger.error("transcribe_one failed", comm_id=comm_id, file_id=file_id, error=str(e))
        await mark_transcription_failed(comm_id, str(e))
        return False
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


async def run_batch(limit: int = 3) -> dict:
    """Cron-точка входа. Обработать до N pending-звонков. Возвращает
    счётчики для логирования: {'processed', 'ok', 'failed'}."""
    pending = await pending_transcriptions(limit=limit)
    if not pending:
        return {"processed": 0, "ok": 0, "failed": 0}

    logger.info("Sales-comms transcribe batch starting", queued=len(pending))
    client = Bitrix24Client()
    ok = failed = 0
    try:
        for item in pending:
            success = await transcribe_one(client, item)
            if success:
                ok += 1
            else:
                failed += 1
    finally:
        try:
            await client.close()
        except Exception:
            pass

    logger.info(
        "Sales-comms transcribe batch finished",
        processed=len(pending), ok=ok, failed=failed,
    )
    # ВАЖНО: выгружаем модель Whisper после батча. На сервере 3.8GB RAM,
    # модель занимает 1.2GB; если её держать постоянно + bot ~600MB +
    # пиковая нагрузка → OOM-killer убивает процесс. Reload между батчами
    # стоит ~9 сек — копейки по сравнению с риском перезапуска.
    try:
        stt.unload()
    except Exception as e:
        logger.warning("Whisper unload failed", error=str(e))

    return {"processed": len(pending), "ok": ok, "failed": failed}
