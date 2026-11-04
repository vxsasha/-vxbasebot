"""Хранилище базы на SQLite."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class Status(StrEnum):
    """Статус человека в базе."""

    USER = "user"
    ADMIN = "admin"
    GARANT = "garant"
    SCAMMER = "scammer"
    SUSPECT = "suspect"


class StaffList(StrEnum):
    """Публичные списки (/admins, /garants) - значение равно колонке в таблице users."""

    ADMINS = "in_admins"
    GARANTS = "in_garants"


MARKED = (Status.SCAMMER, Status.SUSPECT)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    full_name  TEXT,
    status     TEXT NOT NULL DEFAULT 'user',
    added_by   INTEGER,
    added_at   TEXT,
    in_admins  INTEGER NOT NULL DEFAULT 0,
    in_garants INTEGER NOT NULL DEFAULT 0,
    channel    TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);

-- Записи "причина + пруф": у одного человека их может быть несколько
CREATE TABLE IF NOT EXISTS reports (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    reason    TEXT NOT NULL,
    proof     TEXT NOT NULL,
    added_by  INTEGER NOT NULL,
    added_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_user ON reports(user_id);
CREATE INDEX IF NOT EXISTS idx_reports_by ON reports(added_by);

-- В каких группах человеку выставлено расширенное меню команд
CREATE TABLE IF NOT EXISTS group_menus (
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
"""


def _now() -> str:
    # Формат сортируется как строка, поэтому ORDER BY added_at работает
    return datetime.now().strftime("%Y-%m-%d %H:%M")


class Database:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---------- люди ----------

    def touch(self, user_id: int, username: str | None, full_name: str | None) -> None:
        """Запоминает юз и имя, чтобы потом находить человека по @юзу."""
        with self._conn:
            if username:
                # Юз мог перейти к другому человеку - у прежнего владельца стираем
                self._conn.execute(
                    "UPDATE users SET username = NULL WHERE username = ? COLLATE NOCASE AND user_id != ?",
                    (username, user_id),
                )
            self._conn.execute(
                """
                INSERT INTO users (user_id, username, full_name) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, full_name = excluded.full_name
                """,
                (user_id, username, full_name),
            )

    def get(self, user_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    def find_by_username(self, username: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.lstrip("@"),)
        ).fetchone()

    def status_of(self, user_id: int) -> Status:
        row = self.get(user_id)
        return Status(row["status"]) if row else Status.USER

    def _ensure(self, user_id: int) -> None:
        self._conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))

    # ---------- роли ----------

    def set_role(self, user_id: int, status: Status, by: int) -> None:
        """Администратор или гарант; человек сразу попадает в свой публичный список."""
        if status not in (Status.ADMIN, Status.GARANT):
            raise ValueError(f"set_role не принимает статус {status}")
        staff_list = StaffList.ADMINS if status is Status.ADMIN else StaffList.GARANTS
        with self._conn:
            self._ensure(user_id)
            self._conn.execute(
                f"UPDATE users SET status = ?, added_by = ?, added_at = ?, {staff_list} = 1 WHERE user_id = ?",
                (status, by, _now(), user_id),
            )

    def reset(self, user_id: int) -> None:
        """Снимает любую роль, убирает из списков и стирает записи."""
        with self._conn:
            self._ensure(user_id)
            self._conn.execute(
                """
                UPDATE users SET status = 'user', added_by = NULL, added_at = NULL,
                                 in_admins = 0, in_garants = 0, channel = NULL
                WHERE user_id = ?
                """,
                (user_id,),
            )
            self._conn.execute("DELETE FROM reports WHERE user_id = ?", (user_id,))

    def set_channel(self, user_id: int, channel: str | None) -> None:
        with self._conn:
            self._ensure(user_id)
            self._conn.execute("UPDATE users SET channel = ? WHERE user_id = ?", (channel, user_id))

    def set_in_list(self, user_id: int, staff_list: StaffList, value: bool) -> None:
        with self._conn:
            self._ensure(user_id)
            self._conn.execute(f"UPDATE users SET {staff_list} = ? WHERE user_id = ?", (int(value), user_id))

    def list_members(self, staff_list: StaffList) -> list[sqlite3.Row]:
        return self._conn.execute(f"SELECT * FROM users WHERE {staff_list} = 1 ORDER BY added_at").fetchall()

    def with_status(self, status: Status) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM users WHERE status = ?", (status,)).fetchall()

    # ---------- скамеры и подозрительные ----------

    def add_report(self, user_id: int, kind: Status, reason: str, proof: str, by: int) -> Status:
        """Добавляет запись и возвращает итоговый статус. Скамер не откатывается в подозрительного."""
        if kind not in MARKED:
            raise ValueError(f"add_report не принимает статус {kind}")
        now = _now()
        with self._conn:
            self._ensure(user_id)
            current = self.status_of(user_id)
            status = Status.SCAMMER if Status.SCAMMER in (kind, current) else Status.SUSPECT
            self._conn.execute(
                "INSERT INTO reports (user_id, kind, reason, proof, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, kind, reason, proof, by, now),
            )
            # Занесённый в базу теряет роль, списки и канал
            self._conn.execute(
                """
                UPDATE users SET status = ?, added_by = ?, added_at = ?, in_admins = 0, in_garants = 0, channel = NULL
                WHERE user_id = ?
                """,
                (status, by, now, user_id),
            )
        return status

    def reports(self, user_id: int) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM reports WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()

    def added_count(self, by: int) -> int:
        """Сколько разных людей занёс в базу этот админ/владелец."""
        return self._conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM reports WHERE added_by = ?", (by,)
        ).fetchone()[0]

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM users GROUP BY status").fetchall()
        result = {row["status"]: row["n"] for row in rows}
        result["total"] = sum(result.values())
        return result

    # ---------- меню команд в группах ----------

    def group_menu_has(self, chat_id: int, user_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM group_menus WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()
        return row is not None

    def group_menu_set(self, chat_id: int, user_id: int, enabled: bool) -> None:
        with self._conn:
            if enabled:
                self._conn.execute(
                    "INSERT OR IGNORE INTO group_menus (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id)
                )
            else:
                self._conn.execute("DELETE FROM group_menus WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))

    def group_menu_chats(self, user_id: int) -> list[int]:
        rows = self._conn.execute("SELECT chat_id FROM group_menus WHERE user_id = ?", (user_id,))
        return [row[0] for row in rows]
