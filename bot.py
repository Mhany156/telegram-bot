import os
import asyncio
import re
import json
import time
from html import escape

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Document
)
import aiohttp
import aiosqlite

# ==================== CONFIG ====================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PAYMOB_API_KEY = os.getenv("PAYMOB_API_KEY")
PAYMOB_CARD_ID = int(os.getenv("PAYMOB_CARD_INTEGRATION_ID", 0))
PAYMOB_IFRAME_ID = int(os.getenv("PAYMOB_IFRAME_ID", 0))

if not TOKEN: raise RuntimeError("Please set TELEGRAM_TOKEN in .env")
print("Loaded ADMIN_IDS:", ADMIN_IDS)
bot = Bot(token=TOKEN)
dp = Dispatcher()
DB_PATH = "store.db"

# ==================== DB and Helpers ====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0);""")
        await db.execute("""CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, price REAL NOT NULL DEFAULT 0, credential TEXT NOT NULL, is_sold INTEGER DEFAULT 0, p_price REAL, p_cap INTEGER, p_sold INTEGER DEFAULT 0, s_price REAL, s_cap INTEGER, s_sold INTEGER DEFAULT 0, l_price REAL, l_cap INTEGER, l_sold INTEGER DEFAULT 0, chosen_mode TEXT);""")
        await db.execute("""CREATE TABLE IF NOT EXISTS sales_history(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, stock_id INTEGER NOT NULL, category TEXT, credential TEXT, price_paid REAL, mode_sold TEXT, purchase_date TEXT DEFAULT (DATETIME('now', 'localtime')));""")
        await db.execute("""CREATE TABLE IF NOT EXISTS instructions(category TEXT NOT NULL, mode TEXT NOT NULL, message_text TEXT NOT NULL, PRIMARY KEY (category, mode));""")
        await db.commit()

def is_admin(uid: int) -> bool: return uid in ADMIN_IDS
def normalize_digits(s: str) -> str: return s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
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

