"""
Shared utilities: retry decorator and logging setup.

Used by all pipeline scripts.
"""

import logging
import time
import functools
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def setup_logging(name: str = "investing") -> logging.Logger:
    """
    Configure root logger: rotating file + stderr.
    Call once at the top of each entry-point script.
    """
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"{name}.log"

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=7, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(stream_handler)

    return logging.getLogger(name)


# RotatingFileHandler lives in logging.handlers — import lazily to avoid
# circular issues when this module is imported before logging is fully ready.
import logging.handlers  # noqa: E402  (must be after setup_logging definition)


def extract_json_object(text: str) -> str | None:
    """Find the first top-level JSON object using balanced-brace matching.

    Much safer than the greedy ``re.search(r"\\{.*\\}", raw, re.DOTALL)``
    pattern which breaks on nested braces inside markdown or string values.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def retry(max_attempts: int = 3, backoff: float = 2.0, exceptions: tuple = (Exception,)):
    """
    Decorator: retry a function up to max_attempts times with exponential backoff.

    Usage:
        @retry(max_attempts=3, backoff=2.0, exceptions=(requests.RequestException,))
        def call_api(): ...
    """
    def decorator(fn):
        log = logging.getLogger(fn.__module__)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    if attempt == max_attempts - 1:
                        log.error("%s failed after %d attempts: %s", fn.__name__, max_attempts, exc)
                        raise
                    wait = backoff ** attempt
                    log.warning(
                        "%s attempt %d/%d failed: %s — retrying in %.1fs",
                        fn.__name__, attempt + 1, max_attempts, exc, wait,
                    )
                    time.sleep(wait)

        return wrapper
    return decorator
