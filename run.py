import asyncio
import os
import re
import io
import sqlite3
import httpx
import openpyxl
import PIL.Image
import aiohttp

from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, PhotoSize, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile
)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType, ChatMemberStatus
from dotenv import load_dotenv
import pytesseract
from collections import defaultdict

# ВАШ ПУТЬ К TESSERACT
import shutil
tesseract_path = shutil.which("tesseract")
if tesseract_path:
    pytesseract.pytesseract.tesseract_cmd = tesseract_path

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

DB_FILE = "checks.db"

# ============ БАЗА ДАННЫХ (SQLite, отдельные данные по chat_id) ============

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            chat_title TEXT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            year_month TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def save_check(chat_id, chat_title, user_id, username, amount):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    now = datetime.now()
    cur.execute("""
        INSERT INTO checks (chat_id, chat_title, user_id, username, amount, created_at, year, month, year_month)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        chat_id, chat_title, user_id, username, amount,
        now.strftime("%Y-%m-%d %H:%M:%S"), now.year, now.month, now.strftime("%Y-%m")
    ))
    conn.commit()
    conn.close()

def get_user_stats(chat_id):
    """Статистика по пользователям ВНУТРИ конкретной группы"""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT username, amount, created_at FROM checks WHERE chat_id = ?
    """, (chat_id,))
    rows = cur.fetchall()
    conn.close()

    stats = defaultdict(lambda: {"count": 0, "total": 0.0, "first_seen": None, "last_seen": None})
    for username, amount, created_at in rows:
        s = stats[username]
        s["count"] += 1
        s["total"] += amount
        if s["first_seen"] is None or created_at < s["first_seen"]:
            s["first_seen"] = created_at
        if s["last_seen"] is None or created_at > s["last_seen"]:
            s["last_seen"] = created_at
    return stats

def get_monthly_stats(chat_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT year_month, amount FROM checks WHERE chat_id = ?
    """, (chat_id,))
    rows = cur.fetchall()
    conn.close()

    monthly = defaultdict(lambda: {"total": 0.0, "count": 0})
    for year_month, amount in rows:
        monthly[year_month]["total"] += amount
        monthly[year_month]["count"] += 1
    return dict(monthly)

def get_all_checks(chat_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT created_at, username, amount FROM checks WHERE chat_id = ? ORDER BY created_at
    """, (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

# ============ РАСПОЗНАВАНИЕ ============

def extract_amount_from_text(text):
    keyword_patterns = [
        r'(?:СУММА|ИТОГ|ВСЕГО|К ОПЛАТЕ|Получателю зачислится)\s*:?\s*(\d{1,3}(?:[\s.]?\d{3})*(?:[.,]\d{2})?)',
        r'(\d{1,3}(?:[\s.]?\d{3})*(?:[.,]\d{2})?)\s*(?:СУМ|РУБ|₽|\$)',
    ]
    for pattern in keyword_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            amount_raw = match if isinstance(match, str) else match[0]
            amount_clean = amount_raw.replace(" ", "").replace(",", ".")
            amount_clean = re.sub(r"\.00$", "", amount_clean)
            try:
                amount_float = float(amount_clean)
                if 1000 <= amount_float <= 10000000:
                    return amount_float
            except:
                pass

    all_numbers = re.findall(r'(\d{1,3}(?:[\s]?\d{3})*(?:[.,]\d{2})?)', text)
    candidates = []
    for num in all_numbers:
        num_clean = num.replace(" ", "").replace(",", ".")
        try:
            num_float = float(num_clean)
            if 1000 <= num_float <= 10000000 and len(str(int(num_float))) <= 8:
                candidates.append(num_float)
        except:
            pass

    if candidates:
        return min(candidates)
    return None

# ============ ПРОВЕРКА АДМИНА ============

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        return False

# ============ КЛАВИАТУРЫ ============

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="👤 Моя статистика")],
            [KeyboardButton(text="🏆 Топ"), KeyboardButton(text="📁 Экспорт")],
            [KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )

