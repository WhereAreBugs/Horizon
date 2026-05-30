from datetime import datetime, timezone

from starlette.testclient import TestClient

from src.ai.summarizer import DailySummarizer
from src.models import ContentItem, SourceType, TelegramBotConfig
from src.services.telegram_bot import (
    TelegramRunStore,
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


def _item(idx: int) -> ContentItem:
    return ContentItem(
        id=f"rss:test:{idx}",
        source_type=SourceType.RSS,
        title=f"Important item {idx}",
        url=f"https://example.com/{idx}",
        content="Body",
        author="example",
        published_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        ai_score=8.5,
        ai_summary=f"Summary for item {idx}",
        ai_tags=["ai", "security"],
        metadata={
            "feed_name": "Example Feed",
            "title_zh": f"重要资讯 {idx}",
            "detailed_summary_zh": f"第 {idx} 条资讯摘要。",
        },
    )


def test_build_telegram_payload_and_keyboard():
    cfg = TelegramBotConfig(max_items=7, page_size=3)
    payload = build_telegram_run_payload(
        config=cfg,
        important_items=[_item(i) for i in range(1, 9)],
        all_items_count=10,
        date="2026-05-30",
        lang="zh",
        summarizer=DailySummarizer(),
    )

    assert "Horizon 每日速递" in payload["overview"]
    assert "本次收录前 7 条" in payload["overview"]
    assert len(payload["items"]) == 7

    text = build_overview_text(payload, page=1, page_size=3)
    assert "第 2/3 页 · 当前 4-6/7" in text

    keyboard = build_overview_keyboard(
        "abc123",
        payload["items"],
        page=0,
        page_size=3,
        lang="zh",
    )
    assert keyboard["inline_keyboard"][0][0]["callback_data"] == "hzn:i:abc123:1:0"
    assert keyboard["inline_keyboard"][2][0]["callback_data"] == "hzn:i:abc123:3:0"
    assert keyboard["inline_keyboard"][3][1]["callback_data"] == "hzn:o:abc123:1"


def test_callback_edits_message_to_detail_and_back(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret")

    cfg = TelegramBotConfig()
    store = TelegramRunStore(tmp_path)
    store.save(
        {
            "run_id": "run123",
            "language": "zh",
            "overview": "overview text",
            "page_size": 5,
            "items": [
                {
                    "index": 1,
                    "title": "Item one",
                    "score": 8.5,
                    "url": "https://example.com/1",
                    "text": "detail text",
                }
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
                "data": "hzn:i:run123:1:0",
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
    assert '<a href="https://example.com/1">Item one</a>' in fake.calls[0][1]["text"]
    assert "detail text" in fake.calls[0][1]["text"]
    assert fake.calls[0][1]["reply_markup"]["inline_keyboard"][1][0][
        "callback_data"
    ] == "hzn:o:run123:0"
    assert fake.calls[0][1]["reply_markup"]["inline_keyboard"][0][0][
        "url"
    ] == "https://example.com/1"

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json={
            "callback_query": {
                "id": "cb2",
                "data": "hzn:o:run123:0",
                "message": {
                    "message_id": 55,
                    "chat": {"id": 123},
                },
            }
        },
    )

    assert response.status_code == 200
    assert fake.calls[2][0] == "editMessageText"
    assert fake.calls[2][1]["text"] == "overview text\n\n第 1/1 页 · 当前 1-1/1"


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
