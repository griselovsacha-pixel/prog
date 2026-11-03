"""Рассылка всем пользователям бота из админки.

Как устроено:
  * Пост хранится как «текст + entities» в формате Telegram (смещения в единицах UTF-16). Админка собирает их
    из визуального редактора, поэтому поддерживается всё форматирование Telegram: жирный, курсив, подчёркнутый,
    зачёркнутый, спойлер, моно, блок кода (с языком), цитата и сворачиваемая цитата, ссылки и премиум-эмодзи.
    Никакого parse_mode: Telegram получает ровно то, что собрал редактор, ничего не нужно экранировать.
  * clean_post() проверяет пост до отправки: длину, смещения, правила вложенности Telegram, ссылки.
  * Broadcaster шлёт сообщения пачками из базы (без загрузки всех пользователей в память), держит темп
    ниже лимита Telegram (~30 сообщений/с на бота), переживает FloodWait, повторяет при сетевых сбоях,
    считает заблокировавших бота и умеет отменяться. Одновременно идёт одна рассылка.
  * Если Telegram не принимает премиум-эмодзи (бот не имеет на них права), сообщение уходит с обычными
    смайликами вместо них; в итогах это видно отдельным счётчиком.
  * Если Telegram отклоняет сам пост у первых получателей (ошибка форматирования), рассылка останавливается
    сразу, а не «успешно» проваливается для всей базы.

Модуль не импортирует aiogram: отправку и поиск эмодзи ему передаёт main.py, ошибки Telegram он различает
по классам. Так его можно проверять без установленного aiogram (tests/test_broadcast.py).

Настройка (необязательная): BROADCAST_RATE — сообщений в секунду, 1…30 (по умолчанию 20).
"""
import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone

log = logging.getLogger("harledy.broadcast")

MAX_TEXT = 4096                   # лимит Telegram, в единицах UTF-16
MAX_ENTITIES = 100
MAX_EMOJI_LOOKUP = 50             # столько ID за один запрос к Telegram
ABORT_AFTER = 5                   # столько отказов подряд при нуле успехов — пост бракованный, стоп
WORKERS = 10
BATCH = 1000
SAVE_EVERY = 4.0                  # секунд между сохранениями прогресса в базу

TYPES = {"bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre",
         "blockquote", "expandable_blockquote", "text_link", "custom_emoji"}
QUOTES = {"blockquote", "expandable_blockquote"}
SOLID = {"code", "pre"}                       # внутри них другого форматирования быть не может
LINK_RE = re.compile(r"(?i)^(https?://|tg://|mailto:)\S+$")
LANG_RE = re.compile(r"^[A-Za-z0-9_+#.\-]{1,30}$")
EMOJI_ID_RE = re.compile(r"^\d{1,32}$")
PACK_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _rate() -> float:
    try:
        return min(30.0, max(1.0, float(os.environ.get("BROADCAST_RATE", "20"))))
    except ValueError:
        return 20.0


def u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


class Post:
    """Проверенный пост: text + entities (словари в формате Telegram MessageEntity)."""

    def __init__(self, text: str, entities: list, silent: bool = False, no_preview: bool = False):
        self.text, self.entities, self.silent, self.no_preview = text, entities, silent, no_preview

    @property
    def has_emoji(self) -> bool:
        return any(e["type"] == "custom_emoji" for e in self.entities)

    def plain_entities(self) -> list:
        """Те же entities без премиум-эмодзи: в тексте на их месте остаётся обычный смайлик."""
        return [e for e in self.entities if e["type"] != "custom_emoji"]


