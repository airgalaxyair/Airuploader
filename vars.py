import os

API_ID    = int(os.environ.get("API_ID", "0"))
API_HASH  = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Parse AUTH_USERS safely
_raw = os.environ.get("AUTH_USERS", "")
try:
    AUTH_USERS = [int(x.strip()) for x in _raw.split(",") if x.strip()]
except:
    AUTH_USERS = []

CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
