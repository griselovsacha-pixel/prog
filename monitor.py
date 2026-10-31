"""Автопроверка живости инструментов каталога.

Раз в сутки (и по команде /check) проходит по каталогу:
  * сайты   — заходит по ссылке; 200 и страница открылась → «активен», иначе пишет, какая ошибка и что делать;
  * боты    — через Telethon-сессию (StringSession) отправляет боту /start и ждёт ответ до STATUS_BOT_WAIT секунд
              (по умолчанию 5 минут): ответил → «активен», молчит → «не отвечает», юзернейм свободен → «удалён».
Результаты лежат в таблице tool_status, сайт показывает их в карточке инструмента («Статус:»),
а владельцу в Telegram приходит отчёт.

Переменные окружения: TG_API_ID, TG_API_HASH, TG_SESSION (см. STATUS.md).
Без них проверяются только сайты, а в отчёте будет пометка, что боты не проверены.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger("harledy.monitor")


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


TG_API_ID = _int_env("TG_API_ID", 0, 0, 2**31 - 1)
TG_API_HASH = os.environ.get("TG_API_HASH", "").strip()
TG_SESSION = os.environ.get("TG_SESSION", "").strip()
BOT_WAIT = _int_env("STATUS_BOT_WAIT", 300, 10, 900)           # сколько ждём ответ бота на /start
SITE_TIMEOUT = _int_env("STATUS_SITE_TIMEOUT", 20, 5, 60)      # сколько ждём ответ сайта
EVERY_HOURS = _int_env("STATUS_EVERY_HOURS", 24, 1, 168)       # как часто проверять
REPORT_TZ = os.environ.get("ADMIN_TZ", "Europe/Kyiv")
POLL = 3.0            # как часто смотрим, не ответил ли бот
RETRY_PAUSE = 5.0     # пауза перед повторной проверкой упавшего сайта
SEND_GAP = 3.0        # пауза между /start разным ботам (чтобы не ловить флуд-лимит Telegram)
ALLOW_PRIVATE = False  # True только в тестах: разрешает адреса вида 127.0.0.1

# состояния
OK, NO_REPLY, DELETED, SITE_ERROR, LIMITED, UNKNOWN = "ok", "no_reply", "deleted", "site_error", "limited", "unknown"
BAD = frozenset({NO_REPLY, DELETED, SITE_ERROR})   # серая метка в дереве, «вечная ссылка» выходит вперёд

MSG_BOT_OK = "Активен, работает и отвечает"
MSG_BOT_NO_REPLY = "Бот не отвечает, возможны технические работы"
MSG_BOT_DELETED = ("Бот удалён, воспользуйтесь постоянной ссылкой "
                   "или ожидайте, пока администратор вручную заменит ссылку")
MSG_SITE_OK = "Активен, работает без ошибок"
MSG_SITE_NO_REPLY = "Сайт не отвечает, возможны технические работы. Попробуйте позже."
MSG_SITE_DELETED = ("Сайт закрыт или удалён, воспользуйтесь зеркалом "
                    "или ожидайте, пока администратор вручную заменит ссылку")


def manual_message(kind: str, state: str) -> str:
    """Текст статуса, который админ выставил вручную (ok / no_reply / deleted)."""
    if kind == "bot":
        return {OK: MSG_BOT_OK, NO_REPLY: MSG_BOT_NO_REPLY, DELETED: MSG_BOT_DELETED}[state]
    return {OK: MSG_SITE_OK, NO_REPLY: MSG_SITE_NO_REPLY, DELETED: MSG_SITE_DELETED}[state]


@dataclass
class Result:
    state: str
    message: str
    code: int | None = None
    kind: str = "site"       # bot | site | tg


# ====================== каталог → список проверок ======================
TG_HOSTS = {"t.me", "www.t.me", "telegram.me", "telegram.dog"}
USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,31}")
TG_RESERVED = {"joinchat", "c", "s", "addstickers", "addemoji", "share", "proxy", "socks", "setlanguage",
               "login", "iv", "boost", "addlist", "giftcode", "invoice", "m", "contact"}


def _split(url: str):
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        return urlsplit(url)
    except ValueError:
        return None


def is_tg_url(url: str) -> bool:
    u = _split(url)
    return bool(u and ((u.scheme in ("http", "https") and (u.hostname or "").lower() in TG_HOSTS)
                       or (u.scheme == "tg")))


def tg_username(url: str) -> str | None:
    """@username из ссылки t.me/name, t.me/name?start=…, tg://resolve?domain=name. Приглашения (+…) и служебные ссылки — None."""
    u = _split(url)
    if not u:
        return None
    host = (u.hostname or "").lower()
    if u.scheme in ("http", "https") and host in TG_HOSTS:
        first = u.path.strip("/").split("/")[0]
    elif u.scheme == "tg" and host == "resolve":
        first = (parse_qs(u.query).get("domain") or [""])[0]
    else:
        return None
    if not first or first.startswith("+") or first.lower() in TG_RESERVED:
        return None
    return first if USERNAME_RE.fullmatch(first) else None


