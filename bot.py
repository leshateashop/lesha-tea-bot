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
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "tea_shop.db")
CATALOG_PATH = os.getenv("CATALOG_PATH", "catalog.csv")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "30838531").strip()

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

# Пороговые уровни можно менять здесь. Персональная скидка заменяет уровень,
# а бонусные рубли не суммируются со скидкой (списание бонусов пока отключено).
LOYALTY_LEVELS = [
    (0, "🌱 Новичок", 0),
    (5000, "🍃 Знаток", 5),
    (15000, "🍵 Ценитель", 7),
    (30000, "🏆 Мастер", 10),
]

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
        [KeyboardButton(text="🍃 Выбрать чай"), KeyboardButton(text="🔎 Поиск чая")],
        [KeyboardButton(text="🛒 Корзина"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="❤️ Избранное"), KeyboardButton(text="📦 Мои заказы")],
        [KeyboardButton(text="🎁 Бонусы"), KeyboardButton(text="🏷 Промокод")],
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
            CREATE TABLE IF NOT EXISTS cart_reminders (
                user_id INTEGER PRIMARY KEY,
                changed_at REAL NOT NULL,
                reminded INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                total_spent INTEGER NOT NULL DEFAULT 0,
                personal_discount INTEGER NOT NULL DEFAULT 0,
                bonus_balance INTEGER NOT NULL DEFAULT 0,
                active_promo TEXT NOT NULL DEFAULT '', promo_waiting INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY, percent INTEGER NOT NULL, max_uses INTEGER NOT NULL DEFAULT 0,
                uses INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT DEFAULT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT NOT NULL, user_id INTEGER NOT NULL, order_id INTEGER,
                PRIMARY KEY(code, user_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                product_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, product_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                review_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, order_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS review_sessions (
                user_id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS checkout_sessions (
                user_id INTEGER PRIMARY KEY, step TEXT NOT NULL, name TEXT DEFAULT '',
                phone TEXT DEFAULT '', delivery TEXT DEFAULT '', address TEXT DEFAULT ''
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS search_sessions (
                user_id INTEGER PRIMARY KEY, query TEXT NOT NULL DEFAULT ''
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                name TEXT, phone TEXT, delivery TEXT, address TEXT, items_json TEXT,
                total INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'Новый',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                subtotal INTEGER NOT NULL DEFAULT 0,
                discount_percent INTEGER NOT NULL DEFAULT 0,
                discount_amount INTEGER NOT NULL DEFAULT 0,
                loyalty_credited INTEGER NOT NULL DEFAULT 0, promo_code TEXT NOT NULL DEFAULT ''
            )
        """)
        # Мягкая миграция: добавляет новые поля в уже существующую базу без потери заказов.
        for table, column, definition in [
            ("users", "total_spent", "INTEGER NOT NULL DEFAULT 0"),
            ("users", "personal_discount", "INTEGER NOT NULL DEFAULT 0"),
            ("users", "bonus_balance", "INTEGER NOT NULL DEFAULT 0"),
            ("users", "active_promo", "TEXT NOT NULL DEFAULT ''"),
            ("users", "promo_waiting", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "subtotal", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "discount_percent", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "discount_amount", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "loyalty_credited", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "promo_code", "TEXT NOT NULL DEFAULT ''"),
        ]:
            existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def loyalty_info(user_id):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT total_spent, personal_discount, bonus_balance FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"spent": 0, "personal": 0, "bonus": 0, "level": LOYALTY_LEVELS[0][1], "level_discount": 0, "discount": 0, "next_threshold": LOYALTY_LEVELS[1][0]}
    spent, personal, bonus = row
    level_name, level_discount, next_threshold = LOYALTY_LEVELS[0][1], LOYALTY_LEVELS[0][2], None
    for i, (threshold, name, discount) in enumerate(LOYALTY_LEVELS):
        if spent >= threshold:
            level_name, level_discount = name, discount
            next_threshold = LOYALTY_LEVELS[i + 1][0] if i + 1 < len(LOYALTY_LEVELS) else None
    return {
        "spent": spent, "personal": personal, "bonus": bonus, "level": level_name,
        "level_discount": level_discount, "discount": personal if personal > 0 else level_discount,
        "next_threshold": next_threshold,
    }

def price_with_discount(user_id, subtotal):
    info = loyalty_info(user_id)
    percent = info["discount"]
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT active_promo FROM users WHERE user_id=?", (user_id,)).fetchone()
        code = row[0] if row else ""
        promo = con.execute("SELECT percent, max_uses, uses, active, expires_at FROM promo_codes WHERE code=?", (code,)).fetchone() if code else None
        if promo and promo[3] and (not promo[1] or promo[2] < promo[1]):
            if not promo[4] or promo[4] >= __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
                # Скидки не складываются: применяется большая из доступных.
                percent = max(percent, int(promo[0]))
            else:
                con.execute("UPDATE users SET active_promo='' WHERE user_id=?", (user_id,))
                code = ""
        elif code:
            con.execute("UPDATE users SET active_promo='' WHERE user_id=?", (user_id,))
            code = ""
    amount = round(subtotal * percent / 100)
    return subtotal - amount, percent, amount

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
    builder.button(text="❤️ В избранное", callback_data=f"fav:add:{product['id']}")
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
            "INSERT INTO users(user_id, username, first_name) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
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

def product_list_keyboard(products, page, total_pages, prefix, back_callback):
    builder = InlineKeyboardBuilder()
    for product in products:
        status = "" if product["available"] else " · нет в наличии"
        builder.button(
            text=f"{product['name']} · {product['price']} ₽{status}"[:64],
            callback_data=f"product:{product['id']}",
        )
    nav = []
    if page > 0:
        nav.append(("◀️", f"{prefix}:{page-1}"))
    if page + 1 < total_pages:
        nav.append(("▶️", f"{prefix}:{page+1}"))
    for label, data in nav:
        builder.button(text=label, callback_data=data)
    builder.button(text="⬅️ Назад", callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()

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
    page_size = 8
    page = 0
    total_pages = max(1, (len(products) + page_size - 1) // page_size)
    shown = products[page * page_size:(page + 1) * page_size]
    markup = product_list_keyboard(shown, page, total_pages, f"catpage:{category}", "categories")
    await callback.message.edit_text(
        f"<b>{escape(category)}</b> · {len(products)} товаров\nВыбери товар (страница {page+1}/{total_pages}):",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("catpage:"))
async def show_category_page(callback: CallbackQuery):
    try:
        _, category, page_raw = callback.data.split(":", 2)
        page = max(0, int(page_raw))
    except (ValueError, TypeError):
        await callback.answer("Не удалось открыть страницу", show_alert=True)
        return
    products = products_for_category(category)
    page_size = 8
    total_pages = max(1, (len(products) + page_size - 1) // page_size)
    page = min(page, total_pages - 1)
    shown = products[page * page_size:(page + 1) * page_size]
    markup = product_list_keyboard(shown, page, total_pages, f"catpage:{category}", "categories")
    await callback.message.edit_text(
        f"<b>{escape(category)}</b> · {len(products)} товаров\nВыбери товар (страница {page+1}/{total_pages}):",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()

@dp.message(F.text == "🔎 Поиск чая")
async def search_start(message: Message):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT INTO search_sessions(user_id, query) VALUES (?, '') ON CONFLICT(user_id) DO UPDATE SET query=''", (message.from_user.id,))
    await message.answer("Напиши название чая или часть названия. Например: габа, Ворон, Да Хун Пао.")

@dp.message(lambda message: bool(message.from_user) and search_is_waiting(message.from_user.id))
async def search_receive(message: Message):
    query = (message.text or "").strip()
    if not query:
        await message.answer("Напиши название чая текстом.")
        return
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE search_sessions SET query=? WHERE user_id=?", (query, message.from_user.id))
    await send_search_results(message, query, 0)

async def send_search_results(message_or_callback, query, page):
    matches = [p for p in all_visible_products() if norm(query) in norm(p["name"]) or norm(query) in norm(p["category"]) or norm(query) in norm(p["description"])]
    if not matches:
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.edit_text("Ничего не найдено. Попробуй другое название.", reply_markup=category_keyboard())
            await message_or_callback.answer()
        else:
            await message_or_callback.answer("Ничего не найдено. Попробуй другое название.")
        return
    page_size = 8
    total_pages = max(1, (len(matches) + page_size - 1) // page_size)
    page = min(max(0, page), total_pages - 1)
    shown = matches[page * page_size:(page + 1) * page_size]
    builder = InlineKeyboardBuilder()
    for product in shown:
        status = "" if product["available"] else " · нет в наличии"
        builder.button(text=f"{product['name']} · {product['price']} ₽{status}"[:64], callback_data=f"product:{product['id']}")
    if page > 0:
        builder.button(text="◀️ Назад", callback_data=f"searchpage:{page-1}")
    if page + 1 < total_pages:
        builder.button(text="Вперёд ▶️", callback_data=f"searchpage:{page+1}")
    builder.button(text="🔎 Искать снова", callback_data="search:again")
    builder.button(text="❤️ В избранное", callback_data=f"fav:add:{product['id']}")
    builder.button(text="⬅️ К категориям", callback_data="categories")
    builder.adjust(1)
    text = f"🔎 <b>Результаты поиска: {escape(query)}</b>\nНайдено: {len(matches)} · страница {page+1}/{total_pages}"
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await message_or_callback.answer()
    else:
        await message_or_callback.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

def search_is_waiting(user_id):
    try:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute("SELECT query FROM search_sessions WHERE user_id=?", (user_id,)).fetchone()
        return row is not None and row[0] == ""
    except sqlite3.Error:
        return False

@dp.callback_query(F.data.startswith("searchpage:"))
async def search_page(callback: CallbackQuery):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT query FROM search_sessions WHERE user_id=?", (callback.from_user.id,)).fetchone()
    if not row or not row[0]:
        await callback.answer("Начни поиск заново", show_alert=True)
        return
    await send_search_results(callback, row[0], int(callback.data.split(":", 1)[1]))

@dp.callback_query(F.data == "search:again")
async def search_again(callback: CallbackQuery):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE search_sessions SET query='' WHERE user_id=?", (callback.from_user.id,))
    await callback.message.answer("Напиши название чая или часть названия.")
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
            photo_value = product["photo"].strip()
            photo_path = Path(photo_value)
            if not photo_path.is_absolute() and not photo_path.exists():
                candidate = Path("photos") / photo_value
                if candidate.exists():
                    photo_path = candidate
            photo_source = FSInputFile(str(photo_path)) if photo_path.is_file() else photo_value
            await callback.message.answer_photo(
                photo=photo_source,
                caption=caption,
                reply_markup=product_keyboard(product),
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Не удалось отправить фото товара %s (photo=%r)", product["id"], product["photo"])
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
        con.execute("""INSERT INTO cart_reminders(user_id, changed_at, reminded) VALUES (?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET changed_at=excluded.changed_at, reminded=0""",
            (callback.from_user.id, __import__('time').time()))
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
    lines, subtotal_total = [], 0
    builder = InlineKeyboardBuilder()
    for product_id, quantity in rows:
        p = catalog.get(str(product_id))
        if not p:
            continue
        subtotal = p["price"] * quantity
        subtotal_total += subtotal
        lines.append(f"• {escape(p['name'])} × {quantity} — {subtotal} ₽")
        builder.button(text=f"Убрать: {p['name']}"[:64], callback_data=f"remove:{product_id}")
    builder.button(text="✅ Оформить заказ", callback_data="checkout:begin")
    builder.adjust(1)
    total, discount_percent, discount_amount = price_with_discount(message.from_user.id, subtotal_total)
    if discount_percent:
        lines.append(f"\nСкидка клиента ({discount_percent}%): −{discount_amount} ₽")
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
        remaining = con.execute("SELECT COUNT(*) FROM cart WHERE user_id=?", (callback.from_user.id,)).fetchone()[0]
        if remaining:
            con.execute("UPDATE cart_reminders SET changed_at=?, reminded=0 WHERE user_id=?", (__import__('time').time(), callback.from_user.id))
        else:
            con.execute("DELETE FROM cart_reminders WHERE user_id=?", (callback.from_user.id,))
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
        con.execute("INSERT OR REPLACE INTO checkout_sessions(user_id, step, name, phone, delivery, address) VALUES (?, 'details', ?, '', 'Уточнить по сообщению', '')", (callback.from_user.id, callback.from_user.full_name or callback.from_user.first_name or 'Покупатель'))
    await callback.message.answer("🧾 <b>Оформление заказа</b>\n\nНапишите <b>одним сообщением</b> ваш телефон и данные доставки: город, адрес или пункт выдачи. Способ доставки тоже укажите, если уже знаете.\n\nИмя и Telegram мы добавим автоматически. Пример: +7 900 123-45-67, СДЭК, пункт выдачи в Москве — адрес уточню.", parse_mode="HTML")
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
    items, subtotal_total = [], 0
    for pid, qty in cart_rows:
        product = catalog.get(str(pid))
        if product and product["available"]:
            subtotal = product["price"] * qty
            subtotal_total += subtotal
            items.append({"id": str(pid), "name": product["name"], "quantity": qty, "price": product["price"], "subtotal": subtotal, "weight": product["weight"]})
    total, discount_percent, discount_amount = price_with_discount(user_id, subtotal_total)
    with sqlite3.connect(DB_PATH) as con:
        promo_row = con.execute("SELECT active_promo FROM users WHERE user_id=?", (user_id,)).fetchone()
        promo_code = promo_row[0] if promo_row else ""
    if not items:
        await callback.answer("В корзине нет доступных товаров", show_alert=True)
        return
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "INSERT INTO orders(user_id, name, phone, delivery, address, items_json, total, subtotal, discount_percent, discount_amount, promo_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, phone, delivery, address, json.dumps(items, ensure_ascii=False), total, subtotal_total, discount_percent, discount_amount, promo_code),
        )
        order_id = cur.lastrowid
        if promo_code:
            con.execute("INSERT OR IGNORE INTO promo_redemptions(code,user_id,order_id) VALUES (?,?,?)", (promo_code,user_id,order_id))
            con.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (promo_code,))
            con.execute("UPDATE users SET active_promo='' WHERE user_id=?", (user_id,))
        con.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
        con.execute("DELETE FROM cart_reminders WHERE user_id = ?", (user_id,))
        con.execute("DELETE FROM checkout_sessions WHERE user_id = ?", (user_id,))
    item_lines = "\n".join(f"• {escape(item['name'])} × {item['quantity']} — {item['subtotal']} ₽" for item in items)
    discount_line = f"\nСкидка клиента ({discount_percent}%): −{discount_amount} ₽" if discount_percent else ""
    await callback.message.answer(f"✅ <b>Заказ №{order_id} принят!</b>\n\n{item_lines}{discount_line}\n\n<b>Итого: {total} ₽</b>\nИмя: {escape(name)}\nТелефон: {escape(phone)}\nДоставка: {escape(delivery)}\nАдрес/пункт выдачи: {escape(address)}\n\nМы свяжемся с вами для подтверждения.", parse_mode="HTML", reply_markup=MAIN_MENU)
    if ADMIN_CHAT_ID:
        try:
            username = callback.from_user.username
            if username:
                telegram_link = f"<a href=\"https://t.me/{escape(username)}\">@{escape(username)}</a>"
                contact_url = f"https://t.me/{username}"
            else:
                telegram_link = f"<a href=\"tg://user?id={user_id}\">Открыть профиль покупателя</a>"
                contact_url = f"tg://user?id={user_id}"
            admin_keyboard = InlineKeyboardBuilder()
            admin_keyboard.button(text="✉️ Написать покупателю", url=contact_url)
            await callback.bot.send_message(
                int(ADMIN_CHAT_ID),
                f"🛎 <b>Новый заказ №{order_id}</b>\n\n{item_lines}{discount_line}\n\n<b>Итого: {total} ₽</b>\n\n"
                f"<b>Покупатель</b>\nИмя: {escape(name)}\nТелефон: {escape(phone)}\n"
                f"Telegram: {telegram_link}\nID: <code>{user_id}</code>\n\n"
                f"<b>Доставка</b>\nСпособ: {escape(delivery)}\nАдрес / пункт выдачи: {escape(address)}",
                parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=admin_keyboard.as_markup()
            )
        except Exception:
            logging.exception("Не удалось отправить заказ владельцу")
    await callback.answer("Заказ оформлен")

@dp.message(lambda message: bool(message.from_user) and checkout_step(message.from_user.id) is not None)
async def checkout_text_step(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пожалуйста, отправьте данные текстом одним сообщением.")
        return
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT step, name, phone, delivery, address FROM checkout_sessions WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return
    step, name, phone, delivery, address = row
    if step != "details":
        await message.answer("Оформление уже начато. Отправьте телефон и данные доставки одним сообщением или нажмите «Отменить».")
        return

    # Сохраняем весь ответ покупателя одним полем, чтобы ничего не потерять.
    import re
    phone_match = re.search(r"(?:\+?\d[\d()\-\s]{7,}\d)", text)
    phone = phone_match.group(0).strip() if phone_match else "Не указан отдельно"
    # Убираем телефон из исходного сообщения, чтобы он не дублировался в адресе.
    details_without_phone = text
    if phone_match:
        details_without_phone = text[:phone_match.start()] + " " + text[phone_match.end():]
    details_without_phone = re.sub(r"[;,|]+", " ", details_without_phone)
    details_without_phone = " ".join(details_without_phone.split()).strip(" .,-")
    lowered = text.lower()
    if "сдэк" in lowered or "cdek" in lowered:
        delivery = "СДЭК"
    elif "ozon" in lowered or "озон" in lowered:
        delivery = "Ozon"
    elif "курьер" in lowered:
        delivery = "Курьер"
    elif "самовывоз" in lowered:
        delivery = "Самовывоз"
    else:
        delivery = "Уточнить по сообщению"
    address = (details_without_phone or "Адрес уточнить")[:1000]
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE checkout_sessions SET phone = ?, delivery = ?, address = ?, step = 'confirm' WHERE user_id = ?", (phone[:100], delivery, address, user_id))
        cart_rows = con.execute("SELECT product_id, quantity FROM cart WHERE user_id = ?", (user_id,)).fetchall()
    catalog = {str(p["id"]): p for p in all_visible_products()}
    items, subtotal_total = [], 0
    for pid, qty in cart_rows:
        product = catalog.get(str(pid))
        if product and product["available"]:
            subtotal = product["price"] * qty
            subtotal_total += subtotal
            items.append(f"• {escape(product['name'])} × {qty} — {subtotal} ₽")
    total, discount_percent, discount_amount = price_with_discount(user_id, subtotal_total)
    with sqlite3.connect(DB_PATH) as con:
        promo_row = con.execute("SELECT active_promo FROM users WHERE user_id=?", (user_id,)).fetchone()
        promo_code = promo_row[0] if promo_row else ""
    if not items:
        await message.answer("В корзине нет доступных товаров. Вернитесь в каталог и выберите товары заново.")
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить заказ", callback_data="checkout:confirm")
    builder.button(text="❌ Отменить", callback_data="checkout:cancel")
    builder.adjust(1)
    await message.answer(
        "<b>Проверьте заказ</b>\n\n" + "\n".join(items) +
        (f"\n\nСкидка клиента ({discount_percent}%): −{discount_amount} ₽" if discount_percent else "") +
        f"\n\n<b>Итого: {total} ₽</b>\nИмя: {escape(name)}\nТелефон: {escape(phone)}\n" +
        f"Данные доставки: {escape(address)}\n\nTelegram-профиль будет прикреплён к заказу.",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )

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

@dp.message(F.text == "🏷 Промокод")
async def promo_prompt(message: Message):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE users SET promo_waiting=1 WHERE user_id=?", (message.from_user.id,))
    await message.answer("Отправь промокод одним сообщением. Чтобы убрать действующий промокод, напиши СБРОС.")

@dp.message(lambda message: bool(message.from_user) and promo_is_waiting(message.from_user.id))
async def promo_apply(message: Message):
    user_id = message.from_user.id
    code = (message.text or "").strip().upper()
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE users SET promo_waiting=0 WHERE user_id=?", (user_id,))
        if code in {"СБРОС", "СБРОСИТЬ", "ОТМЕНА"}:
            con.execute("UPDATE users SET active_promo='' WHERE user_id=?", (user_id,))
            await message.answer("Промокод отключён.", reply_markup=MAIN_MENU)
            return
        promo = con.execute("SELECT percent,max_uses,uses,active,expires_at FROM promo_codes WHERE code=?", (code,)).fetchone()
        already = con.execute("SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?", (code,user_id)).fetchone()
    if not promo or not promo[3]:
        await message.answer("Такого действующего промокода нет. Проверь написание.", reply_markup=MAIN_MENU)
        return
    from datetime import datetime
    if promo[1] and promo[2] >= promo[1]:
        await message.answer("У этого промокода закончились использования.", reply_markup=MAIN_MENU); return
    if promo[4] and promo[4] < datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
        await message.answer("Срок действия промокода закончился.", reply_markup=MAIN_MENU); return
    if already:
        await message.answer("Ты уже использовал этот промокод.", reply_markup=MAIN_MENU); return
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE users SET active_promo=? WHERE user_id=?", (code,user_id))
    await message.answer(f"✅ Промокод {escape(code)} применён: скидка {promo[0]}%. Она не суммируется с персональной скидкой и уровнем лояльности — применяется большая.", reply_markup=MAIN_MENU, parse_mode="HTML")

def promo_is_waiting(user_id):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT promo_waiting FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row[0])

@dp.message(F.text == "❤️ Избранное")
async def favorites(message: Message):
    user_id = message.from_user.id
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT product_id FROM favorites WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    products = [find_product(row[0]) for row in rows]
    products = [p for p in products if p]
    if not products:
        await message.answer("❤️ Избранное пока пустое. Открой чай в каталоге и нажми «❤️ В избранное».", reply_markup=MAIN_MENU)
        return
    builder = InlineKeyboardBuilder()
    for product in products:
        status = "" if product["available"] else " · нет в наличии"
        builder.button(text=f"{product['name']} · {product['price']} ₽{status}"[:64], callback_data=f"product:{product['id']}")
        builder.button(text=f"Убрать: {product['name']}"[:60], callback_data=f"fav:remove:{product['id']}")
    builder.adjust(1)
    await message.answer("❤️ <b>Избранное</b>\nВыбери товар, чтобы открыть карточку, или убери его из списка.", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("fav:add:"))
async def favorite_add(callback: CallbackQuery):
    product_id = callback.data.split(":", 2)[2]
    if not find_product(product_id):
        await callback.answer("Товар не найден в каталоге.", show_alert=True)
        return
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO favorites(user_id, product_id) VALUES (?,?)", (callback.from_user.id, product_id))
    await callback.answer("Добавлено в избранное ❤️")

@dp.callback_query(F.data.startswith("fav:remove:"))
async def favorite_remove(callback: CallbackQuery):
    product_id = callback.data.split(":", 2)[2]
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM favorites WHERE user_id=? AND product_id=?", (callback.from_user.id, product_id))
    await callback.answer("Убрано из избранного")
    # Обновляем список избранного в том же сообщении.
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT product_id FROM favorites WHERE user_id=? ORDER BY created_at DESC", (callback.from_user.id,)).fetchall()
    products = [find_product(row[0]) for row in rows]
    products = [p for p in products if p]
    if not products:
        await callback.message.edit_text("❤️ Избранное пусто.")
        return
    builder = InlineKeyboardBuilder()
    for product in products:
        status = "" if product["available"] else " · нет в наличии"
        builder.button(text=f"{product['name']} · {product['price']} ₽{status}"[:64], callback_data=f"product:{product['id']}")
        builder.button(text=f"Убрать: {product['name']}"[:60], callback_data=f"fav:remove:{product['id']}")
    builder.adjust(1)
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())

@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    info = loyalty_info(message.from_user.id)
    discount_source = f"Персональная скидка: {info['personal']}%" if info["personal"] else f"Скидка по уровню: {info['level_discount']}%"
    next_line = f"До следующего уровня: ещё {max(0, info['next_threshold'] - info['spent'])} ₽\n" if info["next_threshold"] is not None else "У вас максимальный уровень!\n"
    await message.answer(
        f"👤 <b>Ваш профиль</b>\n\nИмя: {escape(message.from_user.full_name or message.from_user.first_name or 'Покупатель')}\n"
        f"Telegram: @{escape(message.from_user.username) if message.from_user.username else 'не указан'}\n"
        f"Telegram ID: <code>{message.from_user.id}</code>\n\n"
        f"🏷 Уровень: <b>{escape(info['level'])}</b>\n{discount_source}\n"
        f"Потрачено на выполненные заказы: {info['spent']} ₽\n{next_line}"
        f"🎁 Бонусный баланс: {info['bonus']} ₽\n\n"
        "Персональную скидку может назначить владелец магазина. Бонусные рубли пока не списываются вместе со скидкой.",
        parse_mode="HTML",
    )

@dp.message(F.text == "📦 Мои заказы")
async def orders(message: Message):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT id, total, status, created_at FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 10", (message.from_user.id,)).fetchall()
    if not rows:
        await message.answer("Пока у вас нет оформленных заказов.")
        return
    builder = InlineKeyboardBuilder()
    lines = []
    for oid, total, status, created in rows:
        lines.append(f"Заказ №{oid} — {total} ₽\nСтатус: {escape(status)}\nДата: {escape(created or '')}")
        builder.button(text=f"🔁 Повторить заказ №{oid}", callback_data=f"order:repeat:{oid}")
    builder.adjust(1)
    await message.answer("📦 <b>Ваши заказы</b>\n\n" + "\n\n".join(lines), parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("order:repeat:"))
async def repeat_order(callback: CallbackQuery):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Не удалось определить заказ", show_alert=True)
        return
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT items_json FROM orders WHERE id=? AND user_id=?", (order_id, callback.from_user.id)).fetchone()
    if not row:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    try:
        old_items = json.loads(row[0] or "[]")
    except (TypeError, json.JSONDecodeError):
        old_items = []
    catalog = {str(p["id"]): p for p in all_visible_products()}
    added, unavailable = [], []
    with sqlite3.connect(DB_PATH) as con:
        for item in old_items:
            pid = str(item.get("id", ""))
            product = catalog.get(pid)
            if not product or not product.get("available"):
                unavailable.append(str(item.get("name", pid)))
                continue
            try:
                qty = max(1, int(item.get("quantity", 1)))
            except (TypeError, ValueError):
                qty = 1
            con.execute("""INSERT INTO cart(user_id, product_id, quantity) VALUES (?, ?, ?)
                ON CONFLICT(user_id, product_id) DO UPDATE SET quantity=quantity+excluded.quantity""",
                (callback.from_user.id, pid, qty))
            con.execute("""INSERT INTO cart_reminders(user_id, changed_at, reminded) VALUES (?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET changed_at=excluded.changed_at, reminded=0""",
                (callback.from_user.id, __import__('time').time()))
            added.append(f"• {escape(product['name'])} × {qty} — по текущей цене {product['price']} ₽")
    if not added:
        await callback.answer("Товары из заказа сейчас недоступны", show_alert=True)
        return
    text = "🔁 <b>Товары добавлены в корзину</b>\n\n" + "\n".join(added)
    if unavailable:
        text += "\n\n⚠️ Нет в наличии:\n" + "\n".join(f"• {escape(name)}" for name in unavailable)
    text += "\n\nЦены актуальные. Проверь корзину перед оформлением."
    await callback.message.answer(text, parse_mode="HTML", reply_markup=MAIN_MENU)
    await callback.answer("Заказ повторён")

@dp.message(F.text == "🎁 Бонусы")
async def bonuses(message: Message):
    info = loyalty_info(message.from_user.id)
    await message.answer(
        f"🎁 <b>Ваша программа лояльности</b>\n\nУровень: {escape(info['level'])}\n"
        f"Скидка сейчас: {info['discount']}%\nНакоплено покупок: {info['spent']} ₽\n"
        f"Бонусный баланс: {info['bonus']} ₽\n\n"
        "Скидка и бонусы не суммируются. Списание бонусных рублей подключим отдельно.",
        parse_mode="HTML",
    )

@dp.message(F.text == "💬 Поддержка")
async def support(message: Message):
    await message.answer("Напиши сюда свой вопрос — сообщение увидим в этом чате. Для быстрого заказа можно также написать в Telegram-магазин.")

@dp.message(Command("client"))
async def admin_client(message: Message):
    if not message.from_user or message.from_user.id != int(ADMIN_CHAT_ID or 0):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /client TELEGRAM_ID")
        return
    user_id = int(parts[1])
    with sqlite3.connect(DB_PATH) as con:
        user = con.execute("SELECT username, first_name, total_spent, personal_discount, bonus_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        orders_count = con.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (user_id,)).fetchone()[0]
    if not user:
        await message.answer("Клиент ещё не запускал бота. Попроси его нажать /start.")
        return
    username, first_name, spent, personal, bonus = user
    info = loyalty_info(user_id)
    username_text = f"@{escape(username)}" if username else "не указан"
    await message.answer(
        f"👤 <b>Клиент</b>\nИмя: {escape(first_name or '—')}\nTelegram: {username_text}\nID: <code>{user_id}</code>\n"
        f"Заказов: {orders_count}\nВыполненные покупки: {spent} ₽\nУровень: {escape(info['level'])}\n"
        f"Скидка по уровню: {info['level_discount']}%\nПерсональная скидка: {personal}%\nБонусы: {bonus} ₽\n\n"
        f"Назначить скидку: <code>/discount {user_id} 15</code>\nСнять персональную скидку: <code>/discount {user_id} 0</code>",
        parse_mode="HTML",
    )

@dp.message(Command("discount"))
async def admin_discount(message: Message):
    if not message.from_user or message.from_user.id != int(ADMIN_CHAT_ID or 0):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Использование: /discount TELEGRAM_ID ПРОЦЕНТ\nНапример: /discount 123456789 15")
        return
    user_id, percent = int(parts[1]), int(parts[2])
    if percent > 50:
        await message.answer("Для безопасности скидка не может превышать 50%.")
        return
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("UPDATE users SET personal_discount=? WHERE user_id=?", (percent, user_id))
        if cur.rowcount == 0:
            await message.answer("Клиент не найден. Попроси его нажать /start в боте.")
            return
    await message.answer(f"✅ Персональная скидка для клиента <code>{user_id}</code> установлена: {percent}%.", parse_mode="HTML")
    try:
        await message.bot.send_message(user_id, f"🎁 Для вас установлена персональная скидка {percent}% в «Чайной лавке дяди Лёши».")
    except Exception:
        logging.info("Не удалось уведомить клиента %s о скидке", user_id)

@dp.message(Command("promo_create"))
async def admin_promo_create(message: Message):
    if not message.from_user or message.from_user.id != int(ADMIN_CHAT_ID or 0):
        return
    parts = (message.text or "").split()
    if len(parts) not in (3,4) or not parts[2].isdigit() or (len(parts)==4 and not parts[3].isdigit()):
        await message.answer("Создать промокод: /promo_create КОД ПРОЦЕНТ [ЛИМИТ]\nПример: /promo_create TEA10 10 100\nЛимит 0 или без лимита = без ограничения.")
        return
    code, percent = parts[1].upper(), int(parts[2])
    limit = int(parts[3]) if len(parts)==4 else 0
    if not code.isalnum() or not 1 <= percent <= 50 or limit < 0:
        await message.answer("Код — латинские буквы/цифры без пробелов, скидка от 1 до 50%, лимит неотрицательный."); return
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT INTO promo_codes(code,percent,max_uses,uses,active) VALUES (?,?,?,0,1) ON CONFLICT(code) DO UPDATE SET percent=excluded.percent,max_uses=excluded.max_uses,uses=0,active=1,expires_at=NULL", (code,percent,limit))
        con.execute("DELETE FROM promo_redemptions WHERE code=?", (code,))
    await message.answer(f"✅ Промокод {escape(code)} создан: скидка {percent}%, лимит: {limit if limit else 'без ограничений'}.", parse_mode="HTML")

@dp.message(Command("promo_list"))
async def admin_promo_list(message: Message):
    if not message.from_user or message.from_user.id != int(ADMIN_CHAT_ID or 0): return
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT code,percent,max_uses,uses,active FROM promo_codes ORDER BY code").fetchall()
    if not rows:
        await message.answer("Промокодов пока нет."); return
    await message.answer("\n".join(f"{code}: {pct}%, использовано {used}/{limit if limit else '∞'}, {'активен' if active else 'выключен'}" for code,pct,limit,used,active in rows))

@dp.message(Command("promo_off"))
async def admin_promo_off(message: Message):
    if not message.from_user or message.from_user.id != int(ADMIN_CHAT_ID or 0): return
    parts = (message.text or "").split()
    if len(parts)!=2:
        await message.answer("Использование: /promo_off КОД"); return
    code=parts[1].upper()
    with sqlite3.connect(DB_PATH) as con:
        cur=con.execute("UPDATE promo_codes SET active=0 WHERE code=?", (code,))
    await message.answer("Промокод выключен." if cur.rowcount else "Промокод не найден.")

@dp.message(Command("order_done"))
async def admin_order_done(message: Message):
    if not message.from_user or message.from_user.id != int(ADMIN_CHAT_ID or 0):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /order_done НОМЕР_ЗАКАЗА")
        return
    order_id = int(parts[1])
    with sqlite3.connect(DB_PATH) as con:
        order = con.execute("SELECT user_id, total, status, loyalty_credited FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            await message.answer("Заказ не найден.")
            return
        user_id, total, status, credited = order
        if credited:
            await message.answer("Этот заказ уже учтён в накоплениях — повторного начисления не будет.")
            return
        con.execute("UPDATE orders SET status='Выполнен', loyalty_credited=1 WHERE id=?", (order_id,))
        con.execute("UPDATE users SET total_spent=total_spent+? WHERE user_id=?", (total, user_id))
    info = loyalty_info(user_id)
    await message.answer(f"✅ Заказ №{order_id} отмечен выполненным. В накопления клиента добавлено {total} ₽.\nНовый уровень: {info['level']}, скидка по уровню: {info['level_discount']}%.")
    review_keyboard = InlineKeyboardBuilder()
    review_keyboard.button(text="✍️ Оставить отзыв", callback_data=f"review:start:{order_id}")
    try:
        await message.bot.send_message(user_id, f"Спасибо за покупку! Заказ №{order_id} отмечен выполненным и добавлен в программу лояльности. Всего накоплено: {info['spent']} ₽. Уровень: {info['level']}.\n\nБудем рады вашему отзыву о покупке.", reply_markup=review_keyboard.as_markup())
    except Exception:
        pass

@dp.callback_query(F.data.startswith("review:start:"))
async def review_start(callback: CallbackQuery):
    if not callback.from_user:
        return
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректный номер заказа", show_alert=True)
        return
    with sqlite3.connect(DB_PATH) as con:
        order = con.execute("SELECT status FROM orders WHERE id=? AND user_id=?", (order_id, callback.from_user.id)).fetchone()
        existing = con.execute("SELECT 1 FROM reviews WHERE user_id=? AND order_id=?", (callback.from_user.id, order_id)).fetchone()
        if order and order[0] == "Выполнен" and not existing:
            con.execute("INSERT OR REPLACE INTO review_sessions(user_id, order_id) VALUES (?,?)", (callback.from_user.id, order_id))
        else:
            order = None
    if not order:
        await callback.answer("Отзыв уже отправлен или заказ ещё не выполнен.", show_alert=True)
        return
    await callback.message.answer(f"Напишите одним сообщением отзыв о заказе №{order_id}. Он появится после проверки.")
    await callback.answer()

@dp.message(lambda message: bool(message.from_user) and review_waiting(message.from_user.id))
async def review_receive(message: Message):
    user_id = message.from_user.id
    review_text = (message.text or "").strip()
    if not review_text or len(review_text) < 3:
        await message.answer("Напишите отзыв текстом, хотя бы несколько слов.")
        return
    if len(review_text) > 1500:
        await message.answer("Отзыв слишком длинный. Максимум 1500 символов.")
        return
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT order_id FROM review_sessions WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return
        order_id = row[0]
        con.execute("INSERT OR IGNORE INTO reviews(user_id, order_id, review_text, status) VALUES (?,?,?,'pending')", (user_id, order_id, review_text))
        con.execute("DELETE FROM review_sessions WHERE user_id=?", (user_id,))
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Одобрить", callback_data=f"review:approve:{user_id}:{order_id}")
    keyboard.button(text="❌ Отклонить", callback_data=f"review:reject:{user_id}:{order_id}")
    keyboard.adjust(2)
    try:
        await message.bot.send_message(int(ADMIN_CHAT_ID), f"📝 <b>Новый отзыв — заказ №{order_id}</b>\nКлиент: <code>{user_id}</code>\n\n{escape(review_text)}", parse_mode="HTML", reply_markup=keyboard.as_markup())
    except Exception:
        logging.exception("Не удалось отправить отзыв администратору")
    await message.answer("Спасибо! Отзыв отправлен на проверку.", reply_markup=MAIN_MENU)

def review_waiting(user_id):
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("SELECT 1 FROM review_sessions WHERE user_id=?", (user_id,)).fetchone() is not None

@dp.callback_query(F.data.startswith("review:approve:"))
async def review_approve(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id != int(ADMIN_CHAT_ID or 0):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    user_id, order_id = int(parts[2]), int(parts[3])
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("UPDATE reviews SET status='approved' WHERE user_id=? AND order_id=? AND status='pending'", (user_id, order_id))
    if cur.rowcount:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"Отзыв по заказу №{order_id} опубликован/одобрен.")
        try:
            await callback.bot.send_message(user_id, "Спасибо! Ваш отзыв одобрен. Благодарим, что поделились впечатлениями о чае.")
        except Exception:
            pass
    await callback.answer()

@dp.callback_query(F.data.startswith("review:reject:"))
async def review_reject(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id != int(ADMIN_CHAT_ID or 0):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    user_id, order_id = int(parts[2]), int(parts[3])
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("UPDATE reviews SET status='rejected' WHERE user_id=? AND order_id=? AND status='pending'", (user_id, order_id))
    if cur.rowcount:
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await callback.bot.send_message(user_id, "Спасибо за отзыв. К сожалению, мы не сможем его опубликовать.")
        except Exception:
            pass
    await callback.answer()

@dp.message(Command("catalog"))
async def catalog_count(message: Message):
    products = all_visible_products()
    available = sum(1 for p in products if p["available"])
    await message.answer(f"Каталог загружен: {len(products)} позиций, в наличии: {available}.")

@dp.message()
async def fallback(message: Message):
    await message.answer("Выбери действие в меню ниже.", reply_markup=MAIN_MENU)

async def abandoned_cart_reminder_loop(bot: Bot):
    """Напоминает о незавершённой корзине через 3 часа; не отправляет повторно."""
    import time
    while True:
        await asyncio.sleep(900)
        cutoff = time.time() - 3 * 60 * 60
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute("""SELECT r.user_id FROM cart_reminders r
                WHERE r.reminded=0 AND r.changed_at<=?
                AND EXISTS (SELECT 1 FROM cart c WHERE c.user_id=r.user_id)""", (cutoff,)).fetchall()
        for (user_id,) in rows:
            try:
                await bot.send_message(
                    user_id,
                    "🍃 В вашей корзине остался чай. Если захотите продолжить, откройте «🛒 Корзина». Цены и наличие проверим при оформлении.",
                    reply_markup=MAIN_MENU,
                )
                with sqlite3.connect(DB_PATH) as con:
                    con.execute("UPDATE cart_reminders SET reminded=1 WHERE user_id=?", (user_id,))
            except Exception:
                logging.info("Не удалось отправить напоминание о корзине пользователю %s", user_id)

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
    init_db()
    bot = Bot(token=BOT_TOKEN)
    asyncio.create_task(abandoned_cart_reminder_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
