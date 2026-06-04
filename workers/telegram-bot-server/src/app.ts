import { marked } from "marked";
import sanitizeHtml from "sanitize-html";

export interface Env {
  HORIZON_TG_RUNS: KVNamespace;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_CHAT_ID?: string;
  TELEGRAM_WEBHOOK_SECRET?: string;
  HORIZON_INGEST_SECRET?: string;
  PUBLIC_BASE_URL?: string;
  PAGE_TITLE?: string;
  RUN_TTL_SECONDS?: string;
  DISABLE_WEB_PAGE_PREVIEW?: string;
}

interface TelegramItem {
  index?: number;
  title?: string;
  score?: string | number;
  url?: string;
  text?: string;
  markdown?: string;
  excerpt?: string;
}

interface RunPayload {
  run_id?: string;
  created_at?: string;
  date?: string;
  language?: string;
  all_items_count?: number;
  important_items_count?: number;
  delivered_items_count?: number;
  max_items?: number;
  summary_markdown?: string;
  page_size?: number;
  overview_limit?: number;
  overview?: string;
  items?: TelegramItem[];
  telegram_chat_id?: string;
}

interface IngestBody {
  payload?: RunPayload;
  chat_id?: string | number;
}

const CALLBACK_RE = /^hzn:o:([A-Za-z0-9_-]{1,64}):(\d+)$/;
const RUN_RE = /^[A-Za-z0-9_-]{1,64}$/;
const DEFAULT_PAGE_SIZE = 5;
const DEFAULT_OVERVIEW_LIMIT = 3600;
const DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 30;

export async function handleRequest(
  request: Request,
  env: Env,
  _ctx?: Pick<ExecutionContext, "waitUntil">,
): Promise<Response> {
  const url = new URL(request.url);

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }

  try {
    if (request.method === "GET" && url.pathname === "/healthz") {
      return json({ ok: true });
    }
    if (request.method === "GET" && url.pathname === "/") {
      return html(renderHome());
    }
    if (request.method === "POST" && url.pathname === "/api/runs") {
      return await ingestRun(request, env);
    }
    if (request.method === "POST" && url.pathname === "/telegram/webhook") {
      return await telegramWebhook(request, env);
    }
    if (request.method === "GET" && url.pathname.startsWith("/summary/")) {
      const runId = decodeURIComponent(url.pathname.slice("/summary/".length));
      return await summaryPage(runId, env);
    }
    return json({ ok: false, error: "not found" }, 404);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return json({ ok: false, error: message }, 500);
  }
}

async function ingestRun(request: Request, env: Env): Promise<Response> {
  if (!env.HORIZON_INGEST_SECRET) {
    return json({ ok: false, error: "missing HORIZON_INGEST_SECRET" }, 500);
  }
  if (!isAuthorizedIngest(request, env)) {
    return json({ ok: false, error: "forbidden" }, 403);
  }

  const rawBody = await request.json();
  if (!rawBody || typeof rawBody !== "object") {
    return json({ ok: false, error: "request body must be a JSON object" }, 400);
  }

  const body = rawBody as IngestBody | RunPayload;
  const payload = normalizePayload(body);
  const bodyChatId = (body as IngestBody).chat_id;
  const chatId = String(bodyChatId || env.TELEGRAM_CHAT_ID || "").trim();
  if (!chatId) {
    return json({ ok: false, error: "missing Telegram chat id" }, 400);
  }

  payload.telegram_chat_id = chatId;
  await storePayload(env, payload);

  const keyboard = buildOverviewKeyboard(payload, 0, publicBaseUrl(request, env));
  const message: Record<string, unknown> = {
    chat_id: chatId,
    text: buildOverviewText(payload, 0),
    parse_mode: "HTML",
    disable_web_page_preview: env.DISABLE_WEB_PAGE_PREVIEW !== "false",
  };
  if (keyboard.inline_keyboard.length) {
    message.reply_markup = keyboard;
  }
  const telegramResult = await telegramCall(env, "sendMessage", message);

  return json({
    ok: true,
    run_id: payload.run_id,
    summary_url: `${publicBaseUrl(request, env)}/summary/${payload.run_id}`,
    telegram: telegramResult.result || null,
  });
}

