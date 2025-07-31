import os, re, io, tempfile, requests
from datetime import datetime
from dateutil import tz
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# --- OAuth utente per Gmail ---
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- Service Account per Drive (proprietario = drive-accessor) ---
from google.oauth2.service_account import Credentials as SACredentials

# ---- Config ----
load_dotenv()
SENDER_FILTER = os.getenv("SENDER_FILTER", "rassegnastampa@telpress.it")
SUBJECT_PREFIX = os.getenv("SUBJECT_PREFIX", "Rassegna STAMPA")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Rome")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")  # <<-- NEW

# Scopes: Gmail read-only + Drive file (Drive scope serve comunque al flusso OAuth locale, ma in cloud useremo SA)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

TOKEN_PATH = "token_google.pkl"  # un token per entrambe le API (Gmail + Drive utente, ma per Drive useremo SA)
CLIENT_SECRET = "client_secret.json"

MONTHS_IT = {
    "gennaio": "01", "febbraio": "02", "marzo": "03", "aprile": "04",
    "maggio": "05", "giugno": "06", "luglio": "07", "agosto": "08",
    "settembre": "09", "ottobre": "10", "novembre": "11", "dicembre": "12"
}

# ----------------- Auth & Services -----------------
def get_creds():
    """OAuth utente (serve a Gmail; Drive lo useremo via SA)"""
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds

def build_gmail_service(creds):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

def build_drive_service_as_service_account():
    """Drive via Service Account: i file risultano di proprietà del SA (es. drive-accessor)."""
    sa_creds = SACredentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    return build("drive", "v3", credentials=sa_creds, cache_discovery=False)

# ----------------- Gmail helpers -----------------
def gmail_search_latest(gmail):
    q = f'from:{SENDER_FILTER} subject:"{SUBJECT_PREFIX}" newer_than:3d'
    res = gmail.users().messages().list(userId="me", q=q, maxResults=5).execute()
    msgs = res.get("messages", [])
    if not msgs:
        return None
    # prendi il più recente (internalDate più alto)
    latest = None
    latest_ts = 0
    for m in msgs:
        full = gmail.users().messages().get(userId="me", id=m["id"], format="full").execute()
        ts = int(full.get("internalDate", "0"))
        if ts > latest_ts:
            latest_ts = ts
            latest = full
    return latest

def extract_subject_date(subject: str):
    """
    Esempio: 'Rassegna STAMPA del 31 luglio 2025' -> '2025.07.31.pdf'
    """
    m = re.search(r"del\s+(\d{1,2})\s+([A-Za-zàèéìòù]+)\s+(\d{4})", subject, re.IGNORECASE)
    if not m:
        return None
    day = int(m.group(1))
    month_it = m.group(2).lower()
    year = int(m.group(3))
    month = MONTHS_IT.get(month_it)
    if not month:
        return None
    return f"{year:04d}.{month}.{day:02d}.pdf"

def parts_iter(payload):
    """Itera ricorsivamente le parti MIME"""
    stack = [payload]
    while stack:
        p = stack.pop()
        if p.get("parts"):
            stack.extend(p["parts"])
        else:
            yield p

def get_html_body(message):
    payload = message.get("payload", {})
    for p in parts_iter(payload):
        mime = p.get("mimeType", "")
        if mime == "text/html":
            data = p.get("body", {}).get("data")
            if data:
                import base64
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return None

def extract_pdf_link_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    # priorità: link con testo che contiene 'Clicca' e 'PDF'
    a = soup.find("a", string=lambda s: s and "clicca" in s.lower() and "pdf" in s.lower())
    if a and a.get("href"):
        return a["href"]
    # fallback: primo link che contiene .pdf
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if ".pdf" in href.lower():
            return href
    return None

# ----------------- Download & Drive -----------------
def ensure_pdf_bytes(url: str) -> bytes:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    # A volte il server manda inline; ci fidiamo del contenuto
    return r.content

def drive_find_file(drive, name, folder_id):
    q = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    res = drive.files().list(q=q, fields="files(id,name)").execute()
    files = res.get("files", [])
    return files[0] if files else None

def drive_upload_bytes(drive, content: bytes, name: str, folder_id: str):
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/pdf", resumable=False)
    metadata = {"name": name, "parents": [folder_id]}
    return drive.files().create(body=metadata, media_body=media, fields="id,name").execute()

# ----------------- Main -----------------
def main():
    if not DRIVE_FOLDER_ID:
        raise RuntimeError("DRIVE_FOLDER_ID mancante in .env")

    # Gmail con OAuth utente
    creds = get_creds()
    gmail = build_gmail_service(creds)

    # Drive con Service Account (proprietario = SA)
    drive = build_drive_service_as_service_account()

    msg = gmail_search_latest(gmail)
    if not msg:
        print("[INFO] Nessuna email trovata da Telpress negli ultimi 3 giorni.")
        return

    headers = msg.get("payload", {}).get("headers", [])
    subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "(senza oggetto)")
    print(f"[INFO] Trovata email: {subject}")

    # deduci nome file dalla data nel subject, se possibile
    out_name = extract_subject_date(subject)
    if not out_name:
        # fallback: oggi (timezone Europe/Rome)
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(TIMEZONE))
        out_name = now.strftime("%Y.%m.%d") + ".pdf"

    # dedup su Drive
    if drive_find_file(drive, out_name, DRIVE_FOLDER_ID):
        print(f"[INFO] {out_name} esiste già su Drive. Esco.")
        return

    html = get_html_body(msg)
    if not html:
        raise RuntimeError("Corpo HTML non trovato nella mail: non posso estrarre il link al PDF.")

    pdf_url = extract_pdf_link_from_html(html)
    if not pdf_url:
        raise RuntimeError("Nessun link al PDF trovato nel corpo email.")

    print(f"[INFO] Link PDF: {pdf_url}")

    pdf_bytes = ensure_pdf_bytes(pdf_url)
    print(f"[INFO] PDF scaricato ({len(pdf_bytes)} bytes). Carico su Drive...")

    up = drive_upload_bytes(drive, pdf_bytes, out_name, DRIVE_FOLDER_ID)
    print(f"[OK] Caricato su Drive: {up.get('name')} (id={up.get('id')})")

if __name__ == "__main__":
    main()

