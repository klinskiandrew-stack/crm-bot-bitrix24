#!/usr/bin/env bash
# Безопасный деплой crm-bot. Запускать НА СЕРВЕРЕ из /opt/crm-bot:
#   sudo bash scripts/safe_deploy.sh
#
# Гарантии:
#   1. Запоминает текущий коммит ДО pull (для отката).
#   2. git pull --ff-only.
#   3. py_compile ВСЕХ .py — если хоть один не компилится, откат и выход
#      БЕЗ рестарта (прод продолжает работать на старом коде).
#   4. Рестарт сервиса.
#   5. Ждёт "Run polling for bot" в логах до 60 сек. Если не появилось —
#      авто-откат на прошлый коммит + рестарт.
#
# История: 30.05 UnboundLocalError ушёл в прод и положил бота на часы,
# потому что деплой был "git pull + restart" без проверок. Этот скрипт
# закрывает тот класс инцидентов.

set -uo pipefail

REPO=/opt/crm-bot
SERVICE=crm-bot
SRC_USER=crmbot
LOG_TAG="safe_deploy"

cd "$REPO" || { echo "[$LOG_TAG] FATAL: no $REPO"; exit 1; }

echo "[$LOG_TAG] === START $(date -u +%H:%M:%S) ==="

# 1. запомнить текущий коммит
PREV_COMMIT=$(sudo -u "$SRC_USER" git rev-parse HEAD)
echo "[$LOG_TAG] prev commit: $PREV_COMMIT"

# 2. pull
if ! sudo -u "$SRC_USER" git pull --ff-only origin main 2>&1 | tail -3; then
    echo "[$LOG_TAG] FATAL: git pull failed — aborting, prod untouched"
    exit 1
fi
NEW_COMMIT=$(sudo -u "$SRC_USER" git rev-parse HEAD)
echo "[$LOG_TAG] new commit:  $NEW_COMMIT"

if [ "$PREV_COMMIT" = "$NEW_COMMIT" ]; then
    echo "[$LOG_TAG] nothing new to deploy"
    exit 0
fi

# 3. compile-check всех .py (кроме venv/.git).
# ВАЖНО: `python -m compileall -q` печатает ошибку, но возвращает EXIT=0 —
# на него полагаться НЕЛЬЗЯ. Используем compile_dir(quiet=2), который
# возвращает False при любой синтаксической ошибке → sys.exit(1).
echo "[$LOG_TAG] compiling all .py ..."
COMPILE_OUT=$(sudo -u "$SRC_USER" venv/bin/python -c "
import compileall, re, sys
ok = compileall.compile_dir('.', quiet=2, rx=re.compile(r'venv|\.git'), maxlevels=5)
sys.exit(0 if ok else 1)
" 2>&1)
COMPILE_RC=$?
if [ "$COMPILE_RC" != "0" ]; then
    echo "[$LOG_TAG] COMPILE FAILED:"
    echo "$COMPILE_OUT" | tail -15
    echo "[$LOG_TAG] rolling back to $PREV_COMMIT, NO restart (prod untouched)"
    sudo -u "$SRC_USER" git reset --hard "$PREV_COMMIT"
    echo "[$LOG_TAG] rolled back. Prod still on old working code."
    exit 1
fi
echo "[$LOG_TAG] compile OK"

# 4. рестарт
echo "[$LOG_TAG] restarting $SERVICE ..."
systemctl restart "$SERVICE"

# 5. ждать polling до 60 сек
echo "[$LOG_TAG] waiting for 'Run polling for bot' (up to 60s) ..."
OK=0
for i in $(seq 1 12); do
    sleep 5
    if journalctl -u "$SERVICE" --since "70 sec ago" --no-pager 2>/dev/null \
            | grep -q "Run polling for bot"; then
        OK=1
        echo "[$LOG_TAG] polling UP after ~$((i*5))s"
        break
    fi
    # ловим краш-сигнатуры раньше таймаута
    if journalctl -u "$SERVICE" --since "70 sec ago" --no-pager 2>/dev/null \
            | grep -qE "Traceback|UnboundLocal|ModuleNotFound|SyntaxError|Forcing os._exit"; then
        echo "[$LOG_TAG] crash signature in logs — abort wait"
        break
    fi
done

if [ "$OK" = "1" ]; then
    echo "[$LOG_TAG] === DEPLOY OK $(date -u +%H:%M:%S) ==="
    exit 0
fi

# 6. авто-откат
echo "[$LOG_TAG] polling DID NOT start — ROLLING BACK to $PREV_COMMIT"
sudo -u "$SRC_USER" git reset --hard "$PREV_COMMIT"
systemctl restart "$SERVICE"
sleep 8
if journalctl -u "$SERVICE" --since "20 sec ago" --no-pager 2>/dev/null \
        | grep -q "Run polling for bot"; then
    echo "[$LOG_TAG] rollback OK — prod restored on $PREV_COMMIT"
else
    echo "[$LOG_TAG] ⚠️ rollback restart unclear — CHECK MANUALLY"
fi
echo "[$LOG_TAG] === DEPLOY FAILED, rolled back ==="
exit 1
