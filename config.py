"""
Configuration and credential management for TeraBox Downloader.
Stores credentials, session cookies, and app constants.
"""

import json
import os

# ─── Constants ───────────────────────────────────────────────────────────────

APP_ID = "250528"
CHANNEL = "dubox"
CLIENTTYPE = "0"
WEB = "1"

# TeraBox base host (may change due to region redirects)
DEFAULT_HOST = "https://www.1024terabox.com"

# User-Agent string
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Supported TeraBox domains for URL validation
SUPPORTED_DOMAINS = [
    "terabox.com", "www.terabox.com",
    "terabox.app", "www.terabox.app",
    "terabox.fun", "www.terabox.fun",
    "teraboxapp.com", "www.teraboxapp.com",
    "1024tera.com", "www.1024tera.com",
    "1024terabox.com", "www.1024terabox.com",
    "freeterabox.com", "www.freeterabox.com",
    "mirrobox.com", "www.mirrobox.com",
    "nephobox.com", "www.nephobox.com",
    "4funbox.co", "www.4funbox.co",
    "momerybox.com", "www.momerybox.com",
    "tibibox.com", "www.tibibox.com",
    "terasharefile.com", "www.terasharefile.com",
]

# Download settings
DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB

# Config file path
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# ─── Config Management ───────────────────────────────────────────────────────


def load_config() -> dict:
    """Load config from config.json. Returns empty dict if not found."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(data: dict):
    """Save config dict to config.json."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_credentials() -> tuple[str, str]:
    """
    Get TeraBox email and password from config.
    Returns (email, password) or raises ValueError if not configured.
    """
    cfg = load_config()
    email = cfg.get("email", "")
    password = cfg.get("password", "")
    if not email or not password:
        raise ValueError(
            "TeraBox credentials not configured. "
            "Please add 'email' and 'password' to config.json:\n"
            f'  {CONFIG_FILE}\n'
            '  Example: {"email": "you@example.com", "password": "yourpass"}'
        )
    return email, password


def get_cached_session() -> str | None:
    """Get cached ndus session cookie, or None if not cached."""
    cfg = load_config()
    return cfg.get("ndus")


def save_session(ndus: str):
    """Cache the ndus session cookie to config.json."""
    cfg = load_config()
    cfg["ndus"] = ndus
    save_config(cfg)


def get_host() -> str:
    """Get the TeraBox API host (may be updated by region redirect)."""
    cfg = load_config()
    return cfg.get("host", DEFAULT_HOST)


def save_host(host: str):
    """Save updated TeraBox host to config."""
    cfg = load_config()
    cfg["host"] = host
    save_config(cfg)