async function telegramWebhook(request: Request, env: Env): Promise<Response> {
  const expectedSecret = env.TELEGRAM_WEBHOOK_SECRET || "";
  if (expectedSecret) {
    const got = request.headers.get("x-telegram-bot-api-secret-token") || "";
    if (got !== expectedSecret) {
      return json({ ok: false, error: "forbidden" }, 403);
    }
  }

  const update = (await request.json()) as Record<string, any>;
  const callback = update.callback_query || {};
  const callbackId = callback.id;
  const data = String(callback.data || "");
  const match = CALLBACK_RE.exec(data);
  if (!match) {
    if (callbackId) {
      await answerCallback(env, callbackId, "Unsupported action");
    }
    return json({ ok: true });
  }

  const runId = match[1];
  const payload = await loadPayload(env, runId);
  if (!payload) {
    if (callbackId) {
      await answerCallback(env, callbackId, "Expired");
    }
    return json({ ok: true });
  }

  const message = callback.message || {};
  const chat = message.chat || {};
  const expectedChat = String(payload.telegram_chat_id || env.TELEGRAM_CHAT_ID || "");
  if (expectedChat && String(chat.id || "") !== expectedChat) {
    if (callbackId) {
      await answerCallback(env, callbackId, "Unauthorized");
    }
    return json({ ok: false, error: "unauthorized" }, 403);
  }

  const page = clampPage(parsePositiveInt(match[2], 0), payload);
  await telegramCall(env, "editMessageText", {
    chat_id: chat.id,
    message_id: message.message_id,
    text: buildOverviewText(payload, page),
    parse_mode: "HTML",
    disable_web_page_preview: env.DISABLE_WEB_PAGE_PREVIEW !== "false",
    reply_markup: buildOverviewKeyboard(payload, page, publicBaseUrl(request, env)),
  });
  if (callbackId) {
    await answerCallback(env, callbackId);
  }
  return json({ ok: true });
}

async function summaryPage(runId: string, env: Env): Promise<Response> {
  if (!RUN_RE.test(runId)) {
    return html(renderNotFound("Invalid run id"), 404);
  }
  const payload = await loadPayload(env, runId);
  if (!payload) {
    return html(renderNotFound("Summary expired or not found"), 404);
  }
  return html(renderSummary(payload, env));
}

function normalizePayload(body: IngestBody | RunPayload): RunPayload {
  const maybeWrapped = body as IngestBody;
  const payload =
    maybeWrapped.payload && typeof maybeWrapped.payload === "object"
      ? maybeWrapped.payload
      : (body as RunPayload);
  if (!payload || typeof payload !== "object") {
    throw new Error("payload must be a JSON object");
  }
  if (!Array.isArray(payload.items)) {
    payload.items = [];
  }
  payload.run_id = RUN_RE.test(String(payload.run_id || ""))
    ? String(payload.run_id)
    : crypto.randomUUID().replaceAll("-", "").slice(0, 18);
  payload.created_at = payload.created_at || new Date().toISOString();
  payload.language = payload.language || "zh";
  payload.items = payload.items.map((item, index) => ({
    ...item,
    index: parsePositiveInt(item.index, index + 1),
  }));
  payload.page_size = parsePositiveInt(payload.page_size, DEFAULT_PAGE_SIZE);
  payload.overview_limit = parsePositiveInt(
    payload.overview_limit,
    DEFAULT_OVERVIEW_LIMIT,
  );
  payload.important_items_count = parsePositiveInt(
    payload.important_items_count,
    payload.items.length,
  );
  payload.delivered_items_count = payload.items.length;
  return payload;
}

async function storePayload(env: Env, payload: RunPayload): Promise<void> {
  const ttl = Math.max(
    60,
    parsePositiveInt(env.RUN_TTL_SECONDS, DEFAULT_TTL_SECONDS),
  );
  await env.HORIZON_TG_RUNS.put(runKey(payload.run_id || ""), JSON.stringify(payload), {
    expirationTtl: ttl,
  });
}

