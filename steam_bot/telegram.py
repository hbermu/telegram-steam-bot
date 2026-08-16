"""Bot API client, standard library only.

No venv and no pip on purpose: a SteamOS update replaces /usr wholesale, and a
virtualenv built against one Python version does not survive the next one.

Knows nothing about Stardew. Uploads stream from a temporary file rather than
being assembled in memory, because a GIF can run to tens of megabytes.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


class ConflictError(TelegramError):
    """Another process is polling with the same token."""


class Client:
    def __init__(self, token: str):
        self._token = token

    # -- transport --------------------------------------------------------
    def _url(self, method: str) -> str:
        return API.format(token=self._token, method=method)

    def _read(self, request: urllib.request.Request, timeout: float) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except Exception:
                raise TelegramError(f"HTTP {exc.code}") from exc
        if payload.get("ok"):
            return payload["result"]

        description = payload.get("description", "no description")
        code = payload.get("error_code")
        if code == 409:
            raise ConflictError(description)
        if code == 429:
            retry = payload.get("parameters", {}).get("retry_after", 5)
            log.warning("flood control, waiting %ss", retry)
            time.sleep(retry + 1)
            raise TelegramError(f"429: {description}")
        raise TelegramError(f"{code}: {description}")

    def call(self, method: str, http_timeout: float = 30, **params) -> dict:
        clean = {
            key: (json.dumps(value) if isinstance(value, (dict, list)) else value)
            for key, value in params.items()
            if value is not None
        }
        data = urllib.parse.urlencode(clean).encode()
        request = urllib.request.Request(self._url(method), data=data)
        return self._read(request, http_timeout)

    def upload(self, method: str, field: str, path: Path, http_timeout: float = 300,
               **params) -> dict:
        boundary = uuid.uuid4().hex
        with tempfile.TemporaryFile() as body:
            for key, value in params.items():
                if value is None:
                    continue
                if isinstance(value, (dict, list)):
                    value = json.dumps(value)
                body.write(f"--{boundary}\r\n".encode())
                body.write(
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
                )
                body.write(f"{value}\r\n".encode())

            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            body.write(f"--{boundary}\r\n".encode())
            body.write(
                f'Content-Disposition: form-data; name="{field}"; '
                f'filename="{path.name}"\r\n'.encode()
            )
            body.write(f"Content-Type: {mime}\r\n\r\n".encode())
            with path.open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    body.write(chunk)
            body.write(f"\r\n--{boundary}--\r\n".encode())

            length = body.tell()
            body.seek(0)
            request = urllib.request.Request(
                self._url(method),
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(length),
                },
            )
            return self._read(request, http_timeout)

    # -- methods used by the bot ------------------------------------------
    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: int, poll: int = 50) -> list[dict]:
        # The socket must outlive the long poll itself, hence poll + margin.
        return self.call(
            "getUpdates", http_timeout=poll + 15, offset=offset, timeout=poll,
            allowed_updates=["message", "callback_query"],
        )

    def send_message(self, chat_id, text, reply_markup=None) -> dict:
        return self.call(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
            reply_markup=reply_markup, disable_web_page_preview=True,
        )

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None) -> dict:
        return self.call(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=text,
            parse_mode="HTML", reply_markup=reply_markup,
        )

    def answer_callback_query(self, query_id, text=None) -> dict:
        return self.call("answerCallbackQuery", callback_query_id=query_id, text=text)

    def send_photo(self, chat_id, path: Path, caption=None, reply_markup=None) -> dict:
        return self.upload(
            "sendPhoto", "photo", path, chat_id=chat_id, caption=caption,
            parse_mode="HTML", reply_markup=reply_markup,
        )

    def send_document(self, chat_id, path: Path, caption=None) -> dict:
        return self.upload(
            "sendDocument", "document", path, chat_id=chat_id, caption=caption,
            parse_mode="HTML",
        )

    def send_video(self, chat_id, path: Path, caption=None, width=None,
                   height=None, duration=None) -> dict:
        # Telegram only renders a scrubber and streams progressively when it is
        # told the geometry up front; without it the clip arrives as a file.
        return self.upload(
            "sendVideo", "video", path, chat_id=chat_id, caption=caption,
            parse_mode="HTML", supports_streaming=True,
            width=width, height=height, duration=duration,
        )

    def send_animation(self, chat_id, path: Path, caption=None, width=None,
                       height=None, duration=None) -> dict:
        # Takes H.264 as happily as a real GIF — a Telegram "GIF" is MP4 under
        # the hood — and that is how a clip gets to autoplay and loop.
        return self.upload(
            "sendAnimation", "animation", path, chat_id=chat_id, caption=caption,
            parse_mode="HTML", width=width, height=height, duration=duration,
        )


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """Inline keyboard from (label, callback_data) pairs.

    callback_data is capped at 64 bytes by the API, which is why buttons carry
    ``gif|445722261|Farm`` and never a farm name.
    """
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }
