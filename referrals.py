"""Профиль и реферальная система: логика, запросы к базе и тексты сообщений.

Модуль не импортирует aiogram и main.py, поэтому проверяется отдельно (tests/test_referrals.py).
Кнопки и обработчики лежат в bot_profile.py, а сюда они передают pool (asyncpg) и send(chat_id, text).

Как работает реферал:
  1. Человек открывает бота по ссылке t.me/<бот>?start=ref_<id пригласившего>. Если он в боте впервые,
     создаётся запись referrals со статусом pending, а пригласившему приходит «зашёл, но не перешёл в инструмент».
  2. Когда этот человек в мини-аппе или на сайте переходит в инструмент (события QUALIFY в /api/track),
     запись переводится в paid (+REWARD к балансу) или в twink (+0), если у него тот же IP и User-Agent,
     что у пригласившего или у уже засчитанных рефералов этого же пригласившего.
"""
import asyncio
import html
import json
import logging
import re

log = logging.getLogger("harledy.refs")

REWARD = 1                      # звёзд за одного засчитанного реферала
# подарки для вывода (как в меню Telegram): (смайлик, цена в звёздах), по два в ряду, последний — один
GIFTS = [("🧸", 15), ("💝", 15), ("🌹", 25), ("🎁", 25), ("🍾", 50), ("💐", 50),
         ("🚀", 50), ("🎂", 50), ("🏆", 100), ("💍", 100), ("💎", 100)]
MIN_WITHDRAW = min(a for _, a in GIFTS)     # вывод доступен от самого дешёвого подарка
MAX_PENDING = 3                 # необработанных заявок на вывод от одного человека
QUALIFY = ("bot", "eternal")    # события /api/track, после которых реферал засчитывается («перешёл в инструмент»)
SEP = " › "                     # разделитель пути инструмента, как в catalog.py и на сайте
START_RE = re.compile(r"ref_(\d{1,15})")

SCHEMA = """
alter table users add column if not exists balance integer not null default 0;
create table if not exists referrals(
    referred_id bigint primary key,
    referrer_id bigint not null,
    status text not null default 'pending',
    created_at timestamptz not null default now(),
    resolved_at timestamptz
);
create index if not exists referrals_referrer_idx on referrals(referrer_id, status);
alter table referrals enable row level security;
create table if not exists withdrawals(
    id bigserial primary key,
    tg_id bigint not null,
    gift text not null,
    amount integer not null,
    balance_before integer not null,
    invited integer not null default 0,
    status text not null default 'pending',
    created_at timestamptz not null default now(),
    resolved_at timestamptz
);
create index if not exists withdrawals_user_idx on withdrawals(tg_id, status);
alter table withdrawals enable row level security;
"""

# ---- эмодзи в тексте сообщений: ключ → (ID, запасной смайлик)
EM = {
    "head":    ("6044356915029348425", "👤"),   # заголовок «Профиль»
    "name":    ("6032994772321309200", "🪪"),   # Имя
    "balance": ("5904462880941545555", "💰"),   # Баланс
    "refs":    ("6033108709213736873", "👥"),   # Рефералов
    "recent":  ("5766994197705921104", "🕘"),   # Недавние инструменты
    "star":    ("5330122687136158530", "⭐"),   # звезда
    "ref":     ("6032644646587338669", "👥"),   # уведомления о рефералах
    "ref_head": ("6033125983572201397", "🤝"),  # заголовок «Реферальная система»
}


def em(key: str) -> str:
    eid, fb = EM[key]
    return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'


def e(s) -> str:
    return html.escape(str(s or ""), quote=False)


