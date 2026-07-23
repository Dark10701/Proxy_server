"""Filesystem locations resolved relative to the checkout, not the CWD.

The proxy used to default to paths like ``config/blocked_domains.txt``,
which only resolved when it was launched from inside ``proxy_server/``.
Anchoring on ``__file__`` lets it run from anywhere.
"""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

DEFAULT_BLOCKED_DOMAINS = PACKAGE_DIR / "config" / "blocked_domains.txt"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_METRICS = DEFAULT_LOG_DIR / "metrics.csv"
DEFAULT_ACCESS_LOG = DEFAULT_LOG_DIR / "access.log"
DEFAULT_ERROR_LOG = DEFAULT_LOG_DIR / "error.log"
