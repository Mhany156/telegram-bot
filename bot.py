
import os
import asyncio
import re
import json
import time
import contextlib
from html import escape
import hmac
import hashlib

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Document
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

import aiohttp
import aiosqlite
from aiohttp import web

# ==================== CONFIG ====================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# --- Paymob variables ---
PAYMOB_API_KEY = os.getenv("PAYMOB_API_KEY")
PAYMOB_CARD_ID = int(os.getenv("PAYMOB_CARD_INTEGRATION_ID", 0))
PAYMOB_WALLET_ID = int(os.getenv("PAYMOB_WALLET_INTEGRATION_ID", 0))
PAYMOB_IFRAME_ID = int(os.getenv("PAYMOB_IFRAME_ID", 0))
PAYMOB_HMAC_SECRET = os.getenv("PAYMOB_HMAC_SECRET", "")  # Optional: HMAC verification

# --- Web server (for webhook) ---
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
WEB_BASE_PATH = os.getenv("WEB_BASE_PATH", "").rstrip("/")  # e.g., "/api"

if not TOKEN:
    raise RuntimeError("Please set TELEGRAM_TOKEN in .env")

print("Loaded ADMIN_IDS:", ADMIN_IDS)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ==================== DB ====================
DB_PATH = "store.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0,
        email TEXT,
        phone TEXT
        );""")
        await db.execute("""CREATE TABLE IF NOT EXISTS stock(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            credential TEXT NOT NULL,
            is_sold INTEGER DEFAULT 0,
            p_price REAL, p_cap INTEGER, p_sold INTEGER DEFAULT 0,
            s_price REAL, s_cap INTEGER, s_sold INTEGER DEFAULT 0,
            l_price REAL, l_cap INTEGER, l_sold INTEGER DEFAULT 0,
            chosen_mode TEXT
        );""")
        await db.execute("""CREATE TABLE IF NOT EXISTS sales_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stock_id INTEGER NOT NULL,
            category TEXT,
            credential TEXT,
            price_paid REAL,
            mode_sold TEXT,
            purchase_date TEXT DEFAULT (DATETIME('now', 'localtime'))
        );""")
        await db.execute("""CREATE TABLE IF NOT EXISTS instructions(
            category TEXT NOT NULL,
            mode TEXT NOT NULL,
            message_text TEXT NOT NULL,
            PRIMARY KEY (category, mode)
        );""")
        await db.execute("""CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_order_id TEXT UNIQUE,
            user_id INTEGER,
            amount_cents INTEGER,
            status TEXT,
            created_at TEXT DEFAULT (DATETIME('now','localtime'))
        );""")

        await db.execute("CREATE INDEX IF NOT EXISTS idx_stock_category ON stock(category);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sales_user ON sales_history(user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(merchant_order_id);")
        await db.commit()
    await migrate_db()

async def migrate_db():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("PRAGMA table_info(stock)")
        cols = {row[1] for row in await cur.fetchall()}
        to_add = [
            ("p_price","REAL"),("p_cap","INTEGER"),("p_sold","INTEGER DEFAULT 0"),
            ("s_price","REAL"),("s_cap","INTEGER"),("s_sold","INTEGER DEFAULT 0"),
            ("l_price","REAL"),("l_cap","INTEGER"),("l_sold","INTEGER DEFAULT 0"),
            ("chosen_mode","TEXT")
        ]
        for name, spec in to_add:
            if name not in cols:
                try:
                    await db.execute(f"ALTER TABLE stock ADD COLUMN {name} {spec}")
                except Exception as e:
                    print("[WARN] migration:", name, e)
        await db.commit()

# ==================== HELPERS ====================
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def normalize_digits(s: str) -> str:
    return s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))

def parse_float_loose(s: str):
    if not s: return None
    s = normalize_digits(s).replace(",", ".")
    m = re.search(r'[-+]?\d+(?:\.\d+)?', s)
    return float(m.group(0)) if m else None

def parse_int_loose(s: str):
    if not s: return None
    s = normalize_digits(s)
    m = re.search(r'\d{1,12}', s)
    return int(m.group(0)) if m else None

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 شحن الرصيد (آلي)", callback_data="charge_menu")],
        [InlineKeyboardButton(text="🛍️ الكتالوج / شراء", callback_data="catalog")],
        [InlineKeyboardButton(text="💼 رصيدي", callback_data="balance")],
    ])

# ---- users / balances ----

# ==== CONTACT MANAGEMENT ====
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EG_PHONE_RE = re.compile(r"^\+?20?1[0-25]\d{8}$")

def valid_email(s: str) -> bool:
    return bool(EMAIL_RE.match(s or ""))

def valid_phone(s: str) -> bool:
    return bool(EG_PHONE_RE.match(normalize_digits((s or "").strip())))

@dp.message(Command("setcontact"))
async def setcontact_cmd(m: Message, state: FSMContext):
    await state.set_state(ContactStates.waiting_email)
    await m.reply("✉️ من فضلك أرسل بريدك الإلكتروني (مثال: name@example.com)")

@dp.message(StateFilter(ContactStates.waiting_email))
async def take_email(m: Message, state: FSMContext):
    email = (m.text or "").strip()
    if not valid_email(email):
        await m.reply("⚠️ بريد غير صالح. حاول مرة أخرى.")
        return
    await state.update_data(email=email)
    await state.set_state(ContactStates.waiting_phone)
    await m.reply("📱 أرسل رقم هاتفك (مصري) بصيغة 01XXXXXXXXX أو +201XXXXXXXXX")

@dp.message(StateFilter(ContactStates.waiting_phone))
async def take_phone(m: Message, state: FSMContext):
    phone = (m.text or "").strip()
    if not valid_phone(phone):
        await m.reply("⚠️ رقم غير صالح. أعد الإرسال بصيغة 01XXXXXXXXX أو +201XXXXXXXXX.")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET email=?, phone=? WHERE user_id=?", (data["email"], phone, m.from_user.id))
        await db.commit()
    await state.clear()
    await m.reply("✅ تم حفظ بيانات التواصل بنجاح. يمكنك الآن استخدام /charge")

@dp.message(Command("mycontact"))
async def mycontact_cmd(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT email, phone FROM users WHERE user_id=?", (m.from_user.id,))
        row = await cur.fetchone()
        if row:
            email, phone = row
  await m.reply(f"بعتلي رقم موبايلك وإيميلك بالشكل ده:\n\nمثال:\n📧 example@email.com\n📱 01012345678")
          await m.reply(f"📇 بياناتك:
Email: {email or '-'}
Phone: {phone or '-'}")
        else:
            await m.reply("لا يوجد بيانات مسجلة لك. استخدم /setcontact")


async def get_or_create_user(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        r = await cur.fetchone()
        if r is None:
            await db.execute("INSERT INTO users(user_id,balance) VALUES(?,0)", (user_id,))
            await db.commit()
            return 0.0
        return float(r[0])

async def set_balance(user_id: int, bal: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id,balance) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance",
            (user_id, bal)
        )
        await db.commit()

async def change_balance(user_id: int, delta: float) -> bool:
    bal = await get_or_create_user(user_id)
    new_bal = bal + delta
    if new_bal < 0: return False
    await set_balance(user_id, new_bal)
    return True

# ---- stock helpers ----
async def add_stock_row_modes(category: str, credential: str,
                              p_price=None,p_cap=None,
                              s_price=None,s_cap=None,
                              l_price=None,l_cap=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO stock(category, price, credential,
                              p_price, p_cap, s_price, s_cap, l_price, l_cap)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (category, 0, credential, p_price, p_cap, s_price, s_cap, l_price, l_cap))
        await db.commit()

async def add_stock_simple(category: str, price: float, credential: str):
    await add_stock_row_modes(category, credential, p_price=price, p_cap=1)

async def clear_stock_category(category: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM stock WHERE category=?", (category,))
        await db.commit()
        return cur.rowcount

async def list_stock_items(category: str, limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, price, credential, p_price, s_price, l_price FROM stock
            WHERE IFNULL(is_sold,0)=0 AND category=?
            ORDER BY id ASC
            LIMIT ?
        """, (category, limit))
        return await cur.fetchall()

