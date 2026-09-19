import asyncio
import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import Agent
from app.config import Settings
from app.main import create_app
from app.news import NEWS_TOOL, read_news, usage
from app.prompts import Prompt
from app.store import Store

NOW = datetime(2026, 9, 19, 10, tzinfo=UTC).timestamp()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("app.news.time.time", lambda: NOW)
    value = Store(str(tmp_path))
    value.save_settings(
        Settings(news_enabled=True, news_api_key="private-news-key", model="chat", api_key="ai-key")
    )
    yield value
    value.db.close()


def article():
    return {
        "data": [
            {
                "title": "Tokyo science fair",
                "url": "https://example.com/story",
                "description": "Science event",
                "published_at": "2026-09-19T09:00:00Z",
                "source": "example.com",
                "snippet": "A short excerpt",
            }
        ]
    }


async def test_request_response_and_token_log_redaction(store, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=article())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await read_news(store, client, {"country": "jp", "category": "science"})
    params = requests[0].url.params
    assert params["api_token"] == "private-news-key"
    assert "Japan" in params["search"]
    assert params["published_after"] == "2026-09-16T10:00:00"
    assert params["sort"] == "published_at" and params["limit"] == "5"
    assert result["articles"][0]["url"] == "https://example.com/story"
    assert result["status"] == "ok"
    assert "private-news-key" not in caplog.text
    assert "private-news-key" not in json.dumps(result)
    assert usage(store)["requests"] == 1


@pytest.mark.parametrize("parameter", ["topic", "query"])
async def test_chosen_topic_search_returns_five_excerpts(store, parameter):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{**article()["data"][0], "title": f"Robotics story {i}"} for i in range(8)]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await read_news(store, client, {"country": "jp", parameter: "robotics"})
    params = requests[0].url.params
    assert ' + ("robotics")' in params["search"]
    assert "categories" not in params  # Do not randomly narrow a chosen topic.
    assert params["limit"] == "5"
    assert len(result["articles"]) == 5
    assert result["topic"] == "robotics"
    assert len(requests) == 1  # No article-body requests.
    assert "topic" in NEWS_TOOL["function"]["parameters"]["properties"]
    assert "query" not in NEWS_TOOL["function"]["parameters"]["properties"]


@pytest.mark.parametrize(
    "args",
    [
        {"topic": ["robotics"]},
        {"topic": "x" * 121},
        {"topic": "robotics", "query": "travel"},
    ],
)
async def test_invalid_topics_do_not_consume_quota(store, args):
    def handler(request):
        pytest.fail("Invalid topics must not make a request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await read_news(store, client, args)
    assert result["reason"] == "invalid_arguments"
    assert usage(store)["requests"] == 0


@pytest.mark.parametrize(
    "status,reason",
    [
        (402, "provider_quota"),
        (429, "provider_rate_limit"),
        (401, "provider_error"),
        (500, "provider_error"),
    ],
)
async def test_provider_errors_cool_down_without_echoing_response(store, status, reason):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-news-key provider details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await read_news(store, client, {})
        second = await read_news(store, client, {})
    assert first["reason"] == reason
    assert second["reason"] == "provider_cooldown"
    assert len(calls) == 1
    assert "private-news-key" not in json.dumps(first)


@pytest.mark.parametrize("kind", ["timeout", "bad_json", "bad_shape", "error_body"])
async def test_unavailable_responses_are_nonfatal(store, kind):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("private-news-key", request=request)
        if kind == "bad_json":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json=[] if kind == "bad_shape" else {"error": {"code": "oops"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await read_news(store, client, {})
    assert result["status"] == "unavailable"
    assert result["articles"] == []
    assert "private-news-key" not in json.dumps(result)


async def test_daily_limit_concurrency_restart_and_reset(store, tmp_path, monkeypatch):
    store.save_settings(store.settings().model_copy(update={"news_daily_limit": 1}))
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, json=article())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await asyncio.gather(read_news(store, client, {}), read_news(store, client, {}))
        assert [r["status"] for r in results] == ["ok", "unavailable"]
        reopened = Store(str(tmp_path))
        assert (await read_news(reopened, client, {}))["reason"] == "daily_limit"
        monkeypatch.setattr("app.news.time.time", lambda: NOW + 86400)
        assert (await read_news(reopened, client, {}))["status"] == "ok"
        reopened.db.close()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "setting,value,reason",
    [
        ("news_enabled", False, "disabled"),
        ("news_api_key", "", "missing_api_key"),
        ("news_daily_limit", 0, "daily_limit"),
    ],
)
async def test_disabled_missing_key_or_zero_budget_avoid_network(store, setting, value, reason):
    store.save_settings(store.settings().model_copy(update={setting: value}))

    def handler(request):
        pytest.fail("Must not request news")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await read_news(store, client, {}))["reason"] == reason


@pytest.mark.parametrize("proactive", [True, False])
async def test_agent_finishes_after_failed_news_tool(store, proactive):
    requests = []

    def handler(request):
        if request.url.host == "api.thenewsapi.com":
            return httpx.Response(402, json={"error": {"code": "usage_limit_reached"}})
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "news1",
                                        "type": "function",
                                        "function": {"name": "read_news", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "How is your day?"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(store.settings(), client, store).run(
            Prompt("Join in", proactive=proactive)
        )
    assert answer.text == "How is your day?"
    assert "News is optional" in requests[0]["messages"][0]["content"]
    assert any(t["function"]["name"] == "read_news" for t in requests[0]["tools"])
    assert "provider_quota" in requests[1]["messages"][-1]["content"]


def test_dashboard_secret_settings_and_news_test(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with TestClient(create_app(str(tmp_path))) as client:
        headers = {"X-Moose-Request": "1"}
        response = client.patch(
            "/api/settings",
            headers=headers,
            json={
                "news_api_key": "private-news-key",
                "news_enabled": True,
                "news_daily_limit": 0,
            },
        )
        assert response.status_code == 200
        assert response.json()["has_news_api_key"]
        assert "private-news-key" not in client.get("/api/state").text
        assert (
            b"private-news-key"
            not in client.app.state.store.db.execute("SELECT value FROM settings").fetchone()[0]
        )
        assert client.post("/api/test/news").status_code == 403
        result = client.post("/api/test/news", headers=headers)
        assert result.status_code == 200
        assert "daily_limit" in result.json()["message"]
