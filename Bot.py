
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

# ================ НАСТРОЙКИ ================

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


# ================ БАЗА ДАННЫХ ================

def connect_db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users_v2 (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                nickname TEXT DEFAULT '',
                messages INTEGER DEFAULT 0,
                last_active TEXT DEFAULT '',
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

        # Добавляем недостающие поля в уже существующую таблицу.
        columns = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(users_v2)"
            ).fetchall()
        }

        if "last_active" not in columns:
            db.execute(
                "ALTER TABLE users_v2 ADD COLUMN last_active TEXT DEFAULT ''"
            )

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_logs_chat_date
            ON message_logs_v2(chat_id, created_at)
        """)


def save_user(chat, user):
    with connect_db() as db:
        db.execute("""
            INSERT INTO users_v2 (
                chat_id, user_id, first_name, last_name, username
            )
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
            SET messages = messages + 1,
                last_active = ?
            WHERE chat_id = ? AND user_id = ?
        """, (now, chat_id, user_id))

        db.execute("""
            INSERT INTO message_logs_v2
                (chat_id, user_id, created_at)
            VALUES (?, ?, ?)
        """, (chat_id, user_id, now))


def display_name(row):
    nickname, first_name, last_name, username = row

    if nickname:
        return nickname

    full_name = f"{first_name} {last_name}".strip()

    if full_name:
        return full_name

    if username:
        return f"@{username}"

    return "Без имени"


def format_last_active(value):
    if not value:
        return "нет данных"

    try:
        last = datetime.fromisoformat(value)

        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)

        seconds = max(
            0,
            int(
                (
                    datetime.now(timezone.utc) - last
                ).total_seconds()
            ),
        )

        if seconds < 60:
            return "менее минуты назад"

        if seconds < 3600:
            return f"{seconds // 60} мин. назад"

        if seconds < 86400:
            return f"{seconds // 3600} ч. назад"

        return f"{seconds // 86400} дн. назад"

    except (ValueError, TypeError):
        return "нет данных"


# ================ ПРОВЕРКА АДМИНА ================

async def is_admin(update, context):
    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id,
        )

        return member.status in ("administrator", "creator")

    except Exception:
        logger.exception("Ошибка проверки администратора")
        return False


# ================ ПОДСЧЁТ СООБЩЕНИЙ ================

async def count_message(update, context):
    if not update.effective_chat or not update.effective_user:
        return

    chat = update.effective_chat
    user = update.effective_user

    if user.is_bot:
        return

    save_user(chat, user)
    add_message(chat.id, user.id)


# ================ ПРОФИЛЬ ================

async def profile(update, context):
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message

    parts = message.text.strip().split(maxsplit=1)

    if len(parts) > 1 and parts[1].startswith("@"):
        username = parts[1][1:].strip().lstrip("@")

        with connect_db() as db:
            row = db.execute("""
                SELECT
                    nickname, first_name, last_name, username,
                    user_id, messages, last_active
                FROM users_v2
                WHERE chat_id = ?
                AND LOWER(username) = LOWER(?)
                LIMIT 1
            """, (chat.id, username)).fetchone()

        if not row:
            await message.reply_text(
                "Не нашёл пользователя.\n"
                "Он должен хотя бы раз написать в группе."
            )
            return

        nickname_value, first, last, username_value = row[:4]
        target_id = row[4]
        total = row[5]
        last_active = row[6]

    else:
        save_user(chat, user)

        with connect_db() as db:
            row = db.execute("""
                SELECT
                    nickname, first_name, last_name, username,
                    user_id, messages, last_active
                FROM users_v2
                WHERE chat_id = ? AND user_id = ?
            """, (chat.id, user.id)).fetchone()

        nickname_value, first, last, username_value = row[:4]
        target_id = row[4]
        total = row[5]
        last_active = row[6]

    name = display_name((
        nickname_value,
        first,
        last,
        username_value,
    ))

    username_display = (
        f"@{username_value}"
        if username_value
        else name
    )

    # Проверяем, состоит ли пользователь в группе.
    try:
        member = await context.bot.get_chat_member(
            chat.id,
            target_id,
        )

        is_member = member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception:
        is_member = False

    status = (
        "💚 Состоит в чате"
        if is_member
        else "❤️ Не состоит в чате"
    )

    # Статистика за разные периоды.
    now = datetime.now(timezone.utc)

    periods = [
        ("день", now - timedelta(days=1)),
        ("неделя", now - timedelta(days=7)),
        ("месяц", now - timedelta(days=30)),
    ]

    activity = []

    with connect_db() as db:
        for period_name, start in periods:
            count = db.execute("""
                SELECT COUNT(*)
                FROM message_logs_v2
                WHERE chat_id = ?
                AND user_id = ?
                AND created_at >= ?
            """, (
                chat.id,
                target_id,
                start.isoformat(),
            )).fetchone()[0]

            activity.append(count)

        # Общее количество сообщений.
        activity.append(total or 0)

        # Награды пользователя.
        awards = db.execute("""
            SELECT award
            FROM awards_v2
            WHERE chat_id = ? AND user_id = ?
            ORDER BY id DESC
        """, (chat.id, target_id)).fetchall()

    result = (
        f"👤 Это пользователь {username_display}\n"
        f"{status}\n\n"
        f"Последний актив: {format_last_active(last_active)}\n\n"
        f"Актив (д|н|м|весь): "
        f"{activity[0]} | {activity[1]} | "
        f"{activity[2]} | {activity[3]}\n\n"
        f"🏆 НАГРАДЫ:\n"
    )

    if awards:
        for award in awards:
            result += f"🎗 {award[0]}\n"
    else:
        result += "🎗 Пока нет наград"

    await message.reply_text(result)


# ================ СМЕНА НИКА ================

async def nickname(update, context):
    chat = update.effective_chat
    user = update.effective_user
    text = update.effective_message.text.strip()

    parts = text.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        await update.effective_message.reply_text(
            "Напиши:\n+ник Новый ник\n+ник сброс"
        )
        return

    new_nickname = parts[1].strip()

    if len(new_nickname) > 40:
        await update.effective_message.reply_text(
            "Ник не должен быть длиннее 40 символов."
        )
        return

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
            db.execute("""
                UPDATE users_v2
                SET nickname = ?
                WHERE chat_id = ? AND user_id = ?
            """, (new_nickname, chat.id, user.id))

            answer = f"Твой новый ник: {new_nickname}"

    await update.effective_message.reply_text(answer)


# ================ НАГРАДЫ ================

async def show_awards(update, context):
    chat = update.effective_chat
    user = update.effective_user

    with connect_db() as db:
        rows = db.execute("""
            SELECT award
            FROM awards_v2
            WHERE chat_id = ? AND user_id = ?
            ORDER BY id DESC
        """, (chat.id, user.id)).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "🏆 НАГРАДЫ:\n🎗 Пока нет наград."
        )
        return

    result = ["🏆 НАГРАДЫ:"]

    for award in rows:
        result.append(f"🎗 {award[0]}")

    await update.effective_message.reply_text(
        "\n".join(result)
    )


async def give_award(update, context):
    message = update.effective_message

    if not await is_admin(update, context):
        await message.reply_text(
            "Выдавать награды могут только администраторы."
        )
        return

    reply = message.reply_to_message

    if not reply or not reply.from_user:
        await message.reply_text(
            "Ответь на сообщение участника командой:\n"
            "!наградить Текст награды"
        )
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text(
            "Укажи текст награды."
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
        f"🏆 {target.first_name} получил(а) награду:\n"
        f"🎗 {award}"
    )


# ================ ТОП СООБЩЕНИЙ ================

async def show_top(update, context, period, title):
    message = update.effective_message

    if not await is_admin(update, context):
        await message.reply_text(
            "Просматривать топ могут только администраторы."
        )
        return

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
                COUNT(m.id) AS total
            FROM message_logs_v2 m
            JOIN users_v2 u
              ON u.chat_id = m.chat_id
              AND u.user_id = m.user_id
            WHERE m.chat_id = ?
              AND m.created_at >= ?
            GROUP BY u.user_id
            ORDER BY total DESC
            LIMIT 10
        """, (
            update.effective_chat.id,
            start.isoformat(),
        )).fetchall()

    if not rows:
        await message.reply_text(
            f"🏆 {title}\nПока сообщений нет."
        )
        return

    result = [f"🏆 {title}"]

    for index, row in enumerate(rows, 1):
        name = display_name(row[:4])
        result.append(f"{index}. {name} — {row[4]}")

    await message.reply_text("\n".join(result))


# ================ КОМАНДЫ ================

async def text_commands(update, context):
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
        await show_top(
            update, context, "day",
            "Топ за последние 24 часа"
        )

    elif command == "неделя":
        await show_top(
            update, context, "week",
            "Топ за 7 дней"
        )

    elif command == "месяц":
        await show_top(
            update, context, "month",
            "Топ за 30 дней"
        )


# ================ START ================

async def start(update, context):
    await update.effective_message.reply_text(
        "Привет! Я бот статистики группы.\n\n"
        "Команды:\n"
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


# ================ ОШИБКИ ================

async def error_handler(update, context):
    logger.error(
        "Ошибка при обработке сообщения",
        exc_info=context.error,
    )


# ================ ЗАПУСК ================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден токен. Добавь BOT_TOKEN в FadeHost."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    group_text = (
        filters.TEXT
        & ~filters.COMMAND
        & filters.ChatType.GROUPS
    )

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
        