def collect(tree: dict) -> list[dict]:
    """Все инструменты-листья. Ключ tool совпадает с тем, что считает сайт: «Категория › Подкатегория › Название»."""
    items: list[dict] = []

    def walk(n: dict, names: list[str]):
        kids = n.get("children")
        if isinstance(kids, list):
            for c in kids:
                if isinstance(c, dict) and c.get("name"):
                    walk(c, names + [str(c["name"])])
            return
        url = str(n.get("bot") or "").strip()
        if not names or not url:
            return
        if n.get("mstatus"):          # статус выставлен админом вручную — автопроверка этот инструмент не трогает
            return
        if is_tg_url(url):
            uname = tg_username(url)
            kind = "bot" if uname else "tg_link"
        elif (_split(url) and _split(url).scheme in ("http", "https")):
            uname, kind = None, "site"
        else:
            return
        items.append({"tool": " › ".join(names), "url": url, "kind": kind, "username": uname})

    if isinstance(tree, dict):
        walk(tree, [])
    return items


# ====================== сайты ======================
HTTP_TEXT = {
    400: ("некорректный запрос", "Адрес в каталоге мог устареть. Сообщите администратору."),
    404: ("страница не найдена", "Адрес мог измениться или сайт удалён. Воспользуйтесь зеркалом (если оно есть) или сообщите администратору."),
    410: ("страница удалена навсегда", "Сайт или раздел закрыт. Воспользуйтесь зеркалом или дождитесь, пока администратор заменит ссылку."),
    500: ("внутренняя ошибка сервера", "Сбой на стороне сайта. Подождите немного и попробуйте позже."),
    502: ("плохой шлюз (Bad Gateway)", "Сервер сайта сейчас недоступен. Обычно это временно: попробуйте через час."),
    503: ("сервис недоступен", "Сайт на техобслуживании или перегружен. Попробуйте позже."),
    504: ("шлюз не дождался ответа", "Сервер сайта не отвечает. Попробуйте позже."),
}
CF_MARKERS = ("just a moment", "cf-browser-verification", "cf-chl", "attention required", "checking your browser")


