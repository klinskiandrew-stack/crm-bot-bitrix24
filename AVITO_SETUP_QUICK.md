# Быстрая настройка Avito API

## 5 минут на подготовку

### 1. Получить credentials от Avito
- Зайти https://developers.avito.ru/ → Мои приложения → Создать
- Скопировать:
  - ✅ Client ID: `PyGrYjlzuN_sqxAqA9h7`
  - ✅ Client Secret: `Db8_EDDhCv6KR85KT4YpZlCacvxeY6BwIg_wOUrE`

### 2. Получить refresh_token
На локальной машине:
```bash
cd /Users/andykravets/Documents/Claude/Projects/Ассистент\ Гроу/.claude/worktrees/sweet-borg-5bd63b

python3 scripts/avito_oauth_init.py \
  --client-id=PyGrYjlzuN_sqxAqA9h7 \
  --client-secret=Db8_EDDhCv6KR85KT4YpZlCacvxeY6BwIg_wOUrE
```

Скрипт выдаст ссылку → зайдёшь → кликнешь "Разрешить" → получишь refresh_token

### 3. Добавить в .env на сервере
SSH на сервер:
```bash
ssh timeweb-crm
nano /opt/crm-bot/.env
```

Добавить в конец:
```
AVITO_CLIENT_ID=PyGrYjlzuN_sqxAqA9h7
AVITO_CLIENT_SECRET=Db8_EDDhCv6KR85KT4YpZlCacvxeY6BwIg_wOUrE
AVITO_REFRESH_TOKEN=<refresh_token из шага 2>
AVITO_USER_ID=<узнать из Avito cabinet или API>
```

### 4. Рестартить бота
```bash
sudo systemctl restart crm-bot
sudo journalctl -u crm-bot -f | grep -i avito
```

Смотреть логи — должно быть `Avito API enabled` или похоже.

### 5. Тестировать в Telegram
Написать боту:
```
Какова статистика по объявлениям на Avito за последние 7 дней?
```

Бот должен вернуть просмотры, контакты, звонки.

---

## Что работает

✅ **Список кампаний** — `/get_avito_campaigns`
```
"Какие кампании у нас активны на Avito?"
```

✅ **Статистика по объявлениям** — `/get_avito_stats(..., stat_type="items")`
```
"Сколько просмотров было вчера?"
"Топ объявлений по контактам за неделю?"
```

✅ **Статистика по кампаниям** — `/get_avito_stats(..., stat_type="campaigns")`
```
"Сколько мы потратили на рекламу в мае?"
"Какой CPC у нас на Avito?"
```

---

## Что дальше?

Для детальной информации → [AVITO_INTEGRATION.md](AVITO_INTEGRATION.md)

Для troubleshooting → см. раздел "Troubleshooting" в AVITO_INTEGRATION.md
