"""
Telebirr + CBE Relay Server — በራስዎ ኮምፒውተር/ሰርቨር (ኢትዮጵያ ውስጥ) ላይ የሚሰራ

ይህ ትንሽ FastAPI አገልግሎት transactioninfo.ethiotelecom.et እና apps.cbe.com.et/
mbreciept.cbe.com.et ን በምትኩ ይጠይቃል (ሰርቨሩ ኢትዮጵያ ውስጥ ስለሆነ አይታገድም)፣ ውጤቱን
እንደ JSON ይመልሳል። Render ላይ ያለው ቦት ይህንን relay ይጠራል።

ማስኬጃ:
    pip install -r requirements.txt
    python main.py
"""
import io
import re
from urllib.parse import urlsplit, urlunsplit

import pdfplumber
import requests
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException

app = FastAPI()


# ==========================================================================
# Telebirr — transactioninfo.ethiotelecom.et (HTML)
# ==========================================================================
def extract_tele_receipt_data(tid: str) -> dict:
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


# ==========================================================================
# CBE — apps.cbe.com.et / mbreciept.cbe.com.et (PDF ወይም HTML)
# Render ላይ ያለው ቦት የተጠቃሚውን ግቤት ተርጉሞ ሙሉ CBE URL ይልክልናል፣ እኛ እናመጣው/እንፈታው
# ==========================================================================
CBE_MASKED_ACCOUNT_RE = r"\b[A-Za-z0-9]\*{2,}\d+\b"
CBE_REMAINING_LABELS = [
    ("payment_type", r"Payment\s*Type"),
    ("date", r"Payment\s*Date\s*&\s*Time"),
    ("reference", r"Reference\s*No\.?\s*\(VAT\s*Invoice\s*No\)"),
    ("reason", r"Reason\s*/\s*Type\s*of\s*service"),
    ("transferred_amount", r"Transferred\s*Amount"),
    ("commission", r"Service\s*Charge\s*:?"),
    ("vat_on_commission", r"VAT\s*\(15%\s*of\s*service\s*charge\)"),
    ("disaster_recovery", r"Disaster\s*Risk\s*Response\s*Fund\s*\(5%\s*of\s*service\s*charge\)"),
    ("total_paid", r"Total\s*amount\s*debited\s*from\s*customer'?s?\s*account"),
    ("amount_in_word", r"Amount\s*in\s*Word"),
]


def _normalize_cbe_url(url: str) -> str:
    """CBE ሰርቨር/CDN hostname ላይ ፊደል-ስሜታዊ (case-sensitive) ሊሆን ስለሚችል
    (ለምሳሌ 'Mbreciept.cbe.com.et' 404 ሲመልስ 'mbreciept.cbe.com.et' ግን ይሰራል)፣
    ዶሜይኑን ብቻ ወደ ትንሽ ፊደል እንቀይራለን — path/token ግን እንዳለ እንተወዋለን።"""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment))


def _extract_cbe_status(text: str) -> str:
    m = re.search(r"Status\s*:?\s*\n?\s*([A-Za-z]{3,})", text, re.I)
    return m.group(1).strip().upper() if m else ""


def _extract_cbe_pdf_fields(text: str) -> dict:
    """CBE ደረሰኝ ላይ ያሉትን label→value ጥንዶች ያወጣል። ለከፋይ/ተቀባይ ስም እና አካውንት
    'Account' የሚለውን label ቃል በቀጥታ ከመፈለግ ይልቅ የተከለለ የመለያ ቁጥር pattern
    (ለምሳሌ 1****0388) ራሱን እንፈልጋለን — ምክንያቱም አንዳንድ የተቀባይ ስሞች ራሳቸው
    'Account' የሚለውን ቃል ስለያዙ በቀላል label ፍለጋ ግራ ሊጋቡ ይችላሉ።"""
    text = re.sub(r"[ \t]+", " ", text)
    data: dict = {}

    status = _extract_cbe_status(text)
    if status:
        data["status"] = status

    account_matches = list(re.finditer(CBE_MASKED_ACCOUNT_RE, text))
    payer_m = re.search(r"Payer\s*:?", text, re.I)
    receiver_m = re.search(r"Receiver\s*:?", text, re.I)

    def _clean_name(raw_name: str) -> str:
        raw_name = re.sub(r"\s*Account\s*:?\s*$", "", raw_name, flags=re.I)
        return raw_name.strip(" \n\t:-")

    if payer_m and account_matches:
        data["payer_name"] = _clean_name(text[payer_m.end():account_matches[0].start()])
        data["payer_account"] = account_matches[0].group().strip()
    if receiver_m and len(account_matches) >= 2:
        data["receiver_name"] = _clean_name(text[receiver_m.end():account_matches[1].start()])
        data["receiver_account"] = account_matches[1].group().strip()

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
    url = _normalize_cbe_url(url)
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

    if "html" in content_type or resp.text.strip().startswith("<"):
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator="\n")
        return _extract_cbe_pdf_fields(text)

    return {}


@app.get("/")
def health():
    return {"status": "relay is running"}


@app.get("/verify")
def verify(tid: str):
    tid = re.sub(r"\s+", "", tid or "")
    if not tid:
        raise HTTPException(status_code=400, detail="tid query param required")
    try:
        data = extract_tele_receipt_data(tid)
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Timeout reaching ethiotelecom")
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(status_code=502, detail=f"Connection error: {e}")
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        raise HTTPException(status_code=code, detail="ethiotelecom HTTP error")
    return {"tid": tid, "data": data}


@app.get("/verify_cbe")
def verify_cbe(url: str):
    """Render ላይ ያለው ቦት የተጠቃሚውን ግቤት ተርጉሞ ሙሉ የ CBE ደረሰኝ URL ይልክልናል
    (ለምሳሌ https://apps.cbe.com.et:100/?id=... ወይም https://mbreciept.cbe.com.et/...)"""
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url query param required")
    try:
        data = extract_cbe_receipt_data(url)
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Timeout reaching CBE")
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(status_code=502, detail=f"Connection error: {e}")
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        raise HTTPException(status_code=code, detail="CBE HTTP error")
    return {"url": url, "data": data}


if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
