"""
Telebirr Payment Verification Bot — TEST BUILD
የተሰራው ለ Render.com deploy ሙከራ ነው፦ ቴሌብር ከ ሃገር ውጭ ካሉ ሰርቨሮች (foreign IP)
የክፍያ ማረጋገጫ ግንኙነት ይቀበል/አይቀበል የሚለውን ለመፈተሽ።

የክፍያ ማረጋገጫ ዘዴ: transactioninfo.ethiotelecom.et/receipt/<TID> ገጽ ላይ  ያለውን
HTML ስክሬፕ በማድረግ (ተመሳሳይ ዘዴ ዋናው mule_vip ቦት የሚጠቀመው)።
"""
import asyncio
import io
import logging
import os
import re

import pdfplumber
import requests
from aiohttp import web
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable ያስፈልጋል (Render dashboard ላይ ያስቀምጡ)")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher()

# relay.py የሚያስኬደው ኮምፒውተርዎ (ኢትዮጵያ ውስጥ) ላይ cloudflared tunnel ሲፈጠር
# የሚገኘውን https://xxxxx.trycloudflare.com URL እዚህ Render Environment variable
# TELEBIRR_RELAY_URL ብለው ያስቀምጡ። ካልተቀመጠ ቀጥታ (ምናልባት ታግዶ የሚቀር) direct fetch ይሞክራል።
RELAY_URL = os.getenv("TELEBIRR_RELAY_URL", "").rstrip("/")


# ==========================================================================
# Telebirr receipt scraping — transactioninfo.ethiotelecom.et
# (RELAY_URL ከተቀመጠ በኩል ያልፋል - relay.py ኢትዮጵያ ውስጥ ካለ ኮምፒውተር ስለሚጠይቅ ችግር የለውም)
# ==========================================================================
def _direct_fetch(tid: str) -> dict:
    url = f"https://transactioninfo.ethiotelecom.et/receipt/{tid}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    data: dict = {}

    def pick(label_regex: str, key: str):
        node = soup.find(string=re.compile(label_regex, re.I))
        if node:
            td = node.find_next("td")
            if td:
                data[key] = td.get_text(strip=True)

    pick(r"Payer\s*Name", "payer_name")
    pick(r"Payer\s*telebirr", "payer_number")
    pick(r"Credited\s*Party\s*name", "credited_party")
    pick(r"Credited\s*party\s*account\s*no", "credited_party_number")
    pick(r"transaction\s*status", "status")
    pick(r"Total\s*Paid\s*Amount", "total_paid")
    return data


def _relay_fetch(tid: str) -> dict:
    resp = requests.get(f"{RELAY_URL}/verify", params={"tid": tid}, timeout=25)
    resp.raise_for_status()
    return resp.json().get("data", {})


def extract_tele_receipt_data(tid: str) -> dict:
    if RELAY_URL:
        return _relay_fetch(tid)
    return _direct_fetch(tid)


# ==========================================================================
# CBE receipt scraping — apps.cbe.com.et (PDF-based, ደረሰኙ HTML ሳይሆን PDF ነው)
# CBE ራሱ ኢትዮጵያ ውስጥ ካለ ሰርቨር ስለሆነ RELAY_URL አያስፈልገውም — ቀጥታ ይፈለጋል።
# ==========================================================================
# የተከለሉ የመለያ ቁጥሮች ብዙ ጊዜ እንደዚህ ይታያሉ: "1****0388", "E****0020"
CBE_MASKED_ACCOUNT_RE = r"\b[A-Za-z0-9]\*{2,}\d+\b"

CBE_REMAINING_LABELS = [
    ("date", r"Payment\s*Date\s*&\s*Time"),
    ("reference", r"Reference\s*No\.?\s*\(VAT\s*Invoice\s*No\)"),
    ("reason", r"Reason\s*/\s*Type\s*of\s*service"),
    ("transferred_amount", r"Transferred\s*Amount"),
    ("commission", r"Commission\s*or\s*Service\s*Charge"),
    ("vat_on_commission", r"15%\s*VAT\s*on\s*Commission"),
    ("total_paid", r"Total\s*amount\s*debited\s*from\s*customers?\s*account"),
    ("amount_in_word", r"Amount\s*in\s*Word"),
]


