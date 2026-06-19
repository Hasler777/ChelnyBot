"""Хранилище состояния диалогов (aiosqlite).

Таблицы:
  users    — профиль и состояние диалога, маппинг на amoCRM
  messages — история сообщений для контекста LLM
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import aiosqlite

from app.config import settings

STATE_CONSULT = "consult"
STATE_HANDOFF = "handoff"


@dataclass
class User:
    tg_id: int
    name: str | None
    phone: str | None
    state: str
    amo_lead_id: int | None
    amojo_conversation_id: str | None
    amojo_chat_id: str | None
    amo_last_note_id: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    name TEXT,
    phone TEXT,
    state TEXT NOT NULL DEFAULT 'consult',
    amo_lead_id INTEGER,
    amojo_conversation_id TEXT,
    amojo_chat_id TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_tg ON messages(tg_id, id);
CREATE INDEX IF NOT EXISTS idx_users_conv ON users(amojo_conversation_id);
"""


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        # миграция для уже существующих БД
        try:
            await self._db.execute(
                "ALTER TABLE users ADD COLUMN amo_last_note_id INTEGER DEFAULT 0"
            )
        except Exception:  # noqa: BLE001 — колонка уже есть
            pass
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Storage не инициализирован (вызовите connect())"
        return self._db

    # ---------- users ----------
    async def get_or_create_user(self, tg_id: int) -> User:
        cur = await self.db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        if row is None:
            now = time.time()
            await self.db.execute(
                "INSERT INTO users (tg_id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (tg_id, STATE_CONSULT, now, now),
            )
            await self.db.commit()
            return User(tg_id, None, None, STATE_CONSULT, None, None, None)
        return self._row_to_user(row)

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
            amo_last_note_id=row["amo_last_note_id"] if "amo_last_note_id" in keys else 0,
        )

    # ---------- messages ----------
    async def add_message(self, tg_id: int, role: str, content: str) -> None:
        await self.db.execute(
            "INSERT INTO messages (tg_id, role, content, ts) VALUES (?, ?, ?, ?)",
            (tg_id, role, content, time.time()),
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
        cur = await self.db.execute(
            "SELECT role, content FROM messages WHERE tg_id = ? ORDER BY id DESC LIMIT ?",
            (tg_id, limit),
        )
        rows = await cur.fetchall()
        out: list[dict] = []
        for row in reversed(rows):
            out.append({"role": row["role"], "content": row["content"]})
        return out


storage = Storage(settings.db_path)
