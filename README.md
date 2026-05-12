# Course Uploader Bot 🤖

A Telegram bot that parses HTML course files (from Noobda/course extractors) and uploads all **videos + PDFs** to a Telegram channel automatically.

---

## Features

- 🎬 Downloads Brightcove HLS videos (`master.m3u8` with `bcov_auth`)
- 📺 Downloads YouTube embed videos
- 📄 Downloads direct PDF links (crwilladmin CDN, etc.)
- 📤 Uploads everything to a Telegram channel with captions
- 📊 Live progress bar during upload
- ▶️ Resume from any position (start_from)
- 🔐 Auth-user restricted

---

## Repo Structure

```
course-uploader-bot/
├── bot.py              # Main bot
├── vars.py             # Configuration
├── requirements.txt    # Python dependencies
├── Dockerfile          # For cloud deployment
├── .gitignore
└── README.md
```

---

## Setup

### 1. Get credentials

| What | Where |
|------|-------|
| `API_ID` & `API_HASH` | https://my.telegram.org/apps |
| `BOT_TOKEN` | @BotFather on Telegram |
| `AUTH_USERS` | Your Telegram user ID (get from @userinfobot) |
| `CHANNEL_ID` | Your channel ID (e.g. `-1001234567890`) |

### 2. Fill `vars.py`

```python
API_ID   = 12345678
API_HASH = "abcdef1234567890abcdef1234567890"
BOT_TOKEN = "123456:ABCdefGHIjklMNOpqrSTUvwxYZ"
AUTH_USERS = [123456789]
CHANNEL_ID = "@mychannel"  # or -1001234567890
```

Or set as **environment variables** (recommended for deployment):
```
API_ID=12345678
API_HASH=abcdef...
BOT_TOKEN=123456:ABC...
AUTH_USERS=123456789
CHANNEL_ID=-1001234567890
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Install system tools

**Ubuntu/Debian (VPS/Koyeb):**
```bash
apt install ffmpeg aria2
```

**Termux (Android):**
```bash
pkg install ffmpeg aria2
pip install yt-dlp
```

### 5. Make bot admin in your channel

Go to your channel → Edit → Administrators → Add bot → give **Post messages** permission.

### 6. Run

```bash
python bot.py
```

---

## How to Use

1. Send `/upload` to the bot
2. Send your HTML course file
3. Bot shows: `113 videos, 316 PDFs found`
4. Choose: `videos` / `pdfs` / `both`
5. Enter start position (e.g. `1`)
6. Enter channel or press `/d` for default
7. Bot downloads and uploads everything ✅

---

## Deploy on Koyeb (Free)

1. Push this repo to GitHub
2. Go to [koyeb.com](https://koyeb.com) → New App → GitHub
3. Select your repo
4. Set environment variables in Koyeb dashboard:
   - `API_ID`, `API_HASH`, `BOT_TOKEN`, `AUTH_USERS`, `CHANNEL_ID`
5. Deploy — bot runs 24/7 for free

---

## Deploy on Railway

1. Push to GitHub
2. Go to [railway.app](https://railway.app) → New Project → GitHub repo
3. Add env variables
4. Deploy

---

## Supported HTML Format

The bot parses HTML files exported by course extractors like Noobda bot.
Links are detected by CSS class:
- `.video-link` → treated as video
- `.pdf-link` → treated as PDF
- Any `master.m3u8` URL → video
- Any `.pdf` URL → PDF

---

## Notes

- Videos are downloaded via `yt-dlp` + `aria2c` (fast multi-connection)
- PDFs are downloaded via `aiohttp` (async)
- Temp files are auto-deleted after upload
- Failed items are listed in the final summary
- The bot retries on FloodWait errors automatically
