"""Оркестрация диалога с LLM: история + инструменты -> ответ клиенту.

Цикл одного хода:
  messages = [system] + история(из БД) + текущая реплика
  -> модель может вызвать search_catalog (выполняем, кладём результат, повторяем)
  -> если модель вызвала handoff_to_florist — возвращаем HandoffData (без текста),
     дальнейшую отправку берёт на себя обработчик.
  -> иначе возвращаем текстовый ответ.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.catalog.woo import Product, catalog
from app.config import settings
from app.db.storage import storage
from app.llm.client import client
from app.llm.prompt import SYSTEM_PROMPT
from app.llm.tools import TOOLS

log = logging.getLogger(__name__)

MAX_TOOL_ITERS = 4

# Ссылка на товары магазина (для проверки, что модель не выдумала URL)
_SHOP_URL_RE = re.compile(r"https?://[^\s)<>]*cvety-naberezhnye\.ru[^\s)<>]*", re.I)


def _has_hallucinated_links(text: str, offered: list[Product]) -> bool:
    """True, если в ответе есть ссылка на магазин, которой НЕТ среди реально
    подобранных товаров (модель сочинила URL/товар)."""
    urls = _SHOP_URL_RE.findall(text)
    if not urls:
        return False
    valid = {p.url.rstrip("/") for p in offered}
    return any(u.rstrip("/") not in valid for u in urls)


def _render_products(offered: list[Product]) -> str:
    """Детерминированный список товаров из настоящих результатов поиска —
    запасной вариант, когда модель сгаллюцинировала ссылки/цены."""
    lines = [
        f"{i}. {p.name} — {int(p.price)} ₽\n{p.url}"
        for i, p in enumerate(offered[:3], 1)
    ]
    return "Вот варианты из нашего каталога:\n\n" + "\n\n".join(lines) + \
        "\n\nКакой больше нравится? 🌷"


async def _record_usage(tg_id: int, resp) -> None:
    """Сохранить расход токенов и стоимость вызова LLM (OpenRouter возвращает
    cost при usage.include=true). Ошибки учёта не должны ломать диалог."""
    try:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        # cost приходит как доп. поле OpenRouter (в USD)
        cost = getattr(usage, "cost", None)
        if cost is None:
            extra = getattr(usage, "model_extra", None) or {}
            cost = extra.get("cost", 0)
        await storage.add_usage(
            tg_id,
            model=getattr(resp, "model", settings.openrouter_model),
            prompt_tokens=int(prompt),
            completion_tokens=int(completion),
            cost=float(cost or 0),
        )
    except Exception as exc:  # noqa: BLE001 — учёт не критичен
        log.warning("Не удалось записать usage для %s: %s", tg_id, exc)


@dataclass
class HandoffData:
    name: str
    phone: str
    product_name: str
    product_url: str
    price: float
    delivery_method: str
    budget: str
    comment: str

    @classmethod
    def from_args(cls, args: dict) -> "HandoffData":
        return cls(
            name=str(args.get("name", "")).strip(),
            phone=str(args.get("phone", "")).strip(),
            product_name=str(args.get("product_name", "")).strip(),
            product_url=str(args.get("product_url", "")).strip(),
            price=float(args.get("price", 0) or 0),
            delivery_method=str(args.get("delivery_method", "")).strip(),
            budget=str(args.get("budget", "")).strip(),
            comment=str(args.get("comment", "")).strip(),
        )


@dataclass
class ConsultResult:
    text: str | None = None
    handoff: HandoffData | None = None


async def _run_search(args: dict) -> tuple[str, list[Product]]:
    products = await catalog.search(
        budget_min=args.get("budget_min"),
        budget_max=args.get("budget_max"),
        query=args.get("query"),
        limit=3,
    )
    if not products:
        return json.dumps({"products": [], "note": "ничего не найдено в этом бюджете"},
                          ensure_ascii=False), []
    return (
        json.dumps({"products": [p.as_dict() for p in products]}, ensure_ascii=False),
        products,
    )


async def generate(tg_id: int, user_text: str) -> ConsultResult:
    """Сформировать ответ консультанта на сообщение пользователя."""
    history = await storage.history(tg_id, limit=20)
    # в контекст LLM — только реплики клиента и ассистента (без сообщений менеджера)
    history = [m for m in history if m["role"] in ("user", "assistant")]
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    # товары из последнего вызова search_catalog — для проверки ответа модели
    offered: list[Product] = []

    for _ in range(MAX_TOOL_ITERS):
        resp = await client.chat.completions.create(
            model=settings.openrouter_model,
            messages=messages,
            tools=TOOLS,
            temperature=0.3,
            extra_body={"usage": {"include": True}},
        )
        await _record_usage(tg_id, resp)
        msg = resp.choices[0].message

        if not msg.tool_calls:
            text = (msg.content or "").strip()
            # защита от галлюцинаций: если модель сослалась на несуществующие
            # товары/ссылки — подменяем на реальный список из каталога
            if offered and _has_hallucinated_links(text, offered):
                log.warning("LLM выдал несуществующие ссылки для %s — заменяю реальными", tg_id)
                text = _render_products(offered)
            return ConsultResult(text=text or "Извините, повторите, пожалуйста?")

        # есть вызовы инструментов — добавляем сообщение ассистента в контекст
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            if name == "handoff_to_florist":
                return ConsultResult(handoff=HandoffData.from_args(args))

            if name == "search_catalog":
                result, offered = await _run_search(args)
            else:
                result = json.dumps({"error": f"unknown tool {name}"}, ensure_ascii=False)

            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "name": name, "content": result}
            )

    log.warning("Достигнут лимит итераций инструментов для %s", tg_id)
    return ConsultResult(text="Секунду, подбираю варианты…")