def get_stats_inline_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Общая статистика", callback_data="stats_all")],
            [InlineKeyboardButton(text="👤 Моя статистика", callback_data="stats_me")],
            [InlineKeyboardButton(text="📅 По месяцам", callback_data="stats_monthly")],
        ]
    )

# ============ БОТ ============

class FlexibleSession(AiohttpSession):
    async def create_session(self):
        disable_ssl = os.getenv("DISABLE_SSL_VERIFY", "false").lower() == "true"
        if disable_ssl:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)
            return self._session
        return await super().create_session()

dp = Dispatcher()

def is_group(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

@dp.message(F.photo)
async def handle_photo(message: Message):
    if not is_group(message):
        return

    try:
        photo: PhotoSize = message.photo[-1]
        bot = message.bot
        file = await bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"

        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            response = await client.get(file_url)
            image_data = response.content

        img = PIL.Image.open(io.BytesIO(image_data))

        width, height = img.size
        if width < 1200:
            new_height = int(height * 1200 / width)
            img = img.resize((1200, new_height), PIL.Image.Resampling.LANCZOS)

        text = pytesseract.image_to_string(img, lang='rus+eng')
        amount = extract_amount_from_text(text)

        if amount:
            username = message.from_user.full_name
            save_check(message.chat.id, message.chat.title, message.from_user.id, username, amount)
            await message.reply(f"✅ Сохранено: {amount:,.0f} сум")
        else:
            # Тихо пропускаем, если не нашли сумму (не спамим в группе)
            pass

    except Exception as e:
        # Тихо логируем ошибку, не спамим в группе
        print(f"Ошибка обработки фото: {e}")

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    if not is_group(message):
        return
    stats = get_user_stats(message.chat.id)
    if not stats:
        await message.reply("📊 Пока нет данных для этой группы!", reply_markup=get_main_keyboard())
        return

    sorted_users = sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True)
    response = "📊 **СТАТИСТИКА ГРУППЫ**\n\n"
    response += f"👥 Участников с чеками: {len(stats)}\n"
    response += f"💰 Общая сумма: {sum(s['total'] for s in stats.values()):,.0f} сум\n"
    response += f"📝 Всего чеков: {sum(s['count'] for s in stats.values())}\n\n🏆 **ТОП:**\n"
    for i, (username, data) in enumerate(sorted_users[:10], 1):
        response += f"\n{i}. **{username}**\n   📄 {data['count']} чеков | 💰 {data['total']:,.0f} сум\n"

    await message.reply(response, parse_mode="Markdown", reply_markup=get_stats_inline_keyboard())

@dp.message(F.text == "👤 Моя статистика")
async def show_my_stats(message: Message):
    if not is_group(message):
        return
    stats = get_user_stats(message.chat.id)
    username = message.from_user.full_name
    if username not in stats:
        await message.reply("📊 У вас пока нет чеков в этой группе!", reply_markup=get_main_keyboard())
        return
    data = stats[username]
    response = (
        f"📊 **ВАША СТАТИСТИКА**\n\n"
        f"📄 Чеков: {data['count']}\n"
        f"💰 Сумма: {data['total']:,.0f} сум\n"
        f"📅 Первый: {data['first_seen']}\n"
        f"📅 Последний: {data['last_seen']}\n"
        f"💵 Средний: {data['total']/data['count']:,.0f} сум"
    )
    await message.reply(response, parse_mode="Markdown", reply_markup=get_main_keyboard())

