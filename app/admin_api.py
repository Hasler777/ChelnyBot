"""Админка: список диалогов и пользователей со стоимостью переписки.

Эндпоинты:
  GET /admin                     -> HTML-страница админки
  GET /admin/api/users?token=..  -> сводка по всем пользователям + итоги
  GET /admin/api/dialog?tg_id=.. -> полная переписка одного диалога + стоимость

Доступ защищён общим секретом ADMIN_TOKEN (если задан в .env).
"""
from __future__ import annotations

import base64
import logging
import os
import re
import time

import aiohttp
from aiohttp import web

from app.config import settings
from app.crm import utm
from app.db.storage import storage
from app.services.dialog_analysis import analyze

log = logging.getLogger(__name__)


def _logo_data_uri() -> str:
    """Логотип ЦветоМира как data-URI (встраиваем прямо в HTML, без статик-роутов)."""
    path = os.path.join(os.path.dirname(__file__), "static", "logo.webp")
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/webp;base64,{b64}"
    except OSError as exc:  # noqa: BLE001 — если файла нет, шапка просто без лого
        log.warning("Не удалось загрузить логотип: %s", exc)
        return ""


_LOGO_URI = _logo_data_uri()

_ROLE_MAP = {"user": "client", "assistant": "bot", "manager": "manager"}

# Кэш курса USD->RUB (живой курс ЦБ РФ), чтобы рубли были корректными
_RATE_CACHE = {"value": 0.0, "ts": 0.0}
_RATE_TTL = 6 * 3600  # обновляем раз в 6 часов


async def _usd_rub_rate() -> float:
    """Курс USD->RUB: приоритет — ручной USD_RUB_RATE из .env, иначе живой курс
    ЦБ РФ (кэш 6 ч). При сбоях возвращаем последнее значение или 0."""
    if settings.usd_rub_rate and settings.usd_rub_rate > 0:
        return float(settings.usd_rub_rate)
    now = time.time()
    if _RATE_CACHE["value"] and now - _RATE_CACHE["ts"] < _RATE_TTL:
        return _RATE_CACHE["value"]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://www.cbr-xml-daily.ru/daily_json.js",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json(content_type=None)
        rate = float(data["Valute"]["USD"]["Value"])
        _RATE_CACHE.update(value=rate, ts=now)
        return rate
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось получить курс USD/RUB: %s", exc)
        return _RATE_CACHE["value"] or 0.0


# Кэш @username бота (для базы deeplink-ссылок UTM). getMe дёргаем редко.
_BOT_CACHE = {"value": "", "ts": 0.0}
_BOT_TTL = 24 * 3600
# Допустимый payload UTM-метки (то, что уходит в ?start=…): латиница/цифры/_/-.
_UTM_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def _bot_username() -> str:
    """@username бота без @ (для ссылок t.me/<username>?start=…).

    Приоритет — ручной TELEGRAM_BOT_USERNAME из .env; иначе Telegram getMe по
    токену (кэш 24 ч). При сбое/без токена — последнее значение или "" (пустая
    строка → фронт прячет кнопку «Копировать»)."""
    if settings.telegram_bot_username:
        return settings.telegram_bot_username.lstrip("@")
    now = time.time()
    if _BOT_CACHE["value"] and now - _BOT_CACHE["ts"] < _BOT_TTL:
        return _BOT_CACHE["value"]
    if not settings.telegram_bot_token:
        return _BOT_CACHE["value"]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json(content_type=None)
        username = (data.get("result") or {}).get("username") or ""
        if username:
            _BOT_CACHE.update(value=username, ts=now)
        return username or _BOT_CACHE["value"]
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось получить username бота (getMe): %s", exc)
        return _BOT_CACHE["value"]


def _check(token: str | None, secret: str) -> bool:
    # если секрет не задан — доступ открыт (удобно для локальной отладки)
    return not secret or token == secret


async def _users_response(
    markup: float, hidden: set[int] | None = None, with_wallet: bool = False
) -> web.Response:
    users = await storage.users_overview()
    # человекочитаемая подпись UTM-источника (для сводки «Источники трафика»);
    # имена кампаний, заведённых владельцем в админке, важнее статичного справочника
    labels = await storage.utm_labels_map()
    for u in users:
        u["utm_label"] = utm.admin_label(u.get("utm_source"), labels)
    if hidden:
        # скрываем тестовые аккаунты из кабинета владельца и пересчитываем итоги
        # только по видимым клиентам (в /admin фильтр не применяется)
        users = [u for u in users if u["tg_id"] not in hidden]
        totals = {
            "users": len(users),
            "messages": sum(u.get("msg_count") or 0 for u in users),
            "tokens": sum(u.get("tokens") or 0 for u in users),
            "calls": sum(u.get("llm_calls") or 0 for u in users),
            "cost": sum(u.get("cost") or 0 for u in users),
        }
    else:
        totals = await storage.totals()
    if markup and markup != 1.0:
        for u in users:
            u["cost"] = (u.get("cost") or 0) * markup
        totals["cost"] = (totals.get("cost") or 0) * markup
    rate = await _usd_rub_rate()
    payload = {"users": users, "totals": totals, "usd_rub_rate": rate}
    if with_wallet:
        # кошелёк ведётся в рублях; списано = расход (с наценкой, в USD) по курсу
        balance = await storage.wallet_balance()
        spent = (totals.get("cost") or 0) * rate
        payload["wallet"] = {
            "balance": balance,
            "spent": spent,
            "remaining": balance - spent,
        }
    return web.json_response(payload)


