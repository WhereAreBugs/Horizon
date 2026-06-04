# Horizon Telegram Bot Server on Cloudflare

This Worker is an outbound gateway for Horizon Telegram delivery.

It lets a Horizon container run without public inbound networking:

1. Horizon posts a completed Telegram run payload to `POST /api/runs`.
2. The Worker stores the payload in Cloudflare KV.
3. The Worker sends the Telegram overview message.
4. Telegram callback queries go to `POST /telegram/webhook` on Cloudflare.
5. The `AI 总结` button opens `GET /summary/<run_id>` on Cloudflare.

Inline buttons are navigation-only. Item titles stay in the Telegram message
body as source links.

## Cloudflare Resources

The recommended path is the deployment helper from the repository root:

```bash
cp workers/telegram-bot-server/.deploy.env.example workers/telegram-bot-server/.deploy.env
```

Fill `.deploy.env` with your Cloudflare token, account ID, usable hostname, and
Telegram settings:

```bash
CLOUDFLARE_API_TOKEN=...
CLOUDFLARE_ACCOUNT_ID=...
HORIZON_WORKER_DOMAIN=horizon.example.com
TELEGRAM_BOT_TOKEN=123456:bot-token
TELEGRAM_CHAT_ID=-1001234567890
```

Then deploy:

```bash
python3 scripts/deploy_cloudflare_worker.py
```

The helper writes `wrangler.toml`, lets Wrangler provision the KV binding, sets
Worker secrets, deploys the Worker, and registers the Telegram webhook. It
prints the two environment variables needed by the Horizon container at the end.

Useful options:

```bash
python3 scripts/deploy_cloudflare_worker.py --dry-run
python3 scripts/deploy_cloudflare_worker.py --skip-webhook
python3 scripts/deploy_cloudflare_worker.py --domain horizon.example.com
```

If you prefer the manual path, use `npm install`, edit `wrangler.toml`, run
`npm run deploy`, then set the same secrets with `npx wrangler secret put`.

## Horizon Container Config

In the Horizon container, enable `telegram_bot` and set:

```bash
TELEGRAM_WORKER_URL=https://horizon-telegram-bot-server.example.workers.dev
TELEGRAM_WORKER_INGEST_SECRET=the-same-value-as-HORIZON_INGEST_SECRET
```

`TELEGRAM_CHAT_ID` may be omitted from the container if it is configured as a
Cloudflare secret. `TELEGRAM_BOT_TOKEN` is not needed in the Horizon container
when `TELEGRAM_WORKER_URL` is set.

## Telegram Webhook

Register Telegram to call Cloudflare:

```bash
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://horizon-telegram-bot-server.example.workers.dev/telegram/webhook",
    "allowed_updates": ["callback_query"],
    "secret_token": "the-same-value-as-TELEGRAM_WEBHOOK_SECRET"
  }'
```

## Cloudflare Pages

The same app is exposed through `functions/[[path]].ts`, so it can be deployed
as Cloudflare Pages Functions.

Bind the KV namespace to the Pages project as `HORIZON_TG_RUNS`, set the same
environment variables/secrets, then deploy:

```bash
npm run deploy:pages
```

Use the Pages domain as `TELEGRAM_WORKER_URL` and set Telegram's webhook to
`https://<pages-domain>/telegram/webhook`.
