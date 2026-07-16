"""HTTP API + фронтенд веб-виджета «Соня» для сайта cvety-naberezhnye.ru.

Тот же мозг, что и у Telegram-бота (app.llm.consultant + app.services.handoff),
но канал общения — чат-пузырь на сайте. Идентификация посетителя: браузер хранит
случайный uuid в localStorage, серверу он маппится на стабильный отрицательный
uid (см. storage.web_session_uid), который дальше используется везде как «tg_id».

Эндпоинты (вызываются JS виджета из браузера):
  POST /web/start    {uuid}          -> история диалога + приветствие + состояние
  POST /web/message  {uuid, text}    -> ответ Сони (или подтверждение хэндоффа)
  GET  /web/stream?uuid=..           -> SSE: ответы флориста в режиме handoff
  GET  /web/widget.js                -> сам виджет (тонкий, самодостаточный)
  GET  /web/demo                     -> тестовая страница с виджетом

Заявки в amoCRM создаются тем же handoff.do_handoff, что и у Telegram-бота —
никакой отдельной логики заявок здесь нет.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import web

from app.bot.texts import FALLBACK_ERROR, GREETING
from app.config import settings
from app.db.storage import STATE_HANDOFF, storage
from app.llm import consultant
from app.services import handoff

log = logging.getLogger(__name__)

# как отображать роли из БД в виджете
_ROLE_MAP = {"user": "me", "assistant": "bot", "manager": "manager"}

# ---------- SSE-хаб: доставка ответов флориста в браузер ----------
# uid -> набор очередей открытых SSE-соединений этого посетителя (вкладок).
_subscribers: dict[int, set[asyncio.Queue]] = {}

# сериализация обработки сообщений по посетителю (как _locks в Telegram-хэндлерах)
_locks: dict[int, asyncio.Lock] = {}
# простой троттлинг: uid -> список меток времени недавних сообщений
_rate: dict[int, list[float]] = {}


def _lock_for(uid: int) -> asyncio.Lock:
    lock = _locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _locks[uid] = lock
    return lock


async def push_to_web(uid: int, text: str) -> None:
    """Разослать текст (ответ флориста) во все открытые SSE-соединения посетителя."""
    queues = _subscribers.get(uid)
    if not queues:
        return
    for q in list(queues):
        try:
            q.put_nowait(text)
        except asyncio.QueueFull:  # pragma: no cover — очередь без лимита
            pass


def _rate_ok(uid: int) -> bool:
    """True, если посетитель не превысил лимит сообщений за окно."""
    limit = settings.web_rate_limit
    if limit <= 0:
        return True
    now = time.time()
    window = settings.web_rate_window_sec
    hits = [t for t in _rate.get(uid, []) if now - t < window]
    hits.append(now)
    _rate[uid] = hits
    return len(hits) <= limit


# ---------- CORS ----------
def _cors_headers(request: web.Request) -> dict:
    origin = request.headers.get("Origin", "").rstrip("/")
    allowed = settings.web_origins
    if "*" in allowed:
        allow = origin or "*"
    elif origin and origin in allowed:
        allow = origin
    else:
        allow = ""  # чужой домен — браузер заблокирует ответ
    headers = {"Vary": "Origin"}
    if allow:
        headers.update(
            {
                "Access-Control-Allow-Origin": allow,
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )
    return headers


async def _preflight(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=_cors_headers(request))


def _greeting() -> str:
    return settings.web_greeting.strip() or GREETING


# ---------- эндпоинты ----------
async def web_start(request: web.Request) -> web.Response:
    headers = _cors_headers(request)
    if not settings.web_enabled:
        return web.json_response({"error": "disabled"}, status=403, headers=headers)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    uuid = (data.get("uuid") or "").strip()
    if not uuid or len(uuid) > 100:
        return web.json_response({"error": "bad uuid"}, status=400, headers=headers)

    uid, is_new = await storage.web_session_uid(uuid)

    if is_new:
        # новая сессия: метка контекста + приветствие в истории (как /start в Telegram)
        await storage.mark_session_start(uid)
        greeting = _greeting()
        await storage.add_message(uid, "assistant", greeting)

    user = await storage.get_user(uid)
    since = user.context_since if user else 0
    rows = await storage.history_full(uid, limit=200, since=since)
    messages = [
        {"from": _ROLE_MAP.get(r["role"], r["role"]), "text": r["content"], "ts": r["ts"]}
        for r in rows
    ]
    return web.json_response(
        {
            "uuid": uuid,
            "messages": messages,
            "state": user.state if user else "consult",
        },
        headers=headers,
    )


async def web_message(request: web.Request) -> web.Response:
    headers = _cors_headers(request)
    if not settings.web_enabled:
        return web.json_response({"error": "disabled"}, status=403, headers=headers)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400, headers=headers)

    uuid = (data.get("uuid") or "").strip()
    text = (data.get("text") or "").strip()
    if not uuid:
        return web.json_response({"error": "bad uuid"}, status=400, headers=headers)
    if not text:
        return web.json_response({"error": "empty"}, status=400, headers=headers)
    if len(text) > 2000:
        text = text[:2000]

    uid, _ = await storage.web_session_uid(uuid)
    if not _rate_ok(uid):
        return web.json_response(
            {"reply": "Слишком много сообщений подряд — секундочку 🙏"},
            status=429,
            headers=headers,
        )

    # тот же порядок обработки, что и в Telegram-хэндлере on_text
    async with _lock_for(uid):
        user = await storage.get_or_create_user(uid, channel="web")

        # режим живого чата с флористом — Соня молчит, пересылаем менеджеру
        if user.state == STATE_HANDOFF:
            await handoff.forward_client_message(uid, text)
            return web.json_response({"ok": True, "mode": "handoff"}, headers=headers)

        try:
            result = await consultant.generate(uid, text)
        except Exception as exc:  # noqa: BLE001
            log.exception("web: ошибка генерации ответа: %s", exc)
            return web.json_response({"reply": FALLBACK_ERROR}, headers=headers)

        await storage.add_message(uid, "user", text)

        if result.handoff is not None:
            # создаём сделку/контакт/чат в amoCRM — ровно как в Telegram
            reply = await handoff.do_handoff(uid, result.handoff)
            await storage.add_message(uid, "assistant", reply)
            return web.json_response(
                {"reply": reply, "handoff": True}, headers=headers
            )

        reply = result.text or FALLBACK_ERROR
        await storage.add_message(uid, "assistant", reply)
        return web.json_response({"reply": reply}, headers=headers)


async def web_stream(request: web.Request) -> web.StreamResponse:
    """SSE: держим соединение и шлём в браузер ответы флориста (режим handoff)."""
    uuid = (request.query.get("uuid") or "").strip()
    if not uuid:
        return web.json_response(
            {"error": "bad uuid"}, status=400, headers=_cors_headers(request)
        )
    uid, _ = await storage.web_session_uid(uuid)

    resp = web.StreamResponse(
        headers={
            **_cors_headers(request),
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # отключить буферизацию на nginx
        }
    )
    await resp.prepare(request)

    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(uid, set()).add(queue)
    try:
        await resp.write(b": connected\n\n")
        while True:
            try:
                text = await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")  # heartbeat, чтобы соединение жило
                continue
            payload = json.dumps(
                {"from": "manager", "text": text, "ts": time.time()},
                ensure_ascii=False,
            )
            await resp.write(f"data: {payload}\n\n".encode("utf-8"))
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("web SSE закрыт: %s", exc)
    finally:
        subs = _subscribers.get(uid)
        if subs:
            subs.discard(queue)
            if not subs:
                _subscribers.pop(uid, None)
    return resp


async def web_widget_js(request: web.Request) -> web.Response:
    return web.Response(
        text=_WIDGET_JS,
        content_type="application/javascript",
        charset="utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


async def web_demo(request: web.Request) -> web.Response:
    return web.Response(text=_DEMO_HTML, content_type="text/html")


# Ссылки фрейма RescalesAI вокруг чата (демо-обёртка). ЗАПОЛНИТЬ реальными
# значениями. Пустая строка "" — кнопка/ссылка скрывается.
_FRAME_LINKS = {
    "__RS_SITE__": "https://rescales.ai",
    "__RS_SOLUTIONS__": "https://rescales.ai/#products",
    "__RS_TG__": "https://t.me/rescalesai",
    "__RS_WA__": "https://wa.me/79933092355",
}


async def web_chat(request: web.Request) -> web.Response:
    """Полноэкранная страница-чат с Соней (без пузыря) — для теста и прямых ссылок."""
    from app.branding import RESCALES_LOGO_SVG
    html = _CHAT_HTML.replace("<!--RS_LOGO-->", RESCALES_LOGO_SVG)
    for token, url in _FRAME_LINKS.items():
        html = html.replace(token, url or "#")
    return web.Response(text=html, content_type="text/html")


def add_web_routes(app: web.Application) -> None:
    app.router.add_post("/web/start", web_start)
    app.router.add_post("/web/message", web_message)
    app.router.add_get("/web/stream", web_stream)
    app.router.add_get("/web/widget.js", web_widget_js)
    app.router.add_get("/web/demo", web_demo)
    app.router.add_get("/web/chat", web_chat)
    app.router.add_route("OPTIONS", "/web/start", _preflight)
    app.router.add_route("OPTIONS", "/web/message", _preflight)


# =====================================================================
#  Фронтенд: самодостаточный виджет (пузырь + панель чата).
#  Конфиг читается из data-атрибутов тега <script>. API-хост по умолчанию —
#  origin, с которого загружен сам widget.js.
# =====================================================================
_WIDGET_JS = r"""
(function () {
  if (window.__sonyaWidgetLoaded) return;
  window.__sonyaWidgetLoaded = true;

  var script = document.currentScript;
  function attr(name, def) {
    return (script && script.getAttribute(name)) || def;
  }
  var API = (attr('data-api', '') || new URL(script.src).origin).replace(/\/$/, '');
  var COLOR = attr('data-color', '#d6336c');
  var TITLE = attr('data-title', 'Соня · ЦветоМира');
  var SUBTITLE = attr('data-subtitle', 'Онлайн-консультант по букетам');
  var POS = attr('data-position', 'right'); // right | left
  var STORAGE_KEY = 'sonya_web_uuid';

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0, v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }
  var UUID = localStorage.getItem(STORAGE_KEY);
  if (!UUID) { UUID = uuid(); localStorage.setItem(STORAGE_KEY, UUID); }

  var side = POS === 'left' ? 'left:20px;' : 'right:20px;';
  var sidePanel = POS === 'left' ? 'left:20px;' : 'right:20px;';
  var css = `
  :host{ all: initial; }
  .snya-wrap{ font-family:system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;
    font-size:14px; line-height:1.4; color:#222; font-weight:400; letter-spacing:normal;
    text-transform:none; -webkit-font-smoothing:antialiased; }
  .snya-wrap *{ box-sizing:border-box; font-family:inherit; margin:0; padding:0;
    text-transform:none; letter-spacing:normal; }
  .snya-btn{position:fixed;bottom:20px;${side}z-index:2147483000;width:60px;height:60px;
    border-radius:50%;background:${COLOR};border:none;cursor:pointer;box-shadow:0 6px 22px rgba(0,0,0,.25);
    display:flex;align-items:center;justify-content:center;transition:transform .15s;}
  .snya-btn:hover{transform:scale(1.06);}
  .snya-btn svg{width:28px;height:28px;fill:#fff;}
  .snya-badge{position:absolute;top:-2px;right:-2px;min-width:18px;height:18px;border-radius:9px;
    background:#fff;color:${COLOR};font:700 11px/18px system-ui;text-align:center;padding:0 4px;display:none;}
  .snya-panel{position:fixed;bottom:92px;${sidePanel}z-index:2147483000;width:370px;max-width:calc(100vw - 40px);
    height:560px;max-height:calc(100vh - 120px);background:#fff;border-radius:16px;overflow:hidden;
    box-shadow:0 12px 48px rgba(0,0,0,.28);display:none;flex-direction:column;}
  .snya-open .snya-panel{display:flex;}
  .snya-head{background:${COLOR};color:#fff;padding:14px 16px;display:flex;align-items:center;gap:10px;flex:0 0 auto;}
  .snya-head .snya-av{width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,.22);
    display:flex;align-items:center;justify-content:center;font-size:20px;flex:0 0 auto;}
  .snya-head .snya-t{font-weight:700;font-size:15px;line-height:1.2;}
  .snya-head .snya-s{font-size:12px;opacity:.85;line-height:1.3;margin-top:1px;}
  .snya-x{margin-left:auto;background:none;border:none;color:#fff;font-size:22px;cursor:pointer;
    opacity:.85;line-height:1;width:28px;height:28px;flex:0 0 auto;}
  .snya-x:hover{opacity:1;}
  .snya-log{flex:1 1 auto;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px;background:#f7f7f9;}
  .snya-row{display:flex;}
  .snya-row.me{justify-content:flex-end;}
  .snya-b{max-width:80%;padding:9px 12px;border-radius:14px;font-size:14px;line-height:1.4;
    white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;}
  .snya-row.me .snya-b{background:${COLOR};color:#fff;border-bottom-right-radius:4px;}
  .snya-row.bot .snya-b,.snya-row.manager .snya-b{background:#fff;color:#222;border:1px solid #ececf0;border-bottom-left-radius:4px;}
  .snya-row.manager .snya-b{border-left:3px solid ${COLOR};}
  .snya-b a{color:inherit;text-decoration:underline;}
  .snya-row.me .snya-b a{color:#fff;}
  .snya-typing{font-size:12px;color:#888;padding:2px 14px 6px;display:none;flex:0 0 auto;background:#f7f7f9;}
  .snya-foot{display:flex;align-items:flex-end;gap:8px;padding:10px;border-top:1px solid #ececf0;background:#fff;flex:0 0 auto;}
  .snya-in{flex:1 1 auto;resize:none;border:1px solid #dcdce2;border-radius:10px;padding:9px 11px;
    font-size:14px;line-height:1.35;height:40px;min-height:40px;max-height:110px;outline:none;
    color:#222;background:#fff;display:block;}
  .snya-in::placeholder{color:#9aa0a6;opacity:1;}
  .snya-in:focus{border-color:${COLOR};}
  .snya-send{flex:0 0 44px;border:none;border-radius:10px;background:${COLOR};color:#fff;
    width:44px;height:40px;cursor:pointer;font-size:17px;display:flex;align-items:center;justify-content:center;}
  .snya-send:hover{filter:brightness(1.05);}
  .snya-send:disabled{opacity:.5;cursor:default;}
  @media (max-width:480px){
    .snya-panel{left:0;right:0;bottom:0;width:100%;max-width:100%;height:82vh;max-height:82vh;
      border-radius:16px 16px 0 0;}
    .snya-btn{bottom:16px;${side}width:56px;height:56px;}
  }
  `;
  // Shadow DOM: полная изоляция от CSS темы сайта (иначе стили WoodMart/Elementor
  // ломают поле ввода и кнопки). Виджет живёт в своём теневом дереве.
  var host = document.createElement('div');
  host.setAttribute('data-sonya-widget', '');
  host.style.cssText = 'all: initial;';  // нейтрализуем стили темы на самом хосте
  document.body.appendChild(host);
  var shadow = host.attachShadow({ mode: 'open' });
  var st = document.createElement('style'); st.textContent = css; shadow.appendChild(st);

  var root = document.createElement('div'); root.className = 'snya-wrap';
  root.innerHTML = `
    <button class="snya-btn" aria-label="Открыть чат">
      <span class="snya-badge">1</span>
      <svg viewBox="0 0 24 24"><path d="M12 3C6.5 3 2 6.9 2 11.7c0 2.4 1.2 4.6 3.1 6.1-.1 1.1-.6 2.4-1.6 3.4 1.6-.2 3.2-.8 4.4-1.8 1.2.4 2.6.6 4 .6 5.5 0 10-3.9 10-8.6S17.5 3 12 3z"/></svg>
    </button>
    <div class="snya-panel" role="dialog" aria-label="Чат с Соней">
      <div class="snya-head">
        <div class="snya-av">🌷</div>
        <div><div class="snya-t"></div><div class="snya-s"></div></div>
        <button class="snya-x" aria-label="Закрыть">×</button>
      </div>
      <div class="snya-log"></div>
      <div class="snya-typing">Соня печатает…</div>
      <div class="snya-foot">
        <textarea class="snya-in" placeholder="Напишите сообщение…" rows="1"></textarea>
        <button class="snya-send">➤</button>
      </div>
    </div>`;
  shadow.appendChild(root);

  var btn = root.querySelector('.snya-btn');
  var badge = root.querySelector('.snya-badge');
  var panel = root.querySelector('.snya-panel');
  var logEl = root.querySelector('.snya-log');
  var input = root.querySelector('.snya-in');
  var sendBtn = root.querySelector('.snya-send');
  var typingEl = root.querySelector('.snya-typing');
  root.querySelector('.snya-t').textContent = TITLE;
  root.querySelector('.snya-s').textContent = SUBTITLE;

  var started = false, es = null, unread = 0, seenTs = 0;

  function esc(s){ var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
  function linkify(s){
    return esc(s).replace(/(https?:\/\/[^\s<]+)/g, function(u){
      return '<a href="'+u+'" target="_blank" rel="noopener">'+u+'</a>'; });
  }
  function atBottom(){ return logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 80; }
  function scroll(){ logEl.scrollTop = logEl.scrollHeight; }

  function addMsg(from, text, ts){
    var stick = atBottom();
    var row = document.createElement('div');
    row.className = 'snya-row ' + (from === 'me' ? 'me' : from);
    row.innerHTML = '<div class="snya-b">' + linkify(text) + '</div>';
    logEl.appendChild(row);
    if (stick) scroll();
    if (ts && ts > seenTs) seenTs = ts;
  }

  function setTyping(on){ typingEl.style.display = on ? 'block' : 'none'; if(on) scroll(); }

  function bumpUnread(){
    if (panel.classList && root.classList.contains('snya-open')) return;
    unread++; badge.textContent = unread; badge.style.display = 'block';
  }

  async function api(path, body){
    var r = await fetch(API + path, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    return r.json();
  }

  async function start(){
    if (started) return; started = true;
    try {
      var data = await api('/web/start', {uuid: UUID});
      logEl.innerHTML = '';
      (data.messages || []).forEach(function(m){ addMsg(m.from, m.text, m.ts); });
      scroll();
    } catch(e){ addMsg('bot', 'Не удалось загрузить чат. Обновите страницу, пожалуйста.'); }
    openStream();
  }

  function openStream(){
    if (es) return;
    try {
      es = new EventSource(API + '/web/stream?uuid=' + encodeURIComponent(UUID));
      es.onmessage = function(ev){
        try {
          var m = JSON.parse(ev.data);
          if (m && m.text){ addMsg('manager', m.text, m.ts); bumpUnread(); }
        } catch(e){}
      };
      es.onerror = function(){ /* EventSource сам переподключится */ };
    } catch(e){}
  }

  async function send(){
    var text = input.value.trim();
    if (!text) return;
    input.value = ''; input.style.height = '40px';
    addMsg('me', text); scroll();
    sendBtn.disabled = true; setTyping(true);
    try {
      var data = await api('/web/message', {uuid: UUID, text: text});
      setTyping(false);
      if (data && data.reply) addMsg('bot', data.reply, data.ts);
      // если ушёл handoff — дальнейшие ответы придут от флориста по SSE
    } catch(e){
      setTyping(false);
      addMsg('bot', 'Ошибка сети, попробуйте ещё раз.');
    }
    sendBtn.disabled = false; input.focus();
  }

  function toggle(){
    var open = root.classList.toggle('snya-open');
    if (open){
      unread = 0; badge.style.display = 'none';
      start(); setTimeout(function(){ input.focus(); scroll(); }, 50);
    }
  }

  btn.addEventListener('click', toggle);
  root.querySelector('.snya-x').addEventListener('click', function(){ root.classList.remove('snya-open'); });
  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', function(e){
    if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
  });
  input.addEventListener('input', function(){
    input.style.height = '40px';
    input.style.height = Math.min(input.scrollHeight, 110) + 'px';
  });
})();
"""

_DEMO_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Демо — виджет Сони</title>
<style>body{font-family:system-ui;margin:0;padding:60px;background:#faf7f8;color:#333}
h1{color:#d6336c}</style></head>
<body>
<h1>🌷 Демо-страница виджета «Соня»</h1>
<p>Это тестовая страница. Чат-пузырь — в правом нижнем углу.</p>
<p>На боевом сайте этот же виджет подключит плагин WordPress одной строкой.</p>
<script src="/web/widget.js" data-title="Соня · ЦветоМира"
        data-subtitle="Онлайн-консультант по букетам" data-color="#d6336c"></script>
</body></html>"""


