import asyncio
import time
from typing import Any

import httpx

SERVICE = "moodle_mobile_app"
# Общий лимит на все запросы бота к Moodle: не больше MAX_PARALLEL одновременно
# и не чаще одного в MIN_GAP секунд — чтобы не перегрузить ни свой сервер, ни Moodle
MAX_PARALLEL = 3
MIN_GAP = 0.3


class MoodleError(Exception):
    def __init__(self, errorcode: str, message: str):
        super().__init__(f"{errorcode}: {message}")
        self.errorcode = errorcode
        self.message = message


class InvalidToken(MoodleError):
    pass


class InvalidLogin(MoodleError):
    pass


def _flatten(params: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        elif isinstance(v, (list, tuple)):
            out.update(_flatten(dict(enumerate(v)), key))
        elif isinstance(v, bool):
            out[key] = int(v)
        else:
            out[key] = v
    return out


class Moodle:
    def __init__(self, base_url: str, http: httpx.AsyncClient):
        self.base = base_url
        self.http = http
        self._parallel = asyncio.Semaphore(MAX_PARALLEL)
        self._pace = asyncio.Lock()
        self._next_at = 0.0

    async def _post(self, path: str, data: dict[str, Any]) -> Any:
        async with self._parallel:
            async with self._pace:
                wait = self._next_at - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._next_at = time.monotonic() + MIN_GAP
            r = await self.http.post(f"{self.base}{path}", data=data)
            r.raise_for_status()
            return r.json()

    async def login(self, username: str, password: str) -> str:
        data = await self._post(
            "/login/token.php", {"username": username, "password": password, "service": SERVICE}
        )
        if data.get("token"):
            return data["token"]
        code = data.get("errorcode", "unknown")
        cls = InvalidLogin if code == "invalidlogin" else MoodleError
        raise cls(code, data.get("error", ""))

    async def call(self, token: str, function: str, **params: Any) -> Any:
        data = await self._post(
            "/webservice/rest/server.php",
            {
                "wstoken": token,
                "wsfunction": function,
                "moodlewsrestformat": "json",
                **_flatten(params),
            },
        )
        if isinstance(data, dict) and "exception" in data:
            code = data.get("errorcode", "unknown")
            cls = InvalidToken if code == "invalidtoken" else MoodleError
            raise cls(code, data.get("message", ""))
        return data
