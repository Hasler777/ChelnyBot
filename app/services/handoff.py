"""Передача диалога флористу: создание сделки в amoCRM + перевод в режим чата."""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.crm import chat, utm
from app.crm.amocrm import AmoError, amo
from app.db.storage import STATE_HANDOFF, storage
from app.llm.consultant import HandoffData

log = logging.getLogger(__name__)

HANDOFF_MESSAGE = "Передаю флористу — он сейчас подключится 🌸"

# держим ссылки на фоновые задачи проставления источника, чтобы их не собрал GC
_source_tasks: set[asyncio.Task] = set()


def _tag_source_async(contact_id: int, source_label: str,
                      name: str = "", phone: str = "") -> None:
    """Фоново проставить источник на сделку.

    В режиме amoJo сделку создаёт чат на СВОЁМ контакте (не на нашем REST-контакте),
    поэтому ищем её среди свежих сделок аккаунта по имени/телефону клиента. Ставим
    тег ДВАЖДЫ с паузой: чат amoJo может дозавести карточку уже после первой
    простановки и перетереть теги; повтор читает текущие теги и домёрживает наш
    источник (идемпотентно)."""
    async def _run() -> None:
        lead_id = await amo.find_recent_lead_for_client(name=name, phone=phone)
        if not lead_id:
            # запасной путь — вдруг сделка всё же на нашем контакте
            lead_id = await amo.find_latest_lead_for_contact(contact_id, attempts=3, delay=2.0)
        if not lead_id:
            log.warning("Сделка клиента (%s / %s) не найдена — источник «%s» не проставлен",
                        name or "—", phone or "—", source_label)
            return
        await amo.apply_source_to_lead(contact_id, source_label, lead_id=lead_id)
        await asyncio.sleep(25)
        await amo.apply_source_to_lead(contact_id, source_label, lead_id=lead_id)

    task = asyncio.create_task(_run())
    _source_tasks.add(task)
    task.add_done_callback(_source_tasks.discard)


async def do_handoff(tg_id: int, data: HandoffData) -> str:
    """Создаёт сделку, открывает чат менеджеру, переводит пользователя в handoff.

    Возвращает текст, который бот отправит клиенту.
    """
    # сохраняем контактные данные пользователя
    await storage.update_user(tg_id, name=data.name or None, phone=data.phone or None)

    # один Telegram-пользователь = один контакт в amoCRM
    user = await storage.get_user(tg_id)
    existing_contact_id = user.amo_contact_id if user else None
    # метка канала (UTM из deeplink /start) для аналитики — с учётом канала
    # клиента: MAX-бот даёт префикс MAX_bot_, Telegram/веб — TG_bot_.
    source_label = utm.resolve_source(
        user.utm_source if user else "", user.channel if user else "tg"
    )

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
                    contact_id=existing_contact_id, source_label=source_label,
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
            dialog_lines = []
            for r in history:
                if not (r.get("content") or r.get("media_url")):
                    continue
                line = f"{role_names.get(r['role'], r['role'])}: {r.get('content') or ''}"
                if r.get("media_url"):  # фото/файл-референс — даём ссылку флористу
                    line += f" {r['media_url']}"
                dialog_lines.append(line.rstrip())
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
            # Сделку чат создаёт асинхронно по входящему сообщению — фоново
            # находим её и вешаем тег/поле источника (не задерживаем ответ клиенту).
            if contact_id:
                _tag_source_async(contact_id, source_label,
                                  name=data.name or "", phone=data.phone or "")
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось открыть чат в amoJo: %s", exc)

    return HANDOFF_MESSAGE


async def forward_client_message(tg_id: int, text: str, *, media_url: str | None = None,
                                 media_type: str | None = None, file_name: str | None = None,
                                 file_size: int | None = None) -> None:
    """В режиме handoff — сохранить сообщение клиента (его покажет виджет в карточке)
    и, если настроен нативный чат amoJo, продублировать туда. Поддерживает медиа
    (фото/файл): media_url — публичная ссылка на файл (наш /media/…)."""
    await storage.add_message(tg_id, "user", text, media_url=media_url,
                              media_type=media_type, media_name=file_name)

    if settings.amojo_enabled:
        user = await storage.get_user(tg_id)
        name = (user.name if user else None) or "Клиент"
        phone = user.phone if user else None
        try:
            await chat.send_to_amo(tg_id=tg_id, text=text, name=name, phone=phone,
                                   media_url=media_url, media_type=media_type,
                                   file_name=file_name, file_size=file_size)
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось переслать сообщение клиента в amoJo: %s", exc)
