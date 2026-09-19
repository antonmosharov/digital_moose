# Digital Moose

A Telegram AI agent with conversation memory, replies to **explicit @mentions**, and configurable, occasional participation in group conversations, with a web control room. Built with Python, FastAPI, SQLite, and an OpenAI-compatible Chat Completions client. No frontend build step is needed.

## Run locally

Install [uv](https://docs.astral.sh/uv/), then:

```sh
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Open **http://127.0.0.1:8000**. The dashboard works before you supply credentials; AI calls and Telegram polling need real credentials.

1. Create a bot with [@BotFather](https://t.me/BotFather). Save its token in **Connections**.
2. For ordinary group mentions, use BotFather's `/setprivacy` → Disable, then remove and re-add the bot, or make it a group admin. [Telegram's delivery rules](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get) explain this requirement. The application stores messages it observes in allowed chats, including unmentioned messages, to provide conversation context. Telegram does not offer arbitrary history backfill through the Bot API.
3. Save an API base URL, API key, and model ID. OpenRouter's base URL is `https://openrouter.ai/api/v1`; use the exact model ID from your provider. Test the saved connections.
4. In **Connected chats**, allow each group/channel ID *before* inviting the bot. IDs are negative, often starting with `-100`. To discover an unknown ID, invite the bot once: the membership update records the chat and the bot leaves it by default. Allow the observed chat and invite it again. You can instead temporarily disable automatic leaving in Agent settings; unapproved chats still cannot receive AI replies.
5. In **Agent settings**, configure conversation memory, daytime hours and timezone, wake-up messages, and occasional participation. Unprompted participation applies only to allowed groups; private chats and channels still require mentions. Both group participation modes are enabled by default when the agent runs.
6. Optionally set an image-output model in **Agent settings** and test it in **Playground**. The chat model needs tool calling for history/media retrieval and image generation, plus vision for image inputs. The image model needs both image input and output for editing.
7. Click **Start agent**. Use one running application process per bot token.

## Behavior

### Optional news reading

Save your The News API token in **Connections → The News API**, then enable the tool
in **Agent settings → Optional news reading**. The token is encrypted in the settings database, masked in
dashboard responses, redacted from HTTPX request logs, and never included in model
prompts. No environment variable is needed. The feature defaults to disabled.

The agent can optionally call `read_news` during a normal reply, group participation,
quiet-chat wake-up, or playground request, up to twice per run. Editable **News guidance**
encourages one relevant story with a source link and the agent's own clearly separated
perspective. News does not trigger participation by itself or bypass existing chat limits.

The tool accepts optional `country` (`ae`, `jp`, `ru`), `category`, and a short `topic`.
The agent chooses a topic based on the conversation or its personality, such as robotics
or food festivals. The old `query` argument remains accepted for existing callers.
Omitting the country chooses one randomly; omitting both category and topic chooses a
random supported category. Queries search for country-related terms in titles,
descriptions, and keywords, so relevance is approximate and the model can skip results.
It requests up to five article excerpts from the last 72 hours, newest first, without
restricting the source language. The provider may return fewer matches or apply a lower
plan limit. The model compares those excerpts and can discuss a relevant story in the
chat's language. Full article retrieval is not enabled.

Uses [The News API's documented All News endpoint](https://www.thenewsapi.com/documentation):
`GET https://api.thenewsapi.com/v1/news/all`, with URL-encoded `api_token`, `search`,
`search_fields`, optional `categories`, `published_after`, `sort=published_at`, and `limit=5`.
Responses contain `meta` pagination information and a `data` article array; the tool
passes bounded titles, descriptions, snippets, source URLs, languages, and publication
timestamps to the model. These are untrusted excerpts, not full article contents.

**Daily news request limit** defaults to 20 and is shared across all chats, playground,
and the saved-connection test. Set it to your allowance or lower; zero blocks requests.
The persistent local counter resets at midnight UTC. Attempts count even when they fail.
The provider's own allowance may differ. HTTP 402 pauses until the next UTC day; HTTP 429
pauses for a minute; other HTTP/network/invalid-response failures pause for five minutes.
Calls time out after 12 seconds and do not retry automatically. Unavailable news becomes
a tool result asking the model to continue without news, never an exception sent to chat.
The connection test can run while news is disabled, but still respects and consumes the
shared request budget. Disable the switch at any time to remove the tool from new runs.

### Names and consciousness

In **Agent settings → Replies when named**, enter a comma-separated string such as
`moose, лось, лосик, лосёнок`. Matching uses whole words, ignores case, and treats
`ё` and `е` alike. We inspect the latest 10 messages within the last hour in the
same chat/topic. When any human message or caption in that window contains a name,
the **Chance when named** replaces the general participation probability (defaults:
50% versus 5%). A named message qualifies on its own; ordinary participation still
requires three messages from two people. Occasional participation must be enabled.
**Natural reply minimum context** defaults to 100 characters. Both ordinary and
name-triggered participation require at least this much combined text/caption content
from that same window, after trimming surrounding whitespace from each message.
Sender names, metadata, and attachment descriptions do not count. Set it to zero to
disable the minimum. Short context is skipped before rolling the probability or
calling the AI, without consuming an attempt. This does not affect explicit tags or
the separate quiet-chat wake-up mode.
Both use the configured pause, daytime window, cooldown, daily limits, and one roll
per pause. The model can still stay silent. Explicit @tags retain direct-reply behavior.
An empty names string disables matching; a name probability of zero skips named pauses.

**Consciousness** is editable persistent text, encrypted with the other settings and
included alongside the system prompt on every request. It is shared across chats and
the playground. **Consciousness guidance** explains how the agent should use it for
observations, preferences, beliefs, highlights, and reflections. The agent can call
`read_consciousness` and `write_consciousness` during direct replies, proactive turns,
and playground requests, with up to three calls to each tool per request. It cannot
run outside those requests. Writes replace the complete text (up to 50,000 characters)
and require the previous text to avoid overwriting concurrent revisions. After a
conflict, the agent must read again and merge. Successful writes immediately update
the current request's system context and persist for future requests and restarts.

The dashboard refreshes consciousness when it has no unsaved edits and sends only
changed settings. Conflicting consciousness edits return an error instead of replacing
newer memories. Clearing the field clears memory. Revoking chat access deletes stored
chat history but does not remove reflections from the shared consciousness; those can
be revised separately in the dashboard.

To seed initially empty consciousness, save your personality and consciousness guidance,
then click **Initialize from conversation history** in the Consciousness card. This
one-time action analyzes all retained text/captions in currently allowed chats (including
private chats only when enabled), in chronological batches through the configured AI
provider. It does not download attachments. Each batch refines a compact draft with
chat, participant, and timestamp context. Large histories require multiple model calls
and may take several minutes. Max response tokens controls the draft output budget.
Only the completed analysis is saved; failures and concurrent memory/personality/access
changes leave existing memory untouched and allow retry. A persistent completion marker
prevents rerunning the action even if consciousness is later cleared. Normal admin and
agent memory editing remains available. The API action is `POST /api/consciousness/prefill`.

| Incoming message | Behavior |
| --- | --- |
| `@your_bot explain the term flexible` | Uses the remaining text as the prompt. |
| Reply to text with `@your_bot explain` | Uses the new instruction plus the original message. |
| Reply to a photo with `@your_bot remove background` | Passes the original photo and edit instruction to the agent. |
| Photo with caption `@your_bot describe this` | Uses the photo and caption. |
| Photo with caption `@your_bot` | Uses a default instruction to interpret the media. |
| Reply to media with just `@your_bot` | Interprets the original media. |
| Unmentioned text/media in an allowed chat | Stored as conversation context; no immediate response. Eligible group pauses may trigger optional participation. |
| A quoted original message contains `@your_bot`, but the new reply does not | Does not count as a mention; stored as context. |

Mention detection uses Telegram entities with UTF-16 offsets, exact usernames, and case-insensitive matching. Code spans, partial usernames, `/commands`, edited messages, and messages from bots do not activate the agent. Text mentions by bot ID also work. An entirely uncaptioned, unmentioned media upload does not trigger an immediate reply; mention the bot in its caption or in a reply.

Replies stay in the original forum topic. Model Markdown is converted into Telegram formatting: bold, italic, strikethrough, clickable links, inline code, and fenced code blocks. Headings become bold text; lists and quotes use readable prefixes. Raw HTML remains literal text. Long replies are split with Unicode-safe formatting offsets. If Telegram explicitly rejects formatting, that chunk is retried as readable plain text. Generated images are sent as **inline photos** after the text reply. Telegram may compress photos and remove transparency. Select **Original file** delivery to preserve the original bytes and alpha channel. Images larger than the photo upload limit, or explicitly rejected as unsupported photos, fall back to document delivery; connectivity and permission errors are not retried as documents. The agent can return text, images, or both. Background removal quality and genuine transparency depend on the chosen image model.

## Media and agent tools

- Image input: JPEG, PNG, WebP, GIF.
- PDF and UTF-8 text files (text files up to 200 KB).
- Voice/audio and video use provider-specific multimodal Chat Completions content blocks. The selected provider/model must support the media type and codec; the app does not transcode media.
- The `create_or_edit_image` tool runs in a bounded tool-call loop and passes attached images to the image model. It can use attachments in the current request/reply and images explicitly fetched from stored history. There is no arbitrary code execution or web browsing tool.
- Multimodal chat image mode uses `modalities: ["image", "text"]` and expects base64 images in `message.images` (or image content blocks). Choose a compatible provider/model. The alternative `/images/generations` mode supports generation only, with `b64_json` responses; it does not support edits.
- Remote image URL outputs are deliberately unsupported; request a provider that returns base64. This avoids fetching model-supplied URLs from the server.
- SVG/vector output is unsupported. Recraft vector models are rejected before generation; Recraft Styles additionally requires a style-reference image and is not a general editing model. For ordinary generation and edits, choose a raster image model (for example, `google/gemini-2.5-flash-image` in multimodal chat mode).
- Image tool failures are surfaced directly and recorded as errors, with safe provider status hints. The chat model cannot replace these failures with a generic apology or repeatedly retry a failed image request.
- Each request includes attachments from the tagged message and its direct reply target. **Telegram albums are not aggregated**: only the individual tagged item and direct reply target are processed. Reply to each desired image separately.
- There is no generated audio/video output tool; output media currently means generated/edited images. Audio/video inputs depend on model support.

Provider references: [image inputs](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding), [audio inputs](https://openrouter.ai/docs/guides/overview/multimodal/audio), [PDF inputs](https://openrouter.ai/docs/guides/overview/multimodal/pdfs), and [Chat Completions](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request).

## Conversation memory and media lookup

For every mention, the bot includes the **10 previous messages in the same chat and forum topic**, excluding messages older than **one hour**. Entries identify the sender, timestamp, text/caption, reply target, and a `telegram:chat:topic:message` reference. The triggering message is provided separately. The bot records its own successfully delivered replies too, including each text bubble and image. Other bots and service events do not trigger replies.

Historical media is represented by its type, filename, and message reference, never its bytes. Two bounded tools let the model request more context:

- `get_previous_messages`: read older retained messages, default 20 and maximum 50 per call. Omitting `before_message_id` advances backward from the initial context or last page; supplying it selects a cursor. Responses include a next cursor and `has_more`. The one-hour filter applies only to automatic context, not this tool. Default budget: **5 calls per request**.
- `get_message_media`: fetch an attachment using its history reference. The bot checks chat/topic access and the request's history boundary, downloads within the configured per-file size limit, then provides the actual content to the model. Retrieved images also become available to the image-editing tool. Default budget: **3 calls per request**. Duplicate fetches are cached within the request. Expired/unavailable files return a tool error.

Both budgets are configurable, and zero disables the respective tool. Invalid calls consume budget; the total loop is bounded with a final completion. History never crosses chats or topics. Individual history entries are limited to 4,000 characters when sent to the model (with an explicit truncation marker). Storage retention is configurable from 3 to 3,650 days. Only messages observed while the bot has access are available; Telegram does not provide full past conversation history to bots. Revoked access prevents subsequent history/media queries. History does not currently synchronize message edits or deletions.

## Natural replies and optional participation

**Multiple replies and memes:** by default, 25% of triggered requests permit the model to send up to three short text messages, followed by any generated images. This is an opportunity, not a requirement: the model chooses whether a split or an unsolicited, relevant meme is appropriate. Code draws the probability and caps the number of text bubbles; a supplemental system instruction controls tone and relevance. Set the probability to zero to disable this behavior. Telegram's mandatory length-based splitting still applies to long text. Unprompted participation stays to one short text reply and cannot generate images.

**Wake-up:** after 48 hours of silence, send one optional friendly message during daytime. The separate wake-up prompt can ask for a joke, greeting, or continuation of an older conversation through the history tool. After that attempt, the bot waits for a new human message before another wake-up; it never keeps waking an unanswered chat. Silence includes the bot's own messages. Timing metadata survives content retention expiry.

**Occasional participation:** after five minutes of silence, draw one 5% probability check for that pause. Eligibility requires at least three human messages from two distinct senders in the preceding hour, with a human speaking last. Pauses older than an hour are not considered for this mode. A failed draw is persisted and never retried until another human message creates a new pause. If selected, the model can still return `[[SILENT]]` when it has nothing useful to add.

Both modes:

- Apply only to allowed groups/supergroups while the agent is running. They are individually configurable and enabled by default.
- Use backend settings `proactive_timezone`, `daytime_start`, and `daytime_end`; defaults are **Asia/Dubai, 09:00–21:00**, with the end hour excluded. Overnight windows work and daylight-saving changes follow the selected IANA timezone. Edit these in **Agent settings** or `PATCH /api/settings`.
- Share a default **six-hour cooldown** and **two attempts per local calendar day**, across all topics in a group. Selected attempts count even if the model declines, fails, or a draft is discarded; failed probability draws do not count. These limits are configurable.
- Persist opportunity checks and budgets across restarts. Before delivery, pending Telegram updates are processed; if the topic changed during generation, the draft is discarded. Access, enable switches, and daytime hours are checked again.
- Keep proactive errors in dashboard activity rather than sending error messages to the group.

The poll loop evaluates opportunities after catching up with Telegram updates (normally within about 25 seconds of an eligible pause, plus inference time). Prompts and media queries still use your configured AI provider and incur its normal usage costs.

## Configuration and security

The dashboard includes chat access, optional private chats, bot enable/pause, API connection, system prompt, image settings, temperature, token budget, tool budget, timeout, per-sender cooldown, and per-file size limits. Settings persist in `data/moose.db`; the settings record is encrypted with `data/secret.key`. Back up both together and protect access to the data directory. Secrets are never returned by the settings API. Empty credential inputs in the UI retain existing secrets; submit an explicit empty string through the settings API to clear a secret while paused.

The local dashboard has no password by default and accepts only loopback clients with approved Host headers. For remote access, set `ADMIN_PASSWORD` and `ALLOWED_HOSTS`, and put the app behind HTTPS. HTTP Basic accepts any username and the configured password. Do not expose a passwordless dashboard through a local reverse proxy, because the proxy may appear as a loopback client. Mutation endpoints require a custom same-origin request header. Static assets contain no credentials.

```sh
ADMIN_PASSWORD='choose-a-strong-password' ALLOWED_HOSTS='moose.example.com' \
  uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
```

`DATA_DIR` changes storage location. Local uvicorn does not automatically load `.env`; export variables or pass `--env-file .env`. Chat titles, IDs, scheduling state, and the latest 1,000 metadata events are stored locally. Conversation text, sender names, and Telegram attachment metadata/file IDs are encrypted using the same local key as settings and retained for 90 days by default. **Unmentioned messages in allowed chats are included.** Media bytes are never retained. Revoking chat access deletes its history and scheduling state; disabling private chats deletes stored private history. The dashboard shows the latest 100 activity events without message text. Recent history, queried history, and explicitly attached or fetched media are sent to your configured AI provider. Keep both the database and encryption key private; they are backed up together.

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
