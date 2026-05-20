#!/usr/bin/env python3
"""Three-step Telethon authorization for the lead-reports listener.

Interactive prompts don't work over SSH, so auth is split into steps.
Creates the dedicated session at settings.telethon_session_path —
separate from barrel-bot's sessions (one .session = one live process).

Usage (run on the server, from /opt/crm-bot):
    venv/bin/python scripts/telethon_auth.py request +79957891919
    venv/bin/python scripts/telethon_auth.py verify 12345
    venv/bin/python scripts/telethon_auth.py password '<2FA_PASSWORD>'
    venv/bin/python scripts/telethon_auth.py status
"""

import asyncio
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from config import settings  # noqa: E402
from telethon import TelegramClient  # noqa: E402
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError  # noqa: E402

API_ID = settings.telethon_api_id
API_HASH = settings.telethon_api_hash
SESSION_PATH = Path(settings.telethon_session_path)
STATE_FILE = Path(f"/tmp/telethon_auth_state_{SESSION_PATH.name}.json")


def _check_creds():
    if not API_ID or not API_HASH:
        print("❌ telethon_api_id / telethon_api_hash не заданы в .env")
        sys.exit(1)


def _make_client() -> TelegramClient:
    """TelegramClient with the high retry counts required for the flaky
    Timeweb→Telegram link — the default of 5 attempts isn't enough."""
    return TelegramClient(
        str(SESSION_PATH), API_ID, API_HASH,
        connection_retries=30, retry_delay=2,
    )


async def request_code(phone: str):
    _check_creds()
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    client = _make_client()
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"✅ Уже авторизован: {me.first_name} (@{me.username or '—'}, id={me.id})")
        await client.disconnect()
        return

    sent = await client.send_code_request(phone)
    STATE_FILE.write_text(json.dumps({"phone": phone, "phone_code_hash": sent.phone_code_hash}))
    STATE_FILE.chmod(0o600)
    print(f"✅ Код отправлен на {phone}")
    print("   Проверь Telegram (сообщение от @Telegram, НЕ SMS) — 5-значный код.")
    print("   Дальше: venv/bin/python scripts/telethon_auth.py verify <КОД>")
    await client.disconnect()


async def verify_code(code: str):
    _check_creds()
    if not STATE_FILE.exists():
        print("❌ Нет ожидающей авторизации — сначала 'request <phone>'")
        sys.exit(1)

    state = json.loads(STATE_FILE.read_text())
    client = _make_client()
    await client.connect()
    try:
        await client.sign_in(
            phone=state["phone"],
            code=code,
            phone_code_hash=state["phone_code_hash"],
        )
        me = await client.get_me()
        print(f"✅ Вошёл как {me.first_name} (@{me.username or '—'}, id={me.id})")
        print(f"   Сессия сохранена: {SESSION_PATH}.session")
        STATE_FILE.unlink(missing_ok=True)
    except PhoneCodeInvalidError:
        print("❌ Неверный код. Проверь и повтори.")
    except SessionPasswordNeededError:
        print("⚠️  Нужен пароль 2FA. Запусти:")
        print("   venv/bin/python scripts/telethon_auth.py password '<2FA_ПАРОЛЬ>'")
    finally:
        await client.disconnect()


async def verify_password(password: str):
    _check_creds()
    client = _make_client()
    await client.connect()
    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        print(f"✅ Вошёл (2FA) как {me.first_name} (@{me.username or '—'}, id={me.id})")
        print(f"   Сессия сохранена: {SESSION_PATH}.session")
        STATE_FILE.unlink(missing_ok=True)
    except Exception as e:
        print(f"❌ Пароль неверный: {e}")
    finally:
        await client.disconnect()


async def status():
    _check_creds()
    client = _make_client()
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"✅ Авторизован: {me.first_name} {me.last_name or ''}".strip())
        print(f"   Username: @{me.username or '—'}")
        print(f"   User ID: {me.id}")
        print(f"   Phone: {me.phone}")
    else:
        print("❌ Не авторизован. Запусти 'request <phone>'.")
    await client.disconnect()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "request":
        if len(sys.argv) < 3:
            print("Usage: telethon_auth.py request <phone>")
            return 1
        asyncio.run(request_code(sys.argv[2]))
    elif cmd == "verify":
        if len(sys.argv) < 3:
            print("Usage: telethon_auth.py verify <code>")
            return 1
        asyncio.run(verify_code(sys.argv[2]))
    elif cmd == "password":
        if len(sys.argv) < 3:
            print("Usage: telethon_auth.py password '<password>'")
            return 1
        asyncio.run(verify_password(sys.argv[2]))
    elif cmd == "status":
        asyncio.run(status())
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
