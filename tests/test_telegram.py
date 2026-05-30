import httpx
import pytest
import respx

from bridge.telegram import TelegramClient


@respx.mock
def test_get_updates_returns_results():
    respx.get("https://api.telegram.org/botTOKEN/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [{"update_id": 1, "message": {"text": "hi"}}],
        })
    )
    client = TelegramClient(token="TOKEN")
    updates = client.get_updates(offset=0, timeout=25)
    assert len(updates) == 1
    assert updates[0]["update_id"] == 1


@respx.mock
def test_get_updates_passes_offset_and_timeout():
    route = respx.get("https://api.telegram.org/botTOKEN/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )
    client = TelegramClient(token="TOKEN")
    client.get_updates(offset=42, timeout=25)
    request = route.calls.last.request
    assert "offset=42" in str(request.url)
    assert "timeout=25" in str(request.url)


@respx.mock
def test_get_file_url():
    respx.get("https://api.telegram.org/botTOKEN/getFile").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": {"file_path": "voice/file_1.ogg"},
        })
    )
    client = TelegramClient(token="TOKEN")
    url = client.get_file_url("FILEID")
    assert url == "https://api.telegram.org/file/botTOKEN/voice/file_1.ogg"


@respx.mock
def test_send_message_posts_chat_id_and_text():
    route = respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    client = TelegramClient(token="TOKEN")
    client.send_message(chat_id=999, text="hello")
    body = route.calls.last.request.content
    assert b'"chat_id": 999' in body
    assert b'"text": "hello"' in body


@respx.mock
def test_repr_redacts_token():
    client = TelegramClient(token="123:secret_value")
    assert "secret_value" not in repr(client)
    assert "REDACTED" in repr(client)


@respx.mock
def test_http_error_message_redacts_token():
    respx.get("https://api.telegram.org/bot123:secret/getUpdates").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    client = TelegramClient(token="123:secret")
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        client.get_updates(offset=0)
    assert "secret" not in str(exc_info.value)
    assert "REDACTED" in str(exc_info.value)
