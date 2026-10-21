"""
Configuration settings for the Email Review project.

One installation, several mailboxes. A *profile* names a mailbox:

    ./venv/bin/python main.py scan                    # profile "icloud": reads .secrets, data in data/
    ./venv/bin/python main.py scan --profile gmail    # reads .secrets.gmail, data in data/gmail/
    EMAIL_PROFILE=gmail ./venv/bin/python main.py scan   # same thing via the environment

The secrets file of a profile holds EMAIL_ADDRESS and APP_PASSWORD and may override
PROVIDER (icloud | gmail), IMAP_HOST, IMAP_PORT, TRASH_FOLDER, SCAN_FOLDERS (comma-separated)
and SKIP_FOLDERS. CLAUDE_API_KEY and CLAUDE_MODEL are taken from the profile file first,
then from .secrets, so the API key is written once.
"""
import os
import sys


def _resolve_profile() -> str:
    """`--profile NAME` anywhere on the command line wins (and is removed so the scripts'
    own argument parsing never sees it), then $EMAIL_PROFILE, then "icloud"."""
    argv = sys.argv
    if "--profile" in argv:
        i = argv.index("--profile")
        if i + 1 < len(argv):
            name = argv[i + 1]
            del argv[i:i + 2]
            return name.strip().lower()
    return (os.environ.get("EMAIL_PROFILE") or "icloud").strip().lower()


PROFILE = _resolve_profile()
_ROOT = os.path.dirname(os.path.dirname(__file__))


def _read_kv(path: str) -> dict:
    out = {}
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    out[key.strip()] = value.strip()
    return out


def _load_secrets(profile: str) -> dict:
    """Profile file first (.secrets.<profile>; for icloud also plain .secrets), with the shared
    .secrets filling in anything missing (the Claude API key, typically)."""
    shared = _read_kv(os.path.join(_ROOT, '.secrets'))
    own_path = os.path.join(_ROOT, f'.secrets.{profile}')
    own = _read_kv(own_path)
    if profile == "icloud" and not own:
        return shared
    merged = {k: v for k, v in shared.items() if k in ("CLAUDE_API_KEY", "CLAUDE_MODEL")}
    merged.update(own)
    return merged


_secrets = _load_secrets(PROFILE)
SECRETS_FILE = ".secrets" if PROFILE == "icloud" and not os.path.exists(os.path.join(_ROOT, ".secrets.icloud")) else f".secrets.{PROFILE}"

# Secrets
CLAUDE_API_KEY = _secrets.get('CLAUDE_API_KEY', '')
EMAIL_ADDRESS = _secrets.get('EMAIL_ADDRESS', None)
APP_PASSWORD = _secrets.get('APP_PASSWORD', None)

# AI model. Opus 5.5 by default; set CLAUDE_MODEL in .secrets to e.g.
# claude-haiku-4-5-20251001 for a cheaper classification pass.
CLAUDE_MODEL = _secrets.get('CLAUDE_MODEL', '').strip() or "claude-opus-5-5"
# Senders with fewer emails than this are skipped by the AI pass (1 = analyze all).
AI_MIN_SENDER_COUNT = 1

# ── Provider defaults ─────────────────────────────────────────────────────────
# Gmail exposes labels as folders. "[Gmail]/All Mail" holds every message once, so it is the
# only folder worth scanning; moving a message from there to "[Gmail]/Trash" removes it from
# every label. Localised accounts name these differently ("[Gmail]/Tutti i messaggi",
# "[Gmail]/Cestino"); the client finds them through the LIST special-use flags \All and \Trash.
PROVIDERS = {
    "icloud": {
        "host": "imap.mail.me.com",
        "trash": "Deleted Messages",
        "scan_folders": None,                       # every folder except SKIP_FOLDERS
        "skip": {"Drafts", "Notes", "Sent Messages", "Deleted Messages", "Trash"},
        "hard_delete": {"Junk", "promotions", "Deleted Messages", "Trash"},
        "password_help": "https://appleid.apple.com → Sign-In & Security → App-Specific Passwords",
    },
    "gmail": {
        "host": "imap.gmail.com",
        "trash": "[Gmail]/Trash",
        "scan_folders": ["[Gmail]/All Mail"],
        "skip": {"INBOX", "[Gmail]/Sent Mail", "[Gmail]/Drafts", "[Gmail]/Spam", "[Gmail]/Trash",
                 "[Gmail]/Important", "[Gmail]/Starred", "[Gmail]"},
        "hard_delete": {"[Gmail]/Spam", "[Gmail]/Trash"},
        "password_help": "https://myaccount.google.com/apppasswords (needs 2-Step Verification)",
    },
}


def _guess_provider() -> str:
    p = _secrets.get("PROVIDER", "").strip().lower()
    if p in PROVIDERS:
        return p
    if PROFILE in PROVIDERS:
        return PROFILE
    domain = (EMAIL_ADDRESS or "").rsplit("@", 1)[-1].lower()
    if domain in ("gmail.com", "googlemail.com") or "google" in domain:
        return "gmail"
    return "icloud"


PROVIDER = _guess_provider()
_P = PROVIDERS[PROVIDER]
# Google shows App Passwords as four groups of four letters; the spaces are not part of it.
if PROVIDER == "gmail" and APP_PASSWORD:
    APP_PASSWORD = APP_PASSWORD.replace(" ", "")


def _split_list(value: str):
    return [x.strip() for x in value.split(",") if x.strip()]


