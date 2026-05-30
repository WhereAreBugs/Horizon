import json

import pytest
from starlette.testclient import TestClient

from src.models import DaemonConfig, TelegramBotConfig
from src.services.daemon import HorizonDaemonScheduler, create_daemon_app
from src.storage.manager import StorageManager


def test_daemon_config_validates_mode():
    with pytest.raises(ValueError):
        DaemonConfig(mode="cron")


def test_daemon_config_validates_time():
    with pytest.raises(ValueError):
        DaemonConfig(time="25:00")


def test_interval_delay_seconds(tmp_path):
    scheduler = HorizonDaemonScheduler(
        storage=StorageManager(data_dir=str(tmp_path)),
        daemon_config=DaemonConfig(mode="interval", interval_hours=1.5),
    )

    assert scheduler._next_delay_seconds() == 5400


def test_daemon_app_healthz_without_scheduler(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": "1.0",
                "ai": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "api_key_env": "OPENAI_API_KEY",
                },
                "sources": {"hackernews": {"enabled": True}},
                "filtering": {"ai_score_threshold": 6.0, "time_window_hours": 24},
                "telegram_bot": TelegramBotConfig(enabled=False).model_dump(),
                "daemon": DaemonConfig(enabled=False).model_dump(),
            }
        ),
        encoding="utf-8",
    )
    storage = StorageManager(data_dir=str(tmp_path))
    config = storage.load_config()

    app = create_daemon_app(config, storage)
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
