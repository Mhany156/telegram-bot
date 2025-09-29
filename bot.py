import os, re, time, hmac, hashlib, asyncio, html, threading
from functools import wraps
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.client.default import DefaultBotProperties
from flask import Flask, request, abort
import aiosqlite, aiohttp

# ==================== ENV ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

KASHIER_API_KEY     = os.getenv("KASHIER_API_KEY", "")
KASHIER_MERCHANT_ID = os.getenv("KASHIER_MERCHANT_ID", "")
KASHIER_SECRET      = os.getenv("KASHIER_SECRET", "")

PP_PERSONAL = os.getenv("KASHIER_PP_PERSONAL", "")
PP_SHARED   = os.getenv("KASHIER_PP_SHARED", "")
PP_LAPTOP   = os.getenv("KASHIER_PP_LAPTOP", "")

# سر لتوقيع ديب لينك (مش كافي لوحده؛ بنستخدمه مع تحقق API)
DEEPLINK_SECRET = os.getenv("DEEPLINK_SECRET", (KASHIER_SECRET or "changeme"))

if not TELEGRAM_TOKEN or ":" not in TELEGRAM_TOKEN:
    raise RuntimeError("Missing/invalid TELEGRAM_TOKEN in environment.")

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
flask_app = Flask(__name__)
BOT_LOOP = None

def escape(t: str) -> str:
    return html.escape(t or "")

# ==================== DB / MIGRATION ====================
DB_PATH = "store.db"

async def _table_columns(db, table: str):
    try:
        cur = await db.execute(f"PRAGMA table_info({table});")
        rows = await cur.fetchall()
        return [r[1] for r in rows]
    except Exception:
        return []

async def _migrate_old_stock_schema(db):
    cols = await _table_columns(db, "stock")
    if not cols:
        return
    if {"cap", "sold", "chosen_mode"}.issubset(set(cols)):
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS stock_v2(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            credential TEXT NOT NULL,
            chosen_mode TEXT CHECK(chosen_mode IN ('personal','shared','laptop')) NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            cap INTEGER NOT NULL DEFAULT 1,
            sold INTEGER NOT NULL DEFAULT 0,
            is_sold INTEGER NOT NULL DEFAULT 0
        );
    """)
    has_p = "p_price" in cols or "p_cap" in cols or "p_sold" in cols
    has_s = "s_price" in cols or "s_cap" in cols or "s_sold" in cols
    has_l = "l_price" in cols or "l_cap" in cols or "l_sold" in cols
    try:
        cur = await db.execute("SELECT * FROM stock;")
        old_rows = await cur.fetchall()
        name_to_idx = {name: idx for idx, name in enumerate(cols)}
        def get(row, name, default=None):
            i = name_to_idx.get(name)
            return (row[i] if i is not None else default) if row else default
        for row in old_rows:
            category = str(get(row, "category", "") or "")
            credential = str(get(row, "credential", "") or "")
            base_price = get(row, "price", 0.0) or 0.0
            chosen_old = get(row, "chosen_mode")
            inserted = False
            if chosen_old in ("personal","shared","laptop"):
                mode = chosen_old
                cap = 3 if mode=="shared" else 1
                await db.execute(
                    "INSERT INTO stock_v2(category,credential,chosen_mode,price,cap,sold,is_sold) VALUES(?,?,?,?,?,0,0)",
                    (category, credential, mode, float(base_price), cap)
                ); inserted=True
            if has_p:
                price = get(row, "p_price", None); cap = get(row, "p_cap", 1) or 1; sold = get(row, "p_sold", 0) or 0
                if price is not None:
                    await db.execute("INSERT INTO stock_v2(category,credential,chosen_mode,price,cap,sold,is_sold) VALUES(?,?,?,?,?,?,?)",
                                     (category, credential, "personal", float(price), int(cap), int(sold), 1 if sold>=cap else 0)); inserted=True
            if has_s:
                price = get(row, "s_price", None); cap = get(row, "s_cap", 3) or 3; sold = get(row, "s_sold", 0) or 0
                if price is not None:
                    await db.execute("INSERT INTO stock_v2(category,credential,chosen_mode,price,cap,sold,is_sold) VALUES(?,?,?,?,?,?,?)",
                                     (category, credential, "shared", float(price), int(cap), int(sold), 1 if sold>=cap else 0)); inserted=True
            if has_l:
                price = get(row, "l_price", None); cap = get(row, "l_cap", 1) or 1; sold = get(row, "l_sold", 0) or 0
                if price is not None:
                    await db.execute("INSERT INTO stock_v2(category,credential,chosen_mode,price,cap,sold,is_sold) VALUES(?,?,?,?,?,?,?)",
                                     (category, credential, "laptop", float(price), int(cap), int(sold), 1 if sold>=cap else 0)); inserted=True
            if not inserted and credential:
                await db.execute("INSERT INTO stock_v2(category,credential,chosen_mode,price,cap,sold,is_sold) VALUES(?,?,?,?,?,0,0)",
                                 (category, credential, "personal", float(base_price), 1))
        await db.execute("DROP TABLE IF EXISTS stock;")
        await db.execute("ALTER TABLE stock_v2 RENAME TO stock;")
        await db.commit()
    except Exception:
        await db.execute("DROP TABLE IF EXISTS stock;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                credential TEXT NOT NULL,
                chosen_mode TEXT CHECK(chosen_mode IN ('personal','shared','laptop')) NOT NULL,
                price REAL NOT NULL DEFAULT 0,
                cap INTEGER NOT NULL DEFAULT 1,
                sold INTEGER NOT NULL DEFAULT 0,
                is_sold INTEGER NOT NULL DEFAULT 0
            );
        """); await db.commit()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0
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
        await _migrate_old_stock_schema(db)
        await db.execute("UPDATE stock SET is_sold=1 WHERE sold>=cap;")
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

