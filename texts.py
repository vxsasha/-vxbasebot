"""Тексты сообщений, карточка проверки и кнопки."""

from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from config import OWNER, Deps
from db import MARKED, Database, StaffList, Status


# ========== Тексты ==========

CAPTION_LIMIT: Final = 1024  # лимит Telegram на подпись к фото


def premium(emoji_id: str, fallback: str) -> str:
    """Премиум-эмодзи; у клиентов без поддержки показывается fallback."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


class Emoji:
    NAME = premium("5886505193180239900", "🔓")
    ID = premium("5985433648810171091", "🏷")
    ROLE = premium("6032625495328165724", "🎭")
    REPORT = premium("6039381989985882045", "📢")
    WAVE = premium("5994750571041525522", "👋")
    SEARCH = premium("5874960879434338403", "🔎")
    PROFILE = premium("5778575233422200567", "👤")
    SHIELD_ID = "6030445631921721471"
    SHIELD = premium(SHIELD_ID, "🛡")


ROLE_NAMES: Final[dict[str, str]] = {
    OWNER: "владелец",
    Status.ADMIN: "администратор",
    Status.GARANT: "гарант",
    Status.SCAMMER: "скамер",
    Status.SUSPECT: "подозрительный пользователь",
    Status.USER: "обычный пользователь",
}


def esc(value: object) -> str:
    return html.escape(str(value)) if value is not None else ""


def visible_len(text: str) -> int:
    """Длина текста так, как её считает Telegram: без HTML-тегов, в единицах UTF-16."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    return len(plain.encode("utf-16-le")) // 2


def name_link(row: sqlite3.Row | None, user_id: int) -> str:
    """Кликабельное имя; если имени нет - @юз или ID."""
    name = None
    if row is not None:
        name = row["full_name"] or (f"@{row['username']}" if row["username"] else None)
    return f'<a href="tg://user?id={user_id}">{esc(name or user_id)}</a>'


def user_line(row: sqlite3.Row) -> str:
    username = f" — @{esc(row['username'])}" if row["username"] else ""
    return f"{name_link(row, row['user_id'])}{username} (<code>{row['user_id']}</code>)"


# ---------- карточка проверки ----------

@dataclass(frozen=True, slots=True)
class Card:
    role: str  # по роли выбирается баннер
    caption: str


def check_card(deps: Deps, user_id: int) -> Card:
    db = deps.db
    row = db.get(user_id)
    role = deps.role_of(user_id)

    name = name_link(row, user_id)
    if row is not None and row["username"]:
        name += f" (@{esc(row['username'])})"

    head = [
        "<b>Проверяем пользователя…</b>",
        "",
        f"<b>{Emoji.NAME}Имя:</b> {name}",
        f"<b>{Emoji.ID}Айди:</b> <code>{user_id}</code>",
        "",
        f"{Emoji.ROLE}<b>Роль: {ROLE_NAMES[role]}</b>",
    ]
    if role in (Status.GARANT, OWNER):
        channel = row["channel"] if row is not None else None
        head.append(f"<b>Канал гаранта:</b> {esc(channel) if channel else 'не указан'}")
    if role == Status.ADMIN:
        head += ["", f"<b>Скамеров добавлено:</b> {db.added_count(user_id)}"]
    if role == OWNER:
        head.append(f"<b>Скамеров слито:</b> {db.added_count(user_id)}")

    tail = _card_footer(deps)

    reports = []
    if role in MARKED:
        reports = [f'{esc(r["reason"])} <a href="{esc(r["proof"])}">пруф</a>' for r in db.reports(user_id)]

    def build(shown: list[str]) -> str:
        lines = list(head)
        if shown:
            lines += ["", *shown]
            if hidden := len(reports) - len(shown):
                lines.append(f"…и ещё {hidden}")
        return "\n".join(lines + tail)

    # Если все записи не влезают в подпись - убираем самые старые
    shown = reports
    caption = build(shown)
    while shown and visible_len(caption) > CAPTION_LIMIT:
        shown = shown[1:]
        caption = build(shown)
    return Card(role=role, caption=caption)


