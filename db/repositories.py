import json
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import structlog
from db.connection import db

logger = structlog.get_logger()


class UserRepository:
    async def get_by_telegram_id(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """Get user by telegram_id."""
        row = await db.fetch_one(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,)
        )
        if not row:
            return None
        return dict(row)

    async def create_user(
        self,
        telegram_id: int,
        role: str = "partner",
        b24_user_ids: List[int] = None,
        display_name: str = None
    ) -> Dict[str, Any]:
        """Create new user."""
        b24_ids_json = json.dumps(b24_user_ids or [])
        await db.execute(
            """INSERT INTO users (telegram_id, role, b24_user_ids, display_name, is_active)
               VALUES (?, ?, ?, ?, 1)""",
            (telegram_id, role, b24_ids_json, display_name)
        )
        await db.commit()
        logger.info("User created", telegram_id=telegram_id, role=role)
        return await self.get_by_telegram_id(telegram_id)

    async def update_user(
        self,
        telegram_id: int,
        **kwargs
    ) -> Dict[str, Any]:
        """Update user fields."""
        if "b24_user_ids" in kwargs and isinstance(kwargs["b24_user_ids"], list):
            kwargs["b24_user_ids"] = json.dumps(kwargs["b24_user_ids"])

        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [telegram_id]

        await db.execute(
            f"UPDATE users SET {fields} WHERE telegram_id = ?",
            values
        )
        await db.commit()
        logger.info("User updated", telegram_id=telegram_id)
        return await self.get_by_telegram_id(telegram_id)

    async def list_users(self, role: str = None) -> List[Dict[str, Any]]:
        """List all users, optionally filtered by role."""
        if role:
            rows = await db.fetch_all(
                "SELECT * FROM users WHERE role = ? AND is_active = 1",
                (role,)
            )
        else:
            rows = await db.fetch_all(
                "SELECT * FROM users WHERE is_active = 1"
            )
        return [dict(row) for row in rows] if rows else []


