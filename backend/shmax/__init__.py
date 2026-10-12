"""SHMAX — Self-hosted MAX <-> Telegram gateway."""

from pathlib import Path as _Path


def _read_version() -> str:
    """Canonical version source (live acceptance #13): the VERSION
    file shipped next to the package. Release checkouts commit this
    file with the committed release version; the build verifies it without rewriting."""
    try:
        return _Path(__file__).with_name("VERSION").read_text(
            encoding="utf-8"
        ).strip() or "0.0.0-dev"
    except OSError:
        return "0.0.0-dev"


__version__ = _read_version()
__service_name__ = "shmax"
