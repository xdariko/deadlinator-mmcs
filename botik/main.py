import asyncio
import logging
import math
import time

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from botik.config import load_config
from botik.handlers import deliver, router, run_sync, send
from botik.moodle import Moodle
from botik.storage import Storage
from botik.sync import Syncer

log = logging.getLogger("botik")

TICK_MINUTES = 5
RETRY_SECONDS = 3600

COMMANDS = [
    BotCommand(command="week", description="Дедлайны на неделю"),
    BotCommand(command="check", description="Проверить Moodle прямо сейчас"),
    BotCommand(command="login", description="Войти в Moodle"),
    BotCommand(command="logout", description="Забыть меня"),
    BotCommand(command="help", description="Что я умею"),
]


async def poll_due(bot: Bot, syncer: Syncer, store: Storage) -> None:
    # Каждый TICK_MINUTES проверяем небольшую порцию тех, кого дольше всех не проверяли,
    # так что за POLL_HOURS обходим всех равномерно, без пиковой нагрузки.
    period = syncer.cfg.poll_hours * 3600
    batch = math.ceil(store.count_active() * TICK_MINUTES * 60 / period)
    due = store.due_users(int(time.time()) - period, batch)
    if not due:
        return
    started = time.monotonic()
    await asyncio.gather(*(poll_one(bot, syncer, store, chat_id) for chat_id in due))
    log.info("polled %d users in %.0fs", len(due), time.monotonic() - started)


async def poll_one(bot: Bot, syncer: Syncer, store: Storage, chat_id: int) -> None:
    try:
        await run_sync(bot, syncer, store, chat_id)
    except Exception:
        log.exception("sync failed for chat %s", chat_id)
        # не долбим Moodle повторами, но и не ждём полный круг — пробуем снова через час
        store.set_synced(chat_id, int(time.time()) - syncer.cfg.poll_hours * 3600 + RETRY_SECONDS)


async def morning(bot: Bot, syncer: Syncer, store: Storage) -> None:
    started = time.monotonic()
    users = store.active_users()
    for user in users:
        try:
            await deliver(bot, store, user.chat_id, syncer.morning(user.chat_id))
        except Exception:
            log.exception("morning reminder failed for chat %s", user.chat_id)
    log.info("morning reminders for %d users in %.0fs", len(users), time.monotonic() - started)


async def weekly_digest(bot: Bot, syncer: Syncer, store: Storage) -> None:
    for user in store.active_users():
        try:
            await send(bot, user.chat_id, syncer.digest(user.chat_id, weekly=True))
        except TelegramForbiddenError:
            store.deactivate(user.chat_id)
        except Exception:
            log.exception("digest failed for chat %s", user.chat_id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на INFO пишет каждый запрос; в логах не должно быть ничего про запросы к Moodle
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config()
    store = Storage(cfg.db_path, cfg.token_key)

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "botik/1.0"}) as http:
        moodle = Moodle(cfg.moodle_url, http)
        syncer = Syncer(moodle, store, cfg)
        bot = Bot(
            cfg.bot_token,
            session=AiohttpSession(proxy=cfg.telegram_proxy),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
        dp = Dispatcher(storage=MemoryStorage(), moodle=moodle, store=store, syncer=syncer)
        dp.include_router(router)

        sched = AsyncIOScheduler(timezone=cfg.tz)
        sched.add_job(
            poll_due, "interval", minutes=TICK_MINUTES,
            args=[bot, syncer, store], max_instances=1, coalesce=True,
        )
        sched.add_job(
            morning, "cron", hour=cfg.remind_hour, minute=0,
            args=[bot, syncer, store], max_instances=1, misfire_grace_time=3600,
        )
        sched.add_job(
            weekly_digest, "cron", day_of_week=cfg.digest_day, hour=cfg.digest_hour, minute=10,
            args=[bot, syncer, store],
        )
        sched.start()

        await bot.set_my_commands(COMMANDS)
        log.info(
            "bot started: %d users, each checked every %d h, morning reminder at %d:00",
            store.count_active(), cfg.poll_hours, cfg.remind_hour,
        )
        try:
            await dp.start_polling(bot)
        finally:
            sched.shutdown(wait=False)
            await bot.session.close()
