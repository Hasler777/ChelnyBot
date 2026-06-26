"""Админка: список диалогов и пользователей со стоимостью переписки.

Эндпоинты:
  GET /admin                     -> HTML-страница админки
  GET /admin/api/users?token=..  -> сводка по всем пользователям + итоги
  GET /admin/api/dialog?tg_id=.. -> полная переписка одного диалога + стоимость

Доступ защищён общим секретом ADMIN_TOKEN (если задан в .env).
"""
from __future__ import annotations

import logging
import time

import aiohttp
from aiohttp import web

from app.config import settings
from app.db.storage import storage

log = logging.getLogger(__name__)

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


def _check(token: str | None, secret: str) -> bool:
    # если секрет не задан — доступ открыт (удобно для локальной отладки)
    return not secret or token == secret


async def _users_response(markup: float) -> web.Response:
    users = await storage.users_overview()
    totals = await storage.totals()
    if markup and markup != 1.0:
        for u in users:
            u["cost"] = (u.get("cost") or 0) * markup
        totals["cost"] = (totals.get("cost") or 0) * markup
    return web.json_response(
        {"users": users, "totals": totals, "usd_rub_rate": await _usd_rub_rate()}
    )


async def _dialog_response(request: web.Request, markup: float) -> web.Response:
    try:
        tg_id = int(request.query.get("tg_id", ""))
    except ValueError:
        return web.json_response({"error": "bad tg_id"}, status=400)

    user = await storage.get_user(tg_id)
    rows = await storage.history_full(tg_id, limit=1000)
    cost = await storage.dialog_cost(tg_id)
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
    return await _users_response(settings.owner_cost_markup)


async def owner_dialog(request: web.Request) -> web.Response:
    if not _check(request.query.get("token"), settings.owner_token):
        return web.json_response({"error": "forbidden"}, status=403)
    return await _dialog_response(request, settings.owner_cost_markup)


def _page(api_base: str) -> web.Response:
    return web.Response(text=_ADMIN_HTML.replace("__API_BASE__", api_base),
                        content_type="text/html")


async def get_admin_page(request: web.Request) -> web.Response:
    return _page("/admin")


async def get_owner_page(request: web.Request) -> web.Response:
    return _page("/owner")


def add_admin_routes(app: web.Application) -> None:
    app.router.add_get("/admin", get_admin_page)
    app.router.add_get("/admin/api/users", api_users)
    app.router.add_get("/admin/api/dialog", api_dialog)
    app.router.add_get("/owner", get_owner_page)
    app.router.add_get("/owner/api/users", owner_users)
    app.router.add_get("/owner/api/dialog", owner_dialog)


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Соня · Админка</title>
<style>
  :root { --bg:#0f141b; --panel:#171e27; --panel2:#1f2733; --line:#2b3947; --txt:#e6edf3; --mut:#9fb3c8; --acc:#2f6feb; }
  * { box-sizing:border-box; }
  html, body { height:100%; margin:0; }
  body { font-family:-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--txt); }
  header { padding:14px 20px; background:var(--panel); border-bottom:1px solid var(--line); display:flex; align-items:center; gap:24px; flex-wrap:wrap; }
  header h1 { font-size:17px; margin:0; font-weight:700; }
  .stats { display:flex; gap:22px; flex-wrap:wrap; }
  .stat { display:flex; flex-direction:column; }
  .stat b { font-size:16px; }
  .stat span { font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:.04em; }
  #search { margin-left:auto; background:var(--bg); border:1px solid var(--line); border-radius:8px; color:var(--txt); padding:8px 12px; font-size:13px; min-width:200px; }
  main { padding:16px 20px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); }
  th { color:var(--mut); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; cursor:pointer; user-select:none; white-space:nowrap; }
  tbody tr { cursor:pointer; }
  tbody tr:hover { background:var(--panel2); }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .cost { font-weight:600; color:#7ee2a8; }
  .badge { font-size:11px; padding:2px 8px; border-radius:10px; background:var(--panel2); color:var(--mut); }
  .badge.handoff { background:#3a2d12; color:#f0c674; }
  .badge.consult { background:#13314a; color:#79b8ff; }
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
</style>
</head>
<body>
  <header>
    <h1>🌷 Соня · диалоги</h1>
    <div class="stats" id="stats"></div>
    <input id="search" placeholder="Поиск по имени / телефону / id…">
  </header>
  <main>
    <table>
      <thead><tr>
        <th data-k="name">Клиент</th>
        <th data-k="phone">Телефон</th>
        <th data-k="state">Статус</th>
        <th data-k="msg_count" class="num">Сообщений</th>
        <th data-k="llm_calls" class="num">Запросов</th>
        <th data-k="tokens" class="num">Токенов</th>
        <th data-k="cost" class="num">Стоимость</th>
        <th data-k="last_ts" class="num">Активность</th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="8" id="empty">Загрузка…</td></tr></tbody>
    </table>
  </main>

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

async function loadUsers(){
  let r;
  try { r = await fetch(`${API}/api/users?token=${encodeURIComponent(token)}`); }
  catch(e){ document.getElementById('empty').textContent='Ошибка сети'; return; }
  if(r.status===403){ promptToken(); return; }
  const j = await r.json();
  rate = j.usd_rub_rate || 0;
  data = j.users || [];
  renderStats(j.totals);
  render();
}

function renderStats(t){
  if(!t) return;
  document.getElementById('stats').innerHTML = [
    ['Пользователей', t.users],
    ['Сообщений', t.messages],
    ['LLM-запросов', t.calls],
    ['Токенов', (t.tokens||0).toLocaleString('ru-RU')],
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
  if(!rows.length){ tb.innerHTML='<tr><td colspan="8" id="empty">Нет диалогов</td></tr>'; return; }
  tb.innerHTML = rows.map(u=>`
    <tr data-id="${u.tg_id}">
      <td>${esc(u.name||'Без имени')}<div class="muted" style="font-size:11px">id ${u.tg_id}</div></td>
      <td>${esc(u.phone||'—')}</td>
      <td><span class="badge ${u.state}">${u.state==='handoff'?'у флориста':'бот'}</span></td>
      <td class="num">${u.msg_count||0}</td>
      <td class="num">${u.llm_calls||0}</td>
      <td class="num">${(u.tokens||0).toLocaleString('ru-RU')}</td>
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
  document.getElementById('dsub').innerHTML = `id ${u.tg_id}${u.phone?' · '+esc(u.phone):''}${u.amo_lead_id?' · сделка #'+u.amo_lead_id:''}`;
  document.getElementById('dcost').innerHTML = [
    ['Стоимость диалога', money(c.cost)],
    ['Токенов', (c.tokens||0).toLocaleString('ru-RU')],
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

loadUsers();
setInterval(loadUsers, 15000);
</script>
</body>
</html>"""