def _blocked_host(url: str) -> bool:
    if ALLOW_PRIVATE:
        return False
    u = _split(url)
    host = ((u.hostname if u else "") or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        return False


def _err(code: int | None, title: str, todo: str) -> Result:
    head = f"Ошибка {code}: {title}" if code else title
    return Result(SITE_ERROR, f"{head}. Что делать: {todo}", code)


async def _site_once(session: aiohttp.ClientSession, url: str) -> tuple[Result, bool]:
    """Один заход на сайт. Второе значение — стоит ли повторить (сбой мог быть случайным)."""
    try:
        async with session.get(url, allow_redirects=True, max_redirects=8) as r:
            body = await asyncio.wait_for(r.content.read(262144), SITE_TIMEOUT)
            code = r.status
            server = (r.headers.get("server") or "").lower()
    except asyncio.TimeoutError:
        return _err(None, f"Сайт не ответил за {SITE_TIMEOUT} секунд",
                    "попробуйте открыть позже, сервер может быть перегружен; при необходимости используйте зеркало."), True
    except aiohttp.TooManyRedirects:
        return _err(None, "Слишком много перенаправлений",
                    "адрес в каталоге мог устареть, сообщите администратору."), False
    except aiohttp.ClientSSLError:
        return _err(None, "Ошибка сертификата безопасности (SSL)",
                    "не вводите на сайте личные данные, пока ошибка не исправлена; воспользуйтесь зеркалом."), False
    except aiohttp.ClientConnectorError as e:
        if isinstance(getattr(e, "os_error", None), socket.gaierror):
            return _err(None, "Домен не найден (ошибка DNS)",
                        "сайт мог закрыться или сменить адрес. Воспользуйтесь зеркалом или дождитесь, пока администратор обновит ссылку."), False
        return _err(None, "Не удалось подключиться к серверу",
                    "сервер выключен или недоступен. Попробуйте позже или воспользуйтесь зеркалом."), True
    except (aiohttp.ClientError, UnicodeError, ValueError) as e:
        return _err(None, f"Сайт не открылся ({type(e).__name__})",
                    "попробуйте открыть вручную; если не получится — сообщите администратору."), True

    text = body[:4096].decode("utf-8", "ignore").lower()
    if code in (401, 403, 429) or (code == 503 and ("cloudflare" in server or any(m in text for m in CF_MARKERS))):
        return Result(LIMITED, f"Сайт отвечает, но закрыт для автопроверки (код {code}). "
                               "Скорее всего он работает — откройте его вручную.", code), False
    if 200 <= code < 300:
        if len(body) == 0:
            return _err(code, "страница пустая", "сайт открылся, но ничего не показал. Попробуйте позже или воспользуйтесь зеркалом."), True
        return Result(OK, MSG_SITE_OK, code), False
    title, todo = HTTP_TEXT.get(code) or (
        ("ошибка сервера", "Сбой на стороне сайта. Попробуйте позже или воспользуйтесь зеркалом.") if code >= 500
        else ("запрос отклонён", "Сайт не открылся. Попробуйте вручную; если не получится — сообщите администратору."))
    return _err(code, title, todo[0].lower() + todo[1:]), code >= 500


async def check_site(session: aiohttp.ClientSession, url: str) -> Result:
    if _blocked_host(url):
        return Result(UNKNOWN, "Автопроверка пропущена: адрес указывает на внутреннюю сеть")
    res, retry = await _site_once(session, url)
    if res.state == SITE_ERROR and retry:          # случайный сбой не должен красить сайт серым: пробуем ещё раз
        await asyncio.sleep(RETRY_PAUSE)
        res, _ = await _site_once(session, url)
    return res


async def check_sites(items: list[dict], on_done) -> None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ru,en;q=0.8",
    }
    timeout = aiohttp.ClientTimeout(total=SITE_TIMEOUT, connect=min(10, SITE_TIMEOUT))
    sem = asyncio.Semaphore(5)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=aiohttp.TCPConnector(limit=10)) as s:
        async def one(it):
            async with sem:
                try:
                    res = await check_site(s, it["url"])
                except Exception as e:                    # одна кривая ссылка не должна ронять всю проверку
                    log.warning("site check %s: %s", it["tool"], e)
                    res = Result(UNKNOWN, "Автопроверка не смогла определить статус сайта")
                await on_done(it, res)
        await asyncio.gather(*(one(i) for i in items))


# ====================== боты (Telethon) ======================
class SessionProblem(Exception):
    """TG_SESSION не работает: отозвана, неверна или используется ещё где-то."""


def telethon_ready() -> bool:
    return bool(TG_API_ID and TG_API_HASH and TG_SESSION)


class BotProbe:
    """Шлёт боту /start и ждёт, пока он ответит. Один клиент на все боты."""

    def __init__(self, client):
        self.c = client
        self.last_in: dict[int, int] = {}      # id бота -> id последнего его сообщения нам
        self._gap = asyncio.Lock()

    async def on_event(self, ev) -> None:
        try:
            if not getattr(ev, "is_private", False):
                return
            sid, mid = ev.sender_id, ev.message.id
            if sid is not None and mid > self.last_in.get(sid, 0):
                self.last_in[sid] = mid
        except Exception:
            pass

    async def probe(self, username: str) -> Result:
        from telethon import errors
        from telethon.tl.functions.contacts import ResolveUsernameRequest, UnblockRequest

        res = None
        for attempt in (1, 2):
            try:
                res = await self.c(ResolveUsernameRequest(username))
                break
            except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
                return Result(DELETED, MSG_BOT_DELETED, kind="bot")
            except errors.FloodWaitError as e:
                if attempt == 2 or e.seconds > 120:
                    return Result(UNKNOWN, "Автопроверка не смогла определить статус: Telegram просит подождать (flood wait)", kind="bot")
                await asyncio.sleep(e.seconds + 1)
        users = list(getattr(res, "users", None) or [])
        if not users:
            if getattr(res, "chats", None):
                return Result(OK, "Активен, ссылка работает (это канал или группа, а не бот)", kind="tg")
            return Result(DELETED, MSG_BOT_DELETED, kind="bot")
        user = users[0]
        if getattr(user, "deleted", False):
            return Result(DELETED, MSG_BOT_DELETED, kind="bot")
        if not getattr(user, "bot", False):
            return Result(OK, "Активен, ссылка работает (это личный аккаунт, а не бот)", kind="tg")

        sent = None
        async with self._gap:                           # /start разным ботам — не чаще раза в SEND_GAP секунд
            for attempt in (1, 2):
                try:
                    sent = await self.c.send_message(user, "/start")
                    break
                except errors.YouBlockedUserError:      # мы сами когда-то заблокировали бота — разблокируем и повторим
                    await self.c(UnblockRequest(user))
                except errors.FloodWaitError as e:
                    if attempt == 2 or e.seconds > 120:
                        return Result(UNKNOWN, "Автопроверка не смогла определить статус: Telegram просит подождать (flood wait)", kind="bot")
                    await asyncio.sleep(e.seconds + 1)
                except errors.RPCError as e:
                    return Result(UNKNOWN, f"Автопроверка не смогла написать боту ({type(e).__name__})", kind="bot")
            await asyncio.sleep(SEND_GAP)
        if sent is None:
            return Result(UNKNOWN, "Автопроверка не смогла написать боту", kind="bot")

        deadline = time.monotonic() + BOT_WAIT
        while True:
            if self.last_in.get(user.id, 0) > sent.id:  # новое сообщение от бота позже нашего /start
                return Result(OK, MSG_BOT_OK, kind="bot")
            left = deadline - time.monotonic()
            if left <= 0:
                return Result(NO_REPLY, MSG_BOT_NO_REPLY, kind="bot")
            await asyncio.sleep(min(POLL, left))