@dp.message(F.text == "🏆 Топ")
async def show_top(message: Message):
    if not is_group(message):
        return
    stats = get_user_stats(message.chat.id)
    if not stats:
        await message.reply("🏆 Пока нет данных!", reply_markup=get_main_keyboard())
        return
    sorted_users = sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True)
    response = "🏆 **ТОП ГРУППЫ**\n\n"
    for i, (username, data) in enumerate(sorted_users[:10], 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        response += f"{medal} **{username}**\n   💰 {data['total']:,.0f} сум ({data['count']} чеков)\n\n"
    await message.reply(response, parse_mode="Markdown", reply_markup=get_main_keyboard())

@dp.message(F.text == "📁 Экспорт")
async def export_stats(message: Message):
    if not is_group(message):
        return

    if not await is_admin(message.bot, message.chat.id, message.from_user.id):
        await message.reply("⛔ Экспорт доступен только администраторам группы.", reply_markup=get_main_keyboard())
        return

    rows = get_all_checks(message.chat.id)
    if not rows:
        await message.reply("📊 Нет данных для экспорта!", reply_markup=get_main_keyboard())
        return

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Чеки"
    ws.append(["Дата", "Пользователь", "Сумма"])
    for created_at, username, amount in rows:
        ws.append([created_at, username, amount])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    chat_title = (message.chat.title or "group").replace(" ", "_")
    filename = f"{chat_title}_{datetime.now().strftime('%Y%m%d')}.xlsx"

    await message.reply_document(
        BufferedInputFile(buf.read(), filename=filename),
        caption=f"📊 Экспорт чеков группы «{message.chat.title}»",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "❓ Помощь")
async def show_help(message: Message):
    if not is_group(message):
        return
    help_text = (
        "🤖 **Доступные команды:**\n\n"
        "📸 Отправьте фото чека — распознаю сумму и сохраню\n"
        "📊 Статистика — общая статистика группы\n"
        "👤 Моя статистика — ваша личная статистика\n"
        "🏆 Топ — рейтинг участников\n"
        "📁 Экспорт — выгрузка в Excel (только для админов)\n"
    )
    await message.reply(help_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id

    if callback.data == "stats_all":
        stats = get_user_stats(chat_id)
        if not stats:
            await callback.message.edit_text("📊 Нет данных!")
            return
        sorted_users = sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True)
        response = "📊 **ОБЩАЯ СТАТИСТИКА ГРУППЫ**\n\n"
        response += f"👥 Участников: {len(stats)}\n💰 Сумма: {sum(s['total'] for s in stats.values()):,.0f} сум\n\n🏆 **ТОП-10:**\n"
        for i, (username, data) in enumerate(sorted_users[:10], 1):
            response += f"{i}. {username[:20]}: {data['total']:,.0f} сум\n"
        await callback.message.edit_text(response, parse_mode="Markdown", reply_markup=get_stats_inline_keyboard())

    elif callback.data == "stats_me":
        stats = get_user_stats(chat_id)
        username = callback.from_user.full_name
        if username not in stats:
            await callback.message.edit_text("📊 У вас нет данных в этой группе!", reply_markup=get_stats_inline_keyboard())
            return
        data = stats[username]
        response = f"📊 **ВАША СТАТИСТИКА**\n\n📄 Чеков: {data['count']}\n💰 Сумма: {data['total']:,.0f} сум\n💵 Средний: {data['total']/data['count']:,.0f} сум"
        await callback.message.edit_text(response, parse_mode="Markdown", reply_markup=get_stats_inline_keyboard())

    elif callback.data == "stats_monthly":
        monthly = get_monthly_stats(chat_id)
        if not monthly:
            await callback.message.edit_text("📅 Нет данных по месяцам!")
            return
        response = "📅 **СТАТИСТИКА ПО МЕСЯЦАМ**\n\n"
        for month in sorted(monthly.keys(), reverse=True):
            data = monthly[month]
            response += f"• **{month}**: {data['total']:,.0f} сум ({data['count']} чеков)\n"
        await callback.message.edit_text(response, parse_mode="Markdown", reply_markup=get_stats_inline_keyboard())

@dp.message()
async def handle_other(message: Message):
    if not is_group(message):
        return
    # Тихо игнорируем все остальные сообщения в группе

async def main():
    init_db()

    try:
        version = pytesseract.get_tesseract_version()
        print(f"✅ Tesseract найден! Версия: {version}")
    except Exception as e:
        print(f"❌ Tesseract не найден! {e}")
        return

    session = NoSSLSession()
    bot = Bot(token=BOT_TOKEN, session=session)

    print("✅ Бот запущен! Работает только в группах.")
    print("📱 Бот отвечает только на фото и команды из меню")

    try:
        await dp.start_polling(bot)
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())