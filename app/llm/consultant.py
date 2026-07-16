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

# Признак «обещаю подобрать», когда модель тянет время вместо вызова search_catalog
_PROMISE_RE = re.compile(
    r"подбер|подыщ|минут|момент|секунд|сейчас\s+(покажу|найд|подбер)|посмотрю|подожд",
    re.I,
)

# Модель написала «передаю/передам ... флористу» обычным текстом, НО не вызвала
# инструмент handoff_to_florist — тогда передачи не происходит, и клиент вынужден
# переспрашивать «а где флорист?». Такой анонс без вызова форсируем в реальную передачу.
_HANDOFF_ANNOUNCE_RE = re.compile(
    r"(переда|соедин|подключ)\w*[^.!?\n]{0,60}флорист",
    re.I,
)
# ...но НЕ форсируем, если это ПРЕДЛОЖЕНИЕ/вопрос («хотите, передам флористу?»),
# а не решение. Иначе клиента передаёт флористу до его согласия.
_HANDOFF_OFFER_RE = re.compile(
    r"хотите|хотели|если\s+хотите|могу\s+переда|можем\s+переда|нужно\s+ли|\?",
    re.I,
)


async def _guard_reply(tg_id: int, text: str, offered: list[Product],
                       exclude_urls: set[str] | None = None) -> str:
    """Защита от выдуманных товаров. Если в ответе есть ссылка на магазин,
    которой НЕТ среди реальных товаров каталога (модель сочинила URL или
    повторила фейк из истории диалога) — подменяем на реальный список.
    Работает даже когда модель не делала свежий поиск в этом ходе."""
    urls = _SHOP_URL_RE.findall(text)
    if not urls:
        return text
    valid = await catalog.known_urls()
    valid |= {p.url.rstrip("/") for p in offered}
    if all(u.rstrip("/") in valid for u in urls):
        return text  # все ссылки ведут на реальные товары
    log.warning("LLM выдал несуществующие ссылки для %s — заменяю реальными", tg_id)
    products = offered or await catalog.search(limit=3, exclude_urls=exclude_urls)
    return _render_products(products) if products else text


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


async def _run_search(args: dict, exclude_urls: set[str] | None = None) -> tuple[str, list[Product]]:
    products = await catalog.search(
        budget_min=args.get("budget_min"),
        budget_max=args.get("budget_max"),
        query=args.get("query"),
        limit=3,
        exclude_urls=exclude_urls,
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

    # уже показанные товары — исключим из нового поиска, чтобы «ещё варианты»
    # давали НОВЫЕ букеты, а не те же самые
    shown_urls: set[str] = set()
    for m in history:
        for u in _SHOP_URL_RE.findall(m.get("content") or ""):
            shown_urls.add(u.rstrip("/"))

    # товары из последнего вызова search_catalog — для проверки ответа модели
    offered: list[Product] = []
    did_search = False  # был ли реальный вызов search_catalog в этом ходе

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
            # Модель анонсировала передачу флористу («передаю ваш запрос флористу»),
            # но НЕ вызвала handoff_to_florist — форсируем реальную передачу, иначе
            # клиент остаётся ждать и вынужден переспрашивать «а где флорист?».
            if (_HANDOFF_ANNOUNCE_RE.search(text)
                    and not _HANDOFF_OFFER_RE.search(text)   # не предложение/вопрос
                    and not _SHOP_URL_RE.search(text)):      # не показ товаров
                log.info("Модель анонсировала хэндофф без инструмента — форсирую передачу для %s", tg_id)
                return ConsultResult(handoff=HandoffData.from_args({}))
            # «Минутку, сейчас подберу!» без вызова инструмента — модель тянет время
            # и ход заканчивается без товаров. Форсируем реальный поиск и показываем
            # настоящие варианты, чтобы клиент не остался без ответа.
            if not did_search and not _SHOP_URL_RE.search(text) and _PROMISE_RE.search(text):
                products = await catalog.search(query=user_text or None, limit=3,
                                                exclude_urls=shown_urls)
                if products:
                    log.info("Модель пообещала, но не искала — форсирую поиск для %s", tg_id)
                    return ConsultResult(text=_render_products(products))
            # защита от галлюцинаций: ссылки на несуществующие товары (в т.ч.
            # повтор фейков из истории) подменяем реальным списком из каталога
            text = await _guard_reply(tg_id, text, offered, exclude_urls=shown_urls)
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
                result, offered = await _run_search(args, exclude_urls=shown_urls)
                did_search = True
            else:
                result = json.dumps({"error": f"unknown tool {name}"}, ensure_ascii=False)

            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "name": name, "content": result}
            )

    log.warning("Достигнут лимит итераций инструментов для %s", tg_id)
    return ConsultResult(text="Секунду, подбираю варианты…")
