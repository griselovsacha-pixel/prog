import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import struct
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("harledy")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]  # Supabase pooler URL (порт 6543)
SITE_URL = (os.environ.get("SITE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/+X8s2DkoSD7M3MTc6")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID") or -1004319976611)  # переменная окружения, если задана, главнее
BASE = Path(__file__).parent

# --- админка (всё секретное только из переменных окружения) ---
ADMIN_IP = os.environ.get("ADMIN_IP", "91.224.45.1")
ADMIN_LOGIN = os.environ.get("ADMIN_LOGIN", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_TOTP = os.environ.get("ADMIN_TOTP_SECRET", "").replace(" ", "").upper()
ADMIN_TZ = os.environ.get("ADMIN_TZ", "Europe/Kyiv")
SESSION_SECRET = (
    os.environ.get("ADMIN_SESSION_SECRET")
    or hashlib.sha256(f"{ADMIN_PASSWORD}|{ADMIN_TOTP}|{BOT_TOKEN}".encode()).hexdigest()
).encode()

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
create table if not exists join_requests(
    tg_id bigint not null,
    chat_id bigint not null,
    created_at timestamptz not null default now(),
    primary key (tg_id, chat_id)
);
create table if not exists sessions(
    id text primary key,
    tg_id bigint not null,
    ip text,
    user_agent text,
    started_at timestamptz not null default now(),
    last_seen timestamptz not null default now()
);
create table if not exists events(
    id bigserial primary key,
    tg_id bigint not null,
    sid text,
    ev text not null,
    tool text,
    ip text,
    created_at timestamptz not null default now()
);
create index if not exists visits_user_idx on visits(tg_id, created_at desc);
create index if not exists visits_created_idx on visits(created_at desc);
create index if not exists users_created_idx on users(created_at desc);
create index if not exists jr_created_idx on join_requests(created_at desc);
create index if not exists sessions_user_idx on sessions(tg_id, started_at desc);
create index if not exists sessions_seen_idx on sessions(last_seen desc);
create index if not exists events_user_idx on events(tg_id, created_at desc);
create index if not exists events_tool_idx on events(ev, tool);
alter table users enable row level security;
alter table visits enable row level security;
alter table join_requests enable row level security;
alter table sessions enable row level security;
alter table events enable row level security;
"""

pool: asyncpg.Pool | None = None
router = Router()


# ====================== Telegram-бот ======================
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


@router.message(F.forward_origin.chat.type == "channel")
async def on_forwarded(m: Message):
    """Перешли боту пост из канала — он покажет ID канала для CHANNEL_ID."""
    await m.answer(f"ID канала: <code>{m.forward_origin.chat.id}</code>")


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


async def diagnose(bot: Bot):
    """Пишет в лог, видит ли бот канал и может ли получать заявки."""
    me = await bot.get_me()
    log.info("bot: @%s id=%s", me.username, me.id)
    try:
        chat = await bot.get_chat(CHANNEL_ID)
        m = await bot.get_chat_member(CHANNEL_ID, me.id)
        can = getattr(m, "can_invite_users", None)
        log.info("channel %r (%s): bot status=%s can_invite_users=%s", chat.title, CHANNEL_ID, m.status, can)
        if m.status not in ("administrator", "creator") or can is False:
            log.warning("БОТ НЕ АДМИН КАНАЛА или у него нет права «Приглашать пользователей»: заявки приходить не будут")
    except TelegramAPIError as e:
        log.warning("Не удалось проверить канал %s (неверный CHANNEL_ID или бот не в канале): %s", CHANNEL_ID, e)


# ====================== фон ======================
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
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)
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


# ====================== общее для веба ======================
def client_ip(req: Request) -> str:
    # заголовки Cloudflare клиент подделать не может, поэтому им доверяем в первую очередь
    for h in ("cf-connecting-ip", "true-client-ip"):
        v = req.headers.get(h)
        if v:
            return v.strip()
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return req.client.host if req.client else ""


def main_url(req: Request) -> str:
    host = req.headers.get("host", "")
    if host.startswith(("admin.", "about.")):
        host = host.split(".", 1)[1]
    scheme = req.headers.get("x-forwarded-proto") or req.url.scheme
    return f"{scheme}://{host}/"


@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    return {"ok": True}


_tok_cache: dict[str, int] = {}


async def uid_by_token(k: str) -> int | None:
    if k in _tok_cache:
        return _tok_cache[k]
    row = await pool.fetchrow("select tg_id from users where token = $1", k) if k else None
    if not row:
        return None
    if len(_tok_cache) > 5000:
        _tok_cache.clear()
    _tok_cache[k] = row["tg_id"]
    return row["tg_id"]


@app.get("/api/tools")
async def tools(request: Request, k: str = ""):
    uid = await uid_by_token(k)
    if not uid:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    await pool.execute(
        "insert into visits(tg_id, ip, user_agent) values($1, $2, $3)",
        uid, client_ip(request), request.headers.get("user-agent", "")[:300],
    )
    data = json.loads((BASE / "data" / "tools.json").read_text("utf-8"))
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


class Track(BaseModel):
    k: str = Field(max_length=100)
    sid: str = Field(max_length=64)
    ev: str = Field(max_length=20)
    tool: str | None = Field(default=None, max_length=200)


@app.post("/api/track")
async def track(request: Request, b: Track):
    """hb — пульс (время на сайте и «онлайн»), select/bot/eternal — действия с инструментами."""
    if b.ev not in ("hb", "select", "bot", "eternal") or not re.fullmatch(r"[\w-]{8,64}", b.sid):
        return JSONResponse({"ok": False}, status_code=400)
    uid = await uid_by_token(b.k)
    if not uid:
        return JSONResponse({"ok": False}, status_code=403)
    ip = client_ip(request)
    await pool.execute(
        """insert into sessions(id, tg_id, ip, user_agent) values($1, $2, $3, $4)
           on conflict (id) do update set last_seen = now()""",
        b.sid, uid, ip, request.headers.get("user-agent", "")[:300],
    )
    if b.ev != "hb":
        await pool.execute(
            "insert into events(tg_id, sid, ev, tool, ip) values($1, $2, $3, $4, $5)",
            uid, b.sid, b.ev, b.tool, ip,
        )
    return {"ok": True}


# ====================== АДМИНКА ======================
def b32_totp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6:06d}"


_last_totp = 0


def verify_totp(code: str) -> bool:
    global _last_totp
    code = re.sub(r"\D", "", code or "")
    if len(code) != 6 or not ADMIN_TOTP:
        return False
    now = int(time.time() // 30)
    for c in (now - 1, now, now + 1):          # допуск ±30 сек; один код — один раз
        if c > _last_totp and hmac.compare_digest(b32_totp(ADMIN_TOTP, c), code):
            _last_totp = c
            return True
    return False


def sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{raw}.{hmac.new(SESSION_SECRET, raw.encode(), hashlib.sha256).hexdigest()}"


def unsign(tok: str) -> dict | None:
    try:
        raw, sig = tok.rsplit(".", 1)
        if not hmac.compare_digest(hmac.new(SESSION_SECRET, raw.encode(), hashlib.sha256).hexdigest(), sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return data if data.get("exp", 0) > time.time() else None
    except Exception:
        return None


def admin_enabled() -> bool:
    return bool(ADMIN_LOGIN and ADMIN_PASSWORD and ADMIN_TOTP)


def ip_ok(req: Request) -> bool:
    return client_ip(req) == ADMIN_IP


def session_state(req: Request) -> str:
    ip = client_ip(req)
    for name, kind in (("hadm", "full"), ("hpre", "pre")):
        d = unsign(req.cookies.get(name, ""))
        if d and d.get("t") == kind and d.get("ip") == ip:
            return kind
    return "none"


def guard(req: Request, full: bool = True):
    if not ip_ok(req):
        raise HTTPException(404)
    if full and session_state(req) != "full":
        raise HTTPException(401)


def put_cookie(resp, req: Request, name: str, value: str, max_age: int):
    secure = (req.headers.get("x-forwarded-proto") or req.url.scheme) == "https"
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="strict", secure=secure, path="/")


_fails: dict[str, list] = {}   # ip -> [число ошибок, заблокирован до, начало окна]


def is_locked(ip: str) -> bool:
    f = _fails.get(ip)
    return bool(f and f[1] > time.time())


def add_fail(ip: str):
    now = time.time()
    f = _fails.get(ip)
    if not f or now - f[2] > 900:
        f = [0, 0, now]
    f[0] += 1
    if f[0] >= 5:
        f[1] = now + 900
    _fails[ip] = f


def admin_page(req: Request):
    if not ip_ok(req):
        return RedirectResponse(main_url(req), status_code=302)
    return FileResponse(BASE / "admin" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/admin")
async def admin_route(request: Request):
    return admin_page(request)


@app.get("/admin/")
async def admin_route_slash(request: Request):
    return admin_page(request)


class Login(BaseModel):
    login: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=200)


class Code(BaseModel):
    code: str = Field(default="", max_length=20)


@app.get("/admin/api/session")
async def admin_session(request: Request):
    guard(request, full=False)
    return {"state": session_state(request), "enabled": admin_enabled(), "tz": ADMIN_TZ}


@app.post("/admin/api/login")
async def admin_login(request: Request, body: Login):
    guard(request, full=False)
    ip = client_ip(request)
    if not admin_enabled():
        return JSONResponse({"error": "Админка не настроена: задайте ADMIN_LOGIN, ADMIN_PASSWORD и ADMIN_TOTP_SECRET."}, status_code=503)
    if is_locked(ip):
        return JSONResponse({"error": "Слишком много попыток. Подождите 15 минут."}, status_code=429)
    ok = hmac.compare_digest(body.login.encode(), ADMIN_LOGIN.encode()) & \
        hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode())
    if not ok:
        add_fail(ip)
        await asyncio.sleep(0.7)
        return JSONResponse({"error": "Неверный логин или пароль."}, status_code=401)
    resp = JSONResponse({"ok": True, "step": "2fa"})
    put_cookie(resp, request, "hpre", sign({"t": "pre", "ip": ip, "exp": time.time() + 300}), 300)
    return resp


@app.post("/admin/api/2fa")
async def admin_2fa(request: Request, body: Code):
    guard(request, full=False)
    ip = client_ip(request)
    if session_state(request) != "pre":
        return JSONResponse({"error": "Сначала введите логин и пароль."}, status_code=401)
    if is_locked(ip):
        return JSONResponse({"error": "Слишком много попыток. Подождите 15 минут."}, status_code=429)
    if not verify_totp(body.code):
        add_fail(ip)
        await asyncio.sleep(0.7)
        return JSONResponse({"error": "Неверный код."}, status_code=401)
    resp = JSONResponse({"ok": True})
    put_cookie(resp, request, "hadm", sign({"t": "full", "ip": ip, "exp": time.time() + 8 * 3600}), 8 * 3600)
    resp.delete_cookie("hpre", path="/")
    return resp


@app.post("/admin/api/logout")
async def admin_logout(request: Request):
    guard(request, full=False)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("hadm", path="/")
    resp.delete_cookie("hpre", path="/")
    return resp


def day_bounds():
    d0 = datetime.now(ZoneInfo(ADMIN_TZ)).replace(hour=0, minute=0, second=0, microsecond=0)
    return d0, d0 - timedelta(days=6), d0 - timedelta(days=29)   # сегодня, 7 дней, 30 дней


def fill_days(rows, d29):
    m = {r["d"]: r["c"] for r in rows}
    return [{"d": (d29 + timedelta(days=i)).date().isoformat(),
             "v": int(m.get((d29 + timedelta(days=i)).date(), 0))} for i in range(30)]


@app.get("/admin/api/online")
async def admin_online(request: Request):
    guard(request)
    n = await pool.fetchval("select count(distinct tg_id) from sessions where last_seen > now() - interval '45 seconds'")
    return {"online": n}


@app.get("/admin/api/stats")
async def admin_stats(request: Request):
    guard(request)
    d0, d6, d29 = day_bounds()
    tz = ADMIN_TZ
    cnt = """select count(*) as total,
                    count(*) filter (where created_at >= $1) as today,
                    count(*) filter (where created_at >= $2) as week,
                    count(*) filter (where created_at >= $3) as month from {t}"""
    daily = "select (created_at at time zone $2)::date d, count({c}) c from {t} where created_at >= $1 group by 1"
    (users, reqs, vis, sess, online, funnel, clicks, u_d, r_d, v_d, hours, devices, tool_rows) = await asyncio.gather(
        pool.fetchrow(cnt.format(t="users"), d0, d6, d29),
        pool.fetchrow(cnt.format(t="join_requests"), d0, d6, d29),
        pool.fetchrow("""select count(*) visits, count(distinct tg_id) uniq,
                                count(*) filter (where created_at >= $1) visits_today,
                                count(distinct tg_id) filter (where created_at >= $1) uniq_today,
                                count(distinct tg_id) filter (where created_at >= $2) uniq_week,
                                count(distinct tg_id) filter (where created_at >= $3) uniq_month from visits""", d0, d6, d29),
        pool.fetchrow("""select count(*) n,
                                coalesce(avg(extract(epoch from last_seen - started_at)), 0) avg_all,
                                coalesce(avg(extract(epoch from last_seen - started_at)) filter (where started_at >= $1), 0) avg_today
                         from sessions""", d0),
        pool.fetchval("select count(distinct tg_id) from sessions where last_seen > now() - interval '45 seconds'"),
        pool.fetchrow("""select (select count(*) from users) users,
                                (select count(distinct tg_id) from join_requests where tg_id in (select tg_id from users)) req,
                                (select count(distinct tg_id) from visits) site,
                                (select count(distinct tg_id) from events where ev = 'select') tool,
                                (select count(distinct tg_id) from events where ev = 'bot') bot"""),
        pool.fetchrow("""select count(*) filter (where ev = 'bot') bot, count(*) filter (where ev = 'eternal') eternal,
                                count(*) filter (where ev = 'select') sel from events"""),
        pool.fetch(daily.format(t="users", c="*"), d29, tz),
        pool.fetch(daily.format(t="join_requests", c="*"), d29, tz),
        pool.fetch(daily.format(t="visits", c="distinct tg_id"), d29, tz),
        pool.fetch("""select extract(hour from created_at at time zone $2)::int h, count(*) c
                      from visits where created_at >= $1 group by 1""", d29, tz),
        pool.fetch("""select case when user_agent ~* 'mobile|android|iphone|ipad' then 'Телефон / планшет' else 'Компьютер' end dev,
                             count(*) c from sessions group by 1"""),
        pool.fetch("""select tool, count(*) filter (where ev = 'select') sel, count(*) filter (where ev = 'bot') bot,
                             count(*) filter (where ev = 'eternal') eternal,
                             count(distinct tg_id) filter (where ev = 'select') people
                      from events where tool is not null group by tool
                      order by sel desc, bot desc limit 15"""),
    )
    hrs = [0] * 24
    for r in hours:
        hrs[r["h"]] = r["c"]
    return jsonable_encoder({
        "tz": tz,
        "users": dict(users), "requests": dict(reqs), "site": dict(vis), "sessions": dict(sess),
        "online": online, "funnel": dict(funnel), "clicks": dict(clicks),
        "users_daily": fill_days(u_d, d29), "requests_daily": fill_days(r_d, d29),
        "visitors_daily": fill_days(v_d, d29), "hours": hrs,
        "devices": [dict(r) for r in devices], "tools": [dict(r) for r in tool_rows],
    })


IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
USER_COLS = """u.tg_id, u.username, u.full_name, u.created_at,
               (select count(*) from visits v where v.tg_id = u.tg_id) visits,
               (select max(created_at) from visits v where v.tg_id = u.tg_id) as last_visit"""


@app.get("/admin/api/search")
async def admin_search(request: Request, q: str = ""):
    guard(request)
    q = q.strip().lstrip("@")[:100]
    if not q:
        return {"items": []}
    by_ip = """u.tg_id in (select tg_id from visits where ip {op} $1 union select tg_id from sessions where ip {op} $1
                           union select tg_id from events where ip {op} $1)"""
    if IPV4.match(q) or ":" in q:
        rows = await pool.fetch(f"select {USER_COLS} from users u where {by_ip.format(op='=')} "
                                "order by last_visit desc nulls last limit 30", q)
    elif re.fullmatch(r"[\d.]+", q) and "." in q:                       # часть IP, например 91.224.
        rows = await pool.fetch(f"select {USER_COLS} from users u where {by_ip.format(op='like')} "
                                "order by last_visit desc nulls last limit 30", q + "%")
    elif q.isdigit():
        rows = await pool.fetch(f"select {USER_COLS} from users u where u.tg_id = $1", int(q))
    else:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = await pool.fetch(f"select {USER_COLS} from users u where u.username ilike $1 or u.full_name ilike $2 "
                                "order by u.created_at desc limit 30", esc + "%", "%" + esc + "%")
    return jsonable_encoder({"items": [dict(r) for r in rows]})


@app.get("/admin/api/user/{tg_id}")
async def admin_user(request: Request, tg_id: int):
    guard(request)
    u = await pool.fetchrow("select tg_id, full_name, username, created_at from users where tg_id = $1", tg_id)
    if not u:
        raise HTTPException(404)
    (jr, vis, ses, ips, sessions, tools_, events) = await asyncio.gather(
        pool.fetchval("select min(created_at) from join_requests where tg_id = $1", tg_id),
        pool.fetchrow("select count(*) as n, min(created_at) as first, max(created_at) as last from visits where tg_id = $1", tg_id),
        pool.fetchrow("""select count(*) n, coalesce(sum(extract(epoch from last_seen - started_at)), 0) total,
                                coalesce(avg(extract(epoch from last_seen - started_at)), 0) avg
                         from sessions where tg_id = $1""", tg_id),
        pool.fetch("""select ip, count(*) as n, max(created_at) as last from visits where tg_id = $1
                      group by ip order by last desc limit 20""", tg_id),
        pool.fetch("""select started_at, extract(epoch from last_seen - started_at) secs, ip, user_agent
                      from sessions where tg_id = $1 order by started_at desc limit 15""", tg_id),
        pool.fetch("""select tool, count(*) filter (where ev = 'select') sel, count(*) filter (where ev = 'bot') bot,
                             count(*) filter (where ev = 'eternal') as eternal, max(created_at) as last
                      from events where tg_id = $1 and tool is not null group by tool order by last desc""", tg_id),
        pool.fetch("select ev, tool, created_at from events where tg_id = $1 order by created_at desc limit 30", tg_id),
    )
    return jsonable_encoder({
        "user": dict(u), "join_request_at": jr, "visits": dict(vis), "sessions_stat": dict(ses),
        "ips": [dict(r) for r in ips], "sessions": [dict(r) for r in sessions],
        "tools": [dict(r) for r in tools_], "events": [dict(r) for r in events],
    })


# ====================== страницы и поддомены ======================
@app.middleware("http")
async def subdomain_pages(request: Request, call_next):
    # about.<домен> и admin.<домен> (когда подключён свой домен)
    host = request.headers.get("host", "").split(":")[0]
    if request.url.path == "/":
        if host.startswith("about."):
            return FileResponse(BASE / "static" / "about.html")
        if host.startswith("admin."):
            return admin_page(request)
    return await call_next(request)


@app.get("/about")
async def about_page():
    return FileResponse(BASE / "static" / "about.html")


@app.get("/collab")
async def collab_page():
    return FileResponse(BASE / "static" / "collab.html")


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
