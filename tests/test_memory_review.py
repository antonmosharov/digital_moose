import json

import httpx
import pytest

from app.agent import Agent
from app.config import Settings
from app.memory import MemoryEditError, apply_edits, blocks, select_blocks
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
        return httpx.Response(200, json=review([{"block_id": "", "text": "Alex prefers tea."}]))

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
        result = review([] if kind == "no_change" else [{"block_id": "nonexistent", "text": "bad"}])
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
            assert (
                json.loads(payload["messages"][1]["content"])["memory_blocks"][0]["text"]
                == "Concurrent 2"
            )
        return httpx.Response(200, json=review([{"block_id": "", "text": "New preference"}]))

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


def test_block_patches_preserve_formatting_and_address_duplicate_text():
    content = "# People\r\nSame fact\r\n\r\nSame fact\nOther fact\n"
    selected = blocks(content)
    result = apply_edits(
        content, [{"block_id": selected[1]["id"], "text": "Corrected"}], {b["id"] for b in selected}
    )
    assert result == "# People\r\nCorrected\r\n\r\nSame fact\nOther fact\n"
    assert apply_edits(content, [], set()) == content
    assert apply_edits("", [{"block_id": "", "text": "First memory"}], set()) == "First memory"


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("unknown", "unknown_or_unselected_block"),
        ("unselected", "unknown_or_unselected_block"),
        ("duplicate", "duplicate_block_edit"),
        ("fields", "invalid_edit_fields"),
        ("types", "invalid_edit_types"),
        ("size", "memory_size_limit"),
        ("list", "invalid_edit_list"),
    ],
)
def test_invalid_block_edits_are_safe(kind, reason):
    content = "Stored private text"
    block_id = blocks(content)[0]["id"]
    good = {"block_id": block_id, "text": "Updated"}
    edits = {
        "unknown": [good, {"block_id": "missing", "text": "bad"}],
        "unselected": [good],
        "duplicate": [good, good],
        "fields": [{"old": "Stored private text", "new": "bad"}],
        "types": [{"block_id": [], "text": "bad"}],
        "size": [{"block_id": "", "text": "x" * 50000}],
        "list": [good] * 21,
    }[kind]
    with pytest.raises(MemoryEditError, match=f"^{reason}$"):
        apply_edits(content, edits, set() if kind == "unselected" else {block_id})


def test_selection_is_bounded_relevant_and_unicode_safe():
    content = "\n".join([f"Unrelated fact {i}" for i in range(100)] + ["Alex likes лосёнок"])
    selected = select_blocks(content, "Alex лосёнок", 1000)
    assert len(json.dumps(selected, ensure_ascii=False)) <= 1000
    assert selected[-1]["text"] == "Alex likes лосёнок"
    assert len(selected) < len(blocks(content))
    assert select_blocks("x" * 2000, "x", 1000) == []


async def test_invalid_edit_retry_recovers_and_logs_safe_reason(store):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Public reply"}}]})
        if count == 2:
            return httpx.Response(
                200, json=review([{"block_id": "secret_invalid_ID", "text": "private output"}])
            )
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert data["retry_reason"] == "unknown_or_unselected_block"
        return httpx.Response(
            200,
            json=review([{"block_id": data["memory_blocks"][0]["id"], "text": "Corrected facts"}]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(store.settings(), client, store).run(Prompt("Correction"))
    assert count == 3
    assert answer.memory_review == "updated"
    assert answer.text == "Public reply"
    assert store.read_consciousness() == "Corrected facts"
    detail = store.activity()[0]["detail"]
    assert "unknown_or_unselected_block" in detail
    assert "secret_invalid_ID" not in detail
    assert "private output" not in detail


async def test_review_only_reads_selected_blocks_and_preserves_others(store):
    content = "\n".join([f"Unrelated fact {i}" for i in range(100)] + ["Alex prefers tea"])
    store.save_settings(
        store.settings().model_copy(
            update={"consciousness": content, "memory_review_context_chars": 1000}
        )
    )
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Noted"}}]})
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        assert "consciousness" not in data
        assert data["omitted_memory_blocks"] > 0
        assert len(json.dumps(data["memory_blocks"], ensure_ascii=False)) <= 1000
        block = next(b for b in data["memory_blocks"] if b["text"] == "Alex prefers tea")
        return httpx.Response(
            200, json=review([{"block_id": block["id"], "text": "Alex prefers coffee"}])
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await Agent(store.settings(), client, store).run(Prompt("Alex prefers coffee now"))
    assert answer.memory_review == "updated"
    assert store.read_consciousness() == content.replace("Alex prefers tea", "Alex prefers coffee")
