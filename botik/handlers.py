import logging
import random

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from botik import texts
from botik.moodle import InvalidToken, Moodle, parse_token
from botik.storage import Storage
from botik.sync import Syncer

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")

TG_LIMIT = 4000


class TokenInput(StatesGroup):
    waiting = State()


async def send(bot: Bot, chat_id: int, text: str) -> None:
    chunk = ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > TG_LIMIT:
            await bot.send_message(chat_id, chunk)
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        await bot.send_message(chat_id, chunk)


async def run_sync(bot: Bot, syncer: Syncer, store: Storage, chat_id: int) -> int:
    try:
        msgs = await syncer.sync(chat_id)
    except InvalidToken:
        store.deactivate(chat_id)
        await send(bot, chat_id, texts.TOKEN_EXPIRED)
        return 0
    try:
        for m in msgs:
            await send(bot, chat_id, m)
    except TelegramForbiddenError:
        log.info("chat %s blocked the bot, deactivating", chat_id)
        store.deactivate(chat_id)
    return len(msgs)


async def apply_token(
    message: Message, text: str, bot: Bot, moodle: Moodle, store: Storage, syncer: Syncer
) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    token = parse_token(text)
    if not token:
        await message.answer(texts.TOKEN_FORMAT)
        return

    status = await message.answer(texts.TOKEN_CHECKING)
    try:
        info = await moodle.call(token, "core_webservice_get_site_info")
    except InvalidToken:
        await status.edit_text(texts.TOKEN_BAD)
        return
    except Exception as e:
        log.exception("token check failed")
        await status.edit_text(texts.TOKEN_FAIL.format(err=type(e).__name__))
        return

    chat_id = message.chat.id
    store.save_user(chat_id, token, info["userid"], info.get("fullname", ""))
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


@router.message(Command("instruction"))
async def cmd_instruction(message: Message):
    await message.answer(texts.INSTRUCTION)


@router.message(Command("token"))
async def cmd_token(
    message: Message, command: CommandObject, state: FSMContext,
    bot: Bot, moodle: Moodle, store: Storage, syncer: Syncer,
):
    if command.args:
        await state.clear()
        await apply_token(message, command.args, bot, moodle, store, syncer)
        return
    await state.set_state(TokenInput.waiting)
    await message.answer(texts.ASK_TOKEN)


@router.message(TokenInput.waiting, F.text)
async def got_token(
    message: Message, state: FSMContext, bot: Bot, moodle: Moodle, store: Storage, syncer: Syncer
):
    await state.clear()
    await apply_token(message, message.text, bot, moodle, store, syncer)


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
    await message.answer(random.choice(texts.CHECKING))
    if await run_sync(bot, syncer, store, message.chat.id) == 0 and store.get_user(message.chat.id).active:
        await message.answer(random.choice(texts.NOTHING_NEW))


@router.message(Command("week"))
async def cmd_week(message: Message, bot: Bot, store: Storage, syncer: Syncer):
    user = store.get_user(message.chat.id)
    if not user or not user.active:
        await message.answer(texts.NEED_LOGIN)
        return
    await message.answer(random.choice(texts.CHECKING))
    await run_sync(bot, syncer, store, message.chat.id)
    if store.get_user(message.chat.id).active:
        await send(bot, message.chat.id, syncer.digest(message.chat.id, weekly=False))


@router.message(F.text)
async def any_text(message: Message, bot: Bot, moodle: Moodle, store: Storage, syncer: Syncer):
    if not message.text.startswith("/") and parse_token(message.text):
        await apply_token(message, message.text, bot, moodle, store, syncer)
    else:
        await message.answer(texts.UNKNOWN)
