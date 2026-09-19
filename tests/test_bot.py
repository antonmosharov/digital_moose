import base64
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import Agent, Answer
from app.config import Settings
from app.main import create_app
from app.prompts import Media, Prompt, UserError, attachments, mention_spans, prompt_text
from app.store import Store
from app.telegram import BotService, Telegram


def message(text="@moose explain", **extra):
    return {
        "message_id": 9,
        "chat": {"id": -100, "type": "supergroup", "title": "Team"},
        "from": {"id": 1, "is_bot": False},
        "text": text,
        "entities": [{"type": "mention", "offset": 0, "length": 6}],
        **extra,
    }


@pytest.mark.parametrize(
    "value,entities,expected",
    [
        ("@moose hi", [{"type": "mention", "offset": 0, "length": 6}], True),
        ("@MOOSE hi", [{"type": "mention", "offset": 0, "length": 6}], True),
        ("@moose_extra", [{"type": "mention", "offset": 0, "length": 12}], False),
        ("@moose hi", [{"type": "code", "offset": 0, "length": 6}], False),
        ("/start@moose", [{"type": "bot_command", "offset": 0, "length": 12}], False),
        ("Moose", [{"type": "text_mention", "offset": 0, "length": 5, "user": {"id": 42}}], True),
    ],
)
def test_exact_mention(value, entities, expected):
    assert bool(mention_spans({"text": value, "entities": entities}, "moose", 42)) == expected


def test_utf16_and_caption():
    msg = {
        "caption": "🫎 @moose explain @moose",
        "caption_entities": [
            {"type": "mention", "offset": 3, "length": 6},
            {"type": "mention", "offset": 18, "length": 6},
        ],
    }
    assert prompt_text(msg, "moose", 42) == "🫎  explain"


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path))
    yield value
    value.db.close()


async def test_reply_prompt_and_access(store):
    telegram = AsyncMock()
    service = BotService(store, AsyncMock())
    service.identity = {"username": "moose", "id": 42}
    reply = {"text": "Flexible", "photo": [{"file_id": "small"}, {"file_id": "large"}]}
    incoming = message(reply_to_message=reply, message_thread_id=8)
    with patch(
        "app.telegram.Agent.run",
        new_callable=AsyncMock,
        return_value=Answer(
            "Explanation", tool_calls=2, tools_used=["read_news", "read_consciousness"]
        ),
    ) as run:
        await service.handle({"message": incoming}, telegram)
        run.assert_not_called()
        telegram.send.assert_not_called()
        store.permit_chat(-100, "Team", True)
        telegram.download.return_value = Media("image.png", "image/png", b"image")
        await service.handle({"message": incoming}, telegram)
        prompt = run.call_args.args[0]
        assert prompt.text == "explain"
        assert prompt.context == "Flexible"
        assert prompt.media[0].data == b"image"
        assert telegram.download.call_args.args[0]["file_id"] == "large"
        telegram.send.assert_awaited_once()
        assert store.activity()[0]["status"] == "success"
        assert store.activity()[0]["reply_messages"] == ["Explanation"]
        assert store.activity()[0]["tools_used"] == ["read_news", "read_consciousness"]


async def test_reply_with_old_mention_does_not_trigger(store):
    service = BotService(store, AsyncMock())
    service.identity = {"username": "moose", "id": 42}
    store.permit_chat(-100, "Team", True)
    incoming = message("hello", entities=[], reply_to_message=message())
    telegram = AsyncMock()
    await service.handle({"message": incoming}, telegram)
    telegram.send.assert_not_called()
    assert not store.activity()


async def test_media_only_tagged_caption_and_sender_cooldown(store):
    service = BotService(store, AsyncMock())
    service.identity = {"username": "moose", "id": 42}
    store.permit_chat(-100, "Team", True)
    incoming = message()
    incoming.pop("text")
    incoming["caption"] = "@moose"
    incoming["caption_entities"] = incoming.pop("entities")
    incoming["document"] = {"file_id": "photo", "mime_type": "image/png"}
    telegram = AsyncMock()
    telegram.download.return_value = Media("photo.png", "image/png", b"image")
    with patch(
        "app.telegram.Agent.run", new_callable=AsyncMock, return_value=Answer("Description")
    ) as run:
        await service.handle({"message": incoming}, telegram)
        assert run.call_args.args[0].text == ""
        assert len(run.call_args.args[0].media) == 1
        await service.handle({"message": incoming}, telegram)
        assert run.await_count == 1
        assert store.activity()[0]["status"] == "limited"


