"""Zoom Server-to-Server OAuth client.

Caches the access token in memory (~50 min TTL — Zoom tokens live 1 hour,
we refresh proactively). Only the 'create_meeting' surface is exposed —
that is all the meetings module currently needs.

Docs:
  - https://developers.zoom.us/docs/internal-apps/s2s-oauth/
  - https://developers.zoom.us/docs/api/meetings/methods/#operation/meetingCreate
"""

import asyncio
import base64
import time
from typing import Optional

import aiohttp
import structlog

from config import settings

logger = structlog.get_logger()

_TOKEN_URL = "https://zoom.us/oauth/token"
_API_BASE = "https://api.zoom.us/v2"
_TOKEN_TTL_SAFETY = 600  # refresh 10 min before expiry


class ZoomError(RuntimeError):
    pass


class ZoomClient:
    def __init__(self):
        self.account_id = settings.zoom_account_id
        self.client_id = settings.zoom_client_id
        self.client_secret = settings.zoom_client_secret
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def configured(self) -> bool:
        return bool(self.account_id and self.client_id and self.client_secret)

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        async with self._lock:
            if self._token and time.time() < self._token_expires_at - _TOKEN_TTL_SAFETY:
                return self._token

            auth = base64.b64encode(
                f"{self.client_id}:{self.client_secret}".encode()
            ).decode()
            async with session.post(
                _TOKEN_URL,
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "account_credentials",
                    "account_id": self.account_id,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error("Zoom token request failed", status=resp.status, body=text[:300])
                    raise ZoomError(f"Zoom token error {resp.status}: {text[:200]}")
                data = await resp.json(content_type=None) if not text else __import__("json").loads(text)
            self._token = data["access_token"]
            self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
            logger.info("Zoom token refreshed", expires_in=data.get("expires_in"))
            return self._token

    async def create_meeting(
        self,
        topic: str,
        start_at_iso: str,
        duration_min: int,
        timezone: str = "Europe/Moscow",
    ) -> dict:
        """Create a scheduled Zoom meeting.

        `start_at_iso` must be in ISO 8601 without timezone (Zoom interprets
        in the provided `timezone`), e.g. '2026-05-20T17:00:00'.
        Returns the parsed Zoom response dict (incl. join_url, id, password).
        """
        if not self.configured():
            raise ZoomError("Zoom is not configured (set ZOOM_ACCOUNT_ID / CLIENT_ID / CLIENT_SECRET)")

        body = {
            "topic": topic or "Созвон",
            "type": 2,  # scheduled
            "start_time": start_at_iso,
            "duration": int(duration_min),
            "timezone": timezone,
            "settings": {
                "join_before_host": True,
                "jbh_time": 0,
                "waiting_room": False,
                "approval_type": 2,  # no registration
                "audio": "both",
                "auto_recording": "none",
            },
        }

        async with aiohttp.ClientSession() as session:
            token = await self._get_token(session)
            async with session.post(
                f"{_API_BASE}/users/me/meetings",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logger.error("Zoom create_meeting failed", status=resp.status, body=text[:300])
                    raise ZoomError(f"Zoom API {resp.status}: {text[:200]}")
                import json as _json
                data = _json.loads(text)

        logger.info(
            "Zoom meeting created",
            meeting_id=data.get("id"),
            topic=topic,
            start=start_at_iso,
            duration=duration_min,
        )
        return data


zoom_client = ZoomClient()
