"""HTTP API для виджета amoCRM (чат-панель в карточке сделки).

Эндпоинты (вызываются JS виджета из браузера менеджера):
  GET  /widget/messages?lead_id=..&token=..   -> история переписки по сделке
  POST /widget/send  {lead_id, text, token}   -> отправить сообщение клиенту в Telegram

Доступ защищён общим секретом WIDGET_TOKEN. CORS разрешён для доменов amoCRM.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiohttp import web

from app.config import settings
from app.db.storage import storage

log = logging.getLogger(__name__)

# как отображать роли в виджете
_ROLE_MAP = {"user": "client", "assistant": "bot", "manager": "manager"}


def _cors_headers(request: web.Request) -> dict:
    origin = request.headers.get("Origin", "")
    allow = origin if origin.endswith(".amocrm.ru") else "*"
    return {
        "Access-Control-Allow-Origin": allow,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _check_token(token: str | None) -> bool:
    # если секрет не задан — пропускаем (удобно для отладки)
    return not settings.widget_token or token == settings.widget_token


async def _preflight(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=_cors_headers(request))


async def get_messages(request: web.Request) -> web.Response:
    headers = _cors_headers(request)
    if not _check_token(request.query.get("token")):
        return web.json_response({"error": "forbidden"}, status=403, headers=headers)
    try:
        lead_id = int(request.query.get("lead_id", ""))
    except ValueError:
        return web.json_response({"error": "bad lead_id"}, status=400, headers=headers)

    user = await storage.find_by_lead_id(lead_id)
    if not user:
        return web.json_response({"messages": [], "found": False}, headers=headers)

    rows = await storage.history_full(user.tg_id, limit=300)
    messages = [
        {"from": _ROLE_MAP.get(r["role"], r["role"]), "text": r["content"], "ts": r["ts"]}
        for r in rows
    ]
    return web.json_response(
        {"messages": messages, "found": True, "client_name": user.name},
        headers=headers,
    )


async def send_message(request: web.Request) -> web.Response:
    headers = _cors_headers(request)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400, headers=headers)

    if not _check_token(data.get("token")):
        return web.json_response({"error": "forbidden"}, status=403, headers=headers)

    try:
        lead_id = int(data.get("lead_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad lead_id"}, status=400, headers=headers)
    text = (data.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty text"}, status=400, headers=headers)

    user = await storage.find_by_lead_id(lead_id)
    if not user:
        return web.json_response({"error": "lead not linked"}, status=404, headers=headers)

    bot: Bot = request.app["bot"]
    try:
        await bot.send_message(user.tg_id, text)
    except Exception as exc:  # noqa: BLE001
        log.exception("Виджет: не удалось отправить сообщение клиенту: %s", exc)
        return web.json_response({"error": "telegram failed"}, status=502, headers=headers)

    await storage.add_message(user.tg_id, "manager", text)
    return web.json_response({"ok": True}, headers=headers)


async def get_panel(request: web.Request) -> web.Response:
    """HTML-страница чат-панели (встраивается в карточку сделки виджетом или
    открывается в браузере менеджером)."""
    return web.Response(text=_PANEL_HTML, content_type="text/html")


def add_widget_routes(app: web.Application) -> None:
    app.router.add_get("/widget/messages", get_messages)
    app.router.add_post("/widget/send", send_message)
    app.router.add_get("/widget/panel", get_panel)
    app.router.add_route("OPTIONS", "/widget/messages", _preflight)
    app.router.add_route("OPTIONS", "/widget/send", _preflight)


_PANEL_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Чат с клиентом</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#1f2733; color:#e6edf3; display:flex; flex-direction:column; }
  #head { padding:10px 14px; background:#171e27; font-size:14px; font-weight:600; border-bottom:1px solid #2b3947; }
  #log { flex:1; overflow-y:auto; padding:12px; display:flex; flex-direction:column; gap:8px; }
  .row { display:flex; }
  .row.right { justify-content:flex-end; }
  .bubble { max-width:78%; padding:8px 11px; border-radius:12px; font-size:13px; line-height:1.35; white-space:pre-wrap; word-wrap:break-word; }
  .client .bubble { background:#2b3947; border-bottom-left-radius:4px; }
  .bot .bubble { background:#243244; color:#9fb3c8; border-bottom-left-radius:4px; }
  .manager .bubble { background:#2f6feb; color:#fff; border-bottom-right-radius:4px; }
  .meta { font-size:10px; opacity:.6; margin:0 4px 2px; }
  #foot { display:flex; padding:8px; gap:6px; background:#171e27; border-top:1px solid #2b3947; }
  #msg { flex:1; resize:none; border:1px solid #2b3947; border-radius:8px; background:#0f141b; color:#e6edf3; padding:8px; font-size:13px; height:38px; }
  #send { border:0; border-radius:8px; background:#2f6feb; color:#fff; padding:0 16px; font-weight:600; cursor:pointer; }
  #send:disabled { opacity:.5; cursor:default; }
  #empty { opacity:.5; text-align:center; margin-top:20px; font-size:13px; }
</style>
</head>
<body>
  <div id="head">Чат с клиентом</div>
  <div id="log"><div id="empty">Загрузка…</div></div>
  <div id="foot">
    <textarea id="msg" placeholder="Написать клиенту…"></textarea>
    <button id="send">→</button>
  </div>
<script>
const qs = new URLSearchParams(location.search);
const leadId = qs.get('lead_id');
const token = qs.get('token') || '';
const log = document.getElementById('log');
const head = document.getElementById('head');
let lastCount = -1;

function fmt(ts){ const d = new Date(ts*1000); return d.toLocaleString('ru-RU',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}); }
function label(from){ return from==='manager' ? 'Вы' : from==='bot' ? 'Соня (бот)' : 'Клиент'; }

async function load(){
  if(!leadId){ log.innerHTML='<div id=empty>Не передан lead_id</div>'; return; }
  try{
    const r = await fetch(`/widget/messages?lead_id=${encodeURIComponent(leadId)}&token=${encodeURIComponent(token)}`);
    const data = await r.json();
    const msgs = data.messages || [];
    if(data.client_name) head.textContent = 'Чат с клиентом · ' + data.client_name;
    if(msgs.length === lastCount) return;
    lastCount = msgs.length;
    if(!msgs.length){ log.innerHTML='<div id=empty>Сообщений пока нет</div>'; return; }
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    log.innerHTML = '';
    for(const m of msgs){
      const side = m.from==='manager' ? 'right' : 'left';
      const row = document.createElement('div');
      row.className = 'row '+side+' '+m.from;
      row.innerHTML = `<div><div class="meta">${label(m.from)} · ${fmt(m.ts)}</div><div class="bubble"></div></div>`;
      row.querySelector('.bubble').textContent = m.text;
      log.appendChild(row);
    }
    if(atBottom) log.scrollTop = log.scrollHeight;
  }catch(e){ /* тихо повторим */ }
}

async function send(){
  const ta = document.getElementById('msg');
  const btn = document.getElementById('send');
  const text = ta.value.trim();
  if(!text) return;
  btn.disabled = true;
  try{
    const r = await fetch('/widget/send', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({lead_id: Number(leadId), text, token})});
    if(r.ok){ ta.value=''; lastCount=-1; await load(); }
    else { alert('Не удалось отправить'); }
  }catch(e){ alert('Ошибка сети'); }
  btn.disabled = false;
}

document.getElementById('send').onclick = send;
document.getElementById('msg').addEventListener('keydown', e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); }});
load();
setInterval(load, 4000);
</script>
</body>
</html>"""
