import httpx
import pytest
import respx
from unittest.mock import MagicMock

from bridge.parse import extract_text, is_authorized
from bridge.telegram import TelegramClient


def test_extract_text_from_text_message():
    update = {"update_id": 1, "message": {"from": {"id": 1}, "text": "hello"}}
    assert extract_text(update, telegram_client=None, transcribe_fn=None) == "hello"


def test_extract_text_strips_whitespace():
    update = {"update_id": 1, "message": {"from": {"id": 1}, "text": "  hi  \n"}}
    assert extract_text(update, telegram_client=None, transcribe_fn=None) == "hi"


def test_extract_text_returns_none_for_unsupported():
    update = {"update_id": 1, "message": {"from": {"id": 1}, "sticker": {}}}
    assert extract_text(update, telegram_client=None, transcribe_fn=None) is None


def test_is_authorized_accepts_paired_user():
    update = {"update_id": 1, "message": {"from": {"id": 42}, "text": "x"}}
    assert is_authorized(update, paired_user_id=42) is True


def test_is_authorized_rejects_other():
    update = {"update_id": 1, "message": {"from": {"id": 99}, "text": "x"}}
    assert is_authorized(update, paired_user_id=42) is False


def test_is_authorized_handles_no_message():
    update = {"update_id": 1}  # edited_message, callback_query, etc.
    assert is_authorized(update, paired_user_id=42) is False


@respx.mock
def test_extract_text_voice_calls_transcribe(tmp_path):
    # getFile + file download mocks
    respx.get("https://api.telegram.org/botTOKEN/getFile").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "result": {"file_path": "voice/file_1.ogg"},
        })
    )
    respx.get("https://api.telegram.org/file/botTOKEN/voice/file_1.ogg").mock(
        return_value=httpx.Response(200, content=b"OGG_BYTES")
    )

    client = TelegramClient(token="TOKEN")
    transcribe = MagicMock(return_value="transcribed words")

    update = {
        "update_id": 1,
        "message": {"from": {"id": 1}, "voice": {"file_id": "FILEID"}},
    }
    result = extract_text(update, telegram_client=client, transcribe_fn=transcribe)

    assert result == "transcribed words"
    assert transcribe.call_count == 1
    called_with = transcribe.call_args[0][0]
    assert called_with.endswith(".ogg")


import os


@respx.mock
def test_extract_text_voice_deletes_temp_file(tmp_path):
    respx.get("https://api.telegram.org/botTOKEN/getFile").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "result": {"file_path": "voice/file_1.ogg"},
        })
    )
    respx.get("https://api.telegram.org/file/botTOKEN/voice/file_1.ogg").mock(
        return_value=httpx.Response(200, content=b"OGG_BYTES")
    )

    client = TelegramClient(token="TOKEN")
    captured_paths = []

    def fake_transcribe(path):
        captured_paths.append(path)
        # Verify file exists during transcription
        assert os.path.exists(path)
        return "transcribed"

    update = {
        "update_id": 1,
        "message": {"from": {"id": 1}, "voice": {"file_id": "FILEID"}},
    }
    extract_text(update, telegram_client=client, transcribe_fn=fake_transcribe)

    # After extract_text returns, the temp file should be deleted
    assert len(captured_paths) == 1
    assert not os.path.exists(captured_paths[0])


from bridge.parse import detect_message_type


def test_detect_message_type_known_types():
    # photo and document are both handled now (Tasks 2 + 3)
    assert detect_message_type({"photo": []}) is None
    assert detect_message_type({"document": {}}) is None
    assert detect_message_type({"sticker": {}}) == "sticker"
    assert detect_message_type({"video": {}}) == "video"
    assert detect_message_type({"animation": {}}) == "GIF"
    assert detect_message_type({"location": {}}) == "location"
    assert detect_message_type({"poll": {}}) == "poll"


def test_detect_message_type_unknown_returns_none():
    assert detect_message_type({"text": "hi"}) is None
    assert detect_message_type({"voice": {}}) is None  # voice is handled, not "unsupported"
    assert detect_message_type({}) is None


from pathlib import Path


@respx.mock
def test_extract_text_image_returns_path(tmp_path, monkeypatch):
    import bridge.parse as parse_mod
    monkeypatch.setattr(parse_mod, "MEDIA_DIR", tmp_path)

    respx.get("https://api.telegram.org/botTOKEN/getFile").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "result": {"file_path": "photos/file_3.jpg"},
        })
    )
    respx.get("https://api.telegram.org/file/botTOKEN/photos/file_3.jpg").mock(
        return_value=httpx.Response(200, content=b"JPEG_BYTES")
    )

    client = TelegramClient(token="TOKEN")
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 1},
            "photo": [
                {"file_id": "small", "width": 90, "height": 90},
                {"file_id": "large", "width": 1280, "height": 1280},
            ],
        },
    }
    result = extract_text(update, telegram_client=client, transcribe_fn=None)
    assert result is not None
    assert "[image:" in result
    assert "large.jpg" in result
    assert (tmp_path / "large.jpg").exists()
    assert (tmp_path / "large.jpg").read_bytes() == b"JPEG_BYTES"