# ==================== كاشير: تحقق الدفع عبر API ====================
_ALLOWED_OK = {"paid", "success", "approved", "captured", "completed", "succeeded"}

def _json_has_paid(data):
    if isinstance(data, dict):
        for k,v in data.items():
            if isinstance(k,str) and k.lower() in ("status","paymentstatus","state","result"):
                try:
                    if str(v).lower() in _ALLOWED_OK: return True
                except Exception:
                    pass
            if _json_has_paid(v): return True
    elif isinstance(data, list):
        for it in data:
            if _json_has_paid(it): return True
    return False

async def kashier_verify_paid(merchant_order_id: str) -> bool:
    """نحاول نسأل أكثر من إندبوينت لحد ما نلاقي حالة SUCCESS/PAID."""
    headers = {}
    if KASHIER_SECRET:
        headers["Authorization"] = KASHIER_SECRET  # حسب لوحة Integrations
    params = {"merchantOrderId": merchant_order_id}
    endpoints = [
        "https://api.kashier.io/payments",
        "https://api.kashier.io/orders",
        "https://api.kashier.io/transactions",
    ]
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        for url in endpoints:
            try:
                async with sess.get(url, params=params, headers=headers) as r:
                    if r.status != 200:
                        continue
                    # أحياناً الـ content-type مش بيتظبط
                    try:
                        data = await r.json(content_type=None)
                    except Exception:
                        txt = await r.text()
                        if '"status"' not in txt:
                            continue
                        # محاولة بدائية
                        data = {"text": txt}
                    if _json_has_paid(data):
                        return True
            except Exception:
                continue
    return False

