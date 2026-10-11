import asyncio
import csv
import logging
import json
import os
import sqlite3
from pathlib import Path
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "tea_shop.db")
CATALOG_PATH = os.getenv("CATALOG_PATH", "catalog.csv")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

# Кнопки категорий сопоставлены с названиями категорий в общем каталоге.
CATEGORY_ALIASES = {
    "Шу пуэр": ["шу пуэр", "шу", "pu_er_shu"],
    "Шэн пуэр": ["шэн пуэр", "шен пуэр", "шэн", "шен", "sheng", "pu_er_sheng"],
    "Улуны": ["улун", "улуны", "oolong"],
    "Красный чай": ["красный чай", "red"],
    "Габа": ["габа", "gaba"],
    "Белый чай": ["белый чай", "white"],
    "Наборы": ["набор", "sets", "set"],
    "Порционный чай": ["порционный чай", "portion"],
    "Зелёный чай": ["зелёный чай", "зеленый чай", "green"],
    "Посуда": ["посуда", "tableware", "teaware", "dishes"],
}

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍃 Выбрать чай"), KeyboardButton(text="🛒 Корзина")],
        [KeyboardButton(text="❤️ Избранное"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="🎁 Бонусы")],
        [KeyboardButton(text="💬 Поддержка")],
    ],
    resize_keyboard=True,
)

def norm(value):
    return " ".join(str(value or "").strip().lower().replace("ё", "е").split())

def get_col(row, *names):
    normalized = {norm(k): v for k, v in row.items() if k is not None}
    for name in names:
        if norm(name) in normalized:
            return (normalized[norm(name)] or "").strip()
    return ""

