"""Клиент каталога WooCommerce Store API (публичный, без авторизации).

Источник: {WOO_BASE_URL}/wp-json/wc/store/products
Кэширует каталог в памяти и фильтрует товары по бюджету/запросу на нашей стороне.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

from app.config import settings

log = logging.getLogger(__name__)

# Категории-аксессуары (допы), которые не должны попадать в подбор букетов —
# их предлагаем отдельно как допродажу, а не как «букет».
_ACCESSORY_CATEGORIES = {"ДОПОЛНИТЕЛЬНЫЕ ТОВАРЫ", "ШАРЫ", "ИГРУШКИ"}


@dataclass
class Product:
    id: int
    name: str
    price: float          # актуальная цена (со скидкой, если есть) в рублях
    regular_price: float
    url: str
    categories: list[str]
    in_stock: bool

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "price": int(self.price),
            "url": self.url,
            "categories": self.categories,
        }


def _to_rubles(amount: str | None, minor_unit: int) -> float:
    """Store API отдаёт цены строкой в минорных единицах (копейках)."""
    if not amount:
        return 0.0
    try:
        return int(amount) / (10 ** minor_unit)
    except (ValueError, TypeError):
        return 0.0


def _parse_product(raw: dict) -> Product | None:
    prices = raw.get("prices") or {}
    minor = int(prices.get("currency_minor_unit", 2) or 2)
    price = _to_rubles(prices.get("price"), minor)
    regular = _to_rubles(prices.get("regular_price"), minor) or price
    if price <= 0:
        return None
    return Product(
        id=int(raw.get("id", 0)),
        name=(raw.get("name") or "").strip(),
        price=price,
        regular_price=regular,
        url=raw.get("permalink") or "",
        categories=[c.get("name", "") for c in raw.get("categories", []) if c.get("name")],
        in_stock=raw.get("is_in_stock", True),
    )


class Catalog:
    """Хранит кэш товаров и обновляет его по TTL."""

    def __init__(self) -> None:
        self._products: list[Product] = []
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _fetch_all(self) -> list[Product]:
        base = settings.woo_base_url.rstrip("/")
        url = f"{base}/wp-json/wc/store/products"
        per_page = 100
        collected: list[Product] = []
        async with aiohttp.ClientSession() as session:
            page = 1
            while len(collected) < settings.woo_max_products:
                params = {"per_page": per_page, "page": page}
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        log.warning("Woo API page %s -> HTTP %s", page, resp.status)
                        break
                    batch = await resp.json()
                if not batch:
                    break
                for raw in batch:
                    p = _parse_product(raw)
                    if p:
                        collected.append(p)
                if len(batch) < per_page:
                    break
                page += 1
        log.info("Загружено товаров из каталога: %d", len(collected))
        return collected

    async def _ensure_fresh(self) -> None:
        if self._products and (time.monotonic() - self._fetched_at) < settings.woo_cache_ttl:
            return
        async with self._lock:
            if self._products and (time.monotonic() - self._fetched_at) < settings.woo_cache_ttl:
                return
            try:
                products = await self._fetch_all()
                if products:
                    self._products = products
                    self._fetched_at = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                log.exception("Не удалось обновить каталог: %s", exc)

    async def search(
        self,
        budget_min: float | None = None,
        budget_max: float | None = None,
        query: str | None = None,
        limit: int = 3,
    ) -> list[Product]:
        """Подбор товаров по бюджету и текстовому запросу.

        Сортировка: сначала наиболее релевантные по запросу, затем ближе к верхней
        границе бюджета (чтобы предлагать что-то побогаче, но в рамках бюджета).
        """
        await self._ensure_fresh()
        items = [p for p in self._products if p.in_stock]

        # Исключаем из подбора аксессуары-допы (шары, игрушки, сладости): это товары
        # для допродажи, а не букеты. Иначе как самые дешёвые они всплывают в «популярных»
        # вместо цветов (на «букет маме» бот показывал шарики за 495 ₽).
        items = [
            p for p in items
            if not (_ACCESSORY_CATEGORIES & {c.strip().upper() for c in p.categories})
        ]

        if budget_min is not None:
            items = [p for p in items if p.price >= budget_min * 0.9]
        if budget_max is not None:
            items = [p for p in items if p.price <= budget_max * 1.05]

        tokens: list[str] = []
        if query:
            tokens = [t for t in query.lower().split() if len(t) > 2]

        # «Тянемся к верхней границе» (предлагаем побогаче) ТОЛЬКО когда клиент назвал
        # полный диапазон бюджета. Если бюджета нет или указан лишь потолок (модель
        # нередко придумывает завышенный budget_max сама) — сортируем недорогое первым,
        # иначе наверх всплывают самые дорогие букеты и пугают клиента.
        anchor_to_max = budget_min is not None and budget_max is not None

        def score(p: Product) -> tuple:
            text = (p.name + " " + " ".join(p.categories)).lower()
            relevance = sum(1 for t in tokens if t in text)
            if anchor_to_max:
                closeness = -abs(budget_max - p.price)
            else:
                closeness = -p.price
            return (relevance, closeness)

        items.sort(key=score, reverse=True)
        return items[:limit]

    async def known_urls(self) -> set[str]:
        """Множество ссылок всех реальных товаров — для проверки, что модель
        не выдумала URL (ссылки сравниваем без хвостового слэша)."""
        await self._ensure_fresh()
        return {p.url.rstrip("/") for p in self._products if p.url}


catalog = Catalog()
