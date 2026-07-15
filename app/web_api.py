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


async def web_chat(request: web.Request) -> web.Response:
    """Полноэкранная страница-чат с Соней (без пузыря) — для теста и прямых ссылок."""
    return web.Response(text=_CHAT_HTML, content_type="text/html")


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
  :root{ --c:#d6336c; }
  *{ box-sizing:border-box; margin:0; padding:0; }
  html,body{ height:100%; }
  body{ font-family:system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;
    background:#f7f7f9; color:#222; display:flex; flex-direction:column; height:100dvh; }
  header{ background:var(--c); color:#fff; padding:12px 16px; display:flex; align-items:center;
    gap:10px; flex:0 0 auto; box-shadow:0 2px 8px rgba(0,0,0,.12); }
  header .av{ width:38px; height:38px; border-radius:50%; background:rgba(255,255,255,.22);
    display:flex; align-items:center; justify-content:center; font-size:20px; }
  header .t{ font-weight:700; font-size:16px; line-height:1.2; }
  header .s{ font-size:12px; opacity:.85; margin-top:1px; }
  #log{ flex:1 1 auto; overflow-y:auto; padding:16px; display:flex; flex-direction:column;
    gap:8px; max-width:820px; width:100%; margin:0 auto; }
  .row{ display:flex; }
  .row.me{ justify-content:flex-end; }
  .b{ max-width:82%; padding:10px 13px; border-radius:14px; font-size:15px; line-height:1.45;
    white-space:pre-wrap; word-wrap:break-word; overflow-wrap:anywhere; }
  .row.me .b{ background:var(--c); color:#fff; border-bottom-right-radius:4px; }
  .row.bot .b,.row.manager .b{ background:#fff; color:#222; border:1px solid #ececf0;
    border-bottom-left-radius:4px; }
  .row.manager .b{ border-left:3px solid var(--c); }
  .b a{ color:inherit; text-decoration:underline; }
  .row.me .b a{ color:#fff; }
  #typing{ font-size:12px; color:#888; padding:0 16px 6px; display:none;
    max-width:820px; width:100%; margin:0 auto; }
  footer{ flex:0 0 auto; background:#fff; border-top:1px solid #ececf0; padding:10px;
    display:flex; align-items:flex-end; gap:8px; max-width:820px; width:100%; margin:0 auto; }
  #in{ flex:1 1 auto; resize:none; border:1px solid #dcdce2; border-radius:12px; padding:11px 13px;
    font-size:15px; line-height:1.35; height:44px; min-height:44px; max-height:130px; outline:none;
    font-family:inherit; }
  #in:focus{ border-color:var(--c); }
  #send{ flex:0 0 48px; width:48px; height:44px; border:none; border-radius:12px; background:var(--c);
    color:#fff; font-size:18px; cursor:pointer; }
  #send:disabled{ opacity:.5; cursor:default; }
  .footwrap{ flex:0 0 auto; background:#fff; border-top:1px solid #ececf0; }
</style></head>
<body>
  <header>
    <div class="av">🌷</div>
    <div><div class="t">Соня · ЦветоМира</div>
    <div class="s">Онлайн-консультант по букетам</div></div>
  </header>
  <div id="log"></div>
  <div id="typing">Соня печатает…</div>
  <div class="footwrap"><footer>
    <textarea id="in" placeholder="Напишите сообщение…" rows="1"></textarea>
    <button id="send">➤</button>
  </footer></div>
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

  sendBtn.addEventListener('click',send);
  input.addEventListener('keydown',function(e){
    if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); send(); } });
  input.addEventListener('input',function(){ input.style.height='44px';
    input.style.height=Math.min(input.scrollHeight,130)+'px'; });
  start(); input.focus();
})();
</script>
</body></html>"""
