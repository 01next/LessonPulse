import logging
import sqlite3
import csv
import io
import os
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import (
    ReplyKeyboardMarkup,
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

# --- 1. НАСТРОЙКИ ---
TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",")]
TEACHER_CODE = os.environ.get("TEACHER_CODE", "TEACHER2024")

GET_RATING, GET_COMMENT = range(2)
BROADCAST_TEXT = 10
TEACHER_CODE_INPUT = 21

DB_NAME = "feedback.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- 2. БАЗА ДАННЫХ ---

def init_db():
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
                first_name TEXT,
                last_name TEXT,
                role TEXT DEFAULT 'student',
                first_seen DATETIME,
                last_active DATETIME
            )
        """)
        conn.commit()


def seed_data():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        for admin_id in ADMIN_IDS:
            cursor.execute("""
                INSERT OR IGNORE INTO users (user_id, username, first_name, role, first_seen, last_active)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (admin_id, "admin", "Администратор", "admin", datetime.now(), datetime.now()))
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


def user_exists(user_id: int) -> bool:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone()[0] > 0


def get_user_role(user_id: int) -> str:
    if user_id in ADMIN_IDS:
        return "admin"
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return result[0] if result else "student"


def set_user_role(user_id: int, role: str):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
        conn.commit()


def register_user(user_id: int, username: str, first_name: str = "", last_name: str = "", role: str = "student"):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
        existing = cursor.fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET last_active = ?, username = ?, first_name = ?, last_name = ? WHERE user_id = ?",
                (datetime.now(), username, first_name, last_name, user_id),
            )
        else:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, last_name, role, first_seen, last_active) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, first_name, last_name, role, datetime.now(), datetime.now()),
            )
        conn.commit()


def save_review_sync(user_id: int, username: str, rating: int, comment: str):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            "INSERT INTO reviews (user_id, username, rating, comment, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, rating, comment, datetime.now()),
        )
        conn.commit()


def get_stats_sync():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT rating, comment, timestamp, username FROM reviews ORDER BY timestamp DESC")
        rows = cursor.fetchall()
        if not rows:
            return None
        ratings = [r[0] for r in rows]
        comments = [(r[1], r[2], r[3]) for r in rows if r[1]]
        distribution = defaultdict(int)
        for r in ratings:
            distribution[r] += 1
        week_ago = datetime.now() - timedelta(days=7)
        recent = [r for r in rows if datetime.fromisoformat(str(r[2])) >= week_ago]
        return {
            "count": len(ratings),
            "avg": round(sum(ratings) / len(ratings), 2),
            "comments": comments[:5],
            "distribution": dict(distribution),
            "recent_count": len(recent),
        }


def get_all_user_ids():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]


def export_reviews_csv():
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

def get_main_menu(role: str = "student"):
    if role in ("teacher", "admin"):
        return ReplyKeyboardMarkup(
            [
                ["📝 Оставить отзыв"],
                ["📊 Отчёт", "📁 Экспорт CSV"],
                ["📢 Рассылка", "👥 Пользователи"],
                ["👤 Профиль", "🔄 Регистрация"],
                ["ℹ️ Помощь"],
            ],
            resize_keyboard=True,
        )
    return ReplyKeyboardMarkup(
        [
            ["📝 Оставить отзыв"],
            ["👤 Профиль", "🔄 Регистрация"],
            ["ℹ️ Помощь"],
        ],
        resize_keyboard=True,
    )


def stars(rating: int) -> str:
    return "⭐" * rating + "☆" * (5 - rating)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or get_user_role(user_id) == "admin"


def is_teacher_or_admin(user_id: int) -> bool:
    return get_user_role(user_id) in ("teacher", "admin") or user_id in ADMIN_IDS


# --- 4. КОМАНДЫ И КНОПКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user_exists(user.id):
        await update.message.reply_text(
            f"👋 Добро пожаловать, {user.first_name}!\n\n"
            "Для начала нужно зарегистрироваться — выберите вашу роль:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👨‍🎓 Я студент", callback_data="role_student")],
                [InlineKeyboardButton("👨‍🏫 Я преподаватель", callback_data="role_teacher")],
            ]),
        )
    else:
        role = get_user_role(user.id)
        register_user(user.id, user.username or "", user.first_name or "", user.last_name or "", role)
        role_text = {"admin": "администратор 👑", "teacher": "преподаватель 👨‍🏫"}.get(role, "студент 👨‍🎓")
        await update.message.reply_text(
            f"С возвращением, {user.first_name}! 👋\nВаша роль: {role_text}",
            reply_markup=get_main_menu(role),
        )


