import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    token_key: str
    moodle_url: str
    db_path: str
    tz: ZoneInfo
    poll_minutes: int
    digest_day: str
    digest_hour: int
    remind_hour: int
    telegram_proxy: str | None


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
    token_key = os.getenv("TOKEN_KEY")
    if not token_key:
        raise SystemExit(
            "TOKEN_KEY не задан. Сгенерируй и впиши в .env:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    moodle_url = os.getenv("MOODLE_URL", "https://edu.mmcs.sfedu.ru").rstrip("/")
    if not moodle_url.startswith("https://"):
        raise SystemExit("MOODLE_URL должен начинаться с https:// — иначе пароли пойдут открытым текстом")
    return Config(
        bot_token=token,
        token_key=token_key,
        moodle_url=moodle_url,
        db_path=os.getenv("DB_PATH", "botik.sqlite3"),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        poll_minutes=int(os.getenv("POLL_MINUTES", "60")),
        digest_day=os.getenv("DIGEST_DAY", "mon"),
        digest_hour=int(os.getenv("DIGEST_HOUR", "9")),
        remind_hour=int(os.getenv("REMIND_HOUR", "9")),
        telegram_proxy=os.getenv("TELEGRAM_PROXY") or None,
    )