# IMAP Server Settings
IMAP_HOST = _secrets.get("IMAP_HOST", "").strip() or _P["host"]
IMAP_PORT = int(_secrets.get("IMAP_PORT", "").strip() or 993)
IMAP_TIMEOUT = 60          # seconds of socket silence before a command is retried
IMAP_MAX_RETRIES = 8       # reconnect attempts per command (~4 min with backoff) before giving up
SCAN_CHUNK_SIZE = 500      # UIDs per header FETCH while scanning
UID_COMMAND_CHUNK = 250    # max UIDs per destructive command (COPY/STORE/EXPUNGE)…
UID_COMMAND_MAX_CHARS = 1500  # …and max length of its sequence-set string
# iCloud answers NO to bulk commands once it throttles the account (seen 2026-10-10 after
# ~14k messages in 18 min with 2,000-UID batches; 250-UID batches ran for an hour cleanly).
# Stop a folder after this many refused batches in a row instead of churning for hours.
IMAP_MAX_CONSECUTIVE_REFUSALS = 5
PASSWORD_HELP = _P["password_help"]

# Data Files — one directory per profile; the original mailbox keeps data/ as before
DATA_DIR = "data" if PROFILE == "icloud" else os.path.join("data", PROFILE)
# Sender index (the scan result; survives restarts, supports resume)
SENDER_INDEX_FILE = os.path.join(DATA_DIR, "sender_index.json")
DECISIONS_FILE = os.path.join(DATA_DIR, "decisions.json")
CATEGORIES_FILE = os.path.join(DATA_DIR, "categories.json")
# Share of a sender's mail carrying List-Unsubscribe to call the sender "bulk"
BULK_RATIO_THRESHOLD = 0.8

# Review Settings
BATCH_SIZE = 50  # Emails per review session
MAX_PREVIEW_LENGTH = 300

# Folders to scan (None = everything not in SKIP_FOLDERS) and folders to skip in overviews
SCAN_FOLDERS = _split_list(_secrets["SCAN_FOLDERS"]) if _secrets.get("SCAN_FOLDERS") else _P["scan_folders"]
SKIP_FOLDERS = set(_split_list(_secrets["SKIP_FOLDERS"])) if _secrets.get("SKIP_FOLDERS") else set(_P["skip"])

# Trash folder for moving deleted emails (reversible deletion); auto-detected at runtime
TRASH_FOLDER = _secrets.get("TRASH_FOLDER", "").strip() or _P["trash"]

# Folders where --hard-delete (permanent expunge) is allowed.
# Anywhere else, messages can only be moved to Trash (safer, recoverable).
HARD_DELETE_ALLOWED_FOLDERS = set(_P["hard_delete"]) | {TRASH_FOLDER}

# Plan / execution artifacts
PLAN_FILE_PREFIX = "plan_"
EXECUTION_FILE_PREFIX = "execution_"

# Category Definitions
CATEGORIES = {
    "newsletters": {
        "emoji": "📰",
        "description": "News digests, daily updates, blogs",
        "domains": ["medium.com", "substack.com", "ilsole24ore.com", "repubblica.it",
                    "economist.com", "nytimes.com", "wsj.com", "theguardian.com"],
        "keywords": ["digest", "newsletter", "weekly", "daily", "update", "news"]
    },
    "promotions": {
        "emoji": "🏷️",
        "description": "Marketing, sales, discounts",
        "domains": ["urbanoutfitters.com", "vans.com", "hockerty.com", "reebok.com",
                    "aliexpress.com", "amazon.com", "ebay.com"],
        "keywords": ["sale", "discount", "off", "deal", "promo", "offer", "save", "free shipping"]
    },
    "social": {
        "emoji": "👥",
        "description": "Social networks, professional networks",
        "domains": ["linkedin.com", "facebook.com", "twitter.com", "instagram.com",
                    "meetup.com", "eventbrite.com"],
        "keywords": ["connection", "network", "invite", "joined", "liked", "commented"]
    },
    "finance": {
        "emoji": "💰",
        "description": "Banks, investments, payments",
        "domains": ["scotiabank.com", "questrade.com", "paypal.com", "stripe.com",
                    "october.eu", "wise.com", "revolut.com"],
        "keywords": ["account", "balance", "payment", "transfer", "statement", "transaction"]
    },
    "travel": {
        "emoji": "✈️",
        "description": "Airlines, hotels, bookings",
        "domains": ["airbnb.com", "booking.com", "expedia.com", "italotreno.it",
                    "ryanair.com", "easyjet.com", "hotels.com"],
        "keywords": ["booking", "reservation", "flight", "hotel", "trip", "travel", "itinerary"]
    },
    "shopping": {
        "emoji": "📦",
        "description": "Orders, shipping, receipts",
        "domains": ["amazon.com", "apple.com", "ups.com", "fedex.com", "dhl.com"],
        "keywords": ["order", "shipped", "delivered", "tracking", "receipt", "invoice", "purchase"]
    },
    "fitness": {
        "emoji": "💪",
        "description": "Fitness, nutrition, health",
        "domains": ["crossfit.com", "revolution-nutrition.com", "myfitnesspal.com",
                    "strava.com", "nike.com"],
        "keywords": ["workout", "training", "nutrition", "fitness", "exercise", "gym"]
    },
    "notifications": {
        "emoji": "🔔",
        "description": "Alerts, system notifications",
        "domains": [],
        "keywords": ["alert", "notification", "reminder", "verify", "confirm", "security"]
    },
    "other": {
        "emoji": "📧",
        "description": "Uncategorized emails",
        "domains": [],
        "keywords": []
    }
}
