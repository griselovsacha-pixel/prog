"""Telegram Mini App (/app): проверка initData и каталог для мини-аппа.

Что здесь:
  * validate_init() — проверка подписи initData (HMAC-SHA256 от токена бота, как в документации Telegram).
    Без неё доверять данным пользователя нельзя: initDataUnsafe на клиенте подделывается.
  * catalog() — каталог для мини-аппа: то же дерево, что и на сайте, но без скрытых веток
    (TMA_HIDE — по названиям и тегам, TMA_HIDE_TEXT — по описанию), и каждому инструменту добавлены
    slug (для ссылок t.me/…?startapp=tool_<slug>) и key (ключ рейтингов/отзывов/статусов).

Только стандартная библиотека, поэтому модуль проверяется отдельно (tests/test_tma.py).
"""
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl

SEP = " › "          # тот же разделитель, что в catalog.py и на сайте
MAX_AGE = 24 * 3600  # initData старше суток не принимаем (при открытии мини-апп Telegram выдаёт свежую)

def validate_init(init_data: str, bot_token: str, max_age: int = MAX_AGE, now: float | None = None) -> dict | None:
    """Возвращает {'user': {...}, 'auth_date': int, 'start_param': str} или None, если подпись неверна/устарела."""
    if not init_data or not bot_token or len(init_data) > 4096:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    got = data.pop("hash", None)
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
        user = json.loads(data.get("user", ""))
    except (ValueError, TypeError):
        return None
    t = time.time() if now is None else now
    if not isinstance(user, dict) or not isinstance(user.get("id"), int) or user["id"] <= 0:
        return None
    if auth_date <= 0 or t - auth_date > max_age or auth_date - t > 300:
        return None
    return {"user": user, "auth_date": auth_date, "start_param": data.get("start_param", "")}


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slugify(s: str) -> str:
    s = "".join(_TR.get(c, c) for c in s.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "tool"


# поля инструмента, которые уходят в мини-апп (остальное, вроде служебного mstatus, не нужно)
FIELDS = ("desc", "bot", "eternal", "type", "price", "price_text", "vpn", "vpn_text", "note", "web", "tags", "aka")

_cache = {"h": None, "out": None}


def catalog(body: bytes | None) -> bytes | None:
    """JSON каталога для мини-аппа: {'tree': {...}, 'count': N}. Кэшируется по хэшу исходного каталога."""
    if not body:
        return None
    h = hashlib.md5(body).hexdigest()
    if _cache["h"] == h:
        return _cache["out"]
    root = json.loads(body)
    slugs, used_links, count = set(), set(), 0

    def uniq(base: str) -> str:
        slug, i = base, 2
        while slug in slugs:
            slug, i = f"{base}-{i}", i + 1
        slugs.add(slug)
        return slug

    def walk(n: dict, path: list[str]) -> dict:
        nonlocal count
        kids = []
        for c in n.get("children") or []:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name", "")).strip()
            if isinstance(c.get("children"), list):
                if not name:
                    continue
                sub = walk(c, path + [name])
                if sub["children"]:
                    kids.append(sub)
                continue
            link = str(c.get("bot") or "").strip()
            if not name or not link or link in used_links or not path:
                continue
            used_links.add(link)
            t = {"name": name, "slug": uniq(slugify(name)), "key": SEP.join(path + [name])}
            for f in FIELDS:
                if c.get(f) not in (None, ""):
                    t[f] = c[f]
            kids.append(t)
            count += 1
        return {"name": n.get("name", ""), "children": kids}

    tree = walk(root, [])
    out = json.dumps({"tree": tree, "count": count}, ensure_ascii=False, separators=(",", ":")).encode()
    _cache.update(h=h, out=out)
    return out
