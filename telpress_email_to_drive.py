
import os, re, io, tempfile, requests, argparse
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
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
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")

# Scopes: Gmail read-only + Drive file
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

TOKEN_PATH = "token_google.pkl"
CLIENT_SECRET = "client_secret.json"

MONTHS_IT = {
    "gennaio": "01", "febbraio": "02", "marzo": "03", "aprile": "04",
    "maggio": "05", "giugno": "06", "luglio": "07", "agosto": "08",
    "settembre": "09", "ottobre": "10", "novembre": "11", "dicembre": "12"
}
MONTHS_IT_INV = {v: k for k, v in MONTHS_IT.items()}

# ----------------- Auth & Services -----------------
def get_creds():
    """OAuth utente (serve a Gmail; in CI usa GOOGLE_TOKEN_JSON, in locale usa token_google.pkl/browser)."""
    # 1) CI / Secrets: se presente GOOGLE_TOKEN_JSON, lo uso direttamente
    token_json = os.getenv("GOOGLE_TOKEN_JSON")
    if token_json:
        import json
        info = json.loads(token_json)
        # NB: richiede anche il client_secret per poter fare il refresh
        return Credentials.from_authorized_user_info(info, SCOPES)

    # 2) Locale: come prima (pickle su disco)
    creds = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as token:
            import pickle
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "wb") as token:
            import pickle
            pickle.dump(creds, token)
    return creds


def build_gmail_service(creds):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

def build_drive_service_as_service_account():
    sa_creds = SACredentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    return build("drive", "v3", credentials=sa_creds, cache_discovery=False)

# ----------------- Gmail helpers -----------------
def gmail_search_latest(gmail, days_window: int = 3):
    q = f'from:{SENDER_FILTER} subject:"{SUBJECT_PREFIX}" newer_than:{days_window}d'
    res = gmail.users().messages().list(userId="me", q=q, maxResults=10).execute()
    msgs = res.get("messages", [])
    if not msgs:
        return None
    latest = None
    latest_ts = 0
    for m in msgs:
        full = gmail.users().messages().get(userId="me", id=m["id"], format="full").execute()
        ts = int(full.get("internalDate", "0"))
        if ts > latest_ts:
            latest_ts = ts
            latest = full
    return latest

def it_subject_date_phrase(d: date) -> str:
    """Frase tipica nell'oggetto Telpress: 'del 26 agosto 2025'."""
    month_it = MONTHS_IT_INV[f"{d.month:02d}"]
    return f"del {d.day} {month_it} {d.year}"

def gmail_search_on_date(gmail, target_date: date):
    """
    Cerca la mail la cui 'subject' contiene la frase per la data specificata.
    Riduce rumore con un filtro temporale ±7 giorni.
    """
    phrase = it_subject_date_phrase(target_date)
    after = (datetime(target_date.year, target_date.month, target_date.day) - timedelta(days=7)).strftime("%Y/%m/%d")
    before = (datetime(target_date.year, target_date.month, target_date.day) + timedelta(days=8)).strftime("%Y/%m/%d")
    q = (
        f'from:{SENDER_FILTER} '
        f'subject:"{SUBJECT_PREFIX}" '
        f'subject:"{phrase}" '
        f'after:{after} before:{before}'
    )
    res = gmail.users().messages().list(userId="me", q=q, maxResults=5).execute()
    msgs = res.get("messages", [])
    if not msgs:
        return None
    target_ts = int(datetime(target_date.year, target_date.month, target_date.day).timestamp() * 1000)
    best = None
    best_diff = None
    for m in msgs:
        full = gmail.users().messages().get(userId="me", id=m["id"], format="full").execute()
        ts = int(full.get("internalDate", "0"))
        diff = abs(ts - target_ts)
        if best is None or diff < best_diff:
            best = full
            best_diff = diff
    return best

