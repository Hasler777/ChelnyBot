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

    # один Telegram-пользователь = один контакт в amoCRM
    user = await storage.get_user(tg_id)
    existing_contact_id = user.amo_contact_id if user else None

    lead_id: int | None = None
    contact_id: int | None = existing_contact_id
    if settings.amo_enabled:
        try:
            if settings.amojo_enabled:
                # Сделку создаёт САМ чат (как каналы VK/MAX/Instagram). Бот только
                # готовит контакт — БЕЗ REST-сделки, иначе вышло бы 2 карточки.
                # Каждый заказ = своя беседа/сделка; флорист принял → закрыл →
                # следующее обращение клиента создаст новую сделку с чатом.
                contact_id = await amo.ensure_contact(
                    name=data.name, phone=data.phone, contact_id=existing_contact_id)
            else:
                lead_id, contact_id = await amo.create_lead(
                    name=data.name, phone=data.phone, product_name=data.product_name,
                    product_url=data.product_url, price=data.price, budget=data.budget,
                    delivery=data.delivery_method, comment=data.comment,
                    contact_id=existing_contact_id,
                )
                log.info("Создана сделка amoCRM #%s для tg_id=%s", lead_id, tg_id)
        except AmoError as exc:
            log.exception("Ошибка amoCRM при хэндофф: %s", exc)
    else:
        log.warning("amoCRM не настроен — сделка не создана (tg_id=%s)", tg_id)

    # переводим в режим живого чата (бот замолкает)
    conversation_id = chat.conversation_id_for(tg_id)
    await storage.update_user(
        tg_id,
        state=STATE_HANDOFF,
        amo_lead_id=lead_id,
        amojo_conversation_id=conversation_id,
        amo_contact_id=contact_id,
    )

    # открываем чат в amoJo. ВАЖНО: сперва создаём чат и привязываем его к контакту,
    # и только потом шлём первое сообщение — иначе amoCRM создаст вторую,
    # «неразобранную» сделку (дубль).
    if settings.amojo_enabled:
        try:
            if contact_id:
                chat_id = await chat.create_chat(
                    tg_id=tg_id, name=data.name or "Клиент", phone=data.phone or None
                )
                if chat_id:
                    try:
                        await amo.link_chat_to_contact(contact_id, chat_id)
                    except AmoError as exc:
                        # привязка не критична — сообщение всё равно отправим
                        log.warning("Привязка чата к контакту не удалась (продолжаем): %s", exc)
            client_name = data.name or "Клиент"
            # транскрипт флористу — переписка текущего заказа (с последнего /start)
            since = user.context_since if user else 0
            history = await storage.history_full(tg_id, limit=80, since=since)
            role_names = {"user": "Клиент", "assistant": "Соня", "manager": "Менеджер"}
            dialog_lines = [
                f"{role_names.get(r['role'], r['role'])}: {r['content']}"
                for r in history if r.get("content")
            ]
            if dialog_lines:
                transcript = "📋 Переписка клиента с Соней:\n\n" + "\n".join(dialog_lines)
                await chat.send_to_amo(tg_id=tg_id, text=transcript,
                                       name=client_name, phone=data.phone or None)
            head = f"📌 Новая заявка от {client_name}"
            if data.phone:
                head += f", тел. {data.phone}"
            details = []
            if data.product_name:
                details.append(
                    f"{data.product_name} — {int(data.price)} ₽" if data.price
                    else data.product_name
                )
            elif data.budget:
                details.append(f"бюджет {data.budget}")
            if data.delivery_method:
                details.append(data.delivery_method)
            if data.comment:
                details.append(data.comment)
            summary = head + ("\n" + ", ".join(details) if details else "")
            await chat.send_to_amo(tg_id=tg_id, text=summary, name=client_name,
                                   phone=data.phone or None)
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось открыть чат в amoJo: %s", exc)

    return HANDOFF_MESSAGE


async def forward_client_message(tg_id: int, text: str) -> None:
    """В режиме handoff — сохранить сообщение клиента (его покажет виджет в карточке)
    и, если настроен нативный чат amoJo, продублировать туда."""
    await storage.add_message(tg_id, "user", text)

    if settings.amojo_enabled:
        user = await storage.get_user(tg_id)
        name = (user.name if user else None) or "Клиент"
        phone = user.phone if user else None
        try:
            await chat.send_to_amo(tg_id=tg_id, text=text, name=name, phone=phone)
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось переслать сообщение клиента в amoJo: %s", exc)
