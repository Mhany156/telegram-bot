import os, re, time, hmac, hashlib, asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.utils.markdown import escape_md as escape
from flask import Flask, request, abort

# ==================== إعداد التوكن ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]

KASHIER_API_KEY = os.getenv("KASHIER_API_KEY") or ""
KASHIER_MERCHANT_ID = os.getenv("KASHIER_MERCHANT_ID") or ""
KASHIER_SECRET = os.getenv("KASHIER_SECRET") or ""

PP_PERSONAL = os.getenv("KASHIER_PP_PERSONAL")
PP_SHARED = os.getenv("KASHIER_PP_SHARED")
PP_LAPTOP = os.getenv("KASHIER_PP_LAPTOP")

bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
flask_app = Flask(__name__)

# ==================== الداتا الوهمية (بدل قاعدة البيانات) ====================
STOCK = {
    "مشترك": {"shared": [{"credential": "shared-user1:pass", "price": 50.0}]},
    "فردي": {"personal": [{"credential": "personal-user1:pass", "price": 100.0}]},
    "لابتوب": {"laptop": [{"credential": "laptop-key-123", "price": 200.0}]}
}
SALES_LOG = []

async def find_item_with_mode(category, mode):
    try:
        return STOCK[category][mode][0]
    except:
        return None

def price_for_mode(item, mode):
    return float(item.get("price", 0))

async def increment_sale_and_finalize(item, mode):
    # نزيل العنصر من الستوك كأنه اتباع
    for cat, modes in STOCK.items():
        if mode in modes and item in modes[mode]:
            modes[mode].remove(item)
            return True
    return False

async def log_sale(user_id, item, price, mode):
    SALES_LOG.append({"user": user_id, "item": item, "price": price, "mode": mode, "time": time.time()})

async def get_instruction(category, mode):
    return f"📌 شكراً لشرائك {category} نوع {mode}."

# ==================== أوامر البداية ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 كتالوج", callback_data="catalog")]
    ])
    await message.answer("أهلاً بك! اختر من الكتالوج:", reply_markup=kb)

@dp.callback_query(F.data == "catalog")
async def cb_catalog(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 مشترك", callback_data="cat::مشترك")],
        [InlineKeyboardButton(text="👤 فردي", callback_data="cat::فردي")],
        [InlineKeyboardButton(text="💻 لابتوب", callback_data="cat::لابتوب")]
    ])
    await c.message.edit_text("اختر الفئة:", reply_markup=kb)

@dp.callback_query(F.data.startswith("cat::"))
async def cb_category(c: CallbackQuery):
    _, category = c.data.split("::",1)
    modes = STOCK.get(category, {})
    rows = []
    for mode, items in modes.items():
        if items:
            rows.append([InlineKeyboardButton(text=f"{mode}", callback_data=f"mode::{category}::{mode}")])
    rows.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="catalog")])
    await c.message.edit_text(f"اختر النوع ({category}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("mode::"))
async def cb_pick_mode(c: CallbackQuery):
    _, category, mode = c.data.split("::", 2)
    item = await find_item_with_mode(category, mode)
    if not item:
        await c.answer("لا يوجد عنصر متاح حالياً", show_alert=True)
        return
    price = price_for_mode(item, mode)
    safe_cat = re.sub(r'[^a-zA-Z0-9_-]+', '_', category)
    merchant_order_id = f"buy-{c.from_user.id}-{safe_cat}-{mode}-{int(time.time())}"
    pp_map = {"personal": PP_PERSONAL, "shared": PP_SHARED, "laptop": PP_LAPTOP}
    base_url = pp_map.get(mode)
    if not base_url:
        await c.answer("صفحة الدفع غير مجهزة", show_alert=True)
        return
    pay_url = f"{base_url}?ref={merchant_order_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 ادفع {price:g} جنيه الآن", url=pay_url)],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data=f"cat::{category}")]
    ])
    await c.message.edit_text(f"الفئة: {category}\nالنوع: {mode}\nالسعر: {price:g} ج.م\nاضغط الدفع لإتمام العملية.", reply_markup=kb)

# ==================== ويبهوك كاشير ====================
def _kashier_verify_signature(raw_body: bytes, received_sig: str) -> bool:
    api_key = (KASHIER_API_KEY or "").encode("utf-8")
    if not api_key or not received_sig:
        return False
    calc = hmac.new(api_key, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received_sig.lower(), calc.lower())

@flask_app.route("/kashier-callback", methods=["POST"])
def kashier_callback():
    try:
        raw = request.get_data() or b""
        sig = request.headers.get("X-Kashier-Signature") or request.headers.get("Kashier-Signature") or request.headers.get("x-signature")
        if not _kashier_verify_signature(raw, sig):
            return abort(400)
        payload = request.json or {}
        status = str(payload.get("status", "")).lower()
        ref = payload.get("reference") or payload.get("orderReference") or payload.get("merchantOrderId") or payload.get("ref")
        if status != "paid" or not ref or not str(ref).startswith("buy-"):
            return ("",200)
        parts = str(ref).split("-", 4)
        if len(parts) < 5:
            return ("",200)
        user_id = int(parts[1])
        category = parts[2].replace("_"," ")
        mode = parts[3]
        async def finalize():
            row = await find_item_with_mode(category, mode)
            if not row:
                await bot.send_message(user_id, "⚠️ تمت عملية الدفع لكن العنصر غير متاح.")
                return
            price = price_for_mode(row, mode)
            ok = await increment_sale_and_finalize(row, mode)
            if not ok:
                await bot.send_message(user_id, "⚠️ العنصر نفد أثناء التخصيص.")
                return
            await log_sale(user_id, row, price, mode)
            credential = escape(row["credential"])
            instructions = await get_instruction(category, mode)
            msg = f"✅ تم الدفع.\n\n📦 <b>{escape(category)} — {escape(mode)}</b>\n📩 <b>بياناتك:</b>\n<code>{credential}</code>"
            if instructions:
                msg += f"\n\n{instructions}"
            await bot.send_message(user_id, msg, parse_mode="HTML")
        loop = dp.loop
        asyncio.run_coroutine_threadsafe(finalize(), loop)
        return ("",200)
    except Exception as e:
        print("[KASHIER CALLBACK ERROR]", e)
        return abort(500)

# ==================== تشغيل ====================
if __name__ == "__main__":
    import threading
    def run_flask():
        flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT",8080)))
    threading.Thread(target=run_flask, daemon=True).start()
    import asyncio
    asyncio.run(dp.start_polling(bot))
