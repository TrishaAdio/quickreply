"""Validation and redaction helpers. Never include secret values in messages."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from qr.config.settings import DEFAULT_DB_NAME

_API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_mongo_url(url: str) -> str | None:
    """Return a readable problem description, or None when the URL is usable."""
    url = (url or "").strip()
    if not url:
        return "MONGO_URL is empty."
    if not (url.startswith("mongodb://") or url.startswith("mongodb+srv://")):
        return "MONGO_URL must start with mongodb:// or mongodb+srv://"
    if any(ch.isspace() for ch in url):
        return "MONGO_URL must not contain spaces."
    if "YOUR_CONNECTION_STRING" in url or "<password>" in url:
        return "MONGO_URL still contains the placeholder text from .env.example."
    try:
        from pymongo import uri_parser
    except ImportError:
        return None  # dependency not installed yet; format checks above passed
    try:
        # Offline validation: SRV URIs are checked structurally because a full
        # parse would perform DNS lookups.
        if url.startswith("mongodb+srv://"):
            _parse_srv_offline(url)
        else:
            uri_parser.parse_uri(url, validate=True, warn=False)
    except Exception as error:  # noqa: BLE001
        message = str(error)
        # pymongo messages may echo the URI; strip anything that looks like it.
        message = message.replace(url, "<MONGO_URL>")
        return f"MONGO_URL is not a valid MongoDB URI ({type(error).__name__}: {message})"
    return None


def _parse_srv_offline(url: str) -> None:
    """Validate an SRV URI shape without performing DNS lookups."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host or host.count(".") < 2:
        raise ValueError("mongodb+srv:// needs a full host name such as cluster0.abcde.mongodb.net")
    if parts.port:
        raise ValueError("mongodb+srv:// URIs must not include a port")
    if "," in (parts.netloc.rsplit("@", 1)[-1]):
        raise ValueError("mongodb+srv:// URIs must contain exactly one host")


def validate_api_id(value: str) -> str | None:
    value = str(value or "").strip()
    if not value.isdigit() or int(value) <= 0:
        return "API_ID must be a positive number from https://my.telegram.org"
    return None


def validate_api_hash(value: str) -> str | None:
    if not _API_HASH_RE.match(str(value or "").strip()):
        return "API_HASH must be the 32-character hex string from https://my.telegram.org"
    return None


def validate_session_name(value: str) -> str | None:
    if not _SESSION_NAME_RE.match(value or ""):
        return "Session names may only use letters, digits, '-' and '_'."
    return None


def database_name(url: str) -> str:
    """Database encoded in the URI path, else the documented default."""
    path = urlsplit(url).path.lstrip("/")
    name = path.split("?", 1)[0].strip()
    return name or DEFAULT_DB_NAME


def redact_mongo_url(url: str) -> str:
    """Return scheme + host only, e.g. ``mongodb+srv://***@cluster0.x.net/db``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid MONGO_URL>"
    hosts = parts.netloc.rsplit("@", 1)[-1]
    creds = "***@" if "@" in parts.netloc else ""
    return f"{parts.scheme}://{creds}{hosts}/{database_name(url)}"


def mask_secret(value: str) -> str:
    return "set (hidden)" if value else "not set"


def scrub(text: str, secrets) -> str:
    """Remove every secret occurrence from an arbitrary message."""
    text = str(text)
    for secret in secrets or ():
        if secret and len(str(secret)) >= 4:
            text = text.replace(str(secret), "<redacted>")
    return text