def unknown_card(deps: Deps, username: str) -> Card:
    """Юза нет в базе бота: ID узнать нельзя, но и в базе скамеров этого юза нет."""
    username = esc(username.lstrip("@"))
    lines = [
        "<b>Проверяем пользователя…</b>",
        "",
        f'<b>{Emoji.NAME}Имя:</b> <a href="https://t.me/{username}">@{username}</a>',
        f"<b>{Emoji.ID}Айди:</b> неизвестен",
        "",
        f"{Emoji.ROLE}<b>Роль: {ROLE_NAMES[Status.USER]}</b>",
        *_card_footer(deps),
    ]
    return Card(role=Status.USER, caption="\n".join(lines))


def _card_footer(deps: Deps) -> list[str]:
    return [
        "",
        f"<b>Дата проверки:</b> {datetime.now():%d.%m.%Y %H:%M}",
        f'<b>{Emoji.REPORT}</b><b>Скамера можно слить в </b>'
        f'<a href="{esc(deps.settings.report_url)}"><b>предложку</b></a>',
    ]


# ---------- остальные тексты ----------

START = (
    f"{Emoji.WAVE} <b>Добро пожаловать в базу скамеров!</b>\n\n"
    "Здесь можно проверить человека перед сделкой.\n\n"
    f"{Emoji.SEARCH} /check @юз или /check ID\n"
    f"{Emoji.SEARCH} <code>чек @юз</code> - то же самое без слеша\n"
    f"{Emoji.PROFILE} /me - проверить себя\n"
    f"{Emoji.SHIELD} /garants - список гарантов\n"
    "👮 /admins - список администраторов\n"
    "📊 /info - информация о базе"
)

_HELP_PUBLIC = (
    "<b>Команды для всех</b>\n"
    "/me - проверить себя\n"
    "/check [юз/ID] - проверка в базе\n"
    "чек [юз/ID] - проверка в базе\n"
    "/garants - список гарантов\n"
    "/admins - список администраторов\n"
    "/info - информация о базе"
)
_HELP_ADMIN = (
    "<b>Для администраторов</b>\n"
    "/add [ID] [причина] [ссылка на пруф] - занести скамера (повторно - ещё одна запись)\n"
    "/add_r [ID] [причина] [ссылка на пруф] - занести подозрительного"
)
_HELP_OWNER = (
    "<b>Для владельца</b>\n"
    "/hire_admin [ID] - сделать администратором\n"
    "/hire_garant [ID] [канал] - сделать гарантом\n"
    "/channel [ID] [ссылка] - канал гаранта (без ссылки - убрать)\n"
    "/role_norm [ID] - снять любую роль\n"
    "/addadmin [ID] - добавить в список админов\n"
    "/deladmin [ID] - убрать из списка админов\n"
    "/addgarant [ID] - добавить в список гарантов\n"
    "/delgarant [ID] - убрать из списка гарантов"
)


def help_text(deps: Deps, user_id: int) -> str:
    parts = [_HELP_PUBLIC]
    if deps.is_admin(user_id):
        parts.append(_HELP_ADMIN)
    if deps.is_owner(user_id):
        parts.append(_HELP_OWNER)
    return "\n\n".join(parts)


_LIST_TITLES: Final = {
    StaffList.GARANTS: ("🛡 <b>Список гарантов</b>", "Список гарантов пока пуст."),
    StaffList.ADMINS: ("👮 <b>Список администраторов</b>", "Список администраторов пока пуст."),
}


def staff_list_text(db: Database, staff_list: StaffList) -> str:
    title, empty = _LIST_TITLES[staff_list]
    rows = db.list_members(staff_list)
    if not rows:
        return f"{title}\n\n{empty}"
    lines = []
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {user_line(row)}")
        if staff_list is StaffList.GARANTS and row["channel"]:
            lines.append(f"     Канал: {esc(row['channel'])}")
    return f"{title}\n\n" + "\n".join(lines)


