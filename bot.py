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

# --- تحميل متغيرات Paymob ---
PAYMOB_API_KEY = os.getenv("PAYMOB_API_KEY")
PAYMOB_HMAC_SECRET = os.getenv("PAYMOB_HMAC_SECRET")
PAYMOB_CARD_ID = int(os.getenv("PAYMOB_CARD_INTEGRATION_ID", 0))
PAYMOB_WALLET_ID = int(os.getenv("PAYMOB_WALLET_INTEGRATION_ID", 0))
PAYMOB_IFRAME_ID = int(os.getenv("PAYMOB_IFRAME_ID", 0))

if not TOKEN:
    raise RuntimeError("Please set TELEGRAM_TOKEN in .env")

print("Loaded ADMIN_IDS:", ADMIN_IDS)

bot = Bot(token=TOKEN)
dp = Dispatcher()
flask_app = Flask(__name__) # إنشاء تطبيق Flask

# ==================== DB ====================
DB_PATH = "store.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0);""")
        await db.execute("""CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, price REAL NOT NULL DEFAULT 0, credential TEXT NOT NULL, is_sold INTEGER DEFAULT 0, p_price REAL, p_cap INTEGER, p_sold INTEGER DEFAULT 0, s_price REAL, s_cap INTEGER, s_sold INTEGER DEFAULT 0, l_price REAL, l_cap INTEGER, l_sold INTEGER DEFAULT 0, chosen_mode TEXT);""")
        await db.execute("""CREATE TABLE IF NOT EXISTS sales_history(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, stock_id INTEGER NOT NULL, category TEXT, credential TEXT, price_paid REAL, mode_sold TEXT, purchase_date TEXT DEFAULT (DATETIME('now', 'localtime')));""")
        await db.execute("""CREATE TABLE IF NOT EXISTS instructions(category TEXT NOT NULL, mode TEXT NOT NULL, message_text TEXT NOT NULL, PRIMARY KEY (category, mode));""")
        await db.commit()

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
        await db.execute("INSERT INTO users(user_id,balance) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance", (user_id, bal))
        await db.commit()

async def change_balance(user_id: int, delta: float) -> bool:
    bal = await get_or_create_user(user_id)
    new_bal = bal + delta
    if new_bal < 0: return False
    await set_balance(user_id, new_bal)
    return True