# --- Регистрация (callback) ---

async def handle_role_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if query.data == "role_student":
        register_user(user.id, user.username or "", user.first_name or "", user.last_name or "", "student")
        await query.edit_message_text(
            "✅ Вы зарегистрированы как студент 👨‍🎓\n\nНажмите «📝 Оставить отзыв» чтобы начать."
        )
        await query.message.reply_text("Выберите действие:", reply_markup=get_main_menu("student"))

    elif query.data == "role_teacher":
        await query.edit_message_text(
            "🔐 Введите код преподавателя в ответном сообщении.\n"
            "Если нет кода — обратитесь к администратору.\n\n"
            "Для отмены отправьте /cancel"
        )
        context.user_data["awaiting_teacher_code"] = True

    elif query.data == "cancel_registration":
        await query.edit_message_text("❌ Регистрация отменена.")


async def handle_teacher_code_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватывает код преподавателя из обычного текста (вне ConversationHandler)."""
    if not context.user_data.get("awaiting_teacher_code"):
        return  # не наш случай

    user = update.effective_user
    entered = update.message.text.strip()

    if entered == TEACHER_CODE:
        register_user(user.id, user.username or "", user.first_name or "", user.last_name or "", "teacher")
        context.user_data["awaiting_teacher_code"] = False
        await update.message.reply_text(
            "✅ Вы зарегистрированы как преподаватель 👨‍🏫\n\n"
            "Теперь доступны: отчёт, экспорт CSV, рассылка, список пользователей.",
            reply_markup=get_main_menu("teacher"),
        )
    else:
        await update.message.reply_text(
            "❌ Неверный код. Попробуйте ещё раз или отправьте /cancel для отмены."
        )


async def registration_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 🔄 Регистрация в меню."""
    user = update.effective_user
    role = get_user_role(user.id) if user_exists(user.id) else "student"
    role_text = {"admin": "администратор 👑", "teacher": "преподаватель 👨‍🏫"}.get(role, "студент 👨‍🎓")
    await update.message.reply_text(
        f"Текущая роль: *{role_text}*\n\nВыберите новую роль:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👨‍🎓 Студент", callback_data="role_student")],
            [InlineKeyboardButton("👨‍🏫 Преподаватель", callback_data="role_teacher")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_registration")],
        ]),
    )


