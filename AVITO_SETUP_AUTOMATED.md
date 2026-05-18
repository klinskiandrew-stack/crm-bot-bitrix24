# Automated Avito API Setup

Полная автоматизированная настройка Avito API за 2 шага.

## Step 1: Authorize & Get Refresh Token

```bash
cd /Users/andykravets/Documents/Claude/Projects/Ассистент\ Гроу/.claude/worktrees/sweet-borg-5bd63b

python3 scripts/oauth_callback_server.py \
  PyGrYjlzuN_sqxAqA9h7 \
  Db8_EDDhCv6KR85KT4YpZlCacvxeY6BwIg_wOUrE
```

Скрипт:
1. Запустит локальный OAuth сервер
2. Выведет URL авторизации
3. **Откройте URL в браузере и кликните "Allow"**
4. Получит refresh_token
5. Сохранит в `/tmp/avito_refresh_token.json`

## Step 2: Deploy to Server

```bash
python3 scripts/deploy_avito.py /tmp/avito_refresh_token.json
```

Скрипт:
1. Запросит твой Avito User ID
2. Подключится к серверу по SSH
3. Добавит credentials в `/opt/crm-bot/.env`
4. Рестартит бота
5. Проверит статус

---

## 🎉 Done!

Авито интеграция готова. Тестируй в Telegram:

```
Какова статистика на Avito за последние 7 дней?
```

---

## Troubleshooting

**"Failed to connect to Avito"**
- Проверь интернет соединение
- Проверь что browser открыт и OAuth сервер получил callback

**"SSH: permission denied"**
- Проверь что SSH ключи настроены для `timeweb-crm`
- Убедись что у пользователя есть доступ к `/opt/crm-bot/`

**"Authorization timeout"**
- OAuth сервер ждёт 10 минут
- Убедись что браузер открыт и авторизация завершена

---

## Manual Setup

Если автоматизированный скрипт не работает:

```bash
ssh timeweb-crm
nano /opt/crm-bot/.env

# Add:
AVITO_CLIENT_ID=PyGrYjlzuN_sqxAqA9h7
AVITO_CLIENT_SECRET=Db8_EDDhCv6KR85KT4YpZlCacvxeY6BwIg_wOUrE
AVITO_REFRESH_TOKEN=<your_token_here>
AVITO_USER_ID=<your_user_id_here>

# Save (Ctrl+O, Enter, Ctrl+X)

sudo systemctl restart crm-bot
```
