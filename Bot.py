
import os
import json
import sqlite3
import logging
import asyncio
import tempfile
import time

from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =============== НАСТРОЙКИ ===============

BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
)

DB_PATH = os.getenv("DB_PATH", "bot_database.db")

GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_JSON"
)
GOOGLE_DRIVE_FOLDER_ID = os.getenv(
    "GOOGLE_DRIVE_FOLDER_ID"
)

BACKUP_INTERVAL = 86400

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

REWARD_LEVELS = [
    (100, "Активист"),
    (500, "Постоянный участник"),
    (1000, "Легенда чата"),
    (5000, "Мастер общения"),
]


# =============== БАЗА ДАННЫХ ===============

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                nickname TEXT DEFAULT '',
                messages INTEGER DEFAULT 0,
                joined TEXT,
                last_seen TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS rewards (
                user_id INTEGER,
                reward TEXT,
                PRIMARY KEY (user_id, reward)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                timestamp INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_user_chat_time
            ON activity(user_id, chat_id, timestamp)
        """)

        # Совместимость со старой базой
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }

        migrations = {
            "username": "TEXT",
            "first_name": "TEXT",
            "nickname": "TEXT DEFAULT ''",
            "messages": "INTEGER DEFAULT 0",
            "joined": "TEXT",
            "last_seen": "TEXT",
        }

        for column, definition in migrations.items():
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE users ADD COLUMN "
                    f"{column} {definition}"
                )

    logger.info("База данных готова.")


# =============== ПОЛЬЗОВАТЕЛИ ===============

def save_user(user):
    now = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        conn.execute("""
            INSERT INTO users (
                user_id, username, first_name,
                nickname, messages, joined, last_seen
            )
            VALUES (?, ?, ?, '', 0, ?, ?)

            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_seen = excluded.last_seen
        """, (
            user.id,
            user.username,
            user.first_name,
            now,
            now,
        ))


def get_user(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT * FROM users
            WHERE user_id = ?
        """, (user_id,)).fetchone()


def get_user_by_username(username):
    username = username.lstrip("@").lower()

    with db() as conn:
        return conn.execute("""
            SELECT * FROM users
            WHERE LOWER(username) = ?
        """, (username,)).fetchone()


def get_display_name(user):
    nickname = user["nickname"]

    if nickname and nickname.strip():
        return nickname.strip()

    if user["username"]:
        return "@" + user["username"]

    return user["first_name"] or "Пользователь"


# =============== НИК ===============

def set_nickname(user_id, nickname):
    with db() as conn:
        cursor = conn.execute("""
            UPDATE users
            SET nickname = ?
            WHERE user_id = ?
        """, (nickname, user_id))

        return cursor.rowcount > 0


async def nickname_command(update, context):
    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    nickname = " ".join(context.args).strip()

    if not nickname:
        await message.reply_text(
            "✏️ Использование:\n"
            "+ник НовыйНик\n\n"
            "Для удаления ника:\n"
            "+ник сброс"
        )
        return

    if len(nickname) > 32:
        await message.reply_text(
            "❌ Максимальная длина ника — 32 символа."
        )
        return

    if any(ord(char) < 32 for char in nickname):
        await message.reply_text(
            "❌ Недопустимые символы."
        )
        return

    save_user(user)

    if nickname.lower() == "сброс":
        set_nickname(user.id, "")

        await message.reply_text(
            "✅ Твой ник сброшен."
        )
        return

    set_nickname(user.id, nickname)

    await message.reply_text(
        f"✅ Твой новый ник: {nickname}"
    )


# =============== АКТИВНОСТЬ ===============

def count_message(user, chat_id):
    now = int(time.time())

    save_user(user)

    with db() as conn:
        conn.execute("""
            UPDATE users
            SET messages = messages + 1,
                last_seen = ?
            WHERE user_id = ?
        """, (
            datetime.now(timezone.utc).isoformat(),
            user.id,
        ))

        conn.execute("""
            INSERT INTO activity (
                user_id, chat_id, timestamp
            )
            VALUES (?, ?, ?)
        """, (
            user.id,
            chat_id,
            now,
        ))

        messages = conn.execute("""
            SELECT messages FROM users
            WHERE user_id = ?
        """, (user.id,)).fetchone()["messages"]

    return messages


def get_activity(user_id, chat_id):
    now = int(time.time())

    day = now - 86400
    week = now - 7 * 86400
    month = now - 30 * 86400

    with db() as conn:
        result = conn.execute("""
            SELECT
                SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS day,
                SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS week,
                SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS month,
                COUNT(*) AS total
            FROM activity
            WHERE user_id = ? AND chat_id = ?
        """, (
            day,
            week,
            month,
            user_id,
            chat_id,
        )).fetchone()

    return (
        result["day"] or 0,
        result["week"] or 0,
        result["month"] or 0,
        result["total"] or 0,
    )


