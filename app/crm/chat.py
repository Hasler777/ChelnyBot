"""amoCRM Chat API (amoJo) — двусторонний чат как канал.

Документация: каждый запрос подписывается по схеме amoJo:
  Content-MD5 = md5(body) hex lower
  X-Signature = hmac_sha1( "METHOD\\nContent-MD5\\nContent-Type\\nDate\\nPATH",
                            key=channel_secret ) hex lower
Ключ подписи — секрет канала чата (AMOJO_CHANNEL_SECRET).

Функции:
  connect_channel()  — разово подключить канал к аккаунту, получить scope_id
  send_to_amo(...)   — отправить входящее сообщение клиента в amoCRM
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from email.utils import formatdate

import aiohttp

from app.config import settings

log = logging.getLogger(__name__)

_CONTENT_TYPE = "application/json"


def _md5_hex(body: bytes) -> str:
    return hashlib.md5(body).hexdigest()


def _signature(method: str, path: str, content_md5: str, date: str) -> str:
    string_to_sign = "\n".join([method.upper(), content_md5, _CONTENT_TYPE, date, path])
    return hmac.new(
        settings.amojo_channel_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()


def _headers(method: str, path: str, body: bytes) -> dict:
    date = formatdate(timeval=None, localtime=False, usegmt=True)
    content_md5 = _md5_hex(body)
    return {
        "Date": date,
        "Content-Type": _CONTENT_TYPE,
        "Content-MD5": content_md5,
        "X-Signature": _signature(method, path, content_md5, date),
    }


async def _post(path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    url = f"{settings.amojo_base_url.rstrip('/')}{path}"
    headers = _headers("POST", path, body)
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=body, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=30)) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise RuntimeError(f"amoJo POST {path} -> {resp.status}: {text}")
            return json.loads(text) if text else {}


async def connect_channel(amojo_account_id: str, title: str = "Соня — Telegram") -> dict:
    """Подключить канал к аккаунту. Возвращает ответ со scope_id.

    amojo_account_id — это amojo_id аккаунта (GET /api/v4/account?with=amojo_id).
    """
    path = f"/v2/origin/custom/{settings.amojo_channel_id}/connect"
    payload = {
        "account_id": amojo_account_id,
        "title": title,
        "hook_api_version": "v2",
    }
    return await _post(path, payload)


def conversation_id_for(tg_id: int) -> str:
    # web-диалоги идут с отрицательным uid — даём им отдельный префикс,
    # чтобы идентификатор беседы в amoCRM был читаемым и не путался с Telegram.
    if tg_id < 0:
        return f"web-{-tg_id}"
    return f"tg-{tg_id}"


def _user_ref(tg_id: int) -> str:
    """Идентификатор пользователя в amoJo (стабильный per-клиент)."""
    if tg_id < 0:
        return f"web-user-{-tg_id}"
    return f"tg-user-{tg_id}"


async def create_chat(*, tg_id: int, name: str, phone: str | None = None) -> str | None:
    """Создаёт чат в amoJo (до первого сообщения), возвращает amojo chat_id.

    Этот id нужен, чтобы привязать чат к контакту в amoCRM
    (POST /api/v4/contacts/chats) — тогда входящее сообщение не создаст
    отдельную «неразобранную» сделку.
    """
    profile: dict = {}
    if phone:
        profile["phone"] = phone
    payload = {
        "conversation_id": conversation_id_for(tg_id),
        "user": {
            "id": _user_ref(tg_id),
            "name": name or "Клиент",
            "profile": profile,
        },
    }
    path = f"/v2/origin/custom/{settings.amojo_scope_id}/chats"
    resp = await _post(path, payload)
    return resp.get("id") if isinstance(resp, dict) else None


async def send_to_amo(*, tg_id: int, text: str, name: str, phone: str | None = None) -> dict:
    """Отправить сообщение клиента в amoCRM (входящее в чат)."""
    now = time.time()
    profile: dict = {}
    if phone:
        profile["phone"] = phone
    payload = {
        "event_type": "new_message",
        "payload": {
            "timestamp": int(now),
            "msec_timestamp": int(now * 1000),
            "msgid": uuid.uuid4().hex,
            "conversation_id": conversation_id_for(tg_id),
            "sender": {
                "id": _user_ref(tg_id),
                "name": name or "Клиент",
                "profile": profile,
            },
            "message": {"type": "text", "text": text},
            "silent": False,
        },
    }
    path = f"/v2/origin/custom/{settings.amojo_scope_id}"
    return await _post(path, payload)
