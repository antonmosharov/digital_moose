import json
import time
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.consciousness import PREFILL_KEY, history_batches, prefill_consciousness
from app.main import create_app
from app.prompts import UserError
from app.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path))
    value.save_settings(Settings(api_key="key", model="test", system_prompt="A thoughtful moose"))
    value.permit_chat(-100, "Friends", True)
    yield value
    value.db.close()


def remember(store, i=1, text="Alex enjoys hiking.", **extra):
    store.remember(
        {
            "message_id": i,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 7, "first_name": "Alex"},
            "date": time.time() - 10,
            "text": text,
            **extra,
        }
    )


def response(text="Alex enjoys hiking in chat -100.", finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"content": text}}]}


async def test_prefill_uses_personality_history_and_can_repeat(store):
    remember(store)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await prefill_consciousness(store, client)
        assert result["batches"] == 1
        assert store.read_consciousness() == result["consciousness"]
        assert store.state(PREFILL_KEY)
        assert "A thoughtful moose" in requests[0]["messages"][0]["content"]
        assert store.settings().consciousness_prompt in requests[0]["messages"][0]["content"]
        assert "Alex enjoys hiking" in requests[0]["messages"][1]["content"]
        assert "tools" not in requests[0]
        assert b"Alex enjoys" not in store.db.execute("SELECT value FROM settings").fetchone()[0]
        await prefill_consciousness(store, client)
        assert (
            json.loads(requests[-1]["messages"][1]["content"])["existing_draft"]
            == result["consciousness"]
        )
        await prefill_consciousness(store, client, mode="rebuild")
        assert json.loads(requests[-1]["messages"][1]["content"])["existing_draft"] == ""
        assert requests[-1]["max_tokens"] == store.settings().memory_review_max_tokens
    assert len(requests) == 3


def test_history_filters_and_batches_all_text(store):
    for i in range(1, 8):
        remember(store, i, f"START-{i} " + "x" * 5000 + f" END-{i}")
    remember(store, 8, "EXPIRED", date=time.time() - 91 * 86400)
    remember(store, 9, "FUTURE", date=time.time() + 100)
    store.observe_chat({"id": -200, "type": "supergroup", "title": "Blocked"})
    remember(store, 10, "BLOCKED", chat={"id": -200})
    store.observe_chat({"id": 123, "type": "private", "first_name": "Private"})
    remember(store, 11, "PRIVATE", chat={"id": 123, "type": "private"})
    batches = history_batches(store, time.time())
    assert len(batches) > 1
    assert all(len(b) <= 12000 for b in batches)
    all_text = "".join(batches)
    for i in range(1, 8):
        assert f"START-{i}" in all_text and f"END-{i}" in all_text
    assert all(s not in all_text for s in ["EXPIRED", "FUTURE", "BLOCKED", "PRIVATE"])


async def test_batch_failure_does_not_save_and_can_retry(store):
    for i in range(1, 5):
        remember(store, i, "x" * 6000)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json=response("Draft", "length" if len(requests) == 2 else "stop")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UserError, match="did not finish"):
            await prefill_consciousness(store, client)
        assert store.read_consciousness() == ""
        assert not store.state(PREFILL_KEY)
        assert json.loads(requests[1]["messages"][1]["content"])["existing_draft"] == "Draft"
        await prefill_consciousness(store, client)
    assert store.read_consciousness() == "Draft"


@pytest.mark.parametrize("change", ["memory", "personality", "access"])
async def test_prefill_preserves_concurrent_changes(store, change):
    remember(store)

    def handler(request):
        if change == "access":
            store.permit_chat(-100, "Friends", False)
        else:
            field = "consciousness" if change == "memory" else "system_prompt"
            store.save_settings(store.settings().model_copy(update={field: "New value"}))
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UserError, match="changed during analysis"):
            await prefill_consciousness(store, client)
    assert not store.state(PREFILL_KEY)
    assert store.read_consciousness() == ("New value" if change == "memory" else "")


@pytest.mark.parametrize("content", [None, "", " ", "x" * 50001])
async def test_invalid_output_never_initializes(store, content):
    remember(store)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(content)))
    ) as client:
        with pytest.raises(UserError, match="empty or invalid"):
            await prefill_consciousness(store, client)
    assert not store.state(PREFILL_KEY)
    assert store.read_consciousness() == ""


def test_prefill_endpoint_guards_and_state(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with TestClient(create_app(str(tmp_path))) as client:
        headers = {"X-Moose-Request": "1"}
        assert client.post("/api/consciousness/prefill").status_code == 403
        store = client.app.state.store
        store.save_settings(Settings(api_key="key", model="test"))
        assert client.post("/api/consciousness/prefill", headers=headers).status_code == 400
        store.permit_chat(-100, "Friends", True)
        remember(store)
        with patch("app.agent.Agent.post", return_value=response()) as post:
            assert client.post("/api/consciousness/prefill", headers=headers).status_code == 200
            assert client.post("/api/consciousness/prefill", headers=headers).status_code == 200
        assert post.await_count == 2
        assert client.get("/api/state").json()["consciousness_prefill"] == {
            "completed": True,
            "running": False,
        }
