"""Канал MAX (мессенджер) — тот же мозг, что и у Telegram-бота.

MAX Bot API (проверено вживую на боевом токене):
  • база          https://botapi.max.ru
  • авторизация   заголовок  Authorization: <token>   (без «Bearer», без query)
  • входящие      GET  /updates   — long polling (types=message_created,bot_started)
  • исходящие     POST /messages?user_id=<id>   body {"text": "..."}

Идентификация: внутренний uid = MAX_UID_BASE + max_user_id (обратимо), дальше
uid используется везде как «tg_id» — ровно как отрицательный uid у веб-виджета.
Ответы флориста возвращаются сюда из amoJo-вебхука (app/crm/webhook.py) по
user.channel == "max".
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from app.bot.texts import FALLBACK_ERROR, GREETING
from app.config import settings
from app.db.storage import (
    STATE_CONSULT,
    STATE_HANDOFF,
    max_uid,
    max_user_id_from_uid,
    storage,
)
from app.llm import consultant
from app.services import handoff

log = logging.getLogger("sonya.max")

# MAX режет текст сообщения на 4000 символов — держим запас (у Сони ответы короткие)
_MAX_TEXT_LEN = 4000
_NON_TEXT_HINT = "Напишите, пожалуйста, текстом — что хотите подобрать? 🌷"


class MaxBot:
    """Лёгкий клиент MAX Bot API: long polling + отправка сообщений."""

    def __init__(self, token: str, api_url: str) -> None:
        self._token = token
        self._api = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        # сериализация обработки по пользователю (как _locks в Telegram-хэндлерах)
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, uid: int) -> asyncio.Lock:
        lock = self._locks.get(uid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[uid] = lock
        return lock

    async def _ensure_session(self) -> aiohttp.ClientSession:
        # ClientSession создаём лениво — уже внутри работающего event loop
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": self._token}
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- исходящие ----------
    async def send_message(self, uid: int, text: str) -> None:
        """Отправить текст пользователю MAX (uid — внутренний, положительный ≥ base)."""
        if not text:
            return
        if len(text) > _MAX_TEXT_LEN:
            text = text[:_MAX_TEXT_LEN]
        user_id = max_user_id_from_uid(uid)
        session = await self._ensure_session()
        try:
            async with session.post(
                f"{self._api}/messages",
                params={"user_id": user_id},
                json={"text": text},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    log.warning("MAX send -> %s: %s", resp.status, body)
        except Exception as exc:  # noqa: BLE001
            log.exception("MAX: не удалось отправить сообщение uid=%s: %s", uid, exc)

    # ---------- обработка входящих ----------
    async def _greet(self, uid: int) -> None:
        """Новый диалог (bot_started или /start): метка сессии + приветствие.
        Аналог on_start в Telegram и первого /web/start у веб-виджета."""
        await storage.get_or_create_user(uid, channel="max")
        await storage.update_user(uid, state=STATE_CONSULT)
        await storage.mark_session_start(uid)
        await storage.add_message(uid, "assistant", GREETING)
        await self.send_message(uid, GREETING)

    async def _on_text(self, uid: int, text: str) -> None:
        async with self._lock_for(uid):
            user = await storage.get_or_create_user(uid, channel="max")

            # первый контакт без bot_started (например, поллинг стартовал позже) —
            # один раз здороваемся, чтобы LLM не приветствовал сам посреди ответа
            if not user.context_since:
                await self._greet(uid)
                user = await storage.get_or_create_user(uid, channel="max")

            # режим живого чата с флористом — Соня молчит, пересылаем менеджеру
            if user.state == STATE_HANDOFF:
                await handoff.forward_client_message(uid, text)
                return

            try:
                result = await consultant.generate(uid, text)
            except Exception as exc:  # noqa: BLE001
                log.exception("MAX: ошибка генерации ответа: %s", exc)
                await self.send_message(uid, FALLBACK_ERROR)
                return

            # порядок как в Telegram/web: вопрос -> ответ
            await storage.add_message(uid, "user", text)

            if result.handoff is not None:
                reply = await handoff.do_handoff(uid, result.handoff)
                await storage.add_message(uid, "assistant", reply)
                await self.send_message(uid, reply)
                return

            reply = result.text or FALLBACK_ERROR
            await storage.add_message(uid, "assistant", reply)
            await self.send_message(uid, reply)

    async def _dispatch(self, upd: dict) -> None:
        kind = upd.get("update_type")

        if kind == "bot_started":
            # у bot_started user_id лежит в объекте user (chat_id — это id
            # диалога, другое пространство; по нему слать как по user_id нельзя).
            # Если id не нашли — не страшно: первый текст всё равно поздоровается.
            u = upd.get("user") or {}
            uid = self._uid_from(u.get("user_id") or upd.get("user_id"))
            if uid is not None:
                await self._greet(uid)
            return

        if kind != "message_created":
            return  # редактирование/удаление/служебные — не трогаем

        msg = upd.get("message") or {}
        sender = msg.get("sender") or {}
        if sender.get("is_bot"):
            return  # эхо собственных сообщений
        uid = self._uid_from(sender.get("user_id"))
        if uid is None:
            return

        body = msg.get("body") or {}
        text = (body.get("text") or "").strip()

        if text == "/start":
            await self._greet(uid)
            return
        if not text:
            await self.send_message(uid, _NON_TEXT_HINT)
            return

        await self._on_text(uid, text)

    @staticmethod
    def _uid_from(max_user_id) -> int | None:
        if max_user_id is None:
            return None
        try:
            return max_uid(int(max_user_id))
        except (TypeError, ValueError):
            return None

    # ---------- long polling ----------
    async def _drain_pending(self) -> int | None:
        """Сбросить накопленные апдейты и вернуть свежий marker — чтобы после
        рестарта не переотвечать на старые сообщения (аналог drop_pending_updates)."""
        session = await self._ensure_session()
        try:
            async with session.get(
                f"{self._api}/updates",
                params={"timeout": 0, "limit": 1000},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                return data.get("marker")
        except Exception as exc:  # noqa: BLE001
            log.warning("MAX: не удалось сбросить старые апдейты: %s", exc)
            return None

    async def run_polling(self) -> None:
        """Основной цикл long polling. Живёт до отмены задачи."""
        marker = await self._drain_pending()
        log.info("MAX long polling запущен (marker=%s)", marker)
        while True:
            try:
                session = await self._ensure_session()
                params: dict = {
                    "timeout": 90,
                    "types": "message_created,bot_started",
                }
                if marker is not None:
                    params["marker"] = marker
                async with session.get(
                    f"{self._api}/updates",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning("MAX /updates -> %s: %s", resp.status, body)
                        await asyncio.sleep(5)
                        continue
                    data = await resp.json()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("MAX polling: сетевая ошибка (%s) — повтор через 5с", exc)
                await asyncio.sleep(5)
                continue

            for upd in data.get("updates", []):
                try:
                    await self._dispatch(upd)
                except Exception as exc:  # noqa: BLE001
                    log.exception("MAX: ошибка обработки апдейта: %s", exc)

            # marker из ответа — точка, с которой запрашивать дальше
            marker = data.get("marker", marker)


# Синглтон канала (как storage). Сессия поднимется лениво при первом обращении.
max_bot = MaxBot(settings.max_bot_token, settings.max_api_url)
