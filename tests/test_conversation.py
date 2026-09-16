import base64
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.agent import Agent, Answer
from app.config import Settings
from app.prompts import Media, Prompt, UserError
from app.store import Store
from app.telegram import BotService, Telegram

NOW = datetime(2026, 9, 16, 8, tzinfo=UTC).timestamp()  # Noon in Dubai.
CHAT = {"id": -100, "type": "supergroup", "title": "Friends"}


def msg(i, *, at=NOW - 301, who=1, text="hello", thread=0, chat=None, **extra):
    return {
        "message_id": i,
        "date": at,
        "chat": chat or CHAT,
        "message_thread_id": thread,
        "from": {"id": who, "first_name": f"Person {who}"},
        "text": text,
        **extra,
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW)
    store = Store(str(tmp_path))
    store.permit_chat(CHAT["id"], CHAT["title"], True)
    yield store
    store.db.close()


def service(store):
    bot = BotService(store, AsyncMock())
    bot.identity = {"id": 42, "username": "moose"}
    return bot


def active(store, **overrides):
    store.save_settings(Settings(enabled=True, model="chat", api_key="key", **overrides))
    for i in range(1, 4):
        store.remember(msg(i, who=i % 2 + 1))


def completion(content=None, calls=None):
    return {"choices": [{"message": {"content": content, "tool_calls": calls or []}}]}


def call(name, **args):
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


async def test_untagged_messages_are_remembered_without_downloading(store):
    bot, telegram = service(store), AsyncMock()
    await bot.handle(
        {
            "message": msg(
                1,
                text="",
                document={
                    "file_id": "private-file-id",
                    "file_name": "chart.png",
                    "mime_type": "image/png",
                },
            )
        },
        telegram,
    )
    telegram.download.assert_not_called()
    telegram.send.assert_not_called()
    page = store.history(-100, 0, 2, until=NOW)
    assert page["messages"][0]["media"] == [{"name": "chart.png", "type": "image/png"}]
    assert page["messages"][0]["reference"] == "telegram:-100:0:1"
    assert "private-file-id" not in json.dumps(page)
    raw = store.db.execute("SELECT value FROM messages").fetchone()[0]
    assert b"private-file-id" not in raw and b"chart.png" not in raw


def test_recent_history_is_ten_messages_in_same_topic_and_one_hour(store):
    for i in range(1, 15):
        store.remember(msg(i, at=NOW - 500, text=f"text {i}"))
    store.remember(msg(15, at=NOW - 3601))
    store.remember(msg(16, thread=7))
    store.remember(msg(17, chat={"id": -200, "type": "group"}))
    store.remember(msg(18, at=NOW + 1))
    prompt = Prompt("explain")
    service(store).add_context(prompt, msg(20), AsyncMock(), NOW)
    assert [m["message_id"] for m in prompt.history] == list(range(5, 15))
    assert all(m["who"] == "Person 1" for m in prompt.history)
    older = prompt.history_reader(None, 2)
    assert [m["message_id"] for m in older["messages"]] == [3, 4]
    assert older["has_more"]
    assert [m["message_id"] for m in prompt.history_reader(None, 20)["messages"]] == [1, 2]
    assert not prompt.history_reader(None, 20)["has_more"]
    # A deliberate cursor can reach older-than-an-hour messages, but never future requests.
    assert 15 in [m["message_id"] for m in prompt.history_reader(9999, 50)["messages"]]
    assert 18 not in [m["message_id"] for m in prompt.history_reader(9999, 50)["messages"]]