def clean_post(text, entities=None, silent=False, no_preview=False) -> Post:
    """Проверяет и нормализует пост. Любая проблема — ValueError с понятным текстом для админа."""
    if not isinstance(text, str):
        raise ValueError("Напишите текст рассылки.")
    text = CTRL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).rstrip()
    if not text.strip():
        raise ValueError("Напишите текст рассылки.")
    n = u16len(text)
    if n > MAX_TEXT:
        raise ValueError(f"Текст длиннее {MAX_TEXT} символов (сейчас {n}): сократите на {n - MAX_TEXT}.")
    if entities is None:
        entities = []
    if not isinstance(entities, list):
        raise ValueError("Некорректное форматирование.")
    if len(entities) > MAX_ENTITIES:
        raise ValueError(f"Слишком много форматирования: {len(entities)} из {MAX_ENTITIES} возможных. "
                         "Уберите лишнее или объедините соседние куски.")
    units = memoryview(text.encode("utf-16-le")).cast("H")       # для проверки, что смещения не режут суррогатную пару

    def inside_pair(i: int) -> bool:
        return 0 < i < n and 0xDC00 <= units[i] <= 0xDFFF

    out = []
    for raw in entities:
        if not isinstance(raw, dict):
            raise ValueError("Некорректное форматирование.")
        t, off, ln = raw.get("type"), raw.get("offset"), raw.get("length")
        if t not in TYPES:
            raise ValueError(f"Неподдерживаемое форматирование: {str(t)[:30]}.")
        if not (isinstance(off, int) and isinstance(ln, int)) or isinstance(off, bool) or isinstance(ln, bool) \
                or off < 0 or ln <= 0 or off >= n:
            raise ValueError("Некорректные границы форматирования.")
        end = min(off + ln, n)                                    # хвостовые пробелы мы обрезали — подрезаем и разметку
        if inside_pair(off) or inside_pair(end):
            raise ValueError("Некорректные границы форматирования (середина символа).")
        e = {"type": t, "offset": off, "length": end - off}
        covered = text.encode("utf-16-le")[off * 2:end * 2].decode("utf-16-le")
        if not covered.strip():
            continue                                              # форматирование пробелов бессмысленно и Telegram его выкинет
        if t == "text_link":
            url = str(raw.get("url") or "").strip()
            if len(url) > 2048 or not LINK_RE.match(url):
                raise ValueError(f"Ссылка «{url[:60]}» не подходит: нужен адрес вида https://…")
            e["url"] = url
        elif t == "pre":
            lang = str(raw.get("language") or "").strip()
            if lang:
                if not LANG_RE.match(lang):
                    raise ValueError(f"Язык подсветки «{lang[:30]}» не подходит: только латиница, цифры и + # . _ -")
                e["language"] = lang
        elif t == "custom_emoji":
            cid = str(raw.get("custom_emoji_id") or "").strip()
            if not EMOJI_ID_RE.match(cid):
                raise ValueError("ID премиум-эмодзи должен состоять из цифр.")
            if len(covered) != len(covered.strip()) or "\n" in covered or end - off > 20:
                raise ValueError("Премиум-эмодзи должно стоять на одном обычном смайлике.")
            e["custom_emoji_id"] = cid
        out.append(e)
    out.sort(key=lambda e: (e["offset"], -e["length"]))

    for i, a in enumerate(out):                                   # правила вложенности Telegram
        for b in out[i + 1:]:
            if b["offset"] >= a["offset"] + a["length"]:
                break                                             # дальше только непересекающиеся (список отсортирован)
            ta, tb = a["type"], b["type"]
            if ta in QUOTES and tb in QUOTES:
                raise ValueError("Цитаты нельзя вкладывать друг в друга.")
            if ta == tb == "custom_emoji":
                raise ValueError("Два премиум-эмодзи наложены друг на друга.")
            for solid, other in ((a, b), (b, a)):
                if solid["type"] in SOLID:
                    ok = other["type"] in QUOTES and other["offset"] <= solid["offset"] \
                        and other["offset"] + other["length"] >= solid["offset"] + solid["length"]
                    if not ok:
                        raise ValueError("Моноширинный текст и блок кода нельзя совмещать с другим форматированием "
                                         "(кроме цитаты вокруг них).")
    return Post(text, out, bool(silent), bool(no_preview))


# ---------------------------------------------------------------- разбор ошибок Telegram (без импорта aiogram)
_GONE = ("chat not found", "user not found", "peer_id_invalid", "user is deactivated", "bot was blocked",
         "bot can't initiate", "have no rights to send", "forbidden")


def err_text(e: Exception) -> str:
    return (getattr(e, "message", None) or str(e) or type(e).__name__).strip()[:300]


def classify(e: Exception) -> str:
    """retry_after | blocked | transient | rejected — как поступить с ошибкой при отправке одному человеку."""
    names = {c.__name__ for c in type(e).__mro__}
    msg = err_text(e).lower()
    if "TelegramRetryAfter" in names or hasattr(e, "retry_after"):
        return "retry_after"
    if "TelegramForbiddenError" in names:
        return "blocked"
    if "TelegramBadRequest" in names:
        return "blocked" if any(s in msg for s in _GONE) else "rejected"
    if "TelegramUnauthorizedError" in names:
        return "rejected"
    if names & {"TelegramNetworkError", "TelegramServerError", "ClientError", "TimeoutError", "OSError"}:
        return "transient"
    return "rejected"


