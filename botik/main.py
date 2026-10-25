import logging
from datetime import datetime, timedelta

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from botik.config import load_config
from botik.handlers import router, run_sync, send
from botik.moodle import Moodle
from botik.storage import Storage
from botik.sync import Syncer

log = logging.getLogger("botik")

COMMANDS = [
    BotCommand(command="week", description="Дедлайны на неделю"),
    BotCommand(command="check", description="Проверить Moodle прямо сейчас"),
    BotCommand(command="token", description="Подключить Moodle по токену"),
    BotCommand(command="instruction", description="Как получить токен"),
    BotCommand(command="logout", description="Забыть меня"),
    BotCommand(command="help", description="Что я умею"),
]


async def poll_all(bot: Bot, syncer: Syncer, store: Storage) -> None:
    users = store.active_users()
    log.info("polling %d users", len(users))
    for user in users:
        try:
            await run_sync(bot, syncer, store, user.chat_id)
        except Exception:
            log.exception("sync failed for chat %s", user.chat_id)


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
    cfg = load_config()
    store = Storage(cfg.db_path, cfg.token_key)

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "botik/1.0"}) as http:
        moodle = Moodle(cfg.moodle_url, http)
        syncer = Syncer(moodle, store, cfg)
        bot = Bot(
            cfg.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
        dp = Dispatcher(storage=MemoryStorage(), moodle=moodle, store=store, syncer=syncer)
        dp.include_router(router)

        sched = AsyncIOScheduler(timezone=cfg.tz)
        next_hour = datetime.now(cfg.tz).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        sched.add_job(
            poll_all, "interval", minutes=cfg.poll_minutes, start_date=next_hour,
            args=[bot, syncer, store], max_instances=1, coalesce=True,
        )
        sched.add_job(
            weekly_digest, "cron", day_of_week=cfg.digest_day, hour=cfg.digest_hour, minute=10,
            args=[bot, syncer, store],
        )
        sched.start()

        await bot.set_my_commands(COMMANDS)
        log.info("bot started, first poll at %s", next_hour)
        try:
            await dp.start_polling(bot)
        finally:
            sched.shutdown(wait=False)
            await bot.session.close()
