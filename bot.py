import logging
import sqlite3
import csv
import io
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import (
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- 1. НАСТРОЙКИ ---
TOKEN = "7841096806:AAHvUUSs1YUk2Y34JGWWSPfScRKGc9ud_NM"
ADMIN_IDS = [7841096806]

# Состояния диалога
GET_RATING, GET_COMMENT = range(2)
BROADCAST_TEXT = 10

DB_NAME = "feedback.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- 2. БАЗА ДАННЫХ ---

def init_db():
    """Создаёт таблицы, если их нет."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                rating INTEGER,
                comment TEXT,
                timestamp DATETIME
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen DATETIME
            )
        """)
        conn.commit()


def seed_data():
    """Добавляет тестовые данные, если база пуста."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM reviews")
        if cursor.fetchone()[0] == 0:
            test_reviews = [
                (101, "student_alpha", 5, "Лекция была очень крутой!", "2023-10-01 10:00:00"),
                (102, "beta_user", 3, "Слишком быстрый темп.", "2023-10-01 10:05:00"),
                (103, "gamma_dev", 4, "Хорошие примеры кода.", "2023-10-01 10:10:00"),
            ]
            cursor.executemany(
                "INSERT INTO reviews (user_id, username, rating, comment, timestamp) VALUES (?, ?, ?, ?, ?)",
                test_reviews,
            )
            conn.commit()
            logger.info("Тестовые данные добавлены в базу.")


def register_user(user_id: int, username: str):
    """Регистрирует пользователя в таблице users."""
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_seen) VALUES (?, ?, ?)",
            (user_id, username, datetime.now()),
        )
        conn.commit()


def save_review_sync(user_id: int, username: str, rating: int, comment: str):
    """Синхронное сохранение отзыва."""
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            "INSERT INTO reviews (user_id, username, rating, comment, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, rating, comment, datetime.now()),
        )
        conn.commit()


def get_stats_sync() -> dict | None:
    """Возвращает статистику отзывов."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT rating, comment, timestamp, username FROM reviews ORDER BY timestamp DESC")
        rows = cursor.fetchall()
        if not rows:
            return None

        ratings = [r[0] for r in rows]
        comments = [(r[1], r[2], r[3]) for r in rows if r[1]]

        # Распределение оценок
        distribution = defaultdict(int)
        for r in ratings:
            distribution[r] += 1

        # Динамика за последние 7 дней
        week_ago = datetime.now() - timedelta(days=7)
        recent = [r for r in rows if datetime.fromisoformat(str(r[2])) >= week_ago]

        return {
            "count": len(ratings),
            "avg": round(sum(ratings) / len(ratings), 2),
            "comments": comments[:5],
            "distribution": dict(distribution),
            "recent_count": len(recent),
        }


def get_all_user_ids() -> list[int]:
    """Возвращает все user_id из таблицы users."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]


def export_reviews_csv() -> str:
    """Возвращает CSV-строку со всеми отзывами."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id, username, rating, comment, timestamp FROM reviews ORDER BY timestamp DESC")
        rows = cursor.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "User ID", "Username", "Rating", "Comment", "Timestamp"])
    writer.writerows(rows)
    return output.getvalue()


# --- 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_main_menu():
    return ReplyKeyboardMarkup(
        [["📝 Оставить отзыв"], ["📊 Отчёт", "📁 Экспорт CSV"], ["📢 Рассылка", "ℹ️ Помощь"]],
        resize_keyboard=True,
    )


def stars(rating: int) -> str:
    return "⭐" * rating + "☆" * (5 - rating)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# --- 4. ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or "unknown")
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n"
        "Я бот для сбора отзывов о лекциях.\n"
        "Выбери действие в меню ниже:",
        reply_markup=get_main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Справка*\n\n"
        "• *Оставить отзыв* — оценить лекцию и написать комментарий\n"
        "• *Отчёт* — статистика всех отзывов (только для преподавателя)\n"
        "• *Экспорт CSV* — скачать все отзывы в файле (только для преподавателя)\n"
        "• *Рассылка* — отправить сообщение всем студентам (только для преподавателя)\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_menu())


# --- Диалог: Отзыв ---

