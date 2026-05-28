"""Локальный dev-сервер для превью дашборда.

Не запускает бота, не лезет в Bitrix24, не нуждается в .env. Отдаёт
HTML-шаблон с локального диска (можно редактировать и видеть изменения
после reload), а все /api/* запросы прозрачно проксирует на прод-сервер
http://31.130.135.86:8001 — там лежит свежий кэш реальных лидов.

Запуск:
    venv/bin/python3 dashboard/dev_preview.py

Затем открыть http://127.0.0.1:8001/dashboard
"""

import logging
import os
from pathlib import Path

import aiohttp
from aiohttp import web

UPSTREAM = "http://31.130.135.86:8001"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Если есть preview-версия с инлайн-данными — используем её (offline mode).
# Иначе живой шаблон + прокси на UPSTREAM.
TEMPLATE_PATH = (
    TEMPLATES_DIR / "dashboard_preview.html"
    if (TEMPLATES_DIR / "dashboard_preview.html").exists()
    else TEMPLATES_DIR / "dashboard.html"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dev-preview")


async def index(request: web.Request) -> web.Response:
    raise web.HTTPFound("/dashboard")


async def dashboard_page(request: web.Request) -> web.Response:
    if not TEMPLATE_PATH.exists():
        return web.Response(text=f"Template not found: {TEMPLATE_PATH}", status=500)
    # Не кэшируем — даём редактору перезагружать сразу
    headers = {"Cache-Control": "no-store, must-revalidate"}
    return web.FileResponse(TEMPLATE_PATH, headers=headers)


async def proxy(request: web.Request) -> web.StreamResponse:
    """Прозрачный прокси на UPSTREAM для всех /api/* запросов."""
    target_url = UPSTREAM + request.path_qs
    method = request.method
    body = await request.read() if method in ("POST", "PUT", "PATCH") else None

    # Прокидываем все заголовки кроме хост-специфичных
    hop_by_hop = {"host", "connection", "keep-alive", "proxy-authorization",
                  "proxy-authenticate", "te", "trailers", "transfer-encoding", "upgrade"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in hop_by_hop}

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, target_url, headers=headers, data=body, allow_redirects=False) as upstream:
                resp_body = await upstream.read()
                resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in hop_by_hop}
                log.info("proxy %s %s -> %s (%d bytes)", method, request.path, upstream.status, len(resp_body))
                return web.Response(body=resp_body, status=upstream.status, headers=resp_headers)
    except Exception as e:
        log.error("proxy %s %s failed: %s", method, request.path, e)
        return web.json_response({"error": "upstream_unreachable", "detail": str(e)}, status=502)


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/dashboard/vk", dashboard_page)  # legacy alias
    if STATIC_DIR.exists():
        app.router.add_static("/static/", path=STATIC_DIR, show_index=False)
    # Прокси на ВСЕ /api/* эндпоинты — GET, POST, прочее
    app.router.add_route("*", "/api/{tail:.*}", proxy)
    app.router.add_route("*", "/healthz", proxy)

    # preview_start (.claude/launch.json) пробрасывает желаемый порт через
    # env var PORT. Если её нет — дефолт 8001.
    port = int(os.environ.get("PORT", "8001"))
    host = os.environ.get("HOST", "127.0.0.1")

    print()
    print("=" * 70)
    print(f"  Dashboard preview running:  http://{host}:{port}/dashboard")
    print(f"  API проксируется на:        {UPSTREAM}")
    print(f"  HTML шаблон:                {TEMPLATE_PATH}")
    print(f"  (редактируешь файл -> жмёшь Reload в браузере)")
    print("=" * 70)
    print()

    web.run_app(app, host=host, port=port, print=lambda *a, **kw: None)


if __name__ == "__main__":
    main()
