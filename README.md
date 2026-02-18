# Gmail Labeler

A self-hosted email labeling system that runs a local LLM (Llama 3.2 via Ollama) to scan Gmail accounts and apply labels based on rules you define through a web interface.

Runs fully in Docker. No data leaves your machine.

---

## Requirements

- Docker + Docker Compose
- A Google Cloud project with the Gmail API enabled
- A machine with at least 4GB RAM (Raspberry Pi 4 or equivalent)

---

## Quick Start

### 1. Clone / copy this project

```
gmail-labeler/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── app/
├── credentials/       ← put credentials.json here
└── data/              ← SQLite database (auto-created)
```

### 2. Set up Google Cloud

1. Go to https://console.cloud.google.com and create a new project.
2. Enable the **Gmail API** and **Google People API**.
3. Go to **Credentials → Create OAuth client ID → Web application**.
4. Add this to Authorized Redirect URIs:
   ```
   http://localhost:5000/oauth/callback
   ```
   (Change to your Pi's address if accessing from another device.)
5. Download the JSON and save it as `credentials/credentials.json`.
6. Add your Gmail address(es) as **test users** on the OAuth consent screen.

### 3. Configure

In `docker-compose.yml`, set:
- `BASE_URL` to match how you access the app (must match the redirect URI above)
- `FLASK_SECRET_KEY` to any random string

### 4. Start

```bash
docker compose up -d
```

On first start, Llama 3.2 will be pulled automatically. This takes a few minutes on a Pi. Watch progress with:

```bash
docker compose logs -f app
```

### 5. Open the web interface

Go to http://localhost:5000

- **Accounts** — Add Gmail accounts via OAuth
- **Prompts** — Add labeling rules (plain English + label name)
- **Settings** — Configure poll interval
- **Logs** — See what the scanner is doing

---

## How it works

1. The poller runs every N seconds (configurable in the UI).
2. For each active Gmail account, it fetches recent emails.
3. Each email is passed through each active prompt rule.
4. The local LLM decides YES or NO per rule.
5. If YES, the label is applied. Labels are auto-created in Gmail if they don't exist.
6. Each email is tracked so it is only evaluated once per account.

---

## Raspberry Pi notes

Works on Pi 4 (4GB+) with 64-bit OS. Inference takes 5–20 seconds per email per prompt.
Docker's `restart: unless-stopped` handles auto-start on boot.
