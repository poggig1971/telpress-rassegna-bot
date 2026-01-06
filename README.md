# rassegna-automation
automatizza il caricamento della rassegna stampa dalla mia email gmail e invis con aziena
python telpress_email_to_drive.py


# Telpress Rassegna Bot

Automatizza il download della rassegna stampa Telpress da Gmail e il caricamento su Google Drive.

## 🚀 Funzionalità
- Legge automaticamente le email provenienti da **Telpress** (`rassegnastampa@telpress.it`).
- Estrae il link al PDF della rassegna.
- Carica il PDF su Google Drive con nome `YYYY.MM.DD.pdf`.
- Evita duplicati: se il file esiste già, viene saltato (a meno di `--force`).
- Supporta:
  - Scarico **ultima rassegna disponibile** (`--days N`).
  - Scarico per una **data specifica** (`--on YYYY-MM-DD`).
  - Scarico per un **intervallo di date** (`--range START:END`).
  - Upload da PDF locale (`--file path`).

---

## 🛠️ Requisiti
- Python 3.11+
- Google API Client (`requirements.txt` lo installa)
- Credenziali Google:
  - `client_secret.json` → per OAuth Gmail.
  - `service_account.json` → per accesso a Drive.
- Token OAuth generato con `genera_token_locale()`.

---

## ⚙️ Setup locale

1. Clona la repo:
   ```bash
   git clone https://github.com/poggig1971/telpress-rassegna-bot.git
   cd telpress-rassegna-bot
   ```

2. Crea ed attiva un virtualenv:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # su Windows: .venv\Scripts\activate
   ```

3. Installa le dipendenze:
   ```bash
   pip install -r requirements.txt
   ```

4. Genera il token locale (una tantum, apre browser):
   ```bash
   python -c "from telpress_email_to_drive import genera_token_locale; genera_token_locale()"
   ```
   - Questo crea `token_google.pkl` **e stampa il JSON** → copialo e usalo come secret `GOOGLE_TOKEN_JSON` su GitHub.

5. Prova il bot in locale:
   ```bash
   python telpress_email_to_drive.py --days 3
   ```

---

## 🔑 Configurazione Secrets GitHub

Vai in **Settings → Secrets → Actions** e aggiungi:

- `SENDER_FILTER` → `rassegnastampa@telpress.it`
- `SUBJECT_PREFIX` → `Rassegna STAMPA`
- `DRIVE_FOLDER_ID` → ID della cartella Drive
- `SERVICE_ACCOUNT_JSON` → contenuto del file `service_account.json`
- `CLIENT_SECRET_JSON` → contenuto del file `client_secret.json`
- `GOOGLE_TOKEN_JSON` → contenuto stampato da `genera_token_locale()`

---

## 🤖 Automazione GitHub Actions

Il workflow è definito in `.github/workflows/rassegna.yml`.

Esegue automaticamente il bot:

- **Ogni giorno alle 08:30 ora italiana**
- Oppure manualmente da **Actions → Run workflow**

### Comando predefinito (ultima rassegna ultimi 3 giorni)
```bash
python telpress_email_to_drive.py --days 3
```

### Esempi alternativi
Scaricare una data specifica:
```bash
python telpress_email_to_drive.py --on 2025-08-26
```

Scaricare un intervallo:
```bash
python telpress_email_to_drive.py --range 2025-08-20:2025-08-26
```

Caricare da file locale:
```bash
python telpress_email_to_drive.py --on 2025-08-26 --file ./RassegnaTelpress_26-08-2025.pdf
```

---

## 📂 Struttura progetto
```
telpress-rassegna-bot/
 ├─ telpress_email_to_drive.py   # script principale
 ├─ requirements.txt             # dipendenze Python
 ├─ .gitignore                   # esclude file sensibili
 ├─ README.md                    # questo file
 └─ .github/
     └─ workflows/
         └─ rassegna.yml         # GitHub Actions workflow
```

---

## 📝 Note
- In locale puoi continuare ad usare `.env` e `token_google.pkl`.
- In CI (GitHub Actions) usiamo esclusivamente i **Secrets**.
- Il Service Account deve avere permesso di scrittura sulla cartella Drive indicata da `DRIVE_FOLDER_ID`.
