"""Алерты владельцу в личку Telegram-бота.

Как работает:
  * alerts.notify(kind, title, body, key=None, cooldown=0) — можно вызывать откуда угодно (и из обычного кода,
    и из потоков): событие кладётся в очередь, отдельная задача отправляет его в Telegram.
  * Всплески склеиваются в одно сообщение; больше 20 сообщений за 10 минут не уходит (остальное считается и
    сообщается одной строкой), так что бот не заспамит ни вас, ни Telegram.
  * Одинаковые события с key и cooldown не повторяются чаще, чем раз в cooldown секунд.
  * Из текстов вырезаются токены, пароли и любые значения секретных переменных окружения.
  * Ошибки уровня ERROR из любых логов (в т.ч. необработанные исключения сайта и бота) уходят сюда сами.

Настройка (все необязательные):
  ALERT_CHAT_ID      кому слать (по умолчанию TOOL_REVIEW_CHAT, т.е. владельцу)
  ALERTS=off         выключить все алерты
  ALERT_MUTE         виды через запятую, которые не слать: user,terms,join,probe,ban,catalog,ai,digest …
  ALERT_DIGEST_HOUR  час суточной сводки по времени ADMIN_TZ (по умолчанию 9; -1 — выключить)
"""
import asyncio
import html
import logging
import os
import re
import time
import traceback
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import security as sec

log = logging.getLogger("harledy.alerts")

ICON = {
    "start": "🟢", "stop": "🔴", "error": "🚨", "db": "🗄", "admin": "🔐", "probe": "🕵️", "ban": "⛔",
    "user": "👤", "terms": "📜", "join": "📨", "catalog": "🛠", "ai": "🤖", "digest": "📊", "info": "ℹ️",
}
ENABLED = os.environ.get("ALERTS", "on").strip().lower() not in ("off", "0", "no", "false")
MUTE = {x.strip().lower() for x in re.split(r"[,\s]+", os.environ.get("ALERT_MUTE", "")) if x.strip()}
MAX_PART = 3800              # лимит Telegram — 4096 символов на сообщение

_bot = None
_chat = 0
_tz = "UTC"
_loop = None
_q: asyncio.Queue | None = None
_task: asyncio.Task | None = None
_pending: list = []                  # события, случившиеся до старта (например, при запуске)
_last = sec.Bounded(2000)            # "вид:ключ" -> когда последний раз слали
_limiter = sec.Limiter(20, 600)      # не больше 20 сообщений за 10 минут
_dropped = 0
_stats: Counter = Counter()          # счётчики событий для суточной сводки


# ---------------------------------------------------------------- вырезание секретов
_URL_CRED = re.compile(r"(\w+://)[^/\s:@]+:[^@\s]+@")
_SECRET_NAME = re.compile(r"TOKEN|SECRET|KEY|PASS|SESSION|DATABASE_URL|HASH", re.I)


def scrub(text) -> str:
    """Убирает из текста всё, что похоже на секрет: токены доступа, пароли в ссылках, значения секретных переменных."""
    s = sec._mask(str(text))
    s = _URL_CRED.sub(r"\1***@", s)
    vals = sorted({v for k, v in os.environ.items() if v and len(v) >= 8 and _SECRET_NAME.search(k)}, key=len, reverse=True)
    for v in vals:
        s = s.replace(v, "***")
    return s


def _now() -> str:
    try:
        return datetime.now(ZoneInfo(_tz)).strftime("%d.%m %H:%M:%S")
    except Exception:
        return datetime.utcnow().strftime("%d.%m %H:%M:%S UTC")


def _render(kind: str, title: str, body: str) -> str:
    head = f"{ICON.get(kind, ICON['info'])} <b>{html.escape(scrub(title))[:200]}</b>"
    body = html.escape(scrub(body).strip())[:700]
    return head + (f"\n{body}" if body else "") + f"\n<i>{_now()}</i>"


# ---------------------------------------------------------------- публичный API
def notify(kind: str, title: str, body: str = "", key: str | None = None, cooldown: int = 0) -> None:
    """Поставить алерт в очередь. Безопасно вызывать откуда угодно; никогда не бросает исключений."""
    try:
        if not ENABLED:
            return
        _stats[kind] += 1
        if kind in MUTE:
            return
        item = (kind, str(title), str(body), key, cooldown)
        if _loop is None:
            if len(_pending) < 20:
                _pending.append(item)
            return
        _loop.call_soon_threadsafe(_put, item)
    except Exception:
        pass


def take_stats() -> dict:
    """Счётчики событий с прошлого вызова (для суточной сводки)."""
    out = dict(_stats)
    _stats.clear()
    return out