# ---------------------------------------------------------------- хранилище
class PgStore:
    """Таблица broadcasts + выборка получателей. Получатели — все строки users (кто хоть раз запускал бота)."""

    def __init__(self, pool):
        self.pool = pool

    async def count_users(self, until) -> int:
        return await self.pool.fetchval("select count(*) from users where created_at <= $1", until)

    async def users_page(self, until, after: int, limit: int) -> list[int]:
        rows = await self.pool.fetch(
            "select tg_id from users where created_at <= $1 and tg_id > $2 order by tg_id limit $3", until, after, limit)
        return [r["tg_id"] for r in rows]

    async def create(self, b: "Run") -> int:
        return await self.pool.fetchval(
            """insert into broadcasts(status, text, entities, silent, no_preview, total)
               values('running', $1, $2::jsonb, $3, $4, $5) returning id""",
            b.post.text, json.dumps(b.post.entities), b.post.silent, b.post.no_preview, b.total)

    async def save(self, b: "Run") -> None:
        await self.pool.execute(
            """update broadcasts set status=$2, sent=$3, blocked=$4, failed=$5, plain=$6, error=$7,
               finished_at = case when $2 = 'running' then null else coalesce(finished_at, now()) end where id=$1""",
            b.id, b.status, b.sent, b.blocked, b.failed, b.plain, b.error or None)

    async def recent(self, n: int) -> list[dict]:
        rows = await self.pool.fetch(
            """select id, created_at, finished_at, status, left(text, 300) as text, silent, no_preview,
                      total, sent, blocked, failed, plain, error
               from broadcasts order by id desc limit $1""", n)
        return [dict(r) for r in rows]

    async def interrupted(self) -> None:
        await self.pool.execute(
            "update broadcasts set status='interrupted', finished_at=now() where status='running'")


# ---------------------------------------------------------------- рассылка
class Run:
    """Состояние одной рассылки."""

    def __init__(self, post: Post, total: int, until):
        self.id = 0
        self.post, self.total, self.until = post, total, until
        self.status = "running"          # running | done | cancelled | aborted
        self.sent = self.blocked = self.failed = self.plain = 0
        self.error = ""
        self.errors: Counter = Counter()
        self.cancel = asyncio.Event()
        self.emoji_off = False           # Telegram не принял премиум-эмодзи: дальше шлём обычными
        self.streak = 0                  # отказов подряд без единого успеха
        self.t0 = time.monotonic()
        self.t1: float | None = None
        self.started = datetime.now(timezone.utc)

    @property
    def done(self) -> int:
        return self.sent + self.blocked + self.failed

    def snapshot(self) -> dict:
        el = (self.t1 or time.monotonic()) - self.t0
        eta = None
        if self.status == "running" and self.done >= 20 and el > 2:
            eta = int((self.total - self.done) / (self.done / el))
        return {"id": self.id, "status": self.status, "total": self.total, "done": self.done, "sent": self.sent,
                "blocked": self.blocked, "failed": self.failed, "plain": self.plain, "error": self.error,
                "errors": [{"text": t, "n": n} for t, n in self.errors.most_common(3)],
                "elapsed": int(el), "eta": eta, "started": self.started.isoformat()}


class Pacer:
    """Держит темп отправки и общую паузу при FloodWait."""

    def __init__(self, rate: float):
        self.gap = 1.0 / rate
        self.next = 0.0
        self.pause_until = 0.0
        self.lock = asyncio.Lock()

    def pause(self, secs: float) -> None:
        self.pause_until = max(self.pause_until, time.monotonic() + secs)

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            t = max(self.next, self.pause_until, now)
            self.next = t + self.gap
        if t > now:
            await asyncio.sleep(t - now)


