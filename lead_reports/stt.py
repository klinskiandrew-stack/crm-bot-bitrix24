"""Speech-to-text wrapper around faster-whisper (local, CPU).

The model is a lazy singleton — loaded into RAM on the first transcribe
call and kept resident. On this 3.8GB box that's the tightest resource,
so the model size is configurable (whisper_model) and a 2GB swap file
backs it up. transcribe() is blocking; callers run it via asyncio.to_thread.
"""

import asyncio
import threading
import time

import structlog

from config import settings

logger = structlog.get_logger()

_model = None
_model_lock = threading.Lock()

# Process-wide async lock — only ONE Whisper transcription runs at a
# time. Two concurrent runs need ~2.5GB and would OOM the box. Shared by
# the lead-reports pipeline (call recordings) and the voice-command
# handler (Telegram voice messages).
transcribe_lock = asyncio.Lock()


def _get_model():
    """Load (once) and return the WhisperModel. Thread-safe."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel

        t0 = time.time()
        logger.info(
            "Loading Whisper model",
            model=settings.whisper_model,
            compute_type=settings.whisper_compute_type,
        )
        _model = WhisperModel(
            settings.whisper_model,
            device="cpu",
            compute_type=settings.whisper_compute_type,
        )
        logger.info("Whisper model loaded", load_sec=round(time.time() - t0, 1))
    return _model


def transcribe(audio_path: str) -> str:
    """Transcribe a Russian audio file to plain text. Blocking — call
    from a worker thread, not the event loop."""
    model = _get_model()
    t0 = time.time()
    segments, info = model.transcribe(
        audio_path,
        language="ru",
        vad_filter=True,  # skip long silences — telephone calls have plenty
    )
    # segments is a generator — transcription actually runs while iterating.
    text = " ".join(seg.text.strip() for seg in segments).strip()
    logger.info(
        "Audio transcribed",
        audio=audio_path,
        chars=len(text),
        duration_sec=round(getattr(info, "duration", 0), 1),
        elapsed_sec=round(time.time() - t0, 1),
    )
    return text


def unload() -> None:
    """Drop the model from RAM (~1.2GB freed). The pipeline calls this
    after finishing a batch so the bot doesn't hold the model resident
    while idle — reload costs only ~9s on the next call."""
    global _model
    with _model_lock:
        if _model is not None:
            _model = None
            logger.info("Whisper model unloaded from RAM")
