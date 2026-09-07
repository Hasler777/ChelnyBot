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


def channel_bucket(channel: str | None) -> str:
    """Канал клиента -> «корзина» UTM: 'max' у MAX-бота, 'tg' у всех остальных
    (Telegram и веб-виджет пользуются тем же ботом/справочником)."""
    return "max" if (channel or "").lower() == "max" else "tg"


def _prefix(channel: str | None) -> str:
    """Префикс метки amoCRM по каналу: MAX-бот -> MAX_bot_, иначе -> TG_bot_."""
    return "MAX_bot_" if channel_bucket(channel) == "max" else "TG_bot_"


def normalize(payload: str | None) -> str:
    """Очистить payload из ссылки (регистр, пробелы, служебные префиксы Telegram)."""
    p = (payload or "").strip().lower()
    # Telegram/MAX допускают только [A-Za-z0-9_-]; на всякий случай отрезаем мусор
    return p


def resolve_source(
    payload: str | None, channel: str = "tg",
    campaigns: dict[str, dict[str, str]] | None = None,
) -> str:
    """utm-payload -> метка для amoCRM (тег на сделке) с учётом канала.

    Один и тот же код метки (напр. vk_senler) у Telegram и у MAX даёт разные
    метки: `TG_bot_ВК senler` и `MAX_bot_ВК senler`.

    Приоритет источника подписи:
      1) кампания, заведённая владельцем в админке (`campaigns` из
         storage.utm_labels_map(), вложенный {payload:{tg|max:label}}) — так тег
         совпадёт с названием кампании (2ГИС/Яндекс и пр.), а не будет «сырым»;
      2) статический справочник SOURCE_LABELS (хранит TG-вариант, префикс канала
         подменяем);
      3) неизвестный payload -> `<prefix><payload>`, чтобы обращение не потерялось.
    Пусто -> прямой вход.
    """
    prefix = _prefix(channel)
    p = normalize(payload)
    if not p:
        return DEFAULT_SOURCE.replace("TG_bot_", prefix)
    if campaigns:
        lab = (campaigns.get(p) or {}).get(channel_bucket(channel))
        if lab:
            # если владелец уже задал имя с префиксом канала — оставляем как есть
            return lab if lab.startswith(("TG_bot_", "MAX_bot_")) else f"{prefix}{lab}"
    label = SOURCE_LABELS.get(p)
    if label:
        return label.replace("TG_bot_", prefix)
    return f"{prefix}{payload.strip()}"


def admin_label(
    payload: str | None,
    channel: str | None = "tg",
    custom: dict[str, dict[str, str]] | None = None,
) -> str | None:
    """Человекочитаемая подпись UTM-источника для админки (без служебного префикса).

      None            -> None  (клиент не размечен — заведён до появления меток);
      пустой payload  -> «Прямой вход» (клиент нажал /start без метки);
      custom[payload][bucket] -> имя кампании из админки для нужного канала
                         (важнее справочника — владелец мог переименовать источник);
      известная метка -> из справочника без префикса («ВК senler»);
      новая кампания  -> сам payload.

    `custom` — вложенный словарь {payload: {'tg'|'max': label}} из
    storage.utm_labels_map(); канал выбирает нужную подпись.
    """
    if payload is None:
        return None
    p = normalize(payload)
    if not p:
        return "Прямой вход"
    bucket = channel_bucket(channel)
    if custom and p in custom:
        # Только подпись СВОЕГО канала: не подставляем MAX-имя TG-клиенту и наоборот
        # (у одного кода метки бывают разные кампании в TG и MAX).
        lab = custom[p].get(bucket)
        if lab:
            return lab
    label = SOURCE_LABELS.get(p)
    if label:
        return label.replace("TG_bot_", "").replace("MAX_bot_", "")
    return p
