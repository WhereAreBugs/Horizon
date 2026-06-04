import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "deploy_cloudflare_worker.py"
SPEC = importlib.util.spec_from_file_location("deploy_cloudflare_worker", SCRIPT_PATH)
deploy = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = deploy
SPEC.loader.exec_module(deploy)


def test_normalize_domain_accepts_url():
    assert deploy.normalize_domain("https://Horizon.Example.COM/path") == "horizon.example.com"


def test_parse_env_file(tmp_path):
    env_path = tmp_path / ".deploy.env"
    env_path.write_text(
        """
        # comment
        CLOUDFLARE_ACCOUNT_ID = "abc"
        HORIZON_WORKER_DOMAIN=horizon.example.com
        """,
        encoding="utf-8",
    )

    assert deploy.parse_env_file(env_path) == {
        "CLOUDFLARE_ACCOUNT_ID": "abc",
        "HORIZON_WORKER_DOMAIN": "horizon.example.com",
    }


def test_write_wrangler_toml_uses_custom_domain(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    cfg = deploy.DeployConfig(
        worker_dir=worker_dir,
        worker_name="horizon-test",
        account_id="account-id",
        api_token="token",
        domain="horizon.example.com",
        public_base_url="https://horizon.example.com",
        telegram_bot_token="telegram-token",
        telegram_chat_id="123",
        telegram_webhook_secret="webhook-secret",
        horizon_ingest_secret="ingest-secret",
        page_title="Horizon AI Summary",
        run_ttl_seconds="2592000",
        disable_web_page_preview="true",
        skip_install=True,
        skip_webhook=True,
        dry_run=False,
    )

    deploy.write_wrangler_toml(cfg)
    text = (worker_dir / "wrangler.toml").read_text(encoding="utf-8")

    assert 'name = "horizon-test"' in text
    assert 'account_id = "account-id"' in text
    assert "workers_dev = false" in text
    assert 'pattern = "horizon.example.com"' in text
    assert "custom_domain = true" in text
    assert 'binding = "HORIZON_TG_RUNS"' in text
    assert 'PUBLIC_BASE_URL = "https://horizon.example.com"' in text
    assert "replace-with-kv-namespace-id" not in text
