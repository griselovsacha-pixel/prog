"""Доп. функции бота: «Добавить свой инструмент» (диалог с подтверждением → админу) и «Поддержать проект».

Модуль не импортирует main.py (чтобы не было циклов): всё нужное передаётся через configure().
Telegram-эмодзи в тексте: впишите ID в словарь EM ниже (None = пока обычный смайлик-заглушка).
"""
import asyncio
import html
import json
import logging
import re
import time
from collections import OrderedDict

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import security as sec

log = logging.getLogger("harledy.bot")
router = Router()

# ---------------------------------------------------------------- настройки
PER_PAGE = 5                                   # категорий на странице
AMOUNTS = (1, 2, 3, 5, 10, 50, 100)            # суммы поддержки, $
HIDE_CATEGORY = re.compile(r"harledy|харледи", re.I)   # эти категории нельзя выбрать
AUTHOR_PROFILE = "https://t.me/SexHarledy"

# ---- ID эмодзи кнопок (даны вами)
BTN = {
    "add": "5931614414351372818",       # Добавить свой инструмент
    "support": "6032937473162614352",   # Поддержать проект
    "next": "5449891990533598330",      # вперёд (только смайл)
    "back": "5451879400750474312",      # назад
    "bot": "5819078828017849357",       # Бот
    "site": "5778168620278354602",      # Сайт
    "confirm": "5825794181183836432",   # Подтвердить
    "boost": "6028530359975548369",     # Дать буст каналу
    "stars": "5958413064658228696",     # Отправить telegram stars
    "money": "5985630530111020079",     # Отправить деньги
    "tip": None,                        # ← «Отправить копеечку автору»: ID не указан
    "cancel": None,                     # ← «Отмена»: ID не указан
    "pay": None,                        # ← «Оплатить»: ID не указан
    "check": None,                      # ← «Проверить оплату»: ID не указан
}

# ---- эмодзи в тексте сообщений: ключ → (ID или None, запасной смайлик). Впишите ID вместо None.
EM = {
    "ask_cat":   (None, "🗂"),   # начало «Пожалуйста, выберите что делает ваш инструмент»
    "ask_kind":  (None, "🧩"),   # начало «Выберите тип инструмента»
    "ask_name":  (None, "✏️"),   # начало «Введите название инструмента»
    "ask_desc":  (None, "📝"),   # начало «Введите описание»
    "ask_link":  (None, "🔗"),   # начало «Отправьте ссылку / юзернейм»
    "confirm":   (None, "📋"),   # заголовок окна подтверждения
    "f_cat":     (None, "🗂"),   # «Категория:» в окне подтверждения и у админа
    "f_kind":    (None, "🧩"),   # «Тип:»
    "f_name":    (None, "✏️"),   # «Название:»
    "f_desc":    (None, "📝"),   # «Описание:»
    "f_link":    (None, "🔗"),   # «Ссылка:»
    "sent":      (None, "✅"),   # «Заявка отправлена»
    "cancelled": (None, "❌"),   # «Отменено»
    "error":     (None, "⚠️"),   # ошибки и подсказки
    "support":   ("5985472565508838112", "💖"),   # «Методы которыми вы можете поддержать наш проект»
    "tip":       (None, "🪙"),   # «Выберите способ»
    "amount":    (None, "💵"),   # «Выберите сумму»
    "invoice":   (None, "🧾"),   # сообщение со счётом
    "paid":      (None, "🎉"),   # «Оплата получена»
}

D = {}   # зависимости из main.py


def configure(**kw):
    """has_access(bot, uid), tools_json() -> bytes|None, admin_id, channel_id, crypto_token, crypto_host"""
    D.update(kw)


def em(key: str) -> str:
    eid, fb = EM[key]
    return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>' if eid else fb


def plain(text: str) -> str:
    return re.sub(r"</?tg-emoji[^>]*>", "", text)


