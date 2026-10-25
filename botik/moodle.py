import base64
import binascii
import re
from typing import Any
from urllib.parse import unquote

import httpx

TOKEN_RE = re.compile(r"\b[0-9a-f]{32}\b")


class MoodleError(Exception):
    def __init__(self, errorcode: str, message: str):
        super().__init__(f"{errorcode}: {message}")
        self.errorcode = errorcode
        self.message = message


class InvalidToken(MoodleError):
    pass


def parse_token(text: str) -> str | None:
    text = unquote(text.strip())
    m = re.search(r"token=([A-Za-z0-9+/=_-]+)", text)
    candidates = [m.group(1)] if m else []
    candidates.append(text)
    for raw in candidates:
        try:
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        parts = decoded.split(":::")
        if len(parts) >= 2 and TOKEN_RE.fullmatch(parts[1]):
            return parts[1]
    m = TOKEN_RE.search(text.lower())
    return m.group(0) if m else None


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