async def check_bots(items: list[dict], on_done) -> None:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH, device_model="Harledy status")
    try:
        await asyncio.wait_for(client.connect(), 30)
        if not await asyncio.wait_for(client.is_user_authorized(), 30):
            raise SessionProblem("сессия не авторизована")
    except SessionProblem:
        await _quiet_disconnect(client)
        raise
    except Exception as e:
        await _quiet_disconnect(client)
        raise SessionProblem(f"не удалось войти по TG_SESSION ({type(e).__name__})") from e

    probe = BotProbe(client)
    client.add_event_handler(probe.on_event, events.NewMessage(incoming=True))
    client.add_event_handler(probe.on_event, events.MessageEdited(incoming=True))
    sem = asyncio.Semaphore(20)

    async def one(it):
        async with sem:
            try:
                res = await probe.probe(it["username"])
            except Exception as e:
                log.warning("bot check %s: %s", it["tool"], e)
                res = Result(UNKNOWN, "Автопроверка не смогла определить статус бота", kind="bot")
            await on_done(it, res)

    try:
        await asyncio.gather(*(one(i) for i in items))
    finally:
        await _quiet_disconnect(client)


async def _quiet_disconnect(client) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


# ====================== база и отчёт ======================
_lock = asyncio.Lock()
_last_scheduled = 0.0      # когда этот процесс в последний раз начинал плановый прогон


def is_running() -> bool:
    return _lock.locked()


async def _save(pool, tool: str, res: Result) -> None:
    await pool.execute(
        """insert into tool_status(tool, kind, state, message, code, fails, checked_at, ok_at)
           values($1, $2, $3::text, $4, $5, $6, now(), case when $3::text = 'ok' then now() end)
           on conflict (tool) do update set
             kind = excluded.kind, state = excluded.state, message = excluded.message, code = excluded.code,
             fails = case when excluded.state in ('no_reply', 'deleted', 'site_error') then tool_status.fails + 1
                          when excluded.state = 'ok' then 0 else tool_status.fails end,
             checked_at = now(),
             ok_at = case when excluded.state = 'ok' then now() else tool_status.ok_at end""",
        tool, res.kind, res.state, res.message, res.code, 1 if res.state in BAD else 0,
    )