# =====================================================================
#  Полноэкранный чат-режим (страница /web/chat) — тот же API, без пузыря.
# =====================================================================
_CHAT_HTML = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Соня · ЦветоМира</title>
<style>
  :root{
    --red:#C1272D; --red-2:#E23B30; --red-ink:#9E1B20;
    --ink:#17171A; --muted:#6B6B73; --bg:#F3EFEE; --panel:#FFFFFF; --line:#ECE7E6;
    --grad:linear-gradient(135deg,#E23B30 0%,#C1272D 100%);
    --font:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;
  }
  *{ box-sizing:border-box; margin:0; padding:0; }
  html,body{ height:100%; }
  body{ font-family:var(--font); color:var(--ink); height:100dvh;
    background:
      radial-gradient(1200px 600px at 100% -10%, rgba(226,59,48,.08), transparent 60%),
      radial-gradient(900px 500px at -10% 110%, rgba(193,39,45,.07), transparent 60%),
      var(--bg);
    display:flex; flex-direction:column; }

  /* ---- Фрейм RescalesAI вокруг чата ---- */
  .topbar{ flex:0 0 auto; display:flex; align-items:center; justify-content:space-between;
    gap:16px; padding:16px 24px; }
  .brand{ display:flex; align-items:center; gap:14px; min-width:0; }
  .brand .rs-logo{ color:var(--ink); width:34px; height:34px; flex:0 0 auto; }
  .brand .rs-logo .rs-logo-svg{ width:34px; height:34px; display:block; }
  .brand .rs-name{ font-weight:800; font-size:18px; letter-spacing:-.02em; color:var(--ink); }
  .brand .rs-name b{ color:var(--red); font-weight:800; }
  .brand .rs-links{ display:flex; gap:8px; margin-left:6px; }
  .navbtn{ text-decoration:none; font-size:13px; font-weight:600; color:var(--muted);
    border:1.5px solid var(--line); background:#fff; padding:7px 13px; border-radius:18px;
    white-space:nowrap; transition:color .15s,border-color .15s,background .15s; }
  .navbtn:hover{ color:var(--red); border-color:var(--red); background:#FFF5F4; }
  .contacts{ display:flex; align-items:center; gap:10px; flex:0 0 auto; }
  .cbtn{ display:inline-flex; align-items:center; gap:8px; text-decoration:none;
    font-size:14px; font-weight:600; color:#fff; padding:9px 16px; border-radius:22px;
    box-shadow:0 6px 16px -6px rgba(0,0,0,.3); transition:filter .15s,transform .1s; }
  .cbtn:hover{ filter:brightness(1.05); } .cbtn:active{ transform:scale(.97); }
  .cbtn svg{ width:18px; height:18px; fill:#fff; }
  .cbtn.tg{ background:#229ED9; } .cbtn.wa{ background:#25D366; }

  .stage{ flex:1 1 auto; min-height:0; display:flex; align-items:center; justify-content:center;
    padding:8px 24px 28px; }

  .app{ width:100%; max-width:820px; height:100%; max-height:840px; background:var(--panel);
    border-radius:22px; box-shadow:0 24px 60px -12px rgba(23,23,26,.22), 0 6px 18px rgba(23,23,26,.06);
    display:flex; flex-direction:column; overflow:hidden; }

  header{ display:flex; align-items:center; gap:12px; padding:16px 20px;
    background:var(--panel); border-bottom:1px solid var(--line); flex:0 0 auto; }
  header .av{ width:46px; height:46px; border-radius:50%; background:var(--grad); color:#fff;
    display:flex; align-items:center; justify-content:center; flex:0 0 auto;
    box-shadow:0 6px 16px -4px rgba(193,39,45,.5); }
  header .av .flower{ width:28px; height:28px; display:block; }
  header .meta{ min-width:0; }
  header .t{ font-weight:700; font-size:17px; line-height:1.2; letter-spacing:-.01em;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  header .s{ font-size:12.5px; color:var(--muted); margin-top:2px; display:flex; align-items:center; gap:6px; }
  header .dot{ width:7px; height:7px; border-radius:50%; background:#2fbf6c;
    box-shadow:0 0 0 3px rgba(47,191,108,.16); flex:0 0 auto; }
  #restart{ margin-left:auto; flex:0 0 auto; display:flex; align-items:center; gap:7px;
    background:#fff; border:1.5px solid var(--line); color:var(--muted); cursor:pointer;
    height:38px; padding:0 14px; border-radius:20px; font-family:inherit; font-size:13px; font-weight:600;
    transition:color .15s,border-color .15s,background .15s; }
  #restart svg{ width:15px; height:15px; fill:currentColor; transition:transform .4s; }
  #restart:hover{ color:var(--red); border-color:var(--red); background:#FFF5F4; }
  #restart:hover svg{ transform:rotate(-180deg); }
  #restart .rlabel{ }
  @media (max-width:520px){ #restart .rlabel{ display:none; } #restart{ padding:0; width:38px; justify-content:center; } }

  #log{ flex:1 1 auto; overflow-y:auto; padding:20px; display:flex; flex-direction:column;
    gap:10px; background:#FBF9F9; }
  #log::-webkit-scrollbar{ width:9px; }
  #log::-webkit-scrollbar-thumb{ background:#E2DBDA; border-radius:9px; border:2px solid #FBF9F9; }
  .row{ display:flex; }
  .row.me{ justify-content:flex-end; }
  .b{ max-width:80%; padding:11px 15px; border-radius:18px; font-size:15px; line-height:1.5;
    white-space:pre-wrap; word-wrap:break-word; overflow-wrap:anywhere; }
  .row.me .b{ background:var(--grad); color:#fff; border-bottom-right-radius:5px;
    box-shadow:0 6px 16px -6px rgba(193,39,45,.5); }
  .row.bot .b,.row.manager .b{ background:#fff; color:var(--ink); border:1px solid var(--line);
    border-bottom-left-radius:5px; box-shadow:0 2px 6px rgba(23,23,26,.04); }
  .row.manager .b{ border-left:3px solid var(--red); }
  .b a{ color:var(--red-ink); text-decoration:underline; text-underline-offset:2px; font-weight:600; }
  .row.me .b a{ color:#fff; }

  #typing{ display:none; padding:0 22px 8px; flex:0 0 auto; background:#FBF9F9; }
  #typing .dots{ display:inline-flex; gap:4px; align-items:center; padding:9px 14px;
    background:#fff; border:1px solid var(--line); border-radius:16px; box-shadow:0 2px 6px rgba(23,23,26,.04); }
  #typing .dots i{ width:7px; height:7px; border-radius:50%; background:#C9BEBD; animation:bl 1.2s infinite; }
  #typing .dots i:nth-child(2){ animation-delay:.2s; } #typing .dots i:nth-child(3){ animation-delay:.4s; }
  @keyframes bl{ 0%,60%,100%{ opacity:.25; transform:translateY(0); } 30%{ opacity:1; transform:translateY(-3px); } }

  footer{ flex:0 0 auto; background:var(--panel); border-top:1px solid var(--line);
    padding:14px 16px; display:flex; align-items:flex-end; gap:10px; }
  #in{ flex:1 1 auto; resize:none; border:1.5px solid var(--line); border-radius:24px;
    padding:12px 18px; font-size:15px; line-height:1.4; height:48px; min-height:48px; max-height:140px;
    outline:none; font-family:inherit; color:var(--ink); background:#FBF9F9; transition:border-color .15s,background .15s; }
  #in::placeholder{ color:#A79F9E; }
  #in:focus{ border-color:var(--red); background:#fff; }
  #send{ flex:0 0 48px; width:48px; height:48px; border:none; border-radius:50%; background:var(--grad);
    color:#fff; cursor:pointer; display:flex; align-items:center; justify-content:center;
    box-shadow:0 6px 16px -4px rgba(193,39,45,.5); transition:filter .15s,transform .1s; }
  #send:hover{ filter:brightness(1.06); } #send:active{ transform:scale(.94); }
  #send:disabled{ opacity:.45; cursor:default; box-shadow:none; }
  #send svg{ width:20px; height:20px; fill:#fff; }

  @media (max-width:900px){
    .brand .rs-links{ display:none; }
  }
  @media (max-width:640px){
    .topbar{ padding:12px 14px; }
    .brand .rs-name{ font-size:16px; }
    .cbtn .clabel{ display:none; } .cbtn{ padding:9px; border-radius:50%; }
    .stage{ padding:0; }
    .app{ max-height:none; border-radius:0; box-shadow:none; }
  }
</style></head>
<body>
  <div class="topbar">
    <div class="brand">
      <a class="rs-logo" href="__RS_SITE__" target="_blank" rel="noopener" aria-label="RescalesAI"><!--RS_LOGO--></a>
      <span class="rs-name"><b>RE</b>scalesAI</span>
      <span class="rs-links">
        <a class="navbtn" href="__RS_SITE__" target="_blank" rel="noopener">Официальный сайт</a>
        <a class="navbtn" href="__RS_SOLUTIONS__" target="_blank" rel="noopener">Решения для бизнеса</a>
      </span>
    </div>
    <div class="contacts">
      <a class="cbtn tg" href="__RS_TG__" target="_blank" rel="noopener" title="Telegram">
        <svg viewBox="0 0 24 24"><path d="M21.9 4.3l-3.3 15.6c-.25 1.1-.9 1.37-1.82.85l-5.03-3.7-2.43 2.34c-.27.27-.5.5-1 .5l.36-5.1L18 6.1c.4-.36-.09-.56-.62-.2L6.9 12.7l-4.95-1.55c-1.08-.34-1.1-1.08.23-1.6l19.35-7.46c.9-.33 1.68.2 1.37 2.21z"/></svg>
        <span class="clabel">Telegram</span>
      </a>
      <a class="cbtn wa" href="__RS_WA__" target="_blank" rel="noopener" title="WhatsApp">
        <svg viewBox="0 0 24 24"><path d="M12 2a10 10 0 00-8.6 15l-1.3 4.7 4.8-1.26A10 10 0 1012 2zm5.8 14.2c-.24.68-1.4 1.3-1.94 1.34-.5.05-1.13.07-1.82-.11-.42-.13-.96-.31-1.65-.61-2.9-1.25-4.8-4.17-4.94-4.36-.15-.2-1.18-1.57-1.18-3s.75-2.13 1.02-2.42a1.07 1.07 0 01.77-.36c.19 0 .39 0 .56.01.18.01.42-.07.66.5.24.59.83 2.02.9 2.17.07.15.12.32.02.51-.1.2-.15.32-.29.49-.15.17-.31.39-.44.52-.15.15-.3.31-.13.6.17.3.76 1.25 1.63 2.02 1.12 1 2.06 1.31 2.35 1.46.3.15.47.12.64-.07.17-.2.74-.86.94-1.16.2-.3.4-.24.66-.15.27.1 1.7.8 2 .95.28.15.47.22.54.34.07.12.07.68-.17 1.35z"/></svg>
        <span class="clabel">WhatsApp</span>
      </a>
    </div>
  </div>
  <div class="stage">
  <div class="app">
    <header>
      <div class="av">
        <svg class="flower" viewBox="0 0 24 24" aria-hidden="true">
          <g fill="#ffffff">
            <ellipse cx="12" cy="6.2" rx="2.5" ry="4.3"/>
            <ellipse cx="12" cy="6.2" rx="2.5" ry="4.3" transform="rotate(60 12 12)"/>
            <ellipse cx="12" cy="6.2" rx="2.5" ry="4.3" transform="rotate(120 12 12)"/>
            <ellipse cx="12" cy="6.2" rx="2.5" ry="4.3" transform="rotate(180 12 12)"/>
            <ellipse cx="12" cy="6.2" rx="2.5" ry="4.3" transform="rotate(240 12 12)"/>
            <ellipse cx="12" cy="6.2" rx="2.5" ry="4.3" transform="rotate(300 12 12)"/>
          </g>
          <circle cx="12" cy="12" r="2.7" fill="#FFCE45"/>
        </svg>
      </div>
      <div class="meta"><div class="t">ЦветоМир</div>
      <div class="s"><span class="dot"></span>Соня — онлайн-консультант по букетам</div></div>
      <button id="restart" type="button" title="Начать чат заново">
        <svg viewBox="0 0 24 24"><path d="M12 5V2L7 7l5 5V8c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/></svg>
        <span class="rlabel">Заново</span>
      </button>
    </header>
    <div id="log"></div>
    <div id="typing"><span class="dots"><i></i><i></i><i></i></span></div>
    <footer>
      <textarea id="in" placeholder="Напишите сообщение…" rows="1"></textarea>
      <button id="send" aria-label="Отправить">
        <svg viewBox="0 0 24 24"><path d="M3.4 20.4l17.45-7.48a1 1 0 000-1.84L3.4 3.6a1 1 0 00-1.39 1.2L4.5 11.5 12 12l-7.5.5-2.49 6.7a1 1 0 001.39 1.2z"/></svg>
      </button>
    </footer>
  </div>
  </div>
<script>
(function(){
  var API = location.origin;
  var KEY = 'sonya_chat_uuid';
  function uuid(){
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,function(c){
      var r=Math.random()*16|0,v=c==='x'?r:(r&0x3)|0x8; return v.toString(16); });
  }
  var UUID = localStorage.getItem(KEY); if(!UUID){ UUID=uuid(); localStorage.setItem(KEY,UUID); }
  var logEl=document.getElementById('log'), input=document.getElementById('in'),
      sendBtn=document.getElementById('send'), typingEl=document.getElementById('typing');

  function esc(s){ var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
  function linkify(s){ return esc(s).replace(/(https?:\/\/[^\s<]+)/g,function(u){
    return '<a href="'+u+'" target="_blank" rel="noopener">'+u+'</a>'; }); }
  function atBottom(){ return logEl.scrollHeight-logEl.scrollTop-logEl.clientHeight<80; }
  function scroll(){ logEl.scrollTop=logEl.scrollHeight; }
  function addMsg(from,text){ var stick=atBottom();
    var row=document.createElement('div'); row.className='row '+(from==='me'?'me':from);
    row.innerHTML='<div class="b">'+linkify(text)+'</div>'; logEl.appendChild(row);
    if(stick) scroll(); }
  function setTyping(on){ typingEl.style.display=on?'block':'none'; if(on) scroll(); }
  async function api(path,body){ var r=await fetch(API+path,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); return r.json(); }

  async function start(){
    try{ var d=await api('/web/start',{uuid:UUID}); logEl.innerHTML='';
      (d.messages||[]).forEach(function(m){ addMsg(m.from,m.text); }); scroll();
    }catch(e){ addMsg('bot','Не удалось загрузить чат. Обновите страницу.'); }
    try{ var es=new EventSource(API+'/web/stream?uuid='+encodeURIComponent(UUID));
      es.onmessage=function(ev){ try{ var m=JSON.parse(ev.data);
        if(m&&m.text) addMsg('manager',m.text); }catch(e){} }; }catch(e){}
  }
  async function send(){ var text=input.value.trim(); if(!text) return;
    input.value=''; input.style.height='44px'; addMsg('me',text); scroll();
    sendBtn.disabled=true; setTyping(true);
    try{ var d=await api('/web/message',{uuid:UUID,text:text}); setTyping(false);
      if(d&&d.reply) addMsg('bot',d.reply);
    }catch(e){ setTyping(false); addMsg('bot','Ошибка сети, попробуйте ещё раз.'); }
    sendBtn.disabled=false; input.focus(); }

  var restartBtn=document.getElementById('restart');
  if(restartBtn) restartBtn.addEventListener('click',function(){
    if(!confirm('Начать чат заново? Текущая переписка очистится.')) return;
    localStorage.removeItem(KEY);           // новая сессия → Соня поздоровается заново
    location.reload();
  });

  sendBtn.addEventListener('click',send);
  input.addEventListener('keydown',function(e){
    if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); send(); } });
  input.addEventListener('input',function(){ input.style.height='44px';
    input.style.height=Math.min(input.scrollHeight,130)+'px'; });
  start(); input.focus();
})();
</script>
</body></html>"""
