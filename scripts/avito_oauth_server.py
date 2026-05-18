#!/usr/bin/env python3
"""Local OAuth callback server for Avito authorization."""

import asyncio
import aiohttp
from aiohttp import web
from urllib.parse import urlencode, parse_qs
import sys
import os


class AvitoOAuthServer:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = "https://api.avito.ru"
        self.refresh_token = None
        self.auth_code = None

    async def handle_callback(self, request: web.Request):
        """Handle OAuth callback from Avito."""
        params = request.rel_url.query
        self.auth_code = params.get("code")
        error = params.get("error")

        if error:
            html = f"""
            <html>
            <head><title>Error</title></head>
            <body>
                <h1>❌ Authorization Failed</h1>
                <p>Error: {error}</p>
                <p>You can close this window.</p>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html")

        if not self.auth_code:
            html = """
            <html>
            <head><title>Error</title></head>
            <body>
                <h1>❌ No Authorization Code</h1>
                <p>Something went wrong. Please try again.</p>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html")

        # Exchange code for tokens
        try:
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
                        html = f"""
                        <html>
                        <head><title>Error</title></head>
                        <body>
                            <h1>❌ Token Exchange Failed</h1>
                            <p>Status: {resp.status}</p>
                            <p>{result}</p>
                        </body>
                        </html>
                        """
                        return web.Response(text=html, content_type="text/html")

                    self.refresh_token = result.get("refresh_token")
                    access_token = result.get("access_token")
                    expires_in = result.get("expires_in")

                    html = f"""
                    <html>
                    <head><title>Success</title></head>
                    <body style="font-family: monospace; padding: 20px;">
                        <h1>✅ Authorization Successful!</h1>
                        <p><strong>Refresh Token (save this to .env):</strong></p>
                        <pre style="background: #f0f0f0; padding: 10px; word-wrap: break-word;">{self.refresh_token}</pre>
                        <p><strong>Access Token:</strong></p>
                        <pre style="background: #f0f0f0; padding: 10px; word-wrap: break-word; max-width: 500px;">{access_token[:100]}...</pre>
                        <p><strong>Expires in:</strong> {expires_in} seconds</p>
                        <p><br/><strong>ℹ️ The CLI tool will also print these values. You can close this window.</strong></p>
                    </body>
                    </html>
                    """
                    return web.Response(text=html, content_type="text/html")

        except Exception as e:
            html = f"""
            <html>
            <head><title>Error</title></head>
            <body>
                <h1>❌ Request Failed</h1>
                <p>{str(e)}</p>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html")

    async def start(self):
        """Start the OAuth callback server."""
        app = web.Application()
        app.router.add_get("/callback", self.handle_callback)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 8080)
        await site.start()

        print("\n" + "=" * 70)
        print("🚀 OAuth Callback Server started on http://localhost:8080")
        print("=" * 70)

        # Generate auth URL
        auth_params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": "http://localhost:8080/callback",
        }
        auth_url = f"{self.base_url}/oauth/authorize?{urlencode(auth_params)}"

        print("\n📍 Visit this URL to authorize:")
        print(f"\n{auth_url}\n")
        print("=" * 70)
        print("ℹ️  You will be redirected to http://localhost:8080/callback")
        print("    The page will display your refresh_token.")
        print("=" * 70 + "\n")

        # Wait for callback (with timeout)
        start_time = asyncio.get_event_loop().time()
        timeout = 600  # 10 minutes

        while not self.refresh_token:
            if asyncio.get_event_loop().time() - start_time > timeout:
                print("❌ Authorization timeout (10 minutes)")
                await runner.cleanup()
                return False

            await asyncio.sleep(1)

        # Print result
        print("\n" + "=" * 70)
        print("✅ SUCCESS! Got refresh_token:")
        print("=" * 70)
        print(f"\nAVITO_REFRESH_TOKEN={self.refresh_token}\n")
        print("=" * 70)
        print("Add this to .env on the server:")
        print("=" * 70 + "\n")

        await runner.cleanup()
        return True


async def main():
    if len(sys.argv) < 3:
        print("Usage: python avito_oauth_server.py <client_id> <client_secret>")
        sys.exit(1)

    client_id = sys.argv[1]
    client_secret = sys.argv[2]

    server = AvitoOAuthServer(client_id, client_secret)
    success = await server.start()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
