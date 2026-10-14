"""
Configuration settings for the Email Review project.
Secrets are loaded from .secrets file.
"""
import os

def _load_secrets():
    """Load secrets from .secrets file."""
    secrets = {}
    secrets_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.secrets')
    
    if os.path.exists(secrets_path):
        with open(secrets_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    secrets[key.strip()] = value.strip()
    return secrets

_secrets = _load_secrets()

# Secrets (loaded from .secrets file)
CLAUDE_API_KEY = _secrets.get('CLAUDE_API_KEY', '')
EMAIL_ADDRESS = _secrets.get('EMAIL_ADDRESS', None)
APP_PASSWORD = _secrets.get('APP_PASSWORD', None)

# AI model. Opus 5.5 by default; set CLAUDE_MODEL in .secrets to e.g.
# claude-haiku-4-5-20251001 for a cheaper classification pass.
CLAUDE_MODEL = _secrets.get('CLAUDE_MODEL', '').strip() or "claude-opus-5-5"
# Senders with fewer emails than this are skipped by the AI pass (1 = analyze all).
AI_MIN_SENDER_COUNT = 1

# IMAP Server Settings
IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993
IMAP_TIMEOUT = 60          # seconds of socket silence before a command is retried
IMAP_MAX_RETRIES = 8       # reconnect attempts per command (~4 min with backoff) before giving up
SCAN_CHUNK_SIZE = 500      # UIDs per header FETCH while scanning
UID_COMMAND_CHUNK = 250    # max UIDs per destructive command (COPY/STORE/EXPUNGE)…
UID_COMMAND_MAX_CHARS = 1500  # …and max length of its sequence-set string
# iCloud answers NO to bulk commands once it throttles the account (seen 2026-10-10 after
# ~14k messages in 18 min with 2,000-UID batches; 250-UID batches ran for an hour cleanly).
# Stop a folder after this many refused batches in a row instead of churning for hours.
IMAP_MAX_CONSECUTIVE_REFUSALS = 5

# Sender index (the scan result; survives restarts, supports resume)
SENDER_INDEX_FILE = "data/sender_index.json"
# Share of a sender's mail carrying List-Unsubscribe to call the sender "bulk"
BULK_RATIO_THRESHOLD = 0.8

# Review Settings
BATCH_SIZE = 50  # Emails per review session
MAX_PREVIEW_LENGTH = 300

# Data Files
DATA_DIR = "data"
DECISIONS_FILE = "data/decisions.json"
CATEGORIES_FILE = "data/categories.json"

# Folders to skip in overview
SKIP_FOLDERS = {"Drafts", "Notes", "Sent Messages", "Deleted Messages", "Trash"}

# Trash folder for moving deleted emails (reversible deletion)
TRASH_FOLDER = "Deleted Messages"  # iCloud default; auto-detected at runtime

# Folders where --hard-delete (permanent expunge) is allowed.
# Anywhere else, messages can only be moved to Trash (safer, recoverable).
HARD_DELETE_ALLOWED_FOLDERS = {"Junk", "promotions", "Deleted Messages", "Trash"}

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
