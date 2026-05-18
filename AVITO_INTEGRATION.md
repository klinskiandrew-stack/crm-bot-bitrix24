# Avito API Integration

Интеграция с Avito API через "Приложение персональной авторизации" (client_credentials).

## Что доступно ботy

| Tool | Что возвращает |
|------|----------------|
| `avito_balance` | Баланс счёта (рубли + бонусы) |
| `avito_items` | Список активных объявлений (id, title, цена, адрес, URL) |
| `avito_stats` | Статистика за период: views/uniqViews/uniqContacts/uniqFavorites по объявлениям |
| `avito_spend` | Расходы за период: разбивка по типам услуг (CPA, тарифы), сторно, депозиты |
| `avito_calls` | Звонки на объявления (calltracking) |

Кэш ответов: 30 минут. Токен живёт 24 часа, обновляется автоматически.

## Настройка (.env)

```
AVITO_CLIENT_ID=PyGrYjlzuN_sqxAqA9h7
AVITO_CLIENT_SECRET=Db8_EDDhCv6KR85KT4YpZlCacvxeY6BwIg_wOUrE
AVITO_USER_ID=22514000
```

Где взять:
- **Client ID/Secret** — developers.avito.ru → Мои приложения → Приложение персональной авторизации
- **User ID** — "Номер профиля" в том же кабинете (для Growzone: `22514000`)

## Используемые endpoints

```
POST /token                                  → access_token (client_credentials)
GET  /core/v1/accounts/self                  → профиль (email/phones)
GET  /core/v1/accounts/{id}/balance/         → баланс
POST /core/v1/accounts/operations_history/   → операции (расходы/депозиты)
GET  /core/v1/items                          → список объявлений
POST /stats/v1/accounts/{id}/items           → статистика по объявлениям
POST /calltracking/v1/getCalls/              → звонки
```

## Примеры запросов в боте

```
Сколько на балансе Avito?
Какие у нас активные объявления на Avito?
Сколько просмотров и контактов было за последние 7 дней?
Сколько мы потратили на Avito с 11 по 17 мая?
Покажи звонки с Avito за вчера.
```
