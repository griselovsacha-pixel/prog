import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import struct
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi import Path as ApiPath
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import ai_assistant as ai
import alerts
import bot_features as bf
import catalog as cat
import monitor as mon
import security as sec

logging.basicConfig(level=logging.INFO)
sec.install_log_masking()   # токены доступа (?k=…) и токен бота не попадают в логи
alerts.install_log_handler()   # ошибки уровня ERROR из любых логов приходят владельцу в личку бота
log = logging.getLogger("harledy")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]  # Supabase pooler URL (порт 6543)
SITE_URL = (os.environ.get("SITE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
DB_SSL = os.environ.get("DB_SSL", "require").strip().lower()
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/+X8s2DkoSD7M3MTc6")
DEV_LINK = os.environ.get("DEV_LINK", "https://t.me/SexHarledy")      # кнопка «Контакт разработчика» на /ai
CHANNEL_ID = int(os.environ.get("CHANNEL_ID") or -1004319976611)  # переменная окружения, если задана, главнее
BASE = Path(__file__).parent
# Telegram ID владельцев: только им бот отвечает ID канала на пересланный пост
ADMIN_TG_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_TG_IDS", ""))}

# заявки «Добавить свой инструмент» приходят этому Telegram ID; Crypto Pay — токен только из переменных окружения
TOOL_REVIEW_CHAT = int(os.environ.get("TOOL_REVIEW_CHAT") or 8521863420)
# куда слать алерты (ошибки, входы в админку, баны, новые пользователи …); по умолчанию — владельцу
ALERT_CHAT = int(os.environ.get("ALERT_CHAT_ID") or TOOL_REVIEW_CHAT)
try:
    DIGEST_HOUR = int(os.environ.get("ALERT_DIGEST_HOUR", "9"))
except ValueError:
    DIGEST_HOUR = 9
CRYPTO_PAY_TOKEN = os.environ.get("CRYPTO_PAY_TOKEN", "").strip()
CRYPTO_PAY_HOST = os.environ.get("CRYPTO_PAY_HOST", "https://pay.crypt.bot").strip()   # для тестов: https://testnet-pay.crypt.bot

# Каталог живёт в базе (таблица catalog_nodes). Файл ниже нужен только для первого переноса в пустую базу
# и как запасной вариант, если база недоступна при запуске: переменная TOOLS_FILE, секретный файл Render, либо data/tools.json
TOOLS_PATHS = [p for p in (os.environ.get("TOOLS_FILE"), "/etc/secrets/tools.json", str(BASE / "data" / "tools.json")) if p]


def parse_nets(raw: str) -> list:
    out = []
    for part in re.split(r"[,\s]+", raw.strip()):
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                logging.getLogger("harledy").warning("ADMIN_IP: %r не похоже на IP или подсеть, пропущено", part)
    return out


# --- админка (всё секретное только из переменных окружения) ---
# IP-адрес(а) админа: один IP, подсеть или список через запятую. Значения по умолчанию НЕТ:
# пока ADMIN_IP не задан, админка закрыта для всех.
ADMIN_NETS = parse_nets(os.environ.get("ADMIN_IP", ""))
sec.EXEMPT_NETS = ADMIN_NETS
ADMIN_LOGIN = os.environ.get("ADMIN_LOGIN", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_TOTP = os.environ.get("ADMIN_TOTP_SECRET", "").replace(" ", "").upper()
ADMIN_TZ = os.environ.get("ADMIN_TZ", "Europe/Kyiv")
# Ключ подписи cookie. Не выводится из пароля: либо задан явно (≥32 символов), либо случайный на запуск.
_secret_env = os.environ.get("ADMIN_SESSION_SECRET", "")
SESSION_SECRET = (_secret_env if len(_secret_env) >= 32 else secrets.token_urlsafe(48)).encode()

# custom emoji
EMOJI_MAIN = "5217848986433502482"
EMOJI_LINK = "5193202823411546657"
EMOJI_GATE = "6021409772350215140"
ICON_CHANNEL = "6021452855167164269"
ICON_DONE = "6021868492037298942"
EMOJI_TERMS = "5881702736843511327"      # текст сообщения с соглашением
ICON_TERMS = "5402398388203305953"       # кнопка «Пользовательское соглашение»
ICON_AGREE = "5357069174512303778"       # кнопка «Я ознакомился и со всем согласен»

SCHEMA = """
create table if not exists users(
    tg_id bigint primary key,
    full_name text,
    username text,
    token text unique not null,
    created_at timestamptz not null default now()
);
alter table users add column if not exists terms_accepted_at timestamptz;
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
create table if not exists ratings(
    tg_id bigint not null,
    tool text not null,
    stars smallint not null check (stars between 1 and 5),
    updated_at timestamptz not null default now(),
    primary key (tg_id, tool)
);
create index if not exists ratings_tool_idx on ratings(tool);
create table if not exists tool_status(
    tool text primary key,
    kind text not null,
    state text not null,
    message text not null,
    code int,
    fails int not null default 0,
    checked_at timestamptz not null default now(),
    ok_at timestamptz
);
alter table tool_status enable row level security;
alter table users enable row level security;
alter table ratings enable row level security;
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


def who(u) -> str:
    return f"{u.full_name}" + (f" (@{u.username})" if u.username else "") + f" · ID {u.id}"


async def ensure_user(u) -> str:
    """Создаёт юзера при первом контакте. Токен ссылки генерируется один раз и больше не меняется."""
    row = await pool.fetchrow(
        """insert into users(tg_id, full_name, username, token)
           values($1, $2, $3, $4)
           on conflict (tg_id) do update
             set full_name = excluded.full_name, username = excluded.username
           returning token, (xmax = 0) as is_new""",
        u.id, u.full_name, u.username, secrets.token_urlsafe(18),
    )
    if row["is_new"]:
        alerts.notify("user", "Новый пользователь", who(u))
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
    await send(                                        # одно сообщение вместо двух — вдвое меньше круговых задержек до Telegram
        bot, chat_id,
        f"{emoji(EMOJI_GATE, '⚠️')} Для использования бота необходима подписка на канал!",
        kb=gate_kb(True), kb_plain=gate_kb(False),
    )


def menu_kb(link: str, icons: bool) -> InlineKeyboardMarkup:
    def btn(text, icon, **kw):
        extra = {"icon_custom_emoji_id": icon} if icons else {}
        return InlineKeyboardButton(text=text, **kw, **extra)

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть Harledy Project", url=link)],
        [btn("Добавить свой инструмент", bf.BTN["add"], callback_data="addtool")],
        [btn("Поддержать проект", bf.BTN["support"], callback_data="support")],
    ])


async def menu_parts(u) -> tuple[str, str]:
    """Текст приветствия и персональная ссылка на сайт (одно и то же для /start и кнопки «Вернуться в главное меню»)."""
    token = await ensure_user(u)
    link = f"{SITE_URL}/?k={token}"
    name = html.escape(f"@{u.username}" if u.username else u.full_name)
    text = (f"Приветствую, {name}!\n"
            f"Сборник инструментов Harledy Project доступен по ссылке ниже {emoji(EMOJI_LINK, '👇')}\n\n"
            "<i>Сайт желательно открывать с компьютера, так как мобильная версия может работать некорректно.</i>")
    return text, link


async def send_menu(bot: Bot, u):
    text, link = await menu_parts(u)
    try:                                               # большой эмодзи отдельным сообщением — как стикер
        await bot.send_message(u.id, emoji(EMOJI_MAIN, "🔍"))
    except TelegramBadRequest:
        await bot.send_message(u.id, "🔍")
    await send(bot, u.id, text, kb=menu_kb(link, True), kb_plain=menu_kb(link, False))



# ---- пользовательское соглашение: без него не работает ни бот, ни каталог на сайте
_terms_ok = sec.Bounded(50000)     # tg_id тех, кто уже принял (принятие не отзывается — кэшируем навсегда)


async def has_terms(uid: int) -> bool:
    if uid in _terms_ok:
        return True
    if await pool.fetchval("select 1 from users where tg_id = $1 and terms_accepted_at is not null", uid):
        _terms_ok.put(uid, True)
        return True
    return False


async def has_full_access(bot: Bot, uid: int) -> bool:
    """Соглашение принято И есть доступ по каналу."""
    return await has_terms(uid) and await has_access(bot, uid)


def terms_kb(icons: bool) -> InlineKeyboardMarkup:
    def btn(text, icon, fb, **kw):
        if icons:
            return InlineKeyboardButton(text=text, icon_custom_emoji_id=icon, **kw)
        return InlineKeyboardButton(text=f"{fb} {text}", **kw)

    rows = []
    if SITE_URL:
        rows.append([btn("Пользовательское соглашение", ICON_TERMS, "📄", url=f"{SITE_URL}/terms")])
    rows.append([btn("Я ознакомился и со всем согласен", ICON_AGREE, "✅", callback_data="accept_terms")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_terms(bot: Bot, chat_id: int):
    await send(
        bot, chat_id,
        f"{emoji(EMOJI_TERMS, '📜')} <i>Перед началом пользования ботом ознакомьтесь с пользовательским соглашением. "
        "Нажав кнопку «Я ознакомлен и согласен», вы подтверждаете, что полностью ознакомлены "
        "и согласны с нашими правилами.</i>",
        kb=terms_kb(True), kb_plain=terms_kb(False),
    )


async def proceed(bot: Bot, u, chat_id: int):
    """Следующий шаг после соглашения: меню (есть доступ) или просьба подписаться."""
    if await has_access(bot, u.id):
        await send_menu(bot, u)
    else:
        await send_gate(bot, chat_id)


@router.message(CommandStart())
async def on_start(m: Message, state: FSMContext):
    await state.clear()                       # /start сбрасывает незаконченный диалог «Добавить инструмент»
    await ensure_user(m.from_user)  # ссылка создаётся сразу, при первом контакте
    if not await has_terms(m.from_user.id):
        return await send_terms(m.bot, m.chat.id)
    await proceed(m.bot, m.from_user, m.chat.id)


STATUS_ADMINS = ADMIN_TG_IDS | {TOOL_REVIEW_CHAT}
_bg_tasks: set = set()


@router.message(Command("check"))
async def on_check_status(m: Message):
    """Владелец: /check — внеочередная проверка всех ссылок каталога (отчёт придёт сюда же)."""
    if not m.from_user or m.from_user.id not in STATUS_ADMINS:
        return
    if mon.is_running():
        return await m.answer("Проверка уже идёт, отчёт придёт сюда.")
    await m.answer("Запустил проверку каталога. Боты жду до 5 минут, потом пришлю отчёт.")
    t = asyncio.create_task(mon.run(pool, tools_json(), bot=m.bot, report_chat=m.chat.id))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


@router.callback_query(F.data == "accept_terms")
async def on_accept_terms(cb: CallbackQuery):
    await ensure_user(cb.from_user)
    res = await pool.execute(
        "update users set terms_accepted_at = now() where tg_id = $1 and terms_accepted_at is null", cb.from_user.id)
    if res.endswith(" 1"):
        alerts.notify("terms", "Принял пользовательское соглашение", who(cb.from_user))
    _terms_ok.put(cb.from_user.id, True)
    await cb.answer("Спасибо! Соглашение принято.")
    try:
        await cb.message.delete()
    except TelegramAPIError:
        pass
    await proceed(cb.bot, cb.from_user, cb.from_user.id)


@router.callback_query(F.data == "check_sub")
async def on_check(cb: CallbackQuery):
    await ensure_user(cb.from_user)
    if not await has_terms(cb.from_user.id):          # старое сообщение с кнопкой «Я подписался»
        await cb.answer()
        return await send_terms(cb.bot, cb.from_user.id)
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
    """Владелец пересылает боту пост из канала — бот покажет ID канала для CHANNEL_ID.
    Остальным не отвечает; пока ADMIN_TG_IDS не задан, функция выключена."""
    if m.from_user and m.from_user.id in ADMIN_TG_IDS:
        await m.answer(f"ID канала: <code>{m.forward_origin.chat.id}</code>")


class BotThrottle(BaseMiddleware):
    """Не даёт одному пользователю заваливать бота (и базу) запросами: не чаще одного события в 0.7 с.
    Заявки в канал и служебные события не отбрасываем никогда: потерянная заявка = человек навсегда у закрытых ворот."""

    def __init__(self):
        self.last = sec.Bounded(20000)

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and not (event.chat_join_request or event.my_chat_member or event.chat_member):
            now = time.monotonic()
            if now - self.last.get(user.id, -1e9) < 0.7:
                if event.callback_query:                 # кнопка не должна «висеть» с крутилкой
                    try:
                        await event.callback_query.answer()
                    except TelegramAPIError:
                        pass
                return None
            self.last.put(user.id, now)
        return await handler(event, data)


@router.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    link = req.invite_link.invite_link if req.invite_link else None
    log.info("join request: user=%s chat_id=%s title=%r link=%s", req.from_user.id, req.chat.id, req.chat.title, link)
    if not is_our_channel(req.chat.id, link):
        return
    res = await pool.execute(
        "insert into join_requests(tg_id, chat_id) values($1, $2) on conflict do nothing",
        req.from_user.id, req.chat.id,
    )
    if res.endswith(" 1"):
        alerts.notify("join", "Заявка в канал", who(req.from_user))


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
                alerts.notify("db", "Сайт не отвечает на собственный /ping", type(e).__name__, key="selfping", cooldown=3600)


async def status_loop(bot: Bot):
    """Раз в сутки проверяет живость сайтов и ботов. Что уже проверено недавно (в т.ч. до перезапуска Render), пропускает."""
    await asyncio.sleep(120)
    while True:
        try:
            await mon.run(pool, tools_json(), bot=bot, report_chat=TOOL_REVIEW_CHAT, only_due=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("проверка каталога не удалась: %s", e)
            alerts.notify("error", "Автопроверка каталога упала", type(e).__name__, key="statusloop", cooldown=3600)
        await asyncio.sleep(900)


async def health_loop():
    """Раз в минуту проверяем базу. Алерт только после двух подряд одинаковых результатов — одиночный сбой не шумит."""
    state, streak = True, 0
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.wait_for(pool.fetchval("select 1"), 8)
            good, err = True, ""
        except asyncio.CancelledError:
            raise
        except Exception as e:
            good, err = False, type(e).__name__
        streak = streak + 1 if good != state else 0
        if streak >= 2:
            state, streak = good, 0
            alerts.notify("db", "База данных снова доступна" if good else "База данных недоступна", err)


async def digest_loop():
    """Раз в сутки (ALERT_DIGEST_HOUR по ADMIN_TZ) — короткая сводка: люди, заходы, ошибки, баны."""
    if DIGEST_HOUR < 0:
        return
    while True:
        tz = ZoneInfo(ADMIN_TZ)
        now = datetime.now(tz)
        nxt = now.replace(hour=min(DIGEST_HOUR, 23), minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            r = await pool.fetchrow(
                """select (select count(*) from users) total,
                          (select count(*) from users where created_at > now() - interval '24 hours') new_users,
                          (select count(*) from join_requests where created_at > now() - interval '24 hours') reqs,
                          (select count(distinct tg_id) from visits where created_at > now() - interval '24 hours') visitors,
                          (select count(*) from events where ev = 'select' and created_at > now() - interval '24 hours') opens,
                          (select count(*) from events where ev = 'bot' and created_at > now() - interval '24 hours') bots,
                          (select count(*) from ratings where updated_at > now() - interval '24 hours') rates""")
            st = alerts.take_stats()
            alerts.notify("digest", "Сводка за сутки",
                          f"Пользователей всего: {r['total']} (новых: {r['new_users']})\n"
                          f"Заявок в канал: {r['reqs']}\nУникальных заходов на сайт: {r['visitors']}\n"
                          f"Открыто инструментов: {r['opens']} · переходов в ботов: {r['bots']} · оценок: {r['rates']}\n"
                          f"Ошибок: {st.get('error', 0)} · банов: {st.get('ban', 0)} · "
                          f"чужих попыток в админку: {st.get('probe', 0)}")
        except Exception as e:
            log.warning("суточная сводка не собралась: %s", e)
        await asyncio.sleep(60)


async def catalog_loop():
    """Раз в 20 секунд смотрим, не правили ли каталог напрямую в Supabase (запрос на 1 строку)."""
    while True:
        await asyncio.sleep(20)
        if pool is not None:
            await cat.poll(pool)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    # command_timeout: зависший запрос к базе не держит соединения вечно
    # TLS к базе включён всегда (отключить можно только явно: DB_SSL=disable); sslmode из самой ссылки главнее
    ssl_kw = {} if ("sslmode=" in DATABASE_URL or DB_SSL == "disable") else {"ssl": "require"}
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0,
                                     command_timeout=10, timeout=15, **ssl_kw)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
    try:                                  # каталог: таблица, перенос из tools.json в пустую базу, загрузка в память
        await cat.init(pool, tools_file_json())
    except Exception as e:
        log.error("каталог из базы не загружен, работаем с файлом: %s", e)

    if not ADMIN_NETS:
        log.warning("ADMIN_IP не задан — админка закрыта для всех")
    elif not admin_enabled():
        log.warning("Админка не настроена: нужны ADMIN_LOGIN, ADMIN_PASSWORD, ADMIN_TOTP_SECRET")
    if len(_secret_env) < 32:
        log.warning("ADMIN_SESSION_SECRET не задан (или короче 32 символов): сессии админа сбрасываются при перезапуске")
    if not sec.ORIGIN_SECRET:
        log.info("ORIGIN_SECRET не задан: нужен только когда сайт стоит за вашим Cloudflare (см. SECURITY.md)")
    if not sec.ALLOWED_HOSTS:
        log.warning("ALLOWED_HOSTS не задан: проверка заголовка Host выключена (см. SECURITY.md)")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    alerts.start(bot, ALERT_CHAT, ADMIN_TZ)
    problems = []
    if not ADMIN_NETS:
        problems.append("ADMIN_IP не задан: админка закрыта для всех")
    elif not admin_enabled():
        problems.append("админка не настроена (ADMIN_LOGIN / ADMIN_PASSWORD / ADMIN_TOTP_SECRET)")
    if len(_secret_env) < 32:
        problems.append("ADMIN_SESSION_SECRET не задан: сессия админа сбрасывается при перезапуске")
    if not sec.ALLOWED_HOSTS:
        problems.append("ALLOWED_HOSTS не задан: проверка Host выключена")
    if not SITE_URL:
        problems.append("SITE_URL не задан")
    alerts.notify("start", "Сервер запущен",
                  "Алерты работают.\n" + ("\n".join("⚠️ " + p for p in problems) if problems else "Настройки безопасности в порядке."))
    sec.ON_BAN = lambda key, kind, off, secs: alerts.notify(
        "ban", "Заблокирован адрес" + (": перебор токенов доступа" if kind == "token" else ""),
        f"{key}\nпричина: {kind} · нарушение №{off} · на {secs // 60} мин", key=key, cooldown=600)
    dp = Dispatcher()
    dp.update.outer_middleware(BotThrottle())
    dp.include_router(router)
    dp.include_router(bf.router)
    bf.configure(has_access=has_full_access, tools_json=tools_json, admin_id=TOOL_REVIEW_CHAT, channel_id=CHANNEL_ID,
                 channel_link=CHANNEL_LINK, menu_parts=menu_parts, menu_kb=menu_kb, crypto_token=CRYPTO_PAY_TOKEN, crypto_host=CRYPTO_PAY_HOST)
    try:                                  # сбой Telegram при старте не должен ронять сайт: поллинг сам переподключится
        await bot.delete_webhook(drop_pending_updates=True)
        await diagnose(bot)
    except Exception as e:
        log.warning("Telegram недоступен при старте, продолжаем без проверки: %s", e)
    tasks = [
        asyncio.create_task(dp.start_polling(bot, handle_signals=False)),
        asyncio.create_task(self_ping()),
        asyncio.create_task(status_loop(bot)),
        asyncio.create_task(catalog_loop()),
        asyncio.create_task(health_loop()),
        asyncio.create_task(digest_loop()),
    ]
    yield
    alerts.notify("stop", "Сервер останавливается", "Перезапуск или деплой на Render.")
    await alerts.stop()                  # дослать очередь до закрытия сессии бота
    await assistant.aclose()
    for t in tasks:
        t.cancel()
    await bot.session.close()
    await pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Самый внешний слой: лимиты, баны, проверка Host/секрета Cloudflare, размер тела, таймаут, заголовки безопасности
app.add_middleware(sec.Shield)


# ИИ-ассистент (страница /ai): ключи и список моделей — только из переменных окружения GEMINI_API_KEY(S), GEMINI_MODELS
assistant = ai.Assistant(channel_link=CHANNEL_LINK)
AI_SLOTS = asyncio.Semaphore(6)      # одновременно не больше 6 запросов к нейросети


# ====================== общее для веба ======================
def client_ip(req: Request) -> str:
    """IP клиента. Считается один раз в sec.Shield: подделанные CF-Connecting-IP / True-Client-IP
    и левые записи X-Forwarded-For игнорируются (см. PROXY_HOPS и IP_HEADER в SECURITY.md)."""
    return req.scope.get("_cip") or sec.ip_from_scope(req.scope)


def main_url(req: Request) -> str:
    if SITE_URL:                      # адрес сайта берём из настроек, а не из заголовка Host
        return SITE_URL + "/"
    host = re.sub(r"[^A-Za-z0-9.:\-]", "", req.headers.get("host", ""))
    if host.startswith(("admin.", "about.")):
        host = host.split(".", 1)[1]
    return f"https://{host}/"


@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    return {"ok": True}


# ---- доступ по персональному токену из бота
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")      # токен бота: secrets.token_urlsafe(18) = 24 символа
_tok_cache = sec.Bounded(5000)       # токен -> (tg_id, годен до)
_bad_tok = sec.Bounded(5000)         # неверный токен -> до какого момента не ходим в базу
TOK_TOOLS = sec.Limiter(10, 60)      # на один токен: список инструментов
TOK_TRACK = sec.Limiter(40, 60)      # на один токен: аналитика
NEW_SID = sec.Limiter(12, 3600)      # на один токен: новых сессий в час
_sid_owner = sec.Bounded(20000)      # sid -> tg_id
_last_visit = sec.Bounded(20000)     # tg_id -> когда писали визит
TOK_EVENTS = sec.Limiter(60, 600)    # на один токен: записей действий в базу за 10 минут (сверх лимита молча отбрасываем)
TOK_RATE = sec.Limiter(20, 600)      # на один токен: оценок инструментов за 10 минут
TOK_AI = sec.Limiter(8, 60)          # на один токен: вопросов ИИ-ассистенту в минуту
TOK_AI_DAY = sec.Limiter(150, 86400) # на один токен: вопросов ИИ в сутки (защита квоты Gemini)
TOK_RATINGS = sec.Limiter(20, 60)    # на один токен: чтение сводного рейтинга
TOK_STATUS = sec.Limiter(20, 60)     # на один токен: чтение статусов инструментов
DB_LOOKUPS = sec.Limiter(300, 60)    # общий потолок поисков НЕИЗВЕСТНЫХ токенов в базе: ботнет с тысячами IP не завалит базу
NOSTORE = {"Cache-Control": "no-store"}


async def resolve_token(k: str, ip: str = "") -> tuple[int | None, bool]:
    """(tg_id, принято ли соглашение). tg_id = None — токен неверный."""
    # неправильный формат или заведомо чужой токен — в базу не ходим; повторные попытки ведут к бану
    if not k or not TOKEN_RE.fullmatch(k):
        sec.BANS.strike(sec.rl_key(ip), "token", 10, 300)
        return None, False
    now = time.monotonic()
    hit = _tok_cache.get(k)
    if hit and hit[2] > now:
        return hit[0], hit[1]
    if _bad_tok.get(k, 0) > now:
        return None, False
    if not DB_LOOKUPS.hit("db"):          # знакомые токены (кэш выше) проходят всегда, новые — в пределах бюджета
        raise HTTPException(429, headers={"Retry-After": "20"})
    row = await pool.fetchrow("select tg_id, terms_accepted_at is not null as ok from users where token = $1", k)
    if not row:
        _bad_tok.put(k, now + 60)
        sec.BANS.strike(sec.rl_key(ip), "token", 10, 300)
        return None, False
    # принявшего кэшируем надолго; не принявшего — на пару секунд, чтобы после кнопки в боте сайт открылся сразу
    _tok_cache.put(k, (row["tg_id"], row["ok"], now + (600 if row["ok"] else 3)))
    return row["tg_id"], row["ok"]


async def uid_by_token(k: str, ip: str = "") -> int | None:
    """tg_id только если токен верный И пользователь принял соглашение — иначе API закрыт."""
    uid, ok = await resolve_token(k, ip)
    return uid if ok else None


def access_key(request: Request, fallback: str = "") -> str:
    """Токен берём из заголовка X-Access-Key (новый клиент) или из ?k= / тела запроса (старые ссылки)."""
    return (request.headers.get("x-access-key") or fallback or "").strip()


# ---- файл со списком инструментов: только для первого переноса в базу и как запасной вариант
_tools_cache = {"body": None, "path": None, "mtime": None, "checked": 0.0}


def tools_file_json() -> bytes | None:
    now = time.monotonic()
    if _tools_cache["body"] is not None and now - _tools_cache["checked"] < 30:
        return _tools_cache["body"]
    _tools_cache["checked"] = now
    for p in TOOLS_PATHS:
        try:
            mtime = os.stat(p).st_mtime_ns
        except OSError:
            continue
        if _tools_cache["path"] == p and _tools_cache["mtime"] == mtime and _tools_cache["body"] is not None:
            return _tools_cache["body"]
        try:
            data = json.loads(Path(p).read_text("utf-8"))
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        except Exception as e:
            log.error("не удалось прочитать список инструментов %s: %s", p, e)
            continue
        _tools_cache.update(body=body, path=p, mtime=mtime)
        return body
    return _tools_cache["body"]


def tools_json() -> bytes | None:
    """Каталог для сайта, ИИ, бота и проверки статусов: готовые байты из памяти (собраны из базы), в базу на запрос не ходим."""
    return cat.body() or tools_file_json()


@app.get("/api/tools")
async def tools(request: Request, k: str = ""):
    ip = client_ip(request)
    key = access_key(request, k)
    uid, terms_ok = await resolve_token(key, ip)
    if not uid:
        return JSONResponse({"error": "forbidden"}, status_code=403, headers=NOSTORE)
    if not terms_ok:
        return JSONResponse({"error": "terms"}, status_code=403, headers=NOSTORE)
    if not TOK_TOOLS.hit(key):
        return JSONResponse({"error": "rate"}, status_code=429, headers={**NOSTORE, "Retry-After": "30"})
    now = time.monotonic()
    if now - _last_visit.get(uid, -1e9) > 60:          # визит пишем не чаще раза в минуту на человека
        _last_visit.put(uid, now)
        try:
            await pool.execute(
                "insert into visits(tg_id, ip, user_agent) values($1, $2, $3)",
                uid, ip, request.headers.get("user-agent", "")[:300],
            )
        except Exception as e:                           # сбой статистики не должен ронять сайт
            log.warning("visit insert failed: %s", e)
            alerts.notify("db", "Не удалось записать визит в базу", type(e).__name__, key="visit", cooldown=600)
    body = tools_json()
    if body is None:
        return JSONResponse({"error": "unavailable"}, status_code=503, headers=NOSTORE)
    return Response(body, media_type="application/json", headers=NOSTORE)


class AiMsg(BaseModel):
    role: str = Field(max_length=10)
    text: str = Field(max_length=2000)


class AiReq(BaseModel):
    q: str = Field(min_length=1, max_length=ai.MAX_QUESTION)
    history: list[AiMsg] = Field(default_factory=list, max_length=12)


@app.post("/api/ai")
async def ai_ask(request: Request, b: AiReq):
    """Вопрос ИИ-ассистенту. Всегда возвращает ответ: при сбое всех моделей Gemini — локальный подбор по каталогу."""
    ip = client_ip(request)
    key = access_key(request)
    if not await uid_by_token(key, ip):
        return JSONResponse({"error": "forbidden"}, status_code=403, headers=NOSTORE)
    if not TOK_AI.hit(key):
        return JSONResponse({"error": "rate"}, status_code=429, headers={**NOSTORE, "Retry-After": "30"})
    if not TOK_AI_DAY.hit(key):
        return JSONResponse({"error": "rate"}, status_code=429, headers={**NOSTORE, "Retry-After": "3600"})
    if AI_SLOTS.locked():
        return JSONResponse({"error": "busy"}, status_code=503, headers={**NOSTORE, "Retry-After": "5"})
    try:                                   # живые данные для ИИ: сбой базы не должен лишать пользователя ответа
        status, ratings = await status_map(), await ratings_summary()
    except Exception as e:
        log.warning("ai: статусы/рейтинги недоступны: %s", e)
        status = ratings = None
    async with AI_SLOTS:
        res = await assistant.answer(b.q, b.history, tools_json(), status, ratings)
    if res.get("fallback"):
        alerts.notify("ai", "ИИ недоступен: ответ из локального каталога",
                      "Gemini не ответил (все модели и ключи). Проверьте ключи и квоту.", key="aifb", cooldown=900)
    if not res["answer"]:
        return JSONResponse({"error": "empty"}, status_code=400, headers=NOSTORE)
    return JSONResponse({"answer": res["answer"], "fallback": res["fallback"], "tools": res.get("tools", []),
                         "links": {"dev": DEV_LINK, "channel": CHANNEL_LINK}}, headers=NOSTORE)


_tool_keys = {"body": None, "keys": None}


def known_tool(name: str) -> bool:
    """Название инструмента (путь «Категория › Название») должно существовать в каталоге: иначе в базу
    можно набить мусорные строки. Если каталог недоступен, проверку пропускаем."""
    body = tools_json()
    if body is None:
        return True
    if _tool_keys["body"] is not body:
        keys: set = set()

        def walk(node, prefix):
            for c in node.get("children") or []:
                if isinstance(c, dict) and isinstance(c.get("name"), str):
                    k = f"{prefix} › {c['name']}" if prefix else c["name"]
                    keys.add(k)
                    walk(c, k)
        try:
            walk(json.loads(body), "")
        except ValueError:
            return True
        _tool_keys["body"], _tool_keys["keys"] = body, frozenset(keys)
    return name in _tool_keys["keys"]


TOOL_RE = re.compile(r"[^\x00-\x1f\x7f<>]{1,200}")   # название инструмента: без управляющих символов и <>


class Track(BaseModel):
    k: str = Field(default="", max_length=100)         # токен теперь приходит в заголовке X-Access-Key
    sid: str = Field(max_length=64)
    ev: str = Field(max_length=20)
    tool: str | None = Field(default=None, max_length=200)


@app.post("/api/track")
async def track(request: Request, b: Track):
    """hb — пульс (время на сайте и «онлайн»), select/bot/eternal — действия с инструментами."""
    if b.ev not in ("hb", "select", "bot", "eternal") or not re.fullmatch(r"[\w-]{8,64}", b.sid):
        return JSONResponse({"ok": False}, status_code=400)
    if b.tool is not None and not (TOOL_RE.fullmatch(b.tool) and known_tool(b.tool)):
        return JSONResponse({"ok": False}, status_code=400)
    ip = client_ip(request)
    key = access_key(request, b.k)
    uid = await uid_by_token(key, ip)
    if not uid:
        return JSONResponse({"ok": False}, status_code=403)
    if not TOK_TRACK.hit(key):
        return JSONResponse({"ok": False}, status_code=429, headers={"Retry-After": "30"})
    owner = _sid_owner.get(b.sid)
    if owner is not None and owner != uid:                # чужую сессию подменить нельзя
        return JSONResponse({"ok": False}, status_code=403)
    if owner is None:
        if not NEW_SID.hit(key):                          # и плодить сессии тоже
            return JSONResponse({"ok": False}, status_code=429, headers={"Retry-After": "300"})
        _sid_owner.put(b.sid, uid)
    ua = request.headers.get("user-agent", "")[:300]
    await pool.execute(
        """insert into sessions(id, tg_id, ip, user_agent) values($1, $2, $3, $4)
           on conflict (id) do update set last_seen = now() where sessions.tg_id = excluded.tg_id""",
        b.sid, uid, ip, ua,
    )
    if b.ev != "hb" and TOK_EVENTS.hit(key):
        await pool.execute(
            "insert into events(tg_id, sid, ev, tool, ip) values($1, $2, $3, $4, $5)",
            uid, b.sid, b.ev, b.tool, ip,
        )
    return {"ok": True}


class Rate(BaseModel):
    k: str = Field(default="", max_length=100)
    tool: str = Field(max_length=200)
    stars: int


@app.post("/api/rate")
async def rate(request: Request, b: Rate):
    """Оценка инструмента от 1 до 5 звёзд. Один человек — одна (перезаписываемая) оценка на инструмент."""
    if not TOOL_RE.fullmatch(b.tool) or not known_tool(b.tool) or not (1 <= b.stars <= 5):
        return JSONResponse({"ok": False}, status_code=400)
    key = access_key(request, b.k)
    uid = await uid_by_token(key, client_ip(request))
    if not uid:
        return JSONResponse({"ok": False}, status_code=403)
    if not TOK_RATE.hit(key):
        return JSONResponse({"ok": False}, status_code=429, headers={"Retry-After": "30"})
    await pool.execute(
        """insert into ratings(tg_id, tool, stars) values($1, $2, $3)
           on conflict (tg_id, tool) do update set stars = excluded.stars, updated_at = now()""",
        uid, b.tool, b.stars,
    )
    row = await pool.fetchrow("select avg(stars)::float av, count(*) n from ratings where tool = $1", b.tool)
    return JSONResponse({"ok": True, "avg": round(row["av"] or 0, 1), "count": row["n"], "mine": b.stars}, headers=NOSTORE)


async def status_map() -> dict:
    """Статусы всех инструментов (автопроверка + ручные от админа): для сайта и для ИИ-ассистента."""
    rows = await pool.fetch(
        "select tool, kind, state, message, extract(epoch from now() - checked_at)::int as age from tool_status")
    out = {r["tool"]: {"s": r["state"], "m": r["message"], "k": r["kind"], "a": r["age"], "bad": r["state"] in mon.BAD}
           for r in rows}
    out.update(cat.manual_status())          # статус, выставленный админом вручную, главнее автопроверки
    return out


async def ratings_summary() -> dict:
    rows = await pool.fetch("select tool, avg(stars)::float av, count(*) n from ratings group by tool")
    return {r["tool"]: {"avg": round(r["av"] or 0, 1), "count": r["n"]} for r in rows}


@app.get("/api/status")
async def status_api(request: Request, k: str = ""):
    """Статусы всех инструментов (результат автопроверки): s — состояние, m — текст, a — возраст проверки в секундах."""
    key = access_key(request, k)
    uid = await uid_by_token(key, client_ip(request))
    if not uid:
        return JSONResponse({"error": "forbidden"}, status_code=403, headers=NOSTORE)
    if not TOK_STATUS.hit(key):
        return JSONResponse({"error": "rate"}, status_code=429, headers={**NOSTORE, "Retry-After": "30"})
    return JSONResponse(await status_map(), headers=NOSTORE)


@app.get("/api/ratings")
async def ratings(request: Request, k: str = ""):
    """Сводный рейтинг по всем инструментам сразу: средняя оценка, число оценок и своя оценка."""
    key = access_key(request, k)
    uid = await uid_by_token(key, client_ip(request))
    if not uid:
        return JSONResponse({"error": "forbidden"}, status_code=403, headers=NOSTORE)
    if not TOK_RATINGS.hit(key):
        return JSONResponse({"error": "rate"}, status_code=429, headers={**NOSTORE, "Retry-After": "30"})
    rows = await pool.fetch(
        """select tool, avg(stars)::float av, count(*) n, max(stars) filter (where tg_id = $1) mine
           from ratings group by tool""",
        uid,
    )
    out = {r["tool"]: {"avg": round(r["av"] or 0, 1), "count": r["n"], "mine": r["mine"]} for r in rows}
    return JSONResponse(out, headers=NOSTORE)


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
    return sec.in_nets(client_ip(req), ADMIN_NETS)


# Серверный список действующих сессий: выход из админки реально отзывает cookie, а не только стирает её в браузере.
# Список живёт в памяти, поэтому после перезапуска сервиса нужно войти заново.
_sessions: dict[str, float] = {}   # jti -> когда истекает


def new_token(kind: str, ip: str, ttl: int) -> str:
    now = time.time()
    for j in [j for j, e in _sessions.items() if e < now]:
        _sessions.pop(j, None)
    while len(_sessions) >= 20:
        _sessions.pop(next(iter(_sessions)))
    jti = secrets.token_urlsafe(16)
    _sessions[jti] = now + ttl
    return sign({"t": kind, "ip": ip, "exp": now + ttl, "j": jti})


def drop_session(req: Request, cookie: str):
    d = unsign(req.cookies.get(cookie, ""))
    if d:
        _sessions.pop(d.get("j"), None)


def session_state(req: Request) -> str:
    ip = client_ip(req)
    for name, kind in (("hadm", "full"), ("hpre", "pre")):
        d = unsign(req.cookies.get(name, ""))
        if d and d.get("t") == kind and d.get("ip") == ip and _sessions.get(d.get("j"), 0) > time.time():
            return kind
    return "none"


def check_origin(req: Request):
    """POST в админку принимаем только со страницы самого сайта (защита от CSRF поверх SameSite=Strict)."""
    # Origin обязателен: браузер всегда шлёт его при POST из fetch; запрос без него — не со страницы админки
    origin = req.headers.get("origin")
    if not origin or urlsplit(origin).netloc != req.headers.get("host", ""):
        raise HTTPException(403)


def probe_alert(req: Request):
    ip = client_ip(req)
    alerts.notify("probe", "Кто-то с чужого IP пробует открыть админку", f"IP {ip}\n{req.method} {req.url.path}",
                  key=ip, cooldown=900)


def guard(req: Request, full: bool = True):
    if not ip_ok(req):
        probe_alert(req)
        raise HTTPException(404)
    if req.method == "POST":
        check_origin(req)
    if full and session_state(req) != "full":
        raise HTTPException(401)


def put_cookie(resp, req: Request, name: str, value: str, max_age: int):
    secure = SITE_URL.startswith("https") or "https" in (req.headers.get("x-forwarded-proto") or req.url.scheme)
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="strict", secure=secure, path="/")


_fails = sec.Bounded(1000)            # ip -> [число ошибок, заблокирован до, начало окна]
_fail_times: deque = deque(maxlen=200)  # время всех неудачных входов — для общей блокировки
GLOBAL_FAIL_LIMIT = 30                  # столько ошибок за 15 минут со всех адресов — вход закрывается для всех


def is_locked(ip: str) -> bool:
    now = time.time()
    f = _fails.get(ip)
    if f and f[1] > now:
        return True
    while _fail_times and _fail_times[0] < now - 900:
        _fail_times.popleft()
    return len(_fail_times) >= GLOBAL_FAIL_LIMIT


def add_fail(ip: str):
    now = time.time()
    _fail_times.append(now)
    f = _fails.get(ip)
    if not f or now - f[2] > 900:
        f = [0, 0, now]
    f[0] += 1
    if f[0] >= 5:
        f[1] = now + 900
    _fails.put(ip, f)


ADMIN_HTML = BASE / "admin" / "index.html"
try:   # встроенные <script>/<style> админки разрешены в CSP только по хэшу их текста
    ADMIN_CSP = sec.admin_csp(ADMIN_HTML.read_text("utf-8"))
except OSError:
    ADMIN_CSP = sec.CSP_PUBLIC


def admin_page(req: Request):
    if not ip_ok(req):
        probe_alert(req)
        return RedirectResponse(main_url(req), status_code=302)
    return FileResponse(ADMIN_HTML, headers={"Cache-Control": "no-store", "Content-Security-Policy": ADMIN_CSP})


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
        alerts.notify("admin", "Вход в админку заблокирован: слишком много неверных попыток", f"IP {ip}", key="lock", cooldown=600)
        return JSONResponse({"error": "Слишком много попыток. Подождите 15 минут."}, status_code=429)
    ok = hmac.compare_digest(body.login.encode(), ADMIN_LOGIN.encode()) & \
        hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode())
    if not ok:
        add_fail(ip)
        alerts.notify("admin", "Неверный логин или пароль админки", f"IP {ip}", key="pw" + ip, cooldown=120)
        await asyncio.sleep(0.7)
        return JSONResponse({"error": "Неверный логин или пароль."}, status_code=401)
    alerts.notify("admin", "Пароль админки верный, ждём код 2FA", f"IP {ip}", key="pre", cooldown=60)
    resp = JSONResponse({"ok": True, "step": "2fa"})
    put_cookie(resp, request, "hpre", new_token("pre", ip, 300), 300)
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
        alerts.notify("admin", "Неверный код 2FA админки", f"IP {ip}", key="2fa" + ip, cooldown=120)
        await asyncio.sleep(0.7)
        return JSONResponse({"error": "Неверный код."}, status_code=401)
    alerts.notify("admin", "Вход в админку выполнен", f"IP {ip}")
    drop_session(request, "hpre")          # код подтверждения одноразовый
    resp = JSONResponse({"ok": True})
    put_cookie(resp, request, "hadm", new_token("full", ip, 4 * 3600), 4 * 3600)
    resp.delete_cookie("hpre", path="/")
    return resp


@app.post("/admin/api/logout")
async def admin_logout(request: Request):
    guard(request, full=False)
    drop_session(request, "hadm")
    drop_session(request, "hpre")
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


_stats_cache = [0.0, None]   # тяжёлая выборка: не чаще раза в 10 секунд, сколько бы ни дёргали


@app.get("/admin/api/stats")
async def admin_stats(request: Request):
    guard(request)
    if _stats_cache[1] is not None and time.monotonic() - _stats_cache[0] < 10:
        return _stats_cache[1]
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
    result = jsonable_encoder({
        "tz": tz,
        "users": dict(users), "requests": dict(reqs), "site": dict(vis), "sessions": dict(sess),
        "online": online, "funnel": dict(funnel), "clicks": dict(clicks),
        "users_daily": fill_days(u_d, d29), "requests_daily": fill_days(r_d, d29),
        "visitors_daily": fill_days(v_d, d29), "hours": hrs,
        "devices": [dict(r) for r in devices], "tools": [dict(r) for r in tool_rows],
    })
    _stats_cache[0], _stats_cache[1] = time.monotonic(), result
    return result


IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
USER_COLS = """u.tg_id, u.username, u.full_name, u.created_at,
               (select count(*) from visits v where v.tg_id = u.tg_id) visits,
               (select max(created_at) from visits v where v.tg_id = u.tg_id) as last_visit"""


def user_filter(q: str) -> tuple[str, list]:
    """WHERE-условие и параметры для поиска пользователей (пустой q — все пользователи)."""
    q = q.strip().lstrip("@")[:100]
    if not q:
        return "true", []
    by_ip = """u.tg_id in (select tg_id from visits where ip {op} $1 union select tg_id from sessions where ip {op} $1
                           union select tg_id from events where ip {op} $1)"""
    if IPV4.match(q) or ":" in q:
        return by_ip.format(op="="), [q]
    if re.fullmatch(r"[\d.]+", q) and "." in q:                        # часть IP, например 91.224.
        return by_ip.format(op="like"), [q + "%"]
    if re.fullmatch(r"[0-9]{1,15}", q):          # isdigit() пропускает «²» и гигантские числа — отсюда были 500-е
        return "u.tg_id = $1", [int(q)]
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "(u.username ilike $1 or u.full_name ilike $2)", [esc + "%", "%" + esc + "%"]


@app.get("/admin/api/search")
async def admin_search(request: Request, q: str = ""):
    guard(request)
    if not q.strip():
        return {"items": []}
    where, params = user_filter(q)
    order = "last_visit desc nulls last" if "select tg_id from visits" in where else "u.created_at desc"
    rows = await pool.fetch(f"select {USER_COLS} from users u where {where} order by {order} limit 30", *params)
    return jsonable_encoder({"items": [dict(r) for r in rows]})


@app.get("/admin/api/users")
async def admin_users(request: Request, q: str = "", offset: int = 0, limit: int = 30):
    """Все пользователи из базы со сводкой по каждому (постранично; q — необязательный поиск)."""
    guard(request)
    offset, limit = max(0, min(offset, 10**7)), max(1, min(limit, 100))
    where, params = user_filter(q)
    n = len(params)
    total = await pool.fetchval(f"select count(*) from users u where {where}", *params)
    rows = await pool.fetch(
        f"""select u.tg_id, u.username, u.full_name, u.created_at,
                   (select min(created_at) from join_requests j where j.tg_id = u.tg_id) join_request_at,
                   (select count(*) from visits v where v.tg_id = u.tg_id) visits,
                   (select min(created_at) from visits v where v.tg_id = u.tg_id) first_visit,
                   (select max(created_at) from visits v where v.tg_id = u.tg_id) last_visit,
                   (select count(*) from sessions s where s.tg_id = u.tg_id) sessions,
                   (select coalesce(sum(extract(epoch from last_seen - started_at)), 0)
                      from sessions s where s.tg_id = u.tg_id) time_total,
                   (select max(last_seen) from sessions s where s.tg_id = u.tg_id) last_seen,
                   (select user_agent from sessions s where s.tg_id = u.tg_id order by started_at desc limit 1) user_agent,
                   (select count(*) from events e where e.tg_id = u.tg_id and e.ev = 'select') tools_opened,
                   (select count(*) from events e where e.tg_id = u.tg_id and e.ev = 'bot') bot_clicks,
                   (select count(*) from events e where e.tg_id = u.tg_id and e.ev = 'eternal') eternal_clicks,
                   (select count(*) from ratings r where r.tg_id = u.tg_id) ratings
            from users u where {where} order by u.created_at desc, u.tg_id desc
            limit ${n + 1} offset ${n + 2}""", *params, limit, offset)
    ids = [r["tg_id"] for r in rows]
    ips: dict[int, list] = {i: [] for i in ids}
    if ids:
        ip_rows = await pool.fetch(
            """select tg_id, ip, count(*) n, max(ts) last from (
                   select tg_id, ip, created_at ts from visits where tg_id = any($1::bigint[])
                   union all select tg_id, ip, started_at from sessions where tg_id = any($1::bigint[])
                   union all select tg_id, ip, created_at from events where tg_id = any($1::bigint[])
               ) x where ip is not null and ip <> '' group by tg_id, ip order by last desc""", ids)
        for r in ip_rows:
            if len(ips[r["tg_id"]]) < 10:
                ips[r["tg_id"]].append({"ip": r["ip"], "n": r["n"], "last": r["last"]})
    items = []
    for r in rows:
        d = dict(r)
        d["time_total"] = float(d["time_total"] or 0)
        d["ips"] = ips.get(r["tg_id"], [])
        items.append(d)
    return jsonable_encoder({"items": items, "total": total, "offset": offset, "limit": limit})


# ---- проверка IP через публичные сервисы (запросы идут с сервера: браузер админки ходит только на свой домен)
_ip_cache = sec.Bounded(500)          # ip -> (результат, годен до)
IP_CHECKS = sec.Limiter(30, 60)       # не больше 30 проверок в минуту, сколько бы ни нажимали


async def _get_json(sess: aiohttp.ClientSession, url: str) -> dict | None:
    try:
        async with sess.get(url, headers={"Accept": "application/json", "User-Agent": "harledy-admin/1.0"}) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            return data if isinstance(data, dict) else None
    except Exception as e:
        log.info("ip lookup %s failed: %s", url.split("?")[0].split("/")[2], type(e).__name__)
        return None


async def _rdns(ip: str) -> str | None:
    try:
        host = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, lambda: socket.gethostbyaddr(ip)[0]), 3)
        return host or None
    except Exception:
        return None


def summarize_ip(ip: str, whois: dict | None, ipapi: dict | None, ipapi_com: dict | None, rdns: str | None) -> dict:
    """Склеивает ответы сервисов в одну сводку; каждое поле берём из первого сервиса, который его вернул."""
    def obj(x):                      # сервисы иногда отдают вместо объекта строку/null — не падаем
        return x if isinstance(x, dict) else {}

    w = obj(whois) if obj(whois).get("success") is not False else {}
    a = obj(ipapi)
    c = obj(ipapi_com) if obj(ipapi_com).get("status") == "success" else {}
    wconn, wtz = obj(w.get("connection")), obj(w.get("timezone"))
    loc, comp, asn = obj(a.get("location")), obj(a.get("company")), obj(a.get("asn"))
    asn_plain = a.get("asn") if isinstance(a.get("asn"), (int, str)) else None

    def first(*vals):
        return next((v for v in vals if v not in (None, "")), None)

    flags = {
        "vpn": first(a.get("is_vpn")), "proxy": first(a.get("is_proxy"), c.get("proxy")),
        "tor": first(a.get("is_tor")), "datacenter": first(a.get("is_datacenter"), c.get("hosting")),
        "mobile": first(a.get("is_mobile"), c.get("mobile")), "abuser": first(a.get("is_abuser")),
    }
    asn_num = first(wconn.get("asn"), asn.get("asn"), asn_plain,
                    str(c.get("as") or "").split(" ")[0].removeprefix("AS") or None)
    out = {
        "ip": ip,
        "country": first(w.get("country"), loc.get("country"), c.get("country")),
        "country_code": first(w.get("country_code"), loc.get("country_code"), c.get("countryCode")),
        "region": first(w.get("region"), loc.get("state"), c.get("regionName")),
        "city": first(w.get("city"), loc.get("city"), c.get("city")),
        "lat": first(w.get("latitude"), loc.get("latitude"), c.get("lat")),
        "lon": first(w.get("longitude"), loc.get("longitude"), c.get("lon")),
        "timezone": first(wtz.get("id"), loc.get("timezone"), c.get("timezone")),
        "isp": first(wconn.get("isp"), comp.get("name"), c.get("isp")),
        "org": first(wconn.get("org"), asn.get("org"), c.get("org")),
        "asn": asn_num,
        "company_type": first(comp.get("type"), asn.get("type")),
        "reverse_dns": rdns,
        "flags": {k: v for k, v in flags.items() if v is not None},
    }
    out["sources"] = [n for n, ok in (("ipwho.is", bool(w)), ("ipapi.is", bool(a)), ("ip-api.com", bool(c))) if ok]
    return out


@app.get("/admin/api/ip/{ip}")
async def admin_ip_check(request: Request, ip: str):
    guard(request)
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        raise HTTPException(400, "Некорректный IP-адрес")
    ip = str(addr)
    if not addr.is_global:
        return {"ip": ip, "private": True, "error": "Это локальный/служебный адрес — в публичных сервисах его нет."}
    hit = _ip_cache.get(ip)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    if not IP_CHECKS.hit("ipcheck"):
        return JSONResponse({"error": "Слишком много проверок, подождите минуту."}, status_code=429)
    timeout = aiohttp.ClientTimeout(total=6)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        whois, ipapi, ipapi_com, rdns = await asyncio.gather(
            _get_json(sess, f"https://ipwho.is/{ip}"),
            _get_json(sess, f"https://api.ipapi.is/?q={ip}"),
            _get_json(sess, f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city,lat,lon,timezone,isp,org,as,mobile,proxy,hosting"),
            _rdns(ip),
        )
    result = summarize_ip(ip, whois, ipapi, ipapi_com, rdns)
    if not result["sources"]:
        return JSONResponse({"error": "Публичные сервисы сейчас недоступны. Попробуйте позже."}, status_code=502)
    _ip_cache.put(ip, (result, time.monotonic() + 3600))
    return result


@app.get("/admin/api/user/{tg_id}")
async def admin_user(request: Request, tg_id: int = ApiPath(ge=0, le=10**15)):
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


# ---- каталог: правка через админку
async def _cat_call(coro, what: str = "", ip: str = "", detail: str = ""):
    try:
        res = await coro
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400, headers=NOSTORE)
    except (asyncpg.PostgresError, OSError, asyncio.TimeoutError) as e:
        log.warning("каталог: ошибка базы: %s", e)
        alerts.notify("db", "Каталог: ошибка базы данных", type(e).__name__, key="catdb", cooldown=300)
        return JSONResponse({"error": "База недоступна, попробуйте ещё раз"}, status_code=503, headers=NOSTORE)
    if what:
        alerts.notify("catalog", f"Каталог изменён: {what}", f"{detail[:150]}\nIP {ip}")
    return JSONResponse({"ok": True, "id": res if isinstance(res, int) else None}, headers=NOSTORE)


async def _json_body(request: Request) -> dict:
    try:
        d = await request.json()
    except Exception:
        raise HTTPException(400)
    if not isinstance(d, dict):
        raise HTTPException(400)
    return d


@app.get("/admin/api/catalog")
async def admin_catalog(request: Request):
    guard(request)
    return JSONResponse(await cat.admin_list(pool), headers=NOSTORE)


@app.get("/admin/api/catalog/export")
async def admin_catalog_export(request: Request):
    """Резервная копия каталога в формате прежнего tools.json."""
    guard(request)
    body = cat.body()
    if body is None:
        raise HTTPException(503)
    return Response(body, media_type="application/json",
                    headers={**NOSTORE, "Content-Disposition": 'attachment; filename="tools.json"'})


@app.post("/admin/api/catalog/create")
async def admin_catalog_create(request: Request):
    guard(request)
    d = await _json_body(request)
    return await _cat_call(cat.create(pool, d), "добавлено", client_ip(request), f"{d.get('kind')}: {d.get('name')}")


@app.post("/admin/api/catalog/update")
async def admin_catalog_update(request: Request):
    guard(request)
    d = await _json_body(request)
    return await _cat_call(cat.update(pool, d), "изменено", client_ip(request), f"id {d.get('id')}: {d.get('name') or ''}")


@app.post("/admin/api/catalog/delete")
async def admin_catalog_delete(request: Request):
    guard(request)
    d = await _json_body(request)
    return await _cat_call(cat.delete(pool, d.get("id")), "удалено", client_ip(request), f"id {d.get('id')}")


@app.post("/admin/api/catalog/move")
async def admin_catalog_move(request: Request):
    guard(request)
    d = await _json_body(request)
    return await _cat_call(cat.move(pool, d.get("id"), d.get("dir")), "перемещено", client_ip(request), f"id {d.get('id')} {d.get('dir')}")


# ====================== страницы и поддомены ======================
@app.api_route("/", methods=["GET", "HEAD"])
async def root_page(request: Request):
    # about.<домен> и admin.<домен> (когда подключён свой домен); остальным — главная
    host = request.headers.get("host", "").split(":")[0].lower()
    if host.startswith("about."):
        return FileResponse(BASE / "static" / "about.html")
    if host.startswith("admin."):
        return admin_page(request)
    return FileResponse(BASE / "static" / "index.html")


@app.get("/about")
async def about_page():
    return FileResponse(BASE / "static" / "about.html")


@app.get("/ai")
async def ai_page():
    return FileResponse(BASE / "static" / "ai.html")


def is_subdomain(req: Request) -> bool:
    """about.<домен>, admin.<домен> и любые другие поддомены основного сайта (SITE_URL)."""
    host = req.headers.get("host", "").split(":")[0].lower().rstrip(".")
    if host.startswith(("about.", "admin.")):
        return True
    main = (urlsplit(SITE_URL).hostname or "").lower() if SITE_URL else ""
    return bool(main) and host != main and host.endswith("." + main)


# Пользовательское соглашение доступно ТОЛЬКО на основном домене. На поддоменах (about., admin., …)
# и по прямому адресу /terms.html отдаём не страницу, а переход на основной сайт.
@app.get("/terms")
@app.get("/terms.html")
async def terms_page(request: Request):
    if is_subdomain(request):
        return RedirectResponse(main_url(request) + "terms", status_code=302)
    return FileResponse(BASE / "static" / "terms.html")


@app.get("/collab")
async def collab_page():
    return FileResponse(BASE / "static" / "collab.html")


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