async def owner_wallet_state() -> dict:
    """Остаток виртуального кошелька владельца в рублях: {balance, spent, remaining}.
    Тот же расчёт, что отдаёт /owner (расход с наценкой по видимым клиентам × курс),
    вынесен отдельно — им пользуется фоновый сторож баланса (services/balance_alert)."""
    users = await storage.users_overview()
    hidden = settings.owner_hidden_ids
    if hidden:
        users = [u for u in users if u["tg_id"] not in hidden]
    cost_usd = sum(u.get("cost") or 0 for u in users) * settings.owner_cost_markup
    rate = await _usd_rub_rate()
    balance = await storage.wallet_balance()
    spent = cost_usd * rate
    return {"balance": balance, "spent": spent, "remaining": balance - spent}


async def _dialog_response(request: web.Request, markup: float) -> web.Response:
    try:
        tg_id = int(request.query.get("tg_id", ""))
    except ValueError:
        return web.json_response({"error": "bad tg_id"}, status=400)

    user = await storage.get_user(tg_id)
    rows = await storage.history_full(tg_id, limit=1000)
    cost = await storage.dialog_cost(tg_id)
    labels = await storage.utm_labels_map()
    if markup and markup != 1.0:
        cost = {**cost, "cost": (cost.get("cost") or 0) * markup}
    messages = [
        {"from": _ROLE_MAP.get(r["role"], r["role"]), "text": r["content"], "ts": r["ts"]}
        for r in rows
    ]
    return web.json_response(
        {
            "messages": messages,
            "cost": cost,
            "user": {
                "tg_id": tg_id,
                "name": user.name if user else None,
                "phone": user.phone if user else None,
                "state": user.state if user else None,
                "amo_lead_id": user.amo_lead_id if user else None,
                "channel": user.channel if user else None,
                "utm_label": utm.admin_label(user.utm_source, labels) if user else None,
            },
            "usd_rub_rate": await _usd_rub_rate(),
        }
    )


# ---- наш кабинет (/admin): реальная стоимость ----
async def api_users(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.admin_token):
        return web.json_response({"error": "forbidden"}, status=403)
    return await _users_response(1.0)


async def api_dialog(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.admin_token):
        return web.json_response({"error": "forbidden"}, status=403)
    return await _dialog_response(request, 1.0)


# ---- кабинет владельца (/owner): стоимость с наценкой ----
async def owner_users(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.owner_token):
        return web.json_response({"error": "forbidden"}, status=403)
    return await _users_response(
        settings.owner_cost_markup, settings.owner_hidden_ids, with_wallet=True
    )


async def owner_dialog(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.owner_token):
        return web.json_response({"error": "forbidden"}, status=403)
    # скрытые из /owner клиенты не открываются и по прямой ссылке
    try:
        tg_id = int(request.query.get("tg_id", ""))
    except ValueError:
        return web.json_response({"error": "bad tg_id"}, status=400)
    if tg_id in settings.owner_hidden_ids:
        return web.json_response({"error": "not found"}, status=404)
    return await _dialog_response(request, settings.owner_cost_markup)


async def _analysis_response(hidden: set[int] | None) -> web.Response:
    texts = await storage.client_dialog_texts(hidden)
    return web.json_response(analyze(texts))


# ---- UTM-кампании (раздел «Реклама (UTM)») ----
async def _utm_list_response() -> web.Response:
    """Список кампаний с числом клиентов + база для готовой deeplink-ссылки."""
    username = await _bot_username()
    link_base = f"https://t.me/{username}?start=" if username else ""
    campaigns = await storage.utm_campaigns_with_counts()
    return web.json_response(
        {"campaigns": campaigns, "bot_username": username, "link_base": link_base}
    )