async def test_private_chats_require_opt_in_and_mention(store):
    private = {"id": 99, "type": "private"}
    assert not store.allowed(private, Settings())
    assert store.allowed(private, Settings(allow_private=True))
    service = BotService(store, AsyncMock())
    service.identity = {"username": "moose", "id": 42}
    store.save_settings(Settings(allow_private=True))
    telegram = AsyncMock()
    await service.handle({"message": message("hello", entities=[], chat=private)}, telegram)
    telegram.send.assert_not_called()


async def test_leave_unapproved_invitation(store):
    service = BotService(store, AsyncMock())
    telegram = AsyncMock()
    await service.handle(
        {
            "my_chat_member": {
                "chat": {"id": -100, "title": "Team", "type": "supergroup"},
                "new_chat_member": {"status": "member"},
            }
        },
        telegram,
    )
    telegram.call.assert_awaited_once_with("leaveChat", {"chat_id": -100})


def test_secrets_encrypted_and_redacted(store):
    store.save_settings(Settings(bot_token="bot-secret", api_key="api-secret"))
    raw = store.db.execute("SELECT value FROM settings").fetchone()[0]
    assert b"api-secret" not in raw and b"bot-secret" not in raw
    public = store.public_settings()
    assert public["has_api_key"] and "api_key" not in public
    assert store.settings().api_key == "api-secret"


async def test_agent_image_tool_round_trip():
    requests = []
    encoded = base64.b64encode(b"png-result").decode()
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call1",
                                "type": "function",
                                "function": {
                                    "name": "create_or_edit_image",
                                    "arguments": json.dumps(
                                        {"prompt": "Remove background", "use_input_images": True}
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "images": [{"image_url": {"url": f"data:image/png;base64,{encoded}"}}]
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "Here is your image."}}]},
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        settings = Settings(api_key="key", model="vision-tool-model", image_model="image-model")
        answer = await Agent(settings, client).run(
            Prompt("remove background", media=[Media("in.png", "image/png", b"in")])
        )
    assert answer.text == "Here is your image."
    assert answer.media[0].data == b"png-result"
    assert answer.tool_calls == 1
    assert requests[1]["modalities"] == ["image", "text"]
    assert requests[1]["messages"][0]["content"][1]["image_url"]["url"].endswith("aW4=")
    assert requests[2]["messages"][-1]["role"] == "tool"


