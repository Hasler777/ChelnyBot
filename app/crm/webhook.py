"""Приём вебхуков amoJo: ответ менеджера -> пересылка клиенту в Telegram."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiohttp import web

from app.config import settings
from app.db.storage import storage

log = logging.getLogger(__name__)


def _extract(data: dict) -> tuple[str | None, str | None, str | None]:
    """Достаём (conversation_id, text, sender_ref) из тела вебхука amoJo (v2).

    Документированный формат: data["payload"]["conversation_id"] и
    data["payload"]["message"]["text"]. Conversation_id — наш идентификатор чата
    (мы задаём его как "tg-<tg_id>"). Оставляем запасные пути на случай отличий.
    """
    payload = data.get("payload", data)
    if not isinstance(payload, dict):
        return None, None, None

    conv = (
        payload.get("conversation_id")
        or payload.get("client_id")
        or (payload.get("receiver") or {}).get("client_id")
    )

    message = payload.get("message") or {}
    text = message.get("text") if isinstance(message, dict) else None

    sender = payload.get("sender") or {}
    sender_ref = sender.get("ref_id")  # есть у сообщений менеджера
    return conv, text, sender_ref


async def handle_webhook(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="bad json")

    log.debug("amoJo webhook: %s", data)
    conversation_id, text, _sender = _extract(data)

    if not conversation_id or not text:
        return web.json_response({"ok": True})  # эхо/служебное событие — игнорируем

    user = await storage.find_by_conversation(conversation_id)
    if not user:
        log.warning("Не найден пользователь для conversation_id=%s", conversation_id)
        return web.json_response({"ok": True})

    try:
        await bot.send_message(user.tg_id, text)
    except Exception as exc:  # noqa: BLE001
        log.exception("Не удалось доставить ответ менеджера в Telegram: %s", exc)

    return web.json_response({"ok": True})


def build_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post(settings.webhook_path, handle_webhook)
    app.router.add_get("/health", lambda _r: web.Response(text="ok"))
    return app