async function loadPayload(env: Env, runId: string): Promise<RunPayload | null> {
  if (!RUN_RE.test(runId)) {
    return null;
  }
  return await env.HORIZON_TG_RUNS.get<RunPayload>(runKey(runId), "json");
}

function buildOverviewText(payload: RunPayload, page: number): string {
  const items = payload.items || [];
  const overview = String(payload.overview || "");
  if (!items.length) {
    return truncate(escapeHtml(overview), telegramLimit(payload));
  }

  const pageSize = pageSizeOf(payload);
  const safePage = clampPage(page, payload);
  const start = safePage * pageSize;
  const pageItems = items.slice(start, start + pageSize);
  const pageTotal = pageCount(payload);
  const end = start + pageItems.length;
  const lang = payload.language || "zh";
  const status =
    lang === "zh"
      ? `第 ${safePage + 1}/${pageTotal} 页 · 当前 ${start + 1}-${end}/${items.length}`
      : `Page ${safePage + 1}/${pageTotal} · Showing ${start + 1}-${end}/${items.length}`;
  const header = `${escapeHtml(overview)}\n\n${escapeHtml(status)}`;
  const limit = telegramLimit(payload);
  const perItemLimit = Math.max(
    120,
    Math.floor((limit - header.length - 2 * pageItems.length) / pageItems.length),
  );
  const blocks = pageItems.map((item) => pageItemBlock(item, perItemLimit));
  let text = [header, ...blocks].join("\n\n");
  if (text.length <= limit) {
    return text;
  }

  const compactBlocks = pageItems.map((item) => pageItemBlock(item, 0).split("\n", 1)[0]);
  text = [header, ...compactBlocks].join("\n\n");
  return truncate(text, limit);
}

function buildOverviewKeyboard(
  payload: RunPayload,
  page: number,
  baseUrl: string,
): { inline_keyboard: Array<Array<Record<string, string>>> } {
  const rows: Array<Array<Record<string, string>>> = [];
  const items = payload.items || [];
  const runId = payload.run_id || "";
  const lang = payload.language || "zh";
  const pageTotal = pageCount(payload);
  const safePage = clampPage(page, payload);

  if (items.length && pageTotal > 1) {
    const nav: Array<Record<string, string>> = [];
    if (safePage > 0) {
      nav.push({
        text: lang === "zh" ? "上一页" : "Prev",
        callback_data: `hzn:o:${runId}:${safePage - 1}`,
      });
    }
    nav.push({
      text:
        lang === "zh"
          ? `第 ${safePage + 1}/${pageTotal} 页`
          : `Page ${safePage + 1}/${pageTotal}`,
      callback_data: `hzn:o:${runId}:${safePage}`,
    });
    if (safePage < pageTotal - 1) {
      nav.push({
        text: lang === "zh" ? "下一页" : "Next",
        callback_data: `hzn:o:${runId}:${safePage + 1}`,
      });
    }
    rows.push(nav);
  }

  if (baseUrl && runId) {
    rows.push([
      {
        text: lang === "zh" ? "AI 总结" : "AI Summary",
        url: `${baseUrl}/summary/${encodeURIComponent(runId)}`,
      },
    ]);
  }
  return { inline_keyboard: rows };
}

function pageItemBlock(item: TelegramItem, excerptLimit: number): string {
  const index = parsePositiveInt(item.index, 0);
  const title = escapeHtml(String(item.title || "Untitled"));
  const url = String(item.url || "");
  const linkedTitle = isHttpUrl(url)
    ? `<a href="${escapeHtmlAttr(url)}">${title}</a>`
    : title;
  const score = item.score === undefined || item.score === "" ? "?" : String(item.score);
  const heading = `${index}. ${linkedTitle} · ${escapeHtml(score)}/10`;
  const body = String(item.excerpt || item.text || "");
  if (!body || excerptLimit <= 0) {
    return heading;
  }
  return `${heading}\n${escapeHtml(truncate(body, excerptLimit))}`;
}

