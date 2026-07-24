"""UTM-источники Telegram-бота: deeplink `/start <utm>` -> метка для amoCRM.

Таблица меток — из базы знаний «UTM_метки_и_теги … База цветов» (лист
UTM-TG-BOT_Челны, бот @Cvetychelny_bot). Значение метки кладётся на сделку
как ТЕГ (и, если настроено поле «Источник трафика» AMO_CF_SOURCE, ещё и в
это поле), чтобы аналитика считала обращения в разбивке по каналам.

Клиент открывает бота ссылкой https://t.me/Cvetychelny_bot?start=<utm> —
Telegram передаёт <utm> как payload команды /start, мы его и разбираем.
"""
from __future__ import annotations

# utm-payload (из ссылки ?start=...) -> «поле источник трафика» из Excel
SOURCE_LABELS: dict[str, str] = {
    "vk_senler": "TG_bot_ВК senler",
    "tg_kanal_post": "TG_bot_Телеграм канал",
    "instagram": "TG_bot_Сторис инста",
    "ya_map": "TG_bot_Яндекс Карты",
    "tilda": "TG_bot_WP_сайт",
    "yabiz": "TG_bot_ЯндексБизнес",
    "tgraffle": "TG_bot_розыгрыш",
    "2gis": "TG_bot_Прямой вход/2 гис",
    "posev": "TG_bot_Посев общий",
    "2gis_storis": "TG_bot_2ГИС Сторис",
    "2gis_cvety": "TG_bot_2ГИС рубрика цветы",
    "2gis_dostavka_cvety": "TG_bot_2ГИС рубрика доставка цветов",
    "2gis_igruska": "TG_bot_2ГИС рубрика игрушка",
    "2gis_towari": "TG_bot_2ГИС рубрика товары",
    "2gis_suveniri": "TG_bot_2ГИС рубрика сувениры",
    "inst_taplink": "TG_bot_Таплинк инста",
    "yabiz_akzia": "TG_bot_ЯндексБизнес - акции",
    "vk_links": "TG_bot_ВК_Ссылки",
    "vk_opisanie": "TG_bot_ВК_Описание",
    "vk_story": "TG_bot_ВК_Сторис",
    "vk_azalia": "TG_bot_ВК_Пост",
    "tgflowerpodpiska": "TG_bot_каналтг - пост цветочная подписка",
    "vk_menu": "TG_bot_ВК_МЕНЮ",
    "google_map": "TG_bot_Гугл карта",
}

# Прямой вход / переход без метки (пустой /start) — так в Excel помечен «2 гис».
DEFAULT_SOURCE = "TG_bot_Прямой вход/2 гис"


def normalize(payload: str | None) -> str:
    """Очистить payload из ссылки (регистр, пробелы, служебные префиксы Telegram)."""
    p = (payload or "").strip().lower()
    # Telegram допускает только [A-Za-z0-9_-]; на всякий случай отрезаем мусор
    return p


def resolve_source(payload: str | None) -> str:
    """utm-payload -> метка для amoCRM.

    Пусто -> прямой вход (DEFAULT_SOURCE). Известный payload -> метка из таблицы.
    Неизвестный (новая кампания, метку ещё не завели) -> `TG_bot_<payload>`,
    чтобы обращение всё равно попало в аналитику, а не потерялось.
    """
    p = normalize(payload)
    if not p:
        return DEFAULT_SOURCE
    label = SOURCE_LABELS.get(p)
    if label:
        return label
    return f"TG_bot_{payload.strip()}"


def admin_label(payload: str | None, custom: dict[str, str] | None = None) -> str | None:
    """Человекочитаемая подпись UTM-источника для админки.

    В отличие от resolve_source (метка для amoCRM с префиксом TG_bot_), здесь
    короткая подпись «для глаз»:
      None            -> None  (клиент не из TG-deeplink: веб/MAX или заведён
                                 до появления UTM-меток — «не размечен»);
      пустой payload  -> «Прямой вход» (клиент нажал /start без метки);
      custom[payload] -> имя кампании, заведённой владельцем в админке (важнее
                         справочника — владелец мог переименовать источник);
      известная метка -> из справочника без служебного префикса («ВК senler»);
      новая кампания  -> сам payload (метку ни в админке, ни в справочнике не завели).
    """
    if payload is None:
        return None
    p = normalize(payload)
    if not p:
        return "Прямой вход"
    if custom and p in custom:
        return custom[p]
    label = SOURCE_LABELS.get(p)
    if label:
        return label.replace("TG_bot_", "")
    return p
