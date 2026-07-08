from pathlib import Path

# Project root
BASE_DIR = Path(__file__).resolve().parent

# Database
DATABASE_PATH = BASE_DIR / "database" / "dastabase.db"

# Cache
CACHE_DIR = BASE_DIR / "cache"
WEBSITE_CACHE_DIR = CACHE_DIR / "websites"

# Logs
LOG_DIR = BASE_DIR / "logs"

# Exports
EXPORT_DIR = BASE_DIR / "exports"

# Documentation
DOCS_DIR = BASE_DIR / "docs"

# Current enrichment version
ENRICHMENT_VERSION = 1

# Request settings
REQUEST_TIMEOUT = 20

USER_AGENT = (
    "Mozilla/5.0 "
    "(Macintosh; Intel Mac OS X) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/138.0 Safari/537.36"
)
