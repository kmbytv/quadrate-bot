import io
import logging
from datetime import date

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from bot.config import WHITELIST, DIRECTOR_CHAT_ID
from bot.vision import extract_return_data
from bot.sheets import append_return_row

logger = logging.getLogger(__name__)
router = Router()


class ReturnFlow(StatesGroup):
    waiting_confirmation = State()
    waiting_correction = State()


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Верно", callback_data="confirm"),
        InlineKeyboardButton(text="✏️ Исправить", callback_data="edit"),
    ]])


def _format_data(data: dict, employee: str, today: str) -> str:
    return (
        f"📋 <b>Распознанные данные:</b>\n\n"
        f"📅 Дата: <b>{today}</b>\n"
        f"🕐 Время: <b>{data['time']}</b>\n"
        f"👤 Сотрудник: <b>{employee}</b>\n"
        f"🔢 ID заказа: <b>{data['order_id']}</b>\n"
        f"📦 Товар: <b>{data['product']}</b>\n"
        f"❌ Причина: <b>{data['reason']}</b>"
    )


@router.message(Command("start"))
async def cmd_start(message: Message):
    if WHITELIST and message.from_user.id not in WHITELIST:
        return
    await message.answer(
        "👋 Привет! Отправь скриншот возврата из приложения Озон, и я всё запишу в таблицу."
    )


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext, bot: Bot):
    if WHITELIST and message.from_user.id not in WHITELIST:
        return

    await message.answer("⏳ Обрабатываю скриншот...")

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    image_bytes = buf.getvalue()

    try:
        data = await extract_return_data(image_bytes)
    except Exception as e:
        logger.error("Vision error: %s", e)
        await message.answer("⚠️ Не удалось распознать скриншот. Попробуй ещё раз.")
        return

    today = date.today().strftime("%d.%m.%Y")
    employee = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)

    await state.set_state(ReturnFlow.waiting_confirmation)
    await state.update_data(data=data, employee=employee, today=today)

    await message.answer(
        _format_data(data, employee, today),
        parse_mode="HTML",
        reply_markup=_confirm_keyboard(),
    )


@router.callback_query(F.data == "confirm", ReturnFlow.waiting_confirmation)
async def on_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    state_data = await state.get_data()
    data: dict = state_data["data"]
    employee: str = state_data["employee"]
    today: str = state_data["today"]

    await callback.message.edit_reply_markup(reply_markup=None)

    try:
        await append_return_row(
            date=today,
            time=data["time"],
            employee=employee,
            order_id=data["order_id"],
            product=data["product"],
            reason=data["reason"],
        )
    except Exception as e:
        logger.error("Sheets error: %s", e)
        await callback.message.answer("⚠️ Ошибка записи в таблицу. Обратитесь к администратору.")
        await state.clear()
        return

    await callback.message.answer("✅ Данные записаны в таблицу!")
    await state.clear()

    summary = (
        f"🔔 <b>Новый возврат</b>\n\n"
        f"📅 {today} | 🕐 {data['time']}\n"
        f"👤 {employee}\n"
        f"🔢 Заказ: <b>{data['order_id']}</b>\n"
        f"📦 {data['product']}\n"
        f"❌ {data['reason']}"
    )
    try:
        await bot.send_message(DIRECTOR_CHAT_ID, summary, parse_mode="HTML")
    except Exception as e:
        logger.warning("Failed to notify director: %s", e)


@router.callback_query(F.data == "edit", ReturnFlow.waiting_confirmation)
async def on_edit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(ReturnFlow.waiting_correction)
    await callback.message.answer(
        "✏️ Напиши, что нужно исправить.\n\n"
        "Формат: <code>поле: значение</code>\n"
        "Поля: <code>order_id</code>, <code>product</code>, <code>reason</code>, <code>time</code>\n\n"
        "Пример:\n<code>order_id: 123456789\nreason: Брак товара</code>",
        parse_mode="HTML",
    )


@router.message(ReturnFlow.waiting_correction)
async def on_correction(message: Message, state: FSMContext, bot: Bot):
    if WHITELIST and message.from_user.id not in WHITELIST:
        return

    state_data = await state.get_data()
    data: dict = state_data["data"].copy()
    employee: str = state_data["employee"]
    today: str = state_data["today"]

    field_map = {
        "order_id": "order_id",
        "id": "order_id",
        "product": "product",
        "товар": "product",
        "reason": "reason",
        "причина": "reason",
        "time": "time",
        "время": "time",
    }

    for line in message.text.strip().splitlines():
        if ":" in line:
            key_raw, _, value = line.partition(":")
            key = key_raw.strip().lower()
            value = value.strip()
            if key in field_map and value:
                data[field_map[key]] = value

    await state.update_data(data=data)
    await state.set_state(ReturnFlow.waiting_confirmation)

    await message.answer(
        _format_data(data, employee, today) + "\n\n<i>Данные обновлены. Подтверди или исправь снова.</i>",
        parse_mode="HTML",
        reply_markup=_confirm_keyboard(),
    )