# ==================== USERS ====================
async def get_or_create_user(uid: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if row: return row[0]
        await db.execute("INSERT INTO users(user_id,balance) VALUES(?,0)", (uid,))
        await db.commit()
        return 0.0

# ==================== STOCK CORE ====================
async def add_stock_item_mode(category: str, mode: str, price: float, credential: str, cap: int | None):
    if mode not in ("personal", "shared", "laptop"):
        raise ValueError("Invalid mode")
    if mode == "shared" and (cap is None or cap <= 0): cap = 3
    if cap is None or cap <= 0: cap = 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO stock(category,credential,chosen_mode,price,cap,sold,is_sold) VALUES(?,?,?,?,?,0,0)",
            (category, credential, mode, price, cap)
        ); await db.commit()

async def list_categories_with_availability():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT category, COUNT(*)
            FROM stock
            WHERE is_sold=0 AND sold<cap
            GROUP BY category
            ORDER BY category
        """); return await cur.fetchall()

async def modes_availability_for(category: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT chosen_mode, COUNT(*)
            FROM stock
            WHERE category=? AND is_sold=0 AND sold<cap
            GROUP BY chosen_mode
        """,(category,)); rows = await cur.fetchall()
        return {m:c for m,c in rows}

async def list_stock_items(category: str, limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, chosen_mode, price, cap, sold, credential
            FROM stock
            WHERE category=? AND is_sold=0 AND sold<cap
            ORDER BY id ASC
            LIMIT ?
        """,(category, limit)); return await cur.fetchall()

async def clear_stock_category(category: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM stock WHERE category=?", (category,)); n = cur.rowcount or 0
        await db.commit(); return n

async def delete_stock_item(stock_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM stock WHERE id=?", (stock_id,)); n = cur.rowcount or 0
        await db.commit(); return n

async def find_item_with_mode(category: str, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, credential, price, cap, sold
            FROM stock
            WHERE category=? AND chosen_mode=? AND is_sold=0 AND sold<cap
            ORDER BY id ASC
            LIMIT 1
        """,(category, mode)); return await cur.fetchone()

async def increment_sale_and_finalize(stock_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE stock SET sold=sold+1 WHERE id=?", (stock_id,))
        await db.execute("UPDATE stock SET is_sold=1 WHERE id=? AND sold>=cap", (stock_id,)); await db.commit()

async def log_sale(user_id: int, stock_id: int, category: str, credential: str, price: float, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sales_history(user_id,stock_id,category,credential,price_paid,mode_sold) VALUES(?,?,?,?,?,?)",
            (user_id, stock_id, category, credential, price, mode)
        ); await db.commit()

async def get_instruction(category: str, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT message_text FROM instructions WHERE category=? AND mode=?", (category, mode))
        row = await cur.fetchone(); return row[0] if row else None

# ==================== USER COMMANDS (Deep-Link + تحقق API) ====================
PRETTY = {"personal":"فردي","shared":"مشترك","laptop":"لابتوب"}

@dp.message(Command("start"))
async def cmd_start(m: Message, command: CommandObject):
    await get_or_create_user(m.from_user.id)

    # Deep-Link: ok-<merchant_order_id>-<sig>
    if command.args and command.args.startswith("ok-"):
        try:
            payload = command.args[3:]
            ref, sig = payload.rsplit("-", 1)
            expected = hashlib.sha256((DEEPLINK_SECRET + ref).encode()).hexdigest()[:16]
            if sig != expected:
                await m.answer("رابط غير صالح."); return
            parts = ref.split("-", 4)  # buy-<uid>-<cat>-<mode>-<ts>
            if len(parts) < 5:
                await m.answer("معرّف العملية غير صحيح."); return
            uid = int(parts[1]); category = parts[2].replace("_"," "); mode = parts[3]
            if m.from_user.id != uid:
                await m.answer("هذا الرابط ليس لحسابك."); return

            # ✅ تحقق فعلي من كاشير
            ok = await kashier_verify_paid(ref)
            if not ok:
                await m.answer("لم أتمكن من التأكد من الدفع الآن. من فضلك انتظر ثواني وجرب الزر مرة أخرى، أو أرسل لي Order ID لو ظهر لك.")
                return

            row = await find_item_with_mode(category, mode)
            if not row:
                await m.answer("تم الدفع لكن المخزون انتهى مؤقتًا. سنسلمك أول ما يتوفر."); return
            stock_id, credential, price, cap, sold = row
            await increment_sale_and_finalize(stock_id)
            await log_sale(uid, stock_id, category, credential, price, mode)
            instructions = await get_instruction(category, mode) or ""
            msg = (f"✅ تم التأكيد.\n\n📦 <b>{escape(category)} — {escape(PRETTY.get(mode,mode))}</b>\n"
                   f"📩 <b>بياناتك:</b>\n<code>{escape(credential)}</code>")
            if instructions: msg += f"\n\n{instructions}"
            await m.answer(msg); return
        except Exception:
            await m.answer("حصل خطأ أثناء التأكيد."); return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍️ الكتالوج", callback_data="catalog")],
    ])
    await m.answer("أهلاً بك! اختر من الكتالوج:", reply_markup=kb)

