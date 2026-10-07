import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "tea_shop.db")

logging.basicConfig(level=logging.INFO)

dp = Dispatcher()

CATEGORIES = [
    ("pu_er_shu", "🍂 Шу пуэр"),
    ("pu_er_sheng", "🌿 Шэн пуэр"),
    ("oolong", "🪨 Улуны"),
    ("red", "🔴 Красный чай"),
    ("gaba", "🌱 Габа"),
    ("white", "🤍 Белый чай"),
    ("sets", "🎁 Наборы"),
]

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            phone TEXT,
            address TEXT,
            bonus_balance INTEGER DEFAULT 0,
            spent INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price INTEGER NOT NULL,
            weight TEXT DEFAULT '50 г',
            photo_id TEXT,
            active INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cart (
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, product_id)
        )
    """)
    conn.commit()
    conn.close()

def register_user(message: Message):
    user = message.from_user
    conn = db()
    conn.execute("""
        INSERT INTO users (telegram_id, name, username, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            name=excluded.name,
            username=excluded.username
    """, (
        user.id,
        user.full_name,
        user.username or "",
        datetime.now().isoformat(timespec="seconds"),
    ))
    conn.commit()
    conn.close()

def main_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="🍃 Выбрать чай")
    builder.button(text="🛒 Корзина")
    builder.button(text="❤️ Избранное")
    builder.button(text="👤 Профиль")
    builder.button(text="📦 Мои заказы")
    builder.button(text="🎁 Бонусы")
    builder.button(text="💬 Поддержка")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True)

def category_keyboard():
    rows = []
    for key, title in CATEGORIES:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"cat:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(Command("start"))
async def start(message: Message):
    register_user(message)
    await message.answer(
        "🍵 <b>Чайная лавка дяди Лёши</b>\n\n"
        "Китайский чай без лишнего пафоса.\n\n"
        "Выбирай чай, собирай корзину и оформляй заказ прямо здесь.",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )

@dp.message(Command("catalog"))
@dp.message(F.text == "🍃 Выбрать чай")
async def catalog(message: Message):
    await message.answer(
        "🍃 <b>Выберите категорию</b>",
        reply_markup=category_keyboard(),
        parse_mode="HTML",
    )

@dp.callback_query(F.data.startswith("cat:"))
async def category(callback: CallbackQuery):
    category_key = callback.data.split(":", 1)[1]
    category_name = dict(CATEGORIES).get(category_key, "Чай")
    conn = db()
    products = conn.execute(
        "SELECT * FROM products WHERE category=? AND active=1 ORDER BY id",
        (category_key,),
    ).fetchall()
    conn.close()

    if not products:
        await callback.message.edit_text(
            f"{category_name}\n\nПока здесь ничего нет. "
            "Товары добавим следующим этапом.",
            reply_markup=category_keyboard(),
        )
        await callback.answer()
        return

    for p in products:
        text = (
            f"<b>{p['name']}</b>\n"
            f"{p['description']}\n\n"
            f"⚖️ {p['weight']}\n"
            f"💰 {p['price']:,} ₽".replace(",", " ")
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Добавить в корзину", callback_data=f"add:{p['id']}")],
        ])
        if p["photo_id"]:
            await callback.message.answer_photo(p["photo_id"], caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")

    await callback.answer()

@dp.callback_query(F.data.startswith("add:"))
async def add_to_cart(callback: CallbackQuery):
    product_id = int(callback.data.split(":", 1)[1])
    conn = db()
    conn.execute("""
        INSERT INTO cart (user_id, product_id, quantity)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, product_id)
        DO UPDATE SET quantity = quantity + 1
    """, (callback.from_user.id, product_id))
    conn.commit()
    conn.close()
    await callback.answer("Добавлено в корзину 🛒")

@dp.message(Command("cart"))
@dp.message(F.text == "🛒 Корзина")
async def cart(message: Message):
    conn = db()
    rows = conn.execute("""
        SELECT p.name, p.price, c.quantity
        FROM cart c JOIN products p ON p.id=c.product_id
        WHERE c.user_id=?
    """, (message.from_user.id,)).fetchall()
    conn.close()

    if not rows:
        await message.answer("🛒 Корзина пока пустая.", reply_markup=main_menu())
        return

    total = sum(r["price"] * r["quantity"] for r in rows)
    lines = ["🛒 <b>Ваша корзина</b>\n"]
    for r in rows:
        lines.append(f"• {r['name']} × {r['quantity']} — {r['price'] * r['quantity']:,} ₽".replace(",", " "))
    lines.append(f"\n<b>Итого: {total:,} ₽</b>".replace(",", " "))
    lines.append("\nОформление заказа подключим следующим этапом.")

    await message.answer("\n".join(lines), reply_markup=main_menu(), parse_mode="HTML")

@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    register_user(message)
    conn = db()
    user = conn.execute(
        "SELECT * FROM users WHERE telegram_id=?", (message.from_user.id,)
    ).fetchone()
    conn.close()
    await message.answer(
        f"👤 <b>Ваш профиль</b>\n\n"
        f"Имя: {user['name']}\n"
        f"📱 Телефон: {user['phone'] or '—'}\n"
        f"📍 Адрес: {user['address'] or '—'}\n"
        f"🎁 Бонусный баланс: <b>{user['bonus_balance']} ₽</b>\n\n"
        f"💳 <b>Накопительная карта</b>\n"
        f"Уровень: 🌱 Новичок\n"
        f"Кэшбэк: 5%\n"
        f"Потрачено: {user['spent']:,} ₽".replace(",", " "),
        reply_markup=main_menu(),
        parse_mode="HTML",
    )

@dp.message(Command("orders"))
@dp.message(F.text == "📦 Мои заказы")
async def orders(message: Message):
    await message.answer(
        "📦 <b>Мои заказы</b>\n\n"
        "История заказов появится здесь после подключения оформления заказа.",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )

@dp.message(F.text == "🎁 Бонусы")
async def bonuses(message: Message):
    await message.answer(
        "🎁 <b>Бонусная программа</b>\n\n"
        "На первом этапе: 5% кэшбэка после получения заказа.\n"
        "Позже добавим уровни, реферальную программу и персональные бонусы.",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )

@dp.message(F.text == "❤️ Избранное")
async def favorites(message: Message):
    await message.answer(
        "❤️ Избранное пока пустое.\n\n"
        "Эту функцию подключим вместе с каталогом.",
        reply_markup=main_menu(),
    )

@dp.message(F.text == "💬 Поддержка")
async def support(message: Message):
    await message.answer(
        "💬 <b>Поддержка</b>\n\n"
        "Напишите сюда свой вопрос — на следующем этапе подключим связь с Лёшей.",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Добавьте его в переменные окружения.")
    init_db()
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
