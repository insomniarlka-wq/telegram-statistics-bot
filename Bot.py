
import os
import sqlite3
import logging
from html import escape

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Настройки
BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TOKEN")
)

DB_NAME = "statistics.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# Создание базы данных
def init_db():
    with sqlite3.connect(DB_NAME) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER,
                user_id INTEGER,
                name TEXT,
                username TEXT,
                messages INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)


# Подсчёт сообщений
async def count_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    with sqlite3.connect(DB_NAME) as db:
        db.execute("""
            INSERT INTO users (
                chat_id, user_id, name, username, messages
            )
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET
                name = excluded.name,
                username = excluded.username,
                messages = messages + 1
        """, (
            chat.id,
            user.id,
            user.full_name,
            user.username or "",
        ))


# Старт
async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.effective_message.reply_text(
        "Привет! 👋\n\n"
        "Я бот статистики Telegram-групп.\n\n"
        "Мои команды:\n"
        "/stats — статистика группы\n"
        "/mystats — твоя статистика\n"
        "/top — рейтинг участников\n\n"
        "Добавь меня в группу, чтобы я начал "
        "считать сообщения."
    )


# Общая статистика
async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "Команда работает внутри группы."
        )
        return

    with sqlite3.connect(DB_NAME) as db:
        total = db.execute(
            "SELECT COALESCE(SUM(messages), 0) "
            "FROM users WHERE chat_id = ?",
            (chat.id,)
        ).fetchone()[0]

        users = db.execute(
            "SELECT COUNT(*) FROM users WHERE chat_id = ?",
            (chat.id,)
        ).fetchone()[0]

    await update.effective_message.reply_text(
        f"📊 Статистика группы\n\n"
        f"👥 Участников с сообщениями: {users}\n"
        f"💬 Всего сообщений: {total}"
    )


# Личная статистика
async def mystats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "Команда работает внутри группы."
        )
        return

    with sqlite3.connect(DB_NAME) as db:
        result = db.execute(
            "SELECT messages FROM users "
            "WHERE chat_id = ? AND user_id = ?",
            (chat.id, user.id)
        ).fetchone()

    count = result[0] if result else 0

    await update.effective_message.reply_text(
        f"👤 Твоя статистика\n\n"
        f"Имя: {user.full_name}\n"
        f"💬 Сообщений: {count}"
    )


# Топ участников
async def top(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "Команда работает внутри группы."
        )
        return

    with sqlite3.connect(DB_NAME) as db:
        rows = db.execute(
            "SELECT name, username, messages "
            "FROM users WHERE chat_id = ? "
            "ORDER BY messages DESC LIMIT 10",
            (chat.id,)
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "Пока статистики нет."
        )
        return

    result = "🏆 ТОП-10 участников\n\n"

    medals = ["🥇", "🥈", "🥉"]

    for index, (name, username, messages) in enumerate(rows):
        medal = medals[index] if index < 3 else f"{index + 1}."
        safe_name = escape(name)
        result += f"{medal} {safe_name} — {messages} 💬\n"

    await update.effective_message.reply_text(result)


# Запуск
def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден токен! Добавь BOT_TOKEN "
            "в переменные окружения FadeHost."
        )

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("mystats", mystats))
    app.add_handler(CommandHandler("top", top))

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            count_message
        )
    )

    logger.info("Бот статистики запущен!")

    app.run_polling()


if __name__ == "__main__":
    main()
  
