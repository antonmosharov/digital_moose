import json

import httpx
import pytest

from app.agent import Agent
from app.config import Settings
from app.prompts import Prompt
from app.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path))
    value.save_settings(
        Settings(
            model="chat", api_key="key", consciousness="Known facts", memory_review_max_tokens=4500
        )
    )
    yield value
    value.db.close()


def review(edits):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "review",
                            "type": "function",
                            "function": {
                                "name": "review_consciousness",
                                "arguments": json.dumps({"edits": edits}),
                            },
                        }
                    ],
                },
            }
        ]
    }


@pytest.mark.parametrize("proactive,silent", [(False, False), (True, False), (True, True)])
async def test_review_runs_even_without_optional_tools_and_has_separate_budget(
    store, proactive, silent
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[[SILENT]]" if silent else "Nice!"}}]},
            )
        return httpx.Response(200, json=review([{"old": "", "new": "Alex prefers tea."}]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(store.settings(), client, store).run(
            Prompt("I prefer tea", proactive=proactive)
        )
    assert answer.silent is silent
    assert answer.memory_review == "updated"
    assert answer.tools_used == ["review_consciousness"]
    assert store.read_consciousness() == "Known facts\nAlex prefers tea."
    assert requests[1]["max_tokens"] == 4500
    if proactive:
        assert requests[0]["max_tokens"] == 500
    assert requests[1]["tool_choice"]["function"]["name"] == "review_consciousness"


@pytest.mark.parametrize("kind", ["no_change", "network", "invalid", "truncated"])
async def test_review_outcome_preserves_reply(store, kind):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Public reply"}}]})
        if kind == "network":
            raise httpx.ReadTimeout("credentials must not leak")
        result = review([] if kind == "no_change" else [{"old": "nonexistent", "new": "bad"}])
        if kind == "truncated":
            result["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(store.settings(), client, store).run(Prompt("Hi"))
    assert answer.text == "Public reply"
    assert answer.memory_review == ("no_change" if kind == "no_change" else "failed")
    assert store.read_consciousness() == "Known facts"
    if kind != "no_change":
        event = store.activity()[0]
        assert event["status"] == "error"
        assert "review_consciousness" in event["detail"]
        assert "4500" in event["detail"]
        if kind == "truncated":
            assert "output_token_limit" in event["detail"]


@pytest.mark.parametrize("always_conflict", [False, True])
async def test_conflicting_review_retries_once_with_fresh_memory(store, always_conflict):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Hi"}}]})
        if count == 2 or always_conflict:
            store.write_consciousness(f"Concurrent {count}", store.read_consciousness())
        if count == 3:
            payload = json.loads(request.content)
            assert json.loads(payload["messages"][1]["content"])["consciousness"] == "Concurrent 2"
        return httpx.Response(200, json=review([{"old": "", "new": "New preference"}]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(store.settings(), client, store).run(Prompt("Preference"))
    assert count == 3
    assert answer.memory_review == ("conflict" if always_conflict else "updated")
    assert store.read_consciousness() == (
        "Concurrent 3" if always_conflict else "Concurrent 2\nNew preference"
    )


def test_edits_are_atomic_and_preserve_unrelated_memory(store):
    revision = store.consciousness_snapshot()["revision"]
    with pytest.raises(ValueError):
        store.revise_consciousness(
            revision, edits=[{"old": "Known", "new": "Updated"}, {"old": "absent", "new": "wrong"}]
        )
    assert store.read_consciousness() == "Known facts"
    assert (
        store.revise_consciousness(revision, edits=[{"old": "Known", "new": "Updated"}])
        == "updated"
    )
    assert store.revise_consciousness(revision, content="Stale overwrite") == "conflict"
    assert store.read_consciousness() == "Updated facts"
