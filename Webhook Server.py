import os
import asyncio
import hmac
import hashlib
import aiosqlite
from flask import Flask, request, abort
from dotenv import load_dotenv
from aiogram import Bot

# ==================== CONFIG ====================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
PAYMOB_HMAC_SECRET = os.getenv("PAYMOB_HMAC_SECRET")
DB_PATH = "store.db"

bot = Bot(token=TOKEN)
flask_app = Flask(__name__)

# ==================== DB Helpers (Simplified for Webhook) ====================
async def get_user_balance(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        r = await cur.fetchone()
        return float(r[0]) if r else 0.0

async def change_balance(user_id: int, delta: float) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        r = await cur.fetchone()
        if r is None:
            new_bal = delta
            await db.execute("INSERT INTO users(user_id,balance) VALUES(?,?)", (user_id, new_bal))
        else:
            new_bal = r[0] + delta
            await db.execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, user_id))
        await db.commit()
    return new_bal

# ==================== WEBHOOK LISTENER ====================
@flask_app.route('/webhook', methods=['GET', 'POST'])
def paymob_webhook():
    try:
        if request.method == 'GET':
            hmac_keys = sorted([key for key in request.args.keys() if key != 'hmac'])
            concatenated_string = "".join([request.args.get(key, '') for key in hmac_keys])
            
            received_hmac = request.args.get('hmac')
            if not received_hmac: return abort(400)
            
            h = hmac.new(PAYMOB_HMAC_SECRET.encode('utf-8'), concatenated_string.encode('utf-8'), hashlib.sha512)
            calculated_hmac = h.hexdigest()

            if not hmac.compare_digest(calculated_hmac, received_hmac):
                print(f"[WEBHOOK-GET] HMAC verification failed!")
                return abort(403)
            
            if request.args.get('success') == 'true':
                print("[WEBHOOK-GET] Received successful transaction response.")
                merchant_order_id = request.args.get('merchant_order_id')
                if merchant_order_id and merchant_order_id.startswith('tg-'):
                    parts = merchant_order_id.split('-')
                    user_id = int(parts[1])
                    amount_egp = float(request.args.get('amount_cents')) / 100
                    
                    # Run async functions in a new event loop
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    
                    new_balance = loop.run_until_complete(change_balance(user_id, amount_egp))
                    
                    confirmation_message = f"✅ تم شحن رصيدك بنجاح بمبلغ {amount_egp:g} ج.م.\nرصيدك الجديد هو: {new_balance:g} ج.м."
                    loop.run_until_complete(bot.send_message(user_id, confirmation_message))
                    loop.close()

            return ("Transaction processed, you can close this window.", 200)
        
        elif request.method == 'POST':
            print("[WEBHOOK-POST] Received POST callback. Ignoring as GET is primary.")
            return ('POST callback received', 200)

    except Exception as e:
        print(f"[WEBHOOK ERROR] An error occurred: {e}")
        return abort(500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)
