"""Logging utilities for access and error logs."""

import logging
from pathlib import Path
from typing import Optional


class ProxyLogger:
    """Configure logging for access and error events."""

    def __init__(self, access_log_path: str, error_log_path: str) -> None:
        self.access_log_path = Path(access_log_path)
        self.error_log_path = Path(error_log_path)
        self.access_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.error_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("proxy")
        self.logger.setLevel(logging.INFO)
        self._configure_handlers()

    def _configure_handlers(self) -> None:
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S"
        )

        if not self.logger.handlers:
            access_handler = logging.FileHandler(self.access_log_path)
            access_handler.setLevel(logging.INFO)
            access_handler.setFormatter(formatter)

            error_handler = logging.FileHandler(self.error_log_path)
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(formatter)

            stream_handler = logging.StreamHandler()
            stream_handler.setLevel(logging.INFO)
            stream_handler.setFormatter(formatter)

            self.logger.addHandler(access_handler)
            self.logger.addHandler(error_handler)
            self.logger.addHandler(stream_handler)

    def info(self, message: str, *args: Optional[object]) -> None:
        self.logger.info(message, *args)

    def error(self, message: str, *args: Optional[object]) -> None:
        self.logger.error(message, *args)
