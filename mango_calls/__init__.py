"""Mango Telecom call analysis — fetches recordings from a lead's CRM
timeline, transcribes them via the local Whisper, and grades the
manager's work against the regulation file (`manager_call_script.md`).

Uses the same STT + analyser as `lead_reports` for КЦ calls — just a
different source (CRM activities instead of the Sphere ИТМ chat) and a
different evaluation script.
"""
