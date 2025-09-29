import os, re, time, hmac, hashlib, asyncio, html, threading
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
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

def escape(t:str)->str: return html.escape(t or "")

# ==================== DB ====================
DB_PATH = "store.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0
        );""")
        # كل صف = كريدنشال لنمط واحد (personal/shared/laptop) مع سعة (cap) وعدّاد (sold)
        await db.execute("""CREATE TABLE IF NOT EXISTS stock(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            credential TEXT NOT NULL,
            chosen_mode TEXT CHECK(chosen_mode IN ('personal','shared','laptop')) NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            cap INTEGER NOT NULL DEFAULT 1,
            sold INTEGER NOT NULL DEFAULT 0,
            is_sold INTEGER NOT NULL DEFAULT 0
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
        await db.execute("UPDATE stock SET is_sold=1 WHERE sold>=cap;")
        await db.commit()

# ==================== HELPERS ====================
def is_admin(uid:int)->bool: return uid in ADMIN_IDS
def normalize_digits(s:str)->str: return s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩","0123456789"))
def parse_float_loose(s:str):
    if not s: return None
    s = normalize_digits(s).replace(",", ".")
    m = re.search(r'[-+]?\d+(?:\.\d+)?', s)
    return float(m.group(0)) if m else None
def parse_int_loose(s:str):
    if not s: return None
    s = normalize_digits(s)
    m = re.search(r'\d{1,12}', s)
    return int(m.group(0)) if m else None

# ==================== USERS ====================
async def get_or_create_user(uid:int)->float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?",(uid,))
        row = await cur.fetchone()
        if row: return row[0]
        await db.execute("INSERT INTO users(user_id,balance) VALUES(?,0)",(uid,))
        await db.commit()
        return 0.0

# ==================== STOCK CORE ====================
async def add_stock_item_mode(category:str, mode:str, price:float, credential:str, cap:int|None):
    if mode not in ("personal","shared","laptop"):
        raise ValueError("Invalid mode")
    if mode=="shared" and (cap is None or cap<=0): cap = 3
    if cap is None or cap<=0: cap = 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO stock(category,credential,chosen_mode,price,cap,sold,is_sold) VALUES(?,?,?,?,?,0,0)",
            (category, credential, mode, price, cap)
        )
        await db.commit()

async def list_categories_with_availability():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT category, COUNT(*)
            FROM stock
            WHERE is_sold=0 AND sold<cap
            GROUP BY category
            ORDER BY category
        """)
        return await cur.fetchall()

async def modes_availability_for(category:str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT chosen_mode, COUNT(*)
            FROM stock
            WHERE category=? AND is_sold=0 AND sold<cap
            GROUP BY chosen_mode
        """,(category,))
        rows = await cur.fetchall()
        return {m:c for m,c in rows}

async def list_stock_items(category:str, limit:int=50):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, chosen_mode, price, cap, sold, credential
            FROM stock
            WHERE category=? AND is_sold=0 AND sold<cap
            ORDER BY id ASC
            LIMIT ?
        """,(category, limit))
        return await cur.fetchall()

async def clear_stock_category(category:str)->int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM stock WHERE category=?", (category,))
        n = cur.rowcount or 0
        await db.commit(); return n

async def delete_stock_item(stock_id:int)->int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM stock WHERE id=?", (stock_id,))
        n = cur.rowcount or 0
        await db.commit(); return n

async def find_item_with_mode(category:str, mode:str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, credential, price, cap, sold
            FROM stock
            WHERE category=? AND chosen_mode=? AND is_sold=0 AND sold<cap
            ORDER BY id ASC
            LIMIT 1
        """,(category, mode))
        return await cur.fetchone()

async def increment_sale_and_finalize(stock_id:int)->None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE stock SET sold=sold+1 WHERE id=?", (stock_id,))
        await db.execute("UPDATE stock SET is_sold=1 WHERE id=? AND sold>=cap", (stock_id,))
        await db.commit()

async def log_sale(user_id:int, stock_id:int, category:str, credential:str, price:float, mode:str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sales_history(user_id,stock_id,category,credential,price_paid,mode_sold) VALUES(?,?,?,?,?,?)",
            (user_id, stock_id, category, credential, price, mode)
        )
        await db.commit()

async def get_instruction(category, mode):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT message_text FROM instructions WHERE category=? AND mode=?",(category,mode))
        row = await cur.fetchone()
        return row[0] if row else None

# ==================== USER COMMANDS ====================
@dp.message(Command("start"))
async def cmd_start(m:Message):
    await get_or_create_user(m.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍️ الكتالوج", callback_data="catalog")],
    ])
    await m.answer("أهلاً بك! اختر من الكتالوج:", reply_markup=kb)

@dp.message(Command("whoami"))
async def cmd_whoami(m:Message):
    await m.reply(f"👤 ID: {m.from_user.id}\nName: {m.from_user.full_name}")

