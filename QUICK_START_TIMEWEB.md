# ⚡ Быстрый старт на Timeweb (5 минут)

## 🎯 Перед началом подготовьте:

1. **Telegram Bot Token** - от BotFather (@BotFather в Telegram)
2. **Ваш Telegram ID** - отправьте сообщение @userinfobot
3. **Bitrix24 Webhook URL** - из админ-панели вашего портала
4. **Kie.ai API Key** - из личного кабинета Kie.ai

---

## 1️⃣ Подключиться к серверу

```bash
ssh root@YOUR_SERVER_IP
```

## 2️⃣ Установить базовое ПО

```bash
apt update && apt install -y python3.11 python3.11-venv git sqlite3
useradd -m -s /bin/bash crmbot
su - crmbot
```

## 3️⃣ Клонировать и настроить

```bash
cd /opt
git clone https://github.com/your-repo/crm-bot.git
cd crm-bot

# Создать окружение
python3.11 -m venv venv
source venv/bin/activate

# Установить зависимости
pip install -r requirements.txt

# Создать директории
mkdir -p data logs
```

## 4️⃣ Настроить .env

```bash
nano .env
```

**Вставить и заполнить:**
```
TELEGRAM_BOT_TOKEN=ВАШ_ТОКЕН_ОТ_BOTFATHER
ADMIN_TELEGRAM_ID=ВАШ_TELEGRAM_ID

B24_WEBHOOK_URL=https://ВАШ-ПОРТАЛ.bitrix24.ru/rest/1/YOUR_TOKEN/

KIE_API_KEY=ВАШ_KIE_API_KEY
KIE_BASE_URL=https://api.kie.ai/claude/v1

DATABASE_PATH=./data/bot.sqlite
LOG_LEVEL=INFO
TELEGRAM_PROXY_URL=
```

Сохранить: `Ctrl+O`, `Enter`, `Ctrl+X`

## 5️⃣ Инициализировать БД

```bash
python3 scripts/init_db.py
```

Должны увидеть:
```
✓ Database schema created
✓ Default settings initialized
✓ Admin user created
✅ Database initialization complete!
```

## 6️⃣ Добавить партнеров (опционально)

```bash
python3 << 'EOF'
import asyncio
from db.connection import db
from db.repositories import users

async def add():
    await db.init()
    await users.create_user(
        telegram_id=111111111,  # Telegram ID партнера
        role="partner",
        b24_user_ids=[1, 2, 3],  # ID ответственных в B24
        display_name="Иван Петров"
    )
    await db.close()
    print("✅ Партнер добавлен")

asyncio.run(add())
EOF
```

## 7️⃣ Запустить бота

```bash
# Вариант 1: Прямой запуск (для теста)
python3 main.py

# Если всё OK, нажать Ctrl+C для остановки
```

## 8️⃣ Настроить автозапуск (systemd)

```bash
# Выйти от пользователя crmbot
exit

# Создать systemd сервис
sudo bash -c 'cat > /etc/systemd/system/crm-bot.service << EOF
[Unit]
Description=CRM Bot
After=network.target

[Service]
Type=simple
User=crmbot
WorkingDirectory=/opt/crm-bot
Environment="PATH=/opt/crm-bot/venv/bin"
ExecStart=/opt/crm-bot/venv/bin/python3 /opt/crm-bot/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=crm-bot

[Install]
WantedBy=multi-user.target
EOF'

# Включить и запустить
sudo systemctl daemon-reload
sudo systemctl enable crm-bot
sudo systemctl start crm-bot

# Проверить статус
sudo systemctl status crm-bot
```

## 📋 Полезные команды

```bash
# Просмотр логов (реал-тайм)
sudo journalctl -u crm-bot -f

# Только ошибки
sudo journalctl -u crm-bot | grep -i error

# Статистика
cd /opt/crm-bot && source venv/bin/activate && python3 scripts/healthcheck.py

# Перезагрузка
sudo systemctl restart crm-bot

# Остановка
sudo systemctl stop crm-bot

# Очистка старых логов (> 90 дней)
cd /opt/crm-bot && source venv/bin/activate && python3 << 'EOF'
import asyncio
from db.connection import db
from db.repositories import audit
async def cleanup():
    await db.init()
    await audit.cleanup_old_logs(days=90)
    await db.close()
asyncio.run(cleanup())
EOF
```

## 🧪 Проверка работоспособности

1. **Напишите боту в Telegram:**
   ```
   @bot_username сколько сделок в работе?
   ```

2. **Проверьте логи:**
   ```bash
   sudo journalctl -u crm-bot -n 50
   ```

3. **Проверьте БД:**
   ```bash
   cd /opt/crm-bot
   source venv/bin/activate
   sqlite3 data/bot.sqlite "SELECT COUNT(*) FROM audit_log;"
   ```

## ⚠️ Если что-то не работает

### Бот не запускается
```bash
# Проверить синтаксис
cd /opt/crm-bot && source venv/bin/activate
python3 -m py_compile main.py config.py

# Проверить логи
sudo journalctl -u crm-bot -n 100
```

### Ошибка подключения к Telegram
```bash
# Проверить доступ
curl -I https://api.telegram.org

# Если заблокирована, добавить прокси в .env:
TELEGRAM_PROXY_URL=socks5h://login:password@ip:port
```

### Проблемы с Bitrix24
```bash
# Проверить webhook
curl -X POST https://YOUR-PORTAL.bitrix24.ru/rest/1/YOUR_TOKEN/crm.deal.list.json -d ""
```

---

## 📞 Техническая поддержка

Если нужна помощь, проверьте:
1. Логи: `sudo journalctl -u crm-bot -f`
2. .env: `cat /opt/crm-bot/.env`
3. БД: `sqlite3 /opt/crm-bot/data/bot.sqlite ".tables"`
4. Сеть: `curl -I https://api.telegram.org`

**Готово! 🚀**
