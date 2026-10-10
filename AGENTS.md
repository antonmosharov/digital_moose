# Remote debugging

- The Digital Moose remote debug server is `34.39.124.243`.
- Read `MOOSE_DEBUG_HOST` and `MOOSE_DEBUG_TOKEN` from the local, git-ignored `.env` file. Never print, commit, or copy the token into documentation.
- The token authorizes read-only `GET /api/debug` with `Authorization: Bearer <token>`; it is not an SSH or dashboard credential.
- Prefer HTTPS. Check connectivity before fetching diagnostics; do not send credentials to redirects or unrelated hosts.
- On 2026-10-10, HTTPS port 443 refused connections; `http://34.39.124.243/api/debug` worked. This connection is unencrypted.
- Query parameters include `limit` (1–100), `before`, and `chat_id`. The response includes bot status, recent activity/errors, settings, and bounded conversation history. Start with `limit=1` when only status and errors are needed.
- Keep diagnostic snapshots under ignored `data/`, restrict file permissions, and avoid exposing conversation content or credentials in reports.
- The 2026-10-10 snapshot showed a running bot, with empty replies following `output_token_limit` at 2000 tokens and proactive failures at 500. Production settings reported `minimax/minimax-m3` through OpenRouter. The debug token cannot change settings or deploy fixes; use dashboard/server access for those actions.