# ==================== ADMIN COMMANDS ====================
def admin_only(func):
    async def wrapper(m:Message, *a, **kw):
        if not is_admin(m.from_user.id): return
        return await func(m,*a,**kw)
    return wrapper

@dp.message(Command("stock"))
@admin_only
async def stock_cmd(m:Message):
    rows = await list_categories_with_availability()
    if not rows:
        await m.reply("لا يوجد مخزون متاح."); return
    lines = ["المخزون المتاح:"]
    for cat, cnt in rows: lines.append(f"- {cat}: {cnt} عنصر")
    await m.reply("\n".join(lines))

@dp.message(Command("liststock"))
@admin_only
async def liststock_cmd(m:Message, command:CommandObject):
    if not command.args:
        cats = await list_categories_with_availability()
        if not cats: await m.reply("لا يوجد مخزون."); return
        await m.reply("استخدم: /liststock <category>\nالفئات المتاحة:\n- " + "\n- ".join(c for c,_ in cats))
        return
    cat = command.args.strip()
    rows = await list_stock_items(cat, 100)
    if not rows:
        await m.reply("لا يوجد عناصر لهذه الفئة."); return
    lines = [f"({cat}) العناصر المتاحة:"]
    for sid, mode, price, cap, sold, cred in rows:
        lines.append(f"ID={sid} | mode={mode} | {price}ج | {sold}/{cap} | {cred}")
    await m.reply("\n".join(lines))

@dp.message(Command("clearstock"))
@admin_only
async def clearstock_cmd(m:Message, command:CommandObject):
    if not command.args: await m.reply("الاستخدام: /clearstock <category>"); return
    n = await clear_stock_category(command.args.strip())
    await m.reply(f"🧹 تم حذف {n} عنصر.")

@dp.message(Command("delstock"))
@admin_only
async def delstock_cmd(m:Message, command:CommandObject):
    if not command.args: await m.reply("الاستخدام: /delstock <stock_id>"); return
    sid = parse_int_loose(command.args)
    if not sid: await m.reply("ID غير صالح"); return
    n = await delete_stock_item(sid)
    await m.reply(f"🗑️ تم حذف {n} عنصر.")

# ===== استيراد المخزون =====
ADMIN_IMPORT_STATE = {}  # {uid: {"mode":"simple"|"multi"}}

@dp.message(Command("importstock"))
@admin_only
async def importstock_cmd(m:Message):
    ADMIN_IMPORT_STATE[m.from_user.id] = {"mode":"simple"}
    await m.reply(
        "📥 أرسل ملف TXT أو الصق سطور بهذا الشكل:\n"
        "<category> <price> <credential>\n"
        "— يتم تخزينها كنمط personal بسعة 1."
    )

@dp.message(Command("importstockm"))
@admin_only
async def importstockm_cmd(m:Message):
    ADMIN_IMPORT_STATE[m.from_user.id] = {"mode":"multi"}
    await m.reply(
        "📥 أرسل ملف TXT أو الصق سطور بهذا الشكل:\n"
        "<category> <mode> <price> <credential>\n"
        "المودات: personal | shared | laptop\n"
        "— shared سعتها cap=3 تلقائيًا."
    )

@dp.message(F.document)
async def handle_import_doc(m:Message):
    if not is_admin(m.from_user.id): return
    st = ADMIN_IMPORT_STATE.get(m.from_user.id)
    if not st: return
    try:
        file = await bot.get_file(m.document.file_id)
        from io import BytesIO
        buf = BytesIO(); await bot.download(file, buf)
        text = buf.getvalue().decode("utf-8","ignore")
    except Exception as e:
        await m.reply(f"❌ فشل تنزيل الملف: {e}"); return
    await _process_import_text(m, text, st["mode"])
    ADMIN_IMPORT_STATE.pop(m.from_user.id, None)

@dp.message()
async def handle_import_text(m:Message):
    if not is_admin(m.from_user.id): return
    st = ADMIN_IMPORT_STATE.get(m.from_user.id)
    if not st or not m.text: return
    await _process_import_text(m, m.text, st["mode"])
    ADMIN_IMPORT_STATE.pop(m.from_user.id, None)

async def _process_import_text(m:Message, text:str, mode_flag:str):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    ok, bad = 0, 0
    for ln in lines:
        try:
            if mode_flag=="simple":
                parts = ln.split(maxsplit=2)
                if len(parts)<3: bad+=1; continue
                cat = parts[0]
                price = parse_float_loose(parts[1]); cred = parts[2]
                if price is None: bad+=1; continue
                await add_stock_item_mode(cat, "personal", price, cred, cap=1)
                ok+=1
            else:
                parts = ln.split(maxsplit=3)
                if len(parts)<4: bad+=1; continue
                cat = parts[0]; mode = parts[1].lower()
                price = parse_float_loose(parts[2]); cred = parts[3]
                if mode not in ("personal","shared","laptop"): bad+=1; continue
                if price is None: bad+=1; continue
                cap = 3 if mode=="shared" else 1
                await add_stock_item_mode(cat, mode, price, cred, cap)
                ok+=1
        except Exception:
            bad+=1
    await m.reply(f"✅ تم استيراد: {ok} عنصر.\n❌ فشل: {bad} سطر.")

