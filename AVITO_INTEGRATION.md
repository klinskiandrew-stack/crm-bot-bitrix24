# Avito Ads API Integration

Интеграция с рекламным кабинетом Avito для получения статистики по объявлениям и кампаниям.

## Что доступно

- **Список кампаний** — активные рекламные кампании
- **Статистика по объявлениям** (items) — просмотры, контакты, звонки за период
- **Статистика по кампаниям** (campaigns) — расходы, клики, показы, CPC, CTR

Все данные кэшируются на 30 минут (в памяти).

---

## Шаг 1: Зарегистрировать OAuth приложение

1. Зайти в [https://developers.avito.ru/](https://developers.avito.ru/)
2. Авторизоваться через Avito аккаунт (тот, где рекламный кабинет)
3. Раздел **"Мои приложения"** → **"Создать приложение"**
4. Указать:
   - Название: `GrowZone CRM Bot`
   - Callback URL: `http://localhost:8080/callback` (для локальной разработки)
   - На продакшене: `https://31.130.135.86/avito/callback` или другой

5. Получить:
   - **Client ID** (публичный)
   - **Client Secret** (храни в безопасности!)

> **Безопасность:** Client Secret **НИКОГДА** не выкладывай в открытый вид!
> Используй переменные окружения (.env), не коммитай в git.

---

## Шаг 2: Получить refresh_token

**На локальной машине:**

```bash
python scripts/avito_oauth_init.py \
  --client-id=PyGrYjlzuN_sqxAqA9h7 \
  --client-secret=Db8_EDDhCv6KR85KT4YpZlCacvxeY6BwIg_wOUrE
```

Скрипт:
1. Выдаст ссылку на авторизацию (visit Avito)
2. Ты кликнешь "Разрешить"
3. Перенаправится на callback URL с кодом `?code=...`
4. Скрипт обменяет код на **refresh_token**

Сохрани refresh_token в .env.

---

## Шаг 3: Настроить .env

```bash
# .env (на сервере или локально)

AVITO_CLIENT_ID=PyGrYjlzuN_sqxAqA9h7
AVITO_CLIENT_SECRET=Db8_EDDhCv6KR85KT4YpZlCacvxeY6BwIg_wOUrE
AVITO_REFRESH_TOKEN=<получено из шага 2>
AVITO_USER_ID=<ID твоего аккаунта в Avito>
```

### Как узнать AVITO_USER_ID?

При первом вызове API без user_id, Avito вернёт error с доступными user IDs.
Или:
1. Зайти в https://api.avito.ru/docs (если у тебя есть доступ)
2. В примере запроса видно структуру

---

## Шаг 4: Проверить интеграцию

На сервере:

```bash
# SSH на сервер
ssh timeweb-crm

# Тестовый запрос
cd /opt/crm-bot && python -c "
import asyncio
from avito.client import avito_client

async def test():
    result = await avito_client.get_campaigns('YOUR_USER_ID')
    print(result)

asyncio.run(test())
"
```

Если OK → в ответе будет список кампаний.

---

## Шаг 5: Использовать в боте

Напиши Claude в чате:

```
Какова статистика по объявлениям на Avito за последнюю неделю?
```

Claude автоматически:
1. Вызовет `get_avito_stats(date_from="2026-05-11", date_to="2026-05-18", stat_type="items")`
2. Получит: просмотры, контакты, звонки по каждому объявлению
3. Вернёт в Telegram красиво отформатировано

---

## Доступные tools для Claude

### `get_avito_campaigns()`
Получить список всех активных кампаний.

**Пример:**
```
"Какие кампании сейчас активны?"
→ Claude вызовет get_avito_campaigns
→ вернёт ID, названия, статусы
```

### `get_avito_stats(date_from, date_to, stat_type)`

#### stat_type = "items"
Статистика по отдельным объявлениям.

**Параметры:**
- `date_from`, `date_to` — в формате YYYY-MM-DD
- Возвращает: views, contacts, calls для каждого объявления

**Пример:**
```
"Сколько просмотров за последние 7 дней?"
→ Claude автоматически вычислит даты
→ вызовет get_avito_stats(..., stat_type="items")
→ покажет топ объявлений по просмотрам
```

#### stat_type = "campaigns"
Статистика по кампаниям (расходы, клики, показы).

**Возвращает:** cost, impressions, clicks, CPC, CTR для каждой кампании

**Пример:**
```
"Сколько мы потратили на рекламу на Avito в этом месяце?"
→ get_avito_stats(..., stat_type="campaigns")
→ покажет total_cost, по кампаниям
```

---

## Troubleshooting

### ❌ "Avito API not configured"
✅ Проверь: `AVITO_CLIENT_ID` и `AVITO_CLIENT_SECRET` в .env (на сервере → `/opt/crm-bot/.env`)

### ❌ "AVITO_USER_ID not set"
✅ Добавь `AVITO_USER_ID` в .env

### ❌ "Token refresh failed (403)"
✅ refresh_token истёк или неверный → перейди на Шаг 2, получи новый

### ❌ "Empty response from Avito"
✅ API вернул пустой результат. Возможно:
- Нет объявлений за выбранный период
- Объявления из другого аккаунта

**Проверь:**
```bash
# На сервере
ssh timeweb-crm
sudo journalctl -u crm-bot -f | grep -i avito
```

### ❌ "Кэш не обновляется"
Данные кэшируются на **30 минут**. Дождись обновления или перезагрузи бота:
```bash
sudo systemctl restart crm-bot
```

---

## Архитектура

```
avito/
├── __init__.py
└── client.py          # AvitoClient class
    ├── OAuth 2.0 flow (refresh_token)
    ├── get_campaigns()
    ├── get_stats_items()
    └── get_stats_campaigns()

ai/tools.py            # Tool definitions для Claude
ai/tool_handlers.py    # Обработчики (get_avito_campaigns, get_avito_stats)

config.py              # Настройки (avito_client_id, etc.)
.env                   # Credentials (AVITO_CLIENT_SECRET, AVITO_REFRESH_TOKEN)
```

---

## API документация Avito

- **Official docs:** https://developers.avito.ru/
- **Base URL:** `https://api.avito.ru`
- **Auth:** OAuth 2.0 Bearer token
- **Token lifetime:** 24 часа (auto-refresh on 403)
- **Rate limits:** стандартные REST API лимиты Avito

### Основные endpoints (реализованы в client.py):

```
GET /core/v1/accounts/{user_id}/campaigns
  → Список кампаний

POST /core/v1/accounts/{user_id}/stats/items
  → Статистика по объявлениям (views, contacts, calls)

POST /core/v1/accounts/{user_id}/stats/campaigns
  → Статистика по кампаниям (cost, impressions, clicks)
```

---

## Важно

1. **Refresh token** сохраняется в .env и используется автоматически
2. **Access token** живёт 24 часа, client автоматически получает новый по refresh_token
3. На 403 Forbidden клиент **пересчитывает токен и повторяет запрос**
4. Все запросы **кэшируются на 30 минут** (в памяти)
5. При **рестарте бота кэш очищается**

---

## Следующие шаги (планы)

- [ ] Webhook от Avito для real-time уведомлений
- [ ] Более детальная аналитика (по типам объявлений, регионам)
- [ ] Интеграция расходов Avito в таблицу ЛУС (для рентабельности)
- [ ] Парсинг качества объявлений (рейтинг, отзывы)
