"""Хендлеры команд и меню команд "/" по ролям."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import (
    Bot,
    BotCommand,
    BotCommandScope,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
    Message,
    Update,
    User,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

import texts
from config import ASSETS_DIR, CAPTURES_DIR, Deps, get_deps
from db import StaffList, Status, is_pending

log = logging.getLogger(__name__)


# ========== Меню команд по ролям ==========


PUBLIC_COMMANDS = (
    BotCommand("start", "Главное меню"),
    BotCommand("me", "Проверить себя"),
    BotCommand("check", "Проверить человека: /check @юз или ID"),
    BotCommand("garants", "Список гарантов"),
    BotCommand("admins", "Список администраторов"),
    BotCommand("info", "Информация о базе"),
    BotCommand("help", "Все команды"),
)
ADMIN_COMMANDS = (
    BotCommand("add", "Занести скамера: юз/ID причина пруф"),
    BotCommand("add_r", "Занести подозрительного: юз/ID причина пруф"),
)
OWNER_COMMANDS = (
    BotCommand("hire_admin", "Сделать администратором: ID"),
    BotCommand("hire_garant", "Сделать гарантом: ID [канал]"),
    BotCommand("role_norm", "Снять любую роль: ID"),
    BotCommand("channel", "Канал гаранта: ID ссылка"),
    BotCommand("addgarant", "Добавить в список гарантов: ID"),
    BotCommand("delgarant", "Убрать из списка гарантов: ID"),
    BotCommand("addadmin", "Добавить в список админов: ID"),
    BotCommand("deladmin", "Убрать из списка админов: ID"),
)


def commands_for(deps: Deps, user_id: int) -> tuple[BotCommand, ...]:
    """Расширенное меню человека; пустое - ему положено обычное."""
    if deps.is_owner(user_id):
        return PUBLIC_COMMANDS + ADMIN_COMMANDS + OWNER_COMMANDS
    if deps.is_admin(user_id):
        return PUBLIC_COMMANDS + ADMIN_COMMANDS
    return ()


async def _apply(bot: Bot, scope: BotCommandScope, commands: tuple[BotCommand, ...]) -> bool:
    try:
        if commands:
            await bot.set_my_commands(commands, scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
    except TelegramError as exc:
        # Человек ещё не запускал бота или бота убрали из группы - меню выставится позже
        log.debug("Меню не выставлено (%s): %s", scope, exc)
        return False
    return True


async def refresh_user(bot: Bot, deps: Deps, user_id: int) -> None:
    """Меню человека в личке с ботом и во всех группах, где оно уже выставлено."""
    if is_pending(user_id):
        return
    commands = commands_for(deps, user_id)
    await _apply(bot, BotCommandScopeChat(user_id), commands)
    for chat_id in deps.db.group_menu_chats(user_id):
        await _apply(bot, BotCommandScopeChatMember(chat_id, user_id), commands)
        if not commands:
            deps.db.group_menu_set(chat_id, user_id, False)


async def sync_group(bot: Bot, deps: Deps, chat_id: int, user_id: int) -> None:
    """Человек написал в группе: выставляем или убираем ему расширенное меню, если оно изменилось."""
    commands = commands_for(deps, user_id)
    if bool(commands) != deps.db.group_menu_has(chat_id, user_id):
        if await _apply(bot, BotCommandScopeChatMember(chat_id, user_id), commands):
            deps.db.group_menu_set(chat_id, user_id, bool(commands))


async def setup_menus(bot: Bot, deps: Deps) -> None:
    """При запуске: обычное меню для всех и расширенное - владельцам и администраторам."""
    await bot.set_my_commands(PUBLIC_COMMANDS)
    staff = set(deps.settings.owner_ids) | {row["user_id"] for row in deps.db.with_status(Status.ADMIN)}
    for user_id in staff:
        await refresh_user(bot, deps, user_id)


# ========== Общие помощники ==========

class TargetError(Exception):
    """Не удалось определить, о ком команда; текст - готовый ответ пользователю."""


class UnknownUsername(TargetError):
    """Юза нет в базе бота, поэтому его ID неизвестен."""

    def __init__(self, username: str) -> None:
        self.username = username.lstrip("@")
        super().__init__(texts.username_not_found(self.username))


async def reply_html(message: Message, text: str, **kwargs: Any) -> Message:
    return await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kwargs)


def resolve_target(deps: Deps, update: Update, arg: str | None) -> int | None:
    """ID человека из аргумента (ID или @юз из базы бота) или из сообщения, на которое ответили.

    None - цель не указана. UnknownUsername - такого юза нет в базе бота.
    """
    if arg:
        arg = arg.strip()
        if arg.lstrip("-").isdigit():
            return int(arg)
        if row := deps.db.find_by_username(arg):
            return row["user_id"]
        raise UnknownUsername(arg)

    reply = update.effective_message.reply_to_message
    if reply is not None and reply.from_user is not None:
        return reply.from_user.id
    return None


async def fill_profile(bot: Bot, deps: Deps, user_id: int) -> None:
    """Если у человека в базе нет юза или имени, пробуем узнать их у Telegram по ID.

    Telegram отдаёт данные только о тех, кого бот уже видел; если не вышло - просто оставляем как есть.
    Найденный юз заодно переносит на этот ID запись, занесённую раньше только по юзу.
    """
    if is_pending(user_id):
        return
    row = deps.db.get(user_id)
    if row is not None and row["username"] and row["full_name"]:
        return
    try:
        chat = await bot.get_chat(user_id)
    except TelegramError:
        return
    if chat.type == ChatType.PRIVATE and (chat.username or chat.full_name):
        deps.db.touch(user_id, chat.username, chat.full_name)


async def send_card(message: Message, deps: Deps, user_id: int) -> None:
    """Карточка проверки: баннер роли, подпись и кнопка "Вечная ссылка"."""
    card = texts.check_card(deps, user_id)
    if is_pending(user_id):
        # Настоящий ID неизвестен - кнопка ведёт на t.me/юз
        sent = await _send_banner(message, deps, card, texts.username_link(deps.db.get(user_id)["username"]))
        deps.banner_ids[card.role] = sent.photo[-1].file_id
        return
    try:
        sent = await _send_banner(message, deps, card, texts.permanent_link(user_id))
    except BadRequest as exc:
        # Telegram отклоняет ссылку на профиль, если бот ещё не видел человека (Button_user_invalid)
        # или его приватность это запрещает - тогда карточка уходит без кнопки
        if not any(reason in exc.message.lower() for reason in ("button_user", "privacy")):
            raise
        sent = await _send_banner(message, deps, card, None)
    deps.banner_ids[card.role] = sent.photo[-1].file_id


async def send_unknown_card(message: Message, deps: Deps, username: str) -> None:
    """Карточка для юза, которого нет в базе; кнопка ведёт на t.me/юз, так как ID неизвестен."""
    card = texts.unknown_card(deps, username)
    sent = await _send_banner(message, deps, card, texts.username_link(username))
    deps.banner_ids[card.role] = sent.photo[-1].file_id


async def _send_banner(message: Message, deps: Deps, card: texts.Card, markup: Any) -> Message:
    if file_id := deps.banner_ids.get(card.role):
        photo: Any = file_id
    else:
        photo = (ASSETS_DIR / f"{card.role}.jpg").read_bytes()
    return await message.reply_photo(photo, caption=card.caption, parse_mode=ParseMode.HTML, reply_markup=markup)


# ========== Команды для всех ==========

# "чек", "чек @юз", "чек 123" - проверка без слеша
CHECK_TEXT_RE = re.compile(r"^\s*чек(?:\s+(\S+))?\s*$", re.IGNORECASE)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    user_id = update.effective_user.id
    if deps.is_admin(user_id):
        # Если человека назначили до первого /start, меню ещё не выставлено
        await refresh_user(context.bot, deps, user_id)
    # Кнопки под полем ввода - только в личке: в группе они появились бы у всех участников
    is_private = update.effective_chat.type == ChatType.PRIVATE
    await reply_html(update.effective_message, texts.START, reply_markup=texts.main_menu() if is_private else None)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    await reply_html(update.effective_message, texts.help_text(deps, update.effective_user.id))


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_card(update.effective_message, get_deps(context), update.effective_user.id)


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _check(update, context, context.args[0] if context.args else None)


async def check_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if match := CHECK_TEXT_RE.match(update.effective_message.text or ""):
        await _check(update, context, match.group(1))


async def _check(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str | None) -> None:
    deps = get_deps(context)
    message = update.effective_message
    try:
        user_id = resolve_target(deps, update, arg)
    except UnknownUsername as exc:
        await send_unknown_card(message, deps, exc.username)
        return
    if user_id is None:
        await reply_html(message, texts.CHECK_USAGE)
        return
    await fill_profile(context.bot, deps, user_id)
    await send_card(message, deps, user_id)


async def garants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_staff_list(update.effective_message, get_deps(context), StaffList.GARANTS)


async def admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_staff_list(update.effective_message, get_deps(context), StaffList.ADMINS)


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_info(update.effective_message, get_deps(context))


async def _send_info(message: Message, deps: Deps) -> None:
    await reply_html(message, texts.info_text(deps.db, deps.settings.report_url))


async def _send_staff_list(message: Message, deps: Deps, staff_list: StaffList) -> None:
    await reply_html(message, texts.staff_list_text(deps.db, staff_list))


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки под полем ввода (texts.MenuButton)."""
    deps = get_deps(context)
    message = update.effective_message
    match message.text:
        case texts.MenuButton.PROFILE:
            await send_card(message, deps, update.effective_user.id)
        case texts.MenuButton.REPORT:
            await reply_html(message, texts.REPORT_TEXT, reply_markup=texts.report_link(deps.settings.report_url))
        case texts.MenuButton.GARANTS:
            await _send_staff_list(message, deps, StaffList.GARANTS)
        case texts.MenuButton.INFO:
            await _send_info(message, deps)


