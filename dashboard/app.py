"""aiohttp web-приложение для дашборда трафика.

Запускается в той же event loop, что и Telegram-бот (см. main.py). По
дефолту слушает 0.0.0.0:8001.

Маршруты:
  GET  /                          — редирект на /dashboard
  GET  /dashboard                 — HTML страница (мультиканальный)
  GET  /dashboard/vk              — алиас (legacy)
  GET  /api/leads                 — JSON снапшот кэша
  GET  /api/vk-leads              — алиас (legacy)
  POST /api/refresh               — принудительное обновление кэша
  POST /api/vk-refresh            — алиас (legacy)
  GET  /api/comments/{kind}/{id}  — комментарии таймлайна карточки
  GET  /api/vk-comments/{kind}/{id} — алиас (legacy)
  GET  /healthz                   — health-check
"""

import os
from pathlib import Path
from typing import Optional

import structlog
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from dashboard.service import get_service

logger = structlog.get_logger()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DASHBOARD_HTML_PATH = TEMPLATES_DIR / "dashboard.html"

# Опциональный токен — если задан DASHBOARD_TOKEN в .env, требуем
# ?token=... либо заголовок X-Dashboard-Token. Пустая строка = открыто.
ACCESS_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()


def _check_token(request: web.Request) -> Optional[web.Response]:
    if not ACCESS_TOKEN:
        return None
    provided = (
        request.query.get("token")
        or request.headers.get("X-Dashboard-Token")
        or ""
    ).strip()
    if provided != ACCESS_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


async def index(request: web.Request) -> web.Response:
    raise web.HTTPFound("/dashboard")


async def dashboard_page(request: web.Request) -> web.Response:
    err = _check_token(request)
    if err is not None:
        return err
    if not DASHBOARD_HTML_PATH.exists():
        return web.Response(text="dashboard.html template missing", status=500)
    return web.FileResponse(DASHBOARD_HTML_PATH)


async def api_leads(request: web.Request) -> web.Response:
    err = _check_token(request)
    if err is not None:
        return err
    svc = get_service()
    return web.json_response(svc.get_snapshot())


async def api_refresh(request: web.Request) -> web.Response:
    err = _check_token(request)
    if err is not None:
        return err
    svc = get_service()
    await svc.refresh()
    return web.json_response({"ok": True, "last_refresh": svc.get_snapshot()["last_refresh"]})


async def api_comments(request: web.Request) -> web.Response:
    err = _check_token(request)
    if err is not None:
        return err
    kind = request.match_info.get("kind", "lead")
    try:
        entity_id = int(request.match_info.get("id", "0"))
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)
    if kind not in ("lead", "deal"):
        return web.json_response({"error": "bad kind"}, status=400)
    svc = get_service()
    comments = await svc.get_comments_for(kind, entity_id)
    return web.json_response({"comments": comments})


async def healthz(request: web.Request) -> web.Response:
    svc = get_service()
    snap = svc.get_snapshot()
    return web.json_response({
        "ok": True,
        "last_refresh": snap["last_refresh"],
        "leads": snap["stats"]["leads_total"],
        "deals": snap["stats"]["deals_total"],
        "last_error": snap["last_error"],
    })


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)

    # Новые канонические пути
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/api/leads", api_leads)
    app.router.add_post("/api/refresh", api_refresh)
    app.router.add_get("/api/comments/{kind}/{id}", api_comments)

    # Legacy-алиасы
    app.router.add_get("/dashboard/vk", dashboard_page)
    app.router.add_get("/api/vk-leads", api_leads)
    app.router.add_post("/api/vk-refresh", api_refresh)
    app.router.add_get("/api/vk-comments/{kind}/{id}", api_comments)

    app.router.add_get("/healthz", healthz)
    return app


async def start_dashboard_server(
    host: str = "0.0.0.0",
    port: int = 8001,
    refresh_minutes: int = 5,
) -> tuple[web.AppRunner, AsyncIOScheduler]:
    """Поднимает HTTP-сервер и фоновый job обновления кэша."""
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info(
        "Dashboard HTTP started",
        host=host,
        port=port,
        auth=bool(ACCESS_TOKEN),
        url=f"http://{host}:{port}/dashboard",
    )

    svc = get_service()
    try:
        await svc.refresh()
    except Exception:
        logger.exception("Initial dashboard refresh failed (will retry by scheduler)")

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        svc.refresh,
        IntervalTrigger(minutes=refresh_minutes),
        id="dashboard_refresh",
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Dashboard refresh scheduler started", interval_minutes=refresh_minutes)

    return runner, scheduler


async def stop_dashboard_server(runner: web.AppRunner, scheduler: AsyncIOScheduler) -> None:
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    try:
        await runner.cleanup()
    except Exception:
        pass
