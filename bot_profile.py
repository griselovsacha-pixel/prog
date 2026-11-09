"""Кнопки бота «Профиль» и «Реферальная система». Логика и тексты — в referrals.py.

Подключается в main.py: dp.include_router(bp.router) и bp.configure(...).
"""
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery

import bot_features as bf
import referrals as refs

log = logging.getLogger("harledy.profile")
router = Router()
D = {}   # зависимости из main.py: pool() -> asyncpg.Pool, has_access(bot, uid), tools_json() -> bytes|None, bot_username() -> str


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
    return bf.kb([[bf.btn("Создать заявку на вывод", None, "💸", rich, callback_data="ref_wd")],
                  bf.cancel_row(rich)])


@router.callback_query(F.data == "ref")
async def referral(cb: CallbackQuery):
    if not await _allowed(cb):
        return
    balance, count = await refs.balance_and_count(D["pool"](), cb.from_user.id)
    await cb.answer()
    await bf.show(cb.message, refs.ref_text(ref_link(cb.from_user.id), balance, count), ref_kb, edit=True)


@router.callback_query(F.data == "ref_wd")
async def withdraw(cb: CallbackQuery):
    if not await _allowed(cb):
        return
    balance, _ = await refs.balance_and_count(D["pool"](), cb.from_user.id)
    if balance < refs.MIN_WITHDRAW:
        return await cb.answer(f"Ваш баланс: {balance} ⭐. Вывод доступен от {refs.MIN_WITHDRAW} ⭐.", show_alert=True)
    await cb.answer()
    # TODO: оформление заявки на вывод (баланс уже ≥ MIN_WITHDRAW). Пока намеренно пусто.
