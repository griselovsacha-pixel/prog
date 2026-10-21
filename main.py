import asyncio
import html
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("harledy")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]  # Supabase pooler URL (порт 6543)
SITE_URL = (os.environ.get("SITE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
BASE = Path(__file__).parent

EMOJI_MAIN = "5217848986433502482"
EMOJI_LINK = "5193202823411546657"

SCHEMA = """
create table if not exists users(
    tg_id bigint primary key,
    full_name text,
    username text,
    token text unique not null,
    created_at timestamptz not null default now()
);
create table if not exists visits(
    id bigserial primary key,
    tg_id bigint not null references users(tg_id),
    ip text,
    user_agent text,
    created_at timestamptz not null default now()
);
create index if not exists visits_user_idx on visits(tg_id, created_at desc);
"""

pool: asyncpg.Pool | None = None
router = Router()


def emoji(eid: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


async def answer_safe(m: Message, text: str, fallback_text: str, **kw):
    try:
        return await m.answer(text, **kw)
    except TelegramBadRequest:
        return await m.answer(fallback_text, **kw)


@router.message(CommandStart())
async def on_start(m: Message):
    u = m.from_user
    row = await pool.fetchrow(
        """insert into users(tg_id, full_name, username, token)
           values($1, $2, $3, $4)
           on conflict (tg_id) do update
             set full_name = excluded.full_name, username = excluded.username
           returning token""",
        u.id, u.full_name, u.username, secrets.token_urlsafe(18),
    )
    link = f"{SITE_URL}/?k={row['token']}"
    name = html.escape(f"@{u.username}" if u.username else u.full_name)

    await answer_safe(m, emoji(EMOJI_MAIN, "🔍"), "🔍")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть Harledy Project", url=link)]]
    )
    await answer_safe(
        m,
        f"Приветствую, {name}!\nСборник инструментов Harledy Project доступен по ссылке ниже {emoji(EMOJI_LINK, '👇')}",
        f"Приветствую, {name}!\nСборник инструментов Harledy Project доступен по ссылке ниже 👇",
        reply_markup=kb,
    )


async def self_ping():
    """Раз в 10 минут дёргаем свой /ping, чтобы Render free не засыпал."""
    url = os.environ.get("RENDER_EXTERNAL_URL") or SITE_URL
    if not url:
        return
    async with aiohttp.ClientSession() as s:
        while True:
            await asyncio.sleep(600)
            try:
                async with s.get(f"{url}/ping", timeout=aiohttp.ClientTimeout(total=15)) as r:
                    await r.read()
            except Exception as e:
                log.warning("self-ping failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, statement_cache_size=0)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    tasks = [
        asyncio.create_task(dp.start_polling(bot, handle_signals=False)),
        asyncio.create_task(self_ping()),
    ]
    yield
    for t in tasks:
        t.cancel()
    await bot.session.close()
    await pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def client_ip(req: Request) -> str:
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return req.client.host if req.client else ""


@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    return {"ok": True}


@app.get("/api/tools")
async def tools(request: Request, k: str = ""):
    user = await pool.fetchrow("select tg_id from users where token = $1", k) if k else None
    if not user:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    await pool.execute(
        "insert into visits(tg_id, ip, user_agent) values($1, $2, $3)",
        user["tg_id"], client_ip(request), request.headers.get("user-agent", "")[:300],
    )
    data = json.loads((BASE / "data" / "tools.json").read_text("utf-8"))
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
