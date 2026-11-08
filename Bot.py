import os
import re
import json
import sqlite3
import logging
import asyncio
import tempfile

from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= НАСТРОЙКИ =================

BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
)

DB_PATH = os.getenv("DB_PATH", "bot_database.db")

# Google Drive (необязательно)
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_JSON"
)
GOOGLE_DRIVE_FOLDER_ID = os.getenv(
    "GOOGLE_DRIVE_FOLDER_ID"
)

BACKUP_INTERVAL = 86400  # 24 часа

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ================= БАЗА ДАННЫХ =================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
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


def save_user(user):
    now = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        conn.execute("""
            INSERT INTO users (
                user_id, username, first_name,
                joined, last_seen
            )
            VALUES (?, ?, ?, ?, ?)

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


def count_message(user):
    save_user(user)

    with db() as conn:
        conn.execute("""
            UPDATE users
            SET messages = messages + 1
            WHERE user_id = ?
        """, (user.id,))


def set_nickname(user_id, nickname):
    with db() as conn:
        cursor = conn.execute("""
            UPDATE users
            SET nickname = ?
            WHERE user_id = ?
        """, (nickname, user_id))

        return cursor.rowcount > 0


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
    # СНАЧАЛА персональный ник, потом имя Telegram
    nickname = user["nickname"]

    if nickname and nickname.strip():
        return nickname.strip()

    if user["username"]:
        return "@" + user["username"]

    return user["first_name"] or "Пользователь"


# ================= НАГРАДЫ =================

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
                        INSERT INTO rewards (user_id, reward)
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


# ================= ПРОФИЛЬ =================

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    target = None

    if context.args:
        argument = context.args[0]

        if argument.startswith("@"):
            target = get_user_by_username(argument)
        elif argument.isdigit():
            target = get_user(int(argument))

    elif update.message.reply_to_message:
        replied = update.message.reply_to_message.from_user
        save_user(replied)
        target = get_user(replied.id)

    else:
        save_user(update.effective_user)
        target = get_user(update.effective_user.id)

    if not target:
        await update.message.reply_text(
            "Пользователь пока не найден в базе данных."
        )
        return

    name = get_display_name(target)
    rewards = get_rewards(target["user_id"])

    rewards_text = (
        "\n".join("🏅 " + r for r in rewards)
        if rewards else "Пока нет наград"
    )

    await update.message.reply_text(
        f"👤 ПРОФИЛЬ\n\n"
        f"Имя: {name}\n"
        f"ID: {target['user_id']}\n"
        f"Сообщений: {target['messages']}\n\n"
        f"🏆 НАГРАДЫ:\n{rewards_text}"
    )


# ================= УСТАНОВКА НИКА =================

async def nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "Использование:\n+ник НовыйНик"
        )
        return

    new_nickname = " ".join(context.args).strip()

    if len(new_nickname) > 32:
        await update.message.reply_text(
            "Ник не должен быть длиннее 32 символов."
        )
        return

    if not re.fullmatch(r"[\w\s\-\.!?]+", new_nickname, re.UNICODE):
        await update.message.reply_text(
            "Ник содержит недопустимые символы."
        )
        return

    user = update.effective_user
    save_user(user)

    set_nickname(user.id, new_nickname)

    await update.message.reply_text(
        f"✅ Твой новый ник: {new_nickname}\n"
        f"Теперь он будет отображаться в профиле."
    )


# ================= СПИСОК НАГРАД =================

async def rewards_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = "🏆 СПИСОК НАГРАД\n\n"

    for amount, reward in REWARD_LEVELS:
        text += f"🥇 {reward} — {amount} сообщений\n"

    await update.message.reply_text(text)


# ================= СТАТИСТИКА =================

async def statistics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    with db() as conn:
        total_users = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        total_messages = conn.execute(
            "SELECT SUM(messages) FROM users"
        ).fetchone()[0] or 0

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

    for i, user in enumerate(top, 1):
        text += (
            f"{i}. {get_display_name(user)}"
            f" — {user['messages']}\n"
        )

    await update.message.reply_text(text)


# ================= ОБРАБОТКА СООБЩЕНИЙ =================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user

    if user.is_bot:
        return

    text = (update.message.text or "").strip()

    # Команда установки ника
    if text.lower().startswith("+ник"):
        context.args = text.split()[1:]
        await nickname(update, context)
        return

    # Считаем обычные сообщения
    count_message(user)

    record = get_user(user.id)

    new_rewards = update_rewards(
        user.id,
        record["messages"],
    )

    for reward in new_rewards:
        await update.message.reply_text(
            f"🎉 Поздравляем!\n"
            f"Получена награда: {reward}"
        )


# ================= GOOGLE DRIVE =================

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

        credentials = service_account.Credentials.from_service_account_info(
            credentials_data,
            scopes=[
                "https://www.googleapis.com/auth/drive.file"
            ],
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
            destination = sqlite3.connect(backup_path)

            source.backup(destination)

            destination.close()
            source.close()

            media = MediaFileUpload(
                str(backup_path),
                mimetype="application/x-sqlite3",
            )

            service.files().create(
                body={
                    "name": "telegram_bot_backup.db",
                    "parents": [GOOGLE_DRIVE_FOLDER_ID],
                },
                media_body=media,
                fields="id",
            ).execute()

        logger.info("Резервная копия загружена.")

    except Exception:
        logger.exception("Ошибка резервного копирования.")


async def backup_job(context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(upload_backup)


# ================= ЗАПУСК =================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Добавь TELEGRAM_BOT_TOKEN в переменные окружения."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
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
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    application.job_queue.run_repeating(
        backup_job,
        interval=BACKUP_INTERVAL,
        first=60,
    )

    logger.info("Бот запущен!")

    application.run_polling()


if __name__ == "__main__":
    main()