# ---------------------------------------------------------------- мелкие помощники
def plural(n: int, forms: tuple[str, str, str]) -> str:
    """1 день, 2 дня, 5 дней."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def human_age(seconds: float) -> str:
    """«3 дня 5 часов», «2 часа 10 минут», «7 минут»: не больше двух единиц."""
    s = max(0, int(seconds))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    days, hours, mins = (f"{d} {plural(d, ('день', 'дня', 'дней'))}", f"{h} {plural(h, ('час', 'часа', 'часов'))}",
                         f"{m} {plural(m, ('минута', 'минуты', 'минут'))}")
    if d:
        return f"{days} {hours}" if h else days
    if h:
        return f"{hours} {mins}" if m else hours
    return mins if m else "меньше минуты"


def mention(uid: int, username: str | None, full_name: str | None) -> str:
    """@username, а если его нет — имя со ссылкой на профиль."""
    if username:
        return "@" + e(username)
    return f'<a href="tg://user?id={int(uid)}">{e(full_name) or uid}</a>'


def parse_ref(payload: str | None) -> int | None:
    m = START_RE.fullmatch((payload or "").strip())
    return int(m.group(1)) if m else None


def catalog_links(body: bytes | None) -> dict[str, tuple[str, str]]:
    """Путь инструмента («Категория › Подкатегория › Название») → (название, ссылка). Ключи те же, что пишет /api/track."""
    out: dict[str, tuple[str, str]] = {}
    if not body:
        return out
    try:
        root = json.loads(body)
    except ValueError:
        return out

    def walk(n: dict, path: list[str]):
        for c in n.get("children") or []:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name", "")).strip()
            if isinstance(c.get("children"), list):
                if name:
                    walk(c, path + [name])
                continue
            link = str(c.get("bot") or c.get("eternal") or "").strip()
            if name and path and link.startswith(("http://", "https://")):
                out[SEP.join(path + [name])] = (name, link)

    walk(root, [])
    return out


# ---------------------------------------------------------------- рефералы
async def register(pool, send, u, payload: str | None, existed: bool) -> bool:
    """/start с кодом ref_<id>: запоминаем реферала и сообщаем пригласившему. Только для тех, кто в боте впервые."""
    try:
        rid = parse_ref(payload)
        if rid is None or existed or rid == u.id:
            return False
        if not await pool.fetchval("select 1 from users where tg_id = $1", rid):
            return False
        res = await pool.execute(
            "insert into referrals(referred_id, referrer_id) values($1, $2) on conflict do nothing", u.id, rid)
        if not str(res).endswith(" 1"):          # «INSERT 0 1» — запись создана
            return False
        who = mention(u.id, getattr(u, "username", None), getattr(u, "full_name", None))
        await send(rid, f"{em('ref')} {who} зашёл в бота, но ещё не перешёл в инструмент.\n"
                        "<i>Реферал будет засчитан, когда он откроет мини-апп или сайт и перейдёт в любой инструмент.</i>")
        return True
    except Exception as ex:                        # реферальная система не должна ломать /start
        log.warning("referral register failed: %s", ex)
        return False


async def is_twink(pool, uid: int, rid: int) -> bool:
    """Твинк: тот же IP и тот же User-Agent, что у пригласившего или у других засчитанных рефералов этого пригласившего.
    Префикс «[tma] » (метка мини-аппа) при сравнении не учитывается: устройство то же."""
    others = await pool.fetch(
        "select referred_id from referrals where referrer_id = $1 and status = 'paid' and referred_id <> $2 limit 500",
        rid, uid)
    ids = [rid] + [r["referred_id"] for r in others]
    return bool(await pool.fetchval(
        """select 1
           from sessions a
           join sessions b on b.ip = a.ip
            and regexp_replace(b.user_agent, '^\\[tma\\] ', '') = regexp_replace(a.user_agent, '^\\[tma\\] ', '')
           where a.tg_id = $1 and b.tg_id = any($2::bigint[]) and b.tg_id <> a.tg_id
             and coalesce(a.ip, '') <> '' and coalesce(a.user_agent, '') <> ''
           limit 1""", uid, ids))


async def on_tool_event(pool, send, uid: int) -> str | None:
    """Вызывается, когда человек перешёл в инструмент. Возвращает 'paid', 'twink' или None (он не чей-то реферал)."""
    try:
        row = await pool.fetchrow("select referrer_id from referrals where referred_id = $1 and status = 'pending'", uid)
        if not row:
            return None
        rid = row["referrer_id"]
        status = "twink" if await is_twink(pool, uid, rid) else "paid"
        async with pool.acquire() as c:
            async with c.transaction():
                got = await c.fetchval(      # переводим из pending ровно один раз, даже при гонке двух запросов
                    "update referrals set status = $2, resolved_at = now() "
                    "where referred_id = $1 and status = 'pending' returning referrer_id", uid, status)
                if got is None:
                    return None
                if status == "paid":
                    await c.execute("update users set balance = balance + $2 where tg_id = $1", rid, REWARD)
        if status == "twink":
            await send(rid, f"{em('ref')} Замечен новый реферал, но он является вашим твинком. <b>+0</b> {em('star')}")
        else:
            u = await pool.fetchrow("select username, full_name from users where tg_id = $1", uid)
            who = mention(uid, u and u["username"], u and u["full_name"])
            await send(rid, f"{em('ref')} У вас новый реферал {who}! <b>+{REWARD}</b> {em('star')}")
        return status
    except Exception as ex:
        log.warning("referral check failed for %s: %s", uid, ex)
        return None


_bg: set = set()


def spawn(coro) -> None:
    """Запускает фоном (не задерживает ответ сайту) и держит ссылку, чтобы задачу не убрал сборщик мусора."""
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


# ---------------------------------------------------------------- профиль и реферальный экран
async def profile_data(pool, uid: int) -> dict | None:
    r = await pool.fetchrow(
        """select u.balance, extract(epoch from now() - u.created_at)::bigint as age,
                  (select count(*) from referrals r where r.referrer_id = u.tg_id and r.status = 'paid') as refs
           from users u where u.tg_id = $1""", uid)
    return dict(r) if r else None


async def recent_tools(pool, uid: int, catalog_body: bytes | None, limit: int = 5) -> list[tuple[str, str | None]]:
    """Последние инструменты человека: [(название, ссылка или None)]. Ссылки берём из текущего каталога."""
    rows = await pool.fetch(
        """select tool from events where tg_id = $1 and tool is not null and ev in ('select', 'bot', 'eternal')
           group by tool order by max(created_at) desc limit $2""", uid, limit)
    links = catalog_links(catalog_body)
    out = []
    for r in rows:
        hit = links.get(r["tool"])
        out.append(hit if hit else (r["tool"].split(SEP)[-1], None))
    return out


def profile_text(name: str, data: dict, recent: list[tuple[str, str | None]]) -> str:
    lines = [f"{em('head')} <b>Профиль</b>", "",
             f"{em('name')} <b>Имя:</b> {e(name)}",
             f"{em('balance')} <b>Баланс:</b> {data['balance']} {em('star')}",
             f"Вы уже пользуетесь ботом: <b>{human_age(data['age'])}</b>",
             f"{em('refs')} <b>Рефералов:</b> {data['refs']}", "",
             f"{em('recent')} <b>Недавние инструменты:</b>"]
    if recent:
        for title, url in recent:
            lines.append(f'• <a href="{html.escape(url, quote=True)}">{e(title)}</a>' if url else f"• {e(title)}")
    else:
        lines.append("<i>Пока пусто. Откройте мини-апп или сайт и выберите инструмент.</i>")
    return "\n".join(lines)


def ref_text(link: str | None, balance: int, count: int) -> str:
    mine = (f"<b>Ваша ссылка</b> (нажмите, чтобы скопировать):\n<code>{html.escape(link)}</code>"
            if link else "<b>Ваша ссылка</b> временно недоступна, попробуйте позже.")
    return (f"{em('ref_head')} <b>Реферальная система</b>\n\n"
            f"Приглашайте друзей по своей ссылке и получайте звёзды. За каждого приведённого вами реферала мы платим "
            f"<b>{REWARD} {em('star')}</b>: 1 реферал = 1 звезда.\n"
            f"Вывод доступен от <b>{MIN_WITHDRAW} {em('star')}</b>, звёзды выдаются подарком на ваш выбор.\n\n"
            "<b>Важно!</b> Реферал засчитывается только после того, как он откроет мини-апп или сайт и перейдёт в какой-либо "
            "инструмент. Так мы защищаемся от ботов. За твинки (дополнительные аккаунты одного человека) мы не платим.\n\n"
            f"{mine}\n\n"
            f"{em('balance')} <b>Ваш баланс:</b> {balance} {em('star')}\n"
            f"{em('refs')} <b>Засчитано рефералов:</b> {count}")


async def balance_and_count(pool, uid: int) -> tuple[int, int]:
    d = await profile_data(pool, uid)
    return (d["balance"], d["refs"]) if d else (0, 0)


# ---------------------------------------------------------------- вывод звёзд подарком
def picker_text(balance: int) -> str:
    return (f"{em('star')} <b>Выберите подарок</b>, которым хотите получить звёзды.\n\n"
            f"Ваш баланс: <b>{balance}</b> {em('star')}")


def confirm_text(idx: int, balance: int) -> str:
    gift, amount = GIFTS[idx]
    return (f"{em('star')} <b>Вы точно хотите вывести {amount} {em('star')} подарком {gift}?</b>\n\n"
            f"Ваш баланс: {balance} {em('star')}. После подтверждения сумма спишется с баланса, "
            "а заявка уйдёт на проверку.")


def low_text(balance: int, need: int) -> str:
    """Короткий текст для всплывающего окна (кастом-эмодзи там не работают, поэтому обычная звезда)."""
    return f"Ваш баланс: {balance} ⭐. Для этого подарка нужно {need} ⭐."


def admin_text(w: dict, u: dict) -> str:
    """Заявка админу: юзер, ID, баланс, сколько пригласил, сумма. Эмодзи как у заявки «добавить инструмент»."""
    import bot_features as bf      # там же лежат эмодзи заявок; импорт здесь, чтобы логика проверялась без aiogram
    who = f'<a href="tg://user?id={w["tg_id"]}">{e(u.get("full_name")) or w["tg_id"]}</a>' + (
        f" (@{e(u['username'])})" if u.get("username") else "")
    return (f"{bf.em('confirm')} <b>Новая заявка: вывод звёзд</b>\n\n"
            f"{bf.em('f_name')} Юзер: {who}\n"
            f"{bf.em('f_kind')} ID: <code>{w['tg_id']}</code>\n"
            f"{em('balance')} Баланс: <b>{w['balance_before']}</b> {em('star')}\n"
            f"{em('refs')} Пригласил: <b>{w['invited']}</b>\n"
            f"{em('star')} Сумма: <b>{w['amount']}</b> {em('star')} подарком {w['gift']}")


SENT_TEXT = "Заявка отправлена. Как только её проверят, вам придёт уведомление."
APPROVED_TEXT = "Ваша заявка одобрена, подарок отправлен.\nБлагодарим за сотрудничество!"
REJECTED_TEXT = "К сожалению, ваша заявка отклонена. Звёзды возвращены на ваш баланс."


async def create_withdrawal(pool, uid: int, idx: int) -> tuple[str, dict | None]:
    """Заявка на вывод подарка №idx. Сумма сразу списывается с баланса (чтобы нельзя было подать две заявки на одни звёзды)
    и возвращается, если заявку отклонят. Результат: ('ok'|'low'|'many'|'bad', данные)."""
    if not 0 <= idx < len(GIFTS):
        return "bad", None
    gift, amount = GIFTS[idx]
    async with pool.acquire() as c:
        async with c.transaction():
            if (await c.fetchval("select count(*) from withdrawals where tg_id = $1 and status = 'pending'", uid) or 0) >= MAX_PENDING:
                return "many", None
            left = await c.fetchval(
                "update users set balance = balance - $2 where tg_id = $1 and balance >= $2 returning balance", uid, amount)
            if left is None:
                return "low", {"balance": await c.fetchval("select balance from users where tg_id = $1", uid) or 0, "need": amount}
            invited = await c.fetchval("select count(*) from referrals where referrer_id = $1 and status = 'paid'", uid) or 0
            wid = await c.fetchval(
                "insert into withdrawals(tg_id, gift, amount, balance_before, invited) values($1, $2, $3, $4, $5) returning id",
                uid, gift, amount, left + amount, invited)
    return "ok", {"id": wid, "tg_id": uid, "gift": gift, "amount": amount, "balance_before": left + amount, "invited": invited}


async def resolve_withdrawal(pool, wid: int, status: str) -> dict | None:
    """Закрывает заявку один раз: 'paid' (выплачено), 'rejected' (отклонена) или 'failed' (не удалось отправить админу).
    Для всего, кроме paid, звёзды возвращаются на баланс. None — заявка уже обработана или не найдена."""
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "update withdrawals set status = $2, resolved_at = now() where id = $1 and status = 'pending' "
                "returning tg_id, gift, amount", wid, status)
            if row and status != "paid":
                await c.execute("update users set balance = balance + $2 where tg_id = $1", row["tg_id"], row["amount"])
    return dict(row) if row else None
