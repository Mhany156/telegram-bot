

# ==================== Multi-mode Import (TXT or paste) ====================
def parse_stockm_lines(text: str):
    """
    Each line format (spaces allowed inside credential):
    <category> <p_price> <p_cap> <s_price> <s_cap> <l_price> <l_cap> <credential...>
    Lines starting with # are ignored.
    Returns: (rows, ok, fail) where rows is list of tuples:
      (category, p_price, p_cap, s_price, s_cap, l_price, l_cap, credential)
    """
    results = []; ok = fail = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=7)
        if len(parts) < 8:
            fail += 1; continue
        category = parts[0]
        def pf(x): 
            from re import search
            x = normalize_digits(x).replace(",", ".")
            m = search(r'[-+]?\d+(?:\.\d+)?', x)
            return float(m.group(0)) if m else None
        def pi(x):
            from re import search
            x = normalize_digits(x)
            m = search(r'\d+', x)
            return int(m.group(0)) if m else None
        p_price = pf(parts[1]); p_cap = pi(parts[2])
        s_price = pf(parts[3]); s_cap = pi(parts[4])
        l_price = pf(parts[5]); l_cap = pi(parts[6])
        credential = parts[7]
        if any(v is None for v in [p_price, p_cap, s_price, s_cap, l_price, l_cap]):
            fail += 1; continue
        results.append((category, p_price, p_cap, s_price, s_cap, l_price, l_cap, credential))
        ok += 1
    return results, ok, fail

@dp.message(Command("importstockm"))
async def importstockm_cmd(m: Message):
    if not is_admin(m.from_user.id):
        await m.reply("❌ هذا الأمر للأدمن فقط."); return
    await m.reply(
        "📥 أرسل ملف TXT أو الصق السطور بهذا التنسيق (سطر لكل حساب):\n"
        "<category> <p_price> <p_cap> <s_price> <s_cap> <l_price> <l_cap> <credential>\n\n"
        "مثال CapCut لابتوب فقط (سعر 4$, سعة 2):\n"
        "CapCut 0 0 0 0 4 2 email@example.com:pass123\n\n"
        "تلميحات:\n"
        "- تقدر تسيب أي مود مقفول بسعة 0.\n"
        "- الأرقام العربية والفاصلة 3,5 مدعومة."
    )
    dp.workflow_state = {"awaiting_importm": {"admin": m.from_user.id}}

@dp.message(F.document)
async def import_file_multi_or_legacy(m: Message):
    # This overrides the previous F.document handler; we merge both flows here.
    st = getattr(dp, "workflow_state", {})
    w_m = st.get("awaiting_importm")
    w_s = st.get("awaiting_import")
    if not (w_m or w_s):
        return
    if not is_admin(m.from_user.id):
        return
    if w_m and w_m.get("admin") != m.from_user.id:
        return
    if w_s and w_s.get("admin") != m.from_user.id:
        return

    doc: Document = m.document
    if not (doc.mime_type == "text/plain" or (doc.file_name and doc.file_name.lower().endswith(".txt"))):
        await m.reply("⚠️ من فضلك أرسل ملف .txt فقط."); return

    try:
        file = await bot.get_file(doc.file_id)
        from io import BytesIO
        buf = BytesIO()
        await bot.download(file, buf)
        text = buf.getvalue().decode("utf-8", "ignore")
    except Exception as e:
        await m.reply(f"❌ فشل تنزيل الملف: {e}"); return

    if w_m:
        rows, ok, fail = parse_stockm_lines(text)
        for category, p_price, p_cap, s_price, s_cap, l_price, l_cap, credential in rows:
            await add_stock_row_modes(category, credential, p_price, p_cap, s_price, s_cap, l_price, l_cap)
        await m.reply(f"✅ تم استيراد {ok} سطر (مودات). ❌ فشل {fail} سطر.")
        dp.workflow_state = {}
        return

    # legacy simple import
    rows, ok, fail = parse_stock_lines(text)
    for category, price, credential in rows:
        await add_stock_simple(category, price, credential)
    await m.reply(f"✅ تم استيراد {ok} سطر. ❌ فشل {fail} سطر.")
    dp.workflow_state = {}

@dp.message()
async def pasted_multi_or_legacy(m: Message):
    st = getattr(dp, "workflow_state", {})
    w_m = st.get("awaiting_importm")
    if w_m and w_m.get("admin") == m.from_user.id and is_admin(m.from_user.id):
        rows, ok, fail = parse_stockm_lines(m.text or "")
        for category, p_price, p_cap, s_price, s_cap, l_price, l_cap, credential in rows:
            await add_stock_row_modes(category, credential, p_price, p_cap, s_price, s_cap, l_price, l_cap)
        await m.reply(f"✅ تم استيراد {ok} سطر (مودات). ❌ فشل {fail} سطر.")
        dp.workflow_state = {}
        return
    # fall back to any existing pasted flows (e.g., legacy import)
    # (Existing handler may also catch; keeping this ensures multi-mode works)
