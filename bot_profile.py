"""Кнопки бота «Профиль» и «Реферальная система». Логика и тексты — в referrals.py.

Подключается в main.py: dp.include_router(bp.router) и bp.configure(...).
"""
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton

import bot_features as bf
import referrals as refs

log = logging.getLogger("harledy.profile")
router = Router()
D = {}   # зависимости из main.py: pool() -> asyncpg.Pool, has_access(bot, uid), tools_json() -> bytes|None, bot_username() -> str,
         # admin_id (куда слать заявки на вывод), admins() -> set[int] (кто может их одобрять)


def configure(**kw):
    D.update(kw)


def sender(bot):
    """send(chat_id, text) для уведомлений о рефералах: с кастом-эмодзи, при отказе Telegram — обычными смайликами."""
    async def send(chat_id: int, text: str):
        try:
            await bot.send_message(chat_id, text)
        except TelegramBadRequest:
            try:
                await bot.send_message(chat_id, bf.plain(text))
            except TelegramAPIError as ex:
                log.info("referral notice to %s not sent: %s", chat_id, ex)
        except TelegramAPIError as ex:           # человек заблокировал бота и т.п.
            log.info("referral notice to %s not sent: %s", chat_id, ex)
    return send


def ref_link(uid: int) -> str | None:
    name = D["bot_username"]()
    return f"https://t.me/{name}?start=ref_{uid}" if name else None


async def _allowed(cb: CallbackQuery) -> bool:
    """Как и «Добавить инструмент»: только после соглашения и подписки."""
    if await D["has_access"](cb.bot, cb.from_user.id):
        return True
    await cb.answer("Сначала подпишитесь на канал и нажмите /start", show_alert=True)
    return False


def menu_only(rich: bool):
    return bf.kb([bf.cancel_row(rich)])


@router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    if not await _allowed(cb):
        return
    pool = D["pool"]()
    data = await refs.profile_data(pool, cb.from_user.id)
    if not data:
        return await cb.answer("Нажмите /start, чтобы начать", show_alert=True)
    recent = await refs.recent_tools(pool, cb.from_user.id, D["tools_json"]())
    await cb.answer()
    await bf.show(cb.message, refs.profile_text(cb.from_user.full_name, data, recent), menu_only, edit=True)


def ref_kb(rich: bool):
    return bf.kb([[bf.btn("Создать заявку на вывод", "stars", "💸", rich, callback_data="ref_wd")],
                  bf.cancel_row(rich)])


@router.callback_query(F.data == "ref")
async def referral(cb: CallbackQuery):
    if not await _allowed(cb):
        return
    balance, count = await refs.balance_and_count(D["pool"](), cb.from_user.id)
    await cb.answer()
    await bf.show(cb.message, refs.ref_text(ref_link(cb.from_user.id), balance, count), ref_kb, edit=True)


