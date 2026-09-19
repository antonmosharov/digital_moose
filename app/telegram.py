import asyncio
import hashlib
import json
import random
import time
from contextlib import suppress
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.agent import Agent, Answer
from app.formatting import telegram_chunks
from app.prompts import (
    Media,
    Prompt,
    UserError,
    attachments,
    contains_agent_name,
    mention_spans,
    prompt_text,
)
from app.store import Store


class TelegramFormattingError(UserError):
    """Telegram explicitly rejected formatting; the same text can safely be retried unformatted."""


class TelegramPhotoError(UserError):
    """The server explicitly rejected a photo; delivery as a file is safe to retry."""


class Telegram:
    def __init__(
        self, token: str, client: httpx.AsyncClient, *, image_delivery="photo", on_sent=None
    ):
        self.token = token
        self.client = client
        self.image_delivery = image_delivery
        self.on_sent = on_sent

    async def call(self, method: str, data: dict | None = None, files=None):
        try:
            for attempt in range(3):
                kwargs = {"data": data, "files": files} if files else {"json": data or {}}
                response = await self.client.post(
                    f"https://api.telegram.org/bot{self.token}/{method}", **kwargs, timeout=40
                )
                result = response.json()
                if result.get("ok"):
                    return result["result"]
                if result.get("error_code") == 429 and attempt < 2:
                    await asyncio.sleep(min(result.get("parameters", {}).get("retry_after", 5), 60))
                    continue
                code = result.get("error_code", response.status_code)
                if code == 400 and any(
                    fragment in result.get("description", "").casefold()
                    for fragment in ("can't parse entities", "entity", "entities")
                ):
                    raise TelegramFormattingError("Telegram rejected the message formatting.")
                if (
                    method == "sendPhoto"
                    and code == 400
                    and any(
                        fragment in result.get("description", "").casefold()
                        for fragment in (
                            "photo_invalid",
                            "image_process_failed",
                            "photo_content",
                            "file is too big",
                            "wrong file type",
                        )
                    )
                ):
                    raise TelegramPhotoError("Telegram could not display this image as a photo.")
                raise UserError(
                    f"Telegram {method} failed (code {code}). Check bot permissions and configuration."
                )
        except (httpx.HTTPError, ValueError):
            raise UserError(
                "Could not connect to Telegram. Check connectivity and the bot token."
            ) from None

    async def download(self, attachment: dict, limit: int) -> Media:
        if attachment.get("file_size", 0) > limit:
            raise UserError(f"Attachment exceeds the {limit // 1024 // 1024} MB limit.")
        file = await self.call("getFile", {"file_id": attachment["file_id"]})
        try:
            chunks, size = [], 0
            async with self.client.stream(
                "GET",
                f"https://api.telegram.org/file/bot{self.token}/{file['file_path']}",
                timeout=60,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise UserError("Attachment exceeds the configured media size limit.")
                    chunks.append(chunk)
            return Media(attachment["file_name"], attachment["mime_type"], b"".join(chunks))
        except httpx.HTTPError:
            raise UserError("Telegram attachment download failed.") from None

    async def send(self, message: dict, answer: Answer):
        if answer.silent:
            return
        base = {"chat_id": message["chat"]["id"]}
        if message.get("message_id"):
            base["reply_parameters"] = {
                "message_id": message["message_id"],
                "allow_sending_without_reply": True,
            }
        if message.get("message_thread_id"):
            base["message_thread_id"] = message["message_thread_id"]

        def remember(result):
            if self.on_sent and isinstance(result, dict):
                self.on_sent(
                    {
                        "chat": message["chat"],
                        "message_thread_id": message.get("message_thread_id", 0),
                        **result,
                    }
                )

        chunks = [
            chunk for text in (answer.messages or [answer.text]) for chunk in telegram_chunks(text)
        ]
        for index, chunk in enumerate(chunks):
            try:
                result = await self.call("sendMessage", {**base, **chunk})
            except TelegramFormattingError:
                result = await self.call("sendMessage", {**base, "text": chunk["text"]})
            remember(result)
            if index + 1 < len(chunks):
                await asyncio.sleep(1)
        for media in answer.media:
            fields = {k: json.dumps(v) if isinstance(v, dict) else str(v) for k, v in base.items()}
            photo = self.image_delivery == "photo" and len(media.data) <= 10 * 1024 * 1024
            method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
            try:
                result = await self.call(
                    method, fields, files={field: (media.name, media.data, media.mime)}
                )
            except TelegramPhotoError:
                result = await self.call(
                    "sendDocument", fields, files={"document": (media.name, media.data, media.mime)}
                )
            remember(result)


class BotService:
    def __init__(self, store: Store, client: httpx.AsyncClient):
        self.store, self.client = store, client
        self.task: asyncio.Task | None = None
        self.identity: dict = {}
        self.status = "stopped"
        self.error = ""
        self.cooldowns: dict[tuple, float] = {}
        self.lock = asyncio.Lock()

    async def restart(self):
        async with self.lock:
            await self.stop()
            settings = self.store.settings()
            self.identity, self.error = {}, ""
            if settings.enabled and settings.bot_token:
                self.status = "connecting"
                self.task = asyncio.create_task(self.poll())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.status = "stopped"

    async def poll(self):
        settings = self.store.settings()

        def remember_sent(message):
            if self.store.allowed(message["chat"], self.store.settings()):
                self.store.remember(message, is_bot=True)

        telegram = Telegram(
            settings.bot_token,
            self.client,
            image_delivery=settings.image_delivery,
            on_sent=remember_sent,
        )
        offset_key = "offset:" + hashlib.sha256(settings.bot_token.encode()).hexdigest()[:16]
        offset = int(self.store.state(offset_key, "0"))

        async def consume(updates):
            nonlocal offset
            for update in updates:
                await self.handle(update, telegram)
                offset = update["update_id"] + 1
                self.store.set_state(offset_key, str(offset))

        async def refresh_before_delivery():
            # Bound catch-up work; skip the draft if the backlog is still growing.
            for _ in range(3):
                updates = await telegram.call(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 0,
                        "allowed_updates": ["message", "channel_post", "my_chat_member"],
                    },
                )
                if not updates:
                    return True
                await consume(updates)
            return False

        try:
            self.identity = await telegram.call("getMe")
            webhook = await telegram.call("getWebhookInfo")
            if webhook.get("url"):
                raise UserError(
                    "This bot has an active webhook. Remove it before using this polling app."
                )
            self.status = "running"
            while True:
                try:
                    updates = await telegram.call(
                        "getUpdates",
                        {
                            "offset": offset,
                            "timeout": 25,
                            "allowed_updates": ["message", "channel_post", "my_chat_member"],
                        },
                    )
                    self.status, self.error = "running", ""
                    await consume(updates)
                    if not updates:
                        await self.proactive(telegram, before_delivery=refresh_before_delivery)
                except UserError as exc:
                    self.status, self.error = "reconnecting", str(exc)
                    await asyncio.sleep(5)
        except UserError as exc:
            self.status, self.error = "error", str(exc)
        except Exception:  # noqa: BLE001 — isolate worker failures without exposing secret URLs
            self.status, self.error = (
                "error",
                "Unexpected bot error. Restart the bot from the dashboard.",
            )

    async def handle(self, update: dict, telegram: Telegram):
        settings = self.store.settings()
        if member := update.get("my_chat_member"):
            chat = member["chat"]
            self.store.observe_chat(chat)
            if member["new_chat_member"]["status"] in {"left", "kicked"}:
                self.store.forget_chat(chat["id"])
                return
            if (
                member["new_chat_member"]["status"] in {"member", "administrator"}
                and chat["type"] != "private"
                and settings.leave_unauthorized
                and not self.store.allowed(chat, settings)
            ):
                await telegram.call("leaveChat", {"chat_id": chat["id"]})
                self.store.log(
                    chat.get("title", str(chat["id"])), "blocked", "Left an unauthorized chat"
                )
            return
        message = update.get("message") or update.get("channel_post")
        if not message or (
            message.get("from", {}).get("is_bot") and not message.get("sender_chat")
        ):
            return
        username, bot_id = self.identity["username"], self.identity["id"]
        tagged = bool(mention_spans(message, username, bot_id))
        chat = message["chat"]
        self.store.observe_chat(chat)
        title = chat.get("title") or chat.get("first_name") or str(chat["id"])
        if not self.store.allowed(chat, settings):
            if tagged:
                self.store.log(title, "blocked", "Mention ignored: chat access is disabled")
            return
        self.store.remember(message)
        self.store.prune_history(settings, time.time())
        if not tagged:
            return
        now = time.monotonic()
        self.cooldowns = {key: expiry for key, expiry in self.cooldowns.items() if expiry > now}
        key = (chat["id"], message.get("from", {}).get("id", 0))
        if key in self.cooldowns:
            self.store.log(title, "limited", "Mention ignored: sender cooldown")
            return
        self.cooldowns[key] = now + settings.cooldown_seconds
        started = time.monotonic()
        try:
            reply = message.get("reply_to_message", {})
            prompt = Prompt(
                prompt_text(message, username, bot_id),
                reply.get("text") or reply.get("caption") or "",
            )
            self.add_context(prompt, message, telegram)
            all_attachments = attachments(reply) + attachments(message)
            for attachment in all_attachments:
                prompt.media.append(
                    await telegram.download(attachment, settings.max_media_mb * 1024 * 1024)
                )
            with suppress(UserError):
                await telegram.call(
                    "sendChatAction",
                    {
                        "chat_id": chat["id"],
                        "action": "typing",
                        **(
                            {"message_thread_id": message["message_thread_id"]}
                            if message.get("message_thread_id")
                            else {}
                        ),
                    },
                )
            answer = await Agent(settings, self.client, self.store).run(prompt)
            if not self.store.allowed(chat, self.store.settings()):
                return
            await telegram.send(message, answer)
            self.store.log(
                title,
                "success",
                f"Replied · {len(answer.media)} files · {answer.tool_calls} tool calls",
                time.monotonic() - started,
                reply_messages=answer.messages or ([answer.text] if answer.text else []),
                tools_used=answer.tools_used,
                memory_review=answer.memory_review,
            )
        except Exception as exc:  # noqa: BLE001 — one failed prompt must not stop polling
            detail = (
                str(exc)
                if isinstance(exc, UserError)
                else "Unexpected error while processing this request."
            )
            self.store.log(title, "error", detail, time.monotonic() - started)
            if self.store.allowed(chat, self.store.settings()):
                with suppress(UserError):
                    await telegram.send(message, Answer(text=detail))

    def add_context(
        self, prompt: Prompt, message: dict, telegram: Telegram, now: float | None = None
    ):
        now = time.time() if now is None else now
        chat, thread = message["chat"], message.get("message_thread_id", 0)
        prompt.conversation = {
            "chat_id": chat["id"],
            "chat_type": chat.get("type"),
            "thread_id": thread,
            "sender": message.get("from", {}),
        }
        prompt.instruction += (
            "\nCurrent conversation time: "
            + datetime.fromtimestamp(
                now, ZoneInfo(self.store.settings().proactive_timezone)
            ).isoformat()
            + ". Historical message timestamps may be older; do not assume old plans are current."
        )
        ceiling = message["message_id"]
        prompt.history = self.store.history(
            chat["id"], thread, ceiling, since=now - 3600, until=now
        )["messages"]
        cursor = prompt.history[0]["message_id"] if prompt.history else ceiling

        def check_access():
            settings = self.store.settings()
            if not self.store.allowed(chat, settings):
                raise UserError("Access to this conversation was revoked.")
            self.store.prune_history(settings, time.time())
            return settings

        def read_history(before: int | None, limit: int):
            nonlocal cursor
            check_access()
            page = self.store.history(
                chat["id"], thread, min(before or cursor, ceiling), limit=limit, until=now
            )
            if page["next_before_message_id"] is not None:
                cursor = page["next_before_message_id"]
            return page

        async def read_media(reference: str):
            settings = check_access()
            try:
                prefix, ref_chat, ref_thread, ref_message = reference.split(":")
                ref_id = int(ref_message)
                valid = (
                    prefix == "telegram"
                    and int(ref_chat) == chat["id"]
                    and int(ref_thread) == thread
                    and 0 < ref_id < ceiling
                )
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise UserError("Media reference must belong to this conversation's history.")
            attachment = self.store.media_attachment(chat["id"], thread, ref_id)
            if not attachment:
                raise UserError("This media reference is unavailable or has expired.")
            return await telegram.download(attachment, settings.max_media_mb * 1024 * 1024)

        prompt.history_reader, prompt.media_reader = read_history, read_media

    @staticmethod
    def daytime(settings, now: float) -> bool:
        hour = datetime.fromtimestamp(now, ZoneInfo(settings.proactive_timezone)).hour
        start, end = settings.daytime_start, settings.daytime_end
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end if start != end else False

    async def proactive(
        self, telegram: Telegram, *, now: float | None = None, before_delivery=None
    ):
        now = time.time() if now is None else now
        settings = self.store.settings()
        self.store.prune_history(settings, now)
        if (
            not settings.enabled
            or not self.daytime(settings, now)
            or not (settings.wake_enabled or settings.participation_enabled)
        ):
            return
        local = datetime.fromtimestamp(now, ZoneInfo(settings.proactive_timezone))
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        for latest in self.store.conversations():
            chat_id, thread = latest["chat_id"], latest["thread_id"]
            chat = {"id": chat_id, "type": latest["kind"], "title": latest["title"]}
            if chat["type"] not in {"group", "supergroup"} or not self.store.allowed(
                chat, settings
            ):
                continue  # Automatic participation is for allowed group conversations.
            if not self.store.proactive_available(chat_id, now, day_start, settings):
                continue
            silence = now - latest["sent_at"]
            human = latest["human_id"]
            probability = settings.participation_probability
            group = self.store.group_activity(chat_id)
            group_silence = now - group["sent_at"]
            if (
                settings.wake_enabled
                and group_silence >= settings.wake_after_hours * 3600
                and latest["message_id"] == group["message_id"]
                and group["human_id"]
            ):
                kind, instruction, anchor = "wake", settings.wake_prompt, group["human_id"]
                instruction += (
                    f"\nThis is a group-wide inactivity wake-up. Last observed message across all "
                    f"topics: {datetime.fromtimestamp(group['sent_at'], ZoneInfo(settings.proactive_timezone)).isoformat()}. "
                    f"Measured group silence: {int(group_silence)} seconds. "
                    "Do not infer silence from old history or memories, or claim a different duration. "
                    "Prefer a natural opening without announcing how long the chat has been silent."
                )
            elif (
                settings.participation_enabled
                and human
                and not latest["is_bot"]
                and settings.participation_delay_minutes * 60 <= silence <= 3600
            ):
                context = self.store.participation_context(chat_id, thread, now)
                if (
                    sum(len(item["text"].strip()) for item in context)
                    < settings.natural_reply_min_context
                ):
                    continue
                counts = self.store.db.execute(
                    """SELECT COUNT(*), COUNT(DISTINCT sender_id) FROM messages
                       WHERE chat_id=? AND thread_id=? AND is_bot=0 AND sent_at BETWEEN ? AND ?""",
                    (chat_id, thread, now - 3600, now),
                ).fetchone()
                named = any(
                    not item["is_bot"] and contains_agent_name(item["text"], settings.agent_names)
                    for item in context
                )
                if not named and (counts[0] < 3 or counts[1] < 2):
                    continue
                if named:
                    probability = settings.name_mention_probability
                kind, instruction, anchor = "participation", settings.participation_prompt, human
            else:
                continue
            # Persist BEFORE rolling or generating: polling/restarts cannot reroll the same pause.
            claimed = (
                self.store.claim_group_wake(chat_id, anchor)
                if kind == "wake"
                else self.store.claim_opportunity(chat_id, thread, kind, anchor)
            )
            if not claimed:
                continue
            if kind == "participation" and random.random() >= probability:
                continue
            self.store.record_proactive_attempt(chat_id, now)
            message = {"chat": chat, "message_thread_id": thread}
            prompt = Prompt(
                "Consider contributing to this conversation without an explicit mention.",
                instruction=instruction,
                proactive=True,
            )
            self.add_context(
                prompt, {**message, "message_id": latest["message_id"] + 1}, telegram, now
            )
            try:
                answer = await Agent(settings, self.client, self.store).run(prompt)
                if answer.silent:
                    self.store.log(
                        chat["title"],
                        "limited",
                        f"{kind}: chose to stay silent",
                        reply_messages=[],
                        tools_used=answer.tools_used,
                        memory_review=answer.memory_review,
                    )
                    continue
                # Drain updates that arrived during inference before publishing a stale interruption.
                if before_delivery and await before_delivery() is False:
                    continue
                current = self.store.settings()
                if kind == "wake":
                    current_group = self.store.group_activity(chat_id)
                    delivery_now = time.time() if before_delivery else now
                    if (
                        current_group != group
                        or delivery_now - current_group["sent_at"] < current.wake_after_hours * 3600
                    ):
                        continue
                newest = self.store.db.execute(
                    "SELECT message_id FROM conversation_state WHERE chat_id=? AND thread_id=?",
                    (chat_id, thread),
                ).fetchone()
                if (
                    not newest
                    or newest[0] != latest["message_id"]
                    or not current.enabled
                    or not self.store.allowed(chat, current)
                    or not self.daytime(current, time.time() if before_delivery else now)
                    or not (
                        current.wake_enabled if kind == "wake" else current.participation_enabled
                    )
                ):
                    continue
                if kind == "participation":
                    message["message_id"] = latest["message_id"]
                await telegram.send(message, answer)
                self.store.log(
                    chat["title"],
                    "success",
                    f"Unprompted {kind} message",
                    reply_messages=answer.messages or ([answer.text] if answer.text else []),
                    tools_used=answer.tools_used,
                    memory_review=answer.memory_review,
                )
            except Exception as exc:  # noqa: BLE001 — isolate scheduled request failures
                detail = (
                    str(exc)
                    if isinstance(exc, UserError)
                    else "Unexpected proactive request error."
                )
                self.store.log(chat["title"], "error", f"{kind}: {detail}")