async def test_media_references_are_scoped_and_access_revocation_erases_history(store):
    store.remember(
        msg(1, document={"file_id": "file", "file_name": "a.png", "mime_type": "image/png"})
    )
    bot, telegram, prompt = service(store), AsyncMock(), Prompt("look")
    telegram.download.return_value = Media("a.png", "image/png", b"png")
    bot.add_context(prompt, msg(2), telegram, NOW)
    for invalid in ("telegram:-200:0:1", "telegram:-100:9:1", "telegram:-100:0:2", "bad"):
        with pytest.raises(UserError, match="this conversation"):
            await prompt.media_reader(invalid)
    result = await prompt.media_reader("telegram:-100:0:1")
    assert result.data == b"png"
    telegram.download.assert_awaited_once()
    store.permit_chat(-100, "Friends", False)
    assert not store.conversations()
    assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    with pytest.raises(UserError, match="revoked"):
        prompt.history_reader(None, 20)
    with pytest.raises(UserError, match="revoked"):
        await prompt.media_reader("telegram:-100:0:1")


def test_retention_and_duplicate_updates(store):
    store.remember(msg(1, at=NOW - 100 * 86400))
    store.remember(msg(2))
    store.remember(msg(2, text="duplicate", at=NOW))
    store.prune_history(Settings(), NOW)
    assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert store.history(-100, 0, 3, until=NOW)["messages"][0]["text"] == "hello"


async def test_history_and_media_tools_feed_actual_image_into_edit_request():
    requests = []
    responses = [
        completion(calls=[call("get_previous_messages", limit=20)]),
        completion(calls=[call("get_message_media", reference="telegram:-100:0:1")]),
        completion(
            calls=[call("create_or_edit_image", prompt="Turn it purple", use_input_images=True)]
        ),
        {
            "choices": [
                {"message": {"images": [{"image_url": {"url": "data:image/png;base64,b3V0"}}]}}
            ]
        },
        completion("Done"),
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    read = Mock(return_value={"messages": [{"reference": "telegram:-100:0:1"}], "has_more": False})
    fetch = AsyncMock(return_value=Media("old.png", "image/png", b"original"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(
            Settings(api_key="key", model="chat", image_model="image"), client
        ).run(Prompt("Edit the earlier image", history_reader=read, media_reader=fetch))
    assert answer.tool_calls == 3 and answer.media[0].data == b"out"
    read.assert_called_once_with(None, 20)
    fetch.assert_awaited_once_with("telegram:-100:0:1")
    expected = "data:image/png;base64," + base64.b64encode(b"original").decode()
    assert requests[2]["messages"][-1]["content"][-1]["image_url"]["url"] == expected
    assert requests[3]["messages"][0]["content"][-1]["image_url"]["url"] == expected


async def test_tool_budgets_force_a_final_completion_and_block_repeated_fetches():
    requests = []
    responses = [
        completion(calls=[call("get_previous_messages")]),
        completion(calls=[call("get_previous_messages")]),
        completion("Finished"),
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    read = Mock(return_value={"messages": [], "has_more": False})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(
            Settings(api_key="key", model="chat", history_tool_calls=2), client
        ).run(Prompt("Recall", history_reader=read))
    assert read.call_count == answer.tool_calls == 2
    assert requests[-1]["tool_choice"] == "none"


async def test_provider_cannot_exceed_tool_budget_even_in_one_batch():
    responses = [
        completion(calls=[call("get_previous_messages") for _ in range(4)]),
        completion("OK"),
    ]
    read = Mock(return_value={"messages": []})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=responses.pop(0)))
    ) as client:
        answer = await Agent(
            Settings(api_key="key", model="chat", history_tool_calls=1), client
        ).run(Prompt("Recall", history_reader=read))
    assert read.call_count == answer.tool_calls == 1


@pytest.mark.parametrize(
    "chance,expected", [(0, ["One\n\nTwo\n\nThree"]), (1, ["One", "Two\n\nThree"])]
)
async def test_random_burst_is_optional_and_capped(chance, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json=completion("One\n[[NEXT_MESSAGE]]\nTwo\n[[NEXT_MESSAGE]]\nThree")
            )
        )
    ) as client:
        answer = await Agent(
            Settings(
                api_key="key", model="chat", multi_message_probability=chance, max_reply_messages=2
            ),
            client,
        ).run(Prompt("hi"))
    assert answer.messages == expected
    assert "[[NEXT_MESSAGE]]" not in answer.text