def _extract_cbe_pdf_fields(text: str) -> dict:
    """CBE ደረሰኝ PDF ላይ ያሉትን label→value ጥንዶች ያወጣል።
    ለከፋይ/ተቀባይ ስም እና አካውንት 'Account' የሚለውን label ቃል በቀጥታ ከመፈለግ ይልቅ
    የተከለለ የመለያ ቁጥር pattern (ለምሳሌ 1****0388) ራሱን እንፈልጋለን — ምክንያቱም አንዳንድ
    የተቀባይ ስሞች ('Inter Bank Account to Account...') ራሳቸው 'Account' የሚለውን
    ቃል ስለያዙ በቀላል label ፍለጋ ግራ ሊጋቡ ይችላሉ።"""
    text = re.sub(r"[ \t]+", " ", text)
    data: dict = {}

    account_matches = list(re.finditer(CBE_MASKED_ACCOUNT_RE, text))
    payer_m = re.search(r"Payer", text, re.I)
    receiver_m = re.search(r"Receiver", text, re.I)

    def _clean_name(raw_name: str) -> str:
        # ስሙ ካለቀ በኋላ 'Account' የሚለው label ራሱ (ከመለያ ቁጥሩ በፊት ያለው) ተጣብቆ ስለሚቀር እናስወግደዋለን
        raw_name = re.sub(r"\s*Account\s*$", "", raw_name, flags=re.I)
        return raw_name.strip(" \n\t:-")

    if payer_m and account_matches:
        data["payer_name"] = _clean_name(text[payer_m.end():account_matches[0].start()])
        data["payer_account"] = account_matches[0].group().strip()
    if receiver_m and len(account_matches) >= 2:
        data["receiver_name"] = _clean_name(text[receiver_m.end():account_matches[1].start()])
        data["receiver_account"] = account_matches[1].group().strip()

    # ቀሪዎቹ labels ልዩ (unique) ስለሆኑ በተከታታይ (sequential) ፍለጋ በአስተማማኝ ይወጣሉ
    cursor = account_matches[1].end() if len(account_matches) >= 2 else (receiver_m.end() if receiver_m else 0)
    positions = []
    for key, label_pattern in CBE_REMAINING_LABELS:
        m = re.search(label_pattern, text[cursor:], re.I)
        if m:
            start = cursor + m.start()
            end = cursor + m.end()
            positions.append((start, end, key))
            cursor = end

    for i, (start, end, key) in enumerate(positions):
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        value = text[end:next_start].strip(" \n\t:-")
        if value:
            data[key] = value
    return data


def extract_cbe_receipt_data(url: str) -> dict:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "").lower()

    if "pdf" in content_type or resp.content.startswith(b"%PDF"):
        text = ""
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
        return _extract_cbe_pdf_fields(text)

    # PDF ካልሆነ (ለምሳሌ mbreciept.cbe.com.et አንዳንዴ HTML ገጽ ሊመልስ ይችላል) —
    # ተመሳሳይ labels ስላሉት ጽሁፉን ከ HTML ላይ አውጥተን በዚያው field-parser እናልፍበታለን
    if "html" in content_type or resp.text.strip().startswith("<"):
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator="\n")
        return _extract_cbe_pdf_fields(text)

    # ልክ ያልሆነ reference/account ሲላክ CBE ብዙ ጊዜ ያልታወቀ/ባዶ ምላሽ ይመልሳል
    return {}


def _parse_cbe_input(raw: str):
    """ተጠቃሚው ከላከው ጽሁፍ የ CBE ደረሰኝ verification URL ለማውጣት ይሞክራል።
    አራት አይነት ግቤቶችን ይደግፋል፦
      1) ማንኛውም *.cbe.com.et ደረሰኝ ሊንክ (ለምሳሌ apps.cbe.com.et:100/?id=... ወይም
         mbreciept.cbe.com.et/v2-... የመሳሰሉ የተለያዩ ቅርጾች ሊኖሩት ይችላሉ — CBE ራሱ
         ከጊዜ ወደ ጊዜ ቅርጹን ስለሚቀይር domain/path ላይ ብቻ ሳንወሰን ማንኛውንም cbe.com.et
         ሊንክ እንይዛለን)
      2) ብቻውን የተላከ ID string (reference+account suffix ተጣምረው): FT....12345678
      3) reference እና account suffix በክፍተት ተለያይተው: FT.... 12345678
    አልተገኘም ከሆነ None ይመልሳል፣ ካልሆነ ሙሉ URL (ለማምጣት ዝግጁ) ይመልሳል።"""
    raw = raw.strip()

    m = re.search(r"https?://\S*\bcbe\.com\.et\S*", raw, re.I)
    if m:
        return m.group(0).rstrip(").,;]}\u2019\u201d\"'")

    parts = raw.split()
    if len(parts) == 2 and re.match(r"^FT[A-Za-z0-9]{6,}$", parts[0], re.I) and re.match(r"^\d{6,}$", parts[1]):
        ref, acct = parts[0].upper(), parts[1]
        return f"https://apps.cbe.com.et:100/?id={ref}{acct[-8:]}"

    if len(parts) == 1 and re.match(r"^FT[A-Za-z0-9]{10,}$", parts[0], re.I):
        return f"https://apps.cbe.com.et:100/?id={parts[0].upper()}"

    return None



