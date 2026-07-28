"""Приём вебхуков amoJo: ответ менеджера -> пересылка клиенту в Telegram."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiohttp import web

from app.config import settings
from app.db.storage import storage

log = logging.getLogger(__name__)


def _extract(data: dict) -> tuple[str | None, str | None, str | None, dict | None]:
    """Достаём (conversation_id, text, sender_ref, media) из тела вебхука amoJo (v2).

    Реальный формат события: всё вложено в data["message"]:
        {"account_id": "...", "message": {
            "conversation": {"id": "<uuid>", "client_id": "tg-<tg_id>"},
            "sender": {"id": "...", "name": "..."},
            "message": {"type": "text|picture|file", "text": "...",
                        "media": "<url>", "file_name": "...", "file_size": 123}}}
    Наш идентификатор чата — conversation.client_id ("tg-<tg_id>"). media — None или
    {"url","type":"image|file","file_name","file_size"}. Имена медиа-полей у amoJo
    берём defensive (media/link) — точную схему подтверждаем на реальном пейлоаде.
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

    inner = msg.get("message") if isinstance(msg.get("message"), dict) else None
    text = inner.get("text") if inner else msg.get("text")

    media = None
    if inner:
        mtype = (inner.get("type") or "").lower()
        murl = inner.get("media") or inner.get("link") or inner.get("url")
        if murl and mtype and mtype != "text":
            media = {
                "url": murl,
                "type": "image" if mtype in ("picture", "image", "photo") else "file",
                "file_name": inner.get("file_name") or "file",
                "file_size": inner.get("file_size"),
            }

    sender = msg.get("sender") or {}
    sender_ref = sender.get("ref_id")  # есть у сообщений менеджера
    return conv, text, sender_ref, media


async def handle_webhook(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="bad json")

    log.debug("amoJo webhook: %s", data)
    conversation_id, text, _sender, media = _extract(data)

    # Раньше сообщение менеджера без текста дропалось — из-за этого фото не долетали
    # («Не вижу фото»). Теперь пропускаем, если есть текст ИЛИ медиа.
    if not conversation_id or (not text and not media):
        return web.json_response({"ok": True})  # эхо/служебное событие — игнорируем

    user = await storage.find_by_conversation(conversation_id)
    if not user:
        log.warning("Не найден пользователь для conversation_id=%s", conversation_id)
        return web.json_response({"ok": True})

    media_url = media["url"] if media else None
    media_type = media["type"] if media else None
    media_name = media["file_name"] if media else None
    caption = (text or "").strip()
    # что показать/сохранить как текст, если пришло только медиа
    content = caption or (media and ("📷 фото" if media_type == "image"
                                     else f"📎 файл: {media_name}")) or ""

    await storage.add_message(user.tg_id, "manager", content, media_url=media_url,
                              media_type=media_type, media_name=media_name)

    # «Обратная нога» ответа менеджера зависит от канала диалога.
    if user.channel == "web":
        from app.web_api import push_to_web
        await push_to_web(user.tg_id, {"text": content, "media_url": media_url,
                                       "media_type": media_type})
    elif user.channel == "max":
        from app.bot.max_bot import max_bot
        if media_url:
            await max_bot.send_media(user.tg_id, media_url, media_type, caption)
        else:
            await max_bot.send_message(user.tg_id, content)
    elif bot is None:
        log.warning("Telegram отключён (web-only): ответ менеджера для tg-канала пропущен")
    else:
        try:
            if media_url and media_type == "image":
                await bot.send_photo(user.tg_id, media_url, caption=caption or None)
            elif media_url:
                await bot.send_document(user.tg_id, media_url, caption=caption or None)
            else:
                await bot.send_message(user.tg_id, content)
        except Exception as exc:  # noqa: BLE001
            log.warning("Медиа/текст менеджера не доставлены в Telegram (%s), шлю ссылкой: %s",
                        user.tg_id, exc)
            fallback = (caption + "\n" + media_url) if media_url else content
            try:
                await bot.send_message(user.tg_id, fallback)
            except Exception as exc2:  # noqa: BLE001
                log.exception("Не удалось доставить ответ менеджера в Telegram: %s", exc2)

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
    # Раздача медиа из чатов (фото/файлы) — работает и в web-only режиме (bot=None)
    from app.services.media import serve_media
    app.router.add_get("/media/{name}", serve_media)
    add_widget_routes(app)
    add_admin_routes(app)
    add_web_routes(app)
    return app