def remaining_for_mode(row, mode):
    idx = {"personal": (6,7), "shared": (9,10), "laptop": (12,13)}[mode]
    cap = row[idx[0]] if row[idx[0]] is not None else 0
    sold = row[idx[1]] if row[idx[1]] is not None else 0
    return max(cap - sold, 0)

def price_for_mode(row, mode):
    col = {"personal":5, "shared":8, "laptop":11}[mode]
    pr = row[col]
    return pr if pr is not None else row[2]

async def list_categories():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT category,
                   SUM(CASE
                       WHEN (chosen_mode IS NULL AND (IFNULL(p_cap,0)>IFNULL(p_sold,0) OR IFNULL(s_cap,0)>IFNULL(s_sold,0) OR IFNULL(l_cap,0)>IFNULL(l_sold,0)))
                         OR (chosen_mode='personal' AND IFNULL(p_cap,0) > IFNULL(p_sold,0))
                         OR (chosen_mode='shared'  AND IFNULL(s_cap,0) > IFNULL(s_sold,0))
                         OR (chosen_mode='laptop'  AND IFNULL(l_cap,0) > IFNULL(l_sold,0))
                       THEN 1 ELSE 0 END) AS items_available
            FROM stock
            WHERE IFNULL(is_sold,0)=0
            GROUP BY category ORDER BY category
        """)
        return await cur.fetchall()

async def list_modes_for_category(category: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, category, price, credential, IFNULL(is_sold,0),
                   p_price, p_cap, IFNULL(p_sold,0),
                   s_price, s_cap, IFNULL(s_sold,0),
                   l_price, l_cap, IFNULL(l_sold,0),
                   chosen_mode
            FROM stock
            WHERE category=? AND IFNULL(is_sold,0)=0
            ORDER BY id ASC
        """, (category,))
        items = await cur.fetchall()
    res = {}
    for mode in ("personal","shared","laptop"):
        min_price, count = None, 0
        for r in items:
            chosen = r[14]
            rem = remaining_for_mode(r, mode)
            if rem <= 0: continue
            if chosen is None or chosen == mode:
                pr = price_for_mode(r, mode)
                if pr is None: continue
                count += 1
                if min_price is None or pr < min_price: min_price = pr
        if count > 0:
            res[mode] = {"count": count, "min_price": min_price}
    return res