# ==================== CATALOG / PAYMENT ====================
PRETTY = {"personal":"فردي","shared":"مشترك","laptop":"لابتوب"}

@dp.callback_query(F.data=="catalog")
async def cb_catalog(c:CallbackQuery):
    cats = await list_categories_with_availability()
    if not cats:
        await c.message.edit_text("لا يوجد مخزون حالياً.")
        return
    rows = []
    for cat, cnt in cats:
        rows.append([InlineKeyboardButton(text=f"{cat} ({cnt})", callback_data=f"cat::{cat}")])
    rows.append([InlineKeyboardButton(text="🔄 تحديث", callback_data="catalog")])
    await c.message.edit_text("اختر الفئة:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("cat::"))
async def cb_category(c:CallbackQuery):
    _, category = c.data.split("::",1)
    av = await modes_availability_for(category)
    buttons = []
    for mode in ("shared","personal","laptop"):
        if av.get(mode):
            buttons.append([InlineKeyboardButton(text=f"💳 ادفع ({PRETTY[mode]})", callback_data=f"mode::{category}::{mode}")])
    if not buttons:
        await c.answer("لا يوجد عناصر لهذه الفئة الآن.", show_alert=True); return
    buttons.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="catalog")])
    await c.message.edit_text(f"الفئة: {category}\nاختر النمط:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("mode::"))
async def cb_pick_mode(c:CallbackQuery):
    _, category, mode = c.data.split("::",2)
    row = await find_item_with_mode(category, mode)
    if not row:
        await c.answer("لا يوجد عنصر متاح لهذا النمط الآن.", show_alert=True); return
    stock_id, credential, price, cap, sold = row
    safe_cat = re.sub(r'[^a-zA-Z0-9_-]+', '_', category)
    merchant_order_id = f"buy-{c.from_user.id}-{safe_cat}-{mode}-{int(time.time())}"
    pp_map = {"personal":PP_PERSONAL,"shared":PP_SHARED,"laptop":PP_LAPTOP}
    base_url = pp_map.get(mode,"")
    if not base_url:
        await c.answer("صفحة الدفع غير مجهزة.", show_alert=True); return
    sep = "&" if "?" in base_url else "?"
    pay_url = f"{base_url}{sep}ref={merchant_order_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 ادفع {price:.2f} ج.م", url=pay_url)],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data=f"cat::{category}")]
    ])
    await c.message.edit_text(
        f"الفئة: {escape(category)}\nالنمط: {PRETTY.get(mode,mode)}\nالسعر: {price:.2f} ج.م\n"
        f"السعة الحالية: {sold}/{cap}",
        reply_markup=kb
    )

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
        sig = request.headers.get("X-Kashier-Signature") or request.headers.get("Kashier-Signature") or request.headers.get("x-signature")
        if not _kashier_verify_signature(raw, sig): return abort(400)
        payload = request.json or {}
        status = str(payload.get("status","")).lower()
        ref = payload.get("reference") or payload.get("orderReference") or payload.get("merchantOrderId") or payload.get("ref")
        if status!="paid" or not ref or not str(ref).startswith("buy-"): return ("",200)
        parts = str(ref).split("-", 4)  # ["buy", uid, cat, mode, ts]
        if len(parts)<5: return ("",200)
        user_id = int(parts[1]); category = parts[2].replace("_"," "); mode = parts[3]
        async def finalize():
            row = await find_item_with_mode(category, mode)
            if not row:
                await bot.send_message(user_id, "⚠️ تمت عملية الدفع لكن العنصر غير متاح حالياً."); return
            stock_id, credential, price, cap, sold = row
            await increment_sale_and_finalize(stock_id)
            await log_sale(user_id, stock_id, category, credential, price, mode)
            instructions = await get_instruction(category, mode) or ""
            msg = f"✅ تم الدفع.\n\n📦 <b>{escape(category)} — {escape(PRETTY.get(mode,mode))}</b>\n📩 <b>بياناتك:</b>\n<code>{escape(credential)}</code>"
            if instructions: msg += f"\n\n{instructions}"
            await bot.send_message(user_id, msg)
        asyncio.run_coroutine_threadsafe(finalize(), dp.loop)
        return ("",200)
    except Exception as e:
        print("[KASHIER CALLBACK ERROR]", e); return abort(500)

# ==================== RUN ====================
if __name__=="__main__":
    asyncio.get_event_loop().run_until_complete(init_db())
    def run_flask():
        flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(dp.start_polling(bot))
