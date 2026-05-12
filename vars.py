# ============================================================
#  vars.py — Bot Configuration
#  Fill in your values below
# ============================================================

import os

# ── Telegram API credentials ──────────────────────────────────
# Get from https://my.telegram.org/apps
API_ID   = int(os.environ.get("API_ID", "YOUR_API_ID"))
API_HASH = os.environ.get("API_HASH", "YOUR_API_HASH")

# ── Bot token ────────────────────────────────────────────────
# Get from @BotFather on Telegram
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")

# ── Authorized users (Telegram user IDs) ─────────────────────
# Only these users can use the bot
AUTH_USERS = [
    int(x) for x in os.environ.get("AUTH_USERS", "YOUR_USER_ID").split(",")
]

# ── Default output channel ───────────────────────────────────
# Channel ID (e.g. -1001234567890) or username (e.g. @mychannel)
# Bot must be admin in this channel
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@your_channel")