def _put(item) -> None:
    global _dropped
    kind, title, body, key, cooldown = item
    if cooldown and key:
        now = time.monotonic()
        k = f"{kind}:{key}"
        if now - _last.get(k, -1e9) < cooldown:
            return
        _last.put(k, now)
    try:
        _q.put_nowait(_render(kind, title, body))
    except (asyncio.QueueFull, AttributeError):
        _dropped += 1


# ---------------------------------------------------------------- отправка
async def _send(text: str) -> None:
    for attempt in (1, 2):
        try:
            await _bot.send_message(_chat, text, parse_mode="HTML")
            return
        except Exception as e:                        # noqa: BLE001 — алерты не должны ронять сервис
            retry = getattr(e, "retry_after", None)
            if retry and attempt == 1:
                await asyncio.sleep(min(float(retry), 30))
                continue
            if "parse" in str(e).lower() or "entit" in str(e).lower():     # битая разметка — шлём простым текстом
                try:
                    await _bot.send_message(_chat, html.unescape(re.sub(r"<[^>]+>", "", text)), parse_mode=None)
                    return
                except Exception:
                    pass
            log.warning("алерт не отправлен: %s", type(e).__name__)
            return


async def _deliver(batch: list) -> None:
    global _dropped
    if not _limiter.hit("m"):
        _dropped += len(batch)
        return
    parts, cur = [], ""
    if _dropped:
        cur = f"<i>… пропущено событий: {_dropped} (слишком много за 10 минут)</i>"
        _dropped = 0
    for m in batch:
        if cur and len(cur) + len(m) + 2 > MAX_PART:
            parts.append(cur)
            cur = ""
        cur = (cur + "\n\n" + m) if cur else m
    if cur:
        parts.append(cur)
    for p in parts:
        await _send(p)


async def _worker() -> None:
    while True:
        first = await _q.get()
        if first is None:
            return
        batch, stop = [first], False
        await asyncio.sleep(1.5)                       # короткое окно: события всплеска уйдут одним сообщением
        while len(batch) < 10:
            try:
                m = _q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if m is None:
                stop = True
                break
            batch.append(m)
        try:
            await _deliver(batch)
        except Exception as e:                         # noqa: BLE001
            log.warning("сбой доставки алертов: %s", type(e).__name__)
        if stop:
            return
        await asyncio.sleep(0.5)


def start(bot, chat_id: int, tz: str = "UTC") -> None:
    """Запустить отправку. Вызывать из async-кода (lifespan) после создания бота."""
    global _bot, _chat, _tz, _loop, _q, _task
    _bot, _chat, _tz = bot, int(chat_id), tz
    _loop = asyncio.get_running_loop()
    _q = asyncio.Queue(maxsize=200)
    _task = asyncio.create_task(_worker())
    pending, _pending[:] = list(_pending), []
    for item in pending:
        _put(item)


async def stop() -> None:
    """Дослать всё, что в очереди, и остановиться (вызывать до закрытия сессии бота)."""
    global _loop
    if _q is None or _task is None:
        return
    await asyncio.sleep(0.05)            # дать событиям, поставленным через call_soon_threadsafe, попасть в очередь раньше «стоп»-метки
    try:
        _q.put_nowait(None)
    except asyncio.QueueFull:
        pass
    try:
        await asyncio.wait_for(asyncio.shield(_task), 10)
    except Exception:
        _task.cancel()
    _loop = None


# ---------------------------------------------------------------- ошибки из логов
class ErrorForwarder(logging.Handler):
    """Любая запись уровня ERROR (необработанное исключение сайта или бота, сбой фоновой задачи) → алерт."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.ERROR or record.name.startswith("harledy.alerts"):
                return
            msg = record.getMessage()
            exc = loc = ""
            if record.exc_info and record.exc_info[1] is not None:
                e = record.exc_info[1]
                exc = f"{type(e).__name__}: {str(e)[:200]}"
                frames = traceback.extract_tb(record.exc_info[2])
                if frames:
                    f = frames[-1]
                    loc = f"{os.path.basename(f.filename)}:{f.lineno} ({f.name})"
            body = "\n".join(x for x in (msg[:300], exc, f"где: {loc}" if loc else "", f"лог: {record.name}") if x)
            kind = (exc.split(":")[0] or msg[:50])
            notify("error", "Ошибка в работе сервера", body, key=f"{record.name}|{kind}|{loc}", cooldown=300)
        except Exception:
            pass


def install_log_handler() -> None:
    h = ErrorForwarder(level=logging.ERROR)
    logging.getLogger().addHandler(h)
