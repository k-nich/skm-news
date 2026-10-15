import ipaddress
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ValidationError

PRIVATE_SUFFIXES = (".localhost", ".internal", ".local", ".lan", ".home.arpa")


def validate_public_url(value):
    """Reject feed URLs aimed at this server or a private network.

    Hostnames are also resolved and checked before every request (including
    each redirect hop) in feeds.ingest.check_url, and the connection is pinned
    to the address checked.
    """
    if "\\" in value:
        raise ValidationError("Use a plain feed address (no backslashes).")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ValidationError("Enter a valid feed address.") from exc
    if "@" in parts.netloc:
        raise ValidationError("Use a feed address without credentials.")
    host = (parts.hostname or "").rstrip(".").lower()
    if host == "localhost" or host.endswith(PRIVATE_SUFFIXES):
        raise ValidationError("Use a public feed address, not a local or internal host.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValidationError("Use a public feed address, not a private IP.")


def normalise_feed_url(value):
    """Lower-case scheme and host, drop the fragment and an empty '?'."""
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )
