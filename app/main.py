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

    bg_tasks: list[asyncio.Task] = []
    bg_tasks.append(asyncio.create_task(run_balance_alert_loop()))

    # Канал MAX (мессенджер) — тот же движок, запускается независимо от Telegram
    # (в т.ч. в web-only режиме), если задан MAX_BOT_TOKEN.
    if settings.max_enabled:
        from app.bot.max_bot import max_bot
        bg_tasks.append(asyncio.create_task(max_bot.run_polling()))
        log.info("Канал MAX включён (long polling)")

    # Web-only режим (тестовый веб-виджет / только MAX): Telegram-бота не поднимаем,
    # держим aiohttp-сервер с /web/* эндпоинтами + фоновые каналы.
    if not settings.telegram_enabled:
        runner = await _run_webhook_server(None)
        log.info("Telegram отключён (TELEGRAM_ENABLED=false) — web-only режим, "
                 "работает веб-сервер %s:%s%s", settings.webhook_host,
                 settings.webhook_port,
                 " + MAX" if settings.max_enabled else "")
        try:
            await asyncio.Event().wait()  # держим процесс живым
        finally:
            for t in bg_tasks:
                t.cancel()
            await runner.cleanup()
            await storage.close()
            if settings.max_enabled:
                from app.bot.max_bot import max_bot
                await max_bot.close()
        return

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    runner = await _run_webhook_server(bot)
    bg_tasks.append(asyncio.create_task(run_reminder_loop(bot)))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Запуск polling…")
        await dp.start_polling(bot)
    finally:
        for t in bg_tasks:
            t.cancel()
        await runner.cleanup()
        await storage.close()
        await bot.session.close()
        if settings.max_enabled:
            from app.bot.max_bot import max_bot
            await max_bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
