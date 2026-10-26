from typing import Any

import httpx

SERVICE = "moodle_mobile_app"


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

    async def login(self, username: str, password: str) -> str:
        r = await self.http.post(
            f"{self.base}/login/token.php",
            data={"username": username, "password": password, "service": SERVICE},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("token"):
            return data["token"]
        code = data.get("errorcode", "unknown")
        cls = InvalidLogin if code == "invalidlogin" else MoodleError
        raise cls(code, data.get("error", ""))

    async def call(self, token: str, function: str, **params: Any) -> Any:
        r = await self.http.post(
            f"{self.base}/webservice/rest/server.php",
            data={
                "wstoken": token,
                "wsfunction": function,
                "moodlewsrestformat": "json",
                **_flatten(params),
            },
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "exception" in data:
            code = data.get("errorcode", "unknown")
            cls = InvalidToken if code == "invalidtoken" else MoodleError
            raise cls(code, data.get("message", ""))
        return data
