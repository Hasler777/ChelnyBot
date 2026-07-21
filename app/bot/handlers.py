"""Обработчики Telegram (aiogram 3.x)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.bot.texts import FALLBACK_ERROR, GREETING
from app.crm import utm
from app.db.storage import STATE_CONSULT, STATE_HANDOFF, storage
from app.llm import consultant
from app.services import handoff

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


@router.message()
async def on_other(message: Message) -> None:
    """Нетекстовые сообщения (фото/стикеры). В handoff — игнор/пересылка-заглушка."""
    user = await storage.get_or_create_user(message.from_user.id)
    if user.state == STATE_HANDOFF:
        await handoff.forward_client_message(message.from_user.id, "[вложение]")
        return
    await message.answer("Напишите, пожалуйста, текстом — что хотите подобрать? 🌷")
