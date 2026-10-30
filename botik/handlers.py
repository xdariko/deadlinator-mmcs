import logging
import random
from collections.abc import Awaitable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from botik import texts
from botik.moodle import InvalidLogin, InvalidToken, Moodle, MoodleError
from botik.storage import Storage
from botik.sync import Syncer

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")

TG_LIMIT = 4000


class LoginInput(StatesGroup):
    username = State()
    password = State()


async def send(bot: Bot, chat_id: int, text: str) -> None:
    chunk = ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > TG_LIMIT:
            await bot.send_message(chat_id, chunk)
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        await bot.send_message(chat_id, chunk)


async def delete(message: Message) -> bool:
    try:
        await message.delete()
        return True
    except TelegramBadRequest:
        return False


async def run_sync(bot: Bot, syncer: Syncer, store: Storage, chat_id: int) -> int:
    return await deliver(bot, store, chat_id, syncer.sync(chat_id))


async def deliver(bot: Bot, store: Storage, chat_id: int, job: Awaitable[list[str]]) -> int:
    try:
        msgs = await job
    except (InvalidToken, InvalidLogin):
        store.deactivate(chat_id)
        await send(bot, chat_id, texts.LOGIN_EXPIRED)
        return 0
    try:
        for m in msgs:
            await send(bot, chat_id, m)
    except TelegramForbiddenError:
        log.info("chat %s blocked the bot, deactivating", chat_id)
        store.deactivate(chat_id)
    return len(msgs)


async def apply_login(
    message: Message, username: str, password: str,
    bot: Bot, moodle: Moodle, store: Storage, syncer: Syncer,
) -> None:
    status = await message.answer(texts.LOGIN_CHECKING)
    try:
        token = await moodle.login(username, password)
        info = await moodle.call(token, "core_webservice_get_site_info")
    except InvalidLogin:
        await status.edit_text(texts.LOGIN_BAD)
        return
    except MoodleError as e:
        log.warning("login refused: %s", e.errorcode)
        await status.edit_text(texts.LOGIN_REFUSED.format(err=e.errorcode))
        return
    except Exception as e:
        log.exception("login failed")
        await status.edit_text(texts.LOGIN_FAIL.format(err=type(e).__name__))
        return

    chat_id = message.chat.id
    store.save_user(chat_id, username, password, token, info["userid"], info.get("fullname", ""))
    name = info.get("firstname") or info.get("fullname") or "человек"
    await status.edit_text(texts.SCANNING.format(name=name))
    await run_sync(bot, syncer, store, chat_id)
    await message.answer(texts.ready(len(store.get_state(chat_id, "course"))))


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(texts.CANCELLED)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message):
    await message.answer(texts.START)


@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext):
    await state.set_state(LoginInput.username)
    await message.answer(texts.ASK_USERNAME)


@router.message(LoginInput.username, F.text)
async def got_username(message: Message, state: FSMContext):
    username = message.text.strip()
    await delete(message)
    await state.update_data(username=username)
    await state.set_state(LoginInput.password)
    await message.answer(texts.ASK_PASSWORD)


@router.message(LoginInput.password, F.text)
async def got_password(
    message: Message, state: FSMContext, bot: Bot, moodle: Moodle, store: Storage, syncer: Syncer
):
    password = message.text
    deleted = await delete(message)
    username = (await state.get_data()).get("username", "")
    await state.clear()
    if not deleted:
        await message.answer(texts.PASSWORD_NOT_DELETED)
    await apply_login(message, username, password, bot, moodle, store, syncer)


@router.message(Command("logout"))
async def cmd_logout(message: Message, store: Storage):
    store.delete_user(message.chat.id)
    await message.answer(texts.LOGGED_OUT)


@router.message(Command("check"))
async def cmd_check(message: Message, bot: Bot, store: Storage, syncer: Syncer):
    user = store.get_user(message.chat.id)
    if not user or not user.active:
        await message.answer(texts.NEED_LOGIN)
        return
    left = syncer.cooldown_left(user)
    if left > 0:
        await message.answer(texts.check_cooldown(user.last_sync, user.last_sync + left, syncer.cfg.tz))
        return
    await message.answer(random.choice(texts.CHECKING))
    if await run_sync(bot, syncer, store, message.chat.id) == 0 and store.get_user(message.chat.id).active:
        await message.answer(random.choice(texts.NOTHING_NEW))


@router.message(Command("week"))
async def cmd_week(message: Message, bot: Bot, store: Storage, syncer: Syncer):
    user = store.get_user(message.chat.id)
    if not user or not user.active:
        await message.answer(texts.NEED_LOGIN)
        return
    # недавно проверяли — отвечаем из базы, Moodle не трогаем
    cached = syncer.cooldown_left(user) > 0
    if not cached:
        await message.answer(random.choice(texts.CHECKING))
        await run_sync(bot, syncer, store, message.chat.id)
    if store.get_user(message.chat.id).active:
        text = syncer.digest(message.chat.id, weekly=False)
        if cached:
            text += texts.as_of(user.last_sync, syncer.cfg.tz)
        await send(bot, message.chat.id, text)


@router.message(F.text)
async def any_text(message: Message):
    # Бот мог перезапуститься посреди входа и забыть, что ждёт пароль, —
    # тогда пароль прилетит сюда. Не оставляем в чате ничего непонятного.
    if not message.text.startswith("/") and await delete(message):
        await message.answer(texts.UNKNOWN_DELETED)
    else:
        await message.answer(texts.UNKNOWN)
