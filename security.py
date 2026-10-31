"""Защитный слой сайта: лимиты запросов, баны, определение IP клиента, заголовки безопасности.

Только стандартная библиотека, поэтому модуль проверяется отдельно от остального сайта.
Подключается в main.py одной строкой: app.add_middleware(Shield).

Важно: всё хранится в памяти одного процесса (на Render Free он один), все структуры
ограничены по размеру — подмена IP или поток случайных ключей не раздует память.
"""
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import time
from collections import OrderedDict, deque

log = logging.getLogger("harledy.sec")


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- настройки
# Сколько прокси добавляют адрес в X-Forwarded-For ПОСЛЕ клиента. Левые записи клиент
# может подделать, поэтому берём запись с конца. На Render (он сам за Cloudflare) это 2.
PROXY_HOPS = _int_env("PROXY_HOPS", 2, 1, 10)
# Заголовок с реальным IP. Включать (например, cf-connecting-ip) ТОЛЬКО если задан
# ORIGIN_SECRET и весь трафик идёт через ваш Cloudflare: иначе заголовок подделывается.
IP_HEADER = os.environ.get("IP_HEADER", "").strip().lower()
# Секрет, который Cloudflare добавляет к каждому запросу (Transform Rule). Без него — 403.
ORIGIN_SECRET = os.environ.get("ORIGIN_SECRET", "")
ORIGIN_HEADER = os.environ.get("ORIGIN_SECRET_HEADER", "x-origin-secret").strip().lower()
# Разрешённые домены (с поддоменами). Пусто — проверка Host выключена.
ALLOWED_HOSTS = [h.strip().lower().lstrip(".") for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
MAX_BODY = _int_env("MAX_BODY", 4096, 256, 1_000_000)
MAX_BODY_AI = _int_env("MAX_BODY_AI", 24576, 1024, 1_000_000)   # вопрос + история диалога для /api/ai
MAX_INFLIGHT = _int_env("MAX_INFLIGHT", 64, 4, 1000)
REQ_TIMEOUT = _int_env("REQ_TIMEOUT", 30, 5, 120)
REQ_TIMEOUT_AI = _int_env("REQ_TIMEOUT_AI", 45, 10, 120)         # цепочка моделей Gemini дольше обычного запроса
DEBUG_IP = os.environ.get("DEBUG_IP") == "1"
HTTPS_SITE = (os.environ.get("SITE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").startswith("https")
EXEMPT_NETS: list = []  # сети, которые не банятся (админ); заполняет main.py


# ---------------------------------------------------------------- определение IP
def norm_ip(s: str) -> str | None:
    s = (s or "").strip().strip("[]")
    if s.count(":") == 1 and "." in s:           # «1.2.3.4:5678»
        s = s.split(":")[0]
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return None
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return str(a)


def rl_key(ip: str) -> str:
    """Ключ для лимитов: IPv6 группируем по /64, иначе у одного клиента миллиарды адресов."""
    try:
        if ipaddress.ip_address(ip).version == 6:
            return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    except ValueError:
        pass
    return ip


def header(scope, name: bytes) -> str:
    vals = [v.decode("latin-1") for k, v in scope.get("headers", ()) if k == name]
    return ",".join(vals)


_dbg_left = 20


def ip_from_scope(scope) -> str:
    ip = None
    if IP_HEADER:
        ip = norm_ip(header(scope, IP_HEADER.encode()))
    if not ip:
        parts = [p.strip() for p in header(scope, b"x-forwarded-for").split(",") if p.strip()]
        if len(parts) >= PROXY_HOPS:
            ip = norm_ip(parts[-PROXY_HOPS])
    peer = scope.get("client")
    if not ip and peer:
        ip = norm_ip(peer[0])
    ip = ip or "0.0.0.0"
    global _dbg_left
    if DEBUG_IP and _dbg_left > 0 and scope.get("path", "").startswith("/admin"):
        _dbg_left -= 1
        log.warning("DEBUG_IP: xff=%r %s=%r peer=%r hops=%s -> %s", header(scope, b"x-forwarded-for"),
                    IP_HEADER or "-", header(scope, IP_HEADER.encode()) if IP_HEADER else "", peer, PROXY_HOPS, ip)
    return ip


def in_nets(ip: str, nets) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in nets)


# ---------------------------------------------------------------- структуры в памяти
class Bounded(OrderedDict):
    """Словарь с ограничением размера: старые ключи вытесняются."""

    def __init__(self, maxlen: int):
        super().__init__()
        self.maxlen = maxlen

    def put(self, k, v):
        self[k] = v
        self.move_to_end(k)
        while len(self) > self.maxlen:
            self.popitem(last=False)


class Limiter:
    """Скользящее окно: не более `limit` событий за `window` секунд на ключ. Отклонённые не считаются."""

    def __init__(self, limit: int, window: float, max_keys: int = 20000):
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self.hits: OrderedDict[str, deque] = OrderedDict()

    def hit(self, key: str) -> bool:
        now = time.monotonic()
        dq = self.hits.get(key)
        if dq is None:
            dq = self.hits[key] = deque()
            while len(self.hits) > self.max_keys:
                self.hits.popitem(last=False)
        else:
            self.hits.move_to_end(key)
        cutoff = now - self.window
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self.limit:
            return False
        dq.append(now)
        return True


class Bans:
    """Временные баны за повторные нарушения. Срок растёт при рецидиве: 10 мин, 20 мин, … до часа."""

    def __init__(self, max_keys: int = 20000):
        self.bans = Bounded(max_keys)      # key -> [до какого момента, число нарушений]
        self.strikes = Bounded(max_keys)   # (kind, key) -> [счётчик, начало окна]

    def banned(self, key: str) -> bool:
        b = self.bans.get(key)
        return bool(b and b[0] > time.monotonic())

    def strike(self, key: str, kind: str = "rl", limit: int = 20, window: float = 60.0) -> bool:
        if in_nets(key.split("/")[0], EXEMPT_NETS):    # ключ IPv6 имеет вид «адрес/64»
            return False
        now = time.monotonic()
        k = (kind, key)
        c = self.strikes.get(k)
        if not c or now - c[1] > window:
            c = [0, now]
        c[0] += 1
        self.strikes.put(k, c)
        if c[0] < limit:
            return False
        self.strikes.pop(k, None)
        off = (self.bans.get(key) or [0, 0])[1] + 1
        self.bans.put(key, [now + min(3600, 600 * 2 ** (off - 1)), off])
        log.warning("ban: %s on %s (offense #%s)", kind, key, off)
        return True


BANS = Bans()
LIMITS = {
    "ping": Limiter(30, 60),
    "tools": Limiter(20, 60),
    "track": Limiter(80, 60),
    "ai": Limiter(20, 60),
    "auth": Limiter(10, 60),
    "admin": Limiter(150, 60),
    "static": Limiter(300, 60),
}


def classify(path: str) -> str:
    if path == "/ping":
        return "ping"
    if path in ("/api/tools", "/api/status"):
        return "tools"
    if path == "/api/track":
        return "track"
    if path == "/api/ai":
        return "ai"
    if path in ("/admin/api/login", "/admin/api/2fa"):
        return "auth"
    if path.startswith("/admin"):
        return "admin"
    return "static"


# ---------------------------------------------------------------- заголовки безопасности
D3_URL = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"   # единственный внешний скрипт сайта
CSP_PUBLIC = (
    f"default-src 'none'; script-src 'self' {D3_URL}; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'; object-src 'none'"
)


def _inline_hashes(html: str, tag: str) -> list[str]:
    out = []
    for m in re.finditer(rf"<{tag}(?![^>]*\bsrc\s*=)[^>]*>(.*?)</{tag}>", html, re.S | re.I):
        if m.group(1).strip():
            out.append("'sha256-" + base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode() + "'")
    return out


def admin_csp(html: str) -> str:
    """CSP админки: встроенные <script>/<style> разрешены только по хэшу, остальное — свои файлы."""
    sc = " ".join(["'self'", D3_URL, *_inline_hashes(html, "script")])
    st = " ".join(["'self'", *_inline_hashes(html, "style")])
    return (f"default-src 'none'; script-src {sc}; style-src {st}; style-src-attr 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'")


_SEC = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"geolocation=(), microphone=(), camera=(), payment=(), usb=(), interest-cohort=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
]


