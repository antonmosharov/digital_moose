import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agent import Agent
from app.config import Settings
from app.consciousness import PREFILL_KEY, prefill_consciousness
from app.news import read_news
from app.news import usage as news_usage
from app.prompts import Media, Prompt, UserError
from app.store import Store
from app.telegram import BotService, Telegram

STATIC = Path(__file__).parent / "static"
security = HTTPBasic(auto_error=False)


def create_app(data_dir: str | None = None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(data_dir or os.getenv("DATA_DIR", "data"))
        async with httpx.AsyncClient(follow_redirects=False) as client:
            app.state.store, app.state.client = store, client
            app.state.bot = BotService(store, client)
            app.state.playground_lock = asyncio.Lock()
            app.state.consciousness_prefill_lock = asyncio.Lock()
            await app.state.bot.restart()
            yield
            await app.state.bot.stop()
        store.db.close()

    async def auth(
        request: Request, credentials: Annotated[HTTPBasicCredentials | None, Depends(security)]
    ):
        password = os.getenv("ADMIN_PASSWORD", "")
        if password:
            if not credentials or not secrets.compare_digest(
                credentials.password.encode(), password.encode()
            ):
                raise HTTPException(
                    401,
                    "Sign in with your admin password",
                    headers={"WWW-Authenticate": 'Basic realm="Moose"'},
                )
        elif not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "Set ADMIN_PASSWORD to enable remote dashboard access")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("x-moose-request") != "1"
        ):
            raise HTTPException(403, "Missing dashboard request header")

    app = FastAPI(
        title="Digital Moose",
        lifespan=lifespan,
        dependencies=[Depends(auth)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            h.strip()
            for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(",")
        ],
    )

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
            }
        )
        return response

    @app.exception_handler(UserError)
    async def user_error(request: Request, exc: UserError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state(request: Request):
        store, bot = request.app.state.store, request.app.state.bot
        activity = store.activity()
        return {
            "settings": store.public_settings(),
            "news_usage": news_usage(store),
            "consciousness_prefill": {
                "completed": bool(store.state(PREFILL_KEY)),
                "running": request.app.state.consciousness_prefill_lock.locked(),
            },
            "chats": store.chats(),
            "activity": activity,
            "bot": {
                "status": bot.status,
                "error": bot.error,
                "username": bot.identity.get("username", ""),
            },
            "stats": {
                "responses": sum(x["status"] == "success" for x in activity),
                "errors": sum(x["status"] == "error" for x in activity),
            },
        }

    @app.patch("/api/settings")
    async def settings(request: Request, patch: dict):
        store = request.app.state.store
        patch = dict(patch)
        previous = patch.pop("consciousness_previous", None)
        if (
            "consciousness" in patch
            and previous is not None
            and previous != store.read_consciousness()
        ):
            raise HTTPException(
                409, "Consciousness changed. Reload the page and merge your edits before saving."
            )
        try:
            current = store.settings().model_dump()
            current.update(patch)
            updated = Settings.model_validate(current)
        except ValidationError:
            # Pydantic's input field could expose submitted credentials.
            raise HTTPException(
                422, "Invalid settings. Check field names, URLs, and numeric limits."
            ) from None
        if updated.enabled and not all([updated.bot_token, updated.api_key, updated.model]):
            raise HTTPException(
                400, "Set the bot token, AI API key, and chat model before starting"
            )
        store.save_settings(updated)
        if not updated.allow_private:
            for chat in store.chats():
                if chat["kind"] == "private":
                    store.forget_chat(chat["id"])
        store.prune_history(updated, time.time())
        await request.app.state.bot.restart()
        return store.public_settings()

    @app.post("/api/consciousness/prefill")
    async def prefill(request: Request):
        lock = request.app.state.consciousness_prefill_lock
        if lock.locked():
            raise HTTPException(409, "Consciousness initialization is already running.")
        async with lock:
            return await prefill_consciousness(request.app.state.store, request.app.state.client)

    class ChatInput(BaseModel):
        id: int
        title: str = Field(default="", max_length=120)
        allowed: bool = True

    @app.put("/api/chats")
    async def chat(request: Request, value: ChatInput):
        if value.id >= 0:
            raise HTTPException(
                400,
                "Group and channel IDs must be negative. Private chat access is controlled in settings.",
            )
        request.app.state.store.permit_chat(value.id, value.title, value.allowed)
        return {"ok": True}

    @app.post("/api/test/telegram")
    async def test_telegram(request: Request):
        settings = request.app.state.store.settings()
        if not settings.bot_token:
            raise UserError("Save a Telegram bot token first.")
        me = await Telegram(settings.bot_token, request.app.state.client).call("getMe")
        return {"message": f"Connected as @{me['username']}"}

    @app.post("/api/test/ai")
    async def test_ai(request: Request):
        settings = request.app.state.store.settings().model_copy(update={"image_tools": False})
        await Agent(settings, request.app.state.client).run(Prompt("Reply with OK."))
        return {"message": "AI connection is working"}

    @app.post("/api/test/news")
    async def test_news(request: Request):
        result = await read_news(request.app.state.store, request.app.state.client, {}, test=True)
        return {
            "message": (
                f"News connected: {len(result['articles'])} articles returned."
                if result["status"] == "ok"
                else "News connected; no recent matches for this topic."
                if result["status"] == "empty"
                else f"News unavailable: {result['reason']}. Agent replies will continue without news."
            )
        }

    @app.post("/api/playground")
    async def playground(request: Request):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(400, "Expected a prompt object")
        text, context = payload.get("text", ""), payload.get("context", "")
        if (
            not isinstance(text, str)
            or not isinstance(context, str)
            or len(text) + len(context) > 20000
        ):
            raise HTTPException(400, "Prompt must be text, up to 20,000 characters")
        if not text.strip() and not context.strip():
            raise HTTPException(400, "Enter a prompt or reply context")
        async with request.app.state.playground_lock:
            answer = await Agent(
                request.app.state.store.settings(),
                request.app.state.client,
                request.app.state.store,
            ).run(Prompt(text, context))
        return {
            "text": answer.text,
            "messages": answer.messages or ([answer.text] if answer.text else []),
            "images": [m.data_url() for m in answer.media],
            "tool_calls": answer.tool_calls,
        }

    @app.post("/api/playground/media")
    async def playground_media(
        request: Request,
        file: Annotated[UploadFile, File()],
        prompt: Annotated[str, Form()] = "",
        context: Annotated[str, Form()] = "",
    ):
        settings = request.app.state.store.settings()
        limit = settings.max_media_mb * 1024 * 1024
        data = await file.read(limit + 1)
        await file.close()
        if len(data) > limit:
            raise HTTPException(400, "File exceeds the configured media limit")
        if len(prompt) + len(context) > 20000:
            raise HTTPException(400, "Prompt is too long")
        async with request.app.state.playground_lock:
            answer = await Agent(settings, request.app.state.client, request.app.state.store).run(
                Prompt(
                    prompt,
                    context,
                    [
                        Media(
                            file.filename or "attachment",
                            file.content_type or "application/octet-stream",
                            data,
                        )
                    ],
                )
            )
        return {
            "text": answer.text,
            "messages": answer.messages or ([answer.text] if answer.text else []),
            "images": [m.data_url() for m in answer.media],
            "tool_calls": answer.tool_calls,
        }

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
