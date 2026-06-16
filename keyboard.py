"""
Модуль с клавиатурами для Telegram бота
"""
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

def get_main_keyboard():
    """Главная клавиатура"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="👤 Моя статистика")],
            [KeyboardButton(text="🏆 Топ"), KeyboardButton(text="📁 Экспорт")],
            [KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )

def get_stats_inline_keyboard():
    """Инлайн клавиатура для статистики"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Общая статистика", callback_data="stats_all")],
            [InlineKeyboardButton(text="👤 Моя статистика", callback_data="stats_me")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
        ]
    )