def load_catalog():
    path = Path(CATALOG_PATH)
    if not path.exists():
        logging.warning("Каталог не найден: %s", path.resolve())
        return []

    # Excel CSV обычно сохраняется либо в UTF-8, либо в UTF-8 с BOM.
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                sample = f.read(4096)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\\t")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.DictReader(f, dialect=dialect)
                products = []
                for index, row in enumerate(reader, start=1):
                    name = get_col(row, "name", "Название", "Наименование")
                    if not name:
                        continue
                    category = get_col(row, "category", "Категория")
                    price_raw = get_col(row, "price", "Цена ₽", "Цена", "Стоимость")
                    weight = get_col(row, "weight", "Вес", "Фасовка")
                    available_raw = get_col(row, "available", "Наличие", "В наличии")
                    show_raw = get_col(row, "show", "Показывать в боте", "Показывать")
                    description = get_col(row, "description", "Описание")
                    photo = get_col(row, "photo", "Фото", "image", "image_url")
                    product_id = get_col(row, "id", "ID", "Артикул") or str(index)

                    try:
                        price = int(float(price_raw.replace("₽", "").replace(" ", "").replace(",", ".")))
                    except (ValueError, AttributeError):
                        price = 0

                    available = norm(available_raw) in {"есть", "да", "1", "true", "в наличии", "yes", "available"}
                    show = norm(show_raw) in {"да", "есть", "1", "true", "yes", "показывать"}
                    # Если в файле нет столбца показа, товар показываем по умолчанию.
                    if not show_raw:
                        show = True

                    if show and price > 0:
                        products.append({
                            "id": product_id,
                            "category": category,
                            "name": name,
                            "price": price,
                            "weight": weight,
                            "available": available,
                            "description": description,
                            "photo": photo,
                        })
                logging.info("Загружено товаров из каталога: %s", len(products))
                return products
        except UnicodeDecodeError:
            continue
        except Exception:
            logging.exception("Не удалось прочитать каталог %s", path.resolve())
            return []
    logging.error("Не удалось определить кодировку catalog.csv")
    return []

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS cart (
                user_id INTEGER NOT NULL,
                product_id TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, product_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS checkout_sessions (
                user_id INTEGER PRIMARY KEY, step TEXT NOT NULL, name TEXT DEFAULT '',
                phone TEXT DEFAULT '', delivery TEXT DEFAULT '', address TEXT DEFAULT ''
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                name TEXT, phone TEXT, delivery TEXT, address TEXT, items_json TEXT,
                total INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'Новый',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

def products_for_category(category):
    products = load_catalog()
    aliases = CATEGORY_ALIASES.get(category, [norm(category)])
    return [
        p for p in products
        if any(alias in norm(p["category"]) for alias in aliases)
    ]

def category_keyboard():
    builder = InlineKeyboardBuilder()
    for category in CATEGORY_ALIASES:
        builder.button(text=category, callback_data=f"cat:{category}")
    builder.adjust(2)
    return builder.as_markup()

def product_keyboard(product):
    builder = InlineKeyboardBuilder()
    if product["available"]:
        builder.button(text="🛒 В корзину", callback_data=f"add:{product['id']}")
    else:
        builder.button(text="Нет в наличии", callback_data="unavailable")
    builder.button(text="⬅️ К категориям", callback_data="categories")
    builder.adjust(1)
    return builder.as_markup()

def format_product(product):
    text = f"<b>{escape(product['name'])}</b>\n"
    if product["weight"]:
        text += f"Вес: {escape(product['weight'])}\n"
    text += f"Цена: <b>{product['price']} ₽</b>\n"
    text += "Наличие: " + ("есть" if product["available"] else "нет") 
    if product["description"]:
        text += f"\n\n{escape(product['description'])}"
    return text

def all_visible_products():
    return load_catalog()

def find_product(product_id):
    for p in all_visible_products():
        if str(p["id"]) == str(product_id):
            return p
    return None

@dp.message(CommandStart())
async def start(message: Message):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO users(user_id, username, first_name) VALUES (?, ?, ?)",
            (message.from_user.id, message.from_user.username, message.from_user.first_name),
        )
    await message.answer(
        "🍵 <b>Чайная лавка дяди Лёши</b>\n"
        "Китайский чай без лишнего пафоса.\n"
        "Выбирай чай, собирай корзину и оформляй заказ прямо здесь.",
        reply_markup=MAIN_MENU,
        parse_mode="HTML",
    )

@dp.message(F.text == "🍃 Выбрать чай")
async def choose_tea(message: Message):
    await message.answer("Выбери категорию:", reply_markup=category_keyboard())

@dp.callback_query(F.data == "categories")
async def back_to_categories(callback: CallbackQuery):
    await callback.message.edit_text("Выбери категорию:", reply_markup=category_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("cat:"))
async def show_category(callback: CallbackQuery):
    category = callback.data.split(":", 1)[1]
    products = products_for_category(category)
    if not products:
        await callback.message.edit_text(
            f"В категории «{escape(category)}» пока нет товаров в каталоге.",
            reply_markup=category_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for p in products:
        status = "" if p["available"] else " — нет в наличии"
        builder.button(
            text=f"{p['name']} · {p['price']} ₽{status}"[:64],
            callback_data=f"product:{p['id']}",
        )
    builder.button(text="⬅️ К категориям", callback_data="categories")
    builder.adjust(1)
    await callback.message.edit_text(
        f"<b>{escape(category)}</b>\nВыбери товар:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("product:"))
async def show_product(callback: CallbackQuery):
    product_id = callback.data.split(":", 1)[1]
    product = find_product(product_id)
    if not product:
        await callback.answer("Товар не найден в каталоге. Обнови список.", show_alert=True)
        return
    caption = format_product(product)
    if product["photo"]:
        try:
            await callback.message.answer_photo(
                photo=product["photo"],
                caption=caption,
                reply_markup=product_keyboard(product),
                parse_mode="HTML",
            )
        except Exception:
            await callback.message.answer(caption, reply_markup=product_keyboard(product), parse_mode="HTML")
    else:
        await callback.message.answer(caption, reply_markup=product_keyboard(product), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "unavailable")
async def unavailable(callback: CallbackQuery):
    await callback.answer("Этого товара сейчас нет в наличии.", show_alert=True)

@dp.callback_query(F.data.startswith("add:"))
async def add_to_cart(callback: CallbackQuery):
    product_id = callback.data.split(":", 1)[1]
    product = find_product(product_id)
    if not product or not product["available"]:
        await callback.answer("Товара сейчас нет в наличии.", show_alert=True)
        return
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO cart(user_id, product_id, quantity) VALUES (?, ?, 1)
            ON CONFLICT(user_id, product_id) DO UPDATE SET quantity = quantity + 1
        """, (callback.from_user.id, product_id))
    await callback.answer("Добавлено в корзину 🛒")

@dp.message(F.text == "🛒 Корзина")
async def show_cart(message: Message):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT product_id, quantity FROM cart WHERE user_id = ?",
            (message.from_user.id,),
        ).fetchall()
    if not rows:
        await message.answer("Корзина пока пуста. Нажми «🍃 Выбрать чай», чтобы добавить товары.")
        return
    catalog = {str(p["id"]): p for p in all_visible_products()}
    lines, total = [], 0
    builder = InlineKeyboardBuilder()
    for product_id, quantity in rows:
        p = catalog.get(str(product_id))
        if not p:
            continue
        subtotal = p["price"] * quantity
        total += subtotal
        lines.append(f"• {escape(p['name'])} × {quantity} — {subtotal} ₽")
        builder.button(text=f"Убрать: {p['name']}"[:64], callback_data=f"remove:{product_id}")
    builder.button(text="✅ Оформить заказ", callback_data="checkout:begin")
    builder.adjust(1)
    lines.append(f"\n<b>Итого: {total} ₽</b>")
    await message.answer("\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("remove:"))
async def remove_from_cart(callback: CallbackQuery):
    product_id = callback.data.split(":", 1)[1]
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "DELETE FROM cart WHERE user_id = ? AND product_id = ?",
            (callback.from_user.id, product_id),
        )
    await callback.answer("Убрано из корзины")
    await callback.message.answer("Позиция удалена. Открой «🛒 Корзина», чтобы увидеть итог.")

@dp.callback_query(F.data == "checkout:begin")
async def checkout_begin(callback: CallbackQuery):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT product_id, quantity FROM cart WHERE user_id = ?", (callback.from_user.id,)).fetchall()
    catalog = {str(p["id"]): p for p in all_visible_products()}
    if not rows or not any(str(pid) in catalog for pid, _ in rows):
        await callback.answer("Корзина пуста", show_alert=True)
        return
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO checkout_sessions(user_id, step, name, phone, delivery, address) VALUES (?, 'name', '', '', '', '')", (callback.from_user.id,))
    await callback.message.answer("🧾 Оформление заказа\n\nКак к вам обращаться? Напишите имя.")
    await callback.answer()

@dp.callback_query(F.data.startswith("delivery:"))
async def checkout_delivery(callback: CallbackQuery):
    delivery = callback.data.split(":", 1)[1]
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT step FROM checkout_sessions WHERE user_id = ?", (callback.from_user.id,)).fetchone()
        if not row or row[0] != "delivery":
            await callback.answer("Начните оформление из корзины", show_alert=True)
            return
        con.execute("UPDATE checkout_sessions SET delivery = ?, step = 'address' WHERE user_id = ?", (delivery, callback.from_user.id))
    await callback.message.answer(f"Способ доставки: {escape(delivery)}.\n\nНапишите город, адрес или пункт выдачи. Если доставку нужно согласовать, напишите «Согласуем».", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "checkout:confirm")
async def checkout_confirm(callback: CallbackQuery):
    user_id = callback.from_user.id
    with sqlite3.connect(DB_PATH) as con:
        session = con.execute("SELECT name, phone, delivery, address, step FROM checkout_sessions WHERE user_id = ?", (user_id,)).fetchone()
        cart_rows = con.execute("SELECT product_id, quantity FROM cart WHERE user_id = ?", (user_id,)).fetchall()
    if not session or session[4] != "confirm":
        await callback.answer("Начните оформление заново из корзины", show_alert=True)
        return
    name, phone, delivery, address, _ = session
    catalog = {str(p["id"]): p for p in all_visible_products()}
    items, total = [], 0
    for pid, qty in cart_rows:
        product = catalog.get(str(pid))
        if product and product["available"]:
            subtotal = product["price"] * qty
            total += subtotal
            items.append({"id": str(pid), "name": product["name"], "quantity": qty, "price": product["price"], "subtotal": subtotal, "weight": product["weight"]})
    if not items:
        await callback.answer("В корзине нет доступных товаров", show_alert=True)
        return
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("INSERT INTO orders(user_id, name, phone, delivery, address, items_json, total) VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, name, phone, delivery, address, json.dumps(items, ensure_ascii=False), total))
        order_id = cur.lastrowid
        con.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
        con.execute("DELETE FROM checkout_sessions WHERE user_id = ?", (user_id,))
    item_lines = "\n".join(f"• {escape(item['name'])} × {item['quantity']} — {item['subtotal']} ₽" for item in items)
    await callback.message.answer(f"✅ <b>Заказ №{order_id} принят!</b>\n\n{item_lines}\n\n<b>Итого: {total} ₽</b>\nИмя: {escape(name)}\nТелефон: {escape(phone)}\nДоставка: {escape(delivery)}\nАдрес/пункт выдачи: {escape(address)}\n\nМы свяжемся с вами для подтверждения.", parse_mode="HTML", reply_markup=MAIN_MENU)
    if ADMIN_CHAT_ID:
        try:
            await callback.bot.send_message(int(ADMIN_CHAT_ID), f"🛎 <b>Новый заказ №{order_id}</b>\n\n{item_lines}\n\n<b>Итого: {total} ₽</b>\nИмя: {escape(name)}\nТелефон: {escape(phone)}\nTelegram: @{escape(callback.from_user.username or 'нет')} (ID {user_id})\nДоставка: {escape(delivery)}\nАдрес/пункт выдачи: {escape(address)}", parse_mode="HTML")
        except Exception:
            logging.exception("Не удалось отправить заказ владельцу")
    await callback.answer("Заказ оформлен")

@dp.message(lambda message: bool(message.from_user) and checkout_step(message.from_user.id) is not None)
async def checkout_text_step(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пожалуйста, отправьте ответ текстом.")
        return
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT step, name, phone, delivery, address FROM checkout_sessions WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return
    step, name, phone, delivery, address = row
    with sqlite3.connect(DB_PATH) as con:
        if step == "name":
            con.execute("UPDATE checkout_sessions SET name = ?, step = 'phone' WHERE user_id = ?", (text[:100], user_id))
            response = "Теперь напишите номер телефона для связи."
        elif step == "phone":
            if len(text) < 5:
                await message.answer("Похоже, номер слишком короткий. Напишите телефон ещё раз.")
                return
            con.execute("UPDATE checkout_sessions SET phone = ?, step = 'delivery' WHERE user_id = ?", (text[:50], user_id))
            response = None
        elif step == "address":
            con.execute("UPDATE checkout_sessions SET address = ?, step = 'confirm' WHERE user_id = ?", (text[:500], user_id))
            response = None
        else:
            return
    if step == "phone":
        builder = InlineKeyboardBuilder()
        for label in ("СДЭК", "Ozon", "Курьер", "Самовывоз", "Согласуем отдельно"):
            builder.button(text=label, callback_data=f"delivery:{label}")
        builder.adjust(2)
        await message.answer("Выберите способ доставки:", reply_markup=builder.as_markup())
    elif step == "address":
        with sqlite3.connect(DB_PATH) as con:
            current = con.execute("SELECT name, phone, delivery, address FROM checkout_sessions WHERE user_id = ?", (user_id,)).fetchone()
        name, phone, delivery, address = current
        with sqlite3.connect(DB_PATH) as con:
            cart_rows = con.execute("SELECT product_id, quantity FROM cart WHERE user_id = ?", (user_id,)).fetchall()
        catalog = {str(p["id"]): p for p in all_visible_products()}
        items, total = [], 0
        for pid, qty in cart_rows:
            product = catalog.get(str(pid))
            if product:
                subtotal = product["price"] * qty
                total += subtotal
                items.append(f"• {escape(product['name'])} × {qty} — {subtotal} ₽")
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Подтвердить заказ", callback_data="checkout:confirm")
        builder.button(text="❌ Отменить", callback_data="checkout:cancel")
        builder.adjust(1)
        await message.answer(f"<b>Проверьте заказ</b>\n\n" + "\n".join(items) + f"\n\n<b>Итого: {total} ₽</b>\nИмя: {escape(name)}\nТелефон: {escape(phone)}\nДоставка: {escape(delivery)}\nАдрес: {escape(address)}", reply_markup=builder.as_markup(), parse_mode="HTML")
    elif response:
        await message.answer(response)

@dp.callback_query(F.data == "checkout:cancel")
async def checkout_cancel(callback: CallbackQuery):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM checkout_sessions WHERE user_id = ?", (callback.from_user.id,))
    await callback.message.answer("Оформление отменено. Товары остались в корзине.", reply_markup=MAIN_MENU)
    await callback.answer()

def checkout_step(user_id):
    try:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute("SELECT step FROM checkout_sessions WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None

@dp.message(F.text == "❤️ Избранное")
async def favorites(message: Message):
    await message.answer("Избранное пока в разработке. Товары уже можно добавлять в корзину.")

@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    await message.answer(
        f"👤 Ваш профиль\nTelegram ID: {message.from_user.id}\n"
        "Если нужна помощь с заказом — нажми «💬 Поддержка»."
    )

@dp.message(F.text == "📦 Мои заказы")
async def orders(message: Message):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT id, total, status, created_at FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 10", (message.from_user.id,)).fetchall()
    if not rows:
        await message.answer("Пока у вас нет оформленных заказов.")
        return
    lines = [f"Заказ №{oid} — {total} ₽\nСтатус: {status}\nДата: {created}" for oid, total, status, created in rows]
    await message.answer("📦 <b>Ваши заказы</b>\n\n" + "\n\n".join(lines), parse_mode="HTML")

@dp.message(F.text == "🎁 Бонусы")
async def bonuses(message: Message):
    await message.answer("Бонусная программа пока не подключена.")

@dp.message(F.text == "💬 Поддержка")
async def support(message: Message):
    await message.answer("Напиши сюда свой вопрос — сообщение увидим в этом чате. Для быстрого заказа можно также написать в Telegram-магазин.")

@dp.message(Command("catalog"))
async def catalog_count(message: Message):
    products = all_visible_products()
    available = sum(1 for p in products if p["available"])
    await message.answer(f"Каталог загружен: {len(products)} позиций, в наличии: {available}.")

@dp.message()
async def fallback(message: Message):
    await message.answer("Выбери действие в меню ниже.", reply_markup=MAIN_MENU)

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