async def find_item_with_mode(category: str, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, category, price, credential, IFNULL(is_sold,0),
                   p_price, p_cap, IFNULL(p_sold,0),
                   s_price, s_cap, IFNULL(s_sold,0),
                   l_price, l_cap, IFNULL(l_sold,0),
                   chosen_mode
            FROM stock
            WHERE category=? AND IFNULL(is_sold,0)=0
            ORDER BY id ASC
        """, (category,))
        items = await cur.fetchall()
    for r in items:
        chosen = r[14]
        rem = remaining_for_mode(r, mode)
        pr = price_for_mode(r, mode)
        if pr is None or rem <= 0: continue
        if chosen is None or chosen == mode:
            return r
    return None

# ==================== FSM: Import ====================
class ImportStates(StatesGroup):
    single = State()
    multi  = State()

# ==================== USER HANDLERS ====================
@dp.message(Command("start"))
async def start_cmd(m: Message):
    await get_or_create_user(m.from_user.id)
    await m.answer("أهلًا بك 👋\nاختر من القائمة:", reply_markup=main_menu_kb())

@dp.message(Command("whoami"))
async def whoami_cmd(m: Message):
    await m.reply(f"Your user_id: {m.from_user.id}\nAdmin: {is_admin(m.from_user.id)}")

@dp.message(Command("balance"))
async def balance_cmd(m: Message):
    bal = await get_or_create_user(m.from_user.id)
    await m.answer(f"رصيدك الحالي: {bal:g} ج.م", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "balance")
async def cb_balance(c: CallbackQuery):
    bal = await get_or_create_user(c.from_user.id)
    await c.message.edit_text(f"رصيدك الحالي: {bal:g} ج.م", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "charge_menu")
async def cb_charge_menu(c: CallbackQuery):
    await c.message.edit_text("لشحن رصيدك، استخدم الأمر التالي:\n`/charge <amount>`\nمثال: `/charge 100`.", parse_mode="Markdown")

@dp.callback_query(F.data == "back_home")
async def cb_back_home(c: CallbackQuery):
    await c.message.edit_text("اختر من القائمة:", reply_markup=main_menu_kb())

# ==================== IMPORT COMMANDS ====================
@dp.message(Command("importstock"))
async def importstock_cmd(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.set_state(ImportStates.single)
    await m.reply("📥 أرسل TXT أو الصق سطور:\n<category> <price> <credential>")

@dp.message(Command("importstockm"))
async def importstockm_cmd(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.set_state(ImportStates.multi)
    await m.reply("📥 أرسل TXT:\n<cat> <p_p> <p_c> <s_p> <s_c> <l_p> <l_c> <cred>")

def parse_stock_lines(text: str):
    ok, fail, res = 0, 0, []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"): continue
        parts = line.split(maxsplit=2)
        if len(parts) < 3: fail += 1; continue
        cat, price_s, cred = parts[0], parts[1], parts[2]
        price = parse_float_loose(price_s)
        if price is None or not cred: fail += 1; continue
        res.append((cat, price, cred)); ok += 1
    return res, ok, fail

def parse_stockm_lines(text: str):
    results = []; ok = fail = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"): continue
        parts = line.split(maxsplit=7)
        if len(parts) < 8: fail += 1; continue
        cat, p_pr_s, p_c_s, s_pr_s, s_c_s, l_pr_s, l_c_s, cred = parts
        p_price = parse_float_loose(p_pr_s); p_cap = parse_int_loose(p_c_s)
        s_price = parse_float_loose(s_pr_s); s_cap = parse_int_loose(s_c_s)
        l_price = parse_float_loose(l_pr_s); l_cap = parse_int_loose(l_c_s)
        if any(v is None for v in [p_price,p_cap,s_price,s_cap,l_price,l_cap]): fail += 1; continue
        results.append((cat, p_price, p_cap, s_price, s_cap, l_price, l_cap, cred)); ok += 1
    return results, ok, fail

async def add_stock_row_modes(category: str, credential: str,
                              p_price=None,p_cap=None,
                              s_price=None,s_cap=None,
                              l_price=None,l_cap=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO stock(category, price, credential,
                              p_price, p_cap, s_price, s_cap, l_price, l_cap)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (category, 0, credential, p_price, p_cap, s_price, s_cap, l_price, l_cap))
        await db.commit()

@dp.message(StateFilter(ImportStates.single), F.text)
async def import_text_single(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    rows, ok, fail = parse_stock_lines(m.text or "")
    for cat, price, cred in rows:
        await add_stock_simple(cat, price, cred)
    await m.reply(f"✅ تم استيراد {ok}. ❌ فشل {fail}.")
    await state.clear()

@dp.message(StateFilter(ImportStates.multi), F.text)
async def import_text_multi(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    rows, ok, fail = parse_stockm_lines(m.text or "")
    for cat, ppr, pcap, spr, scap, lpr, lcap, cred in rows:
        await add_stock_row_modes(cat, cred, ppr, pcap, spr, scap, lpr, lcap)
    await m.reply(f"✅ تم استيراد {ok} (مودات). ❌ فشل {fail}.")
    await state.clear()

@dp.message(StateFilter(ImportStates.single), F.document)
async def import_file_single(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    doc: Document = m.document
    if not (doc.mime_type == "text/plain" or (doc.file_name and doc.file_name.lower().endswith(".txt"))):
        await m.reply("⚠️ أرسل ملف .txt فقط."); return
    try:
        file = await bot.get_file(doc.file_id)
        from io import BytesIO
        buf = BytesIO()
        await bot.download(file, buf)
        text = buf.getvalue().decode("utf-8", "ignore")
    except Exception as e:
        await m.reply(f"❌ فشل تنزيل الملف: {e}"); return
    rows, ok, fail = parse_stock_lines(text)
    for cat, price, cred in rows:
        await add_stock_simple(cat, price, cred)
    await m.reply(f"✅ تم استيراد {ok}. ❌ فشل {fail}.")
    await state.clear()

@dp.message(StateFilter(ImportStates.multi), F.document)
async def import_file_multi(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    doc: Document = m.document
    if not (doc.mime_type == "text/plain" or (doc.file_name and doc.file_name.lower().endswith(".txt"))):
        await m.reply("⚠️ أرسل ملف .txt فقط."); return
    try:
        file = await bot.get_file(doc.file_id)
        from io import BytesIO
        buf = BytesIO()
        await bot.download(file, buf)
        text = buf.getvalue().decode("utf-8", "ignore")
    except Exception as e:
        await m.reply(f"❌ فشل تنزيل الملف: {e}"); return
    rows, ok, fail = parse_stockm_lines(text)
    for cat, ppr, pcap, spr, scap, lpr, lcap, cred in rows:
        await add_stock_row_modes(cat, cred, ppr, pcap, spr, scap, lpr, lcap)
    await m.reply(f"✅ تم استيراد {ok} (مودات). ❌ فشل {fail}.")
    await state.clear()

# ==================== PAYMOB INTEGRATION ====================
PAYMOB_AUTH_URL = "https://accept.paymob.com/api/auth/tokens"
PAYMOB_ORDER_URL = "https://accept.paymob.com/api/ecommerce/orders"
PAYMOB_PAYMENT_KEY_URL = "https://accept.paymob.com/api/acceptance/payment_keys"
PAYMOB_IFRAME_URL = f"https://accept.paymob.com/api/acceptance/iframes/{PAYMOB_IFRAME_ID}?payment_token={{}}"

async def get_auth_token():
    async with aiohttp.ClientSession() as session:
        async with session.post(PAYMOB_AUTH_URL, json={"api_key": PAYMOB_API_KEY}) as response:
            data = await response.json()
            if response.status != 201 and response.status != 200:
                print("[PAYMOB AUTH ERROR]", response.status, data)
            return data.get("token"), data

async def register_order(token: str, merchant_order_id: str, amount_cents: int):
    payload = {
        "auth_token": token, "delivery_needed": "false", "amount_cents": str(amount_cents),
        "currency": "EGP", "merchant_order_id": merchant_order_id,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(PAYMOB_ORDER_URL, json=payload) as response:
            data = await response.json()
            if response.status not in (200, 201):
                print("[PAYMOB ORDER ERROR]", response.status, data)
            return data.get("id"), data

async def get_payment_key(token: str, order_id: int, amount_cents: int, integration_id: int, user_details: dict):
    payload = {
        "auth_token": token, "amount_cents": str(amount_cents), "expiration": 3600, "order_id": order_id,
        "billing_data": {
            "email": user_details.get("email", "NA"), "first_name": user_details.get("first_name", "NA"),
            "last_name": user_details.get("last_name", "NA"), "phone_number": user_details.get("phone_number", "NA"),
            "apartment": "NA", "floor": "NA", "street": "NA", "building": "NA", "shipping_method": "NA",
            "postal_code": "NA", "city": "NA", "country": "NA", "state": "NA"
        },
        "currency": "EGP", "integration_id": integration_id, "lock_order_when_paid": "true"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(PAYMOB_PAYMENT_KEY_URL, json=payload) as response:
            data = await response.json()
            if response.status not in (200, 201):
                print("[PAYMOB KEY ERROR]", response.status, data)
            return data.get("token"), data

@dp.message(Command("charge"))
async def charge_cmd(m: Message, command: CommandObject):
    if not command.args:
        await m.reply("⚠️ الاستخدام: /charge <amount>\nمثال: /charge 50"); return
    amount_egp = parse_float_loose(command.args)
    if amount_egp is None or amount_egp <= 1:
        await m.reply("⚠️ المبلغ يجب أن يكون رقمًا صحيحًا وأكبر من 1."); return
    amount_cents = int(amount_egp * 100)

    if not (PAYMOB_API_KEY and PAYMOB_CARD_ID > 0 and PAYMOB_IFRAME_ID > 0):
        await m.reply("⚠️ إعدادات الدفع غير مكتملة. راجع .env"); return

    merchant_order_id = f"tg-{m.from_user.id}-{int(time.time())}"

    try:
        token, auth_raw = await get_auth_token()
        if not token:
            raise Exception(f"Failed to get auth token: {auth_raw}")
        order_id, order_raw = await register_order(token, merchant_order_id, amount_cents)
        if not order_id:
            raise Exception(f"Failed to register order: {order_raw}")
        # IMPORTANT: Correctly formatted user_details (unique per transaction)
        payment_key, key_raw = await get_payment_key(
            token=token,
            order_id=order_id,
            amount_cents=amount_cents,
            integration_id=PAYMOB_CARD_ID,
            user_details={
                "email": f"user{m.from_user.id}_{int(time.time())}@example.com",
                "first_name": m.from_user.first_name or "Telegram",
                "last_name": m.from_user.last_name or "User",
                "phone_number": f"+2010{str(m.from_user.id)[-8:]}"
            }
        )
        if not payment_key:
            raise Exception(f"Failed to get payment key: {key_raw}")

        payment_url = PAYMOB_IFRAME_URL.format(payment_key)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO payments(merchant_order_id, user_id, amount_cents, status)
                VALUES (?, ?, ?, 'pending')
            """, (merchant_order_id, m.from_user.id, amount_cents))
            await db.commit()

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"💳 ادفع {amount_egp:g} جنيه الآن", url=payment_url)
        ]])
        await m.reply("تم إنشاء فاتورة الدفع. اضغط على الزر أدناه لإتمام العملية.", reply_markup=kb)

    except Exception as e:
        print("[PAYMOB ERROR]", e)
        await m.reply("حدث خطأ أثناء إنشاء فاتورة الدفع. جرّب مرة أخرى لاحقًا.")