async def get_or_create_user(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        r = await cur.fetchone()
        if r is None:
            await db.execute("INSERT INTO users(user_id,balance) VALUES(?,0)", (user_id,))
            await db.commit()
            return 0.0
        return float(r[0])
async def find_item_with_mode(category: str, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, category, price, credential, IFNULL(is_sold,0), p_price, p_cap, IFNULL(p_sold,0), s_price, s_cap, IFNULL(s_sold,0), l_price, l_cap, IFNULL(l_sold,0), chosen_mode FROM stock WHERE category=? AND IFNULL(is_sold,0)=0 ORDER BY id ASC", (category,))
        items = await cur.fetchall()
    for r in items:
        chosen = r[14]
        rem = max((r[{"personal": 6, "shared": 9, "laptop": 12}[mode]] or 0) - (r[{"personal": 7, "shared": 10, "laptop": 13}[mode]] or 0), 0)
        pr = r[{"personal": 5, "shared": 8, "laptop": 11}[mode]]
        if pr is None or rem <= 0: continue
        if chosen is None or chosen == mode: return r
    return None
async def increment_sale_and_finalize(stock_row, mode: str):
    id_ = stock_row[0]
    sold_field, cap_field = {"personal": ("p_sold","p_cap"), "shared": ("s_sold","s_cap"), "laptop": ("l_sold","l_cap")}[mode]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT {sold_field},{cap_field},chosen_mode FROM stock WHERE id=?", (id_,))
        s, cap, ch = await cur.fetchone()
        ch = mode if ch is None else ch; s = 0 if s is None else s; cap = 0 if cap is None else cap
        if ch != mode or s >= cap: return False
        s += 1
        is_sold_val = 1 if s >= cap and mode == 'personal' else 0
        await db.execute(f"UPDATE stock SET {sold_field}=?, chosen_mode=?, is_sold=CASE WHEN ?=1 THEN 1 ELSE IFNULL(is_sold,0) END WHERE id=?", (s, ch, is_sold_val, id_))
        await db.commit()
    return True
async def log_sale(user_id: int, stock_row: tuple, price: float, mode: str):
    stock_id, category, _, credential, *_ = stock_row
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO sales_history(user_id, stock_id, category, credential, price_paid, mode_sold) VALUES (?, ?, ?, ?, ?, ?)", (user_id, stock_id, category, credential, price, mode))
        await db.commit()
async def get_instruction(category: str, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT message_text FROM instructions WHERE category=? AND mode=?", (category, mode))
        row = await cur.fetchone()
        return row[0] if row else None

# ==================== PAYMOB INTEGRATION ====================
PAYMOB_AUTH_URL = "https://accept.paymob.com/api/auth/tokens"
PAYMOB_ORDER_URL = "https://accept.paymob.com/api/ecommerce/orders"
PAYMOB_PAYMENT_KEY_URL = "https://accept.paymob.com/api/acceptance/payment_keys"
PAYMOB_IFRAME_URL = f"https://accept.paymob.com/api/acceptance/iframes/{PAYMOB_IFRAME_ID}?payment_token={{}}"
async def get_auth_token():
    async with aiohttp.ClientSession() as s:
        async with s.post(PAYMOB_AUTH_URL, json={"api_key": PAYMOB_API_KEY}) as r: return (await r.json()).get("token")
async def register_order(token, merchant_order_id, amount_cents):
    payload = {"auth_token": token, "delivery_needed": "false", "amount_cents": str(amount_cents), "currency": "EGP", "merchant_order_id": merchant_order_id}
    async with aiohttp.ClientSession() as s:
        async with s.post(PAYMOB_ORDER_URL, json=payload) as r: return (await r.json()).get("id")
async def get_payment_key(token, order_id, amount_cents, integration_id):
    payload = {"auth_token": token, "amount_cents": str(amount_cents), "expiration": 3600, "order_id": order_id, "billing_data": {"email": "NA", "first_name": "NA", "last_name": "NA", "phone_number": "NA", "apartment": "NA", "floor": "NA", "street": "NA", "building": "NA", "shipping_method": "NA", "postal_code": "NA", "city": "NA", "country": "NA", "state": "NA"}, "currency": "EGP", "integration_id": integration_id, "lock_order_when_paid": "true"}
    async with aiohttp.ClientSession() as s:
        async with s.post(PAYMOB_PAYMENT_KEY_URL, json=payload) as r: return (await r.json()).get("token")

# ==================== BOT HANDLERS ====================
@dp.message(Command("start"))
async def start_cmd(m: Message):
    await get_or_create_user(m.from_user.id)
    await m.answer("أهلًا بك 👋\nاختر من القائمة:", reply_markup=main_menu_kb())
@dp.message(Command("balance"))
async def balance_cmd(m: Message):
    bal = await get_or_create_user(m.from_user.id)
    await m.answer(f"رصيدك الحالي: {bal:g} ج.м")
@dp.message(Command("charge"))
async def charge_cmd(m: Message, command: CommandObject):
    if not command.args:
        await m.reply("⚠️ الاستخدام: /charge <amount>\nمثال: /charge 50"); return
    amount_egp = parse_float_loose(command.args)
    if amount_egp is None or amount_egp < 5:
        await m.reply("⚠️ المبلغ يجب أن يكون رقمًا صحيحًا و 5 جنيهات أو أكثر."); return
    amount_cents = int(amount_egp * 100)
    merchant_order_id = f"tg-{m.from_user.id}-{int(time.time())}"
    try:
        token = await get_auth_token()
        order_id = await register_order(token, merchant_order_id, amount_cents)
        payment_key = await get_payment_key(token, order_id, amount_cents, PAYMOB_CARD_ID)
        payment_url = PAYMOB_IFRAME_URL.format(payment_key)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 ادفع {amount_egp:g} جنيه الآن", url=payment_url)]])
        await m.reply("تم إنشاء فاتورة الدفع. اضغط على الزر أدناه لإتمام العملية.", reply_markup=kb)
    except Exception as e:
        print(f"[PAYMOB ERROR] {e}")
        await m.reply("حدث خطأ أثناء إنشاء فاتورة الدفع. يرجى المحاولة مرة أخرى لاحقًا.")
@dp.callback_query(F.data == "charge_menu")
async def cb_charge_menu(c: CallbackQuery):
    await c.message.edit_text("لشحن رصيدك، استخدم الأمر التالي في الشات مباشرة:\n`/charge <amount>`\n\n**مثال:**\n`/charge 100` لشحن 100 جنيه.", parse_mode="Markdown")
@dp.callback_query(F.data.startswith("buy::"))
async def cb_buy(c: CallbackQuery):
    _, category, mode = c.data.split("::",2)
    row = await find_item_with_mode(category, mode)
    if not row: await c.answer("لا يوجد عنصر متاح الآن.", show_alert=True); return
    price = row[{"personal": 5, "shared": 8, "laptop": 11}[mode]]
    bal = await get_or_create_user(c.from_user.id)
    if bal < price:
        await c.answer(f"رصيدك لا يكفي. السعر {price:g} ج.м ورصيدك {bal:g} ج.м", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (price, c.from_user.id))
        await db.commit()
    ok = await increment_sale_and_finalize(row, mode)
    if not ok:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (price, c.from_user.id))
            await db.commit()
        await c.answer("نفذ المخزون أثناء الشراء.", show_alert=True); return
    await log_sale(c.from_user.id, row, price, mode)
    credential = escape(row[3])
    instructions = await get_instruction(category, mode)
    message_text = f"📩 <b>بيانات حسابك:</b>\n<code>{credential}</code>"
    if instructions: message_text += f"\n\n{instructions}"
    try: await bot.send_message(c.from_user.id, message_text, parse_mode="HTML")
    except Exception: pass
    await c.message.edit_text(f"✅ تم الشراء: {category}\nالنوع: {mode}\nالسعر: {price:g} ج.м\n\nتم إرسال البيانات والتعليمات في رسالة خاصة.")

# ==================== RUN ====================
async def main():
    await init_db()
    print("Bot started.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

