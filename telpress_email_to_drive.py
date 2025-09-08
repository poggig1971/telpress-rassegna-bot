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

# --- Email notify ---
from email.message import EmailMessage
from email.utils import formataddr

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

# ---------------- Debug toggle ----------------
DEBUG = os.getenv("DEBUG", "0").lower() in ("1", "true", "yes", "on")
def dlog(msg: str):
    if DEBUG:
        print(msg)

# -------------- Utils --------------
def log(msg, quiet=False, always=False):
    if always or not quiet:
        print(msg)

def within_window(now_local: datetime) -> bool:
    """Attivo 08:00–12:59."""
    return 8 <= now_local.hour < 13

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

def get_header(message, name: str):
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None

def gmail_search_today(gmail, tz: str):
    """
    Cerca la rassegna odierna provando 3 query a scalare:
      Q1) from + subject:"<PREFIX>" + "<frase data>" + after/before
      Q2) from + "<frase data>" + after/before
      Q3) from + after/before (fallback: prende l'ultima di oggi)
    Stampa in DEBUG le query e i subject trovati.
    """
    today = datetime.now(ZoneInfo(tz)).date()
    tomorrow = today + timedelta(days=1)
    phrase = it_subject_date_phrase(today)  # es. "del 8 settembre 2025"
    after = today.strftime("%Y/%m/%d")
    before = tomorrow.strftime("%Y/%m/%d")

    q_base = f'from:{SENDER_FILTER} after:{after} before:{before}'
    queries = [
        f'{q_base} subject:"{SUBJECT_PREFIX}" "{phrase}"',
        f'{q_base} "{phrase}"',
        q_base,
    ]

    for idx, q in enumerate(queries, 1):
        dlog(f"[DEBUG] Gmail query {idx}: {q}")
        res = gmail.users().messages().list(userId="me", q=q, maxResults=10).execute()
        msgs = res.get("messages", [])
        if not msgs:
            dlog("[DEBUG] Nessun messaggio trovato con questa query.")
            continue

        full_msgs = []
        for m in msgs:
            full = gmail.users().messages().get(userId="me", id=m["id"], format="full").execute()
            subj = get_header(full, "Subject") or ""
            dlog(f"[DEBUG] - Subject: {subj}")
            full_msgs.append(full)

        # Filtra per frase data dentro il subject (case-insensitive)
        cand = [m for m in full_msgs if phrase.lower() in (get_header(m, "Subject") or "").lower()]
        if cand:
            latest = max(cand, key=lambda x: int(x.get("internalDate", "0")))
            return latest

        # Ultimo fallback (Q3): prendi comunque il più recente della giornata
        if idx == len(queries):
            latest = max(full_msgs, key=lambda x: int(x.get("internalDate", "0")))
            dlog("[DEBUG] Uso fallback: ultimo messaggio odierno del mittente.")
            return latest

    return None

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
    # prova prima un link con testo che contenga 'pdf'
    a = soup.find("a", string=lambda s: s and "pdf" in s.lower())
    if a and a.get("href"):
        return a["href"]
    # altrimenti qualsiasi <a href="...pdf">
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

def drive_view_link(file_id: str) -> str:
    # I destinatari devono avere permessi sulla cartella/file
    return f"https://drive.google.com/file/d/{file_id}/view"

# -------------- Notifica email --------------
def _parse_recipients(raw: str):
    if not raw:
        return []
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]

def _date_it_string(dt: datetime) -> str:
    return f"{dt.day} {MONTHS_IT_INV[f'{dt.month:02d}']} {dt.year}"