async def _utm_create(request: web.Request) -> web.Response:
    """Создать/переименовать UTM-метку. payload — код кампании (?start=…),
    label — человекочитаемое имя. Payload нормализуем в lower, чтобы он сходился
    с users.utm_source (там метки кладутся через utm.normalize)."""
    payload = (request.query.get("payload") or "").strip()
    label = (request.query.get("label") or "").strip()
    if not _UTM_PAYLOAD_RE.match(payload):
        return web.json_response({"error": "bad payload"}, status=400)
    if not label:
        return web.json_response({"error": "empty label"}, status=400)
    label = label[:120]
    payload_norm = utm.normalize(payload)
    await storage.utm_campaign_upsert(payload_norm, label)
    return web.json_response({"ok": True, "payload": payload_norm, "label": label})


async def api_analysis(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.admin_token):
        return web.json_response({"error": "forbidden"}, status=403)
    return await _analysis_response(None)


async def api_utm(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.admin_token):
        return web.json_response({"error": "forbidden"}, status=403)
    return await _utm_list_response()


async def api_utm_create(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.admin_token):
        return web.json_response({"error": "forbidden"}, status=403)
    return await _utm_create(request)


async def owner_analysis(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.owner_token):
        return web.json_response({"error": "forbidden"}, status=403)
    return await _analysis_response(settings.owner_hidden_ids)


async def owner_utm(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.owner_token):
        return web.json_response({"error": "forbidden"}, status=403)
    return await _utm_list_response()


async def owner_utm_create(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.owner_token):
        return web.json_response({"error": "forbidden"}, status=403)
    return await _utm_create(request)


async def owner_wallet_topup(request: web.Request) -> web.Response:
    """Пополнение/корректировка виртуального кошелька (в рублях)."""
    if not _check(request.query.get("token"), settings.owner_token):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        amount = float(request.query.get("amount", ""))
    except ValueError:
        return web.json_response({"error": "bad amount"}, status=400)
    note = request.query.get("note", "")
    balance = await storage.wallet_topup(amount, note)
    return web.json_response({"balance": balance})


def _page(api_base: str, show_tokens: bool = True, show_wallet: bool = False) -> web.Response:
    html = (_ADMIN_HTML.replace("__API_BASE__", api_base)
                       .replace("__LOGO__", _LOGO_URI)
                       .replace("__SHOW_TOKENS__", "true" if show_tokens else "false")
                       .replace("__SHOW_WALLET__", "true" if show_wallet else "false"))
    return web.Response(text=html, content_type="text/html")


async def get_admin_page(request: web.Request) -> web.Response:
    return _page("/admin")


async def get_owner_page(request: web.Request) -> web.Response:
    return _page("/owner", show_tokens=False, show_wallet=True)