async def test_provider_error_does_not_leak_credentials():
    def handler(request):
        return httpx.Response(401, json={"error": "secret-key-and-private-info"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UserError, match="HTTP 401") as error:
            await Agent(Settings(api_key="secret-key", model="model"), client).run(Prompt("hi"))
        assert "secret-key" not in str(error.value)


@pytest.mark.parametrize(
    "inputs,reason", [([], "style-reference"), ([Media("in.png", "image/png", b"in")], "SVG")]
)
async def test_recraft_styles_vector_fails_before_billable_request(inputs, reason):
    client = AsyncMock()
    agent = Agent(Settings(api_key="key", image_model="recraft/recraft-v4-styles-vector"), client)
    with pytest.raises(UserError, match=reason):
        await agent.image("Create an image", inputs)
    client.post.assert_not_called()


async def test_image_failure_is_not_rewritten_by_chat_model():
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["model"] == "chat-model":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call1",
                                        "type": "function",
                                        "function": {
                                            "name": "create_or_edit_image",
                                            "arguments": json.dumps(
                                                {
                                                    "prompt": "Create a purple circle",
                                                    "use_input_images": False,
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(402, json={"error": {"message": "Insufficient credits; secret-key"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        agent = Agent(
            Settings(api_key="secret-key", model="chat-model", image_model="image-model"), client
        )
        with pytest.raises(UserError, match="Image tool failed:.*HTTP 402.*credits") as error:
            await agent.run(Prompt("Create a purple circle"))
        assert "secret-key" not in str(error.value)
    assert len(requests) == 2  # No apology completion or repeat image request.


def test_provider_reference_requirement_is_actionable_and_redacted():
    response = httpx.Response(
        400, json={"error": {"message": "At least one reference image is required. secret prompt"}}
    )
    error = str(Agent.provider_error(response))
    assert "requires a reference image" in error
    assert "secret prompt" not in error


async def test_image_failure_is_logged_as_error(store):
    service = BotService(store, AsyncMock())
    service.identity = {"username": "moose", "id": 42}
    store.permit_chat(-100, "Team", True)
    telegram = AsyncMock()
    with patch(
        "app.telegram.Agent.run",
        new_callable=AsyncMock,
        side_effect=UserError("Image tool failed: provider rejected the input"),
    ):
        await service.handle({"message": message()}, telegram)
    assert store.activity()[0]["status"] == "error"
    assert "provider rejected" in telegram.send.call_args.args[1].text


async def test_media_delivery_sends_inline_photo_in_original_thread():
    telegram = Telegram("token", AsyncMock())
    telegram.call = AsyncMock()
    await telegram.send(
        message(message_thread_id=10), Answer("Done", [Media("result.png", "image/png", b"png")])
    )
    calls = telegram.call.call_args_list
    assert calls[0].args[0] == "sendMessage"
    assert calls[0].args[1]["message_thread_id"] == 10
    assert calls[1].args[0] == "sendPhoto"
    assert calls[1].args[1]["message_thread_id"] == "10"
    assert json.loads(calls[1].args[1]["reply_parameters"])["message_id"] == 9


async def test_download_limit_is_enforced_on_stream():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"123456"))
    ) as client:
        telegram = Telegram("token", client)
        telegram.call = AsyncMock(return_value={"file_path": "photos/file.jpg"})
        with pytest.raises(UserError, match="size limit"):
            await telegram.download(
                {"file_id": "1", "file_name": "a.jpg", "mime_type": "image/jpeg"}, 3
            )


def test_media_encodings():
    assert Media("voice.ogg", "audio/ogg", b"voice").content()["input_audio"]["format"] == "ogg"
    assert Media("doc.pdf", "application/pdf", b"pdf").content()["type"] == "file"
    assert Media("clip.mp4", "video/mp4", b"video").content()["type"] == "video_url"
    assert "hello" in Media("doc.txt", "text/plain", b"hello").content()["text"]
    with pytest.raises(UserError):
        Media("binary.exe", "application/octet-stream", b"x").content()
    assert (
        attachments({"photo": [{"file_id": "small"}, {"file_id": "large"}]})[0]["file_id"]
        == "large"
    )


def test_dashboard_settings_and_csrf(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with TestClient(create_app(str(tmp_path))) as client:
        assert client.get("/").status_code == 200
        assert client.patch("/api/settings", json={"model": "test"}).status_code == 403
        headers = {"X-Moose-Request": "1"}
        result = client.patch(
            "/api/settings", json={"api_key": "secret", "model": "test"}, headers=headers
        )
        assert result.status_code == 200
        assert "secret" not in result.text
        assert (
            client.patch("/api/settings", json={"max_tokens": 0}, headers=headers).status_code
            == 422
        )
        assert (
            client.patch("/api/settings", json={"enabled": True}, headers=headers).status_code
            == 400
        )
        assert (
            client.put(
                "/api/chats", json={"id": -100, "title": "Team"}, headers=headers
            ).status_code
            == 200
        )
        assert client.get("/api/state").json()["chats"][0]["allowed"] == 1
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_dashboard_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    with TestClient(create_app(str(tmp_path))) as client:
        assert client.get("/").status_code == 401
        assert client.get("/api/state", auth=("admin", "wrong")).status_code == 401
        assert client.get("/api/state", auth=("admin", "password")).status_code == 200


def test_playground_media_uses_form_body(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with (
        TestClient(create_app(str(tmp_path))) as client,
        patch("app.main.Agent.run", new_callable=AsyncMock, return_value=Answer("Summary")) as run,
    ):
        response = client.post(
            "/api/playground/media",
            files={"file": ("note.txt", b"A text attachment", "text/plain")},
            data={"prompt": "explain", "context": "original message"},
            headers={"X-Moose-Request": "1"},
        )
        assert response.status_code == 200
        prompt = run.call_args.args[0]
        assert prompt.text == "explain" and prompt.context == "original message"
        assert prompt.media[0].data == b"A text attachment"
