import os, re, time, hmac, hashlib, asyncio, html, json, threading
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Document
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.client.default import DefaultBotProperties
from flask import Flask, request, abort
import aiosqlite

# ==================== ENV ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

KASHIER_API_KEY = os.getenv("KASHIER_API_KEY", "")
KASHIER_MERCHANT_ID = os.getenv("KASHIER_MERCHANT_ID", "")
KASHIER_SECRET = os.getenv("KASHIER_SECRET", "")

PP_PERSONAL = os.getenv("KASHIER_PP_PERSONAL", "")
PP_SHARED  = os.getenv("KASHIER_PP_SHARED", "")
PP_LAPTOP  = os.getenv("KASHIER_PP_LAPTOP", "")

if not TELEGRAM_TOKEN or ":" not in TELEGRAM_TOKEN:
    raise RuntimeError("Missing/invalid TELEGRAM_TOKEN in environment.")

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
flask_app = Flask(__name__)

def escape(text: str) -> str:
    return html.escape(text or "")

# ==================== DB ====================
DB_PATH = "store.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0
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
            purchase_date TEXT DEFAULT (DATETIME('now','localtime'))
        );""")
        await db.execute("""CREATE TABLE IF NOT EXISTS instructions(
            category TEXT NOT NULL,
            mode TEXT NOT NULL,
            message_text TEXT NOT NULL,
            PRIMARY KEY (category, mode)
        );""")
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

# ==================== USERS ====================
async def get_or_create_user(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row: return row[0]
        await db.execute("INSERT INTO users(user_id,balance) VALUES(?,0)", (user_id,))
        await db.commit()
        return 0.0

async def change_balance(user_id: int, delta: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id,balance) VALUES(?,0)", (user_id,))
        await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (delta,user_id))
        await db.commit()

# ==================== STOCK ====================
async def add_stock_item(category:str, price:float, credential:str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO stock(category,price,credential) VALUES(?,?,?)",(category,price,credential))
        await db.commit()

async def list_categories():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT category, COUNT(*) FROM stock WHERE is_sold=0 GROUP BY category")
        return await cur.fetchall()

async def list_stock_items(category:str, limit:int=20):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id,price,credential,p_price,s_price,l_price FROM stock WHERE category=? AND is_sold=0 LIMIT ?",(category,limit))
        return await cur.fetchall()

async def clear_stock_category(category:str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM stock WHERE category=?", (category,))
        count = cur.rowcount
        await db.commit()
        return count

async def delete_stock_item(stock_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM stock WHERE id=?", (stock_id,))
        count = cur.rowcount
        await db.commit()
        return count

async def find_item_with_mode(category, mode):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id,credential,price FROM stock WHERE category=? AND is_sold=0 LIMIT 1",(category,))
        row = await cur.fetchone()
        return row

async def mark_item_sold(stock_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE stock SET is_sold=1 WHERE id=?",(stock_id,))
        await db.commit()

async def log_sale(user_id:int, stock_id:int, category:str, credential:str, price:float, mode:str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO sales_history(user_id,stock_id,category,credential,price_paid,mode_sold) VALUES(?,?,?,?,?,?)",(user_id,stock_id,category,credential,price,mode))
        await db.commit()

async def get_instruction(category, mode):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT message_text FROM instructions WHERE category=? AND mode=?",(category,mode))
        row = await cur.fetchone()
        return row[0] if row else None

# ==================== COMMANDS (USER) ====================
@dp.message(Command("start"))
async def cmd_start(m:Message):
    await get_or_create_user(m.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍️ الكتالوج", callback_data="catalog")],
        [InlineKeyboardButton(text="💼 رصيدي", callback_data="balance")]
    ])
    await m.answer("أهلاً بك! اختَر من القائمة:", reply_markup=kb)

@dp.message(Command("whoami"))
async def cmd_whoami(m:Message):
    await m.reply(f"👤 ID: {m.from_user.id}\nName: {m.from_user.full_name}")

@dp.message(Command("balance"))
async def cmd_balance(m:Message):
    bal = await get_or_create_user(m.from_user.id)
    await m.reply(f"💼 رصيدك الحالي: {bal:.2f} ج.م")

# ==================== COMMANDS (ADMIN) ====================
@dp.message(Command("stock"))
async def stock_cmd(m:Message):
    if not is_admin(m.from_user.id): return
    rows = await list_categories()
    if not rows:
        await m.reply("لا يوجد مخزون."); return
    lines = ["المخزون الحالي:"]
    lines += [f"- {cat}: {cnt} عنصر" for cat,cnt in rows]
    await m.reply("\n".join(lines))

@dp.message(Command("liststock"))
async def liststock_cmd(m:Message, command:CommandObject):
    if not is_admin(m.from_user.id): return
    if not command.args: await m.reply("⚠️ الاستخدام: /liststock <category>"); return
    rows = await list_stock_items(command.args.strip(),20)
    if not rows: await m.reply("لا يوجد عناصر."); return
    lines = [f"أول {len(rows)} عنصر:"]
    for sid,price,cred,p_p,s_p,l_p in rows:
        lines.append(f"ID={sid} | {price} | {cred}")
    await m.reply("\n".join(lines))

@dp.message(Command("clearstock"))
async def clearstock_cmd(m:Message, command:CommandObject):
    if not is_admin(m.from_user.id): return
    if not command.args: await m.reply("⚠️ الاستخدام: /clearstock <category>"); return
    count = await clear_stock_category(command.args.strip())
    await m.reply(f"🧹 تم حذف {count} عنصر.")

@dp.message(Command("delstock"))
async def delstock_cmd(m:Message, command:CommandObject):
    if not is_admin(m.from_user.id): return
    if not command.args: await m.reply("⚠️ الاستخدام: /delstock <stock_id>"); return
    stock_id = parse_int_loose(command.args)
    if not stock_id: return await m.reply("⚠️ ID غير صالح")
    count = await delete_stock_item(stock_id)
    await m.reply(f"🗑️ تم حذف {count} عنصر.")

# ==================== CATALOG ====================
@dp.callback_query(F.data=="catalog")
async def cb_catalog(c:CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 مشترك", callback_data="cat::مشترك")],
        [InlineKeyboardButton(text="👤 فردي", callback_data="cat::فردي")],
        [InlineKeyboardButton(text="💻 لابتوب", callback_data="cat::لابتوب")]
    ])
    await c.message.edit_text("اختر الفئة:", reply_markup=kb)

@dp.callback_query(F.data.startswith("cat::"))
async def cb_category(c:CallbackQuery):
    _, category = c.data.split("::",1)
    rows = await list_stock_items(category,1)
    if not rows: return await c.answer("لا يوجد عناصر.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 ادفع", callback_data=f"mode::{category}::personal")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="catalog")]
    ])
    await c.message.edit_text(f"الفئة {category}، اختر نوع الدفع:", reply_markup=kb)

@dp.callback_query(F.data.startswith("mode::"))
async def cb_pick_mode(c:CallbackQuery):
    _, category, mode = c.data.split("::",2)
    row = await find_item_with_mode(category, mode)
    if not row:
        return await c.answer("لا يوجد عنصر متاح.", show_alert=True)
    stock_id, credential, price = row
    safe_cat = re.sub(r'[^a-zA-Z0-9_-]+', '_', category)
    merchant_order_id = f"buy-{c.from_user.id}-{safe_cat}-{mode}-{int(time.time())}"
    pp_map = {"personal":PP_PERSONAL,"shared":PP_SHARED,"laptop":PP_LAPTOP}
    base_url = pp_map.get(mode,"")
    if not base_url:
        return await c.answer("صفحة الدفع غير مجهزة", show_alert=True)
    sep = "&" if "?" in base_url else "?"
    pay_url = f"{base_url}{sep}ref={merchant_order_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 ادفع {price:.2f} ج.م", url=pay_url)],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data=f"cat::{category}")]
    ])
    await c.message.edit_text(f"الفئة: {category}\nالسعر: {price:.2f} ج.م", reply_markup=kb)

# ==================== KASHIER CALLBACK ====================
def _kashier_verify_signature(raw:bytes, sig:str)->bool:
    api_key = (KASHIER_API_KEY or "").encode()
    if not api_key or not sig: return False
    calc = hmac.new(api_key, raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig.lower(), calc.lower())

@flask_app.route("/kashier-callback", methods=["POST"])
def kashier_callback():
    try:
        raw = request.get_data() or b""
        sig = request.headers.get("X-Kashier-Signature") or request.headers.get("Kashier-Signature")
        if not _kashier_verify_signature(raw,sig): return abort(400)
        payload = request.json or {}
        status = str(payload.get("status","")).lower()
        ref = payload.get("reference") or payload.get("ref")
        if status!="paid" or not ref or not str(ref).startswith("buy-"):
            return ("",200)
        parts = str(ref).split("-",4)
        if len(parts)<5: return ("",200)
        user_id = int(parts[1]); category=parts[2].replace("_"," "); mode=parts[3]
        async def finalize():
            row = await find_item_with_mode(category, mode)
            if not row:
                return await bot.send_message(user_id,"⚠️ تمت عملية الدفع لكن العنصر غير متاح.")
            stock_id, credential, price = row
            await mark_item_sold(stock_id)
            await log_sale(user_id, stock_id, category, credential, price, mode)
            instructions = await get_instruction(category,mode) or ""
            msg = f"✅ تم الدفع.\n📦 {escape(category)} ({escape(mode)})\n📩 بياناتك:\n<code>{escape(credential)}</code>"
            if instructions: msg+=f"\n\n{instructions}"
            await bot.send_message(user_id,msg)
        asyncio.run_coroutine_threadsafe(finalize(), dp.loop)
        return ("",200)
    except Exception as e:
        print("[KASHIER CALLBACK ERROR]",e); return abort(500)

# ==================== RUN ====================
if __name__=="__main__":
    asyncio.get_event_loop().run_until_complete(init_db())
    def run_flask():
        flask_app.run(host="0.0.0.0",port=int(os.getenv("PORT",8080)))
    threading.Thread(target=run_flask,daemon=True).start()
    asyncio.run(dp.start_polling(bot))
