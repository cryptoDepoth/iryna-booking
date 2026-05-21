# 🌸 Pashynska Photography — Mini Session Booking System

Flask-based booking system for **Blossom Mini Sessions** (and future events).
Live date: **May 3, 2026**

## 🌐 Live URLs
- **Public:** `https://pashynska.agency`
- **Public (www):** `https://www.pashynska.agency`
- **Local:** `http://localhost:5001`

## 📁 Structure

```
├── app.py                 # Flask server — slots, reserve, payment, confirm, admin
├── bookings.db            # SQLite database (local, not in git)
├── check_etransfer.py     # Gmail/Himalaya e-Transfer parser (runs via LaunchAgent)
├── sync_notion.py         # Sync bookings → Notion database
├── templates/
│   ├── index.html         # Slot picker + session types
│   ├── payment.html       # e-Transfer instructions + timer
│   └── success.html       # Confirmation + preparation guide
├── static/images/         # Real session photos (not stock!)
└── .env                   # Secrets (local, not in git)
```

## 🔐 Environment Variables

Create `.env` from `.env.example`:

```bash
NOTION_API_KEY="ntn_..."
NOTION_DATABASE_ID="355510b9-cc5b-818c-aec6-d764f116e2b2"
```

| Variable | Source |
|---|---|
| `NOTION_API_KEY` | Notion Integration Token |
| `NOTION_DATABASE_ID` | Database URL → extract ID |
| `FLASK_SECRET_KEY` | Generate: `openssl rand -hex 32` |
| `ADMIN_KEY` | Admin dashboard secret — generate random string |
| `TELEGRAM_BOT_TOKEN` | BotFather |
| `TELEGRAM_CHAT_ID` | Get from @userinfobot |

## 🚀 How to Run Locally

```bash
cd ~/business/iryna/iryna-booking
python3 -m venv venv
source venv/bin/activate
pip install flask requests
export $(cat .env | xargs)
python3 app.py
```

Server runs on `0.0.0.0:5001`.

## 🌍 Public Tunnel (Cloudflare)

Production runs through the named Cloudflare Tunnel `pashynska-booking`.
Both `pashynska.agency` and `www.pashynska.agency` route to `http://127.0.0.1:5001`.

Local launchd jobs:

```bash
launchctl list | grep com.pashynska.booking
```

## 🧠 Business Logic

- **Slots:** Every 20 min from 10:00 to 16:00
- **Session types:** Blossom Mini, Lilac, Maternity, Wedding
- **Pricing:** Admin enters the pre-tax session price. Alberta GST is added automatically in the UI. Default: CAD $190 + $9.50 GST = $199.50 total, **$95.00 deposit now**, **$104.50 balance later**.
- **e-Transfer email:** `iryna.pashynska@gmail.com`
- **Auto-expire:** 15-minute reservation window (unpaid = slot freed)
- **Rate limit:** Max 5 reserve attempts / 10 min per IP
- **Notion sync:** On confirmation, writes `Name`, `Date`, `Time`, `Session Type`, `Instagram`, `Deposit`, `Status`
- **Gmail check:** `check_etransfer.py` runs every 5 min via LaunchAgent, marks deposits as `paid` + confirms client

## 🤖 Website Assistant

The public site includes a small chat helper on `index_v2.html`.

- Endpoint: `POST /assistant/chat`
- Optional LLM: set `ZAI_API_KEY`/`ZAI_MODEL` or `OPENAI_API_KEY`/`OPENAI_MODEL`
- Fallback: works without OpenAI using current event data and simple local answers
- Local knowledge file: `~/.pashynska-data/assistant_knowledge.jsonl`
- Production knowledge file on Fly volume: `/data/assistant_knowledge.jsonl`

Build the sanitized knowledge file from an Instagram export:

```bash
python3 scripts/build_assistant_knowledge.py /path/to/instagram-export.zip
```

Do not commit raw exports or generated knowledge files. The builder redacts links, emails, phone numbers, Instagram handles, and obvious client names.

Production setup:

```bash
fly secrets set AI_PROVIDER="zai" ZAI_API_KEY="..." ZAI_MODEL="glm-4.5-air"
fly ssh sftp put /Users/andrzej/.pashynska-data/assistant_knowledge.jsonl /data/assistant_knowledge.jsonl
```

## 🎯 TODO / Improvements for AI Agents

### Critical
1. **Race condition fix** — simultaneous `/reserve` for same slot → use `BEGIN IMMEDIATE` or optimistic locking
2. **Expired cleanup** — `/expired` endpoint or cron to auto-cancel unpaid >15min reservations
3. **Notion field mapping** — verify `properties.Name.title[0].text.content` works for all entries

### Features
4. **Google Calendar integration** — auto-create event on confirmation with ICS email
5. **SMS reminder** — 24h + 1h before session via Twilio
6. **Admin dashboard** — `/admin` view with all bookings, filter by status
7. **Webhook for e-Transfer** — instead of polling Gmail, use Interac webhook
8. **Multi-day support** — currently hardcoded `DATE = "2026-05-03"`, make configurable
9. **Photo gallery** — Instagram API pull for recent session previews
10. **Analytics** — track conversion funnel: view → reserve → payment → confirm

### DevOps
11. **Production server** — replace Flask dev server with Gunicorn + systemd/LaunchAgent
12. **Database migrations** — Alembic for schema versioning
13. **Testing** — pytest with SQLite in-memory, mock Notion API
14. **CI/CD** — GitHub Actions auto-deploy on push

## 🗄️ Database Schema

```sql
CREATE TABLE bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    time TEXT,
    name TEXT,
    email TEXT,
    phone TEXT,
    instagram TEXT,
    session_type TEXT,
    status TEXT DEFAULT 'available',  -- available | reserved | pending_payment | confirmed | expired | cancelled
    confirmed INTEGER DEFAULT 0,      -- 1 = deposit received
    reserved_until TEXT,              -- ISO timestamp
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date, time)
);
```

## 📋 Notion Database Fields

| Property | Type | Value |
|----------|------|-------|
| Name | Title | Client name |
| Date | Date | Session date |
| Time | Rich text | HH:MM |
| Session Type | Select | Blossom Mini / Lilac / Maternity / Wedding |
| Instagram | Rich text | @handle |
| Deposit | Number | 95 |
| Status | Select | Pending / Paid / Confirmed |

## 🎨 Brand

- ** Photographer:** Iryna Pashynska (@pashynska.photo)
- ** Instagram:** `https://instagram.com/pashynska.photo`
- ** Website:** Built on Wfolio, this Flask app embedded via iframe or linked
- ** Tone:** Warm, floral, spring, Calgary blossoms
- ** Photos:** ONLY real uploaded session photos. No stock images.

## 🛡️ Secrets Policy

- NEVER commit `.env`, `bookings.db`, or real photos to public repo
- GitHub Secret Scanning is active — any `ghp_` or `ntn_` token will block push
- Use GitHub Actions secrets for CI/CD

---

**Created:** May 3, 2026 | **Maintainer:** cryptoDepoth + AI agents
