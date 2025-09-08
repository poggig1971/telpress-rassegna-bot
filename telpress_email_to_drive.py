#!/usr/bin/env python3
import os, re, io, json, argparse, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Gmail OAuth (utente)
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

# Drive Service Account
from google.oauth2.service_account import Credentials as SACredentials
from requests.exceptions import RequestException

# ---------------- Config ----------------
load_dotenv()
SENDER_FILTER = os.getenv("SENDER_FILTER", "rassegnastampa@telpress.it")
SUBJECT_PREFIX = os.getenv("SUBJECT_PREFIX", "Rassegna STAMPA")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Rome")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
TOKEN_PATH = "token_google.pkl"
CLIENT_SECRET = "client_secret.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

MONTHS_IT = {
    "gennaio": "01", "febbraio": "02", "marzo": "03", "aprile": "04",
    "maggio": "05", "giugno": "06", "luglio": "07", "agosto": "08",
    "settembre": "09", "ottobre": "10", "novembre": "11", "dicembre": "12",
}
MONTHS_IT_INV = {v: k for k, v in MONTHS_IT.items()}

# -------------- Utils --------------
def log(msg, quiet=False):
    if not quiet:
        print(msg)

def within_window(now_local: datetime) -> bool:
    """Attivo solo 08:00 <= ora < 12:00 nel fuso definito."""
    return 8 <= now_local.hour < 12

def with_retries(fn, *, tries=5, base_delay=0.8, max_delay=8.0, retriable_http=(429, 500, 502, 503, 504), quiet=False):
    for i in range(1, tries + 1):
        try:
            return fn()
        except HttpError as e:
            status = getattr(e, 'status_code', None) or (e.resp.status if hasattr(e, 'resp') else None)
            if status in retriable_http and i < tries:
                delay = min(max_delay, base_delay * (2 ** (i - 1)))
                log(f"[WARN] HttpError {status}, retry {i}/{tries} fra {delay:.1f}s...", quiet)
                time.sleep(delay); continue
            raise
        except RequestException:
            if i < tries:
                delay = min(max_delay, base_delay * (2 ** (i - 1)))
                log(f"[WARN] Network error, retry {i}/{tries} fra {delay:.1f}s...", quiet)
                time.sleep(delay); continue
            raise

# -------------- Auth & Services --------------
def get_creds():
    """OAuth utente per Gmail (in CI può usare GOOGLE_TOKEN_JSON)."""
    token_json = os.getenv("GOOGLE_TOKEN_JSON")
    if token_json:
        info = json.loads(token_json)
        return Credentials.from_authorized_user_info(info, SCOPES)

    creds = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as token:
            import pickle; creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        with open(TOKEN_PATH, "wb") as token:
            import pickle; pickle.dump(creds, token)
    return creds

def build_gmail_service(creds):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

def build_drive_service_as_service_account(quiet=False):
    sa_creds = SACredentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    log(f"[INFO] Service Account: {sa_creds.service_account_email} — condividi la cartella {DRIVE_FOLDER_ID} con questo indirizzo.", quiet)
    return build("drive", "v3", credentials=sa_creds, cache_discovery=False)

# -------------- Gmail helpers --------------
def it_subject_date_phrase(d) -> str:
    # "del 6 settembre 2025"
    month_it = MONTHS_IT_INV[f"{d.month:02d}"]
    return f"del {d.day} {month_it} {d.year}"

def gmail_search_today(gmail, tz: str):
    """Cerca SOLO la mail odierna usando la frase 'del {gg} {mese_it} {aaaa}' e i limiti di data."""
    today = datetime.now(ZoneInfo(tz)).date()
    tomorrow = today + timedelta(days=1)
    phrase = it_subject_date_phrase(today)
    after = today.strftime("%Y/%m/%d")
    before = tomorrow.strftime("%Y/%m/%d")
    q = f'from:{SENDER_FILTER} subject:{SUBJECT_PREFIX!r} "{phrase}" after:{after} before:{before}'
    res = gmail.users().messages().list(userId="me", q=q, maxResults=10).execute()
    msgs = res.get("messages", [])
    if not msgs:
        return None
    # prendi la più recente di oggi
    latest, latest_ts = None, 0
    for m in msgs:
        full = gmail.users().messages().get(userId="me", id=m["id"], format="full").execute()
        ts = int(full.get("internalDate", "0"))
        if ts > latest_ts:
            latest, latest_ts = full, ts
    return latest

def parts_iter(payload):
    stack = [payload]
    while stack:
        p = stack.pop()
        if p.get("parts"): stack.extend(p["parts"])
        else: yield p

