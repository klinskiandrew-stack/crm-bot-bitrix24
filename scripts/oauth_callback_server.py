#!/usr/bin/env python3
"""Standalone OAuth callback server - waits for auth code and exchanges for refresh_token."""

import asyncio
import aiohttp
import sys
import json
from aiohttp import web
from urllib.parse import urlencode
import signal


class OAuthServer:
    def __init__(self, client_id: str, client_secret: str, output_file: str = "/tmp/avito_refresh_token.json"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.output_file = output_file
        self.base_url = "https://api.avito.ru"
        self.auth_code = None
        self.refresh_token = None
        self.should_exit = False

    def get_auth_url(self):
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": "http://localhost:8080/callback",
        }
        return f"{self.base_url}/oauth/authorize?{urlencode(params)}"

    async def handle_callback(self, request):
        """Handle OAuth callback."""
        try:
            params = request.rel_url.query
            self.auth_code = params.get("code")
            error = params.get("error")

            if error:
                html = f"<h1>❌ Error: {error}</h1>"
                self.should_exit = True
                return web.Response(text=html, content_type="text/html", status=400)

            if not self.auth_code:
                html = "<h1>❌ No authorization code received</h1>"
                self.should_exit = True
                return web.Response(text=html, content_type="text/html", status=400)

            # Exchange code for tokens
            async with aiohttp.ClientSession() as session:
                data = {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "authorization_code",
                    "code": self.auth_code,
                    "redirect_uri": "http://localhost:8080/callback",
                }

                async with session.post(
                    f"{self.base_url}/oauth/token",
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    result = await resp.json()

                    if resp.status != 200:
                        html = f"<h1>❌ Token exchange failed</h1><p>{result}</p>"
                        self.should_exit = True
                        return web.Response(text=html, content_type="text/html", status=400)

                    self.refresh_token = result.get("refresh_token")

                    # Save to file
                    with open(self.output_file, 'w') as f:
                        json.dump({
                            "refresh_token": self.refresh_token,
                            "access_token": result.get("access_token"),
                            "expires_in": result.get("expires_in"),
                        }, f, indent=2)

                    html = f"""
                    <html>
                    <head><title>✅ Success</title></head>
                    <body style="font-family: Arial; padding: 40px; text-align: center;">
                        <h1>✅ Authorization Successful!</h1>
                        <p>Refresh token saved.</p>
                        <p>You can close this window and return to the terminal.</p>
                    </body>
                    </html>
                    """
                    self.should_exit = True
                    return web.Response(text=html, content_type="text/html")

        except Exception as e:
            html = f"<h1>❌ Error</h1><p>{str(e)}</p>"
            self.should_exit = True
            return web.Response(text=html, content_type="text/html", status=500)

    async def run(self):
        """Run the server."""
        app = web.Application()
        app.router.add_get("/callback", self.handle_callback)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 8080)
        await site.start()

        print("\n" + "=" * 70)
        print("🚀 OAuth Callback Server started on http://localhost:8080")
        print("=" * 70)
        print(f"\n📍 Visit this URL to authorize:\n")
        print(self.get_auth_url())
        print("\n" + "=" * 70)
        print("Waiting for authorization...")
        print("=" * 70 + "\n")

        # Wait for authorization
        start_time = asyncio.get_event_loop().time()
        timeout = 600  # 10 minutes

        while not self.should_exit:
            if asyncio.get_event_loop().time() - start_time > timeout:
                print("\n❌ Timeout - authorization took too long")
                await runner.cleanup()
                return False

            await asyncio.sleep(1)

        await runner.cleanup()

        if self.refresh_token:
            print("\n" + "=" * 70)
            print("✅ SUCCESS!")
            print("=" * 70)
            print(f"\nRefresh token saved to: {self.output_file}")
            print("\nYou can now run:")
            print(f"  python3 scripts/deploy_avito.py {self.output_file}")
            print("\n" + "=" * 70 + "\n")
            return True
        else:
            print("\n❌ Failed to get refresh token")
            return False


async def main():
    if len(sys.argv) < 3:
        print("Usage: python oauth_callback_server.py <client_id> <client_secret>")
        sys.exit(1)

    client_id = sys.argv[1]
    client_secret = sys.argv[2]

    server = OAuthServer(client_id, client_secret)
    success = await server.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
