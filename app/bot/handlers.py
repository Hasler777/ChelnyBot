"""Обработчики Telegram (aiogram 3.x)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.bot.texts import FALLBACK_ERROR, GREETING
from app.config import settings
from app.crm import utm
from app.db.storage import STATE_CONSULT, STATE_HANDOFF, storage
from app.llm import consultant
from app.services import handoff, media

log = logging.getLogger(__name__)
router = Router()

# Сериализация обработки сообщений по пользователю: быстрые сообщения подряд
# («8 990...», «але») обрабатываются строго по очереди, без гонок в истории.
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(tg_id: int) -> asyncio.Lock:
    lock = _locks.get(tg_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[tg_id] = lock
    return lock


@router.message(CommandStart())
async def on_start(message: Message, command: CommandObject) -> None:
    tg_id = message.from_user.id
    await storage.get_or_create_user(tg_id)
    # Метка канала из deeplink: ссылка вида ?start=vk_senler -> payload "vk_senler".
    # Пишем источник, только если пришёл непустой payload (не затираем реальную
    # кампанию пустым /start от того же клиента при повторном заходе).
    payload = (command.args or "").strip()
    if payload:
        await storage.update_user(tg_id, utm_source=utm.normalize(payload))
    else:
        user = await storage.get_user(tg_id)
        if not (user and user.utm_source):
            await storage.update_user(tg_id, utm_source="")  # прямой вход
    # новый старт — возвращаем диалог боту и ставим метку новой сессии: бот будет
    # брать в контекст только сообщения после неё (свежий контекст, без устаревших
    # товаров). Переписку и расход НЕ удаляем — всё хранится для админки.
    await storage.update_user(tg_id, state=STATE_CONSULT)
    await storage.mark_session_start(tg_id)
    await message.answer(GREETING)
    # фиксируем приветствие в истории, чтобы LLM не здоровался повторно
    await storage.add_message(tg_id, "assistant", GREETING)


@router.message(F.text)
async def on_text(message: Message) -> None:
    tg_id = message.from_user.id
    text = message.text or ""

    async with _lock_for(tg_id):
        user = await storage.get_or_create_user(tg_id)

        # режим живого чата с флористом — бот молчит, пересылаем менеджеру
        if user.state == STATE_HANDOFF:
            await handoff.forward_client_message(tg_id, text)
            return

        try:
            result = await consultant.generate(tg_id, text)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка генерации ответа: %s", exc)
            await message.answer(FALLBACK_ERROR)
            return

        # сохраняем ход диалога после генерации (порядок: вопрос -> ответ)
        await storage.add_message(tg_id, "user", text)

        if result.handoff is not None:
            reply = await handoff.do_handoff(tg_id, result.handoff)
            await storage.add_message(tg_id, "assistant", reply)
            await message.answer(reply)
            return

        reply = result.text or FALLBACK_ERROR
        await storage.add_message(tg_id, "assistant", reply)
        await message.answer(reply, disable_web_page_preview=False)


async def _extract_tg_media(message: Message) -> tuple[bytes, str, str, str | None] | None:
    """Скачать фото/документ из сообщения Telegram.

    Возвращает (data, media_type, file_name, content_type) или None (нечего качать /
    слишком большой файл / ошибка). media_type: 'image' | 'file'."""
    if message.photo:
        ph = message.photo[-1]  # самый крупный размер
        file_id, size, media_type, fname, ctype = ph.file_id, ph.file_size, "image", "photo.jpg", "image/jpeg"
    elif message.document:
        doc = message.document
        ctype = doc.mime_type
        media_type = "image" if (ctype or "").startswith("image/") else "file"
        file_id, size, fname = doc.file_id, doc.file_size, doc.file_name or "file"
    else:
        return None  # видео/стикер/голос — не поддерживаем (пока)
    if size and size > settings.media_max_bytes:
        return None
    try:
        buf = await message.bot.download(file_id)
        data = buf.read()
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось скачать вложение Telegram: %s", exc)
        return None
    return data, media_type, fname, ctype


@router.message()
async def on_other(message: Message) -> None:
    """Нетекстовые сообщения (фото/файлы). В handoff — пересылаем медиа флористу."""
    tg_id = message.from_user.id
    user = await storage.get_or_create_user(tg_id)
    if user.state != STATE_HANDOFF:
        await message.answer("Напишите, пожалуйста, текстом — что хотите подобрать? 🌷")
        return

    got = await _extract_tg_media(message)
    if not got:
        # не смогли получить файл (большой/неподдерживаемый) — хотя бы отметим текстом
        await handoff.forward_client_message(tg_id, "[вложение]")
        return
    data, media_type, fname, ctype = got
    _, purl = media.save_bytes(data, content_type=ctype, file_name=fname)
    caption = (message.caption or "").strip()
    content = caption or ("📷 фото" if media_type == "image" else f"📎 файл: {fname}")
    await handoff.forward_client_message(
        tg_id, content, media_url=purl, media_type=media_type,
        file_name=fname, file_size=len(data),
    )
