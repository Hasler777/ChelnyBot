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
from dataclasses import dataclass

from app.catalog.woo import catalog
from app.config import settings
from app.db.storage import storage
from app.llm.client import client
from app.llm.prompt import SYSTEM_PROMPT
from app.llm.tools import TOOLS

log = logging.getLogger(__name__)

MAX_TOOL_ITERS = 4


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


async def _run_search(args: dict) -> str:
    products = await catalog.search(
        budget_min=args.get("budget_min"),
        budget_max=args.get("budget_max"),
        query=args.get("query"),
        limit=3,
    )
    if not products:
        return json.dumps({"products": [], "note": "ничего не найдено в этом бюджете"},
                          ensure_ascii=False)
    return json.dumps({"products": [p.as_dict() for p in products]}, ensure_ascii=False)


async def generate(tg_id: int, user_text: str) -> ConsultResult:
    """Сформировать ответ консультанта на сообщение пользователя."""
    history = await storage.history(tg_id, limit=20)
    # в контекст LLM — только реплики клиента и ассистента (без сообщений менеджера)
    history = [m for m in history if m["role"] in ("user", "assistant")]
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    for _ in range(MAX_TOOL_ITERS):
        resp = await client.chat.completions.create(
            model=settings.openrouter_model,
            messages=messages,
            tools=TOOLS,
            temperature=0.6,
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            text = (msg.content or "").strip()
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
                result = await _run_search(args)
            else:
                result = json.dumps({"error": f"unknown tool {name}"}, ensure_ascii=False)

            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "name": name, "content": result}
            )

    log.warning("Достигнут лимит итераций инструментов для %s", tg_id)
    return ConsultResult(text="Секунду, подбираю варианты…")