# ==================== CATALOG & BUY (Atomic) ====================
@dp.callback_query(F.data == "catalog")
async def cb_catalog(c: CallbackQuery):
    rows = await list_categories()
    if not rows:
        await c.message.edit_text("لا توجد مخزونات حاليًا.", reply_markup=main_menu_kb()); return
    kb = [[InlineKeyboardButton(text=f"{cat} — {cnt} عنصر", callback_data=f"cat::{cat}")] for cat, cnt in rows]
    kb.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="back_home")])
    await c.message.edit_text("🛍️ اختر فئة:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

def modes_kb(modes_info, category):
    name = {"personal":"فردي","shared":"مشترك","laptop":"لابتوب"}
    rows = []
    for m in ["personal","shared","laptop"]:
        if m in modes_info:
            mi = modes_info[m]
            rows.append([InlineKeyboardButton(
                text=f"{name[m]} — من {mi['min_price']:g} ج.م ({mi['count']} عنصر)",
                callback_data=f"mode::{category}::{m}"
            )])
    rows.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data.startswith("cat::"))
async def cb_pick_category(c: CallbackQuery):
    category = c.data.split("::",1)[1]
    modes_info = await list_modes_for_category(category)
    if not modes_info: await c.answer("لا يوجد عناصر متاحة في هذه الفئة حاليًا.", show_alert=True); return
    await c.message.edit_text(f"الفئة: {category}\nاختر النوع:", reply_markup=modes_kb(modes_info, category))