async def test_proactive_can_decline_and_has_no_image_tool():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion("[[SILENT]]"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(
            Settings(api_key="key", model="chat", image_model="image"), client
        ).run(Prompt("Consider joining", proactive=True))
    assert answer.silent
    assert "tools" not in requests[0]
    assert requests[0]["max_tokens"] == 500


async def test_participation_rolls_only_once_even_after_restart(store):
    active(store)
    telegram = AsyncMock()
    with (
        patch("app.telegram.random.random", return_value=0.9) as roll,
        patch("app.telegram.Agent.run") as run,
    ):
        await service(store).proactive(telegram, now=NOW)
        await service(store).proactive(telegram, now=NOW + 60)
        roll.assert_called_once()
        run.assert_not_called()
    store.remember(msg(4, at=NOW, who=2))
    with (
        patch("app.telegram.random.random", return_value=0),
        patch(
            "app.telegram.Agent.run", new_callable=AsyncMock, return_value=Answer("Good point")
        ) as run,
    ):
        await service(store).proactive(telegram, now=NOW + 301)
        await service(store).proactive(telegram, now=NOW + 360)
        run.assert_awaited_once()
        telegram.send.assert_awaited_once()
        assert telegram.send.call_args.args[0]["message_id"] == 4


@pytest.mark.parametrize(
    "reason",
    [
        "night",
        "fresh",
        "stale",
        "single_person",
        "bot_last",
        "disabled",
        "private",
        "channel",
        "revoked",
    ],
)
async def test_participation_eligibility(store, reason):
    active(store, participation_probability=1)
    now = NOW
    if reason == "night":
        now = datetime(2026, 9, 16, 0, tzinfo=UTC).timestamp()
    elif reason == "fresh":
        store.remember(msg(4, at=NOW - 60))
    elif reason == "stale":
        now += 3601
    elif reason == "single_person":
        store.db.execute("UPDATE messages SET sender_id=1")
    elif reason == "bot_last":
        store.remember(msg(4), is_bot=True)
    elif reason == "disabled":
        store.save_settings(Settings(enabled=False))
    elif reason in {"private", "channel"}:
        store.db.execute("UPDATE chats SET kind=?", (reason,))
    elif reason == "revoked":
        store.permit_chat(-100, "Friends", False)
    with patch("app.telegram.Agent.run") as run:
        await service(store).proactive(AsyncMock(), now=now)
        run.assert_not_called()


async def test_shared_cooldown_daily_limit_and_silent_attempts(store):
    active(store, participation_probability=1, proactive_cooldown_hours=1, proactive_daily_limit=1)
    telegram = AsyncMock()
    with patch(
        "app.telegram.Agent.run", new_callable=AsyncMock, return_value=Answer(silent=True)
    ) as run:
        await service(store).proactive(telegram, now=NOW)
        for i in range(4, 7):
            store.remember(msg(i, at=NOW + 7200 - 301, who=i % 2 + 1, thread=9))
        await service(store).proactive(telegram, now=NOW + 7200)
        run.assert_awaited_once()
        telegram.send.assert_not_called()


async def test_wake_only_once_until_a_human_speaks_even_with_bot_last(store):
    store.save_settings(Settings(enabled=True))
    store.remember(msg(1, at=NOW - 3 * 86400))
    store.remember(msg(2, at=NOW - 3 * 86400), is_bot=True)
    telegram = AsyncMock()
    with patch(
        "app.telegram.Agent.run", new_callable=AsyncMock, return_value=Answer("Hello again")
    ) as run:
        await service(store).proactive(telegram, now=NOW)
        await service(store).proactive(telegram, now=NOW + 3 * 86400)
        run.assert_awaited_once()
        assert "message_id" not in telegram.send.call_args.args[0]
        assert not run.call_args.args[0].history  # Older context is opt-in through the tool.
        assert run.call_args.args[0].history_reader is not None
        store.remember(msg(3, at=NOW + 3 * 86400))
        await service(store).proactive(telegram, now=NOW + 6 * 86400)
        assert run.await_count == 2


async def test_wake_survives_content_retention_expiry(store):
    store.save_settings(Settings(enabled=True, history_retention_days=3, wake_after_hours=96))
    store.remember(msg(1, at=NOW - 5 * 86400))
    telegram = AsyncMock()
    with patch("app.telegram.Agent.run", new_callable=AsyncMock, return_value=Answer("Hello")):
        await service(store).proactive(telegram, now=NOW)
    telegram.send.assert_awaited_once()
    assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


async def test_new_activity_during_generation_cancels_proactive_delivery(store):
    active(store, participation_probability=1)
    telegram = AsyncMock()

    async def refresh():
        store.remember(msg(4, at=NOW))

    with patch(
        "app.telegram.Agent.run", new_callable=AsyncMock, return_value=Answer("Stale reply")
    ):
        await service(store).proactive(telegram, now=NOW, before_delivery=refresh)
    telegram.send.assert_not_called()


async def test_proactive_failures_do_not_spam_chat_or_retry_same_opportunity(store):
    active(store, participation_probability=1)
    telegram = AsyncMock()
    with patch(
        "app.telegram.Agent.run",
        new_callable=AsyncMock,
        side_effect=UserError("provider unavailable"),
    ) as run:
        await service(store).proactive(telegram, now=NOW)
        await service(store).proactive(telegram, now=NOW + 3 * 3600)
        run.assert_awaited_once()
    telegram.send.assert_not_called()
    assert store.activity()[0]["status"] == "error"


def test_timezone_and_overnight_windows_are_validated():
    assert BotService.daytime(Settings(), NOW)
    assert not BotService.daytime(Settings(proactive_timezone="Pacific/Honolulu"), NOW)
    assert BotService.daytime(Settings(daytime_start=21, daytime_end=13), NOW)
    for changes in (
        {"proactive_timezone": "not/a-zone"},
        {"daytime_start": 9, "daytime_end": 9},
        {"participation_probability": 1.1},
        {"history_tool_calls": 21},
    ):
        with pytest.raises(ValidationError):
            Settings(**changes)


async def test_photo_rejection_falls_back_to_file_and_records_delivered_messages(store):
    methods = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "sendPhoto":
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: PHOTO_INVALID_DIMENSIONS",
                },
            )
        sent = msg(len(methods) + 10, text="Hi" if method == "sendMessage" else "")
        if method == "sendDocument":
            sent["document"] = {
                "file_id": "generated",
                "file_name": "meme.png",
                "mime_type": "image/png",
            }
        return httpx.Response(200, json={"ok": True, "result": sent})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        telegram = Telegram("token", client, on_sent=lambda m: store.remember(m, is_bot=True))
        await telegram.send(msg(9), Answer("Hi", [Media("meme.png", "image/png", b"png")]))
    assert methods == ["sendMessage", "sendPhoto", "sendDocument"]
    assert store.db.execute("SELECT COUNT(*) FROM messages WHERE is_bot=1").fetchone()[0] == 2
    assert store.media_attachment(-100, 0, 13)["file_id"] == "generated"


