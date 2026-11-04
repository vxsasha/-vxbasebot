"""Сборка и запуск бота."""

from __future__ import annotations

import logging
import sys

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

import handlers
import texts
from config import ConfigError, Deps, Settings, from_bot_data, load_settings
from db import Database, StaffList

log = logging.getLogger("bot")


def register(app: Application, settings: Settings) -> None:
    # group=-1: учёт людей выполняется раньше остальных хендлеров и не мешает им
    app.add_handler(TypeHandler(Update, handlers.track), group=-1)

    # Для всех
    app.add_handlers(
        [
            CommandHandler("start", handlers.start),
            CommandHandler("help", handlers.help_command),
            CommandHandler("me", handlers.me),
            CommandHandler("check", handlers.check),
            CommandHandler("garants", handlers.garants),
            CommandHandler("admins", handlers.admins),
            CommandHandler("info", handlers.info),
            MessageHandler(filters.ChatType.PRIVATE & filters.Text(texts.MenuButton.ALL), handlers.menu_button),
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.Regex(handlers.CHECK_TEXT_RE), handlers.check_text
            ),
        ]
    )

    # Владелец и администраторы
    app.add_handlers([CommandHandler("add", handlers.add), CommandHandler("add_r", handlers.add_r)])

    # Только владелец
    toggle = handlers.staff_list_toggle
    app.add_handlers(
        [
            CommandHandler("hire_admin", handlers.hire_admin),
            CommandHandler("hire_garant", handlers.hire_garant),
            CommandHandler("role_norm", handlers.role_norm),
            CommandHandler("channel", handlers.channel),
            CommandHandler("addadmin", toggle(StaffList.ADMINS, True, "/addadmin", "добавлен в список администраторов")),
            CommandHandler("deladmin", toggle(StaffList.ADMINS, False, "/deladmin", "убран из списка администраторов")),
            CommandHandler("addgarant", toggle(StaffList.GARANTS, True, "/addgarant", "добавлен в список гарантов")),
            CommandHandler("delgarant", toggle(StaffList.GARANTS, False, "/delgarant", "убран из списка гарантов")),
        ]
    )

    if settings.capture_id is not None:
        app.add_handler(
            MessageHandler(
                filters.User(settings.capture_id) & filters.ChatType.PRIVATE & ~filters.COMMAND, handlers.capture
            )
        )

    app.add_error_handler(handlers.on_error)


async def _post_init(app: Application) -> None:
    deps = from_bot_data(app.bot_data)
    await handlers.setup_menus(app.bot, deps)
    me = await app.bot.get_me()
    log.info("Бот @%s запущен, владельцы: %s", me.username, ", ".join(map(str, sorted(deps.settings.owner_ids))))
    log.info("База: %s", deps.settings.db_path)
    if deps.settings.db_is_ephemeral:
        log.warning("Volume не подключён: база удалится при следующем деплое. Railway → сервис → Add Volume → /data")


async def _post_shutdown(app: Application) -> None:
    from_bot_data(app.bot_data).db.close()


def build_application(settings: Settings) -> Application:
    app = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    Deps(settings=settings, db=Database(settings.db_path)).install(app.bot_data)
    register(app, settings)
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # httpx пишет строку на каждый запрос к Telegram
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except ConfigError as exc:
        sys.exit(str(exc))
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)