def _cache_value(path: str) -> bytes:
    if path.startswith(("/api/", "/admin", "/ping")):
        return b"no-store"
    return b"public, max-age=300"


def finalize_headers(headers, scope) -> list:
    present = {k.lower() for k, _ in headers}
    out = list(headers)
    for k, v in _SEC:
        if k not in present:
            out.append((k, v))
    if b"content-security-policy" not in present:
        out.append((b"content-security-policy", CSP_PUBLIC.encode()))
    if b"cache-control" not in present:
        out.append((b"cache-control", _cache_value(scope.get("path", "/"))))
    if (HTTPS_SITE or "https" in header(scope, b"x-forwarded-proto")) and b"strict-transport-security" not in present:
        out.append((b"strict-transport-security", b"max-age=31536000"))
    return out


async def _deny(send, scope, status: int, text: str, retry: int | None = None):
    body = text.encode()
    h = [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode()),
         (b"cache-control", b"no-store"), (b"connection", b"close")]
    if retry:
        h.append((b"retry-after", str(retry).encode()))
    await send({"type": "http.response.start", "status": status, "headers": finalize_headers(h, scope)})
    await send({"type": "http.response.body", "body": body})


def host_ok(host: str) -> bool:
    if not ALLOWED_HOSTS:
        return True
    h = host.split(":")[0].lower().rstrip(".")
    return any(h == a or h.endswith("." + a) for a in ALLOWED_HOSTS)