def format_last_active(last_seen):
    if not last_seen:
        return "неизвестно"

    try:
        last = datetime.fromisoformat(last_seen)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)

        seconds = max(
            0,
            int((datetime.now(timezone.utc) - last).total_seconds())
        )

    except (ValueError, TypeError):
        return "неизвестно"

    if seconds < 60:
        return "менее минуты назад"

    if seconds < 3600:
        return f"{seconds // 60} мин. назад"

    if seconds < 86400:
        return f"{seconds // 3600} ч. назад"

    if seconds < 172800:
        return "вчера"

    return f"{seconds // 86400} дн. назад"


# =============== НАГРАДЫ ===============

def update_rewards(user_id, messages):
    new_rewards = []

    with db() as conn:
        for threshold, reward in REWARD_LEVELS:
            if messages >= threshold:
                exists = conn.execute("""
                    SELECT 1 FROM rewards
                    WHERE user_id = ? AND reward = ?
                """, (user_id, reward)).fetchone()

                if not exists:
                    conn.execute("""
                        INSERT OR IGNORE INTO rewards
                        (user_id, reward)
                        VALUES (?, ?)
                    """, (user_id, reward))

                    new_rewards.append(reward)

    return new_rewards


def get_rewards(user_id):
    with db() as conn:
        rows = conn.execute("""
            SELECT reward FROM rewards
            WHERE user_id = ?
        """, (user_id,)).fetchall()

    return [row["reward"] for row in rows]


# =============== ПРОВЕРКА ЧАТА ===============

async def get_membership(context, chat_id, user_id):
    try:
        member = await context.bot.get_chat_member(
            chat_id,
            user_id,
        )

        if member.status in ("creator", "administrator", "member"):
            return "💚 Состоит в чате"

        if member.status == "restricted" and member.is_member:
            return "💚 Состоит в чате"

        if member.status in ("left", "kicked"):
            return "❤️ Не состоит в чате"

        return "Статус неизвестен"

    except Exception as error:
        logger.warning(
            "Не удалось проверить участие: %s",
            error,
        )
        return "Статус неизвестен"


# =============== ПРОФИЛЬ ===============

async def profile(update, context):
    message = update.message

    if not message:
        return

    chat = update.effective_chat
    target = None

    # Профиль по @username или ID
    if context.args:
        argument = context.args[0]

        if argument.startswith("@"):
            target = get_user_by_username(argument)

        elif argument.isdigit():
            target = get_user(int(argument))

    # Профиль через ответ на сообщение
    elif message.reply_to_message:
        replied_user = message.reply_to_message.from_user

        if replied_user and not replied_user.is_bot:
            save_user(replied_user)
            target = get_user(replied_user.id)

    # Свой профиль
    else:
        user = update.effective_user

        if user:
            save_user(user)
            target = get_user(user.id)

    if not target:
        await message.reply_text(
            "❌ Пользователь не найден.\n"
            "Он должен хотя бы раз написать боту "
            "в этом чате."
        )
        return

    user_id = target["user_id"]

    name = get_display_name(target)

    membership = await get_membership(
        context,
        chat.id,
        user_id,
    )

    last_active = format_last_active(
        target["last_seen"]
    )

    day, week, month, total = get_activity(
        user_id,
        chat.id,
    )

    # Для старой базы сохраняем старое общее
    # количество сообщений, если новых записей
    # активности ещё нет.
    total = max(total, target["messages"] or 0)

    rewards = get_rewards(user_id)

    rewards_text = (
        "\n".join("🏅 " + reward for reward in rewards)
        if rewards
        else "🎗 Пока нет наград"
    )

    profile_text = (
        f"👤 Это пользователь {name}\n"
        f"{membership}\n\n"
        f"Последний актив: {last_active}\n\n"
        f"Актив (д|н|м|весь): "
        f"{day} | {week} | {month} | {total}\n\n"
        f"🏆 НАГРАДЫ:\n"
        f"{rewards_text}"
    )

    await message.reply_text(profile_text)


# =============== СПИСОК НАГРАД ===============

async def rewards_command(update, context):
    text = "🏆 СПИСОК НАГРАД\n\n"

    for amount, reward in REWARD_LEVELS:
        text += f"🎗 {reward} — {amount} сообщений\n"

    await update.message.reply_text(text)


# =============== СТАТИСТИКА ===============