# --- Профиль ---

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user_exists(user.id):
        await update.message.reply_text(
            "❌ Вы не зарегистрированы. Отправьте /start для регистрации."
        )
        return

    role = get_user_role(user.id)
    role_emoji = {"admin": "👑", "teacher": "👨‍🏫", "student": "👨‍🎓"}.get(role, "❓")
    role_text = {"admin": "Администратор", "teacher": "Преподаватель", "student": "Студент"}.get(role, "Неизвестно")

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM reviews WHERE user_id = ?", (user.id,))
        reviews_count = cursor.fetchone()[0]
        cursor.execute("SELECT AVG(rating) FROM reviews WHERE user_id = ?", (user.id,))
        avg_row = cursor.fetchone()[0]
        cursor.execute("SELECT first_seen FROM users WHERE user_id = ?", (user.id,))
        first_seen = cursor.fetchone()[0]

    avg_text = f"{avg_row:.1f}" if avg_row else "нет отзывов"
    date_text = str(first_seen)[:10] if first_seen else "неизвестно"

    text = (
        f"{role_emoji} *Ваш профиль*\n\n"
        f"• Имя: {user.first_name} {user.last_name or ''}\n"
        f"• Username: @{user.username or 'нет'}\n"
        f"• ID: `{user.id}`\n"
        f"• Роль: {role_text}\n"
        f"• В системе с: {date_text}\n"
        f"• Оставлено отзывов: {reviews_count}\n"
        f"• Средняя оценка: {avg_text}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_menu(role))


# --- Пользователи ---

async def manage_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_teacher_or_admin(user_id):
        await update.message.reply_text("⛔ Доступ только для преподавателей.")
        return

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.user_id, u.username, u.first_name, u.role,
                   COUNT(r.id) as review_count
            FROM users u
            LEFT JOIN reviews r ON u.user_id = r.user_id
            GROUP BY u.user_id
            ORDER BY u.role, u.first_seen DESC
        """)
        users = cursor.fetchall()

    if not users:
        await update.message.reply_text("Нет зарегистрированных пользователей.")
        return

    students = [u for u in users if u[3] == "student"]
    teachers = [u for u in users if u[3] in ("teacher", "admin")]

    text = f"👥 *Пользователи (всего: {len(users)})*\n\n"

    if teachers:
        text += "*Преподаватели/Админы:*\n"
        for uid, uname, fname, role, rcnt in teachers:
            icon = "👑" if role == "admin" else "👨‍🏫"
            text += f"{icon} {fname or 'без имени'} (@{uname or 'нет'})\n"
        text += "\n"

    if students:
        text += f"*Студенты ({len(students)}):*\n"
        for uid, uname, fname, role, rcnt in students[:20]:
            rev = f" ({rcnt} отз.)" if rcnt > 0 else ""
            text += f"• {fname or 'без имени'} (@{uname or 'нет'}){rev}\n"
        if len(students) > 20:
            text += f"...и ещё {len(students) - 20} студентов\n"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_menu(get_user_role(user_id)))


# --- Помощь ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    role = get_user_role(user_id)

    if is_teacher_or_admin(user_id):
        text = (
            "📖 *Справка для преподавателя*\n\n"
            "📝 *Оставить отзыв* — оценить лекцию\n"
            "📊 *Отчёт* — статистика всех отзывов\n"
            "📁 *Экспорт CSV* — скачать данные в Excel\n"
            "📢 *Рассылка* — сообщение всем пользователям\n"
            "👥 *Пользователи* — список всех в системе\n"
            "👤 *Профиль* — ваши данные и статистика\n"
            "🔄 *Регистрация* — сменить роль\n\n"
            f"🔑 Код для преподавателей: `{TEACHER_CODE}`"
        )
    else:
        text = (
            "📖 *Справка для студента*\n\n"
            "📝 *Оставить отзыв* — оценить лекцию\n"
            "👤 *Профиль* — ваши данные\n"
            "🔄 *Регистрация* — сменить роль\n\n"
            "💡 Один отзыв в день — возвращайтесь после каждой лекции!"
        )

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_menu(role))


# --- Диалог: Отзыв ---

async def start_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not user_exists(user_id):
        await update.message.reply_text("❌ Сначала зарегистрируйтесь — отправьте /start")
        return ConversationHandler.END

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        today = datetime.now().date().isoformat()
        cursor.execute(
            "SELECT COUNT(*) FROM reviews WHERE user_id = ? AND DATE(timestamp) = ?",
            (user_id, today),
        )
        if cursor.fetchone()[0] >= 1:
            await update.message.reply_text(
                "⚠️ Вы уже оставили отзыв сегодня. Возвращайтесь после следующей лекции!",
                reply_markup=get_main_menu(get_user_role(user_id)),
            )
            return ConversationHandler.END

    await update.message.reply_text(
        "Оцените лекцию от 1 до 5:\n\n"
        "1 ⭐ — очень плохо\n2 ⭐⭐ — плохо\n3 ⭐⭐⭐ — нормально\n4 ⭐⭐⭐⭐ — хорошо\n5 ⭐⭐⭐⭐⭐ — отлично",
        reply_markup=ReplyKeyboardMarkup([["1", "2", "3", "4", "5"], ["❌ Отмена"]], resize_keyboard=True),
    )
    return GET_RATING


async def receive_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    try:
        rating = int(update.message.text)
        if not 1 <= rating <= 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Пожалуйста, нажмите одну из кнопок 1–5.")
        return GET_RATING
    context.user_data["rating"] = rating
    await update.message.reply_text(
        f"Вы поставили {stars(rating)}\n\nНапишите комментарий или пропустите:",
        reply_markup=ReplyKeyboardMarkup([["Без комментария"], ["❌ Отмена"]], resize_keyboard=True),
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
        reply_markup=get_main_menu(get_user_role(user.id)),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_teacher_code", None)
    user_id = update.effective_user.id
    role = get_user_role(user_id) if user_exists(user_id) else "student"
    await update.message.reply_text("Действие отменено.", reply_markup=get_main_menu(role))
    return ConversationHandler.END


# --- Отчёт ---

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_teacher_or_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ только для преподавателей.")
        return
    s = get_stats_sync()
    if not s:
        await update.message.reply_text("Отзывов пока нет.")
        return
    dist_text = ""
    for star in range(5, 0, -1):
        cnt = s["distribution"].get(star, 0)
        dist_text += f"{star}⭐ {'█' * cnt} ({cnt})\n"
    text = (
        f"📊 *ОТЧЁТ ПО ОТЗЫВАМ*\n\n"
        f"Всего: *{s['count']}*\n"
        f"Средняя оценка: *{s['avg']} / 5.0*\n"
        f"За 7 дней: *{s['recent_count']}*\n\n"
        f"*Распределение:*\n{dist_text}\n"
        f"*Последние комментарии:*\n"
    )
    for comment, ts, username in s["comments"]:
        if comment:
            text += f"• @{username} ({str(ts)[:10]}): _{comment}_\n"
    await update.message.reply_text(text, parse_mode="Markdown")


# --- Экспорт CSV ---

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_teacher_or_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ только для преподавателей.")
        return
    csv_data = export_reviews_csv()
    filename = f"reviews_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    await update.message.reply_document(
        document=InputFile(io.BytesIO(csv_data.encode("utf-8-sig")), filename=filename),
        caption=f"📁 Экспорт отзывов — {datetime.now().strftime('%d.%m.%Y %H:%M')}",
    )


# --- Рассылка ---

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_teacher_or_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ только для преподавателей.")
        return ConversationHandler.END
    user_ids = get_all_user_ids()
    await update.message.reply_text(
        f"📢 Введите текст рассылки.\nБудет отправлено {len(user_ids)} пользователям.\n\nОтмена — ❌ Отмена:",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return BROADCAST_TEXT


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)
    message_text = update.message.text
    sent, failed = 0, 0
    for uid in get_all_user_ids():
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 *Сообщение от преподавателя:*\n\n{message_text}",
                parse_mode="Markdown",
            )
            sent += 1
        except Exception as e:
            logger.warning("Ошибка рассылки %s: %s", uid, e)
            failed += 1
    await update.message.reply_text(
        f"✅ Готово! Отправлено: {sent}, ошибок: {failed}",
        reply_markup=get_main_menu(get_user_role(update.effective_user.id)),
    )
    return ConversationHandler.END


# --- 5. ЗАПУСК ---

def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан!")

    init_db()
    seed_data()

    app = Application.builder().token(TOKEN).build()

    # Диалог: отзыв
    feedback_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📝 Оставить отзыв$"), start_feedback)],
        states={
            GET_RATING: [MessageHandler(filters.Regex("^[1-5]$"), receive_rating)],
            GET_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comment)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
            CommandHandler("cancel", cancel),
        ],
    )

    # Диалог: рассылка
    broadcast_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📢 Рассылка$"), start_broadcast)],
        states={
            BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_broadcast)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
            CommandHandler("cancel", cancel),
        ],
    )

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))

    # Callback кнопки (inline)
    app.add_handler(CallbackQueryHandler(handle_role_selection, pattern="^role_|^cancel_registration$"))

    # Кнопки меню — используем Regex для точного совпадения с эмодзи
    app.add_handler(MessageHandler(filters.Regex("^📊 Отчёт$"), show_report))
    app.add_handler(MessageHandler(filters.Regex("^📁 Экспорт CSV$"), export_csv))
    app.add_handler(MessageHandler(filters.Regex("^👤 Профиль$"), profile))
    app.add_handler(MessageHandler(filters.Regex("^👥 Пользователи$"), manage_users))
    app.add_handler(MessageHandler(filters.Regex("^🔄 Регистрация$"), registration_menu))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ Помощь$"), help_command))

    # Диалоги
    app.add_handler(feedback_handler)
    app.add_handler(broadcast_handler)

    # Перехват кода преподавателя (должен быть последним)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_teacher_code_message))

    logger.info("Бот запущен! Код преподавателя: %s", TEACHER_CODE)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