# ==========================================================================
# Handlers
# ==========================================================================
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "ሰላም! 👋\n\n"
        "ይህ *የሙከራ ቦት* ነው — ደረሰኙን አረጋግጣለሁ፦\n\n"
        "📲 *Telebirr*፦ TID ብቻ ላኩ (ለምሳሌ `CHQ0FJ403O`)\n\n"
        "🏦 *CBE*፦ ከሚከተሉት አንዱን ላኩ፦\n"
        "  • ደረሰኙ ላይ ያለውን ሙሉ ሊንክ (`https://apps.cbe.com.et:100/?id=...`)\n"
        "  • ብቻውን `FT` reference + የመለያ ቁጥርዎ የመጨረሻ 8 አሃዝ በክፍተት ተለያይተው (ለምሳሌ `FT25211G11JQ 21827223`)\n\n"
        "_(ስክሪንሾት/OCR በዚህ የሙከራ ስሪት ውስጥ የለም — በጽሁፍ ብቻ ላኩ)_"
    )


@dp.message(F.text)
async def handle_text(message: Message):
    raw = (message.text or "").strip()
    if not raw or raw.startswith("/"):
        return

    # መጀመሪያ CBE ግቤት ስለመሆኑ እንፈትሻለን (ክፍተት ያለው reference+account ሊኖረው ስለሚችል
    # whitespace ከመፋቅ በፊት መፈተሽ አለበት)
    cbe_url = _parse_cbe_input(raw)
    if cbe_url:
        await handle_cbe(message, cbe_url)
        return

    tid = re.sub(r"\s+", "", raw)
    await handle_telebirr(message, tid)


async def handle_telebirr(message: Message, tid: str):
    processing = await message.answer("🔄 ደረሰኙን በማረጋገጥ ላይ ነኝ (Telebirr)...")

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, extract_tele_receipt_data, tid)
    except requests.exceptions.Timeout:
        await processing.delete()
        hint = (
            "⚠️ relay.py ኮምፒውተርዎ ላይ እየሰራ አለመሆኑን/cloudflared tunnel ክፍት መሆኑን ያረጋግጡ።"
            if RELAY_URL else
            "⚠️ ይሄ ማለት ቴሌብር ይህን ሰርቨር (Render) IP ገድቦ ይሆናል።"
        )
        await message.answer(
            f"❌ *Timeout*: ምላሽ አልተገኘም።\nTID: `{tid}`\n\n{hint}"
        )
        return
    except requests.exceptions.ConnectionError as e:
        await processing.delete()
        hint = (
            "⚠️ relay.py/cloudflared ክፍት መሆኑን ያረጋግጡ (RELAY_URL ትክክል ስለመሆኑ ያረጋግጡ)።"
            if RELAY_URL else
            "⚠️ ይሄ ማለት ቴሌብር ይህን ሰርቨር (Render) IP ገድቦ ይሆናል።"
        )
        await message.answer(
            f"❌ *Connection Error*: ግንኙነት አልተሳካም።\nTID: `{tid}`\nError: `{e}`\n\n{hint}"
        )
        return
    except requests.exceptions.HTTPError as e:
        await processing.delete()
        code = e.response.status_code if e.response is not None else "?"
        await message.answer(f"❌ HTTP Error ({code}) — TID: `{tid}`")
        return
    except Exception as e:
        await processing.delete()
        logger.error(f"Unexpected error: {e}")
        await message.answer(f"❌ ያልታወቀ ስህተት: `{e}`\nTID: `{tid}`")
        return

    await processing.delete()

    if not data:
        await message.answer(
            f"⚠️ ገጹ ተከፍቷል ግን ምንም ውሂብ አልተገኘም (TID: `{tid}`)።\n"
            "ትክክለኛ TID መሆኑን ያረጋግጡ፣ ወይም ገጹ አቀማመጡ ተቀይሮ ይሆናል።"
        )
        return

    status = (data.get("status") or "").lower()
    ok_status = any(k in status for k in ("complete", "success"))

    payer_name = data.get("payer_name", "-")
    payer_number = data.get("payer_number", "-")
    amount = data.get("total_paid", "-")

    if ok_status:
        text = (
            "✅ ክፍያ ተረጋግጥዋል\n\n"
            "⨳ የክፍያ ዘዴ: Telebirr 📲\n"
            f"⨳ ቴሌግራም ስም: {message.from_user.full_name}\n"
            f"⨳ ከፋይ ስም: {payer_name}\n"
            f"⨳ ስልክ: {payer_number}\n"
            f"⨳ መጠን: {amount} Birr\n"
            f"⨳ TID: `{tid}`\n"
            f"⨳ USER ID: `{message.from_user.id}`"
        )
    else:
        text = (
            f"❌ ክፍያው አልተጠናቀቀም ወይም ሁኔታው አልታወቀም (Status: {data.get('status', 'unknown')})።\n"
            f"TID: `{tid}`\n\n"
            f"_(raw data: {data})_"
        )
    await message.answer(text)