@dp.message(Command("whoami"))
async def cmd_whoami(m: Message):
    await m.reply(f"👤 ID: {m.from_user.id}\nName: {m.from_user.full_name}")

# ==================== ADMIN COMMANDS ====================
def admin_only(func):
    @wraps(func)
    async def wrapper(m: Message, *args, **kwargs):
        if not is_admin(m.from_user.id): return
        return await func(m, *args, **kwargs)
    return wrapper

@dp.message(Command("stock"))
@admin_only
async def stock_cmd(m: Message):
    rows = await list_categories_with_availability()
    if not rows: await m.reply("لا يوجد مخزون متاح."); return
    await m.reply("\n".join(["المخزون المتاح:"]+[f"- {c}: {n} عنصر" for c,n in rows]))

@dp.message(Command("liststock"))
@admin_only
async def liststock_cmd(m: Message, command: CommandObject):
    if not command.args:
        cats = await list_categories_with_availability()
        await m.reply("استخدم: /liststock <category>\nالفئات:\n- " + "\n- ".join(c for c,_ in cats) if cats else "لا يوجد مخزون.")
        return
    cat = command.args.strip()
    rows = await list_stock_items(cat, 100)
    if not rows: await m.reply("لا يوجد عناصر لهذه الفئة."); return
    lines=[f"({cat}) العناصر المتاحة:"]+[f"ID={sid} | mode={mode} | {price}ج | {sold}/{cap} | {cred}" for sid,mode,price,cap,sold,cred in rows]
    await m.reply("\n".join(lines))

@dp.message(Command("clearstock"))
@admin_only
async def clearstock_cmd(m: Message, command: CommandObject):
    if not command.args: await m.reply("الاستخدام: /clearstock <category>"); return
    n = await clear_stock_category(command.args.strip()); await m.reply(f"🧹 تم حذف {n} عنصر.")

@dp.message(Command("delstock"))
@admin_only
async def delstock_cmd(m: Message, command: CommandObject):
    if not command.args: await m.reply("الاستخدام: /delstock <stock_id>"); return
    sid = parse_int_loose(command.args)
    if not sid:
        await m.reply("ID غير صالح"); return
    n = await delete_stock_item(sid); await m.reply(f"🗑️ تم حذف {n} عنصر.")

ADMIN_IMPORT_STATE = {}  # {uid: {"mode":"simple"|"multi"}}

@dp.message(Command("importstock"))
@admin_only
async def importstock_cmd(m: Message):
    ADMIN_IMPORT_STATE[m.from_user.id]={"mode":"simple"}
    await m.reply("📥 أرسل TXT أو الصق:\n<category> <price> <credential>\n— يخزن personal بسعة 1.")

@dp.message(Command("importstockm"))
@admin_only
async def importstockm_cmd(m: Message):
    ADMIN_IMPORT_STATE[m.from_user.id]={"mode":"multi"}
    await m.reply("📥 أرسل TXT أو الصق:\n<category> <mode> <price> <credential>\nالمودات: personal/shared/laptop (shared cap=3).")