def add_admin_routes(app: web.Application) -> None:
    app.router.add_get("/admin", get_admin_page)
    app.router.add_get("/admin/api/users", api_users)
    app.router.add_get("/admin/api/dialog", api_dialog)
    app.router.add_get("/admin/api/analysis", api_analysis)
    app.router.add_get("/admin/api/utm", api_utm)
    app.router.add_post("/admin/api/utm", api_utm_create)
    app.router.add_get("/owner", get_owner_page)
    app.router.add_get("/owner/api/users", owner_users)
    app.router.add_get("/owner/api/dialog", owner_dialog)
    app.router.add_get("/owner/api/analysis", owner_analysis)
    app.router.add_get("/owner/api/utm", owner_utm)
    app.router.add_post("/owner/api/utm", owner_utm_create)
    app.router.add_post("/owner/api/wallet/topup", owner_wallet_topup)


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ЦветоМир · Админка</title>
<style>
  :root { --bg:#0f141b; --panel:#171e27; --panel2:#1f2733; --line:#2b3947; --txt:#e6edf3; --mut:#9fb3c8; --acc:#2f6feb; }
  * { box-sizing:border-box; }
  html, body { min-height:100%; margin:0; }
  body { font-family:-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--txt); display:flex; }
  /* левое меню */
  .sidebar { width:210px; flex:0 0 210px; background:var(--panel); border-right:1px solid var(--line); display:flex; flex-direction:column; gap:4px; padding:14px 10px; position:sticky; top:0; height:100vh; }
  .sidebar .brand { display:flex; align-items:center; gap:9px; padding:4px 8px 14px; font-weight:700; font-size:15px; }
  .sidebar .brand img { height:26px; width:auto; display:block; }
  .navitem { display:flex; align-items:center; gap:10px; background:none; border:0; color:var(--txt); text-align:left; width:100%; padding:10px 12px; border-radius:9px; font-size:14px; font-family:inherit; cursor:pointer; }
  .navitem:hover { background:var(--panel2); }
  .navitem.active { background:var(--acc); color:#fff; }
  .navitem.logout { margin-top:auto; color:var(--mut); }
  .content { flex:1; min-width:0; display:flex; flex-direction:column; }
  .panel { display:none; }
  .panel.active { display:block; }
  header { padding:14px 20px; background:var(--panel); border-bottom:1px solid var(--line); display:flex; align-items:center; gap:24px; flex-wrap:wrap; }
  header h1 { font-size:17px; margin:0; font-weight:700; display:flex; align-items:center; gap:10px; }
  header h1 img.logo { height:30px; width:auto; display:block; }
  .stats { display:flex; gap:22px; flex-wrap:wrap; }
  .stat { display:flex; flex-direction:column; }
  .stat b { font-size:16px; }
  .stat span { font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:.04em; }
  #search { margin-left:auto; background:var(--bg); border:1px solid var(--line); border-radius:8px; color:var(--txt); padding:8px 12px; font-size:13px; min-width:200px; }
  .wallet { display:flex; align-items:center; gap:16px; background:var(--panel2); border:1px solid var(--line); border-radius:12px; padding:10px 16px; }
  .wallet .wrem { font-size:20px; font-weight:700; color:#7ee2a8; line-height:1.1; }
  .wallet .wrem.neg { color:#f08a8a; }
  .wallet .wlabel { display:block; font-size:10px; color:var(--mut); text-transform:uppercase; letter-spacing:.04em; }
  .wallet .wmeta { font-size:11px; color:var(--mut); display:flex; flex-direction:column; gap:2px; }
  .wallet #topup { background:var(--acc); border:0; color:#fff; border-radius:8px; padding:8px 14px; font-size:13px; font-weight:600; cursor:pointer; }
  .wallet #topup:hover { filter:brightness(1.1); }
  main { padding:16px 20px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); }
  th { color:var(--mut); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; cursor:pointer; user-select:none; white-space:nowrap; }
  tbody tr { cursor:pointer; }
  tbody tr:hover { background:var(--panel2); }
  th.num { text-align:right; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .cost { font-weight:600; color:#7ee2a8; }
  .badge { font-size:11px; padding:2px 8px; border-radius:10px; background:var(--panel2); color:var(--mut); }
  .badge.handoff { background:#3a2d12; color:#f0c674; }
  .badge.consult { background:#13314a; color:#79b8ff; }
  .src.tg { background:#13314a; color:#79b8ff; }
  .src.web { background:#1c3a2a; color:#7ee2b8; }
  .src.max { background:#3a2340; color:#e79bff; }
  .muted { color:var(--mut); }
  /* drawer */
  #overlay { position:fixed; inset:0; background:rgba(0,0,0,.55); display:none; }
  #drawer { position:fixed; top:0; right:0; height:100%; width:min(560px,100%); background:var(--panel); border-left:1px solid var(--line); transform:translateX(100%); transition:transform .2s; display:flex; flex-direction:column; }
  #overlay.open { display:block; }
  #overlay.open #drawer { transform:translateX(0); }
  #dhead { padding:14px 18px; border-bottom:1px solid var(--line); }
  #dhead .top { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
  #dhead h2 { font-size:16px; margin:0 0 4px; }
  #dhead .sub { font-size:12px; color:var(--mut); }
  #dhead .costline { margin-top:10px; display:flex; gap:18px; font-size:12px; }
  #dhead .costline b { color:#7ee2a8; font-size:14px; display:block; }
  #close { background:none; border:0; color:var(--mut); font-size:22px; cursor:pointer; line-height:1; }
  #log { flex:1; overflow-y:auto; padding:14px 16px; display:flex; flex-direction:column; gap:8px; }
  .row { display:flex; }
  .row.right { justify-content:flex-end; }
  .bubble { max-width:80%; padding:8px 11px; border-radius:12px; font-size:13px; line-height:1.4; white-space:pre-wrap; word-wrap:break-word; }
  .client .bubble { background:var(--panel2); border-bottom-left-radius:4px; }
  .bot .bubble { background:#243244; color:#9fb3c8; border-bottom-left-radius:4px; }
  .manager .bubble { background:var(--acc); color:#fff; border-bottom-right-radius:4px; }
  .meta { font-size:10px; opacity:.6; margin:0 4px 2px; }
  #empty { color:var(--mut); text-align:center; padding:40px 0; }
  .no-tokens .col-tokens { display:none; }
  /* анализ диалогов */
  .analysis { margin-top:30px; }
  .analysis h2 { font-size:15px; margin:0 0 2px; }
  .analysis .asub { font-size:12px; color:var(--mut); margin-bottom:14px; }
  .agrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px; }
  .acard { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .acard h3 { font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--mut); margin:0 0 12px; }
  .aitem { display:grid; grid-template-columns:132px 1fr 34px; align-items:center; gap:10px; margin-bottom:9px; font-size:13px; }
  .aitem:last-child { margin-bottom:0; }
  .aitem .albl { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .aitem .abar { height:8px; background:var(--panel2); border-radius:5px; overflow:hidden; }
  .aitem .abar > i { display:block; height:100%; background:var(--acc); border-radius:5px; }
  .aitem .acnt { text-align:right; font-variant-numeric:tabular-nums; color:var(--mut); }
  .acard .aempty { color:var(--mut); font-size:12px; }
  /* сводка по источникам трафика (UTM) */
  .srcbreak { margin-bottom:26px; }
  .srcbreak h2 { font-size:15px; margin:0 0 2px; }
  .srcbreak .asub { margin-bottom:14px; }
  .srcbreak .acard.wide .aitem { grid-template-columns:minmax(140px,220px) 1fr 40px; }
  /* раздел «Реклама (UTM)» */
  .panel h2.ptitle { font-size:17px; margin:2px 0 4px; }
  .panel .psub { font-size:12px; color:var(--mut); margin-bottom:16px; }
  #panel-dialogs #search { margin:0 0 14px; display:block; }
  .utm-form { display:flex; flex-wrap:wrap; gap:12px; align-items:flex-end; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:22px; }
  .utm-form .fld { display:flex; flex-direction:column; gap:5px; }
  .utm-form label { font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:.04em; }
  .utm-form input { background:var(--bg); border:1px solid var(--line); border-radius:8px; color:var(--txt); padding:8px 12px; font-size:13px; min-width:180px; }
  .utm-form button { background:var(--acc); border:0; color:#fff; border-radius:8px; padding:9px 18px; font-size:13px; font-weight:600; cursor:pointer; }
  .utm-form button:hover { filter:brightness(1.1); }
  #utmerr { color:#f08a8a; font-size:12px; min-height:16px; margin:-12px 0 12px 2px; }
  .utm-link { display:inline-flex; align-items:center; gap:8px; }
  .utm-link a { color:#79b8ff; text-decoration:none; word-break:break-all; }
  .copy { background:var(--panel2); border:1px solid var(--line); color:var(--mut); border-radius:7px; padding:3px 10px; font-size:12px; cursor:pointer; white-space:nowrap; }
  .copy:hover { color:var(--txt); }
  .utm-hint { color:var(--mut); font-size:12px; }
</style>
</head>
<body>
  <aside class="sidebar">
    <div class="brand"><img class="logo" src="__LOGO__" alt="ЦветоМир">ЦветоМир</div>
    <button class="navitem active" data-panel="dash">📊 Дашборд</button>
    <button class="navitem" data-panel="dialogs">💬 Диалоги</button>
    <button class="navitem" data-panel="utm">📣 Реклама (UTM)</button>
    <button class="navitem logout" id="logout">🚪 Выйти</button>
  </aside>
  <div class="content">
  <header>
    <div class="stats" id="stats"></div>
    <div class="wallet" id="wallet" style="display:none"></div>
  </header>
  <main>
    <section class="panel active" id="panel-dash">
    <section class="srcbreak" id="srcbreak" style="display:none">
      <h2>Источники трафика</h2>
      <div class="asub" id="srcsub"></div>
      <div class="agrid"><div class="acard wide"><h3>Обращения по источникам</h3><div id="srcitems"></div></div></div>
    </section>

    <section class="analysis" id="analysis" style="display:none">
      <h2>Анализ диалогов</h2>
      <div class="asub" id="asub"></div>
      <div class="agrid" id="agrid"></div>
    </section>
    </section>

    <section class="panel" id="panel-dialogs">
    <input id="search" placeholder="Поиск по имени / телефону / id…">
    <table>
      <thead><tr>
        <th data-k="name">Клиент</th>
        <th data-k="phone">Телефон</th>
        <th data-k="channel">Источник</th>
        <th data-k="state">Статус</th>
        <th data-k="msg_count" class="num">Сообщений</th>
        <th data-k="llm_calls" class="num">Запросов</th>
        <th data-k="tokens" class="num col-tokens">Токенов</th>
        <th data-k="cost" class="num">Стоимость</th>
        <th data-k="last_ts" class="num">Активность</th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="9" id="empty">Загрузка…</td></tr></tbody>
    </table>
    </section>

    <section class="panel" id="panel-utm">
      <h2 class="ptitle">Реклама (UTM)</h2>
      <div class="psub">Создайте метку для рекламы — получите ссылку для канала и статистику привлечённых клиентов. Клиент перейдёт по ссылке, нажмёт «Старт» — и обращение зачтётся этой метке.</div>
      <form class="utm-form" id="utmform" onsubmit="return false">
        <div class="fld"><label>Код метки (в ссылке)</label><input id="utmpayload" placeholder="напр. vk_avgust" maxlength="64" autocomplete="off"></div>
        <div class="fld"><label>Название кампании</label><input id="utmlabel" placeholder="напр. ВК август" maxlength="120" autocomplete="off"></div>
        <button id="utmadd">Создать метку</button>
      </form>
      <div id="utmerr"></div>
      <table>
        <thead><tr>
          <th>Кампания</th><th>Код</th><th>Ссылка</th><th class="num">Клиентов</th>
        </tr></thead>
        <tbody id="utmrows"><tr><td colspan="4" class="muted">Загрузка…</td></tr></tbody>
      </table>
    </section>
  </main>
  </div>

  <div id="overlay">
    <div id="drawer">
      <div id="dhead">
        <div class="top">
          <div><h2 id="dname">—</h2><div class="sub" id="dsub"></div></div>
          <button id="close">×</button>
        </div>
        <div class="costline" id="dcost"></div>
      </div>
      <div id="log"></div>
    </div>
  </div>

<script>
const API = '__API_BASE__';
const SHOW_TOKENS = __SHOW_TOKENS__;
const SHOW_WALLET = __SHOW_WALLET__;
if(!SHOW_TOKENS) document.documentElement.classList.add('no-tokens');
const qs = new URLSearchParams(location.search);
let token = qs.get('token') || localStorage.getItem(API + '_token') || '';
let rate = 0;
let data = [];
let sortK = 'last_ts', sortDir = -1;

function money(usd){
  usd = usd || 0;
  const dollars = '$' + usd.toFixed(4);
  if(rate>0){
    const rub = usd * rate;
    const r = rub >= 100 ? Math.round(rub).toLocaleString('ru-RU') : rub.toFixed(2);
    // основная сумма в рублях, доллары — мелким серым рядом
    return `${r} ₽ <span style="opacity:.45;font-size:.85em">${dollars}</span>`;
  }
  return dollars;
}
function fmt(ts){ if(!ts) return '—'; const d=new Date(ts*1000); return d.toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}); }
function label(from){ return from==='manager'?'Менеджер':from==='bot'?'Соня (бот)':'Клиент'; }
function esc(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
const SRC_LABEL = { tg:'Telegram', web:'Сайт', max:'MAX' };
function srcBadge(ch){ const c=SRC_LABEL[ch]?ch:'tg'; return `<span class="badge src ${c}">${SRC_LABEL[c]}</span>`; }
// Итоговый источник клиента для сводки: для TG — UTM-метка кампании
// (или «Прямой вход» / «Не размечен»), для веба и MAX — сам канал.
function sourceLabel(u){
  const ch = u.channel || 'tg';
  if(ch === 'web') return 'Сайт';
  if(ch === 'max') return 'MAX';
  return u.utm_label || 'Не размечен';
}

async function loadUsers(){
  let r;
  try { r = await fetch(`${API}/api/users?token=${encodeURIComponent(token)}`); }
  catch(e){ document.getElementById('empty').textContent='Ошибка сети'; return; }
  if(r.status===403){ promptToken(); return; }
  const j = await r.json();
  rate = j.usd_rub_rate || 0;
  data = j.users || [];
  renderStats(j.totals);
  renderWallet(j.wallet);
  renderSources();
  render();
  loadAnalysis();
}

function renderSources(){
  const sec = document.getElementById('srcbreak');
  if(!data.length){ sec.style.display='none'; return; }
  const counts = {};
  for(const u of data){ const k = sourceLabel(u); counts[k] = (counts[k]||0)+1; }
  const items = Object.entries(counts)
    .map(([label,count])=>({label,count}))
    .sort((a,b)=> b.count-a.count || a.label.localeCompare(b.label));
  sec.style.display = 'block';
  const n = data.length;
  document.getElementById('srcsub').textContent =
    `${n} ${plural(n,'клиент','клиента','клиентов')} · ${items.length} ${plural(items.length,'источник','источника','источников')}`;
  const max = Math.max(1, ...items.map(i=>i.count));
  document.getElementById('srcitems').innerHTML = items.map(i=>`
    <div class="aitem">
      <span class="albl" title="${esc(i.label)}">${esc(i.label)}</span>
      <span class="abar"><i style="width:${Math.round(i.count/max*100)}%"></i></span>
      <span class="acnt">${i.count}</span>
    </div>`).join('');
}

function plural(n,a,b,c){ n=Math.abs(n)%100; const n1=n%10; if(n>10&&n<20)return c; if(n1>1&&n1<5)return b; if(n1===1)return a; return c; }

async function loadAnalysis(){
  let r;
  try { r = await fetch(`${API}/api/analysis?token=${encodeURIComponent(token)}`); }
  catch(e){ return; }
  if(!r.ok) return;
  renderAnalysis(await r.json());
}

function renderAnalysis(j){
  const sec = document.getElementById('analysis');
  const groups = j.groups || [];
  if(!groups.length){ sec.style.display='none'; return; }
  sec.style.display = 'block';
  const n = j.total || 0;
  document.getElementById('asub').textContent =
    `по сообщениям клиентов · ${n} ${plural(n,'диалог','диалога','диалогов')}`;
  document.getElementById('agrid').innerHTML = groups.map(g=>{
    const items = (g.items||[]).filter(i=>i.count>0);
    const max = Math.max(1, ...items.map(i=>i.count));
    const body = items.length ? items.map(i=>`
      <div class="aitem">
        <span class="albl">${esc(i.label)}</span>
        <span class="abar"><i style="width:${Math.round(i.count/max*100)}%"></i></span>
        <span class="acnt">${i.count}</span>
      </div>`).join('') : '<div class="aempty">Нет упоминаний</div>';
    return `<div class="acard"><h3>${esc(g.name)}</h3>${body}</div>`;
  }).join('');
}

function rub(x){ x = x || 0; const v = Math.abs(x) >= 100 ? Math.round(x).toLocaleString('ru-RU') : x.toFixed(2); return v + ' ₽'; }

function renderWallet(w){
  const el = document.getElementById('wallet');
  if(!SHOW_WALLET || !w){ if(el) el.style.display='none'; return; }
  el.style.display = 'flex';
  const low = (w.remaining || 0) <= 0;
  el.innerHTML = `
    <div>
      <span class="wlabel">Остаток на счёте</span>
      <span class="wrem ${low?'neg':''}">${rub(w.remaining)}</span>
    </div>
    <div class="wmeta">
      <div>Пополнено: ${rub(w.balance)}</div>
      <div>Списано: ${rub(w.spent)}</div>
    </div>`;
}

function renderStats(t){
  if(!t) return;
  document.getElementById('stats').innerHTML = [
    ['Пользователей', t.users],
    ['Сообщений', t.messages],
    ...(SHOW_TOKENS ? [['LLM-запросов', t.calls], ['Токенов', (t.tokens||0).toLocaleString('ru-RU')]] : []),
    ['Затраты всего', money(t.cost)],
  ].map(([s,v])=>`<div class="stat"><b>${v}</b><span>${s}</span></div>`).join('');
}

function render(){
  const q = (document.getElementById('search').value||'').toLowerCase().trim();
  let rows = data.filter(u => !q ||
    (u.name||'').toLowerCase().includes(q) ||
    (u.phone||'').toLowerCase().includes(q) ||
    String(u.tg_id).includes(q));
  rows.sort((a,b)=>{
    let x=a[sortK], y=b[sortK];
    if(typeof x==='string'||typeof y==='string'){ x=(x||'').toString(); y=(y||'').toString(); return x.localeCompare(y)*sortDir; }
    return ((x||0)-(y||0))*sortDir;
  });
  const tb = document.getElementById('rows');
  if(!rows.length){ tb.innerHTML='<tr><td colspan="9" id="empty">Нет диалогов</td></tr>'; return; }
  tb.innerHTML = rows.map(u=>`
    <tr data-id="${u.tg_id}">
      <td>${esc(u.name||'Без имени')}<div class="muted" style="font-size:11px">id ${u.tg_id}</div></td>
      <td>${esc(u.phone||'—')}</td>
      <td>${srcBadge(u.channel)}</td>
      <td><span class="badge ${u.state}">${u.state==='handoff'?'у флориста':'бот'}</span></td>
      <td class="num">${u.msg_count||0}</td>
      <td class="num">${u.llm_calls||0}</td>
      <td class="num col-tokens">${(u.tokens||0).toLocaleString('ru-RU')}</td>
      <td class="num cost">${money(u.cost)}</td>
      <td class="num muted">${fmt(u.last_ts)}</td>
    </tr>`).join('');
  tb.querySelectorAll('tr').forEach(tr=>tr.onclick=()=>openDialog(tr.dataset.id));
}

document.querySelectorAll('th[data-k]').forEach(th=>{
  th.onclick=()=>{ const k=th.dataset.k; sortDir = sortK===k ? -sortDir : (k==='name'||k==='phone'?1:-1); sortK=k; render(); };
});
document.getElementById('search').oninput = render;

async function openDialog(tgId){
  const ov=document.getElementById('overlay'); ov.classList.add('open');
  document.getElementById('log').innerHTML='<div id="empty">Загрузка…</div>';
  const r = await fetch(`${API}/api/dialog?tg_id=${tgId}&token=${encodeURIComponent(token)}`);
  const j = await r.json();
  const u=j.user||{}, c=j.cost||{};
  document.getElementById('dname').textContent = u.name||'Без имени';
  document.getElementById('dsub').innerHTML = `id ${u.tg_id}${u.phone?' · '+esc(u.phone):''}${u.amo_lead_id?' · сделка #'+u.amo_lead_id:''} · <span class="muted">источник:</span> ${esc(sourceLabel(u))}`;
  document.getElementById('dcost').innerHTML = [
    ['Стоимость диалога', money(c.cost)],
    ...(SHOW_TOKENS ? [['Токенов', (c.tokens||0).toLocaleString('ru-RU')]] : []),
    ['LLM-запросов', c.calls||0],
  ].map(([s,v])=>`<div><span class="muted">${s}</span><b>${v}</b></div>`).join('');
  const log=document.getElementById('log');
  const msgs=j.messages||[];
  if(!msgs.length){ log.innerHTML='<div id="empty">Сообщений нет</div>'; return; }
  log.innerHTML='';
  for(const m of msgs){
    const side = m.from==='manager'?'right':'left';
    const row=document.createElement('div'); row.className='row '+side+' '+m.from;
    row.innerHTML=`<div><div class="meta">${label(m.from)} · ${fmt(m.ts)}</div><div class="bubble"></div></div>`;
    row.querySelector('.bubble').textContent=m.text;
    log.appendChild(row);
  }
  log.scrollTop=0;
}
document.getElementById('close').onclick=()=>document.getElementById('overlay').classList.remove('open');
document.getElementById('overlay').onclick=e=>{ if(e.target.id==='overlay') e.currentTarget.classList.remove('open'); };

function promptToken(){
  const t = prompt('Введите токен доступа (ADMIN_TOKEN):');
  if(t){ token=t; localStorage.setItem(API + '_token', t); loadUsers(); }
  else document.getElementById('empty').textContent='Доступ запрещён';
}

// ---- переключение разделов (левое меню) ----
document.querySelectorAll('.navitem[data-panel]').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.navitem').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel-'+b.dataset.panel).classList.add('active');
  if(b.dataset.panel==='utm') loadUtm();
});
document.getElementById('logout').onclick=()=>{
  localStorage.removeItem(API + '_token'); token=''; promptToken();
};

// ---- раздел «Реклама (UTM)» ----
let utmData = { campaigns:[], link_base:'', bot_username:'' };
const UTM_RE = /^[A-Za-z0-9_-]{1,64}$/;

async function loadUtm(){
  let r;
  try { r = await fetch(`${API}/api/utm?token=${encodeURIComponent(token)}`); }
  catch(e){ return; }
  if(r.status===403){ promptToken(); return; }
  if(!r.ok) return;
  utmData = await r.json();
  renderUtm();
}

function renderUtm(){
  const tb = document.getElementById('utmrows');
  const cs = utmData.campaigns || [];
  if(!cs.length){ tb.innerHTML='<tr><td colspan="4" class="muted">Пока нет меток — создайте первую выше</td></tr>'; return; }
  const base = utmData.link_base || '';
  tb.innerHTML = cs.map(c=>{
    const link = base ? base + encodeURIComponent(c.payload) : '';
    const linkCell = base
      ? `<span class="utm-link"><a href="${esc(link)}" target="_blank" rel="noopener">${esc(link)}</a><button class="copy" data-link="${esc(link)}">Копировать</button></span>`
      : '<span class="utm-hint">ссылка недоступна — задайте TELEGRAM_BOT_USERNAME</span>';
    return `<tr>
      <td>${esc(c.label)}</td>
      <td class="muted">${esc(c.payload)}</td>
      <td>${linkCell}</td>
      <td class="num">${c.count||0}</td>
    </tr>`;
  }).join('');
  tb.querySelectorAll('.copy').forEach(btn=>btn.onclick=()=>copyLink(btn));
}

function copyLink(btn){
  const link = btn.dataset.link;
  const done = ()=>{ const t=btn.textContent; btn.textContent='Скопировано'; setTimeout(()=>btn.textContent=t,1200); };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(link).then(done).catch(()=>fallbackCopy(link,done));
  } else fallbackCopy(link,done);
}
function fallbackCopy(text,done){
  const i=document.createElement('textarea'); i.value=text; document.body.appendChild(i); i.select();
  try{ document.execCommand('copy'); done(); }catch(e){} document.body.removeChild(i);
}

async function createUtm(){
  const err = document.getElementById('utmerr');
  const payload = (document.getElementById('utmpayload').value||'').trim();
  const label = (document.getElementById('utmlabel').value||'').trim();
  err.textContent='';
  if(!UTM_RE.test(payload)){ err.textContent='Код метки: латинские буквы, цифры, _ и - (до 64 символов)'; return; }
  if(!label){ err.textContent='Укажите название кампании'; return; }
  let r;
  try {
    r = await fetch(`${API}/api/utm?token=${encodeURIComponent(token)}&payload=${encodeURIComponent(payload)}&label=${encodeURIComponent(label)}`, {method:'POST'});
  } catch(e){ err.textContent='Ошибка сети'; return; }
  if(r.status===403){ promptToken(); return; }
  if(!r.ok){ err.textContent='Не удалось сохранить метку'; return; }
  document.getElementById('utmpayload').value='';
  document.getElementById('utmlabel').value='';
  loadUtm();
}
document.getElementById('utmadd').onclick=createUtm;

loadUsers();
setInterval(loadUsers, 15000);
</script>
</body>
</html>"""
