"""Хранилище медиа-файлов (фото/документы из чатов) + раздача по /media/<имя>.

Файлы клиента (Telegram/веб/MAX) сохраняем на диск в settings.media_dir и отдаём
публично по неугадываемому имени — этот URL забирает amoCRM (amoJo), Telegram
(send_photo по ссылке) и браузер (виджет/админка). Имя генерим сами
(secrets.token_urlsafe) — из клиентского ввода имя НЕ строим (анти-traversal).
"""
from __future__ import annotations

import logging
import os
import re
import secrets

import aiohttp
from aiohttp import web

from app.config import settings

log = logging.getLogger(__name__)

# Разрешённые расширения. Картинки рендерим как <img>, остальное — как файл-ссылку.
_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "bmp"}
_FILE_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "txt", "mp4", "mov", "webm", "zip"}
_ALLOWED_EXT = _IMAGE_EXT | _FILE_EXT

# content-type -> расширение (для входящих без имени файла)
_CT_EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/gif": "gif", "image/heic": "heic", "image/heif": "heif",
    "image/bmp": "bmp", "application/pdf": "pdf", "video/mp4": "mp4",
    "video/quicktime": "mov", "text/plain": "txt",
}

# Имя файла в /media/<name>: только безопасные символы + расширение.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}\.[A-Za-z0-9]{1,8}$")


def media_dir() -> str:
    d = settings.media_dir or "data/media"
    os.makedirs(d, exist_ok=True)
    return d


def is_image(name_or_ct: str | None) -> bool:
    if not name_or_ct:
        return False
    s = name_or_ct.lower()
    if s.startswith("image/"):
        return True
    ext = s.rsplit(".", 1)[-1]
    return ext in _IMAGE_EXT


def _clean_ext(ext: str | None, content_type: str | None, file_name: str | None) -> str:
    """Определить безопасное расширение: из ext -> имени файла -> content-type -> bin."""
    cand = (ext or "").lower().lstrip(".")
    if not cand and file_name and "." in file_name:
        cand = file_name.rsplit(".", 1)[-1].lower()
    if not cand and content_type:
        cand = _CT_EXT.get(content_type.split(";")[0].strip().lower(), "")
    cand = re.sub(r"[^a-z0-9]", "", cand)[:8]
    if cand not in _ALLOWED_EXT:
        # неизвестное/пустое — кладём как .bin, отдадим как файл (не картинку)
        cand = cand if cand and cand.isalnum() else "bin"
    return cand


def public_url(filename: str) -> str:
    base = (settings.widget_public_url or "").rstrip("/")
    return f"{base}/media/{filename}"


def save_bytes(data: bytes, *, ext: str | None = None,
               content_type: str | None = None, file_name: str | None = None) -> tuple[str, str]:
    """Сохранить байты на диск под неугадываемым именем. Возвращает (filename, public_url)."""
    safe_ext = _clean_ext(ext, content_type, file_name)
    name = f"{secrets.token_urlsafe(16)}.{safe_ext}"
    path = os.path.join(media_dir(), name)
    with open(path, "wb") as f:
        f.write(data)
    return name, public_url(name)


async def save_from_url(url: str, *, headers: dict | None = None,
                        max_bytes: int | None = None) -> tuple[str, str, int, str | None] | None:
    """Скачать файл по URL (со стрим-лимитом) и сохранить у нас.

    Возвращает (filename, public_url, file_size, content_type) или None при ошибке/
    превышении лимита. Нужен для MAX (перехост вложения) и, при желании, для
    долгого хранения медиа менеджера."""
    cap = max_bytes or settings.media_max_bytes
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers or {},
                             timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status != 200:
                    log.warning("save_from_url: %s -> HTTP %s", url, r.status)
                    return None
                ct = r.headers.get("Content-Type")
                buf = bytearray()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    buf += chunk
                    if len(buf) > cap:
                        log.warning("save_from_url: файл больше лимита (%s байт), обрезаю", cap)
                        return None
        name, purl = save_bytes(bytes(buf), content_type=ct,
                                file_name=url.split("/")[-1].split("?")[0])
        return name, purl, len(buf), ct
    except Exception as exc:  # noqa: BLE001
        log.warning("save_from_url: не удалось скачать %s: %s", url, exc)
        return None


async def serve_media(request: web.Request) -> web.StreamResponse:
    """GET /media/<name> — отдать файл. Доступ по неугадываемому имени, без пароля."""
    name = request.match_info.get("name", "")
    if not _NAME_RE.match(name):
        return web.Response(status=404, text="not found")
    root = os.path.realpath(media_dir())
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return web.Response(status=404, text="not found")
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})