def send_notification_email(file_id: str, file_name: str, now_local: datetime, *, quiet=False):
    """
    Invia una mail via SMTP (Aruba/Gmail) con i seguenti ENV:
      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_SECURE (ssl|starttls)
      NOTIFY_TO (lista separata da virgole o ;), opzionali:
      NOTIFY_SUBJECT, NOTIFY_BODY, SMTP_SENDER_NAME, SMTP_REPLY_TO
    """
    recipients = _parse_recipients(os.getenv("NOTIFY_TO", ""))
    if not recipients:
        log("[INFO] NOTIFY_TO non configurato: salto invio email.", quiet)
        return

    smtp_host = os.getenv("SMTP_HOST", "smtps.aruba.it")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER")  # es. news@ancepiemonte.it
    smtp_pass = os.getenv("SMTP_PASS")
    smtp_secure = os.getenv("SMTP_SECURE", "ssl").lower()  # "ssl" (465) o "starttls" (587)
    sender_name = os.getenv("SMTP_SENDER_NAME", "News")
    reply_to = os.getenv("SMTP_REPLY_TO")

    if not smtp_user or not smtp_pass:
        log("[WARN] SMTP_USER/SMTP_PASS mancanti: impossibile inviare email.", quiet)
        return

    date_it = _date_it_string(now_local)
    link = drive_view_link(file_id)

    subject_tmpl = os.getenv("NOTIFY_SUBJECT", "Rassegna stampa {date_it} caricata")
    body_tmpl = os.getenv(
        "NOTIFY_BODY",
        "Buongiorno,\n\nla rassegna stampa del {date_it} è stata caricata su Drive.\n"
        "File: {file_name}\nLink: {drive_link}\n\nCordiali saluti."
    )
    subject = subject_tmpl.format(date_it=date_it, file_name=file_name, drive_link=link)
    body = body_tmpl.format(date_it=date_it, file_name=file_name, drive_link=link)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, smtp_user))
    msg["To"] = ", ".join(recipients)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)

    try:
        import smtplib
        use_ssl = (smtp_secure == "ssl") or (smtp_port == 465)
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as s:
                s.login(smtp_user, smtp_pass)
                s.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
                s.starttls()
                s.login(smtp_user, smtp_pass)
                s.send_message(msg)

        log(f"[OK] Notifica email inviata a: {', '.join(recipients)}", quiet)
    except Exception as e:
        log(f"[WARN] Invio email fallito: {e}", quiet)

# -------------- Main --------------
def main():
    parser = argparse.ArgumentParser(description="Carica SOLO la rassegna odierna Telpress su Drive e invia notifica email (no cancellazioni).")
    parser.add_argument("--quiet", action="store_true", help="Log minimo.")
    parser.add_argument("--force-now", action="store_true", help="Ignora la finestra 08-12:59 (per test).")
    args = parser.parse_args()

    if not DRIVE_FOLDER_ID:
        raise RuntimeError("DRIVE_FOLDER_ID mancante nelle variabili d'ambiente")

    now_local = datetime.now(ZoneInfo(TIMEZONE))
    if not args.force_now and not within_window(now_local):
        # visibile sempre, anche con --quiet
        print(f"[INFO] Fuori finestra oraria (ora locale {now_local:%H:%M}). Nessuna azione.")
        return

    # Nome file odierno
    out_name = now_local.strftime("%Y.%m.%d") + ".pdf"

    # Drive (SA): se esiste già, esco
    drive = build_drive_service_as_service_account(quiet=args.quiet)
    existing = drive_find_file(drive, out_name, DRIVE_FOLDER_ID)
    if existing:
        log(f"[INFO] {out_name} esiste già (id={existing['id']}). Nessun upload, nessuna mail.", args.quiet)
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
        dlog(f"[DEBUG] Link PDF trovato nell'HTML: {pdf_url}")
        log(f"[INFO] Link PDF: {pdf_url}", args.quiet)
        pdf_bytes = ensure_pdf_bytes(pdf_url, quiet=args.quiet)

    if not pdf_bytes:
        att = extract_pdf_attachment_bytes(gmail, msg)
        if att:
            att_name, pdf_bytes = att
            dlog(f"[DEBUG] Allegato PDF: {att_name}, size={len(pdf_bytes)}")
            log(f"[INFO] Allegato PDF trovato: {att_name} ({len(pdf_bytes)} bytes)", args.quiet)

    if not pdf_bytes:
        log("[INFO] Nessun PDF (link/allegato) nella mail odierna. Riproverò al prossimo giro.", args.quiet)
        return

    up = drive_upload_bytes(drive, pdf_bytes, out_name, DRIVE_FOLDER_ID, quiet=args.quiet)
    file_id = up.get("id")
    log(f"[OK] Caricato su Drive: {up.get('name')} (id={file_id})", args.quiet)

    # Notifica email solo su upload riuscito
    send_notification_email(file_id, out_name, now_local, quiet=args.quiet)

if __name__ == "__main__":
    main()