def get_html_body(message):
    payload = message.get("payload", {})
    for p in parts_iter(payload):
        if p.get("mimeType") == "text/html":
            data = p.get("body", {}).get("data")
            if data:
                import base64
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return None

def extract_pdf_link_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    a = soup.find("a", string=lambda s: s and "pdf" in s.lower())
    if a and a.get("href"):
        return a["href"]
    for tag in soup.find_all("a", href=True):
        if ".pdf" in tag["href"].lower():
            return tag["href"]
    return None

def extract_pdf_attachment_bytes(gmail, message):
    """Se esiste un allegato PDF, ritorna (filename, bytes), altrimenti None."""
    for p in parts_iter(message.get("payload", {})):
        if p.get("filename", "").lower().endswith(".pdf"):
            att_id = p.get("body", {}).get("attachmentId")
            if not att_id: continue
            att = gmail.users().messages().attachments().get(
                userId="me", messageId=message["id"], id=att_id
            ).execute()
            import base64
            data = base64.urlsafe_b64decode(att["data"])
            return (p["filename"], data)
    return None

# -------------- Drive --------------
def ensure_pdf_bytes(url: str, quiet=False) -> bytes:
    def _do():
        r = requests.get(url, timeout=60); r.raise_for_status(); return r.content
    return with_retries(_do, quiet=quiet)

def drive_find_file(drive, name, folder_id):
    q = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    res = drive.files().list(q=q, fields="files(id,name)").execute()
    files = res.get("files", []); return files[0] if files else None

def drive_upload_bytes(drive, content: bytes, name: str, folder_id: str, quiet=False):
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/pdf", resumable=False)
    metadata = {"name": name, "parents": [folder_id]}
    return with_retries(lambda: drive.files().create(body=metadata, media_body=media, fields="id,name").execute(), quiet=quiet)

# -------------- Main --------------
def main():
    parser = argparse.ArgumentParser(description="Carica SOLO la rassegna odierna Telpress su Drive (no cancellazioni).")
    parser.add_argument("--quiet", action="store_true", help="Log minimo.")
    parser.add_argument("--force-now", action="store_true", help="Ignora la finestra 08-12 (per test).")
    args = parser.parse_args()

    if not DRIVE_FOLDER_ID:
        raise RuntimeError("DRIVE_FOLDER_ID mancante nelle variabili d'ambiente")

    now_local = datetime.now(ZoneInfo(TIMEZONE))
    if not args.force_now and not within_window(now_local):
        log(f"[INFO] Fuori finestra oraria (ora locale {now_local:%H:%M}). Nessuna azione.", args.quiet)
        return

    # Nome file odierno
    out_name = now_local.strftime("%Y.%m.%d") + ".pdf"

    # Drive (SA): se esiste già, esco
    drive = build_drive_service_as_service_account(quiet=args.quiet)
    existing = drive_find_file(drive, out_name, DRIVE_FOLDER_ID)
    if existing:
        log(f"[INFO] {out_name} esiste già (id={existing['id']}). Nessun upload.", args.quiet)
        return

    # Gmail
    creds = get_creds()
    gmail = build_gmail_service(creds)

    # Cerca SOLO la mail di oggi
    msg = gmail_search_today(gmail, TIMEZONE)
    if not msg:
        log("[INFO] Nessuna email Telpress odierna trovata. Riproverò al prossimo giro.", args.quiet)
        return

    # Estrai PDF (link o allegato)
    html = get_html_body(msg)
    pdf_bytes = None

    pdf_url = extract_pdf_link_from_html(html) if html else None
    if pdf_url:
        log(f"[INFO] Link PDF: {pdf_url}", args.quiet)
        pdf_bytes = ensure_pdf_bytes(pdf_url, quiet=args.quiet)

    if not pdf_bytes:
        att = extract_pdf_attachment_bytes(gmail, msg)
        if att:
            att_name, pdf_bytes = att
            log(f"[INFO] Allegato PDF trovato: {att_name} ({len(pdf_bytes)} bytes)", args.quiet)

    if not pdf_bytes:
        log("[INFO] Nessun PDF (link/allegato) nella mail odierna. Riproverò al prossimo giro.", args.quiet)
        return

    up = drive_upload_bytes(drive, pdf_bytes, out_name, DRIVE_FOLDER_ID, quiet=args.quiet)
    log(f"[OK] Caricato su Drive: {up.get('name')} (id={up.get('id')})", args.quiet)

if __name__ == "__main__":
    main()

