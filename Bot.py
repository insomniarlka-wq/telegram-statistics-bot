
import os
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TOKEN")
)

DB = "statistics.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def connect():
    return sqlite3.connect(DB)


def init_db():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER,
                user_id INTEGER,
                username TEXT,
                first_name TEXT,
                nickname TEXT DEFAULT '',
                messages INTEGER DEFAULT 0,
                PRIMARY KEY(chat_id, user_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                created_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS awards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                award TEXT,
                given_by INTEGER,
                created_at TEXT
            )
        """)


def save_user(chat, user):
    with connect() as db:
        db.execute("""
            INSERT INTO users
            (chat_id, user_id, username, first_name, messages)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (
            chat.id,
            user.id,
            user.username or "",
            user.full_name
        ))


async def is_admin(update, context):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        return False

    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ("administrator", "creator")


async def count_message(update, context):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or not chat:
        return

    if user.is_bot or chat.type not in ("group", "supergroup"):
        return

    save_user(chat, user)

    now = datetime.now(timezone.utc).isoformat()

    with connect() as db:
        db.execute("""
            UPDATE users
            SET messages = messages + 1
            WHERE chat_id = ? AND user_id = ?
        """, (chat.id, user.id))

        db.execute("""
            INSERT INTO message_logs
            (chat_id, user_id, created_at)
            VALUES (?, ?, ?)
        """, (chat.id, user.id, now))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Привет! Я веду статистику сообщений этого чата.\n\n"
        "Команды:\n"
        "профиль — твой профиль\n"
        "профиль @username — профиль участника\n"
        "награды — список твоих наград\n"
        "+ник Новый ник — установить ник\n"
        "+ник сброс — сбросить ник\n"
        "!наградить Текст — выдать награду (ответом)\n"
        "день — топ за 24 часа\n"
        "неделя — топ за 7 дней\n"
        "месяц — топ за 30 дней\n\n"
        "Топ и выдача наград доступны администраторам."
    )


async def profile(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Профили доступны в группе.")
        return

    target_id = user.id

    if context.args:
        requested = context.args[0].lstrip("@").lower()

        with connect() as db:
            row = db.execute("""
                SELECT user_id FROM users
                WHERE chat_id = ? AND LOWER(username) = ?
            """, (chat.id, requested)).fetchone()

        if not row:
            await msg.reply_text(
                "Участник не найден. Сначала он должен написать сообщение в группе."
            )
            return

        target_id = row[0]

    with connect() as db:
        row = db.execute("""
            SELECT username, first_name, nickname, messages
            FROM users
            WHERE chat_id = ? AND user_id = ?
        """, (chat.id, target_id)).fetchone()

        award_count = db.execute("""
            SELECT COUNT(*) FROM awards
            WHERE chat_id = ? AND user_id = ?
        """, (chat.id, target_id)).fetchone()[0]

    if not row:
        await msg.reply_text("Профиль пока не найден.")
        return

    username, first_name, nickname, messages = row

    display_name = nickname or first_name

    await msg.reply_text(
        f"👤 ПРОФИЛЬ\n\n"
        f"Имя: {display_name}\n"
        f"Username: @{username if username else 'не установлен'}\n"
        f"💬 Сообщений: {messages}\n"
        f"🏅 Наград: {award_count}"
    )


async def awards(update, context):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "Награды доступны в группе."
        )
        return

    with connect() as db:
        rows = db.execute("""
            SELECT award, created_at FROM awards
            WHERE chat_id = ? AND user_id = ?
            ORDER BY id DESC
        """, (chat.id, user.id)).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "🏅 У тебя пока нет наград."
        )
        return

    result = "🏅 ТВОИ НАГРАДЫ\n\n"

    for index, (award, date) in enumerate(rows, 1):
        result += f"{index}. {award}\n"

    await update.effective_message.reply_text(result)


