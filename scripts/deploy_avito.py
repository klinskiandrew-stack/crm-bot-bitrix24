#!/usr/bin/env python3
"""Deploy Avito credentials to server from saved refresh_token."""

import json
import sys
import subprocess
import os
from datetime import datetime


def load_tokens(token_file: str):
    """Load refresh token from file."""
    if not os.path.exists(token_file):
        print(f"❌ Token file not found: {token_file}")
        sys.exit(1)

    with open(token_file, 'r') as f:
        data = json.load(f)

    return data


def prompt_user_id():
    """Prompt for Avito User ID."""
    print("\n" + "=" * 70)
    print("👤 Your Avito Account ID")
    print("=" * 70)
    print("\nYou can find this in:")
    print("  1. Avito cabinet → Settings")
    print("  2. API response from Avito")
    print("  3. Use a placeholder for now and update later")
    print("")

    user_id = input("Enter your Avito User ID (or press Enter for placeholder): ").strip()
    return user_id or "YOUR_AVITO_USER_ID_HERE"


def deploy_to_server(client_id: str, client_secret: str, refresh_token: str, user_id: str):
    """Deploy to Timeweb server via SSH."""
    print("\n" + "=" * 70)
    print("📤 Deploying to server")
    print("=" * 70 + "\n")

    # Build env content
    env_lines = [
        f"\n# Avito Ads API - Deployed at {datetime.now().isoformat()}",
        f"AVITO_CLIENT_ID={client_id}",
        f"AVITO_CLIENT_SECRET={client_secret}",
        f"AVITO_REFRESH_TOKEN={refresh_token}",
        f"AVITO_USER_ID={user_id}",
    ]
    env_content = "\n".join(env_lines)

    # SSH command to append to .env
    ssh_cmd = f"""
ssh timeweb-crm bash << 'SSHEOF'
set -e

ENV_FILE="/opt/crm-bot/.env"
BACKUP="${{ENV_FILE}}.backup.$(date +%s)"

echo "📋 Backing up .env to $BACKUP..."
cp "$ENV_FILE" "$BACKUP"

echo "✓ Adding Avito credentials to $ENV_FILE..."
cat >> "$ENV_FILE" << 'ENVEOF'
{env_content}
ENVEOF

echo "✓ Credentials saved"

echo ""
echo "🔄 Restarting crm-bot service..."
sudo systemctl restart crm-bot
sleep 3

echo ""
echo "📋 Bot status:"
sudo systemctl is-active crm-bot && echo "✅ Bot is running" || echo "❌ Bot failed to start"

echo ""
echo "📊 Recent logs:"
sudo journalctl -u crm-bot -n 20 --no-pager

SSHEOF
"""

    try:
        result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=120)

        if result.returncode == 0:
            print(result.stdout)
            print("\n" + "=" * 70)
            print("✅ DEPLOYMENT SUCCESSFUL!")
            print("=" * 70)
            print("\n🎉 Avito API is now live!")
            print("\nTest in Telegram:")
            print('  "Какова статистика на Avito за последние 7 дней?"')
            print("\n" + "=" * 70 + "\n")
            return True
        else:
            print(result.stdout)
            if result.stderr:
                print(f"Errors:\n{result.stderr}")
            return False

    except Exception as e:
        print(f"❌ Deployment failed: {e}")
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python deploy_avito.py <token_file>")
        print("       (where token_file was created by oauth_callback_server.py)")
        sys.exit(1)

    token_file = sys.argv[1]

    print("\n" + "=" * 70)
    print("📤 Avito API Deployment")
    print("=" * 70)

    # Load tokens
    tokens = load_tokens(token_file)
    refresh_token = tokens.get("refresh_token")
    client_id = "PyGrYjlzuN_sqxAqA9h7"
    client_secret = "Db8_EDDhCv6KR85KT4YpZlCacvxeY6BwIg_wOUrE"

    if not refresh_token:
        print("❌ No refresh_token in file")
        sys.exit(1)

    print(f"✅ Loaded refresh_token")
    print(f"   Expires in: {tokens.get('expires_in')} seconds")

    # Get user ID
    user_id = prompt_user_id()

    # Deploy
    success = deploy_to_server(client_id, client_secret, refresh_token, user_id)

    if not success:
        print("\n⚠️  Deployment had issues. Check logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
