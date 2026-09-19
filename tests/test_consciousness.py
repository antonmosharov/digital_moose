import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import Agent, Answer
from app.config import Settings
from app.main import create_app
from app.prompts import Prompt, contains_agent_name
from app.store import Store
from tests.test_conversation import NOW, active, call, completion, msg, service


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW)
    value = Store(str(tmp_path))
    value.permit_chat(-100, "Friends", True)
    yield value
    value.db.close()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Hey MOOSE!", True),
        ("ЛОСЬ, привет", True),
        ("лосёнок?", True),
        ("лосенок", True),
        ("лосьон", False),
        ("moosewood", False),
        ("", False),
    ],
)
def test_name_matching(text, expected):
    assert contains_agent_name(text, " moose, , лось, лосёнок, ") is expected
    assert not contains_agent_name(text, " , ")


@pytest.mark.parametrize("probability,roll,expected", [(0.5, 0.4, 1), (0.5, 0.6, 0), (0, 0, 0)])
@pytest.mark.parametrize(
    "message",
    [
        msg(4, text="Лосик, что думаешь?"),
        msg(4, text="", caption="Hey Moose!"),
        msg(4, text="x" * 4050 + " Moose?"),
    ],
)
async def test_named_pause_probability_and_no_reroll(store, probability, roll, expected, message):
    active(store, participation_probability=0, name_mention_probability=probability)
    store.forget_chat(-100)
    store.remember(message)
    telegram = AsyncMock()
    from app.agent import Answer

    with (
        patch("app.telegram.random.random", return_value=roll) as random,
        patch(
            "app.telegram.Agent.run", new_callable=AsyncMock, return_value=Answer("Привет")
        ) as run,
    ):
        await service(store).proactive(telegram, now=NOW)
        await service(store).proactive(telegram, now=NOW + 60)
    assert run.await_count == expected
    assert telegram.send.await_count == expected
    random.assert_called_once()


