# Развертывание на сервере Timeweb

## 1. Подготовка сервера (Ubuntu 22.04 LTS)

### 1.1 SSH доступ и обновление системы
```bash
ssh root@your-server-ip

# Обновить систему
apt update && apt upgrade -y

# Установить необходимые пакеты
apt install -y python3.11 python3.11-venv python3.11-dev sqlite3 git curl wget supervisor
```

### 1.2 Создать пользователя для бота
```bash
# Создать пользователя без прав sudo
useradd -m -s /bin/bash crmbot

# Переключиться на пользователя
su - crmbot
```

## 2. Клонирование и подготовка проекта

```bash
# Как пользователь crmbot
cd /opt && git clone <repo_url> crm-bot
cd crm-bot

# Создать виртуальное окружение
python3.11 -m venv venv
source venv/bin/activate

# Установить зависимости
pip install -r requirements.txt

# Создать директорию для данных и логов
mkdir -p data logs
chmod 700 data
```

## 3. Конфигурация (.env)

```bash
# Создать .env файл
nano .env
```

**Содержимое .env (обновить реальными значениями):**
```
TELEGRAM_BOT_TOKEN=ваш_токен_от_botfather
ADMIN_TELEGRAM_ID=ваш_telegram_id

# Прокси для Telegram (если нужна для РФ)
TELEGRAM_PROXY_URL=

B24_WEBHOOK_URL=https://ваш-портал.bitrix24.ru/rest/1/your_token/

KIE_API_KEY=ваш_kie_api_key
KIE_BASE_URL=https://api.kie.ai/claude/v1

DATABASE_PATH=./data/bot.sqlite
LOG_LEVEL=INFO
SESSION_TTL_MINUTES=30
MAX_SESSION_MESSAGES=20
```

```bash
# Ограничить права доступа к .env
chmod 600 .env
```

## 4. Инициализация БД

```bash
source venv/bin/activate
python3 scripts/init_db.py

# Добавить реальных пользователей (партнеров)
python3 << 'EOF'
import asyncio
from db.connection import db
from db.repositories import users

async def add_partner():
    await db.init()
    
    # Добавить партнера
    await users.create_user(
        telegram_id=123456789,  # ID партнера в Telegram
        role="partner",
        b24_user_ids=[1, 2, 3],  # ID ответственных в Bitrix24
        display_name="Имя Партнера"
    )
    
    await db.close()
    print("✅ Партнер добавлен")

asyncio.run(add_partner())
EOF
```

## 5. Systemd Service (автозапуск при перезагрузке)

```bash
# Создать unit файл (как root)
sudo nano /etc/systemd/system/crm-bot.service
```

**Содержимое /etc/systemd/system/crm-bot.service:**
```ini
[Unit]
Description=CRM Bot for Bitrix24
After=network.target

[Service]
Type=simple
User=crmbot
WorkingDirectory=/opt/crm-bot
Environment="PATH=/opt/crm-bot/venv/bin"
ExecStart=/opt/crm-bot/venv/bin/python3 /opt/crm-bot/main.py

# Автоматический перезапуск при падении
Restart=always
RestartSec=10

# Логирование
StandardOutput=journal
StandardError=journal
SyslogIdentifier=crm-bot

[Install]
WantedBy=multi-user.target
```

```bash
# Включить и запустить сервис
sudo systemctl daemon-reload
sudo systemctl enable crm-bot
sudo systemctl start crm-bot

# Проверить статус
sudo systemctl status crm-bot

# Просмотр логов
sudo journalctl -u crm-bot -f  # live logs
sudo journalctl -u crm-bot --since "1 hour ago"  # за последний час
```

## 6. Проверка доступности

### 6.1 Telegram API
```bash
# Проверить прямой доступ
curl -I --max-time 10 https://api.telegram.org

# Если fail, проверить с прокси (если настроена)
curl -I --max-time 15 -x "http://login:password@ip:port" https://api.telegram.org
```

### 6.2 Bitrix24
```bash
curl -I https://your-portal.bitrix24.ru
```

