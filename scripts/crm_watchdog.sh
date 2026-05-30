#!/bin/bash
# Watchdog для crm-bot: детектит "немоту" aiogram polling.
# Сигнал — pending_update_count в getWebhookInfo (read-only Telegram API,
# НЕ конфликтует с polling бота). Если очередь необработанных сообщений
# непустая ДВА цикла подряд (~5 мин) — polling завис → рестарт + АЛЕРТ
# админу в Telegram.
set -uo pipefail
ENV=/opt/crm-bot/.env
TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
ADMIN=$(grep -E '^ADMIN_TELEGRAM_ID=' "$ENV" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
STATE=/tmp/crm_watchdog_pending.state
LOG=/var/log/crm_watchdog.log
[ -z "$TOKEN" ] && exit 0

# Отправка алерта админу. Молча игнорирует ошибки (сеть/прокси).
alert() {
    local msg="$1"
    [ -z "$ADMIN" ] && return 0
    curl -s --max-time 15 \
        "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d "chat_id=${ADMIN}" \
        --data-urlencode "text=${msg}" >/dev/null 2>&1 || true
}

PENDING=$(curl -s --max-time 20 "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" | grep -oE '"pending_update_count":[0-9]+' | grep -oE '[0-9]+' || echo "")
[ -z "$PENDING" ] && exit 0
PREV=$(cat "$STATE" 2>/dev/null || echo 0)
echo "$PENDING" > "$STATE"
TS=$(date '+%Y-%m-%d %H:%M:%S')
if [ "$PENDING" -gt 0 ] && [ "$PREV" -gt 0 ]; then
    echo "$TS RESTART pending=$PENDING prev=$PREV" >> "$LOG"
    /usr/bin/systemctl restart crm-bot
    echo "0" > "$STATE"
    # Дать боту подняться и проверить polling, чтобы алерт был информативным.
    sleep 12
    if journalctl -u crm-bot --since "20 sec ago" --no-pager 2>/dev/null | grep -q "Run polling for bot"; then
        alert "🔄 Бот «Гроу» завис (очередь ${PENDING} сообщений не разгребалась ~5 мин) — автоматически перезапущен, сейчас работает."
    else
        alert "⚠️ Бот «Гроу» завис и перезапущен watchdog'ом, но polling пока НЕ поднялся. Проверьте сервер."
    fi
else
    echo "$TS ok pending=$PENDING prev=$PREV" >> "$LOG"
fi