async def nickname(update, context):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message

    if chat.type not in ("group", "supergroup"):
        return

    text = msg.text[len("+ник"):].strip()

    if not text:
        await msg.reply_text("Напиши: +ник Новый ник")
        return

    if text.lower() == "сброс":
        new_nick = ""
    else:
        if len(text) > 32:
            await msg.reply_text("Ник должен быть не длиннее 32 символов.")
            return
        new_nick = text

    save_user(chat, user)

    with connect() as db:
        db.execute("""
            UPDATE users SET nickname = ?
            WHERE chat_id = ? AND user_id = ?
        """, (new_nick, chat.id, user.id))

    if new_nick:
        await msg.reply_text(f"✅ Твой ник: {new_nick}")
    else:
        await msg.reply_text("✅ Ник сброшен.")


async def give_award(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    admin = await is_admin(update, context)

    if not admin:
        await msg.reply_text("⛔ Только администраторы могут выдавать награды.")
        return

    if not msg.reply_to_message:
        await msg.reply_text(
            "Ответь на сообщение участника командой:\n"
            "!наградить Текст награды"
        )
        return

    target = msg.reply_to_message.from_user
    award = msg.text[len("!наградить"):].strip()

    if not award:
        await msg.reply_text("Укажи текст награды.")
        return

    if target.is_bot:
        await msg.reply_text("Ботам нельзя выдавать награды.")
        return

    save_user(chat, target)

    with connect() as db:
        db.execute("""
            INSERT INTO awards
            (chat_id, user_id, award, given_by, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            chat.id,
            target.id,
            award,
            update.effective_user.id,
            datetime.now(timezone.utc).isoformat()
        ))

    await msg.reply_text(
        f"🏅 {target.full_name} получил(а) награду:\n"
        f"«{award}»"
    )


async def leaderboard(update, context, period):
    msg = update.effective_message
    chat = update.effective_chat

    if not await is_admin(update, context):
        await msg.reply_text("⛔ Топ доступен только администраторам.")
        return

    since = (
        datetime.now(timezone.utc) - timedelta(days=period)
    ).isoformat()

    with connect() as db:
        rows = db.execute("""
            SELECT u.first_name, u.nickname, COUNT(m.id) AS total
            FROM message_logs m
            JOIN users u
              ON u.chat_id = m.chat_id
             AND u.user_id = m.user_id
            WHERE m.chat_id = ? AND m.created_at >= ?
            GROUP BY m.user_id
            ORDER BY total DESC
            LIMIT 10
        """, (chat.id, since)).fetchall()

    if not rows:
        await msg.reply_text("Пока сообщений для рейтинга нет.")
        return

    titles = {
        1: "ТОП ЗА 24 ЧАСА",
        7: "ТОП ЗА 7 ДНЕЙ",
        30: "ТОП ЗА 30 ДНЕЙ"
    }

    result = f"🏆 {titles[period]}\n\n"
    medals = ["🥇", "🥈", "🥉"]

    for i, (name, nick, total) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i + 1}."
        result += f"{medal} {nick or name} — {total} сообщений\n"

    await msg.reply_text(result)


async def day(update, context):
    await leaderboard(update, context, 1)


async def week(update, context):
    await leaderboard(update, context, 7)


async def month(update, context):
    await leaderboard(update, context, 30)


async def text_commands(update, context):
    text = update.effective_message.text.strip()
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        return

    if text.startswith("профиль"):
        args = text.split(maxsplit=1)
        context.args = args[1].split() if len(args) > 1 else []
        await profile(update, context)

    elif text == "награды":
        await awards(update, context)

    elif text.startswith("+ник"):
        await nickname(update, context)

    elif text.startswith("!наградить"):
        await give_award(update, context)

    elif text == "день":
        await day(update, context)

    elif text == "неделя":
        await week(update, context)

    elif text == "месяц":
        await month(update, context)


def main():
    if not TOKEN:
        raise RuntimeError("Добавь BOT_TOKEN в переменные FadeHost.")

    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS,
            count_message
        ),
        group=0
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS,
            text_commands
        ),
        group=1
    )

    logger.info("Бот статистики запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
    
  
