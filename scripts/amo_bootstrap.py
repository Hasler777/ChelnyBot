"""Разовая настройка amoCRM: получить ID воронок/статусов/полей и подключить
канал чата amoJo.

Запуск:
  python -m scripts.amo_bootstrap            # показать воронки, статусы, поля, amojo_id
  python -m scripts.amo_bootstrap connect    # + подключить канал amoJo (нужны AMOJO_*)

Перед запуском заполните в .env как минимум:
  AMO_BASE_URL, AMO_CLIENT_ID, AMO_CLIENT_SECRET, AMO_REDIRECT_URI,
  и AMO_AUTH_CODE (одноразовый код из интеграции) ИЛИ готовые токены.
Для connect также: AMOJO_CHANNEL_ID, AMOJO_CHANNEL_SECRET.
"""
from __future__ import annotations

import asyncio
import sys

from app.config import settings
from app.crm import chat
from app.crm.amocrm import amo


def _print_header(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


async def show_info() -> str:
    # amojo_id аккаунта (нужен для connect канала)
    account = await amo.get("/api/v4/account", params={"with": "amojo_id"})
    amojo_id = account.get("amojo_id", "")

    _print_header("АККАУНТ")
    print(f"name      : {account.get('name')}")
    print(f"amojo_id  : {amojo_id}   <- AMOJO_ACCOUNT_ID")

    _print_header("ВОРОНКИ И СТАТУСЫ (AMO_PIPELINE_ID / AMO_STATUS_ID)")
    pipelines = await amo.get("/api/v4/leads/pipelines")
    for p in pipelines.get("_embedded", {}).get("pipelines", []):
        print(f"\nВоронка: {p['name']}  (pipeline_id = {p['id']})")
        for s in p.get("_embedded", {}).get("statuses", []):
            print(f"   - {s['name']:<25} status_id = {s['id']}")

    _print_header("ПОЛЯ СДЕЛКИ (AMO_CF_*)")
    lead_fields = await amo.get("/api/v4/leads/custom_fields")
    for f in lead_fields.get("_embedded", {}).get("custom_fields", []):
        print(f"   id={f['id']:<10} type={f['type']:<12} {f['name']}")

    _print_header("ПОЛЯ КОНТАКТА (AMO_CF_CONTACT_PHONE — поле с типом 'phone')")
    contact_fields = await amo.get("/api/v4/contacts/custom_fields")
    for f in contact_fields.get("_embedded", {}).get("custom_fields", []):
        mark = "  <- телефон" if f.get("type") == "phone" or f.get("code") == "PHONE" else ""
        print(f"   id={f['id']:<10} type={f['type']:<12} {f['name']}{mark}")

    return amojo_id


async def main() -> None:
    do_connect = len(sys.argv) > 1 and sys.argv[1] == "connect"

    amojo_id = await show_info()

    if do_connect:
        _print_header("ПОДКЛЮЧЕНИЕ КАНАЛА amoJo")
        if not (settings.amojo_channel_id and settings.amojo_channel_secret):
            print("Заполните AMOJO_CHANNEL_ID и AMOJO_CHANNEL_SECRET в .env (из кабинета интеграций amo).")
            return
        if not amojo_id:
            print("Не получили amojo_id аккаунта — проверьте права интеграции.")
            return
        resp = await chat.connect_channel(amojo_id)
        print("Ответ connect:", resp)
        scope_id = resp.get("scope_id")
        print(f"\nscope_id = {scope_id}   <- AMOJO_SCOPE_ID")
        print("Сохраните AMOJO_SCOPE_ID и AMOJO_ACCOUNT_ID в .env.")


if __name__ == "__main__":
    asyncio.run(main())