# ---------------------------------------------------------------- клавиатуры (rich=True — с кастом-эмодзи на кнопках)
def btn(text: str, key: str | None, fb: str, rich: bool, **kw) -> InlineKeyboardButton:
    eid = BTN.get(key) if key else None
    if rich and eid:
        return InlineKeyboardButton(text=text, icon_custom_emoji_id=eid, **kw)
    return InlineKeyboardButton(text=f"{fb} {text}".strip() if fb else text, **kw)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_row(rich: bool):
    return [btn("Отмена", "cancel", "✖️", rich, callback_data="atcancel")]


async def show(msg: Message, text: str, kb_fn, edit: bool = False):
    """Отправляет (или редактирует) сообщение с кастом-эмодзи; если Telegram отклонил — обычные смайлики."""
    for rich in (True, False):
        t, k = (text if rich else plain(text)), kb_fn(rich)
        try:
            return await (msg.edit_text(t, reply_markup=k) if edit else msg.answer(t, reply_markup=k))
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return None
            if not rich:
                raise
            log.warning("rich message rejected (%s), fallback to plain", e)


# ---------------------------------------------------------------- категории из tools.json
def categories() -> list[str]:
    try:
        data = json.loads((D["tools_json"]() or b"{}"))
    except Exception:
        return []
    out = []
    for c in data.get("children") or []:
        name = str(c.get("name", "")).strip()
        if name and not HIDE_CATEGORY.search(name):
            out.append(name)
    return out


