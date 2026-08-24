"""Хранилище состояния диалогов (aiosqlite).

Таблицы:
  users    — профиль и состояние диалога, маппинг на amoCRM
  messages — история сообщений для контекста LLM
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass

import aiosqlite

from app.config import settings

STATE_CONSULT = "consult"
STATE_HANDOFF = "handoff"

# Единое пространство идентификаторов: внутренний uid (везде зовётся «tg_id»)
#   Telegram — реальный tg_id (положительный, на практике < 10^12);
#   web      — отрицательный uid (см. web_sessions);
#   MAX      — MAX_UID_BASE + max_user_id (обратимо, канал определяется диапазоном).
# 10^12 берём с огромным запасом: Telegram-id до него не дорастут ещё очень долго,
# а MAX user_id (~10^9) укладывается в «полку» [10^12, 10^13).
MAX_UID_BASE = 1_000_000_000_000  # 10^12


def max_uid(max_user_id: int) -> int:
    """Внешний MAX user_id -> внутренний uid."""
    return MAX_UID_BASE + int(max_user_id)


def max_user_id_from_uid(uid: int) -> int:
    """Внутренний uid MAX-пользователя -> внешний MAX user_id."""
    return int(uid) - MAX_UID_BASE


@dataclass
class User:
    tg_id: int
    name: str | None
    phone: str | None
    state: str
    amo_lead_id: int | None
    amojo_conversation_id: str | None
    amojo_chat_id: str | None
    amo_contact_id: int | None = None
    amo_last_note_id: int = 0
    context_since: float = 0.0  # с какого времени брать сообщения в контекст LLM (метка /start)
    channel: str = "tg"  # источник диалога: 'tg' (Telegram) или 'web' (виджет на сайте)
    utm_source: str | None = None  # метка канала из deeplink /start (для аналитики amoCRM)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,   -- для web-диалогов здесь отрицательный uid (см. web_sessions)
    name TEXT,
    phone TEXT,
    state TEXT NOT NULL DEFAULT 'consult',
    amo_lead_id INTEGER,
    amojo_conversation_id TEXT,
    amojo_chat_id TEXT,
    amo_contact_id INTEGER,
    context_since REAL DEFAULT 0,
    channel TEXT NOT NULL DEFAULT 'tg',
    created_at REAL,
    updated_at REAL
);
-- Веб-виджет: браузер хранит случайный uuid в localStorage; ему сопоставляется
-- стабильный отрицательный uid, который дальше используется везде как «tg_id».
CREATE TABLE IF NOT EXISTS web_sessions (
    uuid TEXT PRIMARY KEY,
    uid INTEGER NOT NULL UNIQUE,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount REAL NOT NULL,        -- пополнение в рублях (отрицательное = корректировка)
    note TEXT,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS utm_campaigns (
    payload TEXT NOT NULL,       -- нормализованный (lower) ?start=… ; совпадает с users.utm_source
    channel TEXT NOT NULL DEFAULT 'tg',  -- канал метки: 'tg' (Telegram/веб) или 'max' (MAX-бот)
    label TEXT NOT NULL,         -- человекочитаемое имя кампании (задаёт владелец в админке)
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (payload, channel)  -- один код метки живёт отдельно в каждом канале
);
CREATE INDEX IF NOT EXISTS idx_messages_tg ON messages(tg_id, id);
CREATE INDEX IF NOT EXISTS idx_users_conv ON users(amojo_conversation_id);
CREATE INDEX IF NOT EXISTS idx_usage_tg ON usage(tg_id);
"""


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None
        # выдача uid новым web-сессиям должна быть атомарной (SELECT MIN + INSERT)
        self._web_lock = asyncio.Lock()

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        # миграции для уже существующих БД
        for ddl in (
            "ALTER TABLE users ADD COLUMN amo_last_note_id INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN amo_contact_id INTEGER",
            "ALTER TABLE users ADD COLUMN context_since REAL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN reminder_sent REAL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN channel TEXT NOT NULL DEFAULT 'tg'",
            "ALTER TABLE users ADD COLUMN utm_source TEXT",
            "ALTER TABLE messages ADD COLUMN media_url TEXT",
            "ALTER TABLE messages ADD COLUMN media_type TEXT",
            "ALTER TABLE messages ADD COLUMN media_name TEXT",
        ):
            try:
                await self._db.execute(ddl)
            except Exception:  # noqa: BLE001 — колонка уже есть
                pass
        # utm_campaigns: одиночный ключ payload -> составной (payload, channel).
        # SQLite не умеет ALTER PRIMARY KEY, поэтому пересобираем таблицу.
        cur = await self._db.execute("PRAGMA table_info(utm_campaigns)")
        cols = [r["name"] for r in await cur.fetchall()]
        if cols and "channel" not in cols:
            await self._db.executescript(
                """
                CREATE TABLE utm_campaigns_new (
                    payload TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'tg',
                    label TEXT NOT NULL,
                    created_at REAL,
                    updated_at REAL,
                    PRIMARY KEY (payload, channel)
                );
                INSERT INTO utm_campaigns_new (payload, channel, label, created_at, updated_at)
                    SELECT payload, 'tg', label, created_at, updated_at FROM utm_campaigns;
                DROP TABLE utm_campaigns;
                ALTER TABLE utm_campaigns_new RENAME TO utm_campaigns;
                """
            )
        await self._db.commit()
        await self._seed_max_utm_campaigns()

    # Готовые MAX-метки из базы знаний (лист UTM MAX-бота). Сеются один раз;
    # после этого владелец волен их переименовать/удалить в админке.
    _MAX_UTM_SEED = (
        ("2gis_igruska", "MAX_bot_2ГИС рубрика игрушка"),
        ("2gis_towari", "MAX_bot_2ГИС рубрика товары"),
        ("2gis_suveniri", "MAX_bot_2ГИС рубрика сувениры"),
        ("vk_senler", "MAX_bot_ВК senler"),
    )

    async def _seed_max_utm_campaigns(self) -> None:
        """Однократно завести стартовые MAX-метки (guard во app_state, чтобы не
        воскрешать удалённые владельцем строки при каждом рестарте)."""
        cur = await self._db.execute(
            "SELECT value FROM app_state WHERE key = 'utm_max_seed_v1'"
        )
        if await cur.fetchone():
            return
        now = time.time()
        for payload, label in self._MAX_UTM_SEED:
            await self._db.execute(
                "INSERT OR IGNORE INTO utm_campaigns "
                "(payload, channel, label, created_at, updated_at) "
                "VALUES (?, 'max', ?, ?, ?)",
                (payload, label, now, now),
            )
        await self._db.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES ('utm_max_seed_v1', '1')"
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Storage не инициализирован (вызовите connect())"
        return self._db

    # ---------- users ----------
    async def get_or_create_user(self, tg_id: int, channel: str = "tg") -> User:
        cur = await self.db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        if row is None:
            now = time.time()
            await self.db.execute(
                "INSERT INTO users (tg_id, state, channel, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (tg_id, STATE_CONSULT, channel, now, now),
            )
            await self.db.commit()
            user = User(tg_id, None, None, STATE_CONSULT, None, None, None)
            user.channel = channel
            return user
        return self._row_to_user(row)

    async def web_session_uid(self, uuid: str) -> tuple[int, bool]:
        """Вернуть стабильный uid для браузерного uuid; создать при первом обращении.

        uid — отрицательное число (диапазон, не пересекающийся с Telegram-ID),
        которое дальше используется везде как «tg_id». Второй элемент кортежа —
        признак «сессия только что создана» (нужно показать приветствие)."""
        cur = await self.db.execute(
            "SELECT uid FROM web_sessions WHERE uuid = ?", (uuid,)
        )
        row = await cur.fetchone()
        if row is not None:
            return int(row["uid"]), False
        async with self._web_lock:
            # повторная проверка внутри лока — вдруг создали параллельно
            cur = await self.db.execute(
                "SELECT uid FROM web_sessions WHERE uuid = ?", (uuid,)
            )
            row = await cur.fetchone()
            if row is not None:
                return int(row["uid"]), False
            cur = await self.db.execute("SELECT MIN(uid) AS m FROM web_sessions")
            m = (await cur.fetchone())["m"]
            uid = (int(m) if m is not None else 0) - 1  # -1, -2, -3, …
            now = time.time()
            await self.db.execute(
                "INSERT INTO web_sessions (uuid, uid, created_at) VALUES (?, ?, ?)",
                (uuid, uid, now),
            )
            await self.db.commit()
        await self.get_or_create_user(uid, channel="web")
        return uid, True

    async def get_user(self, tg_id: int) -> User | None:
        cur = await self.db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        return self._row_to_user(row) if row else None

    async def find_by_conversation(self, conversation_id: str) -> User | None:
        cur = await self.db.execute(
            "SELECT * FROM users WHERE amojo_conversation_id = ?", (conversation_id,)
        )
        row = await cur.fetchone()
        return self._row_to_user(row) if row else None

    async def find_by_lead_id(self, lead_id: int) -> User | None:
        cur = await self.db.execute(
            "SELECT * FROM users WHERE amo_lead_id = ?", (lead_id,)
        )
        row = await cur.fetchone()
        return self._row_to_user(row) if row else None

    async def update_user(self, tg_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.db.execute(
            f"UPDATE users SET {cols} WHERE tg_id = ?", (*fields.values(), tg_id)
        )
        await self.db.commit()

    async def list_handoff_users(self) -> list[User]:
        """Активные диалоги в режиме handoff с привязанной сделкой amoCRM."""
        cur = await self.db.execute(
            "SELECT * FROM users WHERE state = ? AND amo_lead_id IS NOT NULL",
            (STATE_HANDOFF,),
        )
        rows = await cur.fetchall()
        return [self._row_to_user(r) for r in rows]

    @staticmethod
    def _row_to_user(row: aiosqlite.Row) -> User:
        keys = row.keys()
        return User(
            tg_id=row["tg_id"],
            name=row["name"],
            phone=row["phone"],
            state=row["state"],
            amo_lead_id=row["amo_lead_id"],
            amojo_conversation_id=row["amojo_conversation_id"],
            amojo_chat_id=row["amojo_chat_id"],
            amo_contact_id=row["amo_contact_id"] if "amo_contact_id" in keys else None,
            amo_last_note_id=row["amo_last_note_id"] if "amo_last_note_id" in keys else 0,
            context_since=row["context_since"] if "context_since" in keys else 0.0,
            channel=row["channel"] if "channel" in keys else "tg",
            utm_source=row["utm_source"] if "utm_source" in keys else None,
        )

    # ---------- messages ----------
    async def mark_session_start(self, tg_id: int) -> None:
        """Отметка нового диалога (/start): бот будет брать в контекст только
        сообщения ПОСЛЕ этой метки. Саму переписку и расход НЕ удаляем —
        они полностью хранятся для админки.

        Здесь же снимаем метку напоминания: новый /start — это новая сессия,
        и если клиент снова замолчит, бот должен иметь право напомнить ещё раз.
        Без этого сброса флаг reminder_sent, выставленный при прошлом пинге,
        оставался бы навсегда (его сбрасывает только сообщение role='user'),
        и повторно зашедший молчащий клиент напоминания уже не получал."""
        await self.db.execute(
            "UPDATE users SET context_since = ?, reminder_sent = 0 WHERE tg_id = ?",
            (time.time(), tg_id),
        )
        await self.db.commit()

    async def add_message(self, tg_id: int, role: str, content: str,
                          media_url: str | None = None, media_type: str | None = None,
                          media_name: str | None = None) -> None:
        await self.db.execute(
            "INSERT INTO messages (tg_id, role, content, ts, media_url, media_type, media_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tg_id, role, content, time.time(), media_url, media_type, media_name),
        )
        # клиент снова написал — снимаем метку напоминания, чтобы при новой
        # паузе бот смог напомнить ещё раз
        if role == "user":
            await self.db.execute(
                "UPDATE users SET reminder_sent = 0 WHERE tg_id = ?", (tg_id,)
            )
        await self.db.commit()

    async def add_raw_message(self, tg_id: int, role: str, payload: dict) -> None:
        """Сохранить сообщение ассистента, содержащее tool_calls (как JSON)."""
        await self.db.execute(
            "INSERT INTO messages (tg_id, role, content, ts) VALUES (?, ?, ?, ?)",
            (tg_id, role, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        await self.db.commit()

    async def history(self, tg_id: int, limit: int = 20) -> list[dict]:
        """Контекст для LLM — только сообщения ТЕКУЩЕЙ сессии (после последнего
        /start). Вся переписка при этом остаётся в БД для админки."""
        cur = await self.db.execute(
            "SELECT context_since FROM users WHERE tg_id = ?", (tg_id,)
        )
        row = await cur.fetchone()
        since = (row["context_since"] if row and row["context_since"] else 0) or 0
        cur = await self.db.execute(
            "SELECT role, content FROM messages WHERE tg_id = ? AND ts >= ? "
            "ORDER BY id DESC LIMIT ?",
            (tg_id, since, limit),
        )
        rows = await cur.fetchall()
        out: list[dict] = []
        for row in reversed(rows):
            out.append({"role": row["role"], "content": row["content"]})
        return out

    async def history_full(self, tg_id: int, limit: int = 200, since: float = 0) -> list[dict]:
        """Полная история с временем и id (для админки). since>0 — только текущая
        сессия (например, транскрипт заказа флористу)."""
        cur = await self.db.execute(
            "SELECT id, role, content, ts, media_url, media_type, media_name "
            "FROM messages WHERE tg_id = ? AND ts >= ? ORDER BY id DESC LIMIT ?",
            (tg_id, since, limit),
        )
        rows = await cur.fetchall()
        keys = rows[0].keys() if rows else []
        return [
            {
                "id": r["id"], "role": r["role"], "content": r["content"], "ts": r["ts"],
                "media_url": r["media_url"] if "media_url" in keys else None,
                "media_type": r["media_type"] if "media_type" in keys else None,
                "media_name": r["media_name"] if "media_name" in keys else None,
            }
            for r in reversed(rows)
        ]

    # ---------- напоминания при молчании ----------
    async def users_to_remind(
        self, idle_seconds: float, max_idle_seconds: float
    ) -> list[int]:
        """tg_id клиентов, которым пора напомнить: в режиме бота (consult),
        напоминание ещё не отправлено, последнее сообщение — от бота (assistant),
        и пауза в диапазоне [idle_seconds, max_idle_seconds].

        Верхняя граница нужна, чтобы после рестарта не пинговать старые диалоги
        (давно молчащие чаты в окно не попадают)."""
        now = time.time()
        lo = now - max_idle_seconds  # последнее сообщение не раньше этого
        hi = now - idle_seconds      # и не позже этого
        cur = await self.db.execute(
            """
            SELECT u.tg_id AS tg_id,
                   (SELECT m.role FROM messages m WHERE m.tg_id = u.tg_id
                    ORDER BY m.id DESC LIMIT 1) AS last_role,
                   (SELECT MAX(m.ts) FROM messages m WHERE m.tg_id = u.tg_id) AS last_ts
            FROM users u
            WHERE u.state = ? AND COALESCE(u.reminder_sent, 0) = 0
              AND COALESCE(u.channel, 'tg') = 'tg'
            """,
            (STATE_CONSULT,),
        )
        rows = await cur.fetchall()
        return [
            r["tg_id"]
            for r in rows
            if r["last_role"] == "assistant"
            and r["last_ts"] is not None
            and lo <= r["last_ts"] <= hi
        ]

    async def mark_reminded(self, tg_id: int) -> None:
        await self.db.execute(
            "UPDATE users SET reminder_sent = ? WHERE tg_id = ?", (time.time(), tg_id)
        )
        await self.db.commit()

    # ---------- usage / стоимость ----------
    async def add_usage(
        self,
        tg_id: int,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
    ) -> None:
        """Зафиксировать расход токенов и стоимость одного вызова LLM."""
        await self.db.execute(
            "INSERT INTO usage (tg_id, model, prompt_tokens, completion_tokens, "
            "total_tokens, cost, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tg_id,
                model,
                prompt_tokens,
                completion_tokens,
                prompt_tokens + completion_tokens,
                cost,
                time.time(),
            ),
        )
        await self.db.commit()

    async def users_overview(
        self, since: float | None = None, until: float | None = None
    ) -> list[dict]:
        """Сводка по пользователям: профиль, число сообщений и стоимость.

        since/until (эпоха) — окно месяца: показываем клиентов, ЗАВЕДЁННЫХ в этом
        окне (когорта месяца), а сообщения/расход по каждому считаем тоже в окне.
        Без окна — все клиенты за всё время."""
        windowed = since is not None and until is not None
        mwin = "AND m.ts >= ? AND m.ts < ?" if windowed else ""
        gwin = "AND g.ts >= ? AND g.ts < ?" if windowed else ""
        ucond = "WHERE u.created_at >= ? AND u.created_at < ?" if windowed else ""
        sql = f"""
            SELECT u.tg_id, u.name, u.phone, u.state, u.amo_lead_id,
                   COALESCE(u.channel, 'tg') AS channel,
                   u.utm_source,
                   u.created_at, u.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.tg_id = u.tg_id {mwin}) AS msg_count,
                   (SELECT MAX(ts) FROM messages m WHERE m.tg_id = u.tg_id {mwin}) AS last_ts,
                   (SELECT COALESCE(SUM(cost), 0) FROM usage g WHERE g.tg_id = u.tg_id {gwin}) AS cost,
                   (SELECT COALESCE(SUM(total_tokens), 0) FROM usage g WHERE g.tg_id = u.tg_id {gwin}) AS tokens,
                   (SELECT COUNT(*) FROM usage g WHERE g.tg_id = u.tg_id {gwin}) AS llm_calls
            FROM users u
            {ucond}
            ORDER BY (last_ts IS NULL), last_ts DESC
        """
        params: list = []
        if windowed:
            w = [since, until]
            params += w * 5  # msg_count, last_ts, cost, tokens, llm_calls
            params += w      # ucond (created_at)
        cur = await self.db.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def totals(self, since: float | None = None, until: float | None = None) -> dict:
        """Глобальные итоги для шапки. С окном месяца: клиенты, заведённые в окне;
        сообщения и расход — за это же окно (весь объём месяца)."""
        windowed = since is not None and until is not None
        ucond = "WHERE created_at >= ? AND created_at < ?" if windowed else ""
        tscond = "WHERE ts >= ? AND ts < ?" if windowed else ""
        w = [since, until] if windowed else []
        cur = await self.db.execute(f"SELECT COUNT(*) AS users FROM users {ucond}", w)
        users = (await cur.fetchone())["users"]
        cur = await self.db.execute(
            f"SELECT COALESCE(SUM(cost), 0) AS cost, "
            f"COALESCE(SUM(total_tokens), 0) AS tokens, COUNT(*) AS calls FROM usage {tscond}",
            w,
        )
        row = await cur.fetchone()
        cur = await self.db.execute(f"SELECT COUNT(*) AS msgs FROM messages {tscond}", w)
        msgs = (await cur.fetchone())["msgs"]
        return {
            "users": users,
            "messages": msgs,
            "cost": row["cost"],
            "tokens": row["tokens"],
            "calls": row["calls"],
        }

    # ---------- кошелёк владельца (виртуальный баланс в рублях) ----------
    async def wallet_balance(self) -> float:
        """Сумма всех пополнений (за вычетом ручных корректировок), в рублях."""
        cur = await self.db.execute("SELECT COALESCE(SUM(amount), 0) AS bal FROM wallet")
        row = await cur.fetchone()
        return float(row["bal"] or 0.0)

    async def wallet_topup(self, amount: float, note: str = "") -> float:
        """Пополнить (amount>0) или скорректировать (amount<0) баланс. Возвращает
        новый баланс."""
        await self.db.execute(
            "INSERT INTO wallet (amount, note, ts) VALUES (?, ?, ?)",
            (amount, note, time.time()),
        )
        await self.db.commit()
        return await self.wallet_balance()

    async def client_dialog_texts(
        self, hidden: set[int] | None = None,
        since: float | None = None, until: float | None = None,
    ) -> list[str]:
        """Склеенный текст сообщений КЛИЕНТА (role='user') по каждому диалогу.
        Одна строка на tg_id. hidden — tg_id, которые исключить (скрытые из /owner).
        since/until — окно месяца по ts сообщения (для помесячного анализа)."""
        win = ""
        params: list = []
        if since is not None and until is not None:
            win = "AND ts >= ? AND ts < ?"
            params = [since, until]
        cur = await self.db.execute(
            f"SELECT tg_id, content FROM messages WHERE role = 'user' {win} ORDER BY tg_id, id",
            params,
        )
        rows = await cur.fetchall()
        by_id: dict[int, list[str]] = {}
        hidden = hidden or set()
        for r in rows:
            if r["tg_id"] in hidden:
                continue
            by_id.setdefault(r["tg_id"], []).append(r["content"] or "")
        return [" ".join(parts) for parts in by_id.values()]

    # ---------- служебное key-value состояние ----------
    async def state_get(self, key: str) -> str | None:
        cur = await self.db.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def state_set(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    # ---------- UTM-кампании (метки, заведённые владельцем в админке) ----------
    async def utm_campaign_upsert(self, payload: str, label: str, channel: str = "tg") -> None:
        """Завести/переименовать кампанию в канале. payload — уже нормализованный
        (lower), совпадает с users.utm_source. created_at ставится только при
        вставке. Один payload живёт отдельно в каждом канале ('tg'/'max')."""
        now = time.time()
        await self.db.execute(
            "INSERT INTO utm_campaigns (payload, channel, label, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(payload, channel) DO UPDATE SET label = excluded.label, "
            "updated_at = excluded.updated_at",
            (payload, channel, label, now, now),
        )
        await self.db.commit()

    async def utm_campaigns_with_counts(self) -> list[dict]:
        """Список кампаний (с каналом) и числом привлечённых клиентов ИМЕННО
        этого канала: MAX-метка считает только клиентов MAX, TG-метка —
        Telegram и веб. Метки с 0 клиентов тоже показываем."""
        cur = await self.db.execute(
            """
            SELECT c.payload, c.channel, c.label, c.created_at,
                   (SELECT COUNT(*) FROM users u
                     WHERE u.utm_source = c.payload
                       AND ( (c.channel = 'max' AND u.channel = 'max')
                          OR (c.channel <> 'max' AND COALESCE(u.channel,'tg') <> 'max') )
                   ) AS count
            FROM utm_campaigns c
            ORDER BY count DESC, c.label COLLATE NOCASE
            """
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def utm_labels_map(self) -> dict[str, dict[str, str]]:
        """{payload: {'tg'|'max': label}} — имена кампаний по каналам для
        подмешивания в utm.admin_label (у одного payload бывают разные подписи
        в TG и MAX)."""
        cur = await self.db.execute("SELECT payload, channel, label FROM utm_campaigns")
        rows = await cur.fetchall()
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            bucket = "max" if r["channel"] == "max" else "tg"
            out.setdefault(r["payload"], {})[bucket] = r["label"]
        return out

    async def dialog_cost(self, tg_id: int) -> dict:
        """Стоимость и расход токенов одного диалога."""
        cur = await self.db.execute(
            "SELECT COALESCE(SUM(cost), 0) AS cost, "
            "COALESCE(SUM(total_tokens), 0) AS tokens, COUNT(*) AS calls "
            "FROM usage WHERE tg_id = ?",
            (tg_id,),
        )
        row = await cur.fetchone()
        return {"cost": row["cost"], "tokens": row["tokens"], "calls": row["calls"]}


storage = Storage(settings.db_path)
