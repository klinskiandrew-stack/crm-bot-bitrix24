"""Lead reports module — collects structured lead reports posted by the
'Amely' bot into the sphere ИТМ Telegram chat, parses them, stores them,
and (later stages) transcribes the call recordings and runs AI analysis.

Stage 1: Telethon listener + report parser + SQLite storage.
"""