def build_report(rows: dict, prev: dict, checked: set, total: int, skipped_bots: str | None) -> str:
    def names(pred):
        out = []
        for tool, r in rows.items():
            if pred(r):
                short = tool.split(" › ")[-1]
                days = f", {r['fails']}-й день" if r["fails"] > 1 else ""
                out.append(f"{html.escape(short)}{' (' + str(r['code']) + ')' if r['code'] else ''}{days}")
        return ", ".join(out)

    now_txt = datetime.now(ZoneInfo(REPORT_TZ)).strftime("%d.%m %H:%M")
    n_ok = sum(1 for r in rows.values() if r["state"] == OK)
    lines = [f"📊 <b>Проверка каталога</b> · {now_txt}", f"✅ Активны: {n_ok} из {total}"]
    groups = [
        ("🤖 Бот не отвечает", lambda r: r["state"] == NO_REPLY),
        ("🗑 Бот удалён", lambda r: r["state"] == DELETED),
        ("🌐 Сайт с ошибкой", lambda r: r["state"] == SITE_ERROR),
        ("ℹ️ Сайт закрыт для автопроверки (скорее всего работает)", lambda r: r["state"] == LIMITED),
        ("❔ Не удалось проверить", lambda r: r["state"] == UNKNOWN and r.get("kind") != "tg"),
    ]
    for title, pred in groups:
        cnt = sum(1 for r in rows.values() if pred(r))
        if cnt:
            lines.append(f"{title} ({cnt}): {names(pred)}")
    worse = [t.split(" › ")[-1] for t, r in rows.items()
             if t in checked and r["state"] in BAD and (prev.get(t) or {}).get("state") not in BAD]
    better = [t.split(" › ")[-1] for t, r in rows.items()
              if t in checked and r["state"] == OK and (prev.get(t) or {}).get("state") in BAD]
    if worse:
        lines.append("🔻 Новые проблемы: " + ", ".join(html.escape(x) for x in worse))
    if better:
        lines.append("🔺 Снова работают: " + ", ".join(html.escape(x) for x in better))
    if skipped_bots:
        lines.append(f"⚠️ Боты не проверены: {html.escape(skipped_bots)}")
    if not any(sum(1 for r in rows.values() if pred(r)) for _, pred in groups) and not skipped_bots:
        lines.append("Все ссылки в порядке.")
    return "\n".join(lines)


async def run(pool, tools_body, bot=None, report_chat: int | None = None, only_due: bool = False) -> str | None:
    """Прогон проверки. only_due=True — только инструменты, которые давно не проверялись (для фонового расписания)."""
    if _lock.locked():
        return None
    async with _lock:
        try:
            tree = json.loads(tools_body) if tools_body else None
        except ValueError:
            tree = None
        items = collect(tree or {})
        if not items:
            return None
        rows_db = await pool.fetch("select tool, state, checked_at from tool_status")
        prev = {r["tool"]: dict(r) for r in rows_db}
        all_items = items
        if only_due:
            global _last_scheduled
            limit = datetime.now().astimezone() - timedelta(hours=EVERY_HOURS, minutes=-10)
            can_bots = telethon_ready()
            items = [i for i in items
                     if (i["kind"] != "bot" or can_bots)               # без сессии боты всё равно не проверятся
                     and (i["tool"] not in prev or prev[i["tool"]]["checked_at"] < limit)]
            # один плановый прогон за период: сбой сессии не должен превращаться в отчёт каждые 15 минут
            if not items or time.monotonic() - _last_scheduled < EVERY_HOURS * 3600 * 0.9 and _last_scheduled:
                return None
            _last_scheduled = time.monotonic()
        log.info("проверка каталога: %d инструментов", len(items))

        async def on_done(it, res: Result):
            if it["kind"] == "bot" and res.kind == "site":
                res.kind = "bot"
            try:
                await _save(pool, it["tool"], res)
            except Exception as e:
                log.warning("не удалось сохранить статус %s: %s", it["tool"], e)

        sites = [i for i in items if i["kind"] == "site"]
        bots = [i for i in items if i["kind"] == "bot"]
        others = [i for i in items if i["kind"] == "tg_link"]
        skipped_bots = None
        for it in others:                                   # приватные приглашения t.me/+… автоматически не проверить
            await on_done(it, Result(UNKNOWN, "Это приглашение в Telegram: автопроверка недоступна", kind="tg"))
        tasks = [check_sites(sites, on_done)]
        if bots:
            if telethon_ready():
                tasks.append(check_bots(bots, on_done))
            else:
                skipped_bots = "не заданы TG_API_ID, TG_API_HASH, TG_SESSION"
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, SessionProblem):
                skipped_bots = f"сессия Telegram не работает — {r}. Создайте новую (STATUS.md) и обновите TG_SESSION"
            elif isinstance(r, ImportError):
                skipped_bots = "не установлен пакет telethon"
            elif isinstance(r, BaseException):
                log.warning("проверка завершилась с ошибкой: %r", r)

        current = [i["tool"] for i in collect(tree or {})]
        await pool.execute("delete from tool_status where tool <> all($1::text[])", list(set(current)))
        now_rows = await pool.fetch("select tool, kind, state, code, fails from tool_status")
        rows = {r["tool"]: dict(r) for r in now_rows}
        total = sum(1 for i in all_items if i["kind"] != "tg_link")
        report = build_report(rows, prev, {i["tool"] for i in items}, total, skipped_bots)
        if bot is not None and report_chat:
            try:
                await bot.send_message(report_chat, report)
            except Exception as e:
                log.warning("не удалось отправить отчёт: %s", e)
        return report
