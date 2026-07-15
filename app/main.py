"""Точка входа: aiogram-бот (long polling) + aiohttp-сервер вебхуков amoJo."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiohttp import web

from app.bot.handlers import router
from app.config import settings
from app.crm.webhook import build_app
from app.db.storage import storage
from app.services.balance_alert import run_balance_alert_loop
from app.services.reminder import run_reminder_loop

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sonya")


async def _run_webhook_server(bot: Bot) -> web.AppRunner:
    app = build_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
    await site.start()
    log.info("Webhook-сервер слушает %s:%s%s", settings.webhook_host,
             settings.webhook_port, settings.webhook_path)
    return runner


async def main() -> None:
    await storage.connect()

    # Web-only режим (тестовый веб-виджет): Telegram-бота не поднимаем,
    # держим только aiohttp-сервер с /web/* эндпоинтами.
    if not settings.telegram_enabled:
        runner = await _run_webhook_server(None)
        log.info("Telegram отключён (TELEGRAM_ENABLED=false) — web-only режим, "
                 "работает только веб-сервер %s:%s", settings.webhook_host,
                 settings.webhook_port)
        try:
            await asyncio.Event().wait()  # держим сервер живым
        finally:
            await runner.cleanup()
            await storage.close()
        return

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    runner = await _run_webhook_server(bot)
    reminder_task = asyncio.create_task(run_reminder_loop(bot))
    balance_task = asyncio.create_task(run_balance_alert_loop())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Запуск polling…")
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        balance_task.cancel()
        await runner.cleanup()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