def extract_subject_date(subject: str):
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
    a = soup.find("a", string=lambda s: s and "clicca" in s.lower() and "pdf" in s.lower())
    if a and a.get("href"):
        return a["href"]
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if ".pdf" in href.lower():
            return href
    return None

# ----------------- Download & Drive -----------------
def ensure_pdf_bytes(url: str) -> bytes:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
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


# ----------------- Helpers per data -----------------
def process_single_date(drive, gmail, target_date, out_name=None, force=False):
    """
    Esegue il flusso completo per una singola data:
    - calcola il nome file (se non fornito)
    - controlla duplicato su Drive (skip se presente e non --force)
    - cerca mail Telpress per la data
    - scarica PDF e carica su Drive
    Ritorna una tupla (status, message) dove status in {"skipped", "uploaded", "error"}.
    """
    if out_name is None:
        out_name = f"{target_date.year:04d}.{target_date.month:02d}.{target_date.day:02d}.pdf"

    # Duplicati
    existing = drive_find_file(drive, out_name, DRIVE_FOLDER_ID)
    if existing and not force:
        return ("skipped", f"[SKIP] {out_name} esiste già (id={existing['id']}).")

    if existing and force:
        from googleapiclient.errors import HttpError
        try:
            drive.files().delete(fileId=existing['id']).execute()
            print(f"[INFO] Rimosso file esistente (overwrite): {out_name}")
        except HttpError as e:
            return ("error", f"[ERRORE] impossibile cancellare {out_name}: {e}")

    # Gmail: cerca messaggio del giorno
    msg = gmail_search_on_date(gmail, target_date)
    if not msg:
        return ("error", f"[ERRORE] Nessuna email Telpress trovata per la data {target_date.isoformat()}.")

    headers = msg.get("payload", {}).get("headers", [])
    subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "(senza oggetto)")
    print(f"[INFO] ({target_date}) Email: {subject}")

    html = get_html_body(msg)
    if not html:
        return ("error", f"[ERRORE] Corpo HTML non trovato per la data {target_date.isoformat()}.")

    pdf_url = extract_pdf_link_from_html(html)
    if not pdf_url:
        return ("error", f"[ERRORE] Nessun link PDF trovato per la data {target_date.isoformat()}.")

    print(f"[INFO] ({target_date}) Link PDF: {pdf_url}")
    pdf_bytes = ensure_pdf_bytes(pdf_url)
    print(f"[INFO] ({target_date}) PDF scaricato ({len(pdf_bytes)} bytes). Carico su Drive...")

    up = drive_upload_bytes(drive, pdf_bytes, out_name, DRIVE_FOLDER_ID)
    return ("uploaded", f"[OK] ({target_date}) Caricato: {up.get('name')} (id={up.get('id')})")

# ----------------- Main -----------------