@dp.message(F.document)
async def handle_import_doc(m: Message):
    if not is_admin(m.from_user.id) or m.from_user.id not in ADMIN_IMPORT_STATE: return
    st = ADMIN_IMPORT_STATE.pop(m.from_user.id)
    try:
        file = await bot.get_file(m.document.file_id)
        from io import BytesIO
        buf = BytesIO(); await bot.download(file, buf)
        text = buf.getvalue().decode("utf-8","ignore")
        await _process_import_text(m, text, st["mode"])
    except Exception as e:
        await m.reply(f"❌ فشل معالجة الملف: {e}")

@dp.message(F.text)
async def handle_text(m: Message):
    if is_admin(m.from_user.id) and m.from_user.id in ADMIN_IMPORT_STATE:
        st = ADMIN_IMPORT_STATE.pop(m.from_user.id)
        await _process_import_text(m, m.text or "", st["mode"]); return
    if m.text and m.text.startswith('/'):
        await m.reply("أمر غير معروف. اضغط /start.")

async def _process_import_text(m: Message, text: str, mode_flag: str):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    ok=bad=0
    for ln in lines:
        try:
            if mode_flag=="simple":
                parts = ln.split(maxsplit=2)
                if len(parts)<3: bad+=1; continue
                cat, price_s, cred = parts[0], parts[1], parts[2]
                price = parse_float_loose(price_s)
                if price is None: bad+=1; continue
                await add_stock_item_mode(cat, "personal", price, cred, 1); ok+=1
            else:
                parts = ln.split(maxsplit=3)
                if len(parts)<4: bad+=1; continue
                cat, mode, price_s, cred = parts[0], parts[1].lower(), parts[2], parts[3]
                if mode not in ("personal","shared","laptop"): bad+=1; continue
                price = parse_float_loose(price_s)
                if price is None: bad+=1; continue
                cap = 3 if mode=="shared" else 1
                await add_stock_item_mode(cat, mode, price, cred, cap); ok+=1
        except Exception:
            bad+=1
    await m.reply(f"✅ تم استيراد: {ok} عنصر.\n❌ فشل: {bad} سطر.")

# ==================== الكتالوج + زر الدفع + زر الاستلام ====================
@dp.callback_query(F.data=="catalog")
async def cb_catalog(c: CallbackQuery):
    cats = await list_categories_with_availability()
    if not cats: await c.message.edit_text("لا يوجد مخزون حالياً."); return
    rows = [[InlineKeyboardButton(text=f"{cat} ({cnt})", callback_data=f"cat::{cat}")] for cat,cnt in cats]
    rows.append([InlineKeyboardButton(text="🔄 تحديث", callback_data="catalog")])
    await c.message.edit_text("اختر الفئة:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("cat::"))
async def cb_category(c: CallbackQuery):
    PRETTY = {"personal":"فردي","shared":"مشترك","laptop":"لابتوب"}
    _, category = c.data.split("::",1)
    av = await modes_availability_for(category)
    buttons=[]
    for mode in ("shared","personal","laptop"):
        if av.get(mode):
            buttons.append([InlineKeyboardButton(text=f"💳 ادفع ({PRETTY[mode]})", callback_data=f"mode::{category}::{mode}")])
    if not buttons: await c.answer("لا يوجد عناصر لهذه الفئة الآن.", show_alert=True); return
    buttons.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="catalog")])
    await c.message.edit_text(f"الفئة: {category}\nاختر النمط:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("mode::"))
async def cb_pick_mode(c: CallbackQuery):
    PRETTY = {"personal":"فردي","shared":"مشترك","laptop":"لابتوب"}
    _, category, mode = c.data.split("::",2)
    row = await find_item_with_mode(category, mode)
    if not row: await c.answer("لا يوجد عنصر متاح لهذا النمط الآن.", show_alert=True); return
    stock_id, credential, price, cap, sold = row
    safe_cat = re.sub(r'[^a-zA-Z0-9_-]+','_', category)
    merchant_order_id = f"buy-{c.from_user.id}-{safe_cat}-{mode}-{int(time.time())}"

    pp_map = {"personal":PP_PERSONAL,"shared":PP_SHARED,"laptop":PP_LAPTOP}
    base_url = pp_map.get(mode,"")
    if not base_url: await c.answer("صفحة الدفع غير مجهزة.", show_alert=True); return
    sep = "&" if "?" in base_url else "?"
    pay_url = f"{base_url}{sep}ref={merchant_order_id}"

    me = await bot.get_me()
    sig = hashlib.sha256((DEEPLINK_SECRET + merchant_order_id).encode()).hexdigest()[:16]
    oklink = f"https://t.me/{me.username}?start=ok-{merchant_order_id}-{sig}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 ادفع {price:.2f} ج.م", url=pay_url)],
        [InlineKeyboardButton(text="✅ استلم بعد الدفع", url=oklink)],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data=f"cat::{category}")]
    ])
    await c.message.edit_text(
        f"الفئة: {escape(category)}\nالنمط: {PRETTY.get(mode,mode)}\nالسعر: {price:.2f} ج.م\nالسعة الحالية: {sold}/{cap}",
        reply_markup=kb
    )