async def start_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Антиспам: один отзыв в день
    user_id = update.effective_user.id
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        today = datetime.now().date().isoformat()
        cursor.execute(
            "SELECT COUNT(*) FROM reviews WHERE user_id = ? AND DATE(timestamp) = ?",
            (user_id, today),
        )
        count = cursor.fetchone()[0]

    if count >= 1:
        await update.message.reply_text(
            "⚠️ Вы уже оставили отзыв сегодня. Возвращайтесь после следующей лекции!",
            reply_markup=get_main_menu(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Оцените лекцию от 1 до 5:\n\n"
        "1 ⭐ — очень плохо\n"
        "2 ⭐⭐ — плохо\n"
        "3 ⭐⭐⭐ — нормально\n"
        "4 ⭐⭐⭐⭐ — хорошо\n"
        "5 ⭐⭐⭐⭐⭐ — отлично",
        reply_markup=ReplyKeyboardMarkup(
            [["1", "2", "3", "4", "5"], ["❌ Отмена"]],
            resize_keyboard=True,
        ),
    )
    return GET_RATING


async def receive_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)

    rating = int(update.message.text)
    context.user_data["rating"] = rating
    await update.message.reply_text(
        f"Вы поставили {stars(rating)}\n\nТеперь напишите комментарий или пропустите:",
        reply_markup=ReplyKeyboardMarkup(
            [["Без комментария"], ["❌ Отмена"]],
            resize_keyboard=True,
        ),
    )
    return GET_COMMENT


async def receive_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)

    comment = "" if update.message.text == "Без комментария" else update.message.text
    user = update.effective_user
    rating = context.user_data["rating"]

    save_review_sync(user.id, user.username or "unknown", rating, comment)

    await update.message.reply_text(
        f"✅ Спасибо за отзыв!\nВаша оценка: {stars(rating)}",
        reply_markup=get_main_menu(),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.", reply_markup=get_main_menu())
    return ConversationHandler.END


# --- Функция: Отчёт ---

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ только для преподавателей.")
        return

    s = get_stats_sync()
    if not s:
        await update.message.reply_text("Отзывов пока нет.")
        return

    # Визуализация распределения оценок
    dist_text = ""
    for star in range(5, 0, -1):
        count = s["distribution"].get(star, 0)
        bar = "█" * count
        dist_text += f"{star}⭐ {bar} ({count})\n"

    text = (
        f"📊 *ОТЧЁТ ПО ОТЗЫВАМ*\n\n"
        f"Всего отзывов: *{s['count']}*\n"
        f"Средняя оценка: *{s['avg']} / 5.0*\n"
        f"За последние 7 дней: *{s['recent_count']}*\n\n"
        f"*Распределение оценок:*\n{dist_text}\n"
        f"*Последние комментарии:*\n"
    )
    for comment, ts, username in s["comments"]:
        if comment:
            short_date = str(ts)[:10]
            text += f"• @{username} ({short_date}): _{comment}_\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# --- Функция: Экспорт CSV ---

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ только для преподавателей.")
        return

    csv_data = export_reviews_csv()
    filename = f"reviews_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    file_bytes = csv_data.encode("utf-8-sig")  # utf-8-sig для корректного открытия в Excel

    await update.message.reply_document(
        document=InputFile(io.BytesIO(file_bytes), filename=filename),
        caption=f"📁 Экспорт отзывов — {datetime.now().strftime('%d.%m.%Y %H:%M')}",
    )


# --- Функция: Рассылка ---

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ только для преподавателей.")
        return ConversationHandler.END

    user_ids = get_all_user_ids()
    await update.message.reply_text(
        f"📢 Введите сообщение для рассылки.\n"
        f"Оно будет отправлено {len(user_ids)} пользователям.\n\n"
        "Или нажмите ❌ Отмена:",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return BROADCAST_TEXT


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)

    message_text = update.message.text
    user_ids = get_all_user_ids()

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 *Сообщение от преподавателя:*\n\n{message_text}",
                parse_mode="Markdown",
            )
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ Рассылка завершена!\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}",
        reply_markup=get_main_menu(),
    )
    return ConversationHandler.END


# --- 5. ЗАПУСК ---

def main():
    init_db()
    seed_data()

    app = Application.builder().token(TOKEN).build()

    # Диалог: отзыв
    feedback_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📝 Оставить отзыв"), start_feedback)],
        states={
            GET_RATING: [MessageHandler(filters.Regex("^[1-5]$"), receive_rating)],
            GET_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comment)],
        },
        fallbacks=[MessageHandler(filters.Text("❌ Отмена"), cancel)],
    )

    # Диалог: рассылка
    broadcast_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📢 Рассылка"), start_broadcast)],
        states={
            BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_broadcast)],
        },
        fallbacks=[MessageHandler(filters.Text("❌ Отмена"), cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Text("ℹ️ Помощь"), help_command))
    app.add_handler(MessageHandler(filters.Text("📊 Отчёт"), show_report))
    app.add_handler(MessageHandler(filters.Text("📁 Экспорт CSV"), export_csv))
    app.add_handler(feedback_handler)
    app.add_handler(broadcast_handler)

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
