"""Logging rules and helpers for the whole project.

PHI RULE
Every log line, at any level (DEBUG, INFO, WARNING, ERROR), logs ids and counts only.
Never log a message, medications, names, or any free-form content.
Rule of thumb: "is this an id or a count?" If no, don't log it.
"""
import logging
from time import perf_counter


def configure_logging(level: int = logging.INFO):
    logging.basicConfig(level=level,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')


def elapsed_ms(started: float) -> float:
    """ Milliseconds since `started`, a `perf_counter()` value. """
    return (perf_counter() - started) * 1000
