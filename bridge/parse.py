import os
import tempfile
from pathlib import Path
from typing import Callable, Optional

import httpx


MEDIA_DIR = Path.home() / "agents" / "media" / "tg"

_UNSUPPORTED_TYPES = {
    "photo": "image",
    "document": "document",
    "video": "video",
    "sticker": "sticker",
    "animation": "GIF",
    "location": "location",
    "contact": "contact",
    "poll": "poll",
    "venue": "venue",
    "video_note": "video note",
    "dice": "dice roll",
}


def detect_message_type(msg: dict) -> Optional[str]:
    """Return human-readable name for an unsupported message type, or None.

    None means the type is handled by extract_text (text, voice, audio, photo,
    document) or has no recognizable content. Photo and document remain in
    _UNSUPPORTED_TYPES but are gated by the `handled` early-return.
    """
    handled = {"text", "voice", "audio", "photo", "document"}
    for key in handled:
        if key in msg:
            return None
    for key, name in _UNSUPPORTED_TYPES.items():
        if key in msg:
            return name
    return None


def is_authorized(update: dict, paired_user_id: int) -> bool:
    msg = update.get("message")
    if not msg:
        return False
    return msg.get("from", {}).get("id") == paired_user_id


def extract_text(
    update: dict,
    telegram_client,
    transcribe_fn: Optional[Callable[[str], str]],
) -> Optional[str]:
    msg = update.get("message", {})
    if "text" in msg:
        return msg["text"].strip()
    if "document" in msg:
        return _download_document(msg, telegram_client)
    if "photo" in msg:
        return _download_photo(msg, telegram_client)
    if "voice" in msg or "audio" in msg:
        return _transcribe(msg, telegram_client, transcribe_fn)
    return None


def _download_to_media(telegram_client, file_id: str, suffix: str = "",
                        filename: Optional[str] = None) -> Path:
    """Download a Telegram file_id to MEDIA_DIR.

    Naming: explicit filename if provided, else `<file_id><suffix>`. Dedupes
    by skipping the download if the target path already exists.
    """
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if filename:
        # Strip directory components and reject empty/dot names
        safe_name = Path(filename).name
        if not safe_name or safe_name in {".", ".."}:
            safe_name = f"{file_id}.bin"
        path = MEDIA_DIR / safe_name
    else:
        path = MEDIA_DIR / f"{file_id}{suffix}"
    if path.exists():
        return path
    url = telegram_client.get_file_url(file_id)
    with httpx.stream("GET", url, timeout=60) as r:
        r.raise_for_status()
        with path.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return path


def _download_document(msg: dict, telegram_client) -> Optional[str]:
    if telegram_client is None:
        return None
    doc = msg.get("document", {})
    file_id = doc.get("file_id")
    if not file_id:
        return None
    file_name = doc.get("file_name") or f"{file_id}.bin"
    mime = doc.get("mime_type", "application/octet-stream")
    path = _download_to_media(telegram_client, file_id, suffix="", filename=file_name)
    caption = msg.get("caption", "").strip()
    base = f"[document: {file_name} ({mime}) at {path}]"
    return f"{base}\n{caption}" if caption else base


def _download_photo(msg: dict, telegram_client) -> Optional[str]:
    if telegram_client is None:
        return None
    photos = msg.get("photo") or []
    if not photos:
        return None
    largest = photos[-1]
    file_id = largest["file_id"]
    path = _download_to_media(telegram_client, file_id, suffix=".jpg")
    caption = msg.get("caption", "").strip()
    base = f"[image: {path}]"
    return f"{base}\n{caption}" if caption else base


def _transcribe(msg: dict, telegram_client, transcribe_fn) -> Optional[str]:
    """Download a voice/audio file from Telegram and pass to transcribe_fn.

    `msg` is the inner Telegram `message` object (not the full update).
    Returns the transcript string, or None if dependencies missing or transcription empty.
    Cleans up the temp file even on transcribe failure.
    """
    if transcribe_fn is None or telegram_client is None:
        return None
    file_id = (msg.get("voice") or msg.get("audio", {})).get("file_id")
    if not file_id:
        return None
    url = telegram_client.get_file_url(file_id)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f"tg-{file_id}-", suffix=".ogg", delete=False
        ) as tmp:
            with httpx.stream("GET", url, timeout=60) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes():
                    tmp.write(chunk)
            tmp_path = tmp.name
        transcript = transcribe_fn(tmp_path)
        return transcript.strip() if transcript else None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
