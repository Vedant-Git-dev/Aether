"""Logging configuration for Aether."""

from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    level = os.environ.get("AETHER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Quiet the chatty third-party loggers
    for name in ("httpx", "httpcore", "asyncio", "slack_bolt", "matplotlib"):
        logging.getLogger(name).setLevel(logging.WARNING)