class Broadcaster:
    """send(chat_id, text, entities, silent, no_preview) — корутина, шлёт одно сообщение (бросает ошибки aiogram).
    emoji_lookup(ids) — корутина, возвращает {id: смайлик}. pack_lookup(name) — корутина, возвращает
    [(id, смайлик), …] набора премиум-эмодзи. notify(kind, title, body) — алерты владельцу."""

    def __init__(self, store, send, emoji_lookup=None, notify=None, rate: float | None = None, retry_delay: float = 1.5,
                 pack_lookup=None):
        self.store, self.send, self.emoji_lookup, self.pack_lookup = store, send, emoji_lookup, pack_lookup
        self.notify = notify or (lambda *a, **k: None)
        self.rate = rate or _rate()
        self.retry_delay = retry_delay
        self.run: Run | None = None             # текущая или последняя рассылка этого запуска сервера
        self._task: asyncio.Task | None = None

    # ---- для админки
    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    def progress(self) -> dict | None:
        return self.run.snapshot() if self.run else None

    async def state(self) -> dict:
        until = datetime.now(timezone.utc)
        try:
            users = await self.store.count_users(until)
            history = await self.store.recent(10)
        except Exception as e:                  # noqa: BLE001
            log.warning("рассылка: база недоступна: %s", e)
            raise ValueError("База недоступна, попробуйте ещё раз.")
        for h in history:
            for k in ("created_at", "finished_at"):
                if h.get(k):
                    h[k] = h[k].isoformat()
        return {"users": users, "history": history, "progress": self.progress(), "rate": self.rate,
                "max_text": MAX_TEXT, "max_entities": MAX_ENTITIES}

    async def lookup_emoji(self, ids) -> dict:
        if not isinstance(ids, list):
            raise ValueError("Некорректный запрос.")
        ids = list(dict.fromkeys(str(i).strip() for i in ids))[:MAX_EMOJI_LOOKUP]
        if not ids or not all(EMOJI_ID_RE.match(i) for i in ids):
            raise ValueError("ID премиум-эмодзи должен состоять из цифр.")
        if not self.emoji_lookup:
            raise ValueError("Проверка эмодзи недоступна.")
        try:
            found = await self.emoji_lookup(ids)
        except Exception as e:                  # noqa: BLE001
            log.warning("рассылка: поиск эмодзи: %s", err_text(e))
            raise ValueError("Telegram не ответил на проверку эмодзи: " + err_text(e))
        return {"emoji": {i: found[i] for i in ids if found.get(i)}, "missing": [i for i in ids if not found.get(i)]}

    async def emoji_pack(self, name) -> dict:
        """Все премиум-эмодзи набора: админ выбирает нужное кликом, а не ищет ID вручную."""
        name = str(name or "").strip()
        m = re.search(r"(?i)(?:t\.me|telegram\.me)/addemoji/([A-Za-z0-9_]+)", name)    # можно вставить ссылку на набор целиком
        name = m.group(1) if m else name
        if not PACK_RE.match(name):
            raise ValueError("Укажите ссылку на набор эмодзи (t.me/addemoji/…) или его короткое имя.")
        if not self.pack_lookup:
            raise ValueError("Загрузка наборов недоступна.")
        try:
            items = await self.pack_lookup(name)
        except Exception as e:                  # noqa: BLE001
            log.warning("рассылка: набор эмодзи %s: %s", name, err_text(e))
            raise ValueError("Набор не найден или Telegram не ответил: " + err_text(e))
        items = [{"id": str(i), "emoji": str(ch)} for i, ch in items if EMOJI_ID_RE.match(str(i))]
        if not items:
            raise ValueError("В этом наборе нет премиум-эмодзи.")
        return {"name": name, "emoji": items}

    async def test(self, post: Post, chat_id) -> dict:
        """Одно сообщение для проверки (себе). Честно сообщает, если премиум-эмодзи пришлось заменить."""
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id <= 0:
            raise ValueError("Укажите числовой Telegram ID получателя теста.")
        try:
            await self.send(chat_id, post.text, post.entities, post.silent, post.no_preview)
            return {"ok": True, "emoji_fallback": False}
        except Exception as e:                  # noqa: BLE001
            first = e
        if post.has_emoji and classify(first) == "rejected":
            try:
                await self.send(chat_id, post.text, post.plain_entities(), post.silent, post.no_preview)
                return {"ok": True, "emoji_fallback": True, "reason": err_text(first)}
            except Exception:                   # noqa: BLE001
                pass
        if classify(first) == "blocked":
            raise ValueError("Этот пользователь не получил тест: он не запускал бота или заблокировал его. "
                             "Сначала нажмите /start у бота с этого аккаунта. (" + err_text(first) + ")")
        raise ValueError("Telegram отклонил пост: " + err_text(first))

    async def start(self, post: Post) -> dict:
        if self.running:
            raise ValueError("Рассылка уже идёт. Дождитесь конца или отмените её.")
        until = datetime.now(timezone.utc)
        try:
            total = await self.store.count_users(until)
            if not total:
                raise ValueError("В базе нет ни одного пользователя бота.")
            b = Run(post, total, until)
            b.id = await self.store.create(b)
        except ValueError:
            raise
        except Exception as e:                  # noqa: BLE001
            log.warning("рассылка: не удалось начать: %s", e)
            raise ValueError("База недоступна, рассылка не запущена.")
        self.run = b
        self._task = asyncio.create_task(self._main(b))
        self.notify("info", "Рассылка запущена", f"№{b.id}: получателей {total}, текст: {post.text[:80]}")
        return b.snapshot()

    def cancel(self) -> bool:
        if self.running and self.run:
            self.run.cancel.set()
            return True
        return False

    async def stop(self) -> None:
        """Остановка сервера: прерываем отправку, статус «running» в базе помечается при следующем старте."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except BaseException:               # noqa: BLE001
                pass

    # ---- отправка
    async def _main(self, b: Run) -> None:
        pacer = Pacer(self.rate)
        q: asyncio.Queue = asyncio.Queue(maxsize=WORKERS * 4)

        async def producer():
            after = 0
            try:
                while not b.cancel.is_set():
                    page = await self.store.users_page(b.until, after, BATCH)
                    if not page:
                        break
                    for uid in page:
                        if b.cancel.is_set():
                            return
                        await q.put(uid)
                    after = page[-1]
            finally:
                for _ in range(WORKERS):
                    await q.put(None)

        async def worker():
            while True:
                uid = await q.get()
                if uid is None:
                    return
                if b.cancel.is_set():
                    continue                    # хвост очереди после отмены выбрасываем
                await self._deliver(b, pacer, uid)

        async def saver():
            while True:
                await asyncio.sleep(SAVE_EVERY)
                await self._save(b)

        tasks = [asyncio.create_task(worker()) for _ in range(WORKERS)]
        prod = asyncio.create_task(producer())
        sv = asyncio.create_task(saver())
        try:
            await prod
            await asyncio.gather(*tasks)
            if b.status == "running":
                b.status = "cancelled" if b.cancel.is_set() else "done"
        except asyncio.CancelledError:
            b.status = "aborted"
            b.error = b.error or "Сервер остановился во время рассылки."
            raise
        except Exception as e:                  # noqa: BLE001
            log.exception("рассылка: сбой")
            b.status, b.error = "aborted", f"Сбой рассылки: {err_text(e)}"
        finally:
            sv.cancel()
            for t in (prod, *tasks):
                t.cancel()
            b.t1 = time.monotonic()
            await asyncio.shield(self._save(b))
            self.notify("info", {"done": "Рассылка завершена", "cancelled": "Рассылка отменена"}.get(
                b.status, "Рассылка остановлена"),
                f"№{b.id}: доставлено {b.sent} из {b.total}, недоступны {b.blocked}, ошибок {b.failed}"
                + (f", без премиум-эмодзи {b.plain}" if b.plain else "") + (f"\n{b.error}" if b.error else ""))

    async def _save(self, b: Run) -> None:
        try:
            await self.store.save(b)
        except Exception as e:                  # noqa: BLE001
            log.warning("рассылка: не сохранился прогресс: %s", e)

    async def _deliver(self, b: Run, pacer: Pacer, uid: int) -> None:
        post = b.post
        for attempt in range(4):
            if b.cancel.is_set():
                return
            ents = post.plain_entities() if (b.emoji_off and post.has_emoji) else post.entities
            await pacer.wait()
            try:
                await self.send(uid, post.text, ents, post.silent, post.no_preview)
                b.sent += 1
                b.streak = 0
                if b.emoji_off and post.has_emoji:
                    b.plain += 1
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:              # noqa: BLE001
                how = classify(e)
                if how == "retry_after":
                    pacer.pause(float(getattr(e, "retry_after", 5) or 5) + 1)
                    continue
                if how == "transient":
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                if how == "blocked":
                    b.blocked += 1
                    return
                # rejected: возможно, дело в премиум-эмодзи — пробуем без них
                if post.has_emoji and not b.emoji_off:
                    try:
                        await pacer.wait()
                        await self.send(uid, post.text, post.plain_entities(), post.silent, post.no_preview)
                    except asyncio.CancelledError:
                        raise
                    except Exception:           # noqa: BLE001
                        pass
                    else:
                        b.emoji_off = True
                        b.sent += 1
                        b.plain += 1
                        b.streak = 0
                        log.warning("рассылка №%s: Telegram не принимает премиум-эмодзи (%s), дальше обычными", b.id, err_text(e))
                        return
                b.failed += 1
                b.errors[err_text(e)] += 1
                b.streak += 1
                if b.sent == 0 and b.streak >= ABORT_AFTER and b.status == "running":
                    b.status = "aborted"
                    b.error = "Telegram отклоняет этот пост: " + err_text(e) + ". Рассылка остановлена."
                    b.cancel.set()
                return
        b.failed += 1                           # исчерпали повторы (сеть, FloodWait)
        b.errors["не удалось отправить после повторов"] += 1
