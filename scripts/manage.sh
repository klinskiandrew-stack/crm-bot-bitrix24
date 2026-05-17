#!/bin/bash
# Скрипт для управления ботом на Timeweb сервере

set -e

BOT_DIR="/opt/crm-bot"
SERVICE_NAME="crm-bot"

case "$1" in
  start)
    echo "🚀 Запуск бота..."
    sudo systemctl start $SERVICE_NAME
    sleep 2
    sudo systemctl status $SERVICE_NAME
    ;;

  stop)
    echo "🛑 Остановка бота..."
    sudo systemctl stop $SERVICE_NAME
    sleep 2
    sudo systemctl status $SERVICE_NAME
    ;;

  restart)
    echo "🔄 Перезапуск бота..."
    sudo systemctl restart $SERVICE_NAME
    sleep 2
    sudo systemctl status $SERVICE_NAME
    ;;

  status)
    echo "📊 Статус бота:"
    sudo systemctl status $SERVICE_NAME
    ;;

  logs)
    echo "📋 Логи бота (ctrl+c для выхода):"
    sudo journalctl -u $SERVICE_NAME -f --lines=50
    ;;

  logs-errors)
    echo "❌ Последние ошибки:"
    sudo journalctl -u $SERVICE_NAME | grep -i "error\|exception\|traceback" | tail -20
    ;;

  stats)
    echo "📈 Статистика использования:"
    cd $BOT_DIR
    source venv/bin/activate
    python3 << 'EOF'
import asyncio
from db.connection import db
from db.repositories import audit, users

async def show_stats():
    await db.init()

    users_list = await users.list_users()
    print(f"\n👥 Пользователей: {len(users_list)}")
    for user in users_list:
        print(f"   - {user['display_name']} (role: {user['role']})")

    stats = await audit.get_stats(days=1)
    print(f"\n📊 За последние 24 часа:")
    print(f"   Запросов: {stats.get('total_requests', 0)}")
    print(f"   Input токенов: {stats.get('total_input_tokens', 0)}")
    print(f"   Output токенов: {stats.get('total_output_tokens', 0)}")
    print(f"   Credits потрачено: {stats.get('total_credits', 0):.2f}")
    print(f"   Ошибок: {stats.get('error_count', 0)}")

    # За неделю
    stats_week = await audit.get_stats(days=7)
    print(f"\n📊 За последние 7 дней:")
    print(f"   Запросов: {stats_week.get('total_requests', 0)}")
    print(f"   Credits потрачено: {stats_week.get('total_credits', 0):.2f}")

    await db.close()

asyncio.run(show_stats())
EOF
    ;;

  backup)
    echo "💾 Создание бэкапа БД..."
    cd $BOT_DIR
    BACKUP_FILE="backup_$(date +%Y%m%d_%H%M%S).db"
    cp data/bot.sqlite "$BACKUP_FILE"
    echo "✅ Бэкап создан: $BACKUP_FILE"
    ls -lh "$BACKUP_FILE"
    ;;

  db-check)
    echo "🔍 Проверка целостности БД..."
    cd $BOT_DIR
    sqlite3 data/bot.sqlite "PRAGMA integrity_check;"
    echo "✅ БД в порядке"
    ;;

  tail-logs)
    LINES=${2:-100}
    echo "📋 Последние $LINES строк логов:"
    sudo journalctl -u $SERVICE_NAME -n $LINES
    ;;

  *)
    echo "Использование: $0 {start|stop|restart|status|logs|logs-errors|stats|backup|db-check|tail-logs}"
    echo ""
    echo "Команды:"
    echo "  start          - Запустить бота"
    echo "  stop           - Остановить бота"
    echo "  restart        - Перезапустить бота"
    echo "  status         - Показать статус"
    echo "  logs           - Показать логи в реал-тайм"
    echo "  logs-errors    - Показать только ошибки"
    echo "  stats          - Показать статистику"
    echo "  backup         - Создать бэкап БД"
    echo "  db-check       - Проверить целостность БД"
    echo "  tail-logs N    - Показать последние N строк (по умолчанию 100)"
    exit 1
    ;;
esac
