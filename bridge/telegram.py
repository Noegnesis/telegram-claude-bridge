import re

import httpx

_TOKEN_RE = re.compile(r"/bot(\d+:[A-Za-z0-9_-]+)/")


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("/bot<REDACTED>/", text)


class TelegramClient:
    def __init__(self, token: str, timeout: float = 30.0):
        self._token = token
        self._client = httpx.Client(
            base_url="https://api.telegram.org",
            timeout=timeout,
        )
        self._http_timeout = timeout

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self) -> str:
        return "TelegramClient(token=<REDACTED>)"

    def _check(self, r: httpx.Response) -> None:
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise httpx.HTTPStatusError(
                _redact(str(e)), request=e.request, response=e.response
            ) from None

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict]:
        r = self._client.get(
            f"/bot{self._token}/getUpdates",
            params={"offset": offset, "timeout": timeout},
            timeout=self._http_timeout + timeout,
        )
        self._check(r)
        return r.json()["result"]

    def get_file_url(self, file_id: str) -> str:
        r = self._client.get(
            f"/bot{self._token}/getFile",
            params={"file_id": file_id},
        )
        self._check(r)
        path = r.json()["result"]["file_path"]
        return f"https://api.telegram.org/file/bot{self._token}/{path}"

    def send_message(self, chat_id: int, text: str) -> None:
        r = self._client.post(
            f"/bot{self._token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        self._check(r)
