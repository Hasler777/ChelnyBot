"""Интеграция с amoCRM (REST API v4): OAuth + создание контакта и сделки.

Токены хранятся в JSON-файле рядом с БД и автоматически обновляются по
refresh_token. Первичная авторизация — через одноразовый код (AMO_AUTH_CODE)
или заранее выданные токены в .env.
"""
from __future__ import annotations

import json
import logging
import os
import time

import aiohttp

from app.config import settings

log = logging.getLogger(__name__)

_TOKENS_PATH = os.path.join(os.path.dirname(settings.db_path) or ".", "amo_tokens.json")


class AmoError(Exception):
    pass


class AmoClient:
    def __init__(self) -> None:
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at: float = 0.0
        self._load_tokens()

    # ---------- токены ----------
    def _load_tokens(self) -> None:
        if os.path.exists(_TOKENS_PATH):
            try:
                with open(_TOKENS_PATH, encoding="utf-8") as f:
                    data = json.load(f)
                self._access = data.get("access_token")
                self._refresh = data.get("refresh_token")
                self._expires_at = data.get("expires_at", 0.0)
                return
            except (OSError, json.JSONDecodeError):
                log.warning("Не удалось прочитать %s", _TOKENS_PATH)
        # фолбэк на .env
        self._access = settings.amo_access_token or None
        self._refresh = settings.amo_refresh_token or None
        self._expires_at = 0.0

    def _save_tokens(self) -> None:
        os.makedirs(os.path.dirname(_TOKENS_PATH) or ".", exist_ok=True)
        with open(_TOKENS_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "access_token": self._access,
                    "refresh_token": self._refresh,
                    "expires_at": self._expires_at,
                },
                f,
            )

    async def _exchange(self, payload: dict) -> None:
        url = f"{settings.amo_base_url.rstrip('/')}/oauth2/access_token"
        body = {
            "client_id": settings.amo_client_id,
            "client_secret": settings.amo_client_secret,
            "redirect_uri": settings.amo_redirect_uri,
            **payload,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise AmoError(f"OAuth {resp.status}: {data}")
        self._access = data["access_token"]
        self._refresh = data["refresh_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600)) - 120
        self._save_tokens()
        log.info("amoCRM токены обновлены")

    async def authorize_with_code(self, code: str) -> None:
        await self._exchange({"grant_type": "authorization_code", "code": code})

    async def _refresh_tokens(self) -> None:
        if not self._refresh:
            raise AmoError("Нет refresh_token — нужна первичная авторизация (AMO_AUTH_CODE)")
        await self._exchange({"grant_type": "refresh_token", "refresh_token": self._refresh})

    async def _ensure_token(self) -> None:
        if not self._access:
            if settings.amo_auth_code:
                await self.authorize_with_code(settings.amo_auth_code)
            else:
                await self._refresh_tokens()
        elif self._expires_at and time.time() >= self._expires_at:
            await self._refresh_tokens()

    # ---------- запросы ----------
    async def _request(self, method: str, path: str, *, json_body: dict | list | None = None,
                       params: dict | None = None, _retry: bool = True) -> dict:
        await self._ensure_token()
        url = f"{settings.amo_base_url.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {self._access}"}
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url, headers=headers, json=json_body, params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 401 and _retry:
                    await self._refresh_tokens()
                    return await self._request(method, path, json_body=json_body,
                                               params=params, _retry=False)
                text = await resp.text()
                if resp.status >= 300:
                    raise AmoError(f"{method} {path} -> {resp.status}: {text}")
                return json.loads(text) if text else {}

    async def get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    # ---------- бизнес-операции ----------
    async def _create_contact(self, name: str, phone: str) -> int:
        # переиспользуем существующий контакт по телефону — не плодим дубликаты
        # и сохраняем привязку чата у постоянного клиента
        digits = "".join(ch for ch in phone if ch.isdigit()) if phone else ""
        if digits:
            try:
                found = await self._request("GET", "/api/v4/contacts", params={"query": digits})
                contacts = found.get("_embedded", {}).get("contacts", [])
                if contacts:
                    return contacts[0]["id"]
            except AmoError as exc:
                log.warning("Поиск контакта по телефону не удался: %s", exc)
        cf_values = []
        if settings.amo_cf_contact_phone and phone:
            cf_values.append({
                "field_id": settings.amo_cf_contact_phone,
                "values": [{"value": phone, "enum_code": "WORK"}],
            })
        body = [{"name": name or "Клиент из Telegram", "custom_fields_values": cf_values or None}]
        data = await self._request("POST", "/api/v4/contacts", json_body=body)
        return data["_embedded"]["contacts"][0]["id"]

    def _lead_custom_fields(self, *, product_name: str, product_url: str, price: float,
                            budget: str, delivery: str) -> list[dict]:
        fields = []

        def add(field_id, value):
            if field_id and value not in (None, "", 0):
                fields.append({"field_id": field_id, "values": [{"value": value}]})

        add(settings.amo_cf_product, product_name)
        add(settings.amo_cf_product_url, product_url)
        add(settings.amo_cf_price, str(int(price)) if price else "")
        add(settings.amo_cf_budget, budget)
        add(settings.amo_cf_delivery, delivery)
        add(settings.amo_cf_source, "Telegram-бот Соня")
        return fields

    async def create_lead(self, *, name: str, phone: str, product_name: str,
                          product_url: str, price: float, budget: str,
                          delivery: str, comment: str,
                          contact_id: int | None = None) -> tuple[int, int]:
        """Создаёт сделку, возвращает (lead_id, contact_id).

        Если contact_id передан — переиспользуем его (один tg = один контакт),
        иначе ищем по телефону / создаём новый.
        """
        if not contact_id:
            contact_id = await self._create_contact(name, phone)

        lead_name = f"Заявка из Telegram: {product_name or 'букет'}"
        lead: dict = {
            "name": lead_name,
            "price": int(price) if price else 0,
            "_embedded": {"contacts": [{"id": contact_id}]},
            "custom_fields_values": self._lead_custom_fields(
                product_name=product_name, product_url=product_url, price=price,
                budget=budget, delivery=delivery,
            ) or None,
        }
        if settings.amo_pipeline_id:
            lead["pipeline_id"] = settings.amo_pipeline_id
        if settings.amo_status_id:
            lead["status_id"] = settings.amo_status_id

        data = await self._request("POST", "/api/v4/leads", json_body=[lead])
        lead_id = data["_embedded"]["leads"][0]["id"]

        if comment:
            await self._add_note(lead_id, comment)
        return lead_id, contact_id

    async def link_chat_to_contact(self, contact_id: int, chat_id: str) -> None:
        """Привязывает чат amoJo к контакту, чтобы входящее сообщение не плодило
        отдельную «неразобранную» сделку."""
        body = [{"contact_id": contact_id, "chat_id": chat_id}]
        try:
            await self._request("POST", "/api/v4/contacts/chats", json_body=body)
        except AmoError as exc:
            # чат уже привязан к этому/другому контакту (постоянный клиент) — не критично
            if "AlreadyExists" in str(exc):
                log.info("Чат уже привязан к контакту — пропускаем")
                return
            raise

    async def _add_note(self, lead_id: int, text: str) -> None:
        body = [{"note_type": "common", "params": {"text": text}}]
        try:
            await self._request("POST", f"/api/v4/leads/{lead_id}/notes", json_body=body)
        except AmoError as exc:
            log.warning("Не удалось добавить примечание к сделке %s: %s", lead_id, exc)

    async def add_note(self, lead_id: int, text: str) -> None:
        """Публичная обёртка для добавления примечания к сделке."""
        await self._add_note(lead_id, text)


amo = AmoClient()