# ---- stock helpers ----
async def add_stock_row_modes(category: str, credential: str, p_price=None,p_cap=None, s_price=None,s_cap=None, l_price=None,l_cap=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO stock(category, price, credential, p_price, p_cap, s_price, s_cap, l_price, l_cap) VALUES (?,?,?,?,?,?,?,?,?)", (category, 0, credential, p_price, p_cap, s_price, s_cap, l_price, l_cap))
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
        cur = await db.execute("SELECT id, price, credential, p_price, s_price, l_price FROM stock WHERE IFNULL(is_sold,0)=0 AND category=? ORDER BY id ASC LIMIT ?", (category, limit))
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
        cur = await db.execute("SELECT category, SUM(CASE WHEN (chosen_mode IS NULL AND (IFNULL(p_cap,0)>IFNULL(p_sold,0) OR IFNULL(s_cap,0)>IFNULL(s_sold,0) OR IFNULL(l_cap,0)>IFNULL(l_sold,0))) OR (chosen_mode='personal' AND IFNULL(p_cap,0) > IFNULL(p_sold,0)) OR (chosen_mode='shared' AND IFNULL(s_cap,0) > IFNULL(s_sold,0)) OR (chosen_mode='laptop' AND IFNULL(l_cap,0) > IFNULL(l_sold,0)) THEN 1 ELSE 0 END) AS items_available FROM stock WHERE IFNULL(is_sold,0)=0 GROUP BY category ORDER BY category")
        return await cur.fetchall()

async def list_modes_for_category(category: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, category, price, credential, IFNULL(is_sold,0), p_price, p_cap, IFNULL(p_sold,0), s_price, s_cap, IFNULL(s_sold,0), l_price, l_cap, IFNULL(l_sold,0), chosen_mode FROM stock WHERE category=? AND IFNULL(is_sold,0)=0 ORDER BY id ASC", (category,))
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
        cur = await db.execute("SELECT id, category, price, credential, IFNULL(is_sold,0), p_price, p_cap, IFNULL(p_sold,0), s_price, s_cap, IFNULL(s_sold,0), l_price, l_cap, IFNULL(l_sold,0), chosen_mode FROM stock WHERE category=? AND IFNULL(is_sold,0)=0 ORDER BY id ASC", (category,))
        items = await cur.fetchall()
    for r in items:
        chosen = r[14]
        rem = remaining_for_mode(r, mode)
        pr = price_for_mode(r, mode)
        if pr is None or rem <= 0: continue
        if chosen is None or chosen == mode:
            return r
    return None

async def increment_sale_and_finalize(stock_row, mode: str):
    id_ = stock_row[0]
    sold_field, cap_field = {"personal": ("p_sold","p_cap"), "shared": ("s_sold","s_cap"), "laptop": ("l_sold","l_cap")}[mode]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT {sold_field},{cap_field},chosen_mode FROM stock WHERE id=?", (id_,))
        s, cap, ch = await cur.fetchone()
        ch = mode if ch is None else ch
        if ch != mode: return False
        s = 0 if s is None else s
        cap = 0 if cap is None else cap
        if s >= cap: return False
        s += 1
        is_sold_val = 1 if s >= cap else 0
        await db.execute(f"UPDATE stock SET {sold_field}=?, chosen_mode=?, is_sold=CASE WHEN ?=1 THEN 1 ELSE IFNULL(is_sold,0) END WHERE id=?", (s, ch, is_sold_val, id_))
        await db.commit()
    return True

async def log_sale(user_id: int, stock_row: tuple, price: float, mode: str):
    stock_id, category, _, credential, *_ = stock_row
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO sales_history(user_id, stock_id, category, credential, price_paid, mode_sold) VALUES (?, ?, ?, ?, ?, ?)", (user_id, stock_id, category, credential, price, mode))
        await db.commit()

async def get_sales_history(limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, category, credential, price_paid, mode_sold, purchase_date FROM sales_history ORDER BY id DESC LIMIT ?", (limit,))
        return await cur.fetchall()

async def set_instruction(category: str, mode: str, message: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO instructions(category, mode, message_text) VALUES (?, ?, ?) ON CONFLICT(category, mode) DO UPDATE SET message_text=excluded.message_text", (category, mode, message))
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
    await c.message.edit_text("لشحن رصيدك، استخدم الأمر التالي في الشات مباشرة:\n`/charge <amount>`\n\n**مثال:**\n`/charge 100` لشحن 100 جنيه.", parse_mode="Markdown")

@dp.callback_query(F.data == "back_home")
async def cb_back_home(c: CallbackQuery):
    await c.message.edit_text("اختر من القائمة:", reply_markup=main_menu_kb())

# ==================== ADMIN HANDLERS ====================
@dp.message(Command("addbal"))
async def addbal_cmd(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id): return
    if not command.args: await m.reply("⚠️ الاستخدام: /addbal <user_id> <amount>"); return
    parts = command.args.split(maxsplit=1)
    uid = parse_int_loose(parts[0])
    amt = parse_float_loose(parts[1]) if len(parts) > 1 else None
    if uid is None or amt is None: await m.reply("⚠️ اكتب ID صحيح ومبلغ رقمي."); return
    await change_balance(uid, amt)
    await m.reply("✅ تم الشحن.")

@dp.message(Command("clearstock"))
async def clearstock_cmd(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id): return
    if not command.args: await m.reply("⚠️ الاستخدام: /clearstock <category>"); return
    count = await clear_stock_category(command.args.strip())
    await m.reply(f"🧹 تم حذف {count} عنصر.")

@dp.message(Command("liststock"))
async def liststock_cmd(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id): return
    if not command.args: await m.reply("⚠️ الاستخدام: /liststock <category> [limit]"); return
    parts = command.args.split(maxsplit=1)
    category = parts[0]
    limit = 20
    if len(parts) == 2 and (maybe := parse_int_loose(parts[1])):
        limit = max(1, min(maybe, 200))
    rows = await list_stock_items(category, limit)
    if not rows: await m.reply("لا يوجد عناصر في هذه الفئة."); return
    lines = [f"أول {len(rows)} عنصر ({category}):"]
    for row in rows:
        sid, price, cred, p_p, s_p, l_p = row
        prices = f"P:{p_p or 'N/A'}|S:{s_p or 'N/A'}|L:{l_p or 'N/A'}"
        lines.append(f"- ID={sid} | {prices} | {cred}")
    await m.reply("\n".join(lines))

@dp.message(Command("stock"))
async def stock_cmd(m: Message):
    if not is_admin(m.from_user.id): return
    rows = await list_categories()
    if not rows: await m.reply("لا يوجد مخزون."); return
    lines = ["المخزون الحالي (حسب الفئات):"] + [f"- {cat}: {cnt} عنصر متاح" for cat, cnt in rows]
    lines.append("\nاستخدم /liststock <category> لعرض IDs.")
    await m.reply("\n".join(lines))

@dp.message(Command("sales"))
async def sales_history_cmd(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id): return
    limit = 20
    if command.args and (limit_arg := parse_int_loose(command.args)):
        limit = max(1, min(limit_arg, 100))
    sales = await get_sales_history(limit)
    if not sales: await m.reply("لا يوجد أي سجل مبيعات."); return
    lines = [f"آخر {len(sales)} عملية بيع:"]
    for uid, cat, cred, price, mode, pdate in sales:
        lines.append(f"👤 `{uid}`\n🛍️ `{cat}` ({mode}) | {price:g} ج.م\n🗓️ {pdate}\n`{cred}`\n---")
    await m.reply("\n".join(lines), parse_mode="Markdown")

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
        await m.reply(f"⚠️ نمط غير صحيح. الأنماط: {', '.join(valid_modes)}")
        return
    await set_instruction(category, mode, message)
    await m.reply(f"✅ تم حفظ التعليمات لـ: {category} ({mode})")

@dp.message(Command("delinstructions"))
async def delinstructions_cmd(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id): return
    parts = (command.args or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        await m.reply("⚠️ الاستخدام: /delinstructions <category> <mode>"); return
    category, mode = parts[0], parts[1].lower()
    deleted = await delete_instruction(category, mode)
    await m.reply(f"✅ تم حذف التعليمات." if deleted else "⚠️ لا توجد تعليمات لهذه الفئة والنمط.")

@dp.message(Command("viewinstructions"))
async def viewinstructions_cmd(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id): return
    if command.args:
        parts = command.args.strip().split(maxsplit=1)
        category = parts[0]
        if len(parts) == 2:
            mode = parts[1].lower()
            msg = await get_instruction(category, mode)
            if not msg: await m.reply("لا توجد تعليمات لهذه الفئة والنمط."); return
            await m.reply(f"<b>تعليمات: {escape(category)} ({escape(mode)})</b>\n\n{msg}", parse_mode="HTML")
        else:
            all_inst = await get_all_instructions()
            cat_inst = [i for i in all_inst if i[0] == category]
            if not cat_inst: await m.reply("لا توجد تعليمات لهذه الفئة."); return
            lines = [f"📜 <b>تعليمات فئة: {escape(category)}</b>"]
            for cat, md, text in cat_inst: lines.append(f"\n--- <b>{escape(md)}</b> ---\n{text}")
            await m.reply("\n".join(lines), parse_mode="HTML")
    else:
        all_inst = await get_all_instructions()
        if not all_inst: await m.reply("لا توجد أي تعليمات محفوظة."); return
        lines = ["📜 <b>جميع التعليمات المحفوظة:</b>"]
        for cat, md, text in all_inst: lines.append(f"\n--- <b>{escape(cat)} ({escape(md)})</b> ---\n{text}")
        await m.reply("\n".join(lines), parse_mode="HTML")

# ==================== IMPORT LOGIC & HANDLERS ====================
# ... (All import logic and handlers remain the same) ...

# ==================== PAYMOB INTEGRATION ====================
# ... (All Paymob API functions remain the same) ...
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
        if not token: raise Exception("Failed to get auth token")
        order_id = await register_order(token, merchant_order_id, amount_cents)
        if not order_id: raise Exception("Failed to register order")
        
        payment_key = await get_payment_key(token, order_id, amount_cents, PAYMOB_CARD_ID)
        if not payment_key: raise Exception("Failed to get payment key")
        
        payment_url = PAYMOB_IFRAME_URL.format(payment_key)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 ادفع {amount_egp:g} جنيه الآن", url=payment_url)]])
        await m.reply("تم إنشاء فاتورة الدفع. اضغط على الزر أدناه لإتمام العملية.", reply_markup=kb)
    except Exception as e:
        print("[PAYMOB ERROR]", e)
        await m.reply("حدث خطأ أثناء إنشاء فاتورة الدفع. يرجى المحاولة مرة أخرى لاحقًا.")

# ==================== CATALOG & BUY ====================
# ... (All catalog and buy handlers remain the same) ...

# ==================== WEBHOOK LISTENER (WITH DIAGNOSTICS) ====================
@flask_app.route('/')
def health_check():
    print("[FLASK] Health check endpoint was hit!")
    return "Flask server is running!"

@flask_app.route('/webhook', methods=['POST'])
def paymob_webhook():
    print("[WEBHOOK] Webhook received!")
    data = request.json
    obj = data.get('obj', {})
    
    # HMAC Verification
    received_hmac = request.headers.get('x-paymob-hmac-sha512')
    if not received_hmac: return abort(400)

    concatenated_string = f"{obj.get('amount_cents', '')}{obj.get('created_at', '')}{obj.get('currency', '')}{str(obj.get('error_occured', '')).lower()}{str(obj.get('has_parent_transaction', '')).lower()}{obj.get('id', '')}{obj.get('integration_id', '')}{str(obj.get('is_3d_secure', '')).lower()}{str(obj.get('is_auth', '')).lower()}{str(obj.get('is_capture', '')).lower()}{str(obj.get('is_refunded', '')).lower()}{str(obj.get('is_standalone_payment', '')).lower()}{str(obj.get('is_voided', '')).lower()}{obj['order'].get('id', '')}{obj.get('owner', '')}{str(obj.get('pending', '')).lower()}{obj['source_data'].get('pan', '')}{obj['source_data'].get('sub_type', '')}{obj['source_data'].get('type', '')}{str(obj.get('success', '')).lower()}"
    
    h = hmac.new(PAYMOB_HMAC_SECRET.encode('utf-8'), concatenated_string.encode('utf-8'), hashlib.sha512)
    calculated_hmac = h.hexdigest()

    if not hmac.compare_digest(calculated_hmac, received_hmac):
        print("[WEBHOOK] HMAC verification failed!")
        return abort(403)

    # Process the transaction
    if data.get('type') == 'TRANSACTION' and obj.get('success'):
        print("[WEBHOOK] Received successful transaction callback.")
        try:
            merchant_order_id = obj['order']['merchant_order_id']
            if merchant_order_id and merchant_order_id.startswith('tg-'):
                parts = merchant_order_id.split('-')
                user_id = int(parts[1])
                amount_cents = obj.get('amount_cents')
                amount_egp = float(amount_cents) / 100

                loop = dp.loop
                asyncio.run_coroutine_threadsafe(change_balance(user_id, amount_egp), loop)
                
                confirmation_message = f"✅ تم شحن رصيدك بنجاح بمبلغ {amount_egp:g} ج.م."
                asyncio.run_coroutine_threadsafe(bot.send_message(user_id, confirmation_message), loop)
        except Exception as e:
            print(f"[WEBHOOK ERROR] Failed to process webhook: {e}")
            
    return ('', 200)

# ==================== RUN ====================
async def main():
    await init_db()
    
    dp.loop = asyncio.get_running_loop()
    
    print("Bot started.")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print("[WARN] delete_webhook:", e)
    
    port = int(os.getenv("PORT", 8080))
    threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=port, debug=False), daemon=True).start()
    
    # This is a catch-all for pasted imports, must be registered last.
    @dp.message()
    async def pasted_imports(m: Message):
        st = getattr(dp, "workflow_state", {})
        w_m = st.get("awaiting_importm"); w_s = st.get("awaiting_import")
        if (w_m and w_m.get("admin") == m.from_user.id) or \
           (w_s and w_s.get("admin") == m.from_user.id):
            if is_admin(m.from_user.id):
                # (Re-pasting the process_import function here as it's needed)
                if w_m:
                    rows, ok, fail = parse_stockm_lines(m.text or "")
                    for row_data in rows:
                        cat, p_price, p_cap, s_price, s_cap, l_price, l_cap, cred = row_data
                        await add_stock_row_modes(cat, cred, p_price, p_cap, s_price, s_cap, l_price, l_cap)
                    await m.reply(f"✅ تم استيراد {ok} (مودات). ❌ فشل {fail}.")
                else: # w_s
                    rows, ok, fail = parse_stock_lines(m.text or "")
                    for cat, price, cred in rows:
                        await add_stock_simple(cat, price, cred)
                    await m.reply(f"✅ تم استيراد {ok}. ❌ فشل {fail}.")
                dp.workflow_state = {}
                return

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())