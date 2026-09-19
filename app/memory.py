"""Bounded memory retrieval and atomic, ID-addressed patches (no model text matching)."""

import hashlib
import json
import re


class MemoryEditError(ValueError):
    """Messages are fixed diagnostic codes, never private memory or model output."""


def blocks(content: str) -> list[dict]:
    result = []
    offset = 0
    for index, line in enumerate(content.splitlines(keepends=True)):
        text = line.rstrip("\r\n")
        if text.strip():
            digest = hashlib.sha256(text.encode()).hexdigest()[:8]
            result.append(
                {
                    "id": f"b{index}-{digest}",
                    "text": text,
                    "start": offset,
                    "end": offset + len(text),
                }
            )
        offset += len(line)
    return result


def select_blocks(content: str, query: str, budget: int) -> list[dict]:
    """Rank by lexical overlap; bound serialized memory input, including identifiers."""
    terms = set(re.findall(r"\w+", query.casefold()))
    candidates = blocks(content)
    ranked = sorted(
        candidates,
        key=lambda block: -len(terms & set(re.findall(r"\w+", block["text"].casefold()))),
    )
    selected = []
    for block in ranked:
        item = {"id": block["id"], "text": block["text"]}
        if len(json.dumps([*selected, item], ensure_ascii=False)) <= budget:
            selected.append(item)
    order = {block["id"]: index for index, block in enumerate(candidates)}
    return sorted(selected, key=lambda block: order[block["id"]])


def apply_edits(content: str, edits: list, allowed_ids: set[str]) -> str:
    if not isinstance(edits, list) or len(edits) > 20:
        raise MemoryEditError("invalid_edit_list")
    indexed = {block["id"]: block for block in blocks(content)}
    replacements, additions, seen = [], [], set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"block_id", "text"}:
            raise MemoryEditError("invalid_edit_fields")
        block_id, text = edit["block_id"], edit["text"]
        if not isinstance(block_id, str) or not isinstance(text, str):
            raise MemoryEditError("invalid_edit_types")
        if not block_id:
            if text:
                additions.append(text)
            continue
        if block_id not in indexed or block_id not in allowed_ids:
            raise MemoryEditError("unknown_or_unselected_block")
        if block_id in seen:
            raise MemoryEditError("duplicate_block_edit")
        seen.add(block_id)
        block = indexed[block_id]
        replacements.append((block["start"], block["end"], text))
    for start, end, text in sorted(replacements, reverse=True):
        content = content[:start] + text + content[end:]
    for text in additions:
        content += ("\n" if content and not content.endswith("\n") else "") + text
    if len(content) > 50000:
        raise MemoryEditError("memory_size_limit")
    return content
