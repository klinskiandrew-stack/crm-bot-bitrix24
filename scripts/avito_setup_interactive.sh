#!/bin/bash

# Avito API Setup for GrowZone CRM Bot

set -e

CLIENT_ID="PyGrYjlzuN_sqxAqA9h7"
CLIENT_SECRET="Db8_EDDhCv6KR85KT4YpZlCacvxeY6BwIg_wOUrE"
REDIRECT_URI="http://localhost:8080/callback"
BASE_URL="https://api.avito.ru"

echo ""
echo "========================================================================"
echo "🚀 Avito API Setup - GrowZone CRM Bot"
echo "========================================================================"
echo ""

# Step 1: Show authorization URL
echo "📍 STEP 1: Authorization"
echo "========================================================================"
echo ""
echo "You need to authorize the application. Visit this URL in your browser:"
echo ""

AUTH_URL="${BASE_URL}/oauth/authorize?client_id=${CLIENT_ID}&response_type=code&redirect_uri=${REDIRECT_URI}"
echo "$AUTH_URL"
echo ""

# Step 2: Get auth code
echo "========================================================================"
echo ""
echo "After clicking 'Allow', you'll be redirected to:"
echo "  http://localhost:8080/callback?code=..."
echo ""
echo "Copy the code from the URL (the value after 'code=')"
echo ""
read -p "Paste the authorization code here: " AUTH_CODE

if [ -z "$AUTH_CODE" ]; then
    echo "❌ No authorization code provided"
    exit 1
fi

echo ""
echo "🔄 Exchanging code for tokens..."
echo ""

# Step 3: Exchange code for tokens
RESPONSE=$(curl -s -X POST "${BASE_URL}/oauth/token" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "grant_type=authorization_code" \
    -d "code=${AUTH_CODE}" \
    -d "redirect_uri=${REDIRECT_URI}")

echo "$RESPONSE" | grep -q "refresh_token" || {
    echo "❌ Failed to get tokens:"
    echo "$RESPONSE"
    exit 1
}

REFRESH_TOKEN=$(echo "$RESPONSE" | grep -o '"refresh_token":"[^"]*"' | cut -d'"' -f4)
ACCESS_TOKEN=$(echo "$RESPONSE" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
EXPIRES_IN=$(echo "$RESPONSE" | grep -o '"expires_in":[0-9]*' | cut -d':' -f2)

if [ -z "$REFRESH_TOKEN" ]; then
    echo "❌ Could not extract refresh_token from response"
    echo "$RESPONSE"
    exit 1
fi

echo "✅ Got tokens!"
echo ""
echo "========================================================================"
echo "📋 YOUR CREDENTIALS:"
echo "========================================================================"
echo ""
echo "AVITO_CLIENT_ID=$CLIENT_ID"
echo "AVITO_CLIENT_SECRET=$CLIENT_SECRET"
echo "AVITO_REFRESH_TOKEN=$REFRESH_TOKEN"
echo ""

# Step 4: Get User ID
echo "========================================================================"
echo "🔍 STEP 2: Your Avito Account ID"
echo "========================================================================"
echo ""
echo "To find your User ID, check your Avito cabinet or developers.avito.ru"
echo "For now, you can use a placeholder like 'YOUR_AVITO_USER_ID' and update later"
echo ""
read -p "Enter your Avito User ID (or press Enter for placeholder): " USER_ID
USER_ID=${USER_ID:-"YOUR_AVITO_USER_ID"}

echo ""
echo "========================================================================"
echo "📤 STEP 3: Deploying to server..."
echo "========================================================================"
echo ""

# Deploy to server
SSH_HOST="timeweb-crm"

echo "Connecting to $SSH_HOST..."

ssh "$SSH_HOST" bash <<EOFREMOTE
set -e

ENV_FILE="/opt/crm-bot/.env"

echo "✓ Appending Avito credentials to \$ENV_FILE..."

cat >> "\$ENV_FILE" <<EOFENV

# Avito Ads API
AVITO_CLIENT_ID=$CLIENT_ID
AVITO_CLIENT_SECRET=$CLIENT_SECRET
AVITO_REFRESH_TOKEN=$REFRESH_TOKEN
AVITO_USER_ID=$USER_ID
EOFENV

echo "✓ Credentials saved"

echo ""
echo "🔄 Restarting bot..."
sudo systemctl restart crm-bot
sleep 2

echo ""
echo "📋 Recent logs:"
sudo journalctl -u crm-bot -n 30

EOFREMOTE

echo ""
echo "========================================================================"
echo "✅ SETUP COMPLETE!"
echo "========================================================================"
echo ""
echo "🎉 Avito API is now integrated!"
echo ""
echo "Test it in Telegram:"
echo '  "Какова статистика на Avito за последние 7 дней?"'
echo ""
echo "========================================================================"
echo ""
