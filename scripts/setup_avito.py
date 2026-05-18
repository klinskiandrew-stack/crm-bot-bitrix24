#!/usr/bin/env python3
"""Complete Avito API setup: get refresh_token and deploy to server."""

import subprocess
import sys
import os
import json
import asyncio
import aiohttp
import webbrowser
from aiohttp import web
from urllib.parse import urlencode


class AvitoCLISetup:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = "https://api.avito.ru"
        self.refresh_token = None

    def get_auth_url(self):
        """Generate Avito authorization URL."""
        auth_params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": "http://localhost:8080/callback",
        }
        return f"{self.base_url}/oauth/authorize?{urlencode(auth_params)}"

    async def exchange_code_for_token(self, auth_code: str):
        """Exchange authorization code for refresh_token."""
        async with aiohttp.ClientSession() as session:
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": "http://localhost:8080/callback",
            }

            try:
                async with session.post(
                    f"{self.base_url}/oauth/token",
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    result = await resp.json()
                    if resp.status == 200:
                        self.refresh_token = result.get("refresh_token")
                        return True
                    else:
                        print(f"❌ Error: {result}")
                        return False
            except Exception as e:
                print(f"❌ Request failed: {e}")
                return False

    async def run_callback_server(self):
        """Run local callback server."""
        auth_code_received = asyncio.Event()
        auth_code_value = {"code": None}

        async def handle_callback(request):
            params = request.rel_url.query
            auth_code_value["code"] = params.get("code")
            error = params.get("error")

            if error:
                html = f"<h1>❌ Error</h1><p>{error}</p>"
            elif auth_code_value["code"]:
                html = "<h1>✅ Authorization received!</h1><p>You can close this window.</p>"
                auth_code_received.set()
            else:
                html = "<h1>❌ No authorization code</h1>"

            return web.Response(text=html, content_type="text/html")

        app = web.Application()
        app.router.add_get("/callback", handle_callback)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 8080)
        await site.start()

        print("\n" + "=" * 70)
        print("🚀 OAuth Callback Server started")
        print("=" * 70)
        print(f"\n📍 Opening browser for authorization...\n")

        auth_url = self.get_auth_url()
        webbrowser.open(auth_url)
        print(f"If browser doesn't open, visit:\n{auth_url}\n")
        print("=" * 70)

        # Wait for callback (with timeout)
        try:
            await asyncio.wait_for(auth_code_received.wait(), timeout=600)
            auth_code = auth_code_value["code"]
            await runner.cleanup()
            return auth_code
        except asyncio.TimeoutError:
            print("❌ Authorization timeout")
            await runner.cleanup()
            return None

    async def authorize(self):
        """Run full OAuth flow."""
        auth_code = await self.run_callback_server()
        if not auth_code:
            return False

        print(f"\n🔄 Exchanging code for refresh_token...")
        success = await self.exchange_code_for_token(auth_code)

        if success and self.refresh_token:
            print("\n" + "=" * 70)
            print("✅ SUCCESS!")
            print("=" * 70)
            print(f"\nRefresh Token:\n{self.refresh_token}\n")
            return True
        else:
            print("❌ Failed to get refresh_token")
            return False


def get_user_id():
    """Get Avito user ID from user."""
    print("\n" + "=" * 70)
    print("🔍 Finding your Avito Account ID")
    print("=" * 70)
    print("\nYour User ID should be in Avito cabinet or API response.")
    print("For now, you can set a placeholder and update later.")
    print("\nOr check https://developers.avito.ru/ → API docs")
    print("\n" + "=" * 70)
    user_id = input("\nEnter your Avito User ID (or press Enter for placeholder): ").strip()
    return user_id or "YOUR_AVITO_USER_ID"


def deploy_to_server(refresh_token: str, user_id: str, client_id: str, client_secret: str):
    """Deploy credentials to server via SSH."""
    print("\n" + "=" * 70)
    print("📤 Deploying to Timeweb server")
    print("=" * 70)

    env_content = f"""AVITO_CLIENT_ID={client_id}
AVITO_CLIENT_SECRET={client_secret}
AVITO_REFRESH_TOKEN={refresh_token}
AVITO_USER_ID={user_id}
"""

    print(f"\nDeploying to /opt/crm-bot/.env ...\n")

    # SSH to server and append to .env
    cmd = f"""ssh timeweb-crm 'cat >> /opt/crm-bot/.env << 'ENVEOF'
{env_content}ENVEOF
' """

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print("✅ Credentials saved to server")
            return True
        else:
            print(f"❌ SSH error: {result.stderr}")
            print("\nManual setup required:")
            print(f"ssh timeweb-crm")
            print(f"nano /opt/crm-bot/.env")
            print(f"# Add these lines:")
            print(env_content)
            return False
    except Exception as e:
        print(f"❌ Error: {e}")
        print("\nManual setup required:")
        print(f"ssh timeweb-crm")
        print(f"nano /opt/crm-bot/.env")
        print(f"# Add these lines:")
        print(env_content)
        return False


def restart_bot():
    """Restart the bot on server."""
    print("\n" + "=" * 70)
    print("🔄 Restarting bot")
    print("=" * 70 + "\n")

    cmd = "ssh timeweb-crm 'sudo systemctl restart crm-bot && sleep 2 && sudo journalctl -u crm-bot -n 20'"

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        print(result.stdout)
        if result.returncode == 0:
            print("\n✅ Bot restarted successfully")
            return True
        else:
            print(f"⚠️  Warning: {result.stderr}")
            return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


async def main():
    if len(sys.argv) < 3:
        print("Usage: python setup_avito.py <client_id> <client_secret>")
        sys.exit(1)

    client_id = sys.argv[1]
    client_secret = sys.argv[2]

    print("\n" + "=" * 70)
    print("🚀 Avito API Complete Setup")
    print("=" * 70)

    # Step 1: Authorization
    setup = AvitoCLISetup(client_id, client_secret)
    if not await setup.authorize():
        print("\n❌ Authorization failed")
        sys.exit(1)

    # Step 2: Get User ID
    user_id = get_user_id()

    # Step 3: Deploy to server
    if not deploy_to_server(setup.refresh_token, user_id, client_id, client_secret):
        print("\n⚠️  Manual deployment required")
        sys.exit(1)

    # Step 4: Restart bot
    if not restart_bot():
        print("\n⚠️  Please restart the bot manually:")
        print("ssh timeweb-crm")
        print("sudo systemctl restart crm-bot")
        sys.exit(1)

    # Success
    print("\n" + "=" * 70)
    print("✅ SETUP COMPLETE!")
    print("=" * 70)
    print("\n🎉 Avito API is now integrated!")
    print("\nYou can test it in Telegram:")
    print('  "Какова статистика на Avito за последние 7 дней?"')
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
