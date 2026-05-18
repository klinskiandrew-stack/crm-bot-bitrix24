#!/usr/bin/env python3
"""Initialize Avito OAuth: get refresh_token for first time.

Usage:
    python scripts/avito_oauth_init.py --client-id=YOUR_ID --client-secret=YOUR_SECRET

Will prompt you to:
1. Visit Avito authorization URL
2. Authorize the app
3. Paste the auth code back

Then saves refresh_token to stdout (copy to .env as AVITO_REFRESH_TOKEN).
"""

import asyncio
import aiohttp
import argparse
import sys
from urllib.parse import urlencode


async def get_refresh_token(client_id: str, client_secret: str):
    """OAuth 2.0 authorization code flow."""
    base_url = "https://api.avito.ru"

    # Step 1: Direct user to authorization URL
    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": "http://localhost:8080/callback",  # Can be any registered URI
    }
    auth_url = f"{base_url}/oauth/authorize?{urlencode(auth_params)}"

    print("\n" + "=" * 70)
    print("STEP 1: Authorize the application")
    print("=" * 70)
    print(f"\nVisit this URL to authorize:\n{auth_url}\n")
    print("You will be redirected to a URL with a 'code' parameter.")
    print("=" * 70 + "\n")

    auth_code = input("Paste the authorization code here: ").strip()
    if not auth_code:
        print("Error: Authorization code required", file=sys.stderr)
        return None

    # Step 2: Exchange code for tokens
    print("\nExchanging authorization code for tokens...\n")

    async with aiohttp.ClientSession() as session:
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": "http://localhost:8080/callback",
        }

        try:
            async with session.post(
                f"{base_url}/oauth/token",
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                result = await resp.json()

                if resp.status != 200:
                    print(f"Error: {resp.status}", file=sys.stderr)
                    print(result, file=sys.stderr)
                    return None

                access_token = result.get("access_token")
                refresh_token = result.get("refresh_token")
                expires_in = result.get("expires_in")

                print("=" * 70)
                print("SUCCESS! Got tokens:")
                print("=" * 70)
                print(f"\nAccess token (valid for {expires_in}s):")
                print(f"  {access_token[:50]}...{access_token[-20:]}\n")
                print(f"Refresh token (save this to .env):")
                print(f"  {refresh_token}\n")
                print("=" * 70)
                print("\nAdd to .env:")
                print(f"AVITO_REFRESH_TOKEN={refresh_token}")
                print("=" * 70 + "\n")

                return refresh_token

        except Exception as e:
            print(f"Request failed: {e}", file=sys.stderr)
            return None


def main():
    parser = argparse.ArgumentParser(description="Get Avito refresh_token via OAuth")
    parser.add_argument("--client-id", required=True, help="Avito client ID")
    parser.add_argument("--client-secret", required=True, help="Avito client secret")
    args = parser.parse_args()

    token = asyncio.run(get_refresh_token(args.client_id, args.client_secret))
    sys.exit(0 if token else 1)


if __name__ == "__main__":
    main()