async def test_document_delivery_option_and_burst_order():
    telegram = Telegram("token", AsyncMock(), image_delivery="document")
    telegram.call = AsyncMock()
    with patch("app.telegram.asyncio.sleep", new_callable=AsyncMock):
        await telegram.send(
            msg(9),
            Answer("One\n\nTwo", [Media("m.png", "image/png", b"png")], messages=["One", "Two"]),
        )
    assert [c.args[0] for c in telegram.call.call_args_list] == [
        "sendMessage",
        "sendMessage",
        "sendDocument",
    ]
    assert [c.args[1]["text"] for c in telegram.call.call_args_list[:2]] == ["One", "Two"]


async def test_repeated_media_fetch_is_cached_and_failed_fetch_can_be_explained():
    requests = []
    responses = [
        completion(calls=[call("get_message_media", reference="telegram:-100:0:1")]),
        completion(calls=[call("get_message_media", reference="telegram:-100:0:1")]),
        completion(calls=[call("get_message_media", reference="telegram:-100:0:2")]),
        completion("The first picture is available; the second attachment has expired."),
    ]
    fetch = AsyncMock(
        side_effect=[Media("one.png", "image/png", b"one"), UserError("Media expired")]
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(Settings(api_key="key", model="chat", media_tool_calls=3), client).run(
            Prompt("Compare", media_reader=fetch)
        )
    assert fetch.await_count == 2
    assert answer.tool_calls == 3
    assert requests[-1]["tool_choice"] == "none"
    assert requests[-1]["messages"][-1]["content"] == "Media expired"
    assert len([m for m in requests[-1]["messages"] if m["role"] == "user"]) == 2


async def test_anonymous_admin_messages_are_context_not_other_bot_messages(store):
    telegram, bot = AsyncMock(), service(store)
    await bot.handle(
        {
            "message": msg(
                1,
                **{
                    "from": {"id": 100, "is_bot": True},
                    "sender_chat": CHAT,
                },
            )
        },
        telegram,
    )
    await bot.handle({"message": msg(2, **{"from": {"id": 42, "is_bot": True}})}, telegram)
    page = store.history(-100, 0, 3, until=NOW)["messages"]
    assert len(page) == 1 and page[0]["who"] == "Friends" and not page[0]["is_bot"]
    telegram.send.assert_not_called()


async def test_revocation_during_triggered_generation_prevents_delivery(store):
    bot, telegram = service(store), AsyncMock()

    async def revoke(prompt):
        store.permit_chat(-100, "Friends", False)
        return Answer("Should not send")

    with patch("app.telegram.Agent.run", side_effect=revoke):
        await bot.handle(
            {
                "message": msg(
                    1,
                    text="@moose hello",
                    entities=[
                        {
                            "type": "mention",
                            "offset": 0,
                            "length": 6,
                        }
                    ],
                )
            },
            telegram,
        )
    telegram.send.assert_not_called()
    assert not store.conversations()


async def test_poll_consumes_context_and_remembers_own_delivery(store):
    import asyncio

    store.save_settings(Settings(enabled=True, bot_token="fake-token", api_key="key", model="chat"))
    poll_calls = 0
    tagged = msg(
        2,
        at=NOW,
        text="@moose what about that?",
        entities=[
            {
                "type": "mention",
                "offset": 0,
                "length": 6,
            }
        ],
    )

    async def handler(request):
        nonlocal poll_calls
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            result = {"username": "moose", "id": 42}
        elif method == "getWebhookInfo":
            result = {"url": ""}
        elif method == "getUpdates":
            poll_calls += 1
            if poll_calls > 1:
                raise asyncio.CancelledError
            result = [
                {"update_id": 1, "message": msg(1, text="We are planning a trip.")},
                {"update_id": 2, "message": tagged},
            ]
        elif method == "sendMessage":
            result = msg(3, at=NOW, who=42, text="Sounds good")
        else:
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = BotService(store, client)
        with patch(
            "app.telegram.Agent.run", new_callable=AsyncMock, return_value=Answer("Sounds good")
        ) as run:
            with pytest.raises(asyncio.CancelledError):
                await bot.poll()
            assert run.call_args.args[0].history[0]["text"] == "We are planning a trip."
            assert len(run.call_args.args[0].history) == 1
    history = store.history(-100, 0, 4, until=NOW)["messages"]
    assert history[-1]["is_bot"] and history[-1]["text"] == "Sounds good"
