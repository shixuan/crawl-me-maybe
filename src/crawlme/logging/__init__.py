"""Structured logging — configure once from Settings, then use stdlib loggers.

Usage:
    from crawlme.logging import setup_logging
    from crawlme.config import Settings

    setup_logging(Settings())

    # Then in any module:
    import logging
    logger = logging.getLogger(__name__)
"""

from crawlme.logging.config import setup_logging, to_file

__all__ = ["setup_logging", "to_file"]
