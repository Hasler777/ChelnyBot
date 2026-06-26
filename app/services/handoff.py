"""Передача диалога флористу: контакт в amoCRM + чат amoJo (он же заводит сделку)."""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.crm import chat
from app.crm.amocrm import AmoError, amo
from app.db.storage import STATE_HANDOFF, storage
from app.llm.consultant import HandoffData

log = logging.getLogger(__name__)

HANDOFF_MESSAGE = "Передаю флористу — он сейчас подключится 🌸"


def _order_summary(data: HandoffData) -> str:
    client_name = data.name or "Клиент"
    head = f"📌 Новая заявка от {client_name}"
    if data.phone:
        head += f", тел. {data.phone}"
    details = []
    if data.product_name:
        details.append(
            f"{data.product_name} — {int(data.price)} ₽" if data.price else data.product_name
        )
    elif data.budget:
        details.append(f"бюджет {data.budget}")
    if data.delivery_method:
        details.append(data.delivery_method)
    if data.comment:
        details.append(data.comment)
    return head + ("\n" + ", ".join(details) if details else "")


async def _wait_new_lead(contact_id: int, before: set[int],
                         attempts: int = 6, delay: float = 1.2) -> int | None:
    """Дождаться сделки, которую завёл чат (её id не было среди before)."""
    for _ in range(attempts):
        fresh = (await amo.contact_lead_ids(contact_id)) - before
        if fresh:
            return max(fresh)
        await asyncio.sleep(delay)
    return None


async def do_handoff(tg_id: int, data: HandoffData) -> str:
    """Передаёт клиента флористу: контакт + чат amoJo (он же заводит сделку).

    ВАЖНО про дубли: при включённом amoJo бот НЕ создаёт сделку через REST.
    Входящее сообщение в чат само заводит ОДИН лид (как у каналов VK/MAX/Instagram).
    Если делать ещё и REST-сделку — получаются ДВЕ карточки (сделка + «Неразобранное»).
    """
    await storage.update_user(tg_id, name=data.name or None, phone=data.phone or None)

    user = await storage.get_user(tg_id)
    existing_contact_id = user.amo_contact_id if user else None

    lead_id: int | None = None
    contact_id: int | None = existing_contact_id

    # 1) Контакт (один tg = один контакт; имя/телефон актуальные)
    if settings.amo_enabled:
        try:
            contact_id = await amo.ensure_contact(
                name=data.name, phone=data.phone, contact_id=existing_contact_id)
        except AmoError as exc:
            log.exception("Контакт amoCRM не создан/обновлён: %s", exc)

    # 2) Без чата amoJo — сделку заводим через REST (дубля нет, т.к. чата нет)
    if settings.amo_enabled and not settings.amojo_enabled:
        try:
            lead_id, contact_id = await amo.create_lead(
                name=data.name, phone=data.phone, product_name=data.product_name,
                product_url=data.product_url, price=data.price, budget=data.budget,
                delivery=data.delivery_method, comment=data.comment, contact_id=contact_id,
            )
            log.info("Создана сделка amoCRM #%s (без чата) для tg_id=%s", lead_id, tg_id)
        except AmoError as exc:
            log.exception("Ошибка создания сделки в amoCRM: %s", exc)
    elif not settings.amo_enabled:
        log.warning("amoCRM не настроен — сделка не создана (tg_id=%s)", tg_id)

    # переводим в режим живого чата (бот замолкает)
    conversation_id = chat.conversation_id_for(tg_id)
    await storage.update_user(
        tg_id, state=STATE_HANDOFF, amo_lead_id=lead_id,
        amojo_conversation_id=conversation_id, amo_contact_id=contact_id,
    )

    # 3) amoJo: чат + сводка. Первое сообщение в чат заводит ОДИН лид.
    if settings.amojo_enabled:
        try:
            leads_before: set[int] = set()
            if contact_id:
                leads_before = await amo.contact_lead_ids(contact_id)
                chat_id = await chat.create_chat(
                    tg_id=tg_id, name=data.name or "Клиент", phone=data.phone or None
                )
                if chat_id:
                    try:
                        await amo.link_chat_to_contact(contact_id, chat_id)
                    except AmoError as exc:
                        log.warning("Привязка чата к контакту не удалась (продолжаем): %s", exc)

            client_name = data.name or "Клиент"
            history = await storage.history_full(tg_id, limit=80)
            role_names = {"user": "Клиент", "assistant": "Соня", "manager": "Менеджер"}
            dialog_lines = [
                f"{role_names.get(r['role'], r['role'])}: {r['content']}"
                for r in history if r.get("content")
            ]
            if dialog_lines:
                transcript = "📋 Переписка клиента с Соней:\n\n" + "\n".join(dialog_lines)
                await chat.send_to_amo(tg_id=tg_id, text=transcript,
                                       name=client_name, phone=data.phone or None)
            await chat.send_to_amo(tg_id=tg_id, text=_order_summary(data),
                                   name=client_name, phone=data.phone or None)

            # 4) Ловим лид, который завёл чат, и облагораживаем (имя + цена).
            #    Если новый лид не появился (вернувшийся клиент — чат сел на открытую
            #    сделку) — просто добавляем примечание о новой заявке.
            if contact_id:
                new_lead = await _wait_new_lead(contact_id, leads_before)
                if new_lead:
                    lead_id = new_lead
                    try:
                        await amo.update_lead(
                            lead_id,
                            name=f"Заявка из Telegram: {data.product_name or 'букет'}",
                            price=data.price, product_name=data.product_name,
                            product_url=data.product_url, budget=data.budget,
                            delivery=data.delivery_method,
                        )
                        if data.comment:
                            await amo.add_note(lead_id, data.comment)
                    except AmoError as exc:
                        log.warning("Не удалось облагородить сделку %s: %s", new_lead, exc)
                elif leads_before:
                    lead_id = max(leads_before)
                    await amo.add_note(lead_id, "Новая заявка:\n" + _order_summary(data))
                if lead_id:
                    await storage.update_user(tg_id, amo_lead_id=lead_id)
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
