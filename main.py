"""
Telebirr Relay Server — በራስዎ ኮምፒውተር (ኢትዮጵያ ውስጥ) ላይ የሚሰራ

ይህ ትንሽ FastAPI አገልግሎት transactioninfo.ethiotelecom.et ን በምትኩ ይጠይቃል
(ኮምፒውተርዎ ኢትዮጵያ ውስጥ ስለሆነ Telebirr አያግደውም)፣ ውጤቱን እንደ JSON ይመልሳል።
Render ላይ ያለው ቦት ይህንን relay በ Cloudflare Tunnel URL በኩል ይጠራል።

ማስኬጃ:
    pip install fastapi uvicorn requests beautifulsoup4
    python relay.py
"""
import re

import requests
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException

app = FastAPI()


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


if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