function pageSizeOf(payload: RunPayload): number {
  return Math.max(1, parsePositiveInt(payload.page_size, DEFAULT_PAGE_SIZE));
}

function pageCount(payload: RunPayload): number {
  return Math.max(1, Math.ceil((payload.items || []).length / pageSizeOf(payload)));
}

function clampPage(page: number, payload: RunPayload): number {
  return Math.min(Math.max(page, 0), pageCount(payload) - 1);
}

function telegramLimit(payload: RunPayload): number {
  return Math.min(
    3900,
    Math.max(1, parsePositiveInt(payload.overview_limit, DEFAULT_OVERVIEW_LIMIT)),
  );
}

async function telegramCall(
  env: Env,
  method: string,
  payload: Record<string, unknown>,
): Promise<Record<string, any>> {
  if (!env.TELEGRAM_BOT_TOKEN) {
    throw new Error("missing TELEGRAM_BOT_TOKEN");
  }
  const response = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  const data = (await response.json().catch(() => ({}))) as Record<string, any>;
  if (!response.ok || !data.ok) {
    throw new Error(String(data.description || `Telegram ${method} failed`));
  }
  return data;
}

async function answerCallback(
  env: Env,
  callbackId: string,
  text?: string,
): Promise<void> {
  const payload: Record<string, unknown> = { callback_query_id: callbackId };
  if (text) {
    payload.text = text;
  }
  await telegramCall(env, "answerCallbackQuery", payload);
}

function isAuthorizedIngest(request: Request, env: Env): boolean {
  const bearer = request.headers.get("authorization") || "";
  const token = bearer.startsWith("Bearer ") ? bearer.slice("Bearer ".length) : "";
  const headerToken = request.headers.get("x-horizon-ingest-secret") || "";
  return token === env.HORIZON_INGEST_SECRET || headerToken === env.HORIZON_INGEST_SECRET;
}

function publicBaseUrl(request: Request, env: Env): string {
  return (env.PUBLIC_BASE_URL || new URL(request.url).origin).replace(/\/+$/, "");
}

function parsePositiveInt(value: unknown, fallback: number): number {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function truncate(text: string, limit: number): string {
  if (text.length <= limit) {
    return text;
  }
  const marker = "\n\n...";
  return `${text.slice(0, Math.max(0, limit - marker.length)).trimEnd()}${marker}`;
}

function isHttpUrl(url: string): boolean {
  return url.startsWith("https://") || url.startsWith("http://");
}

function runKey(runId: string): string {
  return `run:${runId}`;
}

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...corsHeaders(),
    },
  });
}

function html(body: string, status = 200): Response {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

function corsHeaders(): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers":
      "authorization,content-type,x-horizon-ingest-secret,x-telegram-bot-api-secret-token",
  };
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeHtmlAttr(value: string): string {
  return escapeHtml(value).replaceAll("'", "&#39;");
}

function renderHome(): string {
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Horizon Telegram Bot Server</title>
  <style>${pageStyles()}</style>
</head>
<body>
  <main class="shell">
    <h1>Horizon Telegram Bot Server</h1>
    <p>Worker is ready. Telegram callbacks are handled at <code>/telegram/webhook</code>; AI summaries are served from <code>/summary/&lt;run_id&gt;</code>.</p>
  </main>
</body>
</html>`;
}

function renderNotFound(message: string): string {
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${escapeHtml(message)}</title>
  <style>${pageStyles()}</style>
</head>
<body>
  <main class="shell">
    <h1>${escapeHtml(message)}</h1>
  </main>
</body>
</html>`;
}

function renderSummary(payload: RunPayload, env: Env): string {
  const lang = payload.language || "zh";
  const title = escapeHtml(env.PAGE_TITLE || "Horizon AI Summary");
  const date = escapeHtml(String(payload.date || ""));
  const overview = escapeHtml(String(payload.overview || ""));
  const items = payload.items || [];
  const markdown = String(payload.summary_markdown || "").trim();
  const renderedContent = markdown
    ? renderMarkdown(markdown)
    : renderMarkdown(buildFallbackMarkdown(payload));

  return `<!doctype html>
<html lang="${lang === "zh" ? "zh-CN" : "en"}">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${title}</title>
  <style>${pageStyles()}</style>
</head>
<body>
  <main class="shell">
    <header class="summary-head">
      <p class="eyebrow">Horizon</p>
      <h1>${title}</h1>
      <p class="meta">${date} · ${items.length}/${parsePositiveInt(payload.all_items_count, items.length)}</p>
    </header>
    <section class="overview">${overview.replaceAll("\n", "<br />")}</section>
    <section class="markdown-body">${renderedContent}</section>
  </main>
</body>
</html>`;
}

