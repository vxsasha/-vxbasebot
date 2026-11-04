"""Настройки из .env и общие зависимости хендлеров (база, права)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from telegram.ext import ContextTypes

from db import Database, Status


# ========== Настройки ==========

ROOT_DIR = Path(__file__).resolve().parent  # папка с main.py
# Баннеры лежат в assets/ или прямо рядом с main.py (так получается при загрузке файлов через сайт GitHub)
ASSETS_DIR = ROOT_DIR / "assets" if (ROOT_DIR / "assets").is_dir() else ROOT_DIR
CAPTURES_DIR = ROOT_DIR / "captures"
DB_FILENAME = "base.db"

DEFAULT_REPORT_URL = "https://t.me/vxbaseroblox"


class ConfigError(RuntimeError):
    """Не хватает обязательной настройки."""


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    owner_ids: frozenset[int]
    report_url: str
    db_path: Path
    # Запущено на Railway без Volume - база пропадёт при каждом деплое
    db_is_ephemeral: bool
    # ID, от которого принимаются образцы оформления; None - приём выключен
    capture_id: int | None


def _parse_ids(raw: str) -> frozenset[int]:
    """'123, 456' -> {123, 456}."""
    return frozenset(int(part) for part in raw.replace(" ", "").split(",") if part.isdigit())


def _db_path() -> tuple[Path, bool]:
    """Путь к базе и признак, что она не переживёт перезапуск.

    DB_PATH из переменных > Volume Railway (Railway сам задаёт RAILWAY_VOLUME_MOUNT_PATH) > рядом с main.py.
    """
    if explicit := os.getenv("DB_PATH", "").strip():
        path = Path(explicit)
        return (path if path.is_absolute() else ROOT_DIR / path), False
    if volume := os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip():
        return Path(volume) / DB_FILENAME, False
    on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT_NAME"))
    return ROOT_DIR / DB_FILENAME, on_railway


def load_settings() -> Settings:
    load_dotenv(ROOT_DIR / ".env")  # локально; на Railway переменные задаются во вкладке Variables

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("Нет BOT_TOKEN: добавьте токен бота в переменные (Railway → Variables или файл .env).")

    owner_ids = _parse_ids(os.getenv("OWNER_ID", ""))
    if not owner_ids:
        raise ConfigError("Нет OWNER_ID: добавьте Telegram ID владельца в переменные (Railway → Variables или .env).")

    db_path, db_is_ephemeral = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    capture = os.getenv("CAPTURE_ID", "").strip()

    return Settings(
        bot_token=token,
        owner_ids=owner_ids,
        report_url=os.getenv("REPORT_URL", DEFAULT_REPORT_URL).strip(),
        db_path=db_path,
        db_is_ephemeral=db_is_ephemeral,
        capture_id=int(capture) if capture.isdigit() else None,
    )


# ========== Зависимости и права ==========

OWNER: Final = "owner"  # роль владельца не хранится в базе, она задаётся в .env

_KEY: Final = "deps"


@dataclass(slots=True)
class Deps:
    settings: Settings
    db: Database
    # file_id баннеров после первой загрузки, чтобы не отправлять файл каждый раз
    banner_ids: dict[str, str] = field(default_factory=dict)

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.settings.owner_ids

    def is_admin(self, user_id: int) -> bool:
        """Администратор базы или владелец."""
        return self.is_owner(user_id) or self.db.status_of(user_id) is Status.ADMIN

    def role_of(self, user_id: int) -> str:
        return OWNER if self.is_owner(user_id) else self.db.status_of(user_id)

    def install(self, bot_data: dict) -> None:
        bot_data[_KEY] = self


def from_bot_data(bot_data: dict) -> Deps:
    return bot_data[_KEY]


def get_deps(context: ContextTypes.DEFAULT_TYPE) -> Deps:
    return from_bot_data(context.bot_data)