async def statistics(update, context):
    chat_id = update.effective_chat.id

    with db() as conn:
        total_users = conn.execute("""
            SELECT COUNT(DISTINCT user_id)
            FROM activity WHERE chat_id = ?
        """, (chat_id,)).fetchone()[0]

        total_messages = conn.execute("""
            SELECT COUNT(*) FROM activity
            WHERE chat_id = ?
        """, (chat_id,)).fetchone()[0]

        top = conn.execute("""
            SELECT
                u.*,
                COUNT(a.id) AS chat_messages
            FROM users u
            JOIN activity a ON a.user_id = u.user_id
            WHERE a.chat_id = ?
            GROUP BY u.user_id
            ORDER BY chat_messages DESC
            LIMIT 10
        """, (chat_id,)).fetchall()

    text = (
        "📊 СТАТИСТИКА ЧАТА\n\n"
        f"👥 Участников: {total_users}\n"
        f"💬 Сообщений: {total_messages}\n\n"
        "🏆 ТОП-10:\n"
    )

    for index, user in enumerate(top, 1):
        text += (
            f"{index}. {get_display_name(user)}"
            f" — {user['chat_messages']}\n"
        )

    await update.message.reply_text(text)


# =============== ОБРАБОТКА СООБЩЕНИЙ ===============

async def handle_message(update, context):
    message = update.message
    user = update.effective_user

    if not message or not user or user.is_bot:
        return

    text = (message.text or "").strip()
    lower = text.lower()

    if lower == "профиль":
        await profile(update, context)
        return

    if lower.startswith("профиль "):
        context.args = text.split(maxsplit=1)[1].split()
        await profile(update, context)
        return

    if lower == "награды":
        await rewards_command(update, context)
        return

    if lower in ("статистика", "стат"):
        await statistics(update, context)
        return

    if lower.startswith("+ник"):
        parts = text.split(maxsplit=1)
        context.args = (
            parts[1].split()
            if len(parts) > 1 else []
        )
        await nickname_command(update, context)
        return

    # Считаем сообщения в группах и супергруппах
    if update.effective_chat.type not in (
        "group",
        "supergroup",
    ):
        return

    messages = count_message(
        user,
        update.effective_chat.id,
    )

    new_rewards = update_rewards(
        user.id,
        messages,
    )

    for reward in new_rewards:
        await message.reply_text(
            f"🎉 Поздравляем!\n"
            f"Получена награда: {reward}"
        )


# =============== GOOGLE DRIVE ===============

def upload_backup():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.info("Google Drive не настроен.")
        return

    if not GOOGLE_DRIVE_FOLDER_ID:
        logger.warning("Не указан ID папки Google Drive.")
        return

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        credentials_data = json.loads(
            GOOGLE_SERVICE_ACCOUNT_JSON
        )

        credentials = (
            service_account.Credentials
            .from_service_account_info(
                credentials_data,
                scopes=[
                    "https://www.googleapis.com/auth/drive"
                ],
            )
        )

        service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = Path(temp_dir) / "backup.db"

            source = sqlite3.connect(DB_PATH)
            destination = sqlite3.connect(str(backup_path))

            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()

            media = MediaFileUpload(
                str(backup_path),
                mimetype="application/x-sqlite3",
                resumable=True,
            )

            query = (
                "name='telegram_bot_backup.db' "
                f"and '{GOOGLE_DRIVE_FOLDER_ID}' in parents "
                "and trashed=false"
            )

            result = service.files().list(
                q=query,
                pageSize=1,
                fields="files(id)",
            ).execute()

            files = result.get("files", [])

            if files:
                service.files().update(
                    fileId=files[0]["id"],
                    media_body=media,
                ).execute()
            else:
                service.files().create(
                    body={
                        "name": "telegram_bot_backup.db",
                        "parents": [GOOGLE_DRIVE_FOLDER_ID],
                    },
                    media_body=media,
                    fields="id",
                ).execute()

        logger.info("Google Drive: резервная копия обновлена.")

    except Exception:
        logger.exception("Ошибка Google Drive backup.")


async def backup_loop(application):
    while True:
        try:
            await asyncio.to_thread(upload_backup)
        except Exception:
            logger.exception("Ошибка резервного копирования.")

        await asyncio.sleep(BACKUP_INTERVAL)


async def post_init(application):
    application.bot_data["backup_task"] = asyncio.create_task(
        backup_loop(application)
    )


async def post_shutdown(application):
    task = application.bot_data.get("backup_task")

    if task:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass


# =============== ЗАПУСК ===============

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN или BOT_TOKEN."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(
        CommandHandler("start", profile)
    )

    application.add_handler(
        CommandHandler("profile", profile)
    )

    application.add_handler(
        CommandHandler("rewards", rewards_command)
    )

    application.add_handler(
        CommandHandler("stats", statistics)
    )

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info("Бот успешно запущен!")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