async def test_consciousness_tools_persist_and_update_system_context(store):
    store.save_settings(Settings(api_key="key", model="chat", consciousness="Original"))
    requests = []
    replies = iter(
        [
            completion(calls=[call("read_consciousness")]),
            completion(
                calls=[call("write_consciousness", previous="Original", content="Likes tea")]
            ),
            completion("Remembered"),
        ]
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(store.settings(), client, store).run(Prompt("Remember tea"))
    assert answer.tool_calls == 2
    assert answer.tools_used == ["read_consciousness", "write_consciousness"]
    assert "Original" in requests[0]["messages"][0]["content"]
    assert "Likes tea" in requests[-1]["messages"][0]["content"]
    assert store.read_consciousness() == "Likes tea"
    assert b"Likes tea" not in store.db.execute("SELECT value FROM settings").fetchone()[0]
    reopened = Store(str(store.db.execute("PRAGMA database_list").fetchone()[2]).rsplit("/", 1)[0])
    assert reopened.read_consciousness() == "Likes tea"
    reopened.db.close()


@pytest.mark.parametrize(
    "position,expected", [(1, True), (4, True), (7, True), (10, True), (11, False)]
)
async def test_names_in_last_ten_messages(store, position, expected):
    active(store, participation_probability=0, name_mention_probability=1)
    store.forget_chat(-100)
    for i in range(1, 12):
        store.remember(msg(i, text="Moose" if i == 12 - position else "hello", who=i % 2 + 1))
    with (
        patch("app.telegram.random.random", return_value=0.5),
        patch("app.telegram.Agent.run", new_callable=AsyncMock, return_value=Answer("Hi")) as run,
    ):
        await service(store).proactive(AsyncMock(), now=NOW)
    assert bool(run.await_count) is expected


@pytest.mark.parametrize("excluded", ["old", "future", "topic", "chat", "bot"])
async def test_name_window_exclusions_use_standard_probability(store, excluded):
    active(store, participation_probability=0.2, name_mention_probability=1)
    extra = {"text": "Moose"}
    if excluded == "old":
        extra["at"] = NOW - 3601
    elif excluded == "future":
        extra["at"] = NOW + 1
    elif excluded == "topic":
        extra["thread"] = 8
        extra["at"] = NOW  # Keep that topic's own pause ineligible.
    elif excluded == "chat":
        extra["chat"] = {"id": -200, "type": "group"}
    else:
        extra["from"] = {"id": 42, "is_bot": True}
    store.remember(msg(4, **extra))
    store.remember(msg(5))
    with (
        patch("app.telegram.random.random", return_value=0.5),
        patch("app.telegram.Agent.run", new_callable=AsyncMock) as run,
    ):
        await service(store).proactive(AsyncMock(), now=NOW)
    run.assert_not_called()


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("length,expected", [(99, False), (100, True), (101, True)])
async def test_context_minimum_before_roll_or_attempt(store, named, length, expected):
    active(
        store,
        natural_reply_min_context=100,
        participation_probability=1,
        name_mention_probability=1,
    )
    store.forget_chat(-100)
    first = "Moose" if named else "hello"
    store.remember(msg(1, text=first))
    store.remember(msg(2, text="yes", who=2))
    store.remember(msg(3, text="  " + "я" * (length - len(first) - 3) + "  "))
    with (
        patch("app.telegram.random.random", return_value=0.5) as roll,
        patch("app.telegram.Agent.run", new_callable=AsyncMock, return_value=Answer("Hi")) as run,
    ):
        await service(store).proactive(AsyncMock(), now=NOW)
    assert bool(run.await_count) is expected
    assert bool(roll.call_count) is expected
    assert (
        bool(store.db.execute("SELECT COUNT(*) FROM proactive_attempts").fetchone()[0]) is expected
    )


async def test_short_context_excludes_older_messages_and_metadata(store):
    active(store, natural_reply_min_context=100, participation_probability=1)
    store.forget_chat(-100)
    store.remember(msg(1, text="x" * 200))  # Outside the latest ten.
    store.remember(msg(2, text="x" * 200, at=NOW - 3601))
    for i in range(3, 13):
        store.remember(msg(i, text="yes", who=i % 2 + 1))
    with patch("app.telegram.Agent.run", new_callable=AsyncMock) as run:
        await service(store).proactive(AsyncMock(), now=NOW)
    run.assert_not_called()


async def test_invalid_and_conflicting_tool_writes_preserve_memory(store):
    store.save_settings(Settings(api_key="key", model="chat", consciousness="Newer memory"))
    replies = iter(
        [
            completion(
                calls=[call("write_consciousness", previous="Old memory", content="Overwrite")]
            ),
            completion(calls=[call("write_consciousness", previous="Newer memory", content=123)]),
            completion("Done"),
        ]
    )
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await Agent(store.settings(), client, store).run(Prompt("Remember"))
    assert store.read_consciousness() == "Newer memory"
    results = [m["content"] for m in requests[-1]["messages"] if m["role"] == "tool"]
    assert results[0].startswith("Conflict:")
    assert results[1] == "Invalid tool arguments."


def test_dashboard_memory_conflict_and_unrelated_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with TestClient(create_app(str(tmp_path))) as client:
        headers = {"X-Moose-Request": "1"}
        assert (
            client.patch(
                "/api/settings",
                headers=headers,
                json={
                    "consciousness": "Admin memory",
                    "consciousness_previous": "",
                },
            ).status_code
            == 200
        )
        assert client.app.state.store.write_consciousness("Agent memory", "Admin memory")
        assert (
            client.patch(
                "/api/settings",
                headers=headers,
                json={
                    "consciousness": "Stale edit",
                    "consciousness_previous": "Admin memory",
                },
            ).status_code
            == 409
        )
        response = client.patch("/api/settings", headers=headers, json={"temperature": 0.9})
        assert response.json()["consciousness"] == "Agent memory"
        assert (
            client.patch(
                "/api/settings",
                headers=headers,
                json={
                    "consciousness": "",
                    "consciousness_previous": "Agent memory",
                },
            ).status_code
            == 200
        )
        assert client.app.state.store.read_consciousness() == ""
