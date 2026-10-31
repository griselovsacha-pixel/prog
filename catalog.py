"""Каталог инструментов в базе (Supabase / Postgres).

Как это работает:
  * Дерево хранится в таблице catalog_nodes (категории и инструменты в одной таблице).
  * Из таблицы собирается ТОТ ЖЕ JSON, что раньше лежал в data/tools.json, и держится в памяти (bytes).
    /api/tools, ИИ, проверка статусов и бот читают эти байты — в базу на запрос никто не ходит,
    поэтому скорость такая же, как у файла.
  * Любое изменение через админку сразу пересобирает кэш. Правки напрямую в таблице Supabase
    подхватываются фоновой проверкой (poll) за ~20 секунд.
  * Первый запуск: если таблица пустая, она заполняется из data/tools.json (или TOOLS_FILE / секретного файла).
"""
import asyncio
import json
import logging
import re
from urllib.parse import urlsplit

import asyncpg

import monitor as mon

log = logging.getLogger("harledy.catalog")

SEP = " › "                      # разделитель пути: «Категория › Подкатегория › Название» (ключ рейтингов/статусов)
MAX_KEY = 190                    # /api/track и /api/rate принимают названия до 200 символов
MAX_DEPTH = 6

TYPES = ("bot", "site", "bot+site", "bot+miniapp")
PRICES = ("free", "freemium", "paid", "unknown")
VPNS = ("no", "yes", "unknown")
STATES = ("ok", "no_reply", "deleted")       # ручные статусы; пусто = «авто» (решает ежедневная проверка)

TEXT_FIELDS = ("desc", "bot", "eternal", "type", "price", "price_text", "vpn", "vpn_text", "note")
KNOWN = set(TEXT_FIELDS) | {"name", "children", "web", "tags", "aka", "extra", "mstatus"}

SCHEMA = """
create table if not exists catalog_nodes(
    id bigserial primary key,
    parent_id bigint references catalog_nodes(id) on delete cascade,
    pos int not null default 0,
    kind text not null check (kind in ('category', 'tool')),
    name text not null,
    "desc" text not null default '',
    bot text not null default '',
    eternal text not null default '',
    type text not null default 'bot',
    price text not null default 'unknown',
    price_text text not null default '',
    vpn text not null default 'unknown',
    vpn_text text not null default '',
    web boolean,
    note text not null default '',
    tags jsonb not null default '[]'::jsonb,
    aka jsonb not null default '[]'::jsonb,
    extra jsonb not null default '[]'::jsonb,
    rest jsonb not null default '{}'::jsonb,
    mstatus text check (mstatus in ('ok', 'no_reply', 'deleted')),
    updated_at timestamptz not null default now()
);
create index if not exists catalog_parent_idx on catalog_nodes(parent_id, pos);
create unique index if not exists catalog_sibling_uq on catalog_nodes(coalesce(parent_id, 0), lower(name));
create or replace function catalog_touch() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;
drop trigger if exists catalog_touch_trg on catalog_nodes;
create trigger catalog_touch_trg before update on catalog_nodes for each row execute function catalog_touch();
alter table catalog_nodes enable row level security;
"""

_lock = asyncio.Lock()
_state = {"body": None, "manual": {}, "sig": None}


# ====================== кэш в памяти ======================
def body() -> bytes | None:
    """Готовый JSON каталога (bytes) — отдаётся мгновенно, без базы."""
    return _state["body"]


def manual_status() -> dict:
    """Ручные статусы: ключ инструмента -> {s, m, k, a, bad, manual} (формат /api/status)."""
    return _state["manual"]


