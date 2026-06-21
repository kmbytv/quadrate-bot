"""
bot.py — телеграм-слой: запись голосового, транскрипция, вызов анализа, ответ.

Изменения против старой версии:
• Анализ вынесен в analyzer.py (map-reduce, развязанная сверка) — здесь только UI.
• Если после всех попыток пропуск клиента НЕ устранён (_unresolved) — продавец
  видит ЗАМЕТНОЕ предупреждение, а не тихую запись битых данных.
• Дубль за день → кнопка "Это второй отчёт — добавить" (день дополняется, не
  затирается). Случайное двойное нажатие по-прежнему отсекается.
"""

import os
import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler,
)
from telegram.request import HTTPXRequest
from dotenv import load_dotenv

load_dotenv()

from analyzer import analyze_report      # noqa: E402  (после load_dotenv — нужно для .env)
from sheets import write_all_sheets       # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")
SPREADSHEET_URL = os.getenv(
    "SPREADSHEET_URL",
    "https://docs.google.com/spreadsheets/d/1eD03b0iI-zKlQKbALnGjjHQmFhNdNZ30VL8c24fhSts/edit",
)
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

WAITING_VOICE, CONFIRM_DUPLICATE = 1, 2

MAIN_KB = ReplyKeyboardMarkup([["📝 Начать отчёт"]], resize_keyboard=True)
CANCEL_KB = ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
DUP_KB = ReplyKeyboardMarkup([["✅ Это второй отчёт — добавить", "🚫 Не записывать"]],
                             resize_keyboard=True)


