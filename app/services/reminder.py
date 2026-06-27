"""Фоновое напоминание клиенту при молчании.

Если после ответа бота клиент N минут ничего не пишет — бот сам присылает
мягкий пинг. Один раз за паузу: метка reminder_sent снимается, только когда
клиент снова напишет (см. storage.add_message с role='user')."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from app.bot.texts import REMINDER
from app.config import settings
from app.db.storage import storage

log = logging.getLogger(__name__)

_CHECK_INTERVAL = 60  # как часто опрашивать БД, сек


async def run_reminder_loop(bot: Bot) -> None:
    if not settings.reminder_enabled:
        log.info("Напоминания при молчании выключены (reminder_enabled=false)")
        return

    idle = settings.reminder_idle_minutes * 60
    # верхняя граница окна: не пинговать диалоги, что молчат уже слишком долго
    # (например, после рестарта бота) — пинг только в пределах получаса от паузы
    max_idle = idle + 30 * 60
    log.info("Напоминания включены: пауза %.0f мин", settings.reminder_idle_minutes)

    while True:
        try:
            tg_ids = await storage.users_to_remind(idle, max_idle)
            for tg_id in tg_ids:
                await _remind(bot, tg_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл не должен падать
            log.exception("Ошибка в цикле напоминаний: %s", exc)
        await asyncio.sleep(_CHECK_INTERVAL)


async def _remind(bot: Bot, tg_id: int) -> None:
    # помечаем сразу, чтобы при сетевой ошибке не задвоить пинг
    await storage.mark_reminded(tg_id)
    try:
        await bot.send_message(tg_id, REMINDER)
    except TelegramForbiddenError:
        log.info("Напоминание не доставлено (бот заблокирован): %s", tg_id)
        return
    except TelegramRetryAfter as exc:
        log.warning("Флуд-лимит при напоминании %s: ждём %s сек", tg_id, exc.retry_after)
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось отправить напоминание %s: %s", tg_id, exc)
        return
    # фиксируем пинг в истории — он становится последним сообщением бота
    await storage.add_message(tg_id, "assistant", REMINDER)
    log.info("Отправлено напоминание клиенту %s", tg_id)