def info_text(db: Database, report_url: str) -> str:
    stats = db.stats()
    return "\n".join(
        [
            f"<b>{premium('5226513232549664618', '🔢')}</b><b> Информация о базе</b>",
            "",
            f"<b>{premium('6032594876506312598', '👥')}Всего в базе: {stats['total']}</b>",
            f"<b>{Emoji.ROLE}Скамеров: {stats.get(Status.SCAMMER, 0)}</b>",
            f"<b>{premium('5938215362473496448', '🚫')}Подозрительных: {stats.get(Status.SUSPECT, 0)}</b>",
            f"<b>{premium('5774022692642492953', '✅')}Гарантов: {stats.get(Status.GARANT, 0)}</b>",
            f"<b>{premium('5778570255555105942', '🔒')}Администраторов: {stats.get(Status.ADMIN, 0)}</b>",
            "",
            f'<a href="{esc(report_url)}"><b>Предложка</b></a>',
        ]
    )


def username_not_found(username: str) -> str:
    # Bot API не даёт узнать ID по юзу - бот знает только тех, кто писал ему или в чат, где он есть
    return (
        f"{Emoji.SEARCH} Бот ещё не встречал @{esc(username.lstrip('@'))}, поэтому не знает его ID.\n\n"
        "Проверьте по ID: <code>/check 123456789</code> или ответьте <code>чек</code> на сообщение этого человека."
    )


CHECK_USAGE = (
    "Укажите юз или ID: <code>/check @юз</code>, <code>/check 123456789</code>\n"
    "или ответьте командой на сообщение человека."
)
OWNER_ONLY = "⛔️ Команда только для владельца."
ADMIN_ONLY = "⛔️ Команда только для владельца и администраторов."


def report_usage(command: str) -> str:
    return (
        f"Формат: <code>{command} [ID] [точная причина] [ссылка на сообщение с пруфом]</code>\n"
        f"Пример: <code>{command} 123456789 Скам на 500 робуксов https://t.me/vxbaseroblox/15</code>"
    )


# ========== Кнопки ==========

class MenuButton:
    """Тексты кнопок под полем ввода; нажатие присылает боту этот текст (иконка в него не входит)."""

    PROFILE = "Мой профиль"
    REPORT = "Слить скамера"
    GARANTS = "Гаранты"
    INFO = "Инфо"
    ALL = (PROFILE, REPORT, GARANTS, INFO)


# Премиум-эмодзи слева от текста кнопки
_MENU_ICONS: Final = {
    MenuButton.PROFILE: "5778575233422200567",  # 👤
    MenuButton.REPORT: "6039381989985882045",  # 📢
    MenuButton.GARANTS: Emoji.SHIELD_ID,  # 🛡
    MenuButton.INFO: "5226513232549664618",  # 🔢
}


def main_menu() -> ReplyKeyboardMarkup:
    def button(text: str) -> KeyboardButton:
        return KeyboardButton(text, icon_custom_emoji_id=_MENU_ICONS[text])

    return ReplyKeyboardMarkup(
        [
            [button(MenuButton.PROFILE)],
            [button(MenuButton.REPORT)],
            [button(MenuButton.GARANTS), button(MenuButton.INFO)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


REPORT_TEXT = "🚨 Если вас обманули, слейте скамера в предложку - там разберутся и занесут его в базу."


def report_link(report_url: str) -> InlineKeyboardMarkup:
    # Кнопка под полем ввода не умеет открывать ссылки, поэтому ссылка - отдельным сообщением
    return InlineKeyboardMarkup([[InlineKeyboardButton("Перейти в предложку", url=report_url)]])


def permanent_link(user_id: int) -> InlineKeyboardMarkup:
    """Ссылка на профиль по ID - работает, даже если человек сменил юз."""
    return _profile_button(f"tg://user?id={user_id}")


def username_link(username: str) -> InlineKeyboardMarkup:
    """Та же кнопка по юзу - когда ID человека неизвестен."""
    return _profile_button(f"https://t.me/{username.lstrip('@')}")


def _profile_button(url: str) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton("Вечная ссылка", url=url, icon_custom_emoji_id=Emoji.SHIELD_ID)
    return InlineKeyboardMarkup([[button]])
