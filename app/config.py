"""Конфигурация приложения из переменных окружения (.env)."""
from __future__ import annotations

from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Telegram
    telegram_bot_token: str

    # OpenRouter / LLM
    openrouter_api_key: str
    openrouter_model: str = "anthropic/claude-sonnet-4.5"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_url: str = "https://cvety-naberezhnye.ru"
    openrouter_app_name: str = "Sonya Flower Bot"

    # Каталог WooCommerce
    woo_base_url: str = "https://cvety-naberezhnye.ru"
    woo_cache_ttl: int = 1800
    woo_max_products: int = 400
    # На какой ценовой сегмент целиться, когда клиент не назвал бюджет (₽).
    # Не самые дешёвые и не премиум — «средний приятный» букет.
    default_budget_anchor: int = 5000

    # amoCRM REST
    amo_base_url: str = ""
    amo_client_id: str = ""
    amo_client_secret: str = ""
    amo_redirect_uri: str = ""
    amo_auth_code: str = ""
    amo_access_token: str = ""
    amo_refresh_token: str = ""
    amo_pipeline_id: Optional[int] = None
    amo_status_id: Optional[int] = None
    amo_cf_product: Optional[int] = None
    amo_cf_product_url: Optional[int] = None
    amo_cf_price: Optional[int] = None
    amo_cf_budget: Optional[int] = None
    amo_cf_delivery: Optional[int] = None
    amo_cf_source: Optional[int] = None
    amo_cf_contact_phone: Optional[int] = None

    # amoCRM Chat API (amoJo)
    amojo_base_url: str = "https://amojo.amocrm.ru"
    amojo_channel_id: str = ""
    amojo_channel_secret: str = ""
    amojo_scope_id: str = ""
    amojo_account_id: str = ""

    # Webhook server
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_path: str = "/amojo/webhook"

    # Виджет amoCRM (чат-панель в карточке сделки)
    widget_public_url: str = "https://144-31-108-55.sslip.io"
    widget_token: str = ""  # общий секрет, который виджет шлёт в запросах

    # Админка (список диалогов, пользователей и стоимости)
    admin_token: str = ""   # пароль для входа в /admin (если пусто — доступ открыт)
    usd_rub_rate: float = 0  # курс USD→RUB для показа стоимости в рублях (0 — не показывать)

    # Прочее
    db_path: str = "data/bot.db"
    log_level: str = "INFO"

    @field_validator(
        "amo_pipeline_id", "amo_status_id", "amo_cf_product", "amo_cf_product_url",
        "amo_cf_price", "amo_cf_budget", "amo_cf_delivery", "amo_cf_source",
        "amo_cf_contact_phone", mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v):
        if v in ("", None):
            return None
        return v

    @property
    def amo_enabled(self) -> bool:
        return bool(self.amo_base_url and (self.amo_access_token or self.amo_refresh_token))

    @property
    def amojo_enabled(self) -> bool:
        return bool(self.amojo_scope_id and self.amojo_channel_secret)


settings = Settings()  # type: ignore[call-arg]
