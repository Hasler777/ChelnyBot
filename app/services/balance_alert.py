"""Фоновый сторож баланса кошелька владельца.

Периодически считает остаток кошелька (тот же, что показывает /owner) и, когда он
падает ниже порога WALLET_ALERT_THRESHOLD_RUB, присылает владельцу уведомление через
ОТДЕЛЬНОГО телеграм-бота (NOTIFY_BOT_TOKEN → NOTIFY_CHAT_ID). Чтобы не спамить, алерт
шлётся один раз: флаг в app_state снимается только когда кошелёк пополнят выше порога.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.token import TokenValidationError

from app.admin_api import owner_wallet_state
from app.config import settings
from app.db.storage import storage

log = logging.getLogger(__name__)

_CHECK_INTERVAL = 15 * 60  # как часто проверять баланс, сек
_STATE_KEY = "wallet_low_alerted"  # "1" — алерт уже отправлен за текущий «низкий» период


async def run_balance_alert_loop() -> None:
    if not settings.wallet_alert_enabled:
        log.info("Сторож баланса выключен (wallet_alert_enabled=false)")
        return
    if not (settings.notify_bot_token and settings.notify_chat_id):
        log.warning("Сторож баланса не настроен: нет NOTIFY_BOT_TOKEN и/или NOTIFY_CHAT_ID")
        return

    try:
        bot = Bot(token=settings.notify_bot_token)
    except TokenValidationError as exc:
        log.error("Сторож баланса: некорректный NOTIFY_BOT_TOKEN (%s)", exc)
        return

    threshold = settings.wallet_alert_threshold_rub
    log.info("Сторож баланса включён: порог %.0f ₽, проверка каждые %d мин",
             threshold, _CHECK_INTERVAL // 60)
    try:
        while True:
            try:
                await _check_once(bot, threshold)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — цикл не должен падать
                log.exception("Ошибка в цикле сторожа баланса: %s", exc)
            await asyncio.sleep(_CHECK_INTERVAL)
    finally:
        await bot.session.close()


async def _check_once(bot: Bot, threshold: float) -> None:
    remaining = (await owner_wallet_state())["remaining"]
    already = (await storage.state_get(_STATE_KEY)) == "1"

    if remaining < threshold:
        if not already and await _notify(bot, remaining, threshold):
            await storage.state_set(_STATE_KEY, "1")
    elif already:
        # кошелёк пополнили выше порога — сбрасываем флаг, снова разрешаем алерт
        await storage.state_set(_STATE_KEY, "0")
        log.info("Баланс восстановлен (%.0f ₽) — флаг алерта снят", remaining)


async def _notify(bot: Bot, remaining: float, threshold: float) -> bool:
    """Шлёт уведомление владельцу. True — доставлено (тогда взводим флаг)."""
    rem = f"{remaining:,.0f}".replace(",", " ")
    thr = f"{threshold:,.0f}".replace(",", " ")
    text = (
        "⚠️ Баланс кабинета владельца заканчивается\n\n"
        f"Остаток: {rem} ₽ (порог {thr} ₽)\n\n"
        "Пополните кошелёк в кабинете /owner, чтобы бот продолжал работать без перебоев."
    )
    try:
        await bot.send_message(settings.notify_chat_id, text)
    except TelegramAPIError as exc:
        # частый случай: владелец ещё не нажал Start у бота-уведомлятора (403).
        # Флаг не взводим — попробуем снова на следующей проверке.
        log.warning("Не удалось отправить алерт о балансе: %s", exc)
        return False
    log.info("Отправлен алерт о низком балансе: остаток %.0f ₽", remaining)
    return True