# ========== Владелец и администраторы: занести в базу ==========


PROOF_RE = re.compile(r"^(https?://)?(t\.me|telegram\.me)/\S+$", re.IGNORECASE)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add ID причина пруф - скамер."""
    await _report(update, context, Status.SCAMMER, "/add")


async def add_r(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add_r ID причина пруф - подозрительный."""
    await _report(update, context, Status.SUSPECT, "/add_r")


async def _report(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: Status, command: str) -> None:
    deps = get_deps(context)
    message = update.effective_message
    author = update.effective_user.id

    if not deps.is_admin(author):
        await reply_html(message, texts.ADMIN_ONLY)
        return

    args = context.args
    if len(args) < 3 or not PROOF_RE.match(args[-1]):
        await reply_html(message, texts.report_usage(command))
        return

    # /add ID @юз причина пруф - сразу привязать юз, чтобы человека находил чек по @юзу
    username = None
    if len(args) >= 4 and args[0].isdigit() and args[1].startswith("@"):
        username = args.pop(1)

    try:
        target = resolve_target(deps, update, args[0])
    except UnknownUsername as exc:
        # Бот не знает ID этого юза - заносим по юзу; когда человек появится, запись перейдёт на его ID
        target = deps.db.add_pending(exc.username)
    await fill_profile(context.bot, deps, target)

    if deps.is_owner(target):
        await reply_html(message, "⛔️ Нельзя занести владельца в базу.")
        return
    if target == author:
        await reply_html(message, "⛔️ Нельзя занести себя.")
        return
    row = deps.db.get(target)
    is_staff = row is not None and (
        row["status"] in (Status.ADMIN, Status.GARANT) or row["in_admins"] or row["in_garants"]
    )
    if is_staff and not deps.is_owner(author):
        await reply_html(message, "⛔️ Администраторов и гарантов может занести только владелец.")
        return

    if username:
        deps.db.link_username(target, username)

    reason = " ".join(args[1:-1])
    proof = args[-1] if args[-1].lower().startswith("http") else f"https://{args[-1]}"
    status = deps.db.add_report(target, kind, reason, proof, author)
    log.info("Пользователь %s -> %s (занёс %s): %s", target, status, author, reason)

    # Если в базу занесли администратора, у него пропадают админские команды
    await refresh_user(context.bot, deps, target)

    label = "скамер" if status is Status.SCAMMER else "подозрительный"
    total = len(deps.db.reports(target))
    await reply_html(
        message,
        f"✅ {texts.name_link(deps.db.get(target), target)} ({texts.id_text(target)}) в базе: {label}. "
        f"Записей: {total}.",
    )