def main():
    parser = argparse.ArgumentParser(description="Scarica rassegna Telpress da Gmail e carica su Drive.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--on", help="Data rassegna (YYYY-MM-DD).")
    group.add_argument("--range", help="Intervallo di date inclusivo: START:END (YYYY-MM-DD:YYYY-MM-DD).")
    parser.add_argument("--days", type=int, default=3, help="Finestra ricerca 'più recenti di' (solo per modalità 'ultima').")
    parser.add_argument("--name", help="Forza nome file in uscita (solo singola data), es: 2025.08.26.pdf.")
    parser.add_argument("--file", help="Carica un PDF locale invece di scaricarlo dalla mail (solo singola data).")
    parser.add_argument("--force", action="store_true", help="Se esiste in Drive, sovrascrive (elimina e ricarica).")
    args = parser.parse_args()

    if not DRIVE_FOLDER_ID:
        raise RuntimeError("DRIVE_FOLDER_ID mancante in .env")

    # Servizi
    drive = build_drive_service_as_service_account()

    # Modalità RANGE: loop su tutte le date, skip se esiste già (default)
    if args.range:
        try:
            start_str, end_str = args.range.split(":")
            start = date.fromisoformat(start_str)
            end = date.fromisoformat(end_str)
        except Exception:
            raise RuntimeError("--range richiede formato START:END con date ISO, es. 2025-08-20:2025-08-26")
        if end < start:
            raise RuntimeError("Nel parametro --range la data END deve essere >= START.")

        # Gmail auth una sola volta
        creds = get_creds()
        gmail = build_gmail_service(creds)

        current = start
        results = []
        while current <= end:
            status, msg = process_single_date(drive, gmail, current, out_name=None, force=args.force)
            print(msg)
            results.append((current.isoformat(), status, msg))
            current += timedelta(days=1)

        # Riepilogo finale
        uploaded = sum(1 for _, s, _ in results if s == "uploaded")
        skipped = sum(1 for _, s, _ in results if s == "skipped")
        errors = [m for _, s, m in results if s == "error"]
        print(f"\n[RIEPILOGO] Caricati: {uploaded}  |  Skippati (già presenti): {skipped}  |  Errori: {len(errors)}")
        if errors:
            print("Dettaglio errori:")
            for e in errors:
                print(" -", e)
            return

    # Modalità SINGOLA DATA oppure ULTIMA RECENTE
    # Determina target_date e nome file
    target_date = None
    if args.on:
        try:
            target_date = date.fromisoformat(args.on)
        except ValueError:
            raise RuntimeError("--on richiede formato YYYY-MM-DD")

    if args.name and target_date is None:
        raise RuntimeError("--name è consentito solo insieme a --on (singola data).")

    if args.file and target_date is None:
        raise RuntimeError("--file è consentito solo insieme a --on (singola data).")

    if args.name:
        out_name = args.name
    elif target_date:
        out_name = f"{target_date.year:04d}.{target_date.month:02d}.{target_date.day:02d}.pdf"
    else:
        now = datetime.now(ZoneInfo(TIMEZONE))
        out_name = now.strftime("%Y.%m.%d") + ".pdf"

    # Duplicati (solo singola data o ultima)
    existing = drive_find_file(drive, out_name, DRIVE_FOLDER_ID)
    if existing and not args.force:
        print(f"[INFO] {out_name} esiste già su Drive (id={existing['id']}). Esco. Usa --force per sovrascrivere.")
        return
    if existing and args.force:
        from googleapiclient.errors import HttpError
        try:
            drive.files().delete(fileId=existing['id']).execute()
            print(f"[INFO] Rimosso file esistente (overwrite): {out_name}")
        except HttpError as e:
            print(f"[WARN] Non sono riuscito a cancellare il file esistente: {e}")

    # Se è stato passato --file, carica direttamente il PDF locale
    if args.file:
        with open(args.file, "rb") as f:
            pdf_bytes = f.read()
        up = drive_upload_bytes(drive, pdf_bytes, out_name, DRIVE_FOLDER_ID)
        print(f"[OK] Caricato da file locale: {up.get('name')} (id={up.get('id')})")
        return

    # Gmail
    creds = get_creds()
    gmail = build_gmail_service(creds)

    if target_date:
        msg = gmail_search_on_date(gmail, target_date)
        if not msg:
            raise RuntimeError(f"Nessuna email Telpress trovata per la data {args.on}.")
    else:
        msg = gmail_search_latest(gmail, days_window=args.days)
        if not msg:
            raise RuntimeError(f"Nessuna email Telpress trovata negli ultimi {args.days} giorni.")

    headers = msg.get("payload", {}).get("headers", [])
    subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "(senza oggetto)")
    print(f"[INFO] Trovata email: {subject}")

    # Se non hai forzato il nome, prova a estrarlo dall'oggetto (fallback già gestito sopra)
    if not args.name and not target_date:
        out_from_subject = extract_subject_date(subject)
        if out_from_subject:
            out_name = out_from_subject

    html = get_html_body(msg)
    if not html:
        raise RuntimeError("Corpo HTML non trovato nella mail.")

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
