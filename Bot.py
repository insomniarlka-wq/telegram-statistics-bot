import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================= НАСТРОЙКИ =================

BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TOKEN")
)

DB_PATH = "statistics.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ================= БАЗА ДАННЫХ =================

def connect_db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with connect_db() as db:
        # Новые таблицы имеют суффикс v2, чтобы не конфликтовать
        # со старой базой и не удалять её данные.
        db.execute("""
            CREATE TABLE IF NOT EXISTS users_v2 (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                nickname TEXT DEFAULT '',
                messages INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS message_logs_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS awards_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                giver_id INTEGER NOT NULL,
                award TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_logs_chat_date
            ON message_logs_v2(chat_id, created_at)
        """)


def save_user(chat, user):
    with connect_db() as db:
        db.execute("""
            INSERT INTO users_v2
                (chat_id, user_id, first_name, last_name, username)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                username = excluded.username
        """, (
            chat.id,
            user.id,
            user.first_name or "",
            user.last_name or "",
            user.username or "",
        ))


def add_message(chat_id, user_id):
    now = datetime.now(timezone.utc).isoformat()

    with connect_db() as db:
        db.execute("""
            UPDATE users_v2
            SET messages = messages + 1
            WHERE chat_id = ? AND user_id = ?
        """, (chat_id, user_id))

        db.execute("""
            INSERT INTO message_logs_v2
                (chat_id, user_id, created_at)
            VALUES (?, ?, ?)
        """, (chat_id, user_id, now))


def display_name(row):
    nickname = row[0]
    first_name = row[1]
    last_name = row[2]
    username = row[3]

    if nickname:
        return nickname

    full_name = f"{first_name} {last_name}".strip()
    return full_name or (f"@{username}" if username else "Без имени")


# ================= ПРОВЕРКА АДМИНА =================

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_user:
        return False

    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id,
        )
        return member.status in ("administrator", "creator")
    except Exception:
        logger.exception("Не удалось проверить администратора")
        return False


# ================= ПОДСЧЁТ СООБЩЕНИЙ =================

async def count_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_chat or not update.effective_user:
        return

    chat = update.effective_chat
    user = update.effective_user

    if user.is_bot:
        return

    save_user(chat, user)
    add_message(chat.id, user.id)


# ================= ПРОФИЛЬ =================

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    text = update.effective_message.text.strip()

    target = user

    parts = text.split(maxsplit=1)

    if len(parts) > 1 and parts[1].startswith("@"):
        username = parts[1][1:].strip().lstrip("@")

        with connect_db() as db:
            row = db.execute("""
                SELECT nickname, first_name, last_name, username,
                       user_id, messages
                FROM users_v2
                WHERE chat_id = ? AND LOWER(username) = LOWER(?)
                LIMIT 1
            """, (chat.id, username)).fetchone()

        if not row:
            await update.effective_message.reply_text(
                "Не нашёл этого участника. "
                "Он должен хотя бы раз написать в группе."
            )
            return

        name = display_name(row[:4])
        user_id = row[4]
        messages = row[5]

    else:
        save_user(chat, target)

        with connect_db() as db:
            row = db.execute("""
                SELECT nickname, first_name, last_name, username, messages
                FROM users_v2
                WHERE chat_id = ? AND user_id = ?
            """, (chat.id, target.id)).fetchone()

        name = display_name(row[:4])
        user_id = target.id
        messages = row[4]

    await update.effective_message.reply_text(
        f"👤 Профиль: {name}\n"
        f"🆔 ID: {user_id}\n"
        f"💬 Сообщений: {messages}"
    )


# ================= НИК =================

async def nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    text = update.effective_message.text.strip()

    parts = text.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        await update.effective_message.reply_text(
            "Напиши: +ник Новый ник\n"
            "Или: +ник сброс"
        )
        return

    new_nickname = parts[1].strip()

    save_user(chat, user)

    with connect_db() as db:
        if new_nickname.lower() == "сброс":
            db.execute("""
                UPDATE users_v2
                SET nickname = ''
                WHERE chat_id = ? AND user_id = ?
            """, (chat.id, user.id))

            answer = "Твой ник сброшен."

        else:
            if len(new_nickname) > 40:
                await update.effective_message.reply_text(
                    "Ник должен быть не длиннее 40 символов."
                )
                return

            db.execute("""
                UPDATE users_v2
                SET nickname = ?
                WHERE chat_id = ? AND user_id = ?
            """, (new_nickname, chat.id, user.id))

            answer = f"Твой новый ник: {new_nickname}"

    await update.effective_message.reply_text(answer)


