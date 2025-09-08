import os
import asyncio
import re
import json
import time
import threading
import hmac
import hashlib
from html import escape

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Document
)
import aiohttp
import aiosqlite
from flask import Flask, request, abort

# ==================== CONFIG ====================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PAYMOB_API_KEY = os.getenv("PAYMOB_API_KEY")
PAYMOB_HMAC_SECRET = os.getenv("PAYMOB_HMAC_SECRET")
PAYMOB_CARD_ID = int(os.getenv("PAYMOB_CARD_INTEGRATION_ID", 0))
PAYMOB_WALLET_ID = int(os.getenv("PAYMOB_WALLET_INTEGRATION_ID", 0))
PAYMOB_IFRAME_ID = int(os.getenv("PAYMOB_IFRAME_ID", 0))
if not TOKEN: raise RuntimeError("Please set TELEGRAM_TOKEN in .env")
print("Loaded ADMIN_IDS:", ADMIN_IDS)
bot = Bot(token=TOKEN)
dp = Dispatcher()
flask_app = Flask(__name__)
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
async def get_or_create_user(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        r = await cur.fetchone()
        if r is None:
            await db.execute("INSERT INTO users(user_id,balance) VALUES(?,0)", (user_id,))
            await db.commit()
            return 0.0
        return float(r[0])
async def change_balance(user_id: int, delta: float):
    bal = await get_or_create_user(user_id)
    new_bal = bal + delta
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, user_id))
        await db.commit()
    return new_bal

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

# ==================== WEBHOOK LISTENER (CORRECTED) ====================
@flask_app.route('/webhook', methods=['GET', 'POST'])
def paymob_webhook():
    try:
        if request.method == 'GET':
            # This is the "Transaction Response Callback" which the user is redirected to.
            # It's the most reliable for immediate balance updates.
            hmac_keys = sorted([key for key in request.args.keys() if key != 'hmac'])
            concatenated_string = "".join([request.args.get(key, '') for key in hmac_keys])
            received_hmac = request.args.get('hmac')
            
            if not received_hmac: return abort(400)

            h = hmac.new(PAYMOB_HMAC_SECRET.encode('utf-8'), concatenated_string.encode('utf-8'), hashlib.sha512)
            calculated_hmac = h.hexdigest()

            if not hmac.compare_digest(calculated_hmac, received_hmac):
                print("[WEBHOOK-GET] HMAC verification failed!")
                return abort(403)
            
            if request.args.get('success') == 'true':
                print("[WEBHOOK-GET] Received successful transaction response.")
                merchant_order_id = request.args.get('merchant_order_id')
                if merchant_order_id and merchant_order_id.startswith('tg-'):
                    parts = merchant_order_id.split('-')
                    user_id = int(parts[1])
                    amount_egp = float(request.args.get('amount_cents')) / 100

                    # Run the async functions in the bot's event loop
                    loop = dp.loop
                    future = asyncio.run_coroutine_threadsafe(change_balance(user_id, amount_egp), loop)
                    new_balance = future.result() # Wait for the result
                    
                    confirmation_message = f"✅ تم شحن رصيدك بنجاح بمبلغ {amount_egp:g} ج.م.\nرصيدك الجديد هو: {new_balance:g} ج.م."
                    asyncio.run_coroutine_threadsafe(bot.send_message(user_id, confirmation_message), loop)
            return ("Transaction processed", 200)

        elif request.method == 'POST':
            # This is the server-to-server "Transaction Processed Callback"
            # It's a good backup but the GET is usually faster for the user.
            print("[WEBHOOK-POST] Received POST callback. Ignoring as GET is primary.")
            return ('POST callback received', 200)

    except Exception as e:
        print(f"[WEBHOOK ERROR] An error occurred: {e}")
        return abort(500)

# ==================== BOT HANDLERS & MAIN ====================
# A few key handlers are here, the rest (admin, etc.) can be assumed to be present
@dp.message(Command("start"))
async def start_cmd(m: Message):
    await get_or_create_user(m.from_user.id)
    await m.answer("أهلًا بك 👋\nاختر من القائمة:", reply_markup=main_menu_kb()) # Assumes main_menu_kb is defined

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

async def main():
    await init_db()
    dp.loop = asyncio.get_running_loop()
    print("Bot started.")
    await bot.delete_webhook(drop_pending_updates=True)
    port = int(os.getenv("PORT", 8080))
    threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=port, debug=False), daemon=True).start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())