@dp.callback_query(F.data.startswith("mode::"))
async def cb_pick_mode(c: CallbackQuery):
    _, category, mode = c.data.split("::",2)
    item = await find_item_with_mode(category, mode)
    if not item: await c.answer("لا يوجد عنصر مناسب الآن.", show_alert=True); return
    price = price_for_mode(item, mode)
    await c.message.edit_text(
        f"الفئة: {category}\nالنوع: {mode}\nالسعر: {price:g} ج.م\nاضغط شراء لإتمام العملية.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ شراء الآن", callback_data=f"buy::{category}::{mode}")],
            [InlineKeyboardButton(text="🔙 رجوع", callback_data=f"cat::{category}")]
        ])
    )

async def atomic_buy(user_id: int, category: str, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("""
                SELECT id, category, price, credential, IFNULL(is_sold,0),
                       p_price, p_cap, IFNULL(p_sold,0),
                       s_price, s_cap, IFNULL(s_sold,0),
                       l_price, l_cap, IFNULL(l_sold,0),
                       chosen_mode
                FROM stock
                WHERE category=? AND IFNULL(is_sold,0)=0
                ORDER BY id ASC
            """, (category,))
            items = await cur.fetchall()
            if not items:
                raise ValueError("NO_ITEM")

            def remaining_for_mode_row(row, m):
                idx = {"personal": (6,7), "shared": (9,10), "laptop": (12,13)}[m]
                cap = row[idx[0]] or 0
                sold = row[idx[1]] or 0
                return max(cap - sold, 0)

            def price_for_mode_row(row, m):
                col = {"personal":5, "shared":8, "laptop":11}[m]
                return (row[col] if row[col] is not None else row[2]) or 0.0

            chosen = None
            for r in items:
                ch = r[14]
                if remaining_for_mode_row(r, mode) > 0 and (ch is None or ch == mode):
                    chosen = r
                    break
            if not chosen:
                raise ValueError("NO_STOCK")

            price = float(price_for_mode_row(chosen, mode))

            cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
            bal = float(row[0] if row else 0.0)
            if bal < price:
                raise ValueError("LOW_BAL")

            await db.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?",
                (price, user_id, price)
            )
            if db.total_changes == 0:
                raise ValueError("LOW_BAL")

            sid = chosen[0]
            sold_field, cap_field = {
                "personal": ("p_sold","p_cap"),
                "shared":   ("s_sold","s_cap"),
                "laptop":   ("l_sold","l_cap"),
            }[mode]
            await db.execute(f"""
                UPDATE stock
                   SET {sold_field} = IFNULL({sold_field},0)+1,
                       chosen_mode = COALESCE(chosen_mode, ?),
                       is_sold = CASE WHEN (SELECT {sold_field} FROM stock WHERE id=?)+1 >= IFNULL({cap_field},0) THEN 1 ELSE IFNULL(is_sold,0) END
                 WHERE id=? AND (chosen_mode IS NULL OR chosen_mode=?)
            """, (mode, sid, sid, mode))

            if db.total_changes == 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (price, user_id))
                raise ValueError("RACE")

            await db.execute("""
                INSERT INTO sales_history(user_id, stock_id, category, credential, price_paid, mode_sold)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, sid, chosen[1], chosen[3], price, mode))

            await db.commit()
            return {"ok": True, "row": chosen, "price": price}
        except Exception:
            with contextlib.suppress(Exception):
                await db.rollback()
            raise

@dp.callback_query(F.data.startswith("buy::"))
async def cb_buy(c: CallbackQuery):
    _, category, mode = c.data.split("::",2)
    try:
        res = await atomic_buy(c.from_user.id, category, mode)
    except ValueError as e:
        code = str(e)
        if code == "LOW_BAL":
            bal = await get_or_create_user(c.from_user.id)
            item = await find_item_with_mode(category, mode)
            price = price_for_mode(item, mode) if item else 0
            await c.answer(f"رصيدك لا يكفي. السعر {price:g} ج.م ورصيدك {bal:g} ج.م", show_alert=True)
        elif code in ("NO_ITEM","NO_STOCK"):
            await c.answer("لا يوجد عنصر متاح الآن.", show_alert=True)
        else:
            await c.answer("حدث تعارض أثناء الشراء. جرّب مرة أخرى.", show_alert=True)
        return

    row = res["row"]; price = res["price"]
    credential = escape(row[3])
    instructions = await get_instruction(category, mode)
    message_text = f"📩 <b>بيانات حسابك:</b>\n<code>{credential}</code>"
    if instructions:
        message_text += f"\n\n<pre>{escape(instructions)}</pre>"
    try:
        await bot.send_message(c.from_user.id, message_text, parse_mode="HTML")
    except Exception:
        pass

    await c.message.edit_text(f"✅ تم الشراء: {category}\nالنوع: {mode}\nالسعر: {price:g} ج.م\n\nتم إرسال البيانات والتعليمات في رسالة خاصة.")

# ==================== INSTRUCTIONS ADMIN ====================
async def set_instruction(category: str, mode: str, message: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO instructions(category, mode, message_text) VALUES (?, ?, ?)
            ON CONFLICT(category, mode) DO UPDATE SET message_text=excluded.message_text
        """, (category, mode, message))
        await db.commit()