_last_log: dict[str, float] = {}


def _log_limited(key: str, msg: str, *args):
    now = time.monotonic()
    if now - _last_log.get(key, 0) > 10:
        _last_log[key] = now
        log.warning(msg, *args)


class Shield:
    """Самый внешний слой: фильтрует мусор до того, как запрос дойдёт до приложения и базы."""

    def __init__(self, app):
        self.app = app
        self.inflight = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        ip = scope["_cip"] = ip_from_scope(scope)
        key = rl_key(ip)
        path = scope.get("path", "/")
        kind = classify(path)

        if BANS.banned(key):
            return await _deny(send, scope, 429, "too many requests", retry=600)
        if scope.get("method") not in ("GET", "HEAD", "POST"):
            BANS.strike(key, "bad", 30)
            return await _deny(send, scope, 405, "method not allowed")
        if kind != "ping":
            if ALLOWED_HOSTS and not host_ok(header(scope, b"host")):
                BANS.strike(key, "bad", 30)
                return await _deny(send, scope, 400, "bad host")
            if ORIGIN_SECRET and not hmac.compare_digest(
                    header(scope, ORIGIN_HEADER.encode()).encode(), ORIGIN_SECRET.encode()):
                BANS.strike(key, "bad", 30)
                _log_limited("origin", "request without valid origin secret (direct hit?) from %s", key)
                return await _deny(send, scope, 403, "forbidden")
        body_cap = MAX_BODY_AI if kind == "ai" else MAX_BODY
        cl = header(scope, b"content-length")
        # isdigit() пропускает «²» и подобные символы, а int() на них падает: проверяем только ASCII-цифры
        if cl and (not (cl.isascii() and cl.isdigit()) or len(cl) > 9 or int(cl) > body_cap):
            BANS.strike(key, "bad", 30)
            return await _deny(send, scope, 413, "payload too large")
        if not LIMITS[kind].hit(key):
            if BANS.strike(key):
                return await _deny(send, scope, 429, "too many requests", retry=600)
            _log_limited("rl:" + kind, "rate limit (%s) exceeded by %s", kind, key)
            return await _deny(send, scope, 429, "too many requests", retry=30)
        if kind != "ping" and self.inflight >= MAX_INFLIGHT:
            _log_limited("shed", "overloaded: shedding requests (inflight=%s)", self.inflight)
            return await _deny(send, scope, 503, "busy", retry=5)

        started = False
        got = 0

        async def recv():
            nonlocal got
            m = await receive()
            if m["type"] == "http.request":       # тело без Content-Length (chunked) тоже ограничиваем
                got += len(m.get("body", b""))
                if got > body_cap:
                    return {"type": "http.request", "body": b"", "more_body": False}
            return m

        async def out(m):
            nonlocal started
            if m["type"] == "http.response.start":
                started = True
                m = {**m, "headers": finalize_headers(m.get("headers", []), scope)}
            await send(m)

        self.inflight += 1
        try:
            await asyncio.wait_for(self.app(scope, recv, out), REQ_TIMEOUT_AI if kind == "ai" else REQ_TIMEOUT)
        except asyncio.TimeoutError:
            _log_limited("timeout", "request timeout on %s", path)
            if not started:
                await _deny(send, scope, 503, "timeout", retry=5)
        finally:
            self.inflight -= 1


# ---------------------------------------------------------------- логи без секретов
_SECRET_RE = re.compile(r"((?:[?&]|\b)k=)[\w-]{8,}|(\d{6,}:[A-Za-z0-9_-]{30,})")


def _mask(v):
    return _SECRET_RE.sub(lambda m: (m.group(1) + "***") if m.group(1) else "***", v) if isinstance(v, str) else v


class MaskSecrets(logging.Filter):
    """Прячет персональные токены доступа (?k=…) и токен бота в любых логах."""

    def filter(self, record):
        try:
            record.msg = _mask(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_mask(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _mask(v) for k, v in record.args.items()}
        except Exception:
            pass
        return True


def install_log_masking():
    f = MaskSecrets()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "aiogram", "harledy", "harledy.sec"):
        lg = logging.getLogger(name)
        lg.addFilter(f)
        for h in lg.handlers:
            h.addFilter(f)