def cat_kb(cats: list[str], page: int, rich: bool):
    start = page * PER_PAGE
    rows = [[InlineKeyboardButton(text=n[:60], callback_data=f"atk:{i}")] for i, n in enumerate(cats[start:start + PER_PAGE], start)]
    nav = []
    if page > 0:
        nav.append(btn("Назад", "back", "◀️", rich, callback_data=f"atc:{page - 1}"))
    if start + PER_PAGE < len(cats):                       # «вперёд» — только смайл
        eid = BTN["next"]
        nav.append(InlineKeyboardButton(text="​", icon_custom_emoji_id=eid, callback_data=f"atc:{page + 1}") if rich
                   else InlineKeyboardButton(text="▶️", callback_data=f"atc:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(cancel_row(rich))
    return kb(rows)


def cat_text(cats: list[str], page: int) -> str:
    pages = max(1, -(-len(cats) // PER_PAGE))
    tail = f"\n\n<i>Страница {page + 1} из {pages}</i>" if pages > 1 else ""
    return f"{em('ask_cat')} Пожалуйста, выберите что делает ваш инструмент:{tail}"


# ---------------------------------------------------------------- проверка введённых данных
USERNAME_RE = re.compile(r"@?([A-Za-z][A-Za-z0-9_]{3,31})")
DOMAIN_RE = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?(/\S*)?")


def norm_link(kind: str, raw: str) -> str | None:
    """Приводит ссылку/юзернейм к https-адресу; None — не похоже на правду."""
    s = raw.strip()
    if not s or len(s) > 300 or re.search(r"\s", s):
        return None
    if kind == "bot":
        m = USERNAME_RE.fullmatch(s)
        if m:
            return "https://t.me/" + m.group(1)
        s = re.sub(r"^(?:tg://resolve\?domain=)", "https://t.me/", s, flags=re.I)
    if re.match(r"(?i)^https?://", s):
        return s if re.match(r"(?i)^https?://[^/\s]+\.[^/\s]+", s) else None
    if re.match(r"(?i)^(t\.me|telegram\.me)/", s) or DOMAIN_RE.fullmatch(s):
        return "https://" + s
    return None


# ---------------------------------------------------------------- «Добавить свой инструмент»
class AddTool(StatesGroup):
    category = State()
    kind = State()
    name = State()
    desc = State()
    link = State()
    confirm = State()


KIND_TITLE = {"bot": "Бот", "site": "Сайт"}


def e(s) -> str:
    return html.escape(str(s or ""), quote=False)


@router.callback_query(F.data == "addtool")
async def add_start(cb: CallbackQuery, state: FSMContext):
    if not await D["has_access"](cb.bot, cb.from_user.id):
        return await cb.answer("Сначала подпишитесь на канал и нажмите /start", show_alert=True)
    cats = categories()
    if not cats:
        return await cb.answer("Список категорий пока недоступен, попробуйте позже.", show_alert=True)
    await cb.answer()
    await state.clear()
    await state.set_state(AddTool.category)
    await state.update_data(cats=cats)
    await show(cb.message, cat_text(cats, 0), lambda r: cat_kb(cats, 0, r))


@router.callback_query(StateFilter(AddTool.category), F.data.regexp(r"^atc:\d{1,3}$"))
async def add_page(cb: CallbackQuery, state: FSMContext):
    cats = (await state.get_data()).get("cats") or []
    page = max(0, min(int(cb.data[4:]), max(0, (len(cats) - 1) // PER_PAGE)))
    await cb.answer()
    await show(cb.message, cat_text(cats, page), lambda r: cat_kb(cats, page, r), edit=True)


@router.callback_query(StateFilter(AddTool.category), F.data.regexp(r"^atk:\d{1,3}$"))
async def add_category(cb: CallbackQuery, state: FSMContext):
    cats = (await state.get_data()).get("cats") or []
    i = int(cb.data[4:])
    if i >= len(cats):
        return await cb.answer("Категория не найдена", show_alert=True)
    await cb.answer()
    await state.update_data(category=cats[i])
    await state.set_state(AddTool.kind)
    await show(cb.message, f"{em('ask_kind')} Выберите, чем является ваш инструмент:",
               lambda r: kb([[btn("Бот", "bot", "🤖", r, callback_data="att:bot"),
                              btn("Сайт", "site", "🌐", r, callback_data="att:site")], cancel_row(r)]), edit=True)


@router.callback_query(StateFilter(AddTool.kind), F.data.in_({"att:bot", "att:site"}))
async def add_kind(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(kind=cb.data[4:])
    await state.set_state(AddTool.name)
    await show(cb.message, f"{em('ask_name')} Введите название инструмента:", lambda r: kb([cancel_row(r)]), edit=True)


@router.message(StateFilter(AddTool.name), F.text)
async def add_name(m: Message, state: FSMContext):
    name = " ".join(m.text.split())
    if not 2 <= len(name) <= 100:
        return await show(m, f"{em('error')} Название должно быть от 2 до 100 символов. Попробуйте ещё раз:", lambda r: kb([cancel_row(r)]))
    await state.update_data(name=name)
    await state.set_state(AddTool.desc)
    await show(m, f"{em('ask_desc')} Введите описание инструмента:", lambda r: kb([cancel_row(r)]))


@router.message(StateFilter(AddTool.desc), F.text)
async def add_desc(m: Message, state: FSMContext):
    desc = m.text.strip()
    if not 5 <= len(desc) <= 1000:
        return await show(m, f"{em('error')} Описание должно быть от 5 до 1000 символов. Попробуйте ещё раз:", lambda r: kb([cancel_row(r)]))
    data = await state.update_data(desc=desc)
    await state.set_state(AddTool.link)
    ask = "ссылку или юзернейм бота" if data["kind"] == "bot" else "ссылку на сайт"
    await show(m, f"{em('ask_link')} Отправьте {ask}:", lambda r: kb([cancel_row(r)]))


def summary(d: dict, link: str) -> str:
    return (f"{em('f_cat')} Категория: <b>{e(d['category'])}</b>\n"
            f"{em('f_kind')} Тип: <b>{KIND_TITLE[d['kind']]}</b>\n"
            f"{em('f_name')} Название: <b>{e(d['name'])}</b>\n"
            f"{em('f_desc')} Описание: {e(d['desc'])}\n"
            f"{em('f_link')} Ссылка: {e(link)}")


@router.message(StateFilter(AddTool.link), F.text)
async def add_link(m: Message, state: FSMContext):
    d = await state.get_data()
    link = norm_link(d["kind"], m.text)
    if not link:
        hint = "@username или ссылку t.me/…" if d["kind"] == "bot" else "ссылку вида https://example.com"
        return await show(m, f"{em('error')} Не похоже на ссылку. Отправьте {hint}:", lambda r: kb([cancel_row(r)]))
    await state.update_data(link=link)
    await state.set_state(AddTool.confirm)
    await show(m, f"{em('confirm')} <b>Проверьте данные вашего инструмента:</b>\n\n{summary(d, link)}",
               lambda r: kb([[btn("Подтвердить", "confirm", "✅", r, callback_data="atok")], cancel_row(r)]))


@router.message(StateFilter(AddTool.name, AddTool.desc, AddTool.link))
async def add_not_text(m: Message):
    await show(m, f"{em('error')} Пожалуйста, отправьте текстовое сообщение.", lambda r: kb([cancel_row(r)]))


@router.callback_query(StateFilter(AddTool.confirm), F.data == "atok")
async def add_confirm(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.clear()                                   # повторное нажатие уже ничего не отправит
    if not d.get("link"):
        return await cb.answer("Заявка устарела, начните заново", show_alert=True)
    u = cb.from_user
    who = f'<a href="tg://user?id={u.id}">{e(u.full_name)}</a>' + (f" (@{e(u.username)})" if u.username else "")
    text = (f"{em('confirm')} <b>Новая заявка: добавить инструмент</b>\n\n{summary(d, d['link'])}\n\n"
            f"От: {who}\nID: <code>{u.id}</code>")
    try:
        try:
            await cb.bot.send_message(D["admin_id"], text)
        except TelegramBadRequest:
            await cb.bot.send_message(D["admin_id"], plain(text))
    except TelegramAPIError as ex:
        log.warning("не удалось отправить заявку админу: %s", ex)
        await cb.answer("Не удалось отправить заявку, попробуйте позже.", show_alert=True)
        return
    log.info("tool submission from user=%s: %r", u.id, d.get("name"))
    await cb.answer()
    await show(cb.message, f"{em('sent')} Спасибо! Ваша заявка отправлена на рассмотрение.", lambda r: kb([]), edit=True)


@router.callback_query(F.data == "atcancel")
async def add_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, f"{em('cancelled')} Отменено.", lambda r: kb([]), edit=True)


@router.callback_query(F.data.regexp(r"^at(c:|k:|t:|ok$)"))
async def add_stale(cb: CallbackQuery):
    await cb.answer("Диалог устарел. Нажмите «Добавить свой инструмент» заново.", show_alert=True)


# ---------------------------------------------------------------- «Поддержать проект»
def boost_url() -> str:
    cid = str(D.get("channel_id") or "")
    return f"https://t.me/boost?c={cid[4:]}" if cid.startswith("-100") else D.get("channel_link", "https://t.me/")


@router.callback_query(F.data == "support")
async def support_menu(cb: CallbackQuery):
    await cb.answer()
    await show(cb.message, f"<b>Методы которыми вы можете поддержать наш проект</b> {em('support')}",
               lambda r: kb([[btn("Отправить копеечку автору", "tip", "🪙", r, callback_data="sp_tip")],
                             [btn("Дать буст каналу", "boost", "🚀", r, url=boost_url())]]))


@router.callback_query(F.data == "sp_tip")
async def support_tip(cb: CallbackQuery):
    await cb.answer()
    await show(cb.message, f"{em('tip')} Выберите способ:",
               lambda r: kb([[btn("Отправить telegram stars", "stars", "⭐", r, url=AUTHOR_PROFILE)],
                             [btn("Отправить деньги", "money", "💵", r, callback_data="sp_money")]]))


@router.callback_query(F.data == "sp_money")
async def support_money(cb: CallbackQuery):
    await cb.answer()
    rows = [[InlineKeyboardButton(text=f"{a}$", callback_data=f"sp_amt:{a}") for a in AMOUNTS[:4]],
            [InlineKeyboardButton(text=f"{a}$", callback_data=f"sp_amt:{a}") for a in AMOUNTS[4:]]]
    await show(cb.message, f"{em('amount')} Выберите сумму поддержки:", lambda r: kb(rows))


# ---- Crypto Pay API (https://help.crypt.bot/crypto-pay-api): счёт на оплату создаём createInvoice
INVOICE_LIMIT = sec.Limiter(5, 600)       # не больше 5 счетов за 10 минут на человека
_paid_seen: OrderedDict = OrderedDict()   # уже обработанные оплаченные счета (чтобы не уведомлять админа дважды)


async def crypto_call(method: str, params: dict) -> dict:
    token, host = D.get("crypto_token"), (D.get("crypto_host") or "https://pay.crypt.bot").rstrip("/")
    if not token:
        raise RuntimeError("CRYPTO_PAY_TOKEN не задан")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post(f"{host}/api/{method}", json=params, headers={"Crypto-Pay-API-Token": token}) as r:
            data = await r.json(content_type=None)
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"Crypto Pay {method}: {data!r}"[:300])
    return data["result"]


@router.callback_query(F.data.regexp(r"^sp_amt:\d{1,3}$"))
async def support_amount(cb: CallbackQuery):
    amount = int(cb.data[7:])
    if amount not in AMOUNTS:
        return await cb.answer()
    if not D.get("crypto_token"):
        return await cb.answer("Оплата временно недоступна.", show_alert=True)
    if not INVOICE_LIMIT.hit(str(cb.from_user.id)):
        return await cb.answer("Слишком много счетов. Попробуйте через несколько минут.", show_alert=True)
    await cb.answer()
    try:
        inv = await crypto_call("createInvoice", {
            "currency_type": "fiat", "fiat": "USD", "amount": str(amount),
            "description": "Поддержка проекта Harledy", "payload": f"u{cb.from_user.id}",
            "expires_in": 3600, "allow_comments": False, "allow_anonymous": False})
    except Exception as ex:
        log.warning("createInvoice failed: %s", ex)
        return await show(cb.message, f"{em('error')} Не удалось создать счёт. Попробуйте позже.", lambda r: kb([]))
    url = inv.get("bot_invoice_url") or inv.get("pay_url")
    iid = inv["invoice_id"]
    await show(cb.message, f"{em('invoice')} Счёт на <b>{amount}$</b> создан. Оплатить можно любой криптовалютой через @CryptoBot.\n"
                           "Счёт действует 1 час. После оплаты нажмите «Проверить оплату».",
               lambda r: kb([[btn("Оплатить", "pay", "💳", r, url=url)],
                             [btn("Проверить оплату", "check", "🔄", r, callback_data=f"sp_chk:{iid}")]]))


@router.callback_query(F.data.regexp(r"^sp_chk:\d{1,15}$"))
async def support_check(cb: CallbackQuery):
    iid = int(cb.data[7:])
    try:
        res = await crypto_call("getInvoices", {"invoice_ids": str(iid)})
        items = res.get("items") if isinstance(res, dict) else res
        inv = items[0] if items else None
    except Exception as ex:
        log.warning("getInvoices failed: %s", ex)
        return await cb.answer("Не удалось проверить оплату, попробуйте через минуту.", show_alert=True)
    if not inv or inv.get("payload") != f"u{cb.from_user.id}":     # чужие счета не показываем
        return await cb.answer("Счёт не найден.", show_alert=True)
    status = inv.get("status")
    if status == "paid":
        await cb.answer()
        if iid not in _paid_seen:
            _paid_seen[iid] = 1
            while len(_paid_seen) > 1000:
                _paid_seen.popitem(last=False)
            u = cb.from_user
            try:
                await cb.bot.send_message(D["admin_id"], plain(f"Получена оплата: {inv.get('amount')} {inv.get('asset') or inv.get('fiat')} "
                                                               f"(счёт на {inv.get('fiat') or ''})\nОт: {e(u.full_name)} ID {u.id}"))
            except TelegramAPIError:
                pass
        await show(cb.message, f"{em('paid')} Оплата получена. Спасибо за поддержку проекта!", lambda r: kb([]), edit=True)
    elif status == "expired":
        await cb.answer("Счёт истёк. Создайте новый.", show_alert=True)
    else:
        await cb.answer("Оплата пока не найдена. Оплатите счёт и нажмите ещё раз.", show_alert=True)
