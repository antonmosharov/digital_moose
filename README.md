# Digital Moose

A Telegram AI agent that responds **only when explicitly @mentioned**, with a web control room. Built with Python, FastAPI, SQLite, and an OpenAI-compatible Chat Completions client. No frontend build step is needed.

## Run locally

Install [uv](https://docs.astral.sh/uv/), then:

```sh
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Open **http://127.0.0.1:8000**. The dashboard works before you supply credentials; AI calls and Telegram polling need real credentials.

1. Create a bot with [@BotFather](https://t.me/BotFather). Save its token in **Connections**.
2. For ordinary group mentions, use BotFather's `/setprivacy` → Disable, then remove and re-add the bot, or make it a group admin. [Telegram's delivery rules](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get) explain this requirement. The application still discards unmentioned messages.
3. Save an API base URL, API key, and model ID. OpenRouter's base URL is `https://openrouter.ai/api/v1`; use the exact model ID from your provider. Test the saved connections.
4. In **Connected chats**, allow each group/channel ID *before* inviting the bot. IDs are negative, often starting with `-100`. To discover an unknown ID, invite the bot once: the membership update records the chat and the bot leaves it by default. Allow the observed chat and invite it again. You can instead temporarily disable automatic leaving in Agent settings; unapproved chats still cannot receive AI replies.
5. Optionally set an image-output model in **Agent settings** and test it in **Playground**. The chat model needs tool calling, plus vision for image inputs. The image model needs both image input and output for editing.
6. Click **Start agent**. Use one running application process per bot token.

## Behavior

| Incoming message | Behavior |
| --- | --- |
| `@your_bot explain the term flexible` | Uses the remaining text as the prompt. |
| Reply to text with `@your_bot explain` | Uses the new instruction plus the original message. |
| Reply to a photo with `@your_bot remove background` | Passes the original photo and edit instruction to the agent. |
| Photo with caption `@your_bot describe this` | Uses the photo and caption. |
| Photo with caption `@your_bot` | Uses a default instruction to interpret the media. |
| Reply to media with just `@your_bot` | Interprets the original media. |
| Unmentioned text/media, or an unmentioned reply to the bot | Ignored, including in private chats. |
| A quoted original message contains `@your_bot`, but the new reply does not | Ignored. |

Mention detection uses Telegram entities with UTF-16 offsets, exact usernames, and case-insensitive matching. Code spans, partial usernames, `/commands`, edited messages, and messages from bots do not activate the agent. Text mentions by bot ID also work. An entirely uncaptioned, unmentioned media upload cannot activate it; mention the bot in its caption or in a reply.

Replies stay in the original forum topic. Long text is split into Telegram-safe chunks and sent as plain text. Images are sent as documents to preserve original bytes and transparency. The agent can return text, images, or both. Background removal quality and genuine transparency depend on the chosen image model.

## Media and agent tools

- Image input: JPEG, PNG, WebP, GIF.
- PDF and UTF-8 text files (text files up to 200 KB).
- Voice/audio and video use provider-specific multimodal Chat Completions content blocks. The selected provider/model must support the media type and codec; the app does not transcode media.
- The `create_or_edit_image` tool runs in a bounded tool-call loop and passes attached images to the image model. It receives only the current request and replied-to message; there is no ambient chat history, arbitrary code execution, or web browsing tool.
- Multimodal chat image mode uses `modalities: ["image", "text"]` and expects base64 images in `message.images` (or image content blocks). Choose a compatible provider/model. The alternative `/images/generations` mode supports generation only, with `b64_json` responses; it does not support edits.
- Remote image URL outputs are deliberately unsupported; request a provider that returns base64. This avoids fetching model-supplied URLs from the server.
- Each request includes attachments from the tagged message and its direct reply target. **Telegram albums are not aggregated**: only the individual tagged item and direct reply target are processed. Reply to each desired image separately.
- There is no generated audio/video output tool; output media currently means generated/edited images. Audio/video inputs depend on model support.

Provider references: [image inputs](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding), [audio inputs](https://openrouter.ai/docs/guides/overview/multimodal/audio), [PDF inputs](https://openrouter.ai/docs/guides/overview/multimodal/pdfs), and [Chat Completions](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request).

## Configuration and security

The dashboard includes chat access, optional private chats, bot enable/pause, API connection, system prompt, image settings, temperature, token budget, tool budget, timeout, per-sender cooldown, and per-file size limits. Settings persist in `data/moose.db`; the settings record is encrypted with `data/secret.key`. Back up both together and protect access to the data directory. Secrets are never returned by the settings API. Empty credential inputs in the UI retain existing secrets; submit an explicit empty string through the settings API to clear a secret while paused.

The local dashboard has no password by default and accepts only loopback clients with approved Host headers. For remote access, set `ADMIN_PASSWORD` and `ALLOWED_HOSTS`, and put the app behind HTTPS. HTTP Basic accepts any username and the configured password. Do not expose a passwordless dashboard through a local reverse proxy, because the proxy may appear as a loopback client. Mutation endpoints require a custom same-origin request header. Static assets contain no credentials.

```sh
ADMIN_PASSWORD='choose-a-strong-password' ALLOWED_HOSTS='moose.example.com' \
  uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
```

`DATA_DIR` changes storage location. Local uvicorn does not automatically load `.env`; export variables or pass `--env-file .env`. Chat titles, IDs, and the latest 1,000 metadata events are stored locally; prompt text and media are not logged or retained. The dashboard shows the latest 100 events. Explicitly tagged content and replied-to attachments are sent to your configured AI provider.

The polling worker processes requests sequentially, applies a per-sender cooldown, retries Telegram rate limits, and persists its polling offset. Use this for small teams; large deployments need a durable work queue and per-chat scheduling. Delivery is **at least once**: a crash after Telegram delivery and before offset persistence can duplicate a reply. Restarting or saving settings cancels an in-flight request, which can be retried at restart. Existing Telegram webhooks are detected and reported, never automatically deleted. Remove an existing webhook explicitly before using this polling service.

## Docker

Set `ADMIN_PASSWORD` in a local `.env` (see `.env.example`), then:

```sh
docker compose up --build -d
```

Open http://localhost:8000 and sign in with any username and that password. Docker binds to loopback by default and keeps settings in the `moose-data` volume. The container runs as an unprivileged user.

## Verify

```sh
uv run pytest
uv run ruff check app tests
```

Tests use mocked provider/Telegram transports, so they do not consume API credits or send messages. Live integration testing requires your own bot token, provider key, model IDs, and an allowed chat.