async def handle_cbe(message: Message, cbe_url: str):
    processing = await message.answer("🔄 ደረሰኙን በማረጋገጥ ላይ ነኝ (CBE)...")

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, extract_cbe_receipt_data, cbe_url)
    except requests.exceptions.Timeout:
        await processing.delete()
        await message.answer(
            f"❌ *Timeout*: CBE ገጹ ምላሽ አልሰጠም።\nLink: `{cbe_url}`"
        )
        return
    except requests.exceptions.ConnectionError as e:
        await processing.delete()
        await message.answer(
            f"❌ *Connection Error*: ግንኙነት አልተሳካም።\nLink: `{cbe_url}`\nError: `{e}`"
        )
        return
    except requests.exceptions.HTTPError as e:
        await processing.delete()
        code = e.response.status_code if e.response is not None else "?"
        await message.answer(f"❌ HTTP Error ({code}) — Link: `{cbe_url}`")
        return
    except Exception as e:
        await processing.delete()
        logger.error(f"Unexpected CBE error: {e}")
        await message.answer(f"❌ ያልታወቀ ስህተት: `{e}`\nLink: `{cbe_url}`")
        return

    await processing.delete()

    if not data or not data.get("reference"):
        await message.answer(
            f"⚠️ ደረሰኝ አልተገኘም ወይም ማንበብ አልተቻለም።\nLink: `{cbe_url}`\n"
            "Reference number እና የመለያ ቁጥር የመጨረሻ 8 አሃዝ (ወይም ሙሉ ሊንኩ) ትክክል መሆናቸውን ያረጋግጡ።"
        )
        return

    # CBE ራሱ ደረሰኝ ገጹ ላይ 'status' አይሰጥም — ትክክለኛ reference ከሆነ PDF ደረሰኙ ራሱ
    # ትክክለኛ (ተጠናቋል) መሆኑን ያመለክታል።
    text = (
        "✅ ክፍያ ተረጋግጥዋል\n\n"
        "⨳ የክፍያ ዘዴ: CBE 🏦\n"
        f"⨳ ቴሌግራም ስም: {message.from_user.full_name}\n"
        f"⨳ ከፋይ ስም: {data.get('payer_name', '-')}\n"
        f"⨳ ከፋይ አካውንት: {data.get('payer_account', '-')}\n"
        f"⨳ ተቀባይ ስም: {data.get('receiver_name', '-')}\n"
        f"⨳ ተቀባይ አካውንት: {data.get('receiver_account', '-')}\n"
        f"⨳ የተላከው መጠን: {data.get('transferred_amount', '-')}\n"
        f"⨳ አጠቃላይ የተቀነሰ: {data.get('total_paid', '-')}\n"
        f"⨳ ቀን: {data.get('date', '-')}\n"
        f"⨳ Reference: `{data.get('reference', '-')}`\n"
        f"⨳ USER ID: `{message.from_user.id}`"
    )
    await message.answer(text)


# ==========================================================================
# Render.com web service ፍላጎት: ክፍት port ላይ HTTP response መስጠት አለበት
# ==========================================================================
async def health(request):
    return web.Response(text="Telebirr test bot is running.")


async def main():
    port = int(os.getenv("PORT", "10000"))
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health-check server listening on :{port}")
    if RELAY_URL:
        logger.info(f"Relay mode ON — verifying via {RELAY_URL}")
    else:
        logger.info("Relay mode OFF — verifying directly against ethiotelecom.et")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