class SettingsRepository:
    async def get(self, key: str) -> Optional[str]:
        """Get setting value by key."""
        row = await db.fetch_one(
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        )
        return row["value"] if row else None

    async def set(self, key: str, value: str):
        """Set or update setting."""
        existing = await self.get(key)
        if existing is None:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
        else:
            await db.execute(
                "UPDATE settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
                (value, key)
            )
        await db.commit()

    async def get_all(self) -> Dict[str, str]:
        """Get all settings as dict."""
        rows = await db.fetch_all("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in rows} if rows else {}


class SessionRepository:
    async def get_session(self, telegram_id: int, chat_id: int) -> Optional[List[Dict]]:
        """Get session messages."""
        row = await db.fetch_one(
            "SELECT messages_json FROM sessions WHERE telegram_id = ? AND chat_id = ?",
            (telegram_id, chat_id)
        )
        if not row:
            return None
        return json.loads(row["messages_json"])

    async def save_session(
        self,
        telegram_id: int,
        chat_id: int,
        messages: List[Dict],
        ttl_minutes: int = 30
    ):
        """Save or update session."""
        messages_json = json.dumps(messages)
        await db.execute(
            """INSERT INTO sessions (telegram_id, chat_id, messages_json, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(telegram_id, chat_id)
               DO UPDATE SET messages_json = ?, updated_at = CURRENT_TIMESTAMP""",
            (telegram_id, chat_id, messages_json, messages_json)
        )
        await db.commit()

    async def cleanup_expired_sessions(self, ttl_minutes: int = 30):
        """Delete expired sessions."""
        cutoff_time = datetime.utcnow() - timedelta(minutes=ttl_minutes)
        await db.execute(
            "DELETE FROM sessions WHERE updated_at < ?",
            (cutoff_time.isoformat(),)
        )
        await db.commit()


class AuditRepository:
    async def log_request(
        self,
        telegram_id: int,
        chat_id: int,
        chat_type: str,
        question: str,
        model_used: str,
        tools_called: List[str] = None,
        answer: str = None,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        credits_consumed: float = 0,
        duration_ms: int = 0,
        error: str = None
    ) -> int:
        """Log a request to audit_log and return id."""
        tools_json = json.dumps(tools_called or [])
        cursor = await db.execute(
            """INSERT INTO audit_log (
                telegram_id, chat_id, chat_type, question, model_used,
                tools_called, answer, input_tokens, cached_input_tokens,
                output_tokens, credits_consumed, duration_ms, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                telegram_id, chat_id, chat_type, question, model_used,
                tools_json, answer, input_tokens, cached_input_tokens,
                output_tokens, credits_consumed, duration_ms, error
            )
        )
        await db.commit()
        return cursor.lastrowid

    async def get_stats(
        self,
        telegram_id: int = None,
        days: int = 7
    ) -> Dict[str, Any]:
        """Get usage statistics."""
        cutoff_time = datetime.utcnow() - timedelta(days=days)
        cutoff_iso = cutoff_time.isoformat()

        query = "SELECT * FROM audit_log WHERE created_at >= ?"
        params = [cutoff_iso]

        if telegram_id:
            query += " AND telegram_id = ?"
            params.append(telegram_id)

        rows = await db.fetch_all(query, params)
        if not rows:
            return {"total_requests": 0}

        rows = [dict(row) for row in rows]
        model_counts = {}
        total_input_tokens = 0
        total_output_tokens = 0
        total_credits = 0
        error_count = 0

        for row in rows:
            model = row["model_used"]
            model_counts[model] = model_counts.get(model, 0) + 1
            total_input_tokens += row.get("input_tokens", 0)
            total_output_tokens += row.get("output_tokens", 0)
            total_credits += row.get("credits_consumed", 0)
            if row.get("error"):
                error_count += 1

        return {
            "total_requests": len(rows),
            "model_distribution": model_counts,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_credits": total_credits,
            "error_count": error_count,
            "avg_input_tokens": total_input_tokens // len(rows) if rows else 0
        }

    async def get_daily_credits_spent(self) -> float:
        """Sum credits consumed today (UTC midnight to now) across all users."""
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        rows = await db.fetch_all(
            "SELECT credits_consumed FROM audit_log WHERE created_at >= ?",
            (today_start,)
        )
        if not rows:
            return 0.0
        return float(sum((row["credits_consumed"] or 0) for row in rows))

    async def get_recent_errors(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent errors from audit log."""
        rows = await db.fetch_all(
            """SELECT id, telegram_id, question, error, created_at
               FROM audit_log WHERE error IS NOT NULL
               ORDER BY created_at DESC LIMIT ?""",
            (limit,)
        )
        return [dict(row) for row in rows] if rows else []

    async def cleanup_old_logs(self, days: int = 90):
        """Delete logs older than N days."""
        cutoff_time = datetime.utcnow() - timedelta(days=days)
        cutoff_iso = cutoff_time.isoformat()
        await db.execute(
            "DELETE FROM audit_log WHERE created_at < ?",
            (cutoff_iso,)
        )
        await db.commit()
        logger.info("Cleaned up old audit logs")


class ChatMembersRepository:
    """People (not bots) seen writing in a chat. Used for participant picker."""

    async def upsert(
        self,
        chat_id: int,
        user_id: int,
        full_name: str = None,
        username: str = None,
    ):
        await db.execute(
            """INSERT INTO chat_members (chat_id, user_id, full_name, username, last_seen_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(chat_id, user_id)
               DO UPDATE SET full_name=excluded.full_name,
                             username=excluded.username,
                             last_seen_at=CURRENT_TIMESTAMP""",
            (chat_id, user_id, full_name, username),
        )
        await db.commit()

    async def list_for_chat(self, chat_id: int) -> List[Dict[str, Any]]:
        rows = await db.fetch_all(
            """SELECT user_id, full_name, username, last_seen_at
               FROM chat_members WHERE chat_id = ?
               ORDER BY last_seen_at DESC""",
            (chat_id,),
        )
        return [dict(r) for r in rows] if rows else []


class MeetingPollsRepository:
    async def create(
        self,
        chat_id: int,
        initiator_id: int,
        meeting_date: str,
        duration_min: int,
        topic: str,
        deadline_at: str,
    ) -> int:
        cur = await db.execute(
            """INSERT INTO meeting_polls
               (chat_id, initiator_id, meeting_date, duration_min, topic, status, deadline_at)
               VALUES (?, ?, ?, ?, ?, 'picking_participants', ?)""",
            (chat_id, initiator_id, meeting_date, duration_min, topic, deadline_at),
        )
        await db.commit()
        return cur.lastrowid

    async def get(self, poll_id: int) -> Optional[Dict[str, Any]]:
        row = await db.fetch_one(
            "SELECT * FROM meeting_polls WHERE id = ?", (poll_id,)
        )
        return dict(row) if row else None

    async def update(self, poll_id: int, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields.keys())
        values = list(fields.values()) + [poll_id]
        await db.execute(f"UPDATE meeting_polls SET {sets} WHERE id = ?", values)
        await db.commit()

    async def add_participant(self, poll_id: int, user_id: int, full_name: str = None):
        await db.execute(
            """INSERT OR REPLACE INTO meeting_poll_participants (poll_id, user_id, full_name)
               VALUES (?, ?, ?)""",
            (poll_id, user_id, full_name),
        )
        await db.commit()

    async def remove_participant(self, poll_id: int, user_id: int):
        await db.execute(
            "DELETE FROM meeting_poll_participants WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        )
        await db.commit()

    async def list_participants(self, poll_id: int) -> List[Dict[str, Any]]:
        rows = await db.fetch_all(
            "SELECT user_id, full_name FROM meeting_poll_participants WHERE poll_id = ?",
            (poll_id,),
        )
        return [dict(r) for r in rows] if rows else []

    async def set_vote(self, poll_id: int, slot_hour: int, user_id: int, vote: Optional[str]):
        """Set vote to 'yes'/'no', or None to clear (delete)."""
        if vote is None:
            await db.execute(
                "DELETE FROM meeting_poll_votes WHERE poll_id=? AND slot_hour=? AND user_id=?",
                (poll_id, slot_hour, user_id),
            )
        else:
            await db.execute(
                """INSERT INTO meeting_poll_votes (poll_id, slot_hour, user_id, vote)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(poll_id, slot_hour, user_id) DO UPDATE SET vote=excluded.vote""",
                (poll_id, slot_hour, user_id, vote),
            )
        await db.commit()

    async def get_vote(self, poll_id: int, slot_hour: int, user_id: int) -> Optional[str]:
        row = await db.fetch_one(
            "SELECT vote FROM meeting_poll_votes WHERE poll_id=? AND slot_hour=? AND user_id=?",
            (poll_id, slot_hour, user_id),
        )
        return row["vote"] if row else None

    async def get_all_votes(self, poll_id: int) -> List[Dict[str, Any]]:
        rows = await db.fetch_all(
            "SELECT slot_hour, user_id, vote FROM meeting_poll_votes WHERE poll_id=?",
            (poll_id,),
        )
        return [dict(r) for r in rows] if rows else []

    async def list_expired_open(self) -> List[Dict[str, Any]]:
        """Polls past deadline but not finalized."""
        rows = await db.fetch_all(
            """SELECT * FROM meeting_polls
               WHERE status IN ('picking_participants', 'voting')
                 AND deadline_at IS NOT NULL
                 AND deadline_at <= CURRENT_TIMESTAMP""",
        )
        return [dict(r) for r in rows] if rows else []


class ScheduledMeetingsRepository:
    async def create(
        self,
        chat_id: int,
        initiator_id: Optional[int],
        topic: str,
        start_at_utc_iso: str,
        duration_min: int,
        zoom_meeting_id: str,
        zoom_join_url: str,
        zoom_start_url: str,
        participants: List[Dict[str, Any]],
    ) -> int:
        cur = await db.execute(
            """INSERT INTO scheduled_meetings
               (chat_id, initiator_id, topic, start_at, duration_min,
                zoom_meeting_id, zoom_join_url, zoom_start_url, participants_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                chat_id, initiator_id, topic, start_at_utc_iso, duration_min,
                zoom_meeting_id, zoom_join_url, zoom_start_url,
                json.dumps(participants, ensure_ascii=False),
            ),
        )
        await db.commit()
        return cur.lastrowid

    async def due_for_reminder(self, minutes_before: int) -> List[Dict[str, Any]]:
        """Meetings starting in (minutes_before .. minutes_before+5) min that
        haven't had a reminder sent yet."""
        now = datetime.utcnow()
        lower = (now + timedelta(minutes=minutes_before)).isoformat()
        upper = (now + timedelta(minutes=minutes_before + 5)).isoformat()
        rows = await db.fetch_all(
            """SELECT * FROM scheduled_meetings
               WHERE reminder_sent_at IS NULL
                 AND start_at BETWEEN ? AND ?""",
            (lower, upper),
        )
        return [dict(r) for r in rows] if rows else []

    async def mark_reminder_sent(self, meeting_id: int):
        await db.execute(
            "UPDATE scheduled_meetings SET reminder_sent_at = CURRENT_TIMESTAMP WHERE id = ?",
            (meeting_id,),
        )
        await db.commit()


# Repository instances
users = UserRepository()
settings = SettingsRepository()
sessions = SessionRepository()
audit = AuditRepository()
chat_members = ChatMembersRepository()
meeting_polls = MeetingPollsRepository()
scheduled_meetings = ScheduledMeetingsRepository()