def _jl(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _node_json(r, kids: dict) -> dict:
    if r["kind"] == "category":
        return {"name": r["name"], "children": [_node_json(c, kids) for c in kids.get(r["id"], [])]}
    out = {"name": r["name"]}
    for f in ("desc", "bot", "eternal", "type", "price", "price_text", "vpn", "vpn_text", "note"):
        if r[f] not in (None, ""):
            out[f] = r[f]
    if r["web"] is not None:
        out["web"] = bool(r["web"])
    out["tags"] = _jl(r["tags"]) or []
    for f in ("aka", "extra"):
        v = _jl(r[f])
        if v:
            out[f] = v
    rest = _jl(r["rest"])
    if isinstance(rest, dict):
        for k, v in rest.items():
            out.setdefault(k, v)
    if r["mstatus"]:
        out["mstatus"] = r["mstatus"]        # сайт его игнорирует; проверка статусов пропускает такие инструменты
    return out


def _build(rows) -> dict | None:
    kids: dict = {}
    root = None
    for r in sorted(rows, key=lambda r: (r["pos"], r["id"])):
        if r["parent_id"] is None:
            root = root or r
        else:
            kids.setdefault(r["parent_id"], []).append(r)
    return _node_json(root, kids) if root else None


def _keys(rows) -> dict:
    """id -> ключ инструмента («Категория › … › Название», без имени корня). У корня ключ пустой."""
    by_id = {r["id"]: r for r in rows}
    out = {}
    for r in rows:
        names, cur, guard = [], r, 0
        while cur is not None and cur["parent_id"] is not None and guard < 50:
            names.append(cur["name"])
            cur = by_id.get(cur["parent_id"])
            guard += 1
        out[r["id"]] = SEP.join(reversed(names))
    return out


def _manual_for(rows, tree: dict) -> dict:
    keys = _keys(rows)
    kinds = {i["tool"]: i["kind"] for i in mon.collect(_strip_manual_flag(tree))}
    out = {}
    for r in rows:
        if r["kind"] != "tool" or not r["mstatus"]:
            continue
        key = keys[r["id"]]
        k = kinds.get(key)       # вид определяет сама ссылка: t.me/бот → бот, остальное → сайт
        kind = "bot" if (k in ("bot", "tg_link") or (k is None and str(r["type"]) == "bot")) else "site"
        state = r["mstatus"]
        out[key] = {"s": state, "m": mon.manual_message(kind, state), "k": kind, "a": 0,
                    "bad": state in mon.BAD, "manual": True}
    return out


def _strip_manual_flag(tree: dict) -> dict:
    """mon.collect пропускает инструменты с ручным статусом; для определения вида ссылки нужен полный список."""
    def walk(n):
        if isinstance(n.get("children"), list):
            return {**n, "children": [walk(c) for c in n["children"]]}
        return {k: v for k, v in n.items() if k != "mstatus"}
    return walk(tree)


def _sig(rows):
    return (len(rows), max((r["updated_at"] for r in rows), default=None))


async def refresh(pool) -> bool:
    """Пересобрать кэш из базы. При сбое базы остаётся прежний кэш."""
    async with _lock:
        rows = await pool.fetch("select * from catalog_nodes")
        tree = _build(rows)
        if tree is None:
            return False
        _state["body"] = json.dumps(tree, ensure_ascii=False, separators=(",", ":")).encode()
        _state["manual"] = _manual_for(rows, tree)
        _state["sig"] = _sig(rows)
        return True


async def poll(pool) -> None:
    """Дёшево проверяет, не менялась ли таблица (в т.ч. руками в Supabase), и при изменении пересобирает кэш."""
    try:
        r = await pool.fetchrow("select count(*) n, max(updated_at) m from catalog_nodes")
        if (r["n"], r["m"]) != _state["sig"]:
            await refresh(pool)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("не удалось обновить каталог из базы (остаётся прежний): %s", e)


# ====================== первый запуск ======================
async def _insert(conn, node: dict, parent_id, pos: int) -> int:
    is_cat = isinstance(node.get("children"), list)
    rest = {k: v for k, v in node.items() if k not in KNOWN}
    vals = {f: str(node.get(f) or "") for f in TEXT_FIELDS}
    vals["type"] = vals["type"] or "bot"
    vals["price"] = vals["price"] or "unknown"
    vals["vpn"] = vals["vpn"] or "unknown"
    web = node.get("web")
    ms = node.get("mstatus") if node.get("mstatus") in STATES else None
    nid = await conn.fetchval(
        """insert into catalog_nodes(parent_id, pos, kind, name, "desc", bot, eternal, type, price, price_text,
                                      vpn, vpn_text, web, note, tags, aka, extra, rest, mstatus)
           values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb,$16::jsonb,$17::jsonb,$18::jsonb,$19)
           returning id""",
        parent_id, pos, "category" if is_cat else "tool", str(node.get("name") or "").strip()[:100] or "Без названия",
        vals["desc"], vals["bot"], vals["eternal"], vals["type"], vals["price"], vals["price_text"],
        vals["vpn"], vals["vpn_text"], web if isinstance(web, bool) else None, vals["note"],
        json.dumps(node.get("tags") or [], ensure_ascii=False),
        json.dumps(node.get("aka") or [], ensure_ascii=False),
        json.dumps(node.get("extra") or [], ensure_ascii=False),
        json.dumps(rest, ensure_ascii=False), ms,
    )
    if is_cat:
        for i, c in enumerate(node["children"]):
            if isinstance(c, dict):
                await _insert(conn, c, nid, i)
    return nid


async def init(pool, seed_body: bytes | None) -> None:
    """Создаёт таблицу, при пустой базе переносит туда tools.json, загружает кэш."""
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
        async with c.transaction():
            await c.execute("select pg_advisory_xact_lock(7311)")     # два запуска сразу не зальют дерево дважды
            n = await c.fetchval("select count(*) from catalog_nodes")
            if not n and seed_body:
                tree = json.loads(seed_body)
                await _insert(c, tree, None, 0)
                log.info("каталог перенесён из файла в базу: %d узлов", await c.fetchval("select count(*) from catalog_nodes"))
    await refresh(pool)


# ====================== админка: чтение ======================
async def admin_list(pool) -> dict:
    rows = await pool.fetch("select * from catalog_nodes order by pos, id")
    keys = _keys(rows)
    st = {r["tool"]: r for r in await pool.fetch("select tool, state, message, checked_at from tool_status")}
    nodes = []
    for r in rows:
        auto = st.get(keys[r["id"]]) if r["kind"] == "tool" else None
        nodes.append({
            "id": r["id"], "parent_id": r["parent_id"], "pos": r["pos"], "kind": r["kind"], "name": r["name"],
            "desc": r["desc"], "bot": r["bot"], "eternal": r["eternal"], "type": r["type"], "price": r["price"],
            "price_text": r["price_text"], "vpn": r["vpn"], "vpn_text": r["vpn_text"], "web": r["web"],
            "note": r["note"], "tags": _jl(r["tags"]) or [], "aka": _jl(r["aka"]) or [], "extra": _jl(r["extra"]) or [],
            "mstatus": r["mstatus"], "auto_state": auto["state"] if auto else None,
            "auto_msg": auto["message"] if auto else None,
        })
    return {"nodes": nodes}


# ====================== админка: проверка данных ======================
def _name(v) -> str:
    s = str(v or "").strip()
    if not s or len(s) > 60:
        raise ValueError("Название: от 1 до 60 символов")
    if re.search(r"[\x00-\x1f\x7f<>]", s) or "›" in s:
        raise ValueError("В названии нельзя символы < > › и управляющие")
    return s


def _url(v, label: str) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    if len(s) > 600 or re.search(r"\s", s):
        raise ValueError(f"{label}: ссылка слишком длинная или с пробелами")
    if urlsplit(s).scheme.lower() not in ("http", "https", "tg"):
        raise ValueError(f"{label}: ссылка должна начинаться с https://, http:// или tg://")
    return s


def _text(v, label: str, limit: int) -> str:
    s = str(v or "").replace("\r\n", "\n").strip()
    if len(s) > limit:
        raise ValueError(f"{label}: не больше {limit} символов")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", s):
        raise ValueError(f"{label}: недопустимые символы")
    return s


def _list(v, label: str, n: int = 30, each: int = 40) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    if not isinstance(v, list):
        raise ValueError(f"{label}: нужен список")
    out = []
    for x in v:
        s = _text(x, label, each)
        if s and s not in out:
            out.append(s)
    if len(out) > n:
        raise ValueError(f"{label}: не больше {n} значений")
    return out


def _extra(v) -> list:
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > 12:
        raise ValueError("Бирки: список до 12 штук")
    out = []
    for e in v:
        if not isinstance(e, dict):
            raise ValueError("Бирки: неверный формат")
        k, t = _text(e.get("k"), "Бирка (ключ)", 20), _text(e.get("t"), "Бирка (текст)", 80)
        if t:
            out.append({"k": k or "info", "t": t})
    return out


def _choice(v, allowed, label: str):
    if v not in allowed:
        raise ValueError(f"{label}: допустимо {', '.join(allowed)}")
    return v


def _clean_tool(d: dict) -> dict:
    """Проверяет только переданные поля инструмента, возвращает {колонка: значение}."""
    out = {}
    if "name" in d:
        out["name"] = _name(d["name"])
    if "desc" in d:
        out["desc"] = _text(d["desc"], "Описание", 2000)
    for f, label in (("bot", "Ссылка"), ("eternal", "Вечная ссылка")):
        if f in d:
            out[f] = _url(d[f], label)
    if "type" in d:
        out["type"] = _choice(d["type"], TYPES, "Тип")
    if "price" in d:
        out["price"] = _choice(d["price"], PRICES, "Цена")
    if "vpn" in d:
        out["vpn"] = _choice(d["vpn"], VPNS, "VPN")
    for f, label in (("price_text", "Текст цены"), ("vpn_text", "Текст VPN")):
        if f in d:
            out[f] = _text(d[f], label, 120)
    if "note" in d:
        out["note"] = _text(d["note"], "Заметка", 300)
    if "web" in d:
        out["web"] = None if d["web"] is None else bool(d["web"])
    if "tags" in d:
        out["tags"] = json.dumps(_list(d["tags"], "Теги"), ensure_ascii=False)
    if "aka" in d:
        out["aka"] = json.dumps(_list(d["aka"], "Другие названия"), ensure_ascii=False)
    if "extra" in d:
        out["extra"] = json.dumps(_extra(d["extra"]), ensure_ascii=False)
    if "mstatus" in d:
        ms = d["mstatus"]
        out["mstatus"] = None if ms in (None, "", "auto") else _choice(ms, STATES, "Статус")
    return out


_CASTS = {"tags": "::jsonb", "aka": "::jsonb", "extra": "::jsonb"}
_COLS = {"name", "desc", "bot", "eternal", "type", "price", "price_text", "vpn", "vpn_text", "note", "web",
         "tags", "aka", "extra", "mstatus", "parent_id", "pos"}


def _set_sql(cols: dict, start: int = 1):
    sets, args = [], []
    for i, (k, v) in enumerate(cols.items(), start):
        assert k in _COLS
        sets.append(f'"{k}" = ${i}{_CASTS.get(k, "")}')
        args.append(v)
    return ", ".join(sets), args


# ====================== админка: изменения ======================
async def _rekey(conn, old: str, new: str) -> None:
    """Переименование/перенос: рейтинги, статусы и события переезжают на новый ключ (и для вложенных узлов)."""
    if not old or old == new:
        return
    for t in ("ratings", "tool_status"):                    # на новом месте не должно остаться чужих хвостов
        await conn.execute(f"delete from {t} where tool = $1 or left(tool, length($1) + 3) = $1 || ' › '", new)
    for t in ("ratings", "tool_status", "events"):
        await conn.execute(
            f"update {t} set tool = $2 || substr(tool, length($1) + 1) "
            f"where tool = $1 or left(tool, length($1) + 3) = $1 || ' › '", old, new)


def _check_depth_and_len(rows) -> None:
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        depth, cur = 0, r
        while cur["parent_id"] is not None:
            cur = by_id[cur["parent_id"]]
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError(f"Слишком глубоко: не больше {MAX_DEPTH} уровней")
    for k in _keys(rows).values():
        if len(k) > MAX_KEY:
            raise ValueError("Путь «Категория › … › Название» слишком длинный, сократите названия")


async def _next_pos(conn, parent_id) -> int:
    return await conn.fetchval("select coalesce(max(pos) + 1, 0) from catalog_nodes where parent_id = $1", parent_id)


def _is_descendant(rows, node_id: int, maybe_child: int) -> bool:
    by_id = {r["id"]: r for r in rows}
    cur = by_id.get(maybe_child)
    while cur is not None:
        if cur["id"] == node_id:
            return True
        cur = by_id.get(cur["parent_id"]) if cur["parent_id"] is not None else None
    return False


async def create(pool, d: dict) -> int:
    kind = d.get("kind")
    if kind not in ("category", "tool"):
        raise ValueError("kind: category или tool")
    try:
        parent_id = int(d.get("parent_id"))
    except (TypeError, ValueError):
        raise ValueError("Не выбрана ветка")
    fields = _clean_tool(d) if kind == "tool" else {"name": _name(d.get("name"))}
    if "name" not in fields:
        raise ValueError("Название: от 1 до 60 символов")
    try:
        async with pool.acquire() as c, c.transaction():
            par = await c.fetchrow("select id, kind from catalog_nodes where id = $1 for update", parent_id)
            if not par or par["kind"] != "category":
                raise ValueError("Родитель должен быть категорией")
            fields.update(parent_id=parent_id, kind=kind, pos=await _next_pos(c, parent_id))
            cols = list(fields)
            ph = ", ".join(f"${i}{_CASTS.get(k, '')}" for i, k in enumerate(cols, 1))
            names = ", ".join(f'"{k}"' for k in cols)
            nid = await c.fetchval(f"insert into catalog_nodes({names}) values({ph}) returning id", *fields.values())
            _check_depth_and_len(await c.fetch("select id, parent_id, name from catalog_nodes"))
    except asyncpg.UniqueViolationError:
        raise ValueError("В этой ветке уже есть элемент с таким названием")
    await refresh(pool)
    return nid


async def update(pool, d: dict) -> None:
    try:
        nid = int(d.get("id"))
    except (TypeError, ValueError):
        raise ValueError("Не указан id")
    try:
        async with pool.acquire() as c, c.transaction():
            rows = await c.fetch("select id, parent_id, name, kind from catalog_nodes for update")
            cur = next((r for r in rows if r["id"] == nid), None)
            if not cur:
                raise ValueError("Элемент не найден")
            fields = _clean_tool(d) if cur["kind"] == "tool" else ({"name": _name(d["name"])} if "name" in d else {})
            if "parent_id" in d and d["parent_id"] != cur["parent_id"]:
                if cur["parent_id"] is None:
                    raise ValueError("Корень переносить нельзя")
                try:
                    np = int(d["parent_id"])
                except (TypeError, ValueError):
                    raise ValueError("Неверная ветка")
                target = next((r for r in rows if r["id"] == np), None)
                if not target or target["kind"] != "category":
                    raise ValueError("Переносить можно только в категорию")
                if _is_descendant(rows, nid, np):
                    raise ValueError("Нельзя перенести ветку внутрь самой себя")
                fields["parent_id"] = np
                fields["pos"] = await _next_pos(c, np)
            if not fields:
                return
            old_key = _keys(rows)[nid]
            sets, args = _set_sql(fields)
            await c.execute(f"update catalog_nodes set {sets} where id = ${len(args) + 1}", *args, nid)
            new_rows = await c.fetch("select id, parent_id, name, kind from catalog_nodes")
            _check_depth_and_len(new_rows)
            await _rekey(c, old_key, _keys(new_rows)[nid])
    except asyncpg.UniqueViolationError:
        raise ValueError("В этой ветке уже есть элемент с таким названием")
    await refresh(pool)


async def delete(pool, nid) -> None:
    try:
        nid = int(nid)
    except (TypeError, ValueError):
        raise ValueError("Не указан id")
    async with pool.acquire() as c, c.transaction():
        rows = await c.fetch("select id, parent_id, name, kind from catalog_nodes for update")
        cur = next((r for r in rows if r["id"] == nid), None)
        if not cur:
            raise ValueError("Элемент не найден")
        if cur["parent_id"] is None:
            raise ValueError("Корневую ветку удалить нельзя")
        key = _keys(rows)[nid]
        await c.execute("delete from catalog_nodes where id = $1", nid)      # вложенное удаляется каскадом
        for t in ("ratings", "tool_status"):
            await c.execute(f"delete from {t} where tool = $1 or left(tool, length($1) + 3) = $1 || ' › '", key)
    await refresh(pool)


async def move(pool, nid, direction) -> None:
    """Сдвинуть вверх (-1) или вниз (+1) среди соседей."""
    try:
        nid, direction = int(nid), int(direction)
    except (TypeError, ValueError):
        raise ValueError("Неверные данные")
    if direction not in (-1, 1):
        raise ValueError("Неверное направление")
    async with pool.acquire() as c, c.transaction():
        cur = await c.fetchrow("select id, parent_id from catalog_nodes where id = $1", nid)
        if not cur or cur["parent_id"] is None:
            raise ValueError("Элемент не найден")
        sib = [r["id"] for r in await c.fetch(
            "select id from catalog_nodes where parent_id = $1 order by pos, id for update", cur["parent_id"])]
        i = sib.index(nid)
        j = i + direction
        if 0 <= j < len(sib):
            sib[i], sib[j] = sib[j], sib[i]
        await c.execute(
            "update catalog_nodes n set pos = v.p from unnest($1::bigint[], $2::int[]) as v(id, p) where n.id = v.id",
            sib, list(range(len(sib))))
    await refresh(pool)
