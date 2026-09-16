from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent import Answer
from app.formatting import telegram_chunks
from app.prompts import UserError, utf16_slice
from app.telegram import Telegram


def styled_text(chunk, kind):
    return [
        utf16_slice(chunk["text"], e["offset"], e["length"])
        for e in chunk["entities"]
        if e["type"] == kind
    ]


def test_common_model_markdown():
    (chunk,) = telegram_chunks(
        "## Explanation\n\n**Flexible** means *adaptable*. ~~Rigid~~.\n\n"
        "- First item\n- Second item\n\n[Read more](https://example.com)"
    )
    assert (
        chunk["text"]
        == "Explanation\n\nFlexible means adaptable. Rigid.\n\n• First item\n• Second item\n\nRead more"
    )
    assert styled_text(chunk, "bold") == ["Explanation", "Flexible"]
    assert styled_text(chunk, "italic") == ["adaptable"]
    assert styled_text(chunk, "strikethrough") == ["Rigid"]
    assert (
        next(e for e in chunk["entities"] if e["type"] == "text_link")["url"]
        == "https://example.com"
    )


def test_code_preserves_markdown_and_html_characters():
    (chunk,) = telegram_chunks('Use `foo_bar`:\n\n```python\nprint("<b>**hello**</b> & x_y")\n```')
    assert styled_text(chunk, "code") == ["foo_bar"]
    assert styled_text(chunk, "pre") == ['print("<b>**hello**</b> & x_y")\n']
    assert next(e for e in chunk["entities"] if e["type"] == "pre")["language"] == "python"
    assert "```" not in chunk["text"]


def test_emoji_offsets_use_utf16():
    (chunk,) = telegram_chunks("🫎 **Hello 🫎** and _bye_.")
    bold = next(e for e in chunk["entities"] if e["type"] == "bold")
    assert bold["offset"] == 3
    assert bold["length"] == 8
    assert styled_text(chunk, "bold") == ["Hello 🫎"]
    assert styled_text(chunk, "italic") == ["bye"]


def test_nested_emphasis_does_not_overlap_code():
    (chunk,) = telegram_chunks("**bold *italic* and `code`**")
    code = next(e for e in chunk["entities"] if e["type"] == "code")
    for entity in chunk["entities"]:
        if entity is not code:
            assert entity["offset"] + entity["length"] <= code["offset"]
    assert styled_text(chunk, "italic") == ["italic"]


@pytest.mark.parametrize("wrapper,kind", [("**{}**", "bold"), ("```text\n{}\n```", "pre")])
def test_long_formatted_text_and_emoji_are_split_safely(wrapper, kind):
    original = "🫎 a" * 2000
    chunks = telegram_chunks(wrapper.format(original))
    assert len(chunks) > 1
    assert "".join(c["text"] for c in chunks).rstrip("\n") == original
    for chunk in chunks:
        size = len(chunk["text"].encode("utf-16-le")) // 2
        assert size <= 3500
        assert styled_text(chunk, kind) == [chunk["text"]]
        for entity in chunk["entities"]:
            assert 0 <= entity["offset"] < size
            assert entity["offset"] + entity["length"] <= size


def test_many_entities_split_without_losing_words():
    chunks = telegram_chunks(" ".join(f"**word{i}**" for i in range(250)))
    assert "".join(c["text"] for c in chunks) == " ".join(f"word{i}" for i in range(250))
    assert all(len(c["entities"]) <= 90 for c in chunks)
    assert sum(len(c["entities"]) for c in chunks) == 250


def test_raw_html_special_characters_and_unclosed_markdown():
    (chunk,) = telegram_chunks("<b>literal</b> & 2 < 3; snake_case; **unfinished; \\*literal\\*")
    assert "<b>literal</b> & 2 < 3; snake_case; **unfinished; *literal*" == chunk["text"]
    assert chunk["entities"] == []


def test_lists_quotes_and_unsafe_links():
    (chunk,) = telegram_chunks("3. Three\n4. Four\n\n> A quote\n\n[unsafe](javascript:alert(1))")
    assert "3. Three\n4. Four" in chunk["text"]
    assert "› A quote" in chunk["text"]
    assert not any(e["type"] == "text_link" for e in chunk["entities"])


async def test_telegram_sends_formatted_entities_in_reply():
    telegram = Telegram("token", AsyncMock())
    telegram.call = AsyncMock()
    await telegram.send(
        {"chat": {"id": -100}, "message_id": 9, "message_thread_id": 8}, Answer("**Hello**")
    )
    method, payload = telegram.call.call_args.args
    assert method == "sendMessage"
    assert payload["text"] == "Hello"
    assert payload["entities"] == [{"type": "bold", "offset": 0, "length": 5}]
    assert payload["reply_parameters"]["message_id"] == 9
    assert payload["message_thread_id"] == 8


async def test_explicit_entity_rejection_retries_readable_plain_text():
    requests = []

    def handler(request):
        import json

        payload = json.loads(request.content)
        requests.append(payload)
        if "entities" in payload:
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: can't parse entities",
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 10}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await Telegram("token", client).send(
            {"chat": {"id": -100}, "message_id": 9}, Answer("**Hello**")
        )
    assert len(requests) == 2
    assert requests[1]["text"] == "Hello"
    assert "entities" not in requests[1]


async def test_permission_errors_are_not_retried_as_formatting_failures():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            403, json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UserError, match="403"):
            await Telegram("token", client).send(
                {"chat": {"id": -100}, "message_id": 9}, Answer("**Hello**")
            )
    assert len(requests) == 1
