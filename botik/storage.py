import re
import sqlite3
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

PLAIN_TOKEN_RE = re.compile(r"[0-9a-f]{32}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id       INTEGER PRIMARY KEY,
    token         TEXT    NOT NULL,
    moodle_userid INTEGER NOT NULL,
    fullname      TEXT,
    initialized   INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1
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


@dataclass
class User:
    chat_id: int
    token: str
    moodle_userid: int
    fullname: str
    initialized: bool
    active: bool


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
    def __init__(self, path: str, token_key: str):
        self.fernet = Fernet(token_key)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._encrypt_plain_tokens()
        self._check_key()

    def _encrypt_plain_tokens(self) -> None:
        rows = self.db.execute("SELECT chat_id, token FROM users").fetchall()
        with self.db:
            for r in rows:
                if PLAIN_TOKEN_RE.fullmatch(r["token"]):
                    self.db.execute(
                        "UPDATE users SET token = ? WHERE chat_id = ?",
                        (self._encrypt(r["token"]), r["chat_id"]),
                    )

    def _check_key(self) -> None:
        row = self.db.execute("SELECT token FROM users LIMIT 1").fetchone()
        if row:
            try:
                self._decrypt(row["token"])
            except InvalidToken:
                raise SystemExit("TOKEN_KEY не подходит к базе: токены зашифрованы другим ключом")

    def _encrypt(self, token: str) -> str:
        return self.fernet.encrypt(token.encode()).decode()

    def _decrypt(self, token: str) -> str:
        return self.fernet.decrypt(token.encode()).decode()

    def _user(self, row: sqlite3.Row) -> User:
        return User(
            chat_id=row["chat_id"], token=self._decrypt(row["token"]),
            moodle_userid=row["moodle_userid"], fullname=row["fullname"] or "",
            initialized=bool(row["initialized"]), active=bool(row["active"]),
        )

    def get_user(self, chat_id: int) -> User | None:
        row = self.db.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        return self._user(row) if row else None

    def active_users(self) -> list[User]:
        rows = self.db.execute("SELECT * FROM users WHERE active = 1").fetchall()
        return [self._user(r) for r in rows]

    def save_user(self, chat_id: int, token: str, moodle_userid: int, fullname: str) -> None:
        old = self.get_user(chat_id)
        with self.db:
            if old and old.moodle_userid != moodle_userid:
                self._wipe(chat_id)
            self.db.execute(
                """INSERT INTO users (chat_id, token, moodle_userid, fullname, initialized, active)
                   VALUES (?, ?, ?, ?, 0, 1)
                   ON CONFLICT (chat_id) DO UPDATE SET
                     token = excluded.token, moodle_userid = excluded.moodle_userid,
                     fullname = excluded.fullname, active = 1""",
                (chat_id, self._encrypt(token), moodle_userid, fullname),
            )

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

    def _wipe(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM state WHERE chat_id = ?", (chat_id,))
        self.db.execute("DELETE FROM assignments WHERE chat_id = ?", (chat_id,))

    def get_state(self, chat_id: int, kind: str) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT key, value FROM state WHERE chat_id = ? AND kind = ?", (chat_id, kind)
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_state(self, chat_id: int, kind: str, key: str, value: str = "1") -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO state (chat_id, kind, key, value) VALUES (?, ?, ?, ?)",
                (chat_id, kind, key, value),
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
                    (chat_id, a.id, a.course, a.name, a.url, a.opens, a.due, a.cutoff,
                     None if a.done is None else int(a.done))
                    for a in items
                ],
            )

    def assignments(self, chat_id: int) -> list[Assignment]:
        rows = self.db.execute("SELECT * FROM assignments WHERE chat_id = ?", (chat_id,)).fetchall()
        return [
            Assignment(
                id=r["assign_id"], course=r["course"], name=r["name"], url=r["url"],
                opens=r["opens"], due=r["due"], cutoff=r["cutoff"],
                done=None if r["done"] is None else bool(r["done"]),
            )
            for r in rows
        ]
