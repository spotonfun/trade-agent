# pip install python-telegram-bot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler

# Zatwierdzenie jednym kliknięciem w Telegramie
keyboard = [[
    InlineKeyboardButton("ZATWIERDŹ", callback_data=f"approve_{ticker}"),
    InlineKeyboardButton("ODRZUĆ",    callback_data=f"reject_{ticker}"),
]]
reply_markup = InlineKeyboardMarkup(keyboard)
await bot.send_message(chat_id, tekst, reply_markup=reply_markup)