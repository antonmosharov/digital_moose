"""Convert model Markdown into text and Telegram entities, without parse-mode escaping."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("strikethrough")


@dataclass(frozen=True)
class Style:
    type: str
    url: str = ""
    language: str = ""

    def entity(self, offset: int, length: int) -> dict:
        result = {"type": self.type, "offset": offset, "length": length}
        if self.url:
            result["url"] = self.url
        if self.language:
            result["language"] = self.language
        return result


@dataclass
class Run:
    text: str
    styles: tuple[Style, ...] = ()


def safe_link(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme in {"http", "https", "mailto"} and not any(
            ord(char) < 32 for char in url
        )
    except ValueError:
        return False


def markdown_runs(source: str) -> list[Run]:
    runs: list[Run] = []

    def emit(text: str, styles=()):
        if not text:
            return
        styles = tuple(dict.fromkeys(styles))
        if runs and runs[-1].styles == styles:
            runs[-1].text += text
        else:
            runs.append(Run(text, styles))

    def inline(tokens, heading: bool, quote_depth: int):
        stack: list[Style | None] = [Style("bold")] if heading else []
        kinds = {"strong": "bold", "em": "italic", "s": "strikethrough"}
        for token in tokens:
            if token.type in {"strong_open", "em_open", "s_open"}:
                stack.append(Style(kinds[token.type.removesuffix("_open")]))
            elif token.type == "link_open":
                url = token.attrGet("href") or ""
                stack.append(Style("text_link", url=url) if safe_link(url) else None)
            elif token.type in {"strong_close", "em_close", "s_close", "link_close"}:
                stack.pop()
            elif token.type == "code_inline":
                # Telegram does not allow code entities nested in bold, italic, or links.
                emit(token.content, (Style("code"),))
            elif token.type in {"softbreak", "hardbreak"}:
                emit("\n" + "› " * quote_depth)
            elif token.type == "image":
                emit(token.content or "Image", [style for style in stack if style])
                url = token.attrGet("src") or ""
                if safe_link(url):
                    emit(f" ({url})")
            else:
                emit(token.content, [style for style in stack if style])

    lists: list[int | None] = []
    heading = False
    quote_depth = 0
    for token in MARKDOWN.parse(source):
        kind = token.type
        if kind == "inline":
            inline(token.children or [], heading, quote_depth)
        elif kind == "heading_open":
            heading = True
        elif kind == "heading_close":
            heading = False
            emit("\n\n")
        elif kind == "paragraph_open" and quote_depth:
            emit("› " * quote_depth)
        elif kind == "paragraph_close":
            emit("\n" if lists else "\n\n")
        elif kind in {"fence", "code_block"}:
            language = token.info.split()[0] if token.info.strip() else ""
            emit(token.content, (Style("pre", language=language[:64]),))
            emit("\n")
        elif kind in {"bullet_list_open", "ordered_list_open"}:
            lists.append(int(token.attrGet("start") or 1) if kind == "ordered_list_open" else None)
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
            if not lists:
                emit("\n")
        elif kind == "list_item_open":
            number = lists[-1]
            emit("  " * (len(lists) - 1) + (f"{number}. " if number is not None else "• "))
            if number is not None:
                lists[-1] = number + 1
        elif kind == "blockquote_open":
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
        elif kind == "hr":
            emit("────\n\n")
    # Remove block separators added by the renderer, preserving whitespace inside code blocks.
    while runs and not runs[-1].styles and not runs[-1].text.strip():
        runs.pop()
    if runs and not runs[-1].styles:
        runs[-1].text = runs[-1].text.rstrip()
    return runs


def telegram_chunks(source: str, limit: int = 3500) -> list[dict]:
    """Keep entity offsets valid across chunk boundaries, including emoji and long code blocks."""
    if limit < 2:
        raise ValueError("Chunk limit must be at least 2 UTF-16 units")
    chunks: list[dict] = []
    text = ""
    entities: list[dict] = []
    units = 0

    def flush():
        nonlocal text, entities, units
        if text.strip():
            chunks.append({"text": text, "entities": entities})
        text, entities, units = "", [], 0

    for run in markdown_runs(source):
        remaining = run.text
        while remaining:
            if len(entities) + len(run.styles) > 90 or units >= limit:
                flush()
            available = limit - units
            taken = 0
            end = 0
            for char in remaining:
                width = 2 if ord(char) > 0xFFFF else 1
                if taken + width > available:
                    break
                taken += width
                end += 1
            if not end:
                flush()
                continue
            # Prefer a nearby line/word boundary, without dropping text or shifting offsets.
            if end < len(remaining):
                boundary = max(remaining.rfind("\n", 0, end), remaining.rfind(" ", 0, end))
                if boundary >= end // 2:
                    end = boundary + 1
                    taken = len(remaining[:end].encode("utf-16-le")) // 2
            text += remaining[:end]
            entities.extend(style.entity(units, taken) for style in run.styles)
            units += taken
            remaining = remaining[end:]
            if remaining:
                flush()
    flush()
    return chunks
