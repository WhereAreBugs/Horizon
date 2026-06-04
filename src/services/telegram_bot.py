"""Interactive Telegram bot delivery and callback service."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from rich.console import Console

from ..ai.markdown_utils import clean_app_summary_markdown
from ..ai.summarizer import DailySummarizer
from ..models import ContentItem, TelegramBotConfig
from ..storage.manager import ConfigError, StorageManager

logger = logging.getLogger(__name__)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CALLBACK_RE = re.compile(
    r"^hzn:(?P<action>[io]):(?P<run>[A-Za-z0-9_-]+)"
    r"(?::(?P<idx>\d+))?(?::(?P<page>\d+))?$"
)


class TelegramBotError(RuntimeError):
    """Raised when Telegram Bot API returns an error."""


class TelegramRunStore:
    """File-backed storage for interactive Telegram message state."""

    def __init__(self, data_dir: str | Path = "data"):
        self.runs_dir = Path(data_dir) / "telegram-bot-runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def save(self, payload: dict[str, Any]) -> str:
        run_id = payload.get("run_id") or secrets.token_urlsafe(9)
        payload["run_id"] = run_id
        payload["created_at"] = payload.get("created_at") or datetime.now(
            timezone.utc
        ).isoformat()

        path = self._path_for(run_id)
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tmp_path.replace(path)
        return run_id

    def load(self, run_id: str) -> Optional[dict[str, Any]]:
        path = self._path_for(run_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            logger.warning("Invalid Telegram run payload: %s", path)
            return None
        return data if isinstance(data, dict) else None

    def _path_for(self, run_id: str) -> Path:
        if not _RUN_ID_RE.match(run_id):
            raise ValueError("invalid Telegram run id")
        return self.runs_dir / f"{run_id}.json"


class TelegramBotClient:
    """Small async Telegram Bot API client."""

    def __init__(self, token: str, http_client: httpx.AsyncClient | None = None):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._client = http_client

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            return await self._post(self._client, method, payload)

        async with httpx.AsyncClient(timeout=30.0) as client:
            return await self._post(client, method, payload)

    async def _post(
        self,
        client: httpx.AsyncClient,
        method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await client.post(f"{self.base_url}/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            description = data.get("description") or "Telegram API error"
            raise TelegramBotError(str(description))
        return data


class TelegramWorkerGateway:
    """Publishes Telegram run payloads to a Cloudflare Worker gateway."""

    def __init__(
        self,
        base_url: str,
        ingest_secret: str = "",
        http_client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.ingest_secret = ingest_secret
        self._client = http_client

    async def publish(
        self,
        payload: dict[str, Any],
        *,
        chat_id: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"payload": payload}
        if chat_id:
            body["chat_id"] = chat_id

        headers = {"Content-Type": "application/json"}
        if self.ingest_secret:
            headers["Authorization"] = f"Bearer {self.ingest_secret}"
            headers["X-Horizon-Ingest-Secret"] = self.ingest_secret

        if self._client is not None:
            return await self._post(self._client, body, headers)

        async with httpx.AsyncClient(timeout=30.0) as client:
            return await self._post(client, body, headers)

    async def _post(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        if not self.base_url:
            raise TelegramBotError("Cloudflare Worker URL is empty")

        response = await client.post(
            f"{self.base_url}/api/runs",
            json=body,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("ok") is False:
            raise TelegramBotError(str(data.get("error") or "Worker gateway error"))
        return data


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n\n..."
    return text[: max(0, limit - len(marker))].rstrip() + marker


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _page_count(items: list[Any], page_size: int) -> int:
    page_size = max(1, page_size)
    return max(1, (len(items) + page_size - 1) // page_size)


def _clamp_page(page: int, items: list[Any], page_size: int) -> int:
    return min(max(page, 0), _page_count(items, page_size) - 1)


def _page_bounds(page: int, items: list[Any], page_size: int) -> tuple[int, int]:
    page = _clamp_page(page, items, page_size)
    page_size = max(1, page_size)
    start = page * page_size
    return start, min(start + page_size, len(items))


def _plain_markdown(markdown: str) -> str:
    """Convert Horizon Markdown into Telegram-safe plain text.

    We intentionally do not set Telegram parse_mode. Horizon summaries can
    contain arbitrary source titles and URLs, and Telegram MarkdownV2 is
    strict enough that unescaped characters can reject the whole message.
    """
    text = clean_app_summary_markdown(markdown)
    text = re.sub(r'<a id="[^"]+"></a>\n?', "", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1\n\2", text)
    text = re.sub(r"`#?([^`]+)`", r"#\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = text.replace("---", "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _item_title(item: ContentItem, language: str) -> str:
    return str(item.metadata.get(f"title_{language}") or item.title)


def _item_excerpt(item: ContentItem, language: str) -> str:
    meta = item.metadata
    parts = [
        meta.get(f"detailed_summary_{language}")
        or meta.get("detailed_summary")
        or item.ai_summary
        or item.content
        or ""
    ]

    background = meta.get(f"background_{language}") or meta.get("background") or ""
    if background:
        label = "背景" if language == "zh" else "Background"
        parts.append(f"{label}: {background}")

    discussion = (
        meta.get(f"community_discussion_{language}")
        or meta.get("community_discussion")
        or ""
    )
    if discussion:
        label = "讨论" if language == "zh" else "Discussion"
        parts.append(f"{label}: {discussion}")

    if item.ai_tags:
        label = "标签" if language == "zh" else "Tags"
        parts.append(f"{label}: " + ", ".join(f"#{tag}" for tag in item.ai_tags))

    text = "\n\n".join(str(part).strip() for part in parts if str(part).strip())
    return _plain_markdown(text)


def _score_sort_value(item: ContentItem) -> float:
    return float(item.ai_score) if item.ai_score is not None else -1.0


def _build_payload_summary_markdown(
    *,
    header: str,
    intro: str,
    hint: str,
    details: list[dict[str, Any]],
) -> str:
    body = "\n\n".join(str(item.get("markdown") or "").strip() for item in details)
    return f"# {header}\n\n> {intro}\n\n{hint}\n\n---\n\n{body}".strip()


def build_telegram_run_payload(
    *,
    config: TelegramBotConfig,
    important_items: list[ContentItem],
    all_items_count: int,
    date: str,
    lang: str,
    summarizer: DailySummarizer,
    summary_markdown: str | None = None,
) -> dict[str, Any]:
    """Build persisted overview/detail payload for Telegram callbacks."""
    selected = sorted(important_items, key=_score_sort_value, reverse=True)[
        : config.max_items
    ]
    header = (
        f"Horizon 每日速递 - {date}"
        if lang == "zh"
        else f"Horizon Daily - {date}"
    )
    if lang == "zh":
        intro = (
            f"从 {all_items_count} 条内容中筛选出 {len(important_items)} 条重要资讯。"
        )
        if len(selected) < len(important_items):
            hint = (
                f"下面按评分展示前 {len(selected)} 条；"
                "点击标题可直接打开来源。"
            )
        else:
            hint = "下面按评分展示全部重要资讯；点击标题可直接打开来源。"
    else:
        intro = (
            f"Selected {len(important_items)} important items from "
            f"{all_items_count} fetched items."
        )
        if len(selected) < len(important_items):
            hint = (
                f"Showing the top {len(selected)} by score across pages. "
                "Tap a title to open the source."
            )
        else:
            hint = (
                "All selected items are shown by score across pages. "
                "Tap a title to open the source."
            )

    lines = [header, "", intro, "", hint]

    overview = _truncate("\n".join(lines).strip(), config.overview_limit)

    details: list[dict[str, Any]] = []
    for i, item in enumerate(selected, start=1):
        detail_text = summarizer.generate_webhook_item(
            item,
            language=lang,
            index=i,
            total=len(selected),
        )
        details.append(
            {
                "index": i,
                "title": _item_title(item, lang),
                "score": item.ai_score or "",
                "url": str(item.url),
                "text": _truncate(_plain_markdown(detail_text), config.item_limit),
                "markdown": detail_text,
                "excerpt": _truncate(_item_excerpt(item, lang), config.item_limit),
            }
        )

    payload_summary_markdown = (
        summary_markdown
        if summary_markdown and len(selected) == len(important_items)
        else _build_payload_summary_markdown(
            header=header,
            intro=intro,
            hint=hint,
            details=details,
        )
    )

    return {
        "date": date,
        "language": lang,
        "all_items_count": all_items_count,
        "important_items_count": len(important_items),
        "delivered_items_count": len(selected),
        "max_items": config.max_items,
        "summary_markdown": payload_summary_markdown,
        "page_size": config.page_size,
        "overview_limit": config.overview_limit,
        "overview": overview,
        "items": details,
    }


def _item_link(item: dict[str, Any]) -> str:
    title = escape(str(item.get("title") or "Untitled"))
    url = str(item.get("url") or "")
    if url.startswith(("http://", "https://")):
        return f'<a href="{escape(url, quote=True)}">{title}</a>'
    return title


def _page_item_block(item: dict[str, Any], excerpt_limit: int) -> str:
    index = int(item.get("index") or 0)
    score = item.get("score") or "?"
    heading = f"{index}. {_item_link(item)} ⭐ {escape(str(score))}/10"
    excerpt = _truncate(str(item.get("excerpt") or item.get("text") or ""), excerpt_limit)
    if not excerpt:
        return heading
    return f"{heading}\n{escape(excerpt)}"


def _build_overview_page_html(
    *,
    overview: str,
    status: str,
    page_items: list[dict[str, Any]],
    limit: int,
) -> str:
    header = f"{escape(overview)}\n\n{escape(status)}"
    if not page_items:
        return _truncate(header, limit)

    per_item_limit = max(120, (limit - len(header) - 2 * len(page_items)) // len(page_items))
    while per_item_limit >= 80:
        blocks = [_page_item_block(item, per_item_limit) for item in page_items]
        text = "\n\n".join([header, *blocks])
        if len(text) <= limit:
            return text
        per_item_limit -= 80

    blocks = [_page_item_block(item, 0).split("\n", 1)[0] for item in page_items]
    text = "\n\n".join([header, *blocks])
    return text if len(text) <= limit else "\n\n".join([header, *blocks[:1]])


def build_overview_text(
    payload: dict[str, Any],
    *,
    page: int = 0,
    page_size: int | None = None,
    limit: int | None = None,
) -> str:
    """Build one HTML overview page with item links in the message body."""
    items = payload.get("items") or []
    overview = str(payload.get("overview") or "")
    if not items:
        return escape(overview)

    page_size = max(1, _safe_int(page_size or payload.get("page_size"), 5))
    page = _clamp_page(page, items, page_size)
    start, end = _page_bounds(page, items, page_size)
    page_total = _page_count(items, page_size)
    lang = str(payload.get("language") or "zh")
    if lang == "zh":
        status = f"第 {page + 1}/{page_total} 页 · 当前 {start + 1}-{end}/{len(items)}"
    else:
        status = f"Page {page + 1}/{page_total} · Showing {start + 1}-{end}/{len(items)}"
    return _build_overview_page_html(
        overview=overview,
        status=status,
        page_items=items[start:end],
        limit=max(1, _safe_int(limit or payload.get("overview_limit"), 3600)),
    )


def build_overview_keyboard(
    run_id: str,
    items: list[dict[str, Any]],
    *,
    page: int = 0,
    page_size: int = 5,
    lang: str = "zh",
) -> dict[str, Any]:
    rows = []
    page_size = max(1, page_size)
    page = _clamp_page(page, items, page_size)

    page_total = _page_count(items, page_size)
    if page_total > 1:
        page_text = f"第 {page + 1}/{page_total} 页" if lang == "zh" else f"Page {page + 1}/{page_total}"
        prev_text = "上一页" if lang == "zh" else "Prev"
        next_text = "下一页" if lang == "zh" else "Next"
        nav = []
        if page > 0:
            nav.append({"text": prev_text, "callback_data": f"hzn:o:{run_id}:{page - 1}"})
        nav.append({"text": page_text, "callback_data": f"hzn:o:{run_id}:{page}"})
        if page < page_total - 1:
            nav.append({"text": next_text, "callback_data": f"hzn:o:{run_id}:{page + 1}"})
        rows.append(nav)

    return {"inline_keyboard": rows}


def build_detail_text(item: dict[str, Any]) -> str:
    """Build HTML detail text with a clickable source title."""
    title = str(item.get("title") or "Untitled")
    url = str(item.get("url") or "")
    body = escape(str(item.get("text") or ""))
    if url.startswith(("http://", "https://")):
        heading = f'<a href="{escape(url, quote=True)}">{escape(title)}</a>'
    else:
        heading = escape(title)
    return f"{heading}\n\n{body}".strip()


def build_detail_keyboard(
    *,
    run_id: str,
    item: dict[str, Any],
    lang: str,
    page: int = 0,
) -> dict[str, Any]:
    back_text = "返回列表" if lang == "zh" else "Back to list"
    open_text = "打开原文" if lang == "zh" else "Open source"
    rows = []
    url = str(item.get("url") or "")
    if url.startswith(("http://", "https://")):
        rows.append([{"text": open_text, "url": url}])
    rows.append([{"text": back_text, "callback_data": f"hzn:o:{run_id}:{page}"}])
    return {"inline_keyboard": rows}


class TelegramBotNotifier:
    """Sends compact interactive Telegram overview messages."""

    def __init__(
        self,
        config: TelegramBotConfig,
        data_dir: str | Path = "data",
        console: Console | None = None,
        client: TelegramBotClient | None = None,
        worker_gateway: TelegramWorkerGateway | None = None,
    ):
        self.config = config
        self.store = TelegramRunStore(data_dir)
        self.console = console or Console()
        token = _env(config.bot_token_env)
        self.client = client or (TelegramBotClient(token) if token else None)
        worker_url = _env(config.worker_url_env)
        self.worker_gateway = worker_gateway or (
            TelegramWorkerGateway(worker_url, _env(config.worker_ingest_secret_env))
            if worker_url
            else None
        )

    async def send_daily_summary(
        self,
        *,
        important_items: list[ContentItem],
        all_items_count: int,
        date: str,
        lang: str,
        summarizer: DailySummarizer,
        summary: str | None = None,
    ) -> None:
        if not self.config.enabled:
            return

        if self.config.languages and lang not in self.config.languages:
            self.console.print(
                f"🔕 Skipping {lang.upper()} Telegram bot notification "
                f"(filtered by telegram_bot.languages)"
            )
            return

        payload = build_telegram_run_payload(
            config=self.config,
            important_items=important_items,
            all_items_count=all_items_count,
            date=date,
            lang=lang,
            summarizer=summarizer,
            summary_markdown=summary,
        )

        chat_id = _env(self.config.chat_id_env)
        if self.worker_gateway is not None:
            self.console.print(
                f"☁️ Sending {lang.upper()} Telegram overview via Cloudflare Worker..."
            )
            await self.worker_gateway.publish(payload, chat_id=chat_id)
            return

        if not chat_id:
            self.console.print(
                f"[yellow]Telegram bot enabled but env var "
                f"'{self.config.chat_id_env}' is not set. Skipping.[/yellow]"
            )
            return
        if self.client is None:
            self.console.print(
                f"[yellow]Telegram bot enabled but env var "
                f"'{self.config.bot_token_env}' is not set. Skipping.[/yellow]"
            )
            return

        run_id = self.store.save(payload)
        keyboard = build_overview_keyboard(
            run_id,
            payload["items"],
            page=0,
            page_size=self.config.page_size,
            lang=lang,
        )

        self.console.print(f"🤖 Sending {lang.upper()} Telegram bot overview...")
        message = {
            "chat_id": chat_id,
            "text": build_overview_text(payload, page=0, page_size=self.config.page_size),
            "parse_mode": "HTML",
            "disable_web_page_preview": self.config.disable_web_page_preview,
        }
        if keyboard["inline_keyboard"]:
            message["reply_markup"] = keyboard
        await self.client.call("sendMessage", message)


def _is_allowed_chat(config: TelegramBotConfig, chat_id: Any) -> bool:
    expected = _env(config.chat_id_env)
    return not expected or str(chat_id) == expected


async def _json_response(payload: dict[str, Any], status_code: int = 200):
    from starlette.responses import JSONResponse

    return JSONResponse(payload, status_code=status_code)


def create_app(
    config: TelegramBotConfig,
    *,
    data_dir: str | Path = "data",
    client: TelegramBotClient | None = None,
    lifespan: Any = None,
):
    """Create the ASGI app used by the Telegram callback service."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.routing import Route

    token = _env(config.bot_token_env)
    bot_client = client or (TelegramBotClient(token) if token else None)
    store = TelegramRunStore(data_dir)

    async def health(_request: Request):
        return await _json_response({"ok": True})

    async def webhook(request: Request):
        secret = _env(config.secret_token_env)
        if secret:
            got = request.headers.get("x-telegram-bot-api-secret-token", "")
            if got != secret:
                return await _json_response({"ok": False, "error": "forbidden"}, 403)

        if bot_client is None:
            return await _json_response(
                {"ok": False, "error": "missing bot token"},
                500,
            )

        update = await request.json()
        callback = update.get("callback_query") or {}
        callback_id = callback.get("id")
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        message_id = message.get("message_id")

        if not _is_allowed_chat(config, chat.get("id")):
            if callback_id:
                await bot_client.call(
                    "answerCallbackQuery",
                    {"callback_query_id": callback_id, "text": "Unauthorized"},
                )
            return await _json_response({"ok": False, "error": "unauthorized"}, 403)

        match = _CALLBACK_RE.match(data)
        if not match:
            if callback_id:
                await bot_client.call(
                    "answerCallbackQuery",
                    {"callback_query_id": callback_id, "text": "Unsupported action"},
                )
            return await _json_response({"ok": True})

        run_id = match.group("run")
        payload = store.load(run_id)
        if payload is None:
            if callback_id:
                await bot_client.call(
                    "answerCallbackQuery",
                    {"callback_query_id": callback_id, "text": "Expired"},
                )
            return await _json_response({"ok": True})

        lang = str(payload.get("language") or "zh")
        action = match.group("action")
        page_size = max(1, _safe_int(payload.get("page_size") or config.page_size, 5))
        if action == "o":
            items = payload.get("items") or []
            page = _clamp_page(_safe_int(match.group("idx"), 0), items, page_size)
            text = build_overview_text(payload, page=page, page_size=page_size)
            reply_markup = build_overview_keyboard(
                run_id,
                items,
                page=page,
                page_size=page_size,
                lang=lang,
            )
            parse_mode = "HTML"
        else:
            idx = int(match.group("idx") or 0)
            page = _safe_int(match.group("page"), 0)
            items = payload.get("items") or []
            item = next((i for i in items if int(i.get("index", 0)) == idx), None)
            if item is None:
                if callback_id:
                    await bot_client.call(
                        "answerCallbackQuery",
                        {"callback_query_id": callback_id, "text": "Item not found"},
                    )
                return await _json_response({"ok": True})
            page = _clamp_page(page, items, page_size)
            text = build_detail_text(item)
            reply_markup = build_detail_keyboard(
                run_id=run_id,
                item=item,
                lang=lang,
                page=page,
            )
            parse_mode = "HTML"

        edit_payload = {
            "chat_id": chat.get("id"),
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": config.disable_web_page_preview,
            "reply_markup": reply_markup,
        }
        if parse_mode:
            edit_payload["parse_mode"] = parse_mode
        await bot_client.call("editMessageText", edit_payload)
        if callback_id:
            await bot_client.call(
                "answerCallbackQuery",
                {"callback_query_id": callback_id},
            )
        return await _json_response({"ok": True})

    return Starlette(
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route(config.webhook_path, webhook, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def _load_telegram_bot_config() -> TelegramBotConfig:
    storage = StorageManager(data_dir="data")
    try:
        config = storage.load_config()
    except FileNotFoundError as exc:
        raise SystemExit("Configuration file not found. Create data/config.json.") from exc
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    if not config.telegram_bot:
        raise SystemExit("telegram_bot config is missing from data/config.json")
    return config.telegram_bot


def _webhook_url(config: TelegramBotConfig) -> str:
    base_url = _env(config.public_base_url_env).rstrip("/")
    if not base_url:
        raise SystemExit(
            f"Missing public base URL env var: {config.public_base_url_env}"
        )
    return base_url + config.webhook_path


async def _set_webhook(config: TelegramBotConfig, drop_pending_updates: bool) -> None:
    token = _env(config.bot_token_env)
    if not token:
        raise SystemExit(f"Missing bot token env var: {config.bot_token_env}")
    client = TelegramBotClient(token)
    payload: dict[str, Any] = {
        "url": _webhook_url(config),
        "allowed_updates": ["callback_query"],
        "drop_pending_updates": drop_pending_updates,
    }
    secret = _env(config.secret_token_env)
    if secret:
        payload["secret_token"] = secret
    result = await client.call("setWebhook", payload)
    Console().print(result.get("description") or "Webhook set")


async def _delete_webhook(config: TelegramBotConfig) -> None:
    token = _env(config.bot_token_env)
    if not token:
        raise SystemExit(f"Missing bot token env var: {config.bot_token_env}")
    client = TelegramBotClient(token)
    result = await client.call("deleteWebhook", {"drop_pending_updates": False})
    Console().print(result.get("description") or "Webhook deleted")


async def _webhook_info(config: TelegramBotConfig) -> None:
    token = _env(config.bot_token_env)
    if not token:
        raise SystemExit(f"Missing bot token env var: {config.bot_token_env}")
    client = TelegramBotClient(token)
    result = await client.call("getWebhookInfo", {})
    Console().print_json(json.dumps(result.get("result", {}), ensure_ascii=False))


def main() -> None:
    """CLI entry point for the Telegram bot callback service."""
    parser = argparse.ArgumentParser(description="Run Horizon Telegram bot service")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="Run the local Telegram callback HTTP service")

    set_parser = sub.add_parser("set-webhook", help="Register Telegram webhook URL")
    set_parser.add_argument(
        "--drop-pending-updates",
        action="store_true",
        help="Ask Telegram to discard pending updates while setting the webhook.",
    )

    sub.add_parser("delete-webhook", help="Delete Telegram webhook registration")
    sub.add_parser("info", help="Show Telegram webhook information")
    args = parser.parse_args()

    load_dotenv()
    config = _load_telegram_bot_config()

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            create_app(config, data_dir="data"),
            host=config.host,
            port=config.port,
            proxy_headers=config.proxy_headers,
            forwarded_allow_ips=config.forwarded_allow_ips,
        )
    elif args.command == "set-webhook":
        asyncio.run(_set_webhook(config, args.drop_pending_updates))
    elif args.command == "delete-webhook":
        asyncio.run(_delete_webhook(config))
    elif args.command == "info":
        asyncio.run(_webhook_info(config))


if __name__ == "__main__":
    main()
