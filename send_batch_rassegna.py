import os
import smtplib
import time
from email.message import EmailMessage

# === CONFIGURAZIONE da variabili di ambiente (GitHub Secrets + .yml) ===
SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ["SMTP_PORT"])
EMAIL_SENDER = os.environ["SMTP_USER"]
EMAIL_PASSWORD = os.environ["SMTP_PASS"]
SMTP_SENDER_NAME = os.environ.get("SMTP_SENDER_NAME", "ANCE Piemonte")  # opzionale

BATCH_SIZE = 5
DELAY_SECONDS = 5  # Pausa tra batch per evitare limiti SMTP

NOTIFY_BCC_FILE = os.environ.get("NOTIFY_BCC_FILE", "notify_bcc.txt")
ATTACHMENT_PATH = os.environ.get("ATTACHMENT_PATH", "rassegna.pdf")
EMAIL_SUBJECT = os.environ.get("EMAIL_SUBJECT", "Rassegna Stampa ANCE Piemonte")
EMAIL_BODY = os.environ.get("EMAIL_BODY", "In allegato la rassegna stampa odierna.")

# === FUNZIONE: carica email da file, escludi righe non valide/commentate ===
def load_recipients(file_path):
    with open(file_path, "r") as f:
        lines = f.readlines()
    return [line.strip() for line in lines if "@" in line and not line.strip().startswith("#")]

# === FUNZIONE: invia email a un batch di destinatari ===
def send_email_batch(recipients, subject, body, attachment_path=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_SENDER_NAME} <{EMAIL_SENDER}>"
    msg["To"] = EMAIL_SENDER  # invio "a se stessi"
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

    # Invio via SMTP (SSL/SMTPS)
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"✅ Email inviata a: {recipients}")
    except Exception as e:
        print(f"❌ Errore durante l'invio a {recipients}: {e}")

# === ESECUZIONE ===
if __name__ == "__main__":
    all_recipients = load_recipients(NOTIFY_BCC_FILE)
    print(f"Totale destinatari trovati: {len(all_recipients)}")

    for i in range(0, len(all_recipients), BATCH_SIZE):
        batch = all_recipients[i : i + BATCH_SIZE]
        print(f"Invio batch {i//BATCH_SIZE + 1}: {batch}")
        send_email_batch(batch, EMAIL_SUBJECT, EMAIL_BODY, ATTACHMENT_PATH)
        if i + BATCH_SIZE < len(all_recipients):
            print(f"⏸ Attesa di {DELAY_SECONDS} secondi prima del prossimo batch...")
            time.sleep(DELAY_SECONDS)

    print("🏁 Invio completato.")
