"""Domain and keyword-based content filtering logic."""

from pathlib import Path
from typing import List


class FilterEngine:
    """Loads and checks blocked domains and keywords for filtering."""

    def __init__(self, blocked_domains_path: str) -> None:
        self.blocked_domains_path = Path(blocked_domains_path)
        self.blocked_domains = self._load_blocked_domains()
        self.blocked_keywords: List[str] = ["adult", "malware", "phishing"]

    def _load_blocked_domains(self) -> List[str]:
        if not self.blocked_domains_path.exists():
            return []
        return [
            line.strip()
            for line in self.blocked_domains_path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def is_blocked(self, host: str, url: str) -> bool:
        host_lower = host.lower()
        url_lower = url.lower()
        for domain in self.blocked_domains:
            if host_lower == domain.lower() or host_lower.endswith(f".{domain.lower()}"):
                return True
        for keyword in self.blocked_keywords:
            if keyword in url_lower:
                return True
        return False
