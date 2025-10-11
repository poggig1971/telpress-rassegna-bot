import os
import smtplib
import time
from email.message import EmailMessage

# === CONFIGURAZIONE ===
SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ["SMTP_PORT"])
EMAIL_SENDER = os.environ["SMTP_USER"]
EMAIL_PASSWORD = os.environ["SMTP_PASS"]
SMTP_SENDER_NAME = os.environ.get("SMTP_SENDER_NAME", "ANCE Piemonte")

BATCH_SIZE = 1              # max 10 destinatari per blocco → compatibile con Aruba
DELAY_SECONDS = 5            # pausa breve tre sec tra invii
RETRY_COUNT = 3              # numero massimo di tentativi
RETRY_DELAY = 30             # attesa tra retry se fallisce

NOTIFY_BCC_FILE = os.environ.get("NOTIFY_BCC_FILE", "notify_bcc.txt")
ATTACHMENT_PATH = os.environ.get("ATTACHMENT_PATH", "rassegna.pdf")
EMAIL_SUBJECT = os.environ.get("EMAIL_SUBJECT", "Rassegna Stampa ANCE Piemonte")
EMAIL_BODY = os.environ.get("EMAIL_BODY", "In allegato la rassegna stampa odierna.")

# === FUNZIONE: carica destinatari ===
def load_recipients(file_path):
    with open(file_path, "r") as f:
        lines = f.readlines()
    return [line.strip() for line in lines if "@" in line and not line.strip().startswith("#")]

# === FUNZIONE: invia email con retry ===
def send_email_batch(recipients, subject, body, attachment_path=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_SENDER_NAME} <{EMAIL_SENDER}>"
    msg["To"] = EMAIL_SENDER  # invio a se stessi
    msg["Bcc"] = ", ".join(recipients)
    msg.set_content(body)

    # Allegato PDF
    if attachment_path:
        with open(attachment_path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="application",
                subtype="pdf",
                filename=os.path.basename(attachment_path)
            )

    # Invio con retry
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                server.send_message(msg)
            print(f"✅ Email inviata a batch ({len(recipients)}): {recipients}")
            return  # uscita dal ciclo se ok
        except smtplib.SMTPServerDisconnected:
            print(f"⚠️ Tentativo {attempt}: connessione chiusa da server, nuovo tentativo tra {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"❌ Errore al tentativo {attempt}: {e}")
            if attempt < RETRY_COUNT:
                print(f"⏳ Ritento tra {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"🚫 Tutti i tentativi falliti per batch: {recipients}")

# === MAIN ===
if __name__ == "__main__":
    all_recipients = load_recipients(NOTIFY_BCC_FILE)
    print(f"Totale destinatari trovati: {len(all_recipients)}")

    for i in range(0, len(all_recipients), BATCH_SIZE):
        batch = all_recipients[i : i + BATCH_SIZE]
        print(f"\n📤 Invio batch {i//BATCH_SIZE + 1}: {batch}")
        send_email_batch(batch, EMAIL_SUBJECT, EMAIL_BODY, ATTACHMENT_PATH)
        if i + BATCH_SIZE < len(all_recipients):
            print(f"⏸ Attesa di {DELAY_SECONDS} secondi prima del prossimo batch...")
            time.sleep(DELAY_SECONDS)

    print("\n🏁 Invio completato.")