async def get_instruction(category: str, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT message_text FROM instructions WHERE category=? AND mode=?", (category, mode))
        row = await cur.fetchone()
        return row[0] if row else None

async def delete_instruction(category: str, mode: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM instructions WHERE category=? AND mode=?", (category, mode))
        await db.commit()
        return cur.rowcount

async def get_all_instructions():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT category, mode, message_text FROM instructions ORDER BY category, mode")
        return await cur.fetchall()

@dp.message(Command("setinstructions"))
async def setinstructions_cmd(m: Message):
    if not is_admin(m.from_user.id): return
    parts = (m.text or "").split(maxsplit=3)
    valid_modes = ["personal", "shared", "laptop"]
    if len(parts) < 4:
        await m.reply(f"⚠️ الاستخدام: /setinstructions <category> <mode> <message>\nالأنماط: {', '.join(valid_modes)}")
        return
    category, mode, message = parts[1], parts[2].lower(), parts[3]
    if mode not in valid_modes:
        await m.reply(f"⚠️ نمط غير صحيح. الأنماط: {', '.join(valid_modes)}"); return
    await set_instruction(category, mode, message)
    await m.reply(f"✅ تم حفظ التعليمات لـ: {category} ({mode})")

@dp.message(Command("viewinstructions"))
async def viewinstructions_cmd(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id): return
    if command.args:
        parts = command.args.strip().split(maxsplit=1)
        category = parts[0]
        if len(parts) == 2:
            mode = parts[1].lower()
            msg = await get_instruction(category, mode)
            if not msg: await m.reply("لا توجد تعليمات."); return
            await m.reply(f"<b>تعليمات: {escape(category)} ({escape(mode)})</b>\n\n<pre>{escape(msg)}</pre>", parse_mode="HTML")
            return
    all_inst = await get_all_instructions()
    if not all_inst: await m.reply("لا توجد أي تعليمات محفوظة."); return
    lines = ["📜 <b>جميع التعليمات</b>"]
    for cat, md, text in all_inst:
        lines.append(f"\n--- <b>{escape(cat)} ({escape(md)})</b> ---\n<pre>{escape(text)}</pre>")
    await m.reply("\n".join(lines), parse_mode="HTML")

# ==================== WEBHOOK: PAYMOB ====================
def _hmac_concat_from_obj(obj: dict) -> str:
    t = obj or {}
    order = t.get("order", {}) or {}
    source_data = t.get("source_data", {}) or {}
    fields = [
        str(t.get("amount_cents", "")),
        str(t.get("created_at", "")),
        str(t.get("currency", "")),
        str(t.get("error_occured", "")),
        str(t.get("has_parent_transaction", "")),
        str(t.get("id", "")),
        str(t.get("integration_id", "")),
        str(t.get("is_3d_secure", "")),
        str(t.get("is_auth", "")),
        str(t.get("is_capture", "")),
        str(t.get("is_refunded", "")),
        str(t.get("is_standalone_payment", "")),
        str(t.get("is_voided", "")),
        str(order.get("id", "")),
        str(t.get("owner", "")),
        str(t.get("pending", "")),
        str(source_data.get("pan", "")),
        str(source_data.get("sub_type", "")),
        str(source_data.get("type", "")),
        str(t.get("success", "")),
    ]
    return "".join(fields)

def verify_paymob_hmac(secret: str, payload: dict, received_hmac: str) -> bool:
    if not secret or not received_hmac:
        return False
    try:
        concat = _hmac_concat_from_obj(payload.get("obj") or payload)
        expected = hmac.new(secret.encode(), concat.encode(), hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, received_hmac)
    except Exception as e:
        print("[HMAC VERIFY ERROR]", e)
        return False

async def mark_payment_paid(merchant_order_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, user_id, amount_cents, status FROM payments WHERE merchant_order_id=?", (merchant_order_id,))
        row = await cur.fetchone()
        if not row:
            print("[WEBHOOK] Unknown merchant_order_id:", merchant_order_id)
            return False
        pid, user_id, amount_cents, status = row
        if status == "paid":
            return True
        await db.execute("UPDATE payments SET status='paid' WHERE id=?", (pid,))
        await db.commit()
    amount = (amount_cents or 0) / 100.0
    await change_balance(user_id, amount)
    try:
        await bot.send_message(user_id, f"✅ تم شحن رصيدك بمبلغ {amount:g} ج.م.")
    except Exception:
        pass
    return True

async def paymob_webhook(request: web.Request):
    try:
        if request.content_type == "application/json":
            data = await request.json()
        else:
            post = await request.post()
            data = {k: v for k, v in post.items()}
            if "obj" in data and isinstance(data["obj"], str):
                try:
                    data["obj"] = json.loads(data["obj"])
                except Exception:
                    pass
        received_hmac = request.query.get("hmac") or data.get("hmac") or ""
        valid = True
        if PAYMOB_HMAC_SECRET:
            valid = verify_paymob_hmac(PAYMOB_HMAC_SECRET, data, received_hmac)
        if not valid:
            return web.json_response({"ok": False, "reason": "invalid_hmac"}, status=400)

        obj = data.get("obj") or data
        success = bool(obj.get("success"))
        order_info = obj.get("order") or {}
        merchant_order_id = str(order_info.get("merchant_order_id") or obj.get("merchant_order_id") or "")

        if not merchant_order_id:
            return web.json_response({"ok": False, "reason": "missing_merchant_order_id"}, status=400)

        if success:
            ok = await mark_payment_paid(merchant_order_id)
            return web.json_response({"ok": ok})
        else:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE payments SET status='failed' WHERE merchant_order_id=? AND status='pending'", (merchant_order_id,))
                await db.commit()
            return web.json_response({"ok": True, "status": "failed"})
    except Exception as e:
        print("[WEBHOOK ERROR]", e)
        return web.json_response({"ok": False, "error": str(e)}, status=500)

@dp.message(Command("confirmcharge"))
async def confirm_cmd(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id): return
    merchant_order_id = (command.args or "").strip()
    if not merchant_order_id:
        await m.reply("الاستخدام: /confirmcharge <merchant_order_id>"); return
    ok = await mark_payment_paid(merchant_order_id)
    await m.reply("✅ تم التأكيد وشحن الرصيد." if ok else "❌ لم يتم العثور على العملية.")

# ==================== RUN ====================
async def run_web_app():
    app = web.Application()
    path = WEB_BASE_PATH + "/paymob/webhook"
    app.router.add_post(path, paymob_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    print(f"[WEB] Paymob webhook listening on http://{WEB_HOST}:{WEB_PORT}{path}")

async def main():
    await init_db()
    print("Bot started.")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print("[WARN] delete_webhook:", e)

    await run_web_app()

    @dp.message()
    async def fallback_handler(m: Message):
        if m.text and m.text.startswith('/'):
            return

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
