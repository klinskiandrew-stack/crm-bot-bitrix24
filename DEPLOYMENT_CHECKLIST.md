# ✅ Чек-лист развертывания на Timeweb

## 📋 Подготовка (выполнить перед развертыванием)

### Telegram Setup
- [ ] Создать бота через @BotFather
- [ ] Получить **TELEGRAM_BOT_TOKEN**
- [ ] Узнать свой **ADMIN_TELEGRAM_ID** (используя @userinfobot)
- [ ] Проверить, что бот добавлен в тестовую группу
- [ ] Проверить, что можно упомянуть бота в группе (@bot_username)

### Bitrix24 Setup
- [ ] Войти в админ-панель портала
- [ ] Перейти в раздел "Интеграция" > "REST API"
- [ ] Создать **Входящий вебхук** (Incoming webhook)
- [ ] Скопировать **B24_WEBHOOK_URL**
- [ ] Убедиться, что у вебхука есть права на:
  - [ ] crm.deal.list
  - [ ] crm.deal.get
  - [ ] crm.lead.list
  - [ ] crm.lead.get
  - [ ] crm.contact.list
  - [ ] crm.contact.get
  - [ ] crm.company.list
  - [ ] crm.activity.list
  - [ ] crm.dealcategory.stage.list
  - [ ] user.get

### Kie.ai Setup
- [ ] Зарегистрироваться на https://kie.ai
- [ ] Получить **KIE_API_KEY**
- [ ] Установить IP-белый список сервера Timeweb (для безопасности)
- [ ] Проверить тарификацию и баланс

### Timeweb Server
- [ ] Получить доступ к серверу (SSH)
- [ ] Узнать **IP адрес** сервера
- [ ] Убедиться, что сервер имеет доступ в интернет
- [ ] Проверить, что установлены Python 3.11+, Git, SQLite3

## 🚀 Развертывание

### Шаг 1: SSH и базовое ПО
- [ ] Подключиться к серверу: `ssh root@IP`
- [ ] Обновить систему: `apt update && apt upgrade -y`
- [ ] Установить ПО: `apt install -y python3.11 python3.11-venv git sqlite3`
- [ ] Создать пользователя: `useradd -m -s /bin/bash crmbot`
- [ ] Переключиться: `su - crmbot`

### Шаг 2: Клонирование проекта
- [ ] Клонировать репозиторий в /opt/crm-bot
- [ ] Создать виртуальное окружение: `python3.11 -m venv venv`
- [ ] Активировать: `source venv/bin/activate`
- [ ] Установить зависимости: `pip install -r requirements.txt`
- [ ] Создать директории: `mkdir -p data logs`

### Шаг 3: Конфигурация
- [ ] Создать файл `.env`
- [ ] Заполнить **TELEGRAM_BOT_TOKEN**
- [ ] Заполнить **ADMIN_TELEGRAM_ID**
- [ ] Заполнить **B24_WEBHOOK_URL**
- [ ] Заполнить **KIE_API_KEY**
- [ ] Установить права: `chmod 600 .env`

### Шаг 4: База данных
- [ ] Инициализировать БД: `python3 scripts/init_db.py`
- [ ] Проверить, что создана `data/bot.sqlite`
- [ ] Добавить партнеров (пользователей) в БД

### Шаг 5: Тестирование перед запуском
- [ ] Проверить конфигурацию: `python3 -c "from config import settings; print(settings.admin_telegram_id)"`
- [ ] Проверить модули: `python3 -m py_compile main.py`
- [ ] Запустить бота: `python3 main.py`
- [ ] Написать боту в Telegram (упомянуть его в группе)
- [ ] Проверить, что получился ответ
- [ ] Остановить бота: `Ctrl+C`

### Шаг 6: Systemd сервис
- [ ] Создать файл `/etc/systemd/system/crm-bot.service`
- [ ] Перезагрузить systemd: `sudo systemctl daemon-reload`
- [ ] Включить автозапуск: `sudo systemctl enable crm-bot`
- [ ] Запустить: `sudo systemctl start crm-bot`
- [ ] Проверить статус: `sudo systemctl status crm-bot`

### Шаг 7: Проверка работоспособности
- [ ] Просмотреть логи: `sudo journalctl -u crm-bot -f`
- [ ] Написать боту в Telegram и получить ответ
- [ ] Проверить, что логируется в БД: `sudo journalctl -u crm-bot | grep "Group message processed"`

## 🔍 Мониторинг

### Первая неделя
- [ ] Ежедневно проверять логи на ошибки
- [ ] Следить за использованием credits на Kie.ai
- [ ] Следить за ростом размера БД
- [ ] Проверить работоспособность маршрутизации моделей

### Регулярное обслуживание
- [ ] Еженедельно: проверять статус сервиса
- [ ] Каждые 2 недели: сделать бэкап БД
- [ ] Ежемесячно: очистить audit_log (> 90 дней)
- [ ] По необходимости: добавить новых партнеров

## 🚨 Проверка после проблем

### Если бот не отвечает
- [ ] Проверить, что сервис запущен: `sudo systemctl status crm-bot`
- [ ] Просмотреть логи: `sudo journalctl -u crm-bot -n 100`
- [ ] Проверить доступ к Telegram: `curl https://api.telegram.org/`
- [ ] Проверить доступ к Kie.ai: `curl https://api.kie.ai/`
- [ ] Перезагрузить сервис: `sudo systemctl restart crm-bot`

### Если медленно работает
- [ ] Проверить размер БД: `du -h data/bot.sqlite`
- [ ] Очистить старые логи: `python3 scripts/healthcheck.py`
- [ ] Проверить количество записей: `sqlite3 data/bot.sqlite "SELECT COUNT(*) FROM audit_log;"`

### Если много ошибок
- [ ] Проверить лог: `sudo journalctl -u crm-bot | grep -i error | tail -20`
- [ ] Проверить .env: `cat .env`
- [ ] Проверить доступ к B24 вебхуку: `curl -X POST B24_WEBHOOK_URL`
- [ ] Проверить API ключи на Kie.ai

## 📊 Полезные команды

```bash
# Просмотр логов
sudo journalctl -u crm-bot -f              # реал-тайм
sudo journalctl -u crm-bot -n 100          # последние 100 строк
sudo journalctl -u crm-bot | grep error    # только ошибки

# Управление
sudo systemctl status crm-bot              # статус
sudo systemctl restart crm-bot             # перезапуск
sudo systemctl stop crm-bot                # остановка

# Статистика
cd /opt/crm-bot && source venv/bin/activate
python3 scripts/healthcheck.py             # здоровье системы

# Бэкап
cp data/bot.sqlite backup_$(date +%Y%m%d).db

# Размер
du -h data/bot.sqlite                      # размер БД
du -h .                                    # размер проекта
```

## 🎯 Успешное развертывание означает:

- ✅ Бот запущен и видит группу
- ✅ При упоминании бот отвечает (может быть "Ошибка при обращении к ИИ", это OK на этапе)
- ✅ Логи не содержат критических ошибок
- ✅ Сервис автоматически перезагружается при падении
- ✅ БД растет (появляются новые записи в audit_log)

---

**Статус:** Готовы к развертыванию! 🚀
