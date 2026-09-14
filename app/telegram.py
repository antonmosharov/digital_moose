import asyncio
import hashlib
import json
import time
from contextlib import suppress

import httpx

from app.agent import Agent, Answer
from app.prompts import Media, Prompt, UserError, attachments, mention_spans, prompt_text
from app.store import Store


class Telegram:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self.token = token
        self.client = client

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
        base = {
            "chat_id": message["chat"]["id"],
            "reply_parameters": {
                "message_id": message["message_id"],
                "allow_sending_without_reply": True,
            },
        }
        if message.get("message_thread_id"):
            base["message_thread_id"] = message["message_thread_id"]
        # Documents preserve original quality and PNG alpha/transparency.
        for media in answer.media:
            fields = {k: json.dumps(v) if isinstance(v, dict) else str(v) for k, v in base.items()}
            await self.call(
                "sendDocument", fields, files={"document": (media.name, media.data, media.mime)}
            )
        for offset in range(0, len(answer.text), 2000):
            await self.call("sendMessage", {**base, "text": answer.text[offset : offset + 2000]})
            if offset + 2000 < len(answer.text):
                await asyncio.sleep(1)


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
        telegram = Telegram(settings.bot_token, self.client)
        offset_key = "offset:" + hashlib.sha256(settings.bot_token.encode()).hexdigest()[:16]
        offset = int(self.store.state(offset_key, "0"))
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
                    for update in updates:
                        await self.handle(update, telegram)
                        offset = update["update_id"] + 1
                        self.store.set_state(offset_key, str(offset))
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
        if not message or message.get("from", {}).get("is_bot"):
            return
        username, bot_id = self.identity["username"], self.identity["id"]
        # Only the NEW message can activate the bot. Quoted mentions never activate it.
        if not mention_spans(message, username, bot_id):
            return
        chat = message["chat"]
        self.store.observe_chat(chat)
        title = chat.get("title") or chat.get("first_name") or str(chat["id"])
        if not self.store.allowed(chat, settings):
            self.store.log(title, "blocked", "Mention ignored: chat access is disabled")
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
            answer = await Agent(settings, self.client).run(prompt)
            await telegram.send(message, answer)
            self.store.log(
                title,
                "success",
                f"Replied · {len(answer.media)} files · {answer.tool_calls} tool calls",
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 — one failed prompt must not stop polling
            detail = (
                str(exc)
                if isinstance(exc, UserError)
                else "Unexpected error while processing this request."
            )
            self.store.log(title, "error", detail, time.monotonic() - started)
            with suppress(UserError):
                await telegram.send(message, Answer(text=detail))