@respx.mock
def test_extract_text_image_includes_caption(tmp_path, monkeypatch):
    import bridge.parse as parse_mod
    monkeypatch.setattr(parse_mod, "MEDIA_DIR", tmp_path)

    respx.get("https://api.telegram.org/botTOKEN/getFile").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "result": {"file_path": "photos/cap.jpg"},
        })
    )
    respx.get("https://api.telegram.org/file/botTOKEN/photos/cap.jpg").mock(
        return_value=httpx.Response(200, content=b"JPEG")
    )

    client = TelegramClient(token="TOKEN")
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 1},
            "photo": [{"file_id": "xyz"}],
            "caption": "  look at this thing  ",
        },
    }
    result = extract_text(update, telegram_client=client, transcribe_fn=None)
    assert "[image:" in result
    assert result.endswith("look at this thing")


@respx.mock
def test_extract_text_image_dedupe(tmp_path, monkeypatch):
    import bridge.parse as parse_mod
    monkeypatch.setattr(parse_mod, "MEDIA_DIR", tmp_path)

    (tmp_path / "dup.jpg").write_bytes(b"CACHED")

    respx.get("https://api.telegram.org/botTOKEN/getFile").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "result": {"file_path": "photos/dup.jpg"},
        })
    )

    client = TelegramClient(token="TOKEN")
    update = {
        "update_id": 1,
        "message": {"from": {"id": 1}, "photo": [{"file_id": "dup"}]},
    }
    result = extract_text(update, telegram_client=client, transcribe_fn=None)
    assert "[image:" in result
    assert (tmp_path / "dup.jpg").read_bytes() == b"CACHED"


@respx.mock
def test_extract_text_document_with_filename_and_mime(tmp_path, monkeypatch):
    import bridge.parse as parse_mod
    monkeypatch.setattr(parse_mod, "MEDIA_DIR", tmp_path)

    respx.get("https://api.telegram.org/botTOKEN/getFile").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "result": {"file_path": "documents/file_42.pdf"},
        })
    )
    respx.get("https://api.telegram.org/file/botTOKEN/documents/file_42.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-")
    )

    client = TelegramClient(token="TOKEN")
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 1},
            "document": {
                "file_id": "doc-id",
                "file_name": "syllabus.pdf",
                "mime_type": "application/pdf",
            },
        },
    }
    result = extract_text(update, telegram_client=client, transcribe_fn=None)
    assert "syllabus.pdf" in result
    assert "application/pdf" in result
    assert (tmp_path / "syllabus.pdf").exists()


@respx.mock
def test_extract_text_document_falls_back_to_file_id_when_no_name(tmp_path, monkeypatch):
    import bridge.parse as parse_mod
    monkeypatch.setattr(parse_mod, "MEDIA_DIR", tmp_path)

    respx.get("https://api.telegram.org/botTOKEN/getFile").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "result": {"file_path": "documents/anon"},
        })
    )
    respx.get("https://api.telegram.org/file/botTOKEN/documents/anon").mock(
        return_value=httpx.Response(200, content=b"data")
    )

    client = TelegramClient(token="TOKEN")
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 1},
            "document": {"file_id": "anon-id"},
        },
    }
    result = extract_text(update, telegram_client=client, transcribe_fn=None)
    assert "anon-id.bin" in result
    assert (tmp_path / "anon-id.bin").exists()


@respx.mock
def test_extract_text_document_sanitizes_path_traversal_in_filename(tmp_path, monkeypatch):
    import bridge.parse as parse_mod
    monkeypatch.setattr(parse_mod, "MEDIA_DIR", tmp_path)

    respx.get("https://api.telegram.org/botTOKEN/getFile").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "result": {"file_path": "documents/evil"},
        })
    )
    respx.get("https://api.telegram.org/file/botTOKEN/documents/evil").mock(
        return_value=httpx.Response(200, content=b"data")
    )

    client = TelegramClient(token="TOKEN")
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 1},
            "document": {
                "file_id": "evil-id",
                "file_name": "../../etc/passwd",
            },
        },
    }
    result = extract_text(update, telegram_client=client, transcribe_fn=None)
    # File lands at tmp_path / "passwd", NOT outside tmp_path
    assert (tmp_path / "passwd").exists()
    # Nothing escaped above tmp_path
    assert not (tmp_path.parent / "etc" / "passwd").exists()
    # Result mentions the sanitized name
    assert "passwd" in result


@respx.mock
def test_extract_text_document_rejects_dot_names(tmp_path, monkeypatch):
    """file_name like '..', '.', '/' should fall back to file_id.bin."""
    import bridge.parse as parse_mod
    monkeypatch.setattr(parse_mod, "MEDIA_DIR", tmp_path)

    respx.get("https://api.telegram.org/botTOKEN/getFile").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "result": {"file_path": "documents/edge"},
        })
    )
    respx.get("https://api.telegram.org/file/botTOKEN/documents/edge").mock(
        return_value=httpx.Response(200, content=b"data")
    )

    client = TelegramClient(token="TOKEN")
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 1},
            "document": {"file_id": "edge-id", "file_name": ".."},
        },
    }
    result = extract_text(update, telegram_client=client, transcribe_fn=None)
    # Falls back to file_id.bin instead of using ".." literally
    assert (tmp_path / "edge-id.bin").exists()
    assert "edge-id.bin" in result
    # The dangerous resolved path is NOT used
    assert not (tmp_path / "..").is_file()
