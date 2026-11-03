"""Рекламный баннер сайта: один баннер, настраивается из админки (вкладка «Баннер»).

Хранится в таблице site_banner (одна строка). Сайт получает его через /api/banner (только с токеном доступа),
показывает в левом нижнем углу, посетитель может закрыть — закрытие помнится, пока админ не изменит баннер.
"""
import json
from urllib.parse import urlsplit

SCHEMA = """
create table if not exists site_banner(
    id int primary key default 1 check (id = 1),
    data jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);
alter table site_banner enable row level security;
"""

LIMITS = {"title": 60, "text": 200, "button": 40, "url": 500}
DEFAULT = {"enabled": False, "title": "", "text": "", "button": "Попробовать бесплатно", "url": "", "icon": True}


def _url_ok(u: str) -> bool:
    """Только https/http: «javascript:» и подобное в ссылку баннера не попадает."""
    try:
        p = urlsplit(u)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def clean(d: dict) -> dict:
    """Проверка того, что прислала админка. ValueError — текст ошибки для админа."""
    if not isinstance(d, dict):
        raise ValueError("Неверные данные")
    out = dict(DEFAULT)
    for k, n in LIMITS.items():
        v = d.get(k, "")
        if not isinstance(v, str):
            raise ValueError("Неверные данные")
        out[k] = v.strip()[:n]
    out["enabled"] = bool(d.get("enabled"))
    out["icon"] = bool(d.get("icon", True))
    if out["url"] and not _url_ok(out["url"]):
        raise ValueError("Ссылка должна начинаться с https://")
    if out["enabled"]:
        if not out["title"]:
            raise ValueError("Заполните заголовок")
        if not out["url"]:
            raise ValueError("Укажите ссылку для кнопки")
        if not out["button"]:
            raise ValueError("Укажите текст кнопки")
    return out


async def load(pool) -> dict:
    """{**баннер, 'v': версия}. Нет строки — выключенный баннер по умолчанию."""
    row = await pool.fetchrow("select data, extract(epoch from updated_at)::bigint v from site_banner where id = 1")
    if not row:
        return {**DEFAULT, "v": 0}
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    return {**DEFAULT, **(data or {}), "v": int(row["v"])}


async def save(pool, d: dict) -> dict:
    c = clean(d)
    await pool.execute(
        """insert into site_banner(id, data, updated_at) values (1, $1::jsonb, now())
           on conflict (id) do update set data = excluded.data, updated_at = now()""",
        json.dumps(c, ensure_ascii=False))
    return await load(pool)


def public(b: dict) -> dict:
    """Что отдаём сайту: выключенный баннер — только {'on': False}, без текста и ссылки."""
    if not b.get("enabled") or not b.get("url"):
        return {"on": False}
    return {"on": True, "v": b["v"], "title": b["title"], "text": b["text"], "button": b["button"],
            "url": b["url"], "icon": b["icon"]}
