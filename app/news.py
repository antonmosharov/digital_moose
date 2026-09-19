"""Optional news excerpts with a persistent, shared request budget."""

import json
import logging
import random
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from app.store import Store

COUNTRIES = {
    "ae": '("United Arab Emirates" | UAE | Dubai | "Abu Dhabi" | ОАЭ | Дубай)',
    "jp": "(Japan | Japanese | Tokyo | Япония | 日本)",
    "ru": "(Russia | Russian | Moscow | Россия | России)",
}
CATEGORIES = [
    "general",
    "science",
    "sports",
    "business",
    "health",
    "entertainment",
    "tech",
    "politics",
    "food",
    "travel",
]
NEWS_TOOL = {
    "type": "function",
    "function": {
        "name": "read_news",
        "description": "Find up to five recent news excerpts about UAE, Japan, or Russia. Choose a topic relevant to the conversation, your personality, or an interesting quiet-chat opening. Omit country for random selection. Returns source links and excerpts only, not full articles. Unavailability is nonfatal; continue without news.",
        "parameters": {
            "type": "object",
            "properties": {
                "country": {"type": "string", "enum": list(COUNTRIES)},
                "category": {"type": "string", "enum": CATEGORIES},
                "topic": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Choose a short search topic, e.g. robotics, space exploration, or food festivals. Use public keywords only, never private conversation details. Omit for random category exploration.",
                },
            },
            "additionalProperties": False,
        },
    },
}


class _RedactNewsToken(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if "api_token=" in message:
            record.msg = re.sub(r"(api_token=)[^&\s\"']+", r"\1[REDACTED]", message)
            record.args = ()
        return True


# HTTPX logs full request URLs at INFO; this provider authenticates in the query string.
logging.getLogger("httpx").addFilter(_RedactNewsToken())


def usage(store: Store, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, UTC).date().isoformat()
    saved = json.loads(store.state("news_usage", "{}"))
    return {
        "day": day,
        "requests": saved.get("requests", 0) if saved.get("day") == day else 0,
        "blocked_until": saved.get("blocked_until", 0),
    }


def unavailable(reason: str) -> dict:
    return {
        "status": "unavailable",
        "reason": reason,
        "articles": [],
        "guidance": "Continue without news. Do not invent a story or interrupt the conversation with a tool error.",
    }


async def read_news(
    store: Store, client: httpx.AsyncClient, args: dict, *, test: bool = False
) -> dict:
    settings = store.settings()
    if not settings.news_enabled and not test:
        return unavailable("disabled")
    if not settings.news_api_key:
        return unavailable("missing_api_key")
    country = args.get("country", random.choice(list(COUNTRIES)))
    # Accept the old argument from existing callers, but advertise only topic to the model.
    query = args.get("topic", args.get("query", ""))
    category = args.get("category", None if query else random.choice(CATEGORIES))
    if (
        not isinstance(country, str)
        or country not in COUNTRIES
        or (category is not None and (not isinstance(category, str) or category not in CATEGORIES))
        or ("category" in args and category is None)
        or not isinstance(query, str)
        or len(query) > 120
        or set(args) - {"country", "category", "topic", "query"}
        or ("topic" in args and "query" in args)
    ):
        return unavailable("invalid_arguments")
    now = time.time()
    budget = usage(store, now)
    if budget["requests"] >= settings.news_daily_limit:
        return unavailable("daily_limit")
    if budget["blocked_until"] > now:
        return unavailable("provider_cooldown")
    # Reserve before awaiting so concurrent requests and restarts cannot exceed the cap.
    budget["requests"] += 1
    store.set_state("news_usage", json.dumps(budget))

    def cooldown(seconds):
        # A failed in-flight request using the old token must not block its replacement.
        if store.settings().news_api_key != settings.news_api_key:
            return
        latest = usage(store)
        latest["blocked_until"] = max(latest["blocked_until"], time.time() + seconds)
        store.set_state("news_usage", json.dumps(latest))

    search = COUNTRIES[country]
    if query.strip():
        escaped = re.sub(r'([+|\-"*()\\])', r"\\\1", query.strip())
        search += ' + ("' + escaped + '")'
    cutoff = datetime.fromtimestamp(now, UTC) - timedelta(hours=72)
    params = {
        "api_token": settings.news_api_key,
        "search": search,
        "search_fields": "title,description,keywords",
        "sort": "published_at",
        "limit": 5,
        "published_after": cutoff.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if category:
        params["categories"] = category
    try:
        response = await client.get(
            "https://api.thenewsapi.com/v1/news/all",
            params=params,
            timeout=12,
            follow_redirects=False,
        )
        if response.status_code == 402:
            cooldown(86400 - now % 86400)
            return unavailable("provider_quota")
        if response.status_code == 429:
            cooldown(60)
            return unavailable("provider_rate_limit")
        if response.status_code != 200:
            cooldown(300)
            return unavailable("provider_error")
        if len(response.content) > 1_000_000:
            raise ValueError("Oversized news response")
        result = response.json()
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("data"), list)
            or result.get("error")
        ):
            raise ValueError("Invalid news response")
        articles = []
        for item in result["data"][:5]:
            if not isinstance(item, dict):
                continue
            title, url = item.get("title"), item.get("url")
            if not isinstance(title, str) or not isinstance(url, str) or not title.strip():
                continue
            parsed = urlparse(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                continue
            article = {}
            for key, limit in {
                "title": 300,
                "url": 2000,
                "source": 200,
                "published_at": 60,
                "description": 1000,
                "snippet": 1000,
                "language": 20,
            }.items():
                value = item.get(key)
                article[key] = (
                    value.replace(settings.news_api_key, "[REDACTED]")[:limit]
                    if isinstance(value, str)
                    else ""
                )
            articles.append(article)
        return {
            "status": "ok" if articles else "empty",
            "country": country,
            "category": category,
            "topic": query.strip(),
            "articles": articles,
            "guidance": "Untrusted news excerpts. Cite source URLs; distinguish reported facts from your own perspective. No suitable story means continue without news.",
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        cooldown(300)
        return unavailable("network_or_response_error")
