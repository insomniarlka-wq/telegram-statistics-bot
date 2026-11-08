
import os
import re
import json
import sqlite3
import logging
import asyncio
import tempfile

from pathlib import Path
from contextlib import closing
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


# =============== БАЗА ДАННЫХ ===============

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return closing(conn)


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

        # Миграция старых баз данных
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

        conn.commit()


# =============== ПОЛЬЗОВАТЕЛИ ===============

def save_user(user):
    now = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        conn.execute("""
            INSERT INTO users (
                user_id,
                username,
                first_name,
                nickname,
                messages,
                joined,
                last_seen
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

        conn.commit()


def get_user(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def get_user_by_username(username):
    username = username.lstrip("@").lower()

    with db() as conn:
        return conn.execute("""
            SELECT * FROM users
            WHERE LOWER(username) = ?
            LIMIT 1
        """, (username,)).fetchone()


def get_display_name(user):
    nickname = user["nickname"]

    if nickname and nickname.strip():
        return nickname.strip()

    if user["username"]:
        return "@" + user["username"]

    return user["first_name"] or "Пользователь"


def set_nickname(user_id, nickname):
    with db() as conn:
        cursor = conn.execute("""
            UPDATE users
            SET nickname = ?
            WHERE user_id = ?
        """, (nickname, user_id))

        conn.commit()
        return cursor.rowcount > 0


def count_message(user):
    save_user(user)

    with db() as conn:
        conn.execute("""
            UPDATE users
            SET messages = messages + 1
            WHERE user_id = ?
        """, (user.id,))

        conn.commit()

        result = conn.execute("""
            SELECT messages FROM users
            WHERE user_id = ?
        """, (user.id,)).fetchone()

        return result["messages"]


# =============== НАГРАДЫ ===============

REWARD_LEVELS = [
    (100, "Активист"),
    (500, "Постоянный участник"),
    (1000, "Легенда чата"),
    (5000, "Мастер общения"),
]


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

        conn.commit()

    return new_rewards


def get_rewards(user_id):
    with db() as conn:
        rows = conn.execute("""
            SELECT reward FROM rewards
            WHERE user_id = ?
        """, (user_id,)).fetchall()

    return [row["reward"] for row in rows]


# =============== ПРОФИЛЬ ===============

async def profile(update, context):
    message = update.message

    if not message:
        return

    target = None

    if context.args:
        argument = context.args[0]

        if argument.startswith("@"):
            target = get_user_by_username(argument)

        elif argument.isdigit():
            target = get_user(int(argument))

    elif message.reply_to_message:
        replied_user = message.reply_to_message.from_user

        if not replied_user.is_bot:
            save_user(replied_user)
            target = get_user(replied_user.id)

    else:
        current_user = update.effective_user

        if current_user:
            save_user(current_user)
            target = get_user(current_user.id)

    if not target:
        await message.reply_text(
            "❌ Пользователь не найден.\n"
            "Он должен хотя бы раз написать сообщение "
            "в чате с ботом."
        )
        return

    name = get_display_name(target)
    rewards = get_rewards(target["user_id"])

    rewards_text = (
        "\n".join("🏅 " + item for item in rewards)
        if rewards else "Пока нет наград"
    )

    await message.reply_text(
        f"👤 ПРОФИЛЬ\n\n"
        f"Имя: {name}\n"
        f"ID: {target['user_id']}\n"
        f"Сообщений: {target['messages']}\n\n"
        f"🏆 НАГРАДЫ:\n{rewards_text}"
    )


# =============== НИК ===============

async def nickname(update, context):
    message = update.message

    if not message:
        return

    new_nickname = " ".join(context.args).strip()

    if not new_nickname:
        await message.reply_text(
            "Использование:\n+ник НовыйНик\n\n"
            "Для удаления ника:\n+ник сброс"
        )
        return

    user = update.effective_user

    if len(new_nickname) > 32:
        await message.reply_text(
            "❌ Максимальная длина ника — 32 символа."
        )
        return

    if any(ord(char) < 32 for char in new_nickname):
        await message.reply_text(
            "❌ Недопустимые символы."
        )
        return

    save_user(user)

    if new_nickname.lower() == "сброс":
        set_nickname(user.id, "")

        await message.reply_text(
            "✅ Персональный ник удалён.\n"
            "Теперь отображается имя Telegram."
        )
        return

    set_nickname(user.id, new_nickname)

    await message.reply_text(
        f"✅ Ник установлен: {new_nickname}\n"
        "Теперь он будет отображаться в профиле."
    )


# =============== НАГРАДЫ ===============

async def rewards_command(update, context):
    text = "🏆 СПИСОК НАГРАД\n\n"

    for amount, reward in REWARD_LEVELS:
        text += f"🥇 {reward} — {amount} сообщений\n"

    await update.message.reply_text(text)


# =============== СТАТИСТИКА ===============

async def statistics(update, context):
    with db() as conn:
        total_users = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        total_messages = conn.execute(
            "SELECT COALESCE(SUM(messages), 0) FROM users"
        ).fetchone()[0]

        top = conn.execute("""
            SELECT * FROM users
            ORDER BY messages DESC
            LIMIT 10
        """).fetchall()

    text = (
        "📊 СТАТИСТИКА ЧАТА\n\n"
        f"👥 Участников: {total_users}\n"
        f"💬 Сообщений: {total_messages}\n\n"
        "🏆 ТОП-10:\n"
    )

    if not top:
        text += "Пока нет участников."
    else:
        for index, user in enumerate(top, 1):
            text += (
                f"{index}. {get_display_name(user)}"
                f" — {user['messages']}\n"
            )

    await update.message.reply_text(text)


# =============== ОБРАБОТКА СООБЩЕНИЙ ===============

async def handle_message(update, context):
    message = update.message

    if not message or not update.effective_user:
        return

    user = update.effective_user

    if user.is_bot:
        return

    text = (message.text or "").strip()
    lower = text.lower()

    # Обычные текстовые команды
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
        context.args = parts[1].split() if len(parts) > 1 else []
        await nickname(update, context)
        return

    # Подсчёт обычного сообщения
    messages = count_message(user)

    new_rewards = update_rewards(user.id, messages)

    for reward in new_rewards:
        await message.reply_text(
            f"🎉 Поздравляем!\n"
            f"Получена награда: {reward}"
        )


# =============== GOOGLE DRIVE ===============

def upload_backup():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.info("Google Drive не подключён.")
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

        credentials = service_account.Credentials.from_service_account_info(
            credentials_data,
            scopes=["https://www.googleapis.com/auth/drive"],
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

            folder_id = GOOGLE_DRIVE_FOLDER_ID.replace(
                "'", "\\'"
            )

            existing = service.files().list(
                q=(
                    "name='telegram_bot_backup.db' "
                    f"and '{folder_id}' in parents "
                    "and trashed=false"
                ),
                pageSize=1,
                fields="files(id)",
            ).execute()

            files = existing.get("files", [])

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

        logger.info("Резервная копия Google Drive обновлена.")

    except Exception:
        logger.exception("Ошибка резервного копирования.")


async def backup_loop(application):
    while True:
        try:
            await asyncio.to_thread(upload_backup)
        except Exception:
            logger.exception("Ошибка фонового резервирования.")

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
            "Не найден TELEGRAM_BOT_TOKEN или BOT_TOKEN."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Команды Telegram
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

    # Обычные сообщения
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info("Бот успешно запущен!")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
                                      
