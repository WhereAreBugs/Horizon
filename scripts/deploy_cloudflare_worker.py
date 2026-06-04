#!/usr/bin/env python3
"""Deploy the Horizon Telegram Cloudflare Worker gateway.

The script wraps Wrangler for the pieces Wrangler already handles well:
deployment, custom-domain binding, KV provisioning, and Worker secrets.
It avoids putting Cloudflare or Telegram tokens into committed files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKER_DIR = REPO_ROOT / "workers" / "telegram-bot-server"
DEFAULT_WORKER_NAME = "horizon-telegram-bot-server"
KV_BINDING = "HORIZON_TG_RUNS"
PLACEHOLDER_IDS = {
    "",
    "replace-with-kv-namespace-id",
    "replace-with-preview-kv-namespace-id",
}


@dataclass(frozen=True)
class DeployConfig:
    worker_dir: Path
    worker_name: str
    account_id: str
    api_token: str
    domain: str
    public_base_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_webhook_secret: str
    horizon_ingest_secret: str
    page_title: str
    run_ttl_seconds: str
    disable_web_page_preview: str
    skip_install: bool
    skip_webhook: bool
    dry_run: bool


def main() -> None:
    args = parse_args()
    env = load_env_files(args.env_file)
    cfg = build_config(args, env)

    write_wrangler_toml(cfg)
    run_npm_install(cfg)
    run_typecheck(cfg)
    run_wrangler_deploy(cfg)
    put_worker_secrets(cfg)

    webhook_url = f"{cfg.public_base_url}/telegram/webhook"
    if not cfg.skip_webhook:
        set_telegram_webhook(cfg, webhook_url)

    print("\nDeployment helper finished.")
    print(f"Worker URL: {cfg.public_base_url}")
    print(f"Telegram webhook: {webhook_url}")
    print("\nUse these Horizon container env vars:")
    print(f"TELEGRAM_WORKER_URL={cfg.public_base_url}")
    print(f"TELEGRAM_WORKER_INGEST_SECRET={cfg.horizon_ingest_secret}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy workers/telegram-bot-server to Cloudflare."
    )
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        help=(
            "Load KEY=VALUE pairs before reading env vars. Can be passed more "
            "than once. Existing process env wins."
        ),
    )
    parser.add_argument("--worker-dir", default=str(DEFAULT_WORKER_DIR))
    parser.add_argument("--worker-name", default=env_default("WORKER_NAME", DEFAULT_WORKER_NAME))
    parser.add_argument("--account-id", default=env_default("CLOUDFLARE_ACCOUNT_ID"))
    parser.add_argument("--api-token", default=env_default("CLOUDFLARE_API_TOKEN"))
    parser.add_argument(
        "--domain",
        default=env_default("HORIZON_WORKER_DOMAIN") or env_default("WORKER_DOMAIN"),
        help="Custom hostname for the Worker, for example horizon.example.com.",
    )
    parser.add_argument(
        "--public-base-url",
        default=env_default("PUBLIC_BASE_URL"),
        help="Override the public URL used by Telegram buttons and webhook.",
    )
    parser.add_argument("--telegram-bot-token", default=env_default("TELEGRAM_BOT_TOKEN"))
    parser.add_argument("--telegram-chat-id", default=env_default("TELEGRAM_CHAT_ID"))
    parser.add_argument(
        "--telegram-webhook-secret",
        default=env_default("TELEGRAM_WEBHOOK_SECRET"),
        help="Generated when omitted.",
    )
    parser.add_argument(
        "--horizon-ingest-secret",
        default=(
            env_default("HORIZON_INGEST_SECRET")
            or env_default("TELEGRAM_WORKER_INGEST_SECRET")
        ),
        help="Generated when omitted. Must match TELEGRAM_WORKER_INGEST_SECRET in Horizon.",
    )
    parser.add_argument("--page-title", default=env_default("PAGE_TITLE", "Horizon AI Summary"))
    parser.add_argument("--run-ttl-seconds", default=env_default("RUN_TTL_SECONDS", "2592000"))
    parser.add_argument(
        "--disable-web-page-preview",
        default=env_default("DISABLE_WEB_PAGE_PREVIEW", "true"),
        choices=["true", "false"],
    )
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-webhook", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def env_default(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_env_files(paths: Iterable[str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    default_paths = [REPO_ROOT / ".env", DEFAULT_WORKER_DIR / ".deploy.env"]
    for path in [*default_paths, *(Path(p) for p in paths)]:
        if not path.exists():
            continue
        for key, value in parse_env_file(path).items():
            merged.setdefault(key, value)
            os.environ.setdefault(key, value)
    return merged


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def build_config(args: argparse.Namespace, _env: dict[str, str]) -> DeployConfig:
    worker_dir = Path(args.worker_dir).expanduser().resolve()
    worker_name = args.worker_name or env_default("WORKER_NAME", DEFAULT_WORKER_NAME)
    account_id = args.account_id or env_default("CLOUDFLARE_ACCOUNT_ID")
    api_token = args.api_token or env_default("CLOUDFLARE_API_TOKEN")
    domain = normalize_domain(
        args.domain or env_default("HORIZON_WORKER_DOMAIN") or env_default("WORKER_DOMAIN")
    )
    public_base_url = (args.public_base_url or env_default("PUBLIC_BASE_URL")).strip().rstrip("/")
    if not public_base_url and domain:
        public_base_url = f"https://{domain}"

    telegram_bot_token = args.telegram_bot_token or env_default("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = args.telegram_chat_id or env_default("TELEGRAM_CHAT_ID")
    telegram_webhook_secret = (
        args.telegram_webhook_secret
        or env_default("TELEGRAM_WEBHOOK_SECRET")
        or secrets.token_urlsafe(32)
    )
    horizon_ingest_secret = (
        args.horizon_ingest_secret
        or env_default("HORIZON_INGEST_SECRET")
        or env_default("TELEGRAM_WORKER_INGEST_SECRET")
        or secrets.token_urlsafe(32)
    )

    missing = [
        name
        for name, value in {
            "CLOUDFLARE_API_TOKEN": api_token,
            "CLOUDFLARE_ACCOUNT_ID": account_id,
            "domain or PUBLIC_BASE_URL": domain or public_base_url,
            "TELEGRAM_BOT_TOKEN": telegram_bot_token,
            "TELEGRAM_CHAT_ID": telegram_chat_id,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit("Missing required values: " + ", ".join(missing))

    return DeployConfig(
        worker_dir=worker_dir,
        worker_name=worker_name,
        account_id=account_id,
        api_token=api_token,
        domain=domain,
        public_base_url=public_base_url,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        telegram_webhook_secret=telegram_webhook_secret,
        horizon_ingest_secret=horizon_ingest_secret,
        page_title=args.page_title or env_default("PAGE_TITLE", "Horizon AI Summary"),
        run_ttl_seconds=str(args.run_ttl_seconds or env_default("RUN_TTL_SECONDS", "2592000")),
        disable_web_page_preview=(
            args.disable_web_page_preview
            or env_default("DISABLE_WEB_PAGE_PREVIEW", "true")
        ),
        skip_install=args.skip_install,
        skip_webhook=args.skip_webhook,
        dry_run=args.dry_run,
    )


def normalize_domain(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].strip().lower().rstrip(".")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value):
        raise SystemExit(f"Invalid domain: {value}")
    if "." not in value:
        raise SystemExit(f"Domain must include a zone suffix: {value}")
    return value


def write_wrangler_toml(cfg: DeployConfig) -> None:
    existing = read_existing_kv_ids(cfg.worker_dir / "wrangler.toml")
    lines = [
        f'name = "{toml_escape(cfg.worker_name)}"',
        'main = "src/index.ts"',
        'compatibility_date = "2026-06-04"',
        f'account_id = "{toml_escape(cfg.account_id)}"',
        f"workers_dev = {'false' if cfg.domain else 'true'}",
        "",
    ]
    if cfg.domain:
        lines.extend(
            [
                "[[routes]]",
                f'pattern = "{toml_escape(cfg.domain)}"',
                "custom_domain = true",
                "",
            ]
        )

    lines.extend(
        [
            "[[kv_namespaces]]",
            f'binding = "{KV_BINDING}"',
        ]
    )
    if existing.get("id"):
        lines.append(f'id = "{toml_escape(existing["id"])}"')
    if existing.get("preview_id"):
        lines.append(f'preview_id = "{toml_escape(existing["preview_id"])}"')

    lines.extend(
        [
            "",
            "[vars]",
            f'PAGE_TITLE = "{toml_escape(cfg.page_title)}"',
            f'RUN_TTL_SECONDS = "{toml_escape(cfg.run_ttl_seconds)}"',
            f'DISABLE_WEB_PAGE_PREVIEW = "{toml_escape(cfg.disable_web_page_preview)}"',
            f'PUBLIC_BASE_URL = "{toml_escape(cfg.public_base_url)}"',
            "",
        ]
    )

    path = cfg.worker_dir / "wrangler.toml"
    if cfg.dry_run:
        print(f"[dry-run] would write {path}")
        print("\n".join(lines))
        return
    path.write_text("\n".join(lines), encoding="utf-8")


def read_existing_kv_ids(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    result: dict[str, str] = {}
    for key in ("id", "preview_id"):
        match = re.search(rf'^\s*{key}\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if match and match.group(1) not in PLACEHOLDER_IDS:
            result[key] = match.group(1)
    return result


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def run_npm_install(cfg: DeployConfig) -> None:
    if cfg.skip_install:
        return
    run(
        ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        cfg,
    )


def run_typecheck(cfg: DeployConfig) -> None:
    run(["npm", "run", "typecheck"], cfg)


def run_wrangler_deploy(cfg: DeployConfig) -> None:
    run(["npx", "wrangler", "deploy"], cfg, cloudflare_env(cfg))


def put_worker_secrets(cfg: DeployConfig) -> None:
    secrets_to_put = {
        "TELEGRAM_BOT_TOKEN": cfg.telegram_bot_token,
        "TELEGRAM_CHAT_ID": cfg.telegram_chat_id,
        "TELEGRAM_WEBHOOK_SECRET": cfg.telegram_webhook_secret,
        "HORIZON_INGEST_SECRET": cfg.horizon_ingest_secret,
    }
    for name, value in secrets_to_put.items():
        run(
            ["npx", "wrangler", "secret", "put", name],
            cfg,
            cloudflare_env(cfg),
            input_text=value + "\n",
            secret_command=True,
        )


def set_telegram_webhook(cfg: DeployConfig, webhook_url: str) -> None:
    payload = {
        "url": webhook_url,
        "allowed_updates": ["callback_query"],
        "secret_token": cfg.telegram_webhook_secret,
        "drop_pending_updates": False,
    }
    if cfg.dry_run:
        print(f"[dry-run] would register Telegram webhook: {webhook_url}")
        return

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{cfg.telegram_bot_token}/setWebhook",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Telegram setWebhook failed: HTTP {exc.code}: {body}") from exc
    if not data.get("ok"):
        raise SystemExit(f"Telegram setWebhook failed: {data}")


def cloudflare_env(cfg: DeployConfig) -> dict[str, str]:
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = cfg.api_token
    env["CLOUDFLARE_ACCOUNT_ID"] = cfg.account_id
    return env


def run(
    cmd: list[str],
    cfg: DeployConfig,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    secret_command: bool = False,
) -> None:
    shown = " ".join(cmd)
    print(f"{'[dry-run] ' if cfg.dry_run else ''}$ {shown}")
    if cfg.dry_run:
        return
    try:
        subprocess.run(
            cmd,
            cwd=cfg.worker_dir,
            env=env,
            input=input_text,
            text=True,
            check=True,
            stdout=None if not secret_command else subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Command failed: {shown}") from exc


if __name__ == "__main__":
    main()
