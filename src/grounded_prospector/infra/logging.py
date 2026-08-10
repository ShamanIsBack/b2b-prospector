"""Console logging setup."""

from __future__ import annotations

import logging
import sys

from rich.console import Console
from rich.logging import RichHandler


def configure_console_encoding() -> None:
    """Make stdout and stderr able to print any name we might find.

    Windows consoles default to a regional code page (cp1250 here), which cannot
    encode Arabic script or the bidirectional control marks that appear in Gulf
    LinkedIn titles. Printing a perfectly good result would then crash the run
    with a UnicodeEncodeError. Switching the streams to UTF-8, and replacing
    anything still unencodable, keeps display problems from becoming failures.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover -- captured streams in tests
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover -- already-detached stream
            continue


console = Console(stderr=True)


def setup_logging(*, verbose: bool = False) -> logging.Logger:
    """Configure rich console logging and return the package logger.

    Logs go to stderr so that piping stdout to a file captures results without
    interleaved progress noise.
    """
    configure_console_encoding()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose)],
        force=True,
    )
    # The SDKs are chatty at DEBUG and drown out our own output.
    for noisy in ("httpx", "httpcore", "google_genai", "google.genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("grounded_prospector")