function buildFallbackMarkdown(payload: RunPayload): string {
  const lang = payload.language || "zh";
  const items = payload.items || [];
  if (!items.length) {
    return lang === "zh" ? "没有条目。" : "No items.";
  }
  return items
    .map((item) => {
      const title = String(item.title || "Untitled").replaceAll("[", "(").replaceAll("]", ")");
      const sourceUrl = String(item.url || "");
      const score = item.score === undefined || item.score === "" ? "?" : String(item.score);
      const heading = isHttpUrl(sourceUrl)
        ? `## [${title}](${sourceUrl}) ⭐️ ${score}/10`
        : `## ${title} ⭐️ ${score}/10`;
      return [heading, "", item.markdown || item.text || item.excerpt || ""]
        .filter((part) => String(part).trim())
        .join("\n");
    })
    .join("\n\n---\n\n");
}

function renderMarkdown(markdown: string): string {
  const rawHtml = marked.parse(markdown, {
    async: false,
    gfm: true,
    breaks: false,
  }) as string;
  return sanitizeHtml(rawHtml, {
    allowedTags: [
      "a",
      "blockquote",
      "br",
      "code",
      "del",
      "details",
      "em",
      "h1",
      "h2",
      "h3",
      "h4",
      "h5",
      "h6",
      "hr",
      "li",
      "ol",
      "p",
      "pre",
      "span",
      "strong",
      "summary",
      "table",
      "tbody",
      "td",
      "th",
      "thead",
      "tr",
      "ul",
    ],
    allowedAttributes: {
      a: ["href", "id", "name", "target", "rel"],
      code: ["class"],
      span: ["id"],
      th: ["align"],
      td: ["align"],
    },
    allowedSchemes: ["http", "https", "mailto"],
    transformTags: {
      a: (_tagName, attribs) => ({
        tagName: "a",
        attribs: {
          ...attribs,
          target: "_blank",
          rel: "noopener noreferrer",
        },
      }),
    },
  });
}

function pageStyles(): string {
  return `
:root {
  color-scheme: light;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f7f7f5;
  color: #1f2933;
}
body {
  margin: 0;
}
.shell {
  width: min(920px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 32px 0 56px;
}
.summary-head {
  border-bottom: 1px solid #d8ddd8;
  padding-bottom: 20px;
}
.eyebrow {
  color: #4f6f62;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0;
  margin: 0 0 8px;
  text-transform: uppercase;
}
h1 {
  font-size: 38px;
  line-height: 1.12;
  margin: 0 0 12px;
}
h2 {
  font-size: 20px;
  line-height: 1.35;
  margin: 0 0 10px;
}
a {
  color: #0f766e;
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
}
.meta {
  color: #64748b;
  font-size: 14px;
  margin: 0;
}
.overview {
  border-bottom: 1px solid #d8ddd8;
  line-height: 1.7;
  margin: 24px 0;
  padding: 0 0 24px;
}
.items {
  display: grid;
  gap: 16px;
}
.item {
  background: #ffffff;
  border: 1px solid #d8ddd8;
  border-radius: 8px;
  padding: 20px;
}
.item p {
  line-height: 1.72;
  margin: 14px 0 0;
}
code {
  background: #ecefed;
  border-radius: 4px;
  padding: 2px 5px;
}
@media (max-width: 640px) {
  .shell {
    width: min(100vw - 24px, 920px);
    padding-top: 22px;
  }
  h1 {
    font-size: 28px;
  }
  .item {
    padding: 16px;
  }
}
`;
}
