#!/bin/bash
# Quick fix script for Timeweb server
# Run: bash fix_server.sh

set -e

echo "======================================================================"
echo "🔧 KIE.AI BASE URL FIX FOR TIMEWEB SERVER"
echo "======================================================================"
echo ""

# Check if running on server
if [ ! -d "/opt/crm-bot" ]; then
    echo "❌ ERROR: /opt/crm-bot directory not found!"
    echo "   This script must be run on the Timeweb server"
    echo "   SSH to: ssh root@31.130.135.86"
    exit 1
fi

echo "✓ Found /opt/crm-bot directory"
echo ""

# Step 1: Show current configuration
echo "📋 CURRENT CONFIGURATION:"
if grep -q "KIE_BASE_URL=https://api.kie.ai/claude/v1" /opt/crm-bot/.env; then
    echo "   ❌ KIE_BASE_URL has incorrect /v1 at the end"
    echo "   Current: $(grep KIE_BASE_URL /opt/crm-bot/.env)"
else
    echo "   ✓ KIE_BASE_URL looks OK"
    echo "   Current: $(grep KIE_BASE_URL /opt/crm-bot/.env)"
fi
echo ""

# Step 2: Create backup
echo "💾 CREATING BACKUP:"
BACKUP_FILE="/opt/crm-bot/.env.backup.$(date +%Y%m%d_%H%M%S)"
cp /opt/crm-bot/.env "$BACKUP_FILE"
echo "   ✓ Backup created at: $BACKUP_FILE"
echo ""

# Step 3: Fix the configuration
echo "🔧 APPLYING FIX:"
sed -i 's|KIE_BASE_URL=https://api.kie.ai/claude/v1|KIE_BASE_URL=https://api.kie.ai/claude|g' /opt/crm-bot/.env

# Verify the fix
if grep -q "KIE_BASE_URL=https://api.kie.ai/claude$" /opt/crm-bot/.env; then
    echo "   ✓ Configuration fixed successfully!"
    echo "   New: $(grep KIE_BASE_URL /opt/crm-bot/.env)"
else
    echo "   ❌ Fix may have failed!"
    echo "   Current: $(grep KIE_BASE_URL /opt/crm-bot/.env)"
fi
echo ""

# Step 4: Restart service
echo "🚀 RESTARTING BOT SERVICE:"
echo "   Stopping service..."
sudo systemctl stop crm-bot
sleep 2

echo "   Starting service..."
sudo systemctl start crm-bot
sleep 3

# Check service status
if sudo systemctl is-active --quiet crm-bot; then
    echo "   ✓ Service started successfully"
else
    echo "   ❌ Service failed to start!"
    echo "   Run: sudo journalctl -u crm-bot -n 50"
    exit 1
fi
echo ""

# Step 5: Show service status
echo "📊 SERVICE STATUS:"
sudo systemctl status crm-bot --no-pager | head -5
echo ""

# Step 6: Show latest logs
echo "📋 LATEST LOGS (last 20 lines):"
sudo journalctl -u crm-bot -n 20 --no-pager
echo ""

echo "======================================================================"
echo "✅ FIX COMPLETED!"
echo "======================================================================"
echo ""
echo "📝 NEXT STEPS:"
echo "   1. Test the bot in Telegram:"
echo "      - Add @grouasistant_bot to a test group"
echo "      - Send: @grouasistant_bot тест"
echo ""
echo "   2. Monitor logs in real-time:"
echo "      sudo journalctl -u crm-bot -f"
echo ""
echo "   3. If issues, view detailed logs:"
echo "      sudo journalctl -u crm-bot -n 100"
echo ""
echo "   4. If you need to revert, use backup:"
echo "      cp $BACKUP_FILE /opt/crm-bot/.env"
echo "      sudo systemctl restart crm-bot"
echo ""