# ========== Владелец: роли, каналы, списки ==========


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


async def _owner_target(update: Update, context: ContextTypes.DEFAULT_TYPE, usage: str) -> int | None:
    """Проверяет права владельца и возвращает ID цели; при ошибке сам отвечает и возвращает None."""
    deps = get_deps(context)
    message = update.effective_message
    if not deps.is_owner(update.effective_user.id):
        await reply_html(message, texts.OWNER_ONLY)
        return None
    try:
        target = resolve_target(deps, update, context.args[0] if context.args else None)
    except TargetError as exc:
        await reply_html(message, str(exc))
        return None
    if target is None:
        await reply_html(message, f"Формат: <code>{usage}</code>")
        return None
    if deps.is_owner(target):
        await reply_html(message, "⛔️ Роль владельца менять нельзя.")
        return None
    await fill_profile(context.bot, deps, target)
    return target


async def _notify(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> None:
    """Сообщение человеку в личку. Если он не запускал бота, Telegram не даст написать - это нормально."""
    try:
        await context.bot.send_message(user_id, text, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        log.debug("Не удалось уведомить %s: %s", user_id, exc)


def _mention(deps: Deps, user_id: int) -> str:
    return texts.name_link(deps.db.get(user_id), user_id)


async def hire_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = await _owner_target(update, context, "/hire_admin [ID]")
    if target is None:
        return
    deps = get_deps(context)
    deps.db.set_role(target, Status.ADMIN, update.effective_user.id)
    await refresh_user(context.bot, deps, target)
    await reply_html(update.effective_message, f"✅ {_mention(deps, target)} теперь администратор базы.")
    await _notify(context, target, "👮 Вы назначены <b>администратором</b> базы. Команды: /help")


async def hire_garant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = await _owner_target(update, context, "/hire_garant [ID] [канал]")
    if target is None:
        return
    deps = get_deps(context)
    deps.db.set_role(target, Status.GARANT, update.effective_user.id)
    if len(context.args) > 1:
        deps.db.set_channel(target, context.args[1])
    # Если гарантом сделали бывшего администратора - убираем ему админские команды
    await refresh_user(context.bot, deps, target)
    await reply_html(update.effective_message, f"✅ {_mention(deps, target)} теперь гарант базы.")
    await _notify(context, target, "🛡 Вы назначены <b>гарантом</b> базы.")


async def channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/channel ID ссылка - канал гаранта в карточке; без ссылки - убрать."""
    target = await _owner_target(update, context, "/channel [ID] [ссылка на канал]")
    if target is None:
        return
    deps = get_deps(context)
    channel = context.args[1] if len(context.args) > 1 else None
    deps.db.set_channel(target, channel)
    result = f"канал: {texts.esc(channel)}" if channel else "канал убран"
    await reply_html(update.effective_message, f"✅ {_mention(deps, target)} - {result}.")


async def role_norm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = await _owner_target(update, context, "/role_norm [ID]")
    if target is None:
        return
    deps = get_deps(context)
    deps.db.reset(target)
    await refresh_user(context.bot, deps, target)
    await reply_html(
        update.effective_message,
        f"✅ С {_mention(deps, target)} сняты все роли, теперь это обычный пользователь.",
    )


def staff_list_toggle(staff_list: StaffList, enabled: bool, command: str, done: str) -> Handler:
    """Хендлер для /addadmin, /deladmin, /addgarant, /delgarant."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        target = await _owner_target(update, context, f"{command} [ID]")
        if target is None:
            return
        deps = get_deps(context)
        deps.db.set_in_list(target, staff_list, enabled)
        await reply_html(update.effective_message, f"✅ {_mention(deps, target)} {done}.")

    return handler


# ========== Служебное: учёт людей, образцы оформления, ошибки ==========


def _remember(deps: Deps, user: User | None) -> None:
    if user is not None and not user.is_bot:
        deps.db.touch(user.id, user.username, user.full_name)


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускается на каждый апдейт: запоминает людей (для поиска по @юзу) и обновляет меню в группах."""
    deps = get_deps(context)
    _remember(deps, update.effective_user)
    message = update.effective_message
    if message is not None and message.reply_to_message is not None:
        _remember(deps, message.reply_to_message.from_user)

    chat, user = update.effective_chat, update.effective_user
    if chat is not None and user is not None and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await sync_group(context.bot, deps, chat.id, user.id)


async def capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Образец оформления в личке: сохраняет HTML текста и скачивает медиа в captures/.

    Нужен только при настройке текстов; включается через CAPTURE_ID в .env.
    """
    message = update.effective_message
    CAPTURES_DIR.mkdir(exist_ok=True)
    number = len(list(CAPTURES_DIR.glob("*.json"))) + 1
    item: dict[str, object] = {"n": number, "html": message.text_html if message.text else message.caption_html}

    media = message.photo[-1] if message.photo else (
        message.animation or message.video or message.document or message.sticker
    )
    if media is not None:
        file = await media.get_file()
        suffix = (file.file_path or "").rpartition(".")[2]
        path = CAPTURES_DIR / f"{number:02d}_media.{suffix or 'bin'}"
        await file.download_to_drive(path)
        item["file"] = path.name

    entities = message.entities or message.caption_entities
    if emoji_ids := [entity.custom_emoji_id for entity in entities if entity.custom_emoji_id]:
        item["custom_emoji"] = emoji_ids

    (CAPTURES_DIR / f"{number:02d}.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    await message.reply_text(f"💾 Сохранено #{number}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Forbidden):
        # Человек заблокировал бота или бота убрали из чата - это не ошибка в коде
        user = update.effective_user if isinstance(update, Update) else None
        log.warning("Не удалось ответить %s: %s", user.id if user else "?", context.error.message)
        return
    log.error("Ошибка при обработке апдейта %s", update, exc_info=context.error)
