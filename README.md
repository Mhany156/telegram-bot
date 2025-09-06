
# Telegram Auto Accounts Bot (Balance Manual / Delivery Auto)

لغة: **العربية**

## الفكرة
بوت يبيع حسابات تلقائيًا (يسلّم بيانات الحساب مباشرة بعد الدفع من الرصيد). شحن الرصيد **يدوي** عبر الأدمِن.

## تشغيل محليًا
1) ثبّت المتطلبات:
```bash
pip install -r requirements.txt
```
2) انسخ `.env.example` إلى `.env` وعدّل القيم (أو استخدم متغيرات البيئة مباشرة):
```
TELEGRAM_TOKEN=توكن_البوت
ADMIN_IDS=123456789,987654321
```
> **ملاحظة**: صيغة ADMIN_IDS أرقام مستخدمين تليجرام للأدمِن مفصولة بفواصل.

3) شغّل:
```bash
python bot.py
```
سيظهر `store.db` تلقائيًا عند أول تشغيل.

## أو عبر Docker
```bash
docker build -t tg-acc-bot .
docker run -it --rm -e TELEGRAM_TOKEN="..." -e ADMIN_IDS="123,456" tg-acc-bot
```

## أو نشر سريع (Railway/Render)
- أنشئ خدمة Python جديدة
- ارفع الملفات (أو اربط الريبو)
- أضف متغيرات البيئة `TELEGRAM_TOKEN` و `ADMIN_IDS`
- شغّل الخدمة (Polling، لا تحتاج Webhook)

## أوامر مفيدة
- إضافة مخزون:
```
/addstock Netflix 3.5 email@example.com:pass123
```
- استعراض المخزون:
```
/stock
```
- شحن رصيد يدوي (من الأدمِن):
```
/addbal <user_id> <amount>
```
- رصيد العميل:
```
/balance
```

## بنية قاعدة البيانات (SQLite)
- users(user_id PK, balance)
- stock(id PK, category, price, credential, is_sold)
- orders(id PK, user_id, stock_id, price, created_at)

## ملاحظات أمان
- التسليم يتم في الخاص مع العميل (يوصل له الحساب مباشرة).
- خذ نسخة احتياطية من `store.db` دوريًا.
- تريد استيراد جماعي؟ ضع لكل سطر: `category|price|credential` وأضف أمرًا لاحقًا.

بالتوفيق! ✨
