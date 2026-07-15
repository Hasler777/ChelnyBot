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

    Реальный формат события: всё вложено в data["message"]:
        {"account_id": "...", "message": {
            "conversation": {"id": "<uuid>", "client_id": "tg-<tg_id>"},
            "sender": {"id": "...", "name": "..."},
            "message": {"type": "text", "text": "..."}}}
    Наш идентификатор чата — conversation.client_id ("tg-<tg_id>"), текст —
    message.message.text. Оставляем запасные пути на случай иного формата.
    """
    msg = data.get("message")
    if not isinstance(msg, dict):
        msg = data  # запасной путь, если формат отличается

    conversation = msg.get("conversation") or {}
    conv = (
        conversation.get("client_id")
        or conversation.get("id")
        or (msg.get("receiver") or {}).get("client_id")
        or msg.get("conversation_id")
        or msg.get("client_id")
    )

    inner = msg.get("message")
    if isinstance(inner, dict):
        text = inner.get("text")
    else:
        text = msg.get("text")

    sender = msg.get("sender") or {}
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

    # «Обратная нога» ответа менеджера зависит от канала диалога:
    #   web — в браузер через SSE (+ сохраняем в историю, чтобы виджет показал
    #         сообщение после перезагрузки страницы);
    #   tg  — как раньше, прямиком в Telegram.
    if user.channel == "web":
        from app.web_api import push_to_web
        await storage.add_message(user.tg_id, "manager", text)
        await push_to_web(user.tg_id, text)
    elif bot is None:
        log.warning("Telegram отключён (web-only): ответ менеджера для tg-канала пропущен")
    else:
        try:
            await bot.send_message(user.tg_id, text)
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось доставить ответ менеджера в Telegram: %s", exc)

    return web.json_response({"ok": True})


def build_app(bot: Bot) -> web.Application:
    from app.admin_api import add_admin_routes
    from app.web_api import add_web_routes
    from app.widget_api import add_widget_routes

    app = web.Application()
    app["bot"] = bot
    # amoCRM шлёт хук на <webhook_path>/<scope_id>; принимаем и с сегментом, и без него
    app.router.add_post(settings.webhook_path, handle_webhook)
    app.router.add_post(settings.webhook_path.rstrip("/") + "/{scope_id}", handle_webhook)
    app.router.add_get("/health", lambda _r: web.Response(text="ok"))
    add_widget_routes(app)
    add_admin_routes(app)
    add_web_routes(app)
    return app
