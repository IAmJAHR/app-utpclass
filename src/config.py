import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

UTP_USERNAME: str = os.getenv("UTP_USERNAME", "").strip()
UTP_PASSWORD: str = os.getenv("UTP_PASSWORD", "").strip()

# Admin & Web Security
ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", UTP_PASSWORD or "admin123").strip()

# Server config (0.0.0.0 required for Dokploy / Docker containers)
HOST: str = os.getenv("HOST", "0.0.0.0").strip()
PORT: int = int(os.getenv("PORT", "8080"))

# Google Drive config
DEFAULT_GDRIVE_FOLDER_ID = "1nU0cJhO6IHaTBgoA80EzZsLxPMWB4ulx"
GDRIVE_FOLDER_ID: str = os.getenv("GDRIVE_FOLDER_ID", DEFAULT_GDRIVE_FOLDER_ID).strip() or DEFAULT_GDRIVE_FOLDER_ID
GDRIVE_API_KEY: str = os.getenv("GDRIVE_API_KEY", "").strip()
GDRIVE_SERVICE_ACCOUNT_JSON: str = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()

DOWNLOAD_DIR: Path = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
if not DOWNLOAD_DIR.is_absolute():
    DOWNLOAD_DIR = BASE_DIR / DOWNLOAD_DIR

DATA_DIR: Path = BASE_DIR / "data"
DB_PATH: Path = DATA_DIR / "state.db"
SESSION_PATH: Path = DATA_DIR / "session.json"

CHECK_INTERVAL_MINUTES: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
HEADLESS: bool = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

API_BASE = "https://api-pao.utpxpedition.com"
TOKEN_URL = "https://auth.utp.edu.pe/realms/Xpedition/protocol/openid-connect/token"
CLASS_URL = "https://class.utp.edu.pe/"
CLIENT_ID = "pao-web"


def ensure_dirs() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
