import asyncio
import json
from datetime import datetime, timezone

import httpx
from starlette.testclient import TestClient

from src.ai.summarizer import DailySummarizer
from src.models import ContentItem, SourceType, TelegramBotConfig
from src.services.telegram_bot import (
    TelegramBotNotifier,
    TelegramRunStore,
    TelegramWorkerGateway,
    build_overview_keyboard,
    build_overview_text,
    build_telegram_run_payload,
    create_app,
)


class FakeTelegramClient:
    def __init__(self):
        self.calls = []

    async def call(self, method, payload):
        self.calls.append((method, payload))
        return {"ok": True, "result": {}}


class FakeWorkerGateway:
    def __init__(self):
        self.calls = []

    async def publish(self, payload, *, chat_id=""):
        self.calls.append({"payload": payload, "chat_id": chat_id})
        return {"ok": True, "run_id": "worker-run"}


def _item(idx: int, score: float = 8.5) -> ContentItem:
    return ContentItem(
        id=f"rss:test:{idx}",
        source_type=SourceType.RSS,
        title=f"Important item {idx}",
        url=f"https://example.com/{idx}",
        content="Body",
        author="example",
        published_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        ai_score=score,
        ai_summary=f"Summary for item {idx}",
        ai_tags=["ai", "security"],
        metadata={
            "feed_name": "Example Feed",
            "title_zh": f"重要资讯 {idx}",
            "detailed_summary_zh": f"第 {idx} 条资讯摘要。",
        },
    )


def test_build_telegram_payload_and_keyboard():
    cfg = TelegramBotConfig(page_size=3)
    payload = build_telegram_run_payload(
        config=cfg,
        important_items=[_item(i) for i in range(1, 9)],
        all_items_count=10,
        date="2026-05-30",
        lang="zh",
        summarizer=DailySummarizer(),
        summary_markdown="# Horizon Summary\n\n[Source](https://example.com)",
    )

    assert "Horizon 每日速递" in payload["overview"]
    assert "下面按评分展示全部重要资讯" in payload["overview"]
    assert payload["summary_markdown"] == "# Horizon Summary\n\n[Source](https://example.com)"
    assert len(payload["items"]) == 8
    assert "markdown" in payload["items"][0]

    text = build_overview_text(payload, page=1, page_size=3)
    assert "第 2/3 页 · 当前 4-6/8" in text
    assert '<a href="https://example.com/4">重要资讯 4</a>' in text
    assert '<a href="https://example.com/1">重要资讯 1</a>' not in text

    keyboard = build_overview_keyboard(
        "abc123",
        payload["items"],
        page=0,
        page_size=3,
        lang="zh",
    )
    assert keyboard["inline_keyboard"] == [
        [
            {"text": "第 1/3 页", "callback_data": "hzn:o:abc123:0"},
            {"text": "下一页", "callback_data": "hzn:o:abc123:1"},
        ]
    ]


def test_build_telegram_payload_caps_and_sorts_by_score():
    cfg = TelegramBotConfig(max_items=100, page_size=10)
    items = [_item(i, score=float(i % 11)) for i in range(1, 121)]

    payload = build_telegram_run_payload(
        config=cfg,
        important_items=items,
        all_items_count=140,
        date="2026-05-30",
        lang="zh",
        summarizer=DailySummarizer(),
    )

    scores = [item["score"] for item in payload["items"]]
    assert len(payload["items"]) == 100
    assert payload["important_items_count"] == 120
    assert payload["delivered_items_count"] == 100
    assert payload["max_items"] == 100
    assert scores == sorted(scores, reverse=True)
    assert "下面按评分展示前 100 条" in payload["overview"]
    assert payload["summary_markdown"].count("## [重要资讯") == 100


def test_notifier_can_publish_via_worker_without_local_bot_env(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    cfg = TelegramBotConfig(enabled=True, page_size=2)
    fake_worker = FakeWorkerGateway()
    notifier = TelegramBotNotifier(
        cfg,
        data_dir=tmp_path,
        worker_gateway=fake_worker,
    )

    asyncio.run(
        notifier.send_daily_summary(
            important_items=[_item(i) for i in range(1, 6)],
            all_items_count=9,
            date="2026-05-30",
            lang="zh",
            summarizer=DailySummarizer(),
        )
    )

    assert len(fake_worker.calls) == 1
    assert fake_worker.calls[0]["chat_id"] == ""
    payload = fake_worker.calls[0]["payload"]
    assert payload["page_size"] == 2
    assert len(payload["items"]) == 5
    assert payload["items"][0]["url"] == "https://example.com/1"


def test_worker_gateway_posts_payload_with_secret():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "run_id": "worker-run"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = TelegramWorkerGateway(
        "https://worker.example",
        ingest_secret="secret",
        http_client=client,
    )

    result = asyncio.run(
        gateway.publish({"items": [{"title": "One"}]}, chat_id="123")
    )
    asyncio.run(client.aclose())

    assert result["run_id"] == "worker-run"
    assert requests[0].url == "https://worker.example/api/runs"
    assert requests[0].headers["authorization"] == "Bearer secret"
    assert requests[0].headers["x-horizon-ingest-secret"] == "secret"
    body = json.loads(requests[0].content)
    assert body == {
        "payload": {"items": [{"title": "One"}]},
        "chat_id": "123",
    }


def test_callback_edits_message_to_paginated_overview(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret")

    cfg = TelegramBotConfig()
    store = TelegramRunStore(tmp_path)
    store.save(
        {
            "run_id": "run123",
            "language": "zh",
            "overview": "overview text",
            "page_size": 2,
            "items": [
                {
                    "index": idx,
                    "title": f"Item {idx}",
                    "score": 8.5,
                    "url": f"https://example.com/{idx}",
                    "excerpt": f"detail text {idx}",
                }
                for idx in range(1, 5)
            ],
        }
    )

    fake = FakeTelegramClient()
    app = create_app(cfg, data_dir=tmp_path, client=fake)
    client = TestClient(app)

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json={
            "callback_query": {
                "id": "cb1",
                "data": "hzn:o:run123:1",
                "message": {
                    "message_id": 55,
                    "chat": {"id": 123},
                },
            }
        },
    )

    assert response.status_code == 200
    assert fake.calls[0][0] == "editMessageText"
    assert fake.calls[0][1]["parse_mode"] == "HTML"
    assert '<a href="https://example.com/3">Item 3</a>' in fake.calls[0][1]["text"]
    assert '<a href="https://example.com/1">Item 1</a>' not in fake.calls[0][1]["text"]
    assert "detail text 3" in fake.calls[0][1]["text"]
    assert fake.calls[0][1]["reply_markup"]["inline_keyboard"] == [
        [
            {"text": "上一页", "callback_data": "hzn:o:run123:0"},
            {"text": "第 2/2 页", "callback_data": "hzn:o:run123:1"},
        ]
    ]


def test_callback_rejects_wrong_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret")

    cfg = TelegramBotConfig()
    fake = FakeTelegramClient()
    app = create_app(cfg, data_dir=tmp_path, client=fake)
    client = TestClient(app)

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        json={"callback_query": {}},
    )

    assert response.status_code == 403
    assert fake.calls == []
