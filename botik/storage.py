import re
import sqlite3
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken

PLAIN_TOKEN_RE = re.compile(r"[0-9a-f]{32}")

# Шифруется всё, что говорит о человеке: логин, пароль, токен, имя, оценки,
# названия курсов и заданий. В открытом виде остаются только id и даты.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id       INTEGER PRIMARY KEY,
    token         TEXT    NOT NULL,
    moodle_userid INTEGER NOT NULL,
    fullname      TEXT,
    initialized   INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    username      TEXT,
    password      TEXT,
    last_sync     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS state (
    chat_id INTEGER NOT NULL,
    kind    TEXT    NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT,
    PRIMARY KEY (chat_id, kind, key)
);
CREATE TABLE IF NOT EXISTS assignments (
    chat_id   INTEGER NOT NULL,
    assign_id INTEGER NOT NULL,
    course    TEXT,
    name      TEXT,
    url       TEXT,
    opens     INTEGER,
    due       INTEGER,
    cutoff    INTEGER,
    done      INTEGER,
    PRIMARY KEY (chat_id, assign_id)
);
"""
SCHEMA_VERSION = 2


@dataclass
class User:
    chat_id: int
    token: str = field(repr=False)
    moodle_userid: int
    fullname: str = field(repr=False)
    initialized: bool
    active: bool
    username: str | None = field(repr=False)
    password: str | None = field(repr=False)
    last_sync: int


@dataclass
class Assignment:
    id: int
    course: str
    name: str
    url: str
    opens: int
    due: int
    cutoff: int
    done: bool | None


class Storage:
    def __init__(self, path: str, key: str):
        self.fernet = Fernet(key)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA secure_delete = ON")
        # WAL + NORMAL: запись не дёргает диск на каждый коммит, а их за проверку сотни
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self._check_key()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(users)")}
        if version >= 1:
            with self.db:
                if "last_sync" not in cols:
                    self.db.execute("ALTER TABLE users ADD COLUMN last_sync INTEGER NOT NULL DEFAULT 0")
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        with self.db:
            if "last_sync" not in cols:
                self.db.execute("ALTER TABLE users ADD COLUMN last_sync INTEGER NOT NULL DEFAULT 0")
            for col in ("username", "password"):
                if col not in cols:
                    self.db.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            for r in self.db.execute("SELECT chat_id, token, fullname FROM users").fetchall():
                token = r["token"]
                if PLAIN_TOKEN_RE.fullmatch(token):
                    token = self._enc(token)
                self.db.execute(
                    "UPDATE users SET token = ?, fullname = ? WHERE chat_id = ?",
                    (token, self._enc(r["fullname"]), r["chat_id"]),
                )
            for r in self.db.execute("SELECT rowid, value FROM state").fetchall():
                self.db.execute(
                    "UPDATE state SET value = ? WHERE rowid = ?", (self._enc(r["value"]), r["rowid"])
                )
            for r in self.db.execute("SELECT rowid, course, name, url FROM assignments").fetchall():
                self.db.execute(
                    "UPDATE assignments SET course = ?, name = ?, url = ? WHERE rowid = ?",
                    (self._enc(r["course"]), self._enc(r["name"]), self._enc(r["url"]), r["rowid"]),
                )
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.execute("VACUUM")

    def _check_key(self) -> None:
        row = self.db.execute("SELECT token FROM users LIMIT 1").fetchone()
        if row:
            try:
                self._dec(row["token"])
            except InvalidToken:
                raise SystemExit("TOKEN_KEY не подходит к базе: данные зашифрованы другим ключом")

    def _enc(self, value: str | None) -> str | None:
        return None if value is None else self.fernet.encrypt(value.encode()).decode()

    def _dec(self, value: str | None) -> str | None:
        return None if value is None else self.fernet.decrypt(value.encode()).decode()

    def _user(self, row: sqlite3.Row) -> User:
        return User(
            chat_id=row["chat_id"], token=self._dec(row["token"]),
            moodle_userid=row["moodle_userid"], fullname=self._dec(row["fullname"]) or "",
            initialized=bool(row["initialized"]), active=bool(row["active"]),
            username=self._dec(row["username"]), password=self._dec(row["password"]),
            last_sync=row["last_sync"],
        )

    def get_user(self, chat_id: int) -> User | None:
        row = self.db.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        return self._user(row) if row else None

    def active_users(self) -> list[User]:
        rows = self.db.execute("SELECT * FROM users WHERE active = 1").fetchall()
        return [self._user(r) for r in rows]

    def count_active(self) -> int:
        return self.db.execute("SELECT count(*) FROM users WHERE active = 1").fetchone()[0]

    def due_users(self, synced_before: int, limit: int) -> list[int]:
        rows = self.db.execute(
            "SELECT chat_id FROM users WHERE active = 1 AND last_sync <= ? ORDER BY last_sync LIMIT ?",
            (synced_before, limit),
        ).fetchall()
        return [r["chat_id"] for r in rows]

    def set_synced(self, chat_id: int, ts: int) -> None:
        with self.db:
            self.db.execute("UPDATE users SET last_sync = ? WHERE chat_id = ?", (ts, chat_id))

    def set_done(self, chat_id: int, assign_id: int, done: bool) -> None:
        with self.db:
            self.db.execute(
                "UPDATE assignments SET done = ? WHERE chat_id = ? AND assign_id = ?",
                (int(done), chat_id, assign_id),
            )

    def save_user(
        self, chat_id: int, username: str, password: str, token: str, moodle_userid: int, fullname: str
    ) -> None:
        old = self.get_user(chat_id)
        with self.db:
            if old and old.moodle_userid != moodle_userid:
                self._wipe(chat_id)
            self.db.execute(
                """INSERT INTO users
                     (chat_id, token, moodle_userid, fullname, initialized, active, username, password)
                   VALUES (?, ?, ?, ?, 0, 1, ?, ?)
                   ON CONFLICT (chat_id) DO UPDATE SET
                     token = excluded.token, moodle_userid = excluded.moodle_userid,
                     fullname = excluded.fullname, active = 1,
                     username = excluded.username, password = excluded.password""",
                (chat_id, self._enc(token), moodle_userid, self._enc(fullname),
                 self._enc(username), self._enc(password)),
            )

    def set_token(self, chat_id: int, token: str) -> None:
        with self.db:
            self.db.execute("UPDATE users SET token = ? WHERE chat_id = ?", (self._enc(token), chat_id))

    def set_initialized(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE users SET initialized = 1 WHERE chat_id = ?", (chat_id,))

    def deactivate(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE users SET active = 0 WHERE chat_id = ?", (chat_id,))

    def delete_user(self, chat_id: int) -> None:
        with self.db:
            self._wipe(chat_id)
            self.db.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        self.db.execute("VACUUM")

    def _wipe(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM state WHERE chat_id = ?", (chat_id,))
        self.db.execute("DELETE FROM assignments WHERE chat_id = ?", (chat_id,))

    def get_state(self, chat_id: int, kind: str) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT key, value FROM state WHERE chat_id = ? AND kind = ?", (chat_id, kind)
        ).fetchall()
        return {r["key"]: self._dec(r["value"]) for r in rows}

    def set_state(self, chat_id: int, kind: str, key: str, value: str = "1") -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO state (chat_id, kind, key, value) VALUES (?, ?, ?, ?)",
                (chat_id, kind, key, self._enc(value)),
            )

    def del_state(self, chat_id: int, kind: str, key: str) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM state WHERE chat_id = ? AND kind = ? AND key = ?", (chat_id, kind, key)
            )

    def replace_assignments(self, chat_id: int, items: list[Assignment]) -> None:
        with self.db:
            self.db.execute("DELETE FROM assignments WHERE chat_id = ?", (chat_id,))
            self.db.executemany(
                "INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (chat_id, a.id, self._enc(a.course), self._enc(a.name), self._enc(a.url),
                     a.opens, a.due, a.cutoff, None if a.done is None else int(a.done))
                    for a in items
                ],
            )

    def assignments(self, chat_id: int) -> list[Assignment]:
        rows = self.db.execute("SELECT * FROM assignments WHERE chat_id = ?", (chat_id,)).fetchall()
        return [
            Assignment(
                id=r["assign_id"], course=self._dec(r["course"]), name=self._dec(r["name"]),
                url=self._dec(r["url"]), opens=r["opens"], due=r["due"], cutoff=r["cutoff"],
                done=None if r["done"] is None else bool(r["done"]),
            )
            for r in rows
        ]
