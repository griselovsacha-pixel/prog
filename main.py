import asyncio
import html
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery, ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("harledy")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]  # Supabase pooler URL (порт 6543)
SITE_URL = (os.environ.get("SITE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/+X8s2DkoSD7M3MTc6")
CHANNEL_ID = int(os.environ["CHANNEL_ID"]) if os.environ.get("CHANNEL_ID") else None
BASE = Path(__file__).parent

# custom emoji
EMOJI_MAIN = "5217848986433502482"
EMOJI_LINK = "5193202823411546657"
EMOJI_GATE = "6021409772350215140"
ICON_CHANNEL = "6021452855167164269"
ICON_DONE = "6021868492037298942"

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
create table if not exists join_requests(
    tg_id bigint not null,
    chat_id bigint not null,
    created_at timestamptz not null default now(),
    primary key (tg_id, chat_id)
);
"""

pool: asyncpg.Pool | None = None
router = Router()


# ---------- helpers ----------
def emoji(eid: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


def strip_emoji(text: str) -> str:
    return re.sub(r"</?tg-emoji[^>]*>", "", text)


async def send(bot: Bot, chat_id: int, text: str, kb=None, kb_plain=None):
    """Отправка с кастом-эмодзи; если Telegram отклонил — шлём обычными смайликами."""
    try:
        return await bot.send_message(chat_id, text, reply_markup=kb)
    except TelegramBadRequest as e:
        log.warning("rich send failed (%s), fallback to plain", e)
        return await bot.send_message(chat_id, strip_emoji(text), reply_markup=kb_plain or kb)


async def ensure_user(u) -> str:
    """Создаёт юзера при первом контакте. Токен ссылки генерируется один раз и больше не меняется."""
    row = await pool.fetchrow(
        """insert into users(tg_id, full_name, username, token)
           values($1, $2, $3, $4)
           on conflict (tg_id) do update
             set full_name = excluded.full_name, username = excluded.username
           returning token""",
        u.id, u.full_name, u.username, secrets.token_urlsafe(18),
    )
    return row["token"]


def is_our_channel(chat_id: int, invite_link: str | None) -> bool:
    if CHANNEL_ID:
        return chat_id == CHANNEL_ID
    return bool(invite_link) and invite_link.rstrip("/") == CHANNEL_LINK.rstrip("/")


async def has_access(bot: Bot, uid: int) -> bool:
    """Доступ есть, если подана заявка в канал (или юзер уже участник канала, например админ)."""
    if await pool.fetchval("select 1 from join_requests where tg_id = $1 limit 1", uid):
        return True
    if CHANNEL_ID:
        try:
            member = await bot.get_chat_member(CHANNEL_ID, uid)
            return member.status in ("member", "administrator", "creator")
        except TelegramAPIError as e:
            log.warning("get_chat_member failed: %s", e)
    return False


def gate_kb(icons: bool) -> InlineKeyboardMarkup:
    def btn(text, icon, **kw):
        extra = {"icon_custom_emoji_id": icon} if icons else {}
        return InlineKeyboardButton(text=text, **kw, **extra)

    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Harledy project", ICON_CHANNEL, url=CHANNEL_LINK)],
        [btn("Я подписался", ICON_DONE, callback_data="check_sub")],
    ])


async def send_gate(bot: Bot, chat_id: int):
    await send(
        bot, chat_id,
        f"{emoji(EMOJI_GATE, '⚠️')} Для использования бота необходима подписка на канал!",
        kb=gate_kb(True), kb_plain=gate_kb(False),
    )


async def send_menu(bot: Bot, u):
    token = await ensure_user(u)
    link = f"{SITE_URL}/?k={token}"
    name = html.escape(f"@{u.username}" if u.username else u.full_name)
    await send(bot, u.id, emoji(EMOJI_MAIN, "🔍"))
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть Harledy Project", url=link)]]
    )
    await send(
        bot, u.id,
        f"Приветствую, {name}!\n"
        f"Сборник инструментов Harledy Project доступен по ссылке ниже {emoji(EMOJI_LINK, '👇')}\n\n"
        "<i>Сайт желательно открывать с компьютера, так как мобильная версия может работать некорректно.</i>",
        kb=kb,
    )


# ---------- handlers ----------
@router.message(CommandStart())
async def on_start(m: Message):
    await ensure_user(m.from_user)  # ссылка создаётся сразу, при первом контакте
    if await has_access(m.bot, m.from_user.id):
        await send_menu(m.bot, m.from_user)
    else:
        await send_gate(m.bot, m.chat.id)


@router.callback_query(F.data == "check_sub")
async def on_check(cb: CallbackQuery):
    await ensure_user(cb.from_user)
    if await has_access(cb.bot, cb.from_user.id):
        await cb.answer("Спасибо за подписку!", show_alert=True)
        try:
            await cb.message.delete()
        except TelegramAPIError:
            pass
        await send_menu(cb.bot, cb.from_user)
    else:
        log.info("check_sub: no join request / membership for user=%s", cb.from_user.id)
        await cb.answer("Подпишитесь, пожалуйста!", show_alert=True)


@router.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    link = req.invite_link.invite_link if req.invite_link else None
    log.info("join request: user=%s chat_id=%s title=%r link=%s", req.from_user.id, req.chat.id, req.chat.title, link)
    if not is_our_channel(req.chat.id, link):
        return
    await pool.execute(
        "insert into join_requests(tg_id, chat_id) values($1, $2) on conflict do nothing",
        req.from_user.id, req.chat.id,
    )


# ---------- web ----------
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


async def diagnose(bot: Bot):
    """Пишет в лог, видит ли бот канал и может ли получать заявки."""
    me = await bot.get_me()
    log.info("bot: @%s id=%s", me.username, me.id)
    if not CHANNEL_ID:
        log.warning("CHANNEL_ID не задан: членство в канале не проверяется, заявки ловятся только по тексту ссылки-приглашения")
        return
    try:
        chat = await bot.get_chat(CHANNEL_ID)
        m = await bot.get_chat_member(CHANNEL_ID, me.id)
        can = getattr(m, "can_invite_users", None)
        log.info("channel %r (%s): bot status=%s can_invite_users=%s", chat.title, CHANNEL_ID, m.status, can)
        if m.status not in ("administrator", "creator") or can is False:
            log.warning("БОТ НЕ АДМИН КАНАЛА или у него нет права «Приглашать пользователей»: заявки приходить не будут")
    except TelegramAPIError as e:
        log.warning("Не удалось проверить канал %s (неверный CHANNEL_ID или бот не в канале): %s", CHANNEL_ID, e)


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
    try:
        await diagnose(bot)
    except Exception as e:
        log.warning("diagnose failed: %s", e)
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


@app.middleware("http")
async def about_subdomain(request: Request, call_next):
    # about.<домен> открывает страницу «О нас» (когда подключён свой домен)
    host = request.headers.get("host", "").split(":")[0]
    if host.startswith("about.") and request.url.path == "/":
        return FileResponse(BASE / "static" / "about.html")
    return await call_next(request)


@app.get("/about")
async def about_page():
    return FileResponse(BASE / "static" / "about.html")


@app.get("/collab")
async def collab_page():
    return FileResponse(BASE / "static" / "collab.html")


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
