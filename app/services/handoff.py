"""Передача диалога флористу: создание сделки в amoCRM + перевод в режим чата."""
from __future__ import annotations

import logging

from app.config import settings
from app.crm import chat
from app.crm.amocrm import AmoError, amo
from app.db.storage import STATE_HANDOFF, storage
from app.llm.consultant import HandoffData

log = logging.getLogger(__name__)

HANDOFF_MESSAGE = "Передаю флористу — он сейчас подключится 🌸"


async def do_handoff(tg_id: int, data: HandoffData) -> str:
    """Создаёт сделку, открывает чат менеджеру, переводит пользователя в handoff.

    Возвращает текст, который бот отправит клиенту.
    """
    # сохраняем контактные данные пользователя
    await storage.update_user(tg_id, name=data.name or None, phone=data.phone or None)

    lead_id: int | None = None
    if settings.amo_enabled:
        try:
            lead_id = await amo.create_lead(
                name=data.name,
                phone=data.phone,
                product_name=data.product_name,
                product_url=data.product_url,
                price=data.price,
                budget=data.budget,
                delivery=data.delivery_method,
                comment=data.comment,
            )
            log.info("Создана сделка amoCRM #%s для tg_id=%s", lead_id, tg_id)
        except AmoError as exc:
            log.exception("Ошибка создания сделки в amoCRM: %s", exc)
    else:
        log.warning("amoCRM не настроен — сделка не создана (tg_id=%s)", tg_id)

    # переводим в режим живого чата (бот замолкает)
    conversation_id = chat.conversation_id_for(tg_id)
    await storage.update_user(
        tg_id,
        state=STATE_HANDOFF,
        amo_lead_id=lead_id,
        amojo_conversation_id=conversation_id,
    )

    # отправляем в amoJo первую реплику-контекст, чтобы у менеджера открылся чат
    if settings.amojo_enabled:
        summary = (
            f"Заявка от {data.name or 'клиента'} ({data.phone}). "
            f"Букет: {data.product_name} — {int(data.price)} ₽, {data.delivery_method}."
        )
        try:
            await chat.send_to_amo(tg_id=tg_id, text=summary, name=data.name or "Клиент",
                                   phone=data.phone or None)
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось открыть чат в amoJo: %s", exc)

    return HANDOFF_MESSAGE


async def forward_client_message(tg_id: int, text: str) -> None:
    """В режиме handoff — переслать сообщение клиента в amoCRM чат."""
    if not settings.amojo_enabled:
        log.warning("amoJo не настроен — сообщение клиента не доставлено менеджеру")
        return
    user = await storage.get_user(tg_id)
    name = (user.name if user else None) or "Клиент"
    phone = user.phone if user else None
    try:
        await chat.send_to_amo(tg_id=tg_id, text=text, name=name, phone=phone)
    except Exception as exc:  # noqa: BLE001
        log.exception("Не удалось переслать сообщение клиента в amoJo: %s", exc)