# ─────────────────────────────────────────────────────────────────────────────
#  ТРАНСКРИПЦИЯ
# ─────────────────────────────────────────────────────────────────────────────
async def transcribe_voice(file_id: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Deepgram nova-3. Блокирующий httpx уносим в executor, чтобы не вешать loop."""
    import asyncio
    import httpx

    file = await context.bot.get_file(file_id)
    voice_bytes = await file.download_as_bytearray()

    def _do():
        client = httpx.Client(proxy=PROXY_URL, timeout=120) if PROXY_URL else httpx.Client(timeout=120)
        with client:
            resp = client.post(
                "https://api.deepgram.com/v1/listen"
                "?model=nova-3&language=ru&smart_format=true&punctuate=true",
                headers={
                    "Authorization": f"Token {DEEPGRAM_API_KEY}",
                    "Content-Type": "audio/ogg",
                },
                content=bytes(voice_bytes),
            )
            resp.raise_for_status()
            data = resp.json()
            return data["results"]["channels"][0]["alternatives"][0]["transcript"]

    return await asyncio.get_event_loop().run_in_executor(None, _do)


# ─────────────────────────────────────────────────────────────────────────────
#  ФОРМАТ СВОДКИ
# ─────────────────────────────────────────────────────────────────────────────
def fmt(val):
    return "—" if val is None else str(val)


def build_summary(name: str, data: dict) -> str:
    daily = data.get("daily", {})
    return (
        f"✅ Отчёт разобран!\n"
        f"{'─' * 25}\n"
        f"👤 {name}\n"
        f"📊 Клиентов: {fmt(daily.get('clients'))}\n"
        f"💰 Продаж: {fmt(daily.get('sales'))} на {fmt(daily.get('sales_amount'))} ₽\n"
        f"📋 КП/счета: {fmt(daily.get('kp'))}\n"
        f"🤝 Потенциальные: {fmt(daily.get('potential'))}\n"
        f"🔄 Follow-up: {fmt(daily.get('followup'))}\n"
        f"⚙️ Задачи: {fmt(daily.get('tasks'))}\n"
        f"📌 Итог: {fmt(daily.get('summary'))}\n"
        f"{'─' * 25}\n"
        f"👥 Клиентов в лиде: {len(data.get('clients', []))}\n"
        f"📝 Задач: {len(data.get('tasks', []))}\n"
        f"🔧 Операций: {len(data.get('operations', []))}\n"
        f"{'─' * 25}\n"
        f"📊 Таблица: {SPREADSHEET_URL}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  ХЕНДЛЕРЫ
# ─────────────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        f"Я бот ежедневных отчётов Quadrate.\n"
        f"Нажми кнопку или отправь /report.",
        reply_markup=MAIN_KB,
    )


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎙 Записывай голосовое — расскажи всё, что было за день.\n"
        "Не торопись: называй каждого клиента, суммы и договорённости.",
        reply_markup=CANCEL_KB,
    )
    return WAITING_VOICE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отчёт отменён.", reply_markup=MAIN_KB)
    return ConversationHandler.END


async def _record_and_notify(update, context, name, data, voice_file_id=None):
    """Запись в таблицу + уведомление руководителю. Возвращает следующее состояние."""
    try:
        result = write_all_sheets(name, data)
        if result.get("status") == "duplicate":
            context.user_data["pending_report"] = data
            context.user_data["pending_voice_file_id"] = voice_file_id
            await update.message.reply_text(
                f"⚠️ Отчёт от {name} за сегодня уже есть в таблице.\n"
                f"Если это ВТОРОЙ отчёт за день — добавлю отдельно, первый не трону.",
                reply_markup=DUP_KB,
            )
            return CONFIRM_DUPLICATE
        await update.message.reply_text(
            f"📊 Данные записаны (отчёт №{result.get('report_no', 1)} за сегодня).",
            reply_markup=MAIN_KB,
        )
        await _notify_manager(context, name, data, voice_file_id)
    except Exception as e:
        logger.error("Ошибка записи в Sheets: %s", e)
        await update.message.reply_text(f"⚠️ Ошибка записи: {e}", reply_markup=MAIN_KB)

    return ConversationHandler.END


async def _notify_manager(context, name, data, voice_file_id=None):
    manager_ids = os.getenv("MANAGER_ID", "")
    if not manager_ids:
        return
    summary = build_summary(name, data)
    text = f"📬 Новый отчёт от {name}\n\n{summary}"
    if data.get("_unresolved"):
        text += "\n\n🔴 ВНИМАНИЕ: возможен пропуск клиента — сверьте с продавцом."
    elif data.get("_warnings"):
        text += "\n\n⚠️ Проверить:\n" + "\n".join(f"• {w}" for w in data["_warnings"])
    for mid in manager_ids.split(","):
        chat_id = int(mid.strip())
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error("Не отправлено руководителю %s: %s", mid, e)
        if voice_file_id:
            try:
                await context.bot.send_voice(chat_id=chat_id, voice=voice_file_id)
            except Exception as e:
                logger.error("Не отправлено голосовое руководителю %s: %s", mid, e)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    await update.message.reply_text("🎙 Получил, распознаю...")

    voice_file_id = update.message.voice.file_id  # сохраняем до любых await

    try:
        text = await transcribe_voice(voice_file_id, context)
    except Exception as e:
        logger.error("Ошибка транскрибации: %s", e)
        await update.message.reply_text(f"⚠️ Не удалось распознать голосовое.\nОшибка: {e}",
                                        reply_markup=MAIN_KB)
        return ConversationHandler.END

    logger.info("RAW TRANSCRIPT (%s):\n%s", name, text)
    await update.message.reply_text(f"📝 Распознано:\n\n{text}")

    if not text.strip():
        await update.message.reply_text("⚠️ Пустая расшифровка — запиши голосовое ещё раз.",
                                        reply_markup=MAIN_KB)
        return ConversationHandler.END

    await update.message.reply_text("🤖 Анализирую отчёт...")
    data = analyze_report(text)

    if not data:
        await update.message.reply_text("⚠️ Не удалось разобрать отчёт. Попробуй ещё раз.",
                                        reply_markup=MAIN_KB)
        return ConversationHandler.END

    await update.message.reply_text(build_summary(name, data), reply_markup=MAIN_KB)

    # Заметный сигнал, если пропуск клиента не удалось устранить за все попытки.
    if data.get("_unresolved"):
        await update.message.reply_text(
            "🔴 Возможно, я не разобрал кого-то из клиентов до конца.\n"
            "Проверь список клиентов в таблице — если кого-то нет, добавь вручную "
            "или перезапиши отчёт, проговорив каждого клиента отдельно."
        )
    elif data.get("_warnings"):
        await update.message.reply_text(
            "⚠️ Проверь вручную:\n" + "\n".join(f"• {w}" for w in data["_warnings"])
        )

    return await _record_and_notify(update, context, name, data, voice_file_id)


async def confirm_duplicate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    data = context.user_data.get("pending_report")
    voice_file_id = context.user_data.get("pending_voice_file_id")

    if update.message.text.startswith("✅") and data:
        try:
            result = write_all_sheets(name, data, force=True)
            await update.message.reply_text(
                f"📊 Добавлено как отчёт №{result.get('report_no', 2)} за сегодня.",
                reply_markup=MAIN_KB,
            )
            await _notify_manager(context, name, data, voice_file_id)
        except Exception as e:
            logger.error("Ошибка записи (force): %s", e)
            await update.message.reply_text(f"⚠️ Ошибка записи: {e}", reply_markup=MAIN_KB)
    else:
        await update.message.reply_text("🚫 Повторная запись отменена.", reply_markup=MAIN_KB)

    context.user_data.pop("pending_report", None)
    context.user_data.pop("pending_voice_file_id", None)
    return ConversationHandler.END


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📝 Начать отчёт":
        return await report(update, context)
    if text == "❌ Отмена":
        return await cancel(update, context)
    await update.message.reply_text("🎙 Нажми «📝 Начать отчёт» или /report.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик — ловит всё, что не поймали хендлеры,
    чтобы бот не падал молча и продавец видел понятное сообщение."""
    logger.error("Необработанная ошибка: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Что-то пошло не так. Попробуй ещё раз или начни заново /report.",
                reply_markup=MAIN_KB,
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────────────────────────────────────

def _make_req(read_timeout=60):
    kw = dict(http_version="1.1", connect_timeout=30, read_timeout=read_timeout, write_timeout=30)
    if PROXY_URL:
        kw["proxy"] = PROXY_URL
    return HTTPXRequest(**kw)


def main():
    builder = (
        Application.builder()
        .token(TOKEN)
        .request(_make_req())
        .get_updates_request(_make_req())
    )
    app = builder.build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("report", report),
            MessageHandler(filters.Regex("^📝 Начать отчёт$"), report),
        ],
        states={
            WAITING_VOICE: [
                MessageHandler(filters.VOICE, handle_voice),
                CommandHandler("cancel", cancel),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
            ],
            CONFIRM_DUPLICATE: [
                MessageHandler(
                    filters.Regex("^(✅ Это второй отчёт — добавить|🚫 Не записывать)$"),
                    confirm_duplicate,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен. Модель: %s", os.getenv("LLM_MODEL", "anthropic/claude-opus-4-5"))
    app.run_polling()


if __name__ == "__main__":
    main()
