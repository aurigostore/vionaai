import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}

# AI (Anthropic-compatible endpoint)
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.rindri.com").strip()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "claude-opus-5-5").strip()
AI_EFFORT = os.getenv("AI_EFFORT", "medium").strip()  # low | medium | high | xhigh | max, kosong = default
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "16000"))

# Lain-lain
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Jakarta"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "40"))  # jumlah pesan obrolan terakhir yang dibawa
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "data" / "agent.db"))


def validate() -> None:
    missing = [
        name
        for name, value in [
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("AI_API_KEY", AI_API_KEY),
            ("ALLOWED_USER_IDS", ALLOWED_USER_IDS),
        ]
        if not value
    ]
    if missing:
        raise SystemExit(f"Isi dulu di file .env: {', '.join(missing)}")