# ================= НАГРАДЫ =================

async def show_awards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    with connect_db() as db:
        rows = db.execute("""
            SELECT award, created_at
            FROM awards_v2
            WHERE chat_id = ? AND user_id = ?
            ORDER BY id DESC
        """, (chat.id, user.id)).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "🏅 У тебя пока нет наград."
        )
        return

    result = ["🏅 Твои награды:"]

    for index, (award, created_at) in enumerate(rows, 1):
        result.append(f"{index}. {award}")

    await update.effective_message.reply_text("\n".join(result))


async def give_award(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.effective_message.reply_text(
            "Выдавать награды могут только администраторы."
        )
        return

    message = update.effective_message
    reply = message.reply_to_message

    if not reply or not reply.from_user:
        await message.reply_text(
            "Нужно ответить на сообщение участника командой:\n"
            "!наградить Текст награды"
        )
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text(
            "Напиши текст награды после !наградить."
        )
        return

    target = reply.from_user
    chat = update.effective_chat
    award = parts[1].strip()

    save_user(chat, target)

    with connect_db() as db:
        db.execute("""
            INSERT INTO awards_v2
                (chat_id, user_id, giver_id, award, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            chat.id,
            target.id,
            update.effective_user.id,
            award,
            datetime.now(timezone.utc).isoformat(),
        ))

    await message.reply_text(
        f"🏅 {target.first_name} получил(а) награду:\n{award}"
    )


# ================= ТОП СООБЩЕНИЙ =================

async def show_top(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    period: str,
    title: str,
):
    if not await is_admin(update, context):
        await update.effective_message.reply_text(
            "Просматривать топ могут только администраторы."
        )
        return

    chat_id = update.effective_chat.id

    now = datetime.now(timezone.utc)

    if period == "day":
        start = now - timedelta(days=1)
    elif period == "week":
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)

    with connect_db() as db:
        rows = db.execute("""
            SELECT
                u.nickname,
                u.first_name,
                u.last_name,
                u.username,
                u.user_id,
                COUNT(m.id) AS total
            FROM message_logs_v2 m
            JOIN users_v2 u
              ON u.chat_id = m.chat_id AND u.user_id = m.user_id
            WHERE m.chat_id = ? AND m.created_at >= ?
            GROUP BY u.user_id
            ORDER BY total DESC
            LIMIT 10
        """, (chat_id, start.isoformat())).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            f"🏆 {title}\nПока сообщений нет."
        )
        return

    result = [f"🏆 {title}"]

    for index, row in enumerate(rows, 1):
        name = display_name(row[:4])
        total = row[5]
        result.append(f"{index}. {name} — {total}")

    await update.effective_message.reply_text("\n".join(result))


# ================= ОБРАБОТКА ТЕКСТОВЫХ КОМАНД =================

async def text_commands(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = update.effective_message.text.strip()
    command = text.lower()

    if command == "профиль" or command.startswith("профиль @"):
        await profile(update, context)

    elif command == "награды":
        await show_awards(update, context)

    elif command.startswith("+ник"):
        await nickname(update, context)

    elif command.startswith("!наградить"):
        await give_award(update, context)

    elif command == "день":
        await show_top(update, context, "day", "Топ за последние 24 часа")

    elif command == "неделя":
        await show_top(update, context, "week", "Топ за 7 дней")

    elif command == "месяц":
        await show_top(update, context, "month", "Топ за 30 дней")


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Привет! Я бот статистики группы.\n\n"
        "Мои команды:\n"
        "профиль\n"
        "профиль @username\n"
        "награды\n"
        "+ник Новый ник\n"
        "+ник сброс\n"
        "!наградить Текст — ответом на сообщение\n"
        "день\n"
        "неделя\n"
        "месяц"
    )


async def error_handler(update, context):
    logger.error(
        "Ошибка при обработке сообщения",
        exc_info=context.error,
    )


# ================= ЗАПУСК =================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден токен. Добавь BOT_TOKEN в переменные FadeHost."
        )

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))

    group_text = filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS

    # Сначала сохраняем сообщения, затем проверяем текстовые команды.
    application.add_handler(
        MessageHandler(group_text, count_message),
        group=0,
    )

    application.add_handler(
        MessageHandler(group_text, text_commands),
        group=1,
    )

    application.add_error_handler(error_handler)

    logger.info("Бот статистики запущен!")

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
                  