# ---------------------------------------------------------------- вывод звёзд подарком
def gifts_kb(rich: bool):
    """Подарки по два в ряду, как в меню Telegram; вместо обычной звезды — наша (иконка кнопки)."""
    star = refs.EM["star"][0]
    btns = [InlineKeyboardButton(text=f"{a} {g}", icon_custom_emoji_id=star, callback_data=f"wd:{i}") if rich
            else InlineKeyboardButton(text=f"{g} {a} ⭐", callback_data=f"wd:{i}")
            for i, (g, a) in enumerate(refs.GIFTS)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    return bf.kb(rows + bf.nav(rich, "ref"))


@router.callback_query(F.data == "ref_wd")
async def withdraw(cb: CallbackQuery):
    if not await _allowed(cb):
        return
    balance, _ = await refs.balance_and_count(D["pool"](), cb.from_user.id)
    if balance < refs.MIN_WITHDRAW:
        return await cb.answer(f"Ваш баланс: {balance} ⭐. Вывод доступен от {refs.MIN_WITHDRAW} ⭐.", show_alert=True)
    await cb.answer()
    await bf.show(cb.message, refs.picker_text(balance), gifts_kb, edit=True)


@router.callback_query(F.data.regexp(r"^wd:\d{1,2}$"))
async def pick_gift(cb: CallbackQuery):
    """Нажали на подарок: хватает звёзд — просим подтвердить, нет — отказ с текущим балансом."""
    if not await _allowed(cb):
        return
    idx = int(cb.data[3:])
    if idx >= len(refs.GIFTS):
        return await cb.answer()
    balance, _ = await refs.balance_and_count(D["pool"](), cb.from_user.id)
    need = refs.GIFTS[idx][1]
    if balance < need:
        return await cb.answer(refs.low_text(balance, need), show_alert=True)
    await cb.answer()
    await bf.show(cb.message, refs.confirm_text(idx, balance), lambda r: bf.kb(
        [[bf.btn("Да, вывести", "confirm", "✅", r, callback_data=f"wdy:{idx}")], *bf.nav(r, "ref_wd")]), edit=True)


def admin_kb(wid: int, rich: bool = True):
    return bf.kb([[bf.btn("Выплачено", "confirm", "✅", rich, callback_data=f"wda:{wid}")],
                  [bf.btn("Отклонить", None, "❌", rich, callback_data=f"wdr:{wid}")]])


async def _to_admin(bot, text: str, wid: int) -> bool:
    for rich in (True, False):
        try:
            await bot.send_message(D["admin_id"], text if rich else bf.plain(text), reply_markup=admin_kb(wid, rich))
            return True
        except TelegramBadRequest as ex:
            if not rich:
                log.warning("withdrawal request %s not sent to admin: %s", wid, ex)
        except TelegramAPIError as ex:
            log.warning("withdrawal request %s not sent to admin: %s", wid, ex)
            return False
    return False


@router.callback_query(F.data.regexp(r"^wdy:\d{1,2}$"))
async def confirm_withdraw(cb: CallbackQuery):
    """«Да, вывести»: списываем сумму, создаём заявку и отправляем её админу."""
    if not await _allowed(cb):
        return
    pool = D["pool"]()
    res, w = await refs.create_withdrawal(pool, cb.from_user.id, int(cb.data[4:]))
    if res == "many":
        return await cb.answer(f"У вас уже {refs.MAX_PENDING} заявки на проверке. Дождитесь ответа.", show_alert=True)
    if res == "low":
        return await cb.answer(refs.low_text(w["balance"], w["need"]), show_alert=True)
    if res != "ok":
        return await cb.answer()
    await cb.answer()
    u = {"full_name": cb.from_user.full_name, "username": cb.from_user.username}
    if not await _to_admin(cb.bot, refs.admin_text(w, u), w["id"]):
        await refs.resolve_withdrawal(pool, w["id"], "failed")           # админ не получил заявку: возвращаем звёзды
        return await bf.show(cb.message, f"{bf.em('error')} Не удалось отправить заявку, звёзды остались на балансе. Попробуйте позже.",
                             menu_only, edit=True)
    await bf.show(cb.message, f"{bf.em('sent')} {refs.SENT_TEXT}", menu_only, edit=True)


async def _close_request(cb: CallbackQuery, status: str, mark: str):
    """Кнопки админа под заявкой: один раз закрываем заявку, правим сообщение и пишем человеку."""
    if cb.from_user.id not in D["admins"]():
        return await cb.answer("Нет доступа", show_alert=True)
    w = await refs.resolve_withdrawal(D["pool"](), int(cb.data[4:]), status)
    if not w:
        await cb.answer("Заявка уже обработана", show_alert=True)
        return await _drop_buttons(cb)
    await cb.answer("Готово")
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n{mark}", reply_markup=None)
    except TelegramAPIError:
        await _drop_buttons(cb)
    text = refs.APPROVED_TEXT if status == "paid" else refs.REJECTED_TEXT
    icon = bf.em("sent") if status == "paid" else bf.em("error")
    await sender(cb.bot)(w["tg_id"], f"{icon} {text}")


async def _drop_buttons(cb: CallbackQuery):
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass


@router.callback_query(F.data.regexp(r"^wda:\d{1,12}$"))
async def approve(cb: CallbackQuery):
    await _close_request(cb, "paid", "✅ <b>Выплачено</b>")


@router.callback_query(F.data.regexp(r"^wdr:\d{1,12}$"))
async def reject(cb: CallbackQuery):
    await _close_request(cb, "rejected", "❌ <b>Отклонено, звёзды возвращены</b>")
