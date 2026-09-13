from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit


TOUR_API_IMAGE_HOST = "tong.visitkorea.or.kr"


def normalize_tourapi_image_url(value: Any) -> str | None:
    """Return a browser-safe absolute image URL supplied by TourAPI."""
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return None

    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or scheme not in {"http", "https"}
    ):
        return None

    if scheme == "https":
        return candidate

    if hostname.lower() != TOUR_API_IMAGE_HOST:
        return None

    return urlunsplit(
        ("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )
