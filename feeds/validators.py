import ipaddress
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

PRIVATE_SUFFIXES = (".localhost", ".internal", ".local", ".lan", ".home.arpa")


def validate_public_url(value):
    """Reject feed URLs aimed at this server or a private network.

    Hostnames are also resolved and checked before every request (including
    each redirect hop) in feeds.ingest.check_url.
    """
    host = (urlsplit(value).hostname or "").rstrip(".").lower()
    if host == "localhost" or host.endswith(PRIVATE_SUFFIXES):
        raise ValidationError("Use a public feed address, not a local or internal host.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValidationError("Use a public feed address, not a private IP.")