# ==================== (اختياري) ويبهوك إن توفر لاحقًا ====================
def _kashier_verify_signature(raw: bytes, sig: str|None) -> bool:
    if not sig: return False
    sig = sig.lower()
    for key in (KASHIER_SECRET, KASHIER_API_KEY):
        if not key: continue
        calc = hmac.new(key.encode(), raw, hashlib.sha256).hexdigest().lower()
        if hmac.compare_digest(sig, calc):
            return True
    return False

@flask_app.route("/kashier-callback", methods=["POST"])
def kashier_callback():
    try:
        raw = request.get_data() or b""
        sig = request.headers.get("X-Kashier-Signature") or request.headers.get("Kashier-Signature") or request.headers.get("x-signature")
        if sig and not _kashier_verify_signature(raw, sig): return abort(400)
        payload = request.json or {}
        status = str(payload.get("status","") or payload.get("paymentStatus","")).lower()
        if status not in {"paid","success","approved","captured"}: return ("",200)
        ref = payload.get("reference") or payload.get("orderReference") or payload.get("merchantOrderId") or payload.get("ref")
        if not ref or not str(ref).startswith("buy-"): return ("",200)
        parts = str(ref).split("-", 4)
        if len(parts) < 5: return ("",200)
        user_id = int(parts[1]); category = parts[2].replace("_"," "); mode = parts[3]
        async def finalize():
            row = await find_item_with_mode(category, mode)
            if not row:
                await bot.send_message(user_id, "⚠️ تمت عملية الدفع لكن العنصر غير متاح حالياً."); return
            stock_id, credential, price, cap, sold = row
            await increment_sale_and_finalize(stock_id)
            await log_sale(user_id, stock_id, category, credential, price, mode)
            msg = f"✅ تم الدفع.\n\n📦 <b>{escape(category)} — {escape(PRETTY.get(mode, mode))}</b>\n📩 <b>بياناتك:</b>\n<code>{escape(credential)}</code>"
            await bot.send_message(user_id, msg)
        if BOT_LOOP and BOT_LOOP.is_running(): asyncio.run_coroutine_threadsafe(finalize(), BOT_LOOP)
        else: asyncio.run(finalize())
        return ("",200)
    except Exception as e:
        print("[KASHIER CALLBACK ERROR]", e); return abort(500)

# ==================== RUN ====================
async def main():
    global BOT_LOOP
    BOT_LOOP = asyncio.get_running_loop()
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    def run_flask():
        flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
