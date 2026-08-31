import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

PROXY_URL = os.getenv("PROXY_URL") or None
DATA_ROOT = Path(os.getenv("DATA_ROOT", str(Path.home() / "data" / "crawls")))
HEADLESS = os.getenv("HEADLESS", "1") == "1"
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "30000"))
