"""Клиент OpenRouter (OpenAI-совместимый API)."""
from __future__ import annotations

from openai import AsyncOpenAI

from app.config import settings

client = AsyncOpenAI(
    api_key=settings.openrouter_api_key,
    base_url=settings.openrouter_base_url,
    default_headers={
        "HTTP-Referer": settings.openrouter_app_url,
        "X-Title": settings.openrouter_app_name,
    },
)