### 6.3 Kie.ai
```bash
curl -I https://api.kie.ai
```

## 7. Мониторинг и логирование

### 7.1 Просмотр логов
```bash
# Реал-тайм логи
sudo journalctl -u crm-bot -f

# Поиск ошибок
sudo journalctl -u crm-bot | grep -i error

# За конкретный период
sudo journalctl -u crm-bot --since "2026-05-17 10:00:00" --until "2026-05-17 11:00:00"
```

### 7.2 Проверка использования ресурсов
```bash
# CPU и Memory
ps aux | grep crm-bot

# Размер БД
du -h /opt/crm-bot/data/bot.sqlite

# Лог размер
du -h /opt/crm-bot/logs/
```

## 8. Бэкап БД

### 8.1 Ручной бэкап
```bash
cd /opt/crm-bot
sqlite3 data/bot.sqlite ".backup backup_$(date +%Y%m%d_%H%M%S).db"
```

### 8.2 Автоматический бэкап (cron)
```bash
# Редактировать crontab
crontab -e

# Добавить строку (бэкап каждый день в 3 AM)
0 3 * * * cd /opt/crm-bot && /opt/crm-bot/venv/bin/python3 -c "import sqlite3; sqlite3.connect('data/bot.sqlite').execute('VACUUM'); import shutil; shutil.copy('data/bot.sqlite', f'backup_$(date +\%Y\%m\%d).db')"
```

## 9. Обновление проекта

```bash
cd /opt/crm-bot
source venv/bin/activate

# Получить последние изменения
git pull origin main

# Обновить зависимости (если нужно)
pip install -r requirements.txt

# Перезагрузить бота
sudo systemctl restart crm-bot
```

## 10. Проверка работоспособности

```bash
# Проверить, что сервис запущен
sudo systemctl is-active crm-bot  # должен вернуть "active"

# Проверить статистику из БД
cd /opt/crm-bot
source venv/bin/activate
python3 << 'EOF'
import asyncio
from db.connection import db
from db.repositories import audit, users

async def check():
    await db.init()
    
    users_list = await users.list_users()
    print(f"👥 Пользователей в системе: {len(users_list)}")
    
    stats = await audit.get_stats(days=1)
    print(f"📊 Запросов сегодня: {stats.get('total_requests', 0)}")
    print(f"💰 Потрачено credits: {stats.get('total_credits', 0):.2f}")
    
    errors = await audit.get_recent_errors(limit=3)
    if errors:
        print(f"❌ Последние ошибки ({len(errors)}):")
        for err in errors:
            print(f"   - {err['error'][:80]}")
    else:
        print("✅ Ошибок не найдено")
    
    await db.close()

asyncio.run(check())
EOF
```

## 11. Решение проблем

### Бот не запускается
```bash
# Проверить логи
sudo journalctl -u crm-bot -n 50

# Проверить синтаксис Python
python3 -m py_compile main.py config.py

# Проверить .env
cat .env | grep -v "^#"
```

### Ошибка при подключении к Telegram
```bash
# Проверить доступность API
curl -v https://api.telegram.org/bot{YOUR_TOKEN}/getMe

# Если блокирована в РФ, настроить TELEGRAM_PROXY_URL
```

### Ошибка при подключении к Bitrix24
```bash
# Проверить webhook URL
curl -X POST -d "" {B24_WEBHOOK_URL}crm.deal.list.json

# Проверить права вебхука в Bitrix24
```

## 12. Безопасность

```bash
# Ограничить права на файлы
chmod 600 .env
chmod 700 data/
chmod 700 logs/

# Проверить права
ls -la .env data/ logs/

# Установить firewall правила (если нужно)
sudo ufw allow 22/tcp  # SSH
sudo ufw allow 443/tcp # HTTPS для исходящего трафика
```

---

**Успешного развертывания! 🚀**

Если нужна помощь, проверьте:
1. Логи: `sudo journalctl -u crm-bot -f`
2. Конфигурация: `cat .env` (без секретов)
3. БД: `sqlite3 data/bot.sqlite ".tables"`
