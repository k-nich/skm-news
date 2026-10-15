"""Fetch public RSS/Atom feeds and store their entries as FeedItems.

Runs from the `ingest_feeds` management command (a Railway cron service).
Downloads run in daemon threads; parsing and every database write happen in
the calling thread, so the ORM is never used concurrently and a hung socket
can't stop the process exiting (Railway skips a cron run while the previous
one is still active).
"""
from __future__ import annotations

import calendar
import hashlib
import html
import io
import ipaddress
import logging
import queue
import re
import socket
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from xml.sax import SAXException

import feedparser
import requests
from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.html import strip_tags

from .models import Feed, FeedItem

log = logging.getLogger(__name__)

USER_AGENT = "SkimNewsBot/1.0 (+https://skm.news)"
ACCEPT = (
    "application/rss+xml, application/atom+xml, application/rdf+xml;q=0.9, "
    "application/xml;q=0.8, text/xml;q=0.8, */*;q=0.5"
)
CONNECT_TIMEOUT = 5  # seconds to connect
READ_TIMEOUT = 15  # seconds of silence before giving up
MAX_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_ENTRIES = 200  # per document; news feeds carry 10-100

TITLE_MAX = 500
SUMMARY_MAX = 2000
AUTHOR_MAX = 200
LINK_MAX = 2000
GUID_MAX = 2000
RAW_TEXT_MAX = 20_000  # input cap per field before HTML stripping
EARLIEST = datetime(2000, 1, 1, tzinfo=dt_timezone.utc)  # older = placeholder date

LOCK_KEY = 0x534B4D46  # Postgres advisory lock id for ingest runs

_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
# Block-level tags become spaces so "<p>One.</p><p>Two.</p>" reads "One. Two."
_BLOCK_TAG = re.compile(
    r"<\s*/?\s*(?:p|br|div|li|ul|ol|h[1-6]|blockquote|tr|td|figure|figcaption)\b[^<>]*>",
    re.I,
)
_ANY_TAG = re.compile(r"<[^<>]*>")
_CHAR_REF = re.compile(rb"&#(?:[xX]([0-9a-fA-F]+)|([0-9]+));")
# feedparser's fallback parser expands entities without limit (memory bomb).
_ENTITY_DECLS = tuple(
    "<!ENTITY".encode(codec)
    for codec in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
)
_TRACKING_PARAM = re.compile(
    r"^(utm_\w+|fbclid|gclid|dclid|mc_cid|mc_eid|ocid|cmpid|sr_share)$", re.I
)
# feedparser reads these zone names as UTC.
_LOCAL_ZONES = {
    "AEST": "+1000", "AEDT": "+1100", "ACST": "+0930", "ACDT": "+1030",
    "AWST": "+0800", "NZST": "+1200", "NZDT": "+1300",
}
_LOCAL_ZONE_SUFFIX = re.compile(r"\s(" + "|".join(_LOCAL_ZONES) + r")\s*$")


def retention_days():
    return getattr(settings, "FEEDS_RETENTION_DAYS", 14)


def sha256(value):
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _describe(exc):
    return strip_controls(f"{type(exc).__name__}: {exc}")[:500]


# --- text -------------------------------------------------------------------


def strip_controls(value):
    """Drop characters Postgres or HTML can't take (NUL, lone surrogates, C0 controls)."""
    return _CONTROL.sub("", value)


def neutralise(text):
    """The front page inserts story text with innerHTML, so no '<' may survive."""
    return text.replace("<", "‹").replace(">", "›")


def truncate(text, limit):
    """Cut at a word boundary and add an ellipsis if longer than `limit`."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


def _strip_tags(text):
    try:
        return strip_tags(text)
    except SuspiciousOperation:  # Django refuses pathological markup
        return _ANY_TAG.sub(" ", text)


def clean_text(value, limit):
    """Feed HTML -> single-line plain text, safe to render as HTML."""
    if not value:
        return ""
    text = strip_controls(str(value)[:RAW_TEXT_MAX])
    for _ in range(4):  # markup can arrive entity-escaped more than once
        cleaned = html.unescape(_strip_tags(_BLOCK_TAG.sub(" ", text)))
        if cleaned == text:
            break
        text = cleaned
    text = _WS.sub(" ", strip_controls(neutralise(text))).strip()
    return truncate(text, limit)


def link_key(url):
    """Hash of a link with scheme, www., trailing slash and tracking params removed."""
    if not url:
        return ""
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_PARAM.match(k)
        )
    )
    # The fragment stays: live blogs give each post its own #anchor.
    return sha256(f"{host}{path}?{query}#{parts.fragment}")


# --- fetching ---------------------------------------------------------------


class FetchError(Exception):
    pass


def _address_allowed(address):
    """Only public internet addresses (tests relax this for a local server)."""
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global


def check_url(url):
    """Raise FetchError unless `url` is http(s) and its host resolves only to public addresses."""
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme not in ("http", "https") or not host:
        raise FetchError(f"Not an http(s) URL: {url[:200]}")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError) as exc:
        raise FetchError(f"Can't resolve {host}: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not _address_allowed(address):
            raise FetchError(f"Blocked: {host} resolves to non-public address {address}")


@dataclass(frozen=True)
class FetchRequest:
    feed_id: int
    url: str
    etag: str = ""
    last_modified: str = ""


@dataclass
class FetchResult:
    feed_id: int
    status: int | None = None
    content: bytes = b""
    etag: str = ""
    last_modified: str = ""
    content_type: str = ""
    final_url: str = ""
    error: str = ""


def _header(resp, name, limit):
    return strip_controls(resp.headers.get(name, ""))[:limit]


def fetch(req):
    """Conditional GET of one feed. Never raises; failures land in `.error`."""
    result = FetchResult(feed_id=req.feed_id, final_url=req.url)
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT}
    if req.etag:
        headers["If-None-Match"] = req.etag
    if req.last_modified:
        headers["If-Modified-Since"] = req.last_modified
    url = req.url
    try:
        # Redirects are followed by hand so every hop passes check_url.
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url)
            resp = requests.get(
                url,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                stream=True,
                allow_redirects=False,
            )
            if not resp.is_redirect:
                break
            resp.close()
            url = urljoin(url, resp.headers["Location"])
        else:
            raise FetchError(f"More than {MAX_REDIRECTS} redirects")
        with resp:
            result.status = resp.status_code
            result.final_url = url
            result.etag = _header(resp, "ETag", 255)
            result.last_modified = _header(resp, "Last-Modified", 64)
            result.content_type = _header(resp, "Content-Type", 200)
            if resp.status_code == 304:
                return result
            if resp.status_code >= 400:
                result.error = f"HTTP {resp.status_code}"
                return result
            body, size = [], 0
            for chunk in resp.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    result.error = f"Response over {MAX_BYTES // (1024 * 1024)} MB"
                    return result
                body.append(chunk)
            result.content = b"".join(body)
    except FetchError as exc:
        result.error = str(exc)
    except requests.RequestException as exc:
        result.error = _describe(exc)
    except Exception as exc:  # a worker thread must report, not die
        log.exception("Unexpected error fetching %s", req.url)
        result.error = _describe(exc)
    return result


def _fetch_all(reqs, *, workers, end, started):
    """Yield FetchResults as downloads finish, until `end` (monotonic).

    Feed ids whose download began are added to `started`.
    """
    jobs, results = queue.SimpleQueue(), queue.SimpleQueue()
    stop, lock = threading.Event(), threading.Lock()
    for req in reqs:
        jobs.put(req)

    def worker():
        while not stop.is_set():
            try:
                req = jobs.get_nowait()
            except queue.Empty:
                return
            with lock:
                started.add(req.feed_id)
            results.put(fetch(req))

    for i in range(max(1, min(workers, len(reqs)))):
        threading.Thread(target=worker, name=f"feed-fetch-{i}", daemon=True).start()
    try:
        for _ in reqs:
            try:
                yield results.get(timeout=max(0.0, end - time.monotonic()))
            except queue.Empty:
                return
    finally:
        stop.set()


# --- parsing ----------------------------------------------------------------


class ParseError(Exception):
    pass


@dataclass
class Entry:
    guid: str
    link: str
    title: str
    summary: str
    author: str
    published_at: datetime


@dataclass
class Parsed:
    entries: list
    well_formed: bool  # False if the XML broke off, e.g. a truncated download


def _fix_char_refs(content):
    """Replace numeric character references that make feedparser raise."""

    def replace(match):
        digits = match.group(1) or match.group(2)
        if len(digits) <= 8:
            code = int(digits, 16) if match.group(1) else int(digits)
            if code and not 0xD800 <= code <= 0xDFFF and code <= 0x10FFFF:
                return match.group(0)
        return b"&#65533;"

    return _CHAR_REF.sub(replace, content)


def parse(content, *, base_url="", content_type="", now=None):
    """Parse RSS 0.9x/1.0/2.0 or Atom bytes into normalised Entries."""
    now = now or timezone.now()
    if not content or not content.strip():
        raise ParseError("Empty response")
    if any(decl in content for decl in _ENTITY_DECLS):
        raise ParseError("Document declares XML entities")
    # No content-location: feedparser would turn bare GUIDs into URLs under the
    # feed's own address. Relative links are resolved in _entry_link instead.
    headers = {"content-type": content_type} if content_type else {}
    try:
        # BytesIO, not bytes: feedparser opens bare bytes/str that look like a
        # URL or file path.
        parsed = feedparser.parse(io.BytesIO(_fix_char_refs(content)), response_headers=headers)
    except Exception as exc:
        raise ParseError(_describe(exc)) from exc
    well_formed = not (parsed.get("bozo") and isinstance(parsed.get("bozo_exception"), SAXException))
    if not parsed.entries:
        if parsed.get("version"):  # a recognised feed that currently has no items
            return Parsed([], well_formed)
        if parsed.get("bozo"):
            raise ParseError(_describe(parsed.bozo_exception))
        raise ParseError("Not an RSS or Atom document")
    entries = []
    for raw in parsed.entries[:MAX_ENTRIES]:
        try:
            entry = _normalise(raw, now, base_url)
        except Exception:  # one bad entry must not sink the feed
            log.warning("Skipping unreadable entry in %s", base_url, exc_info=True)
            continue
        if entry is not None:
            entries.append(entry)
    return Parsed(entries, well_formed)


def _normalise(raw, now, base_url):
    link = _entry_link(raw, base_url)
    summary = clean_text(_raw_summary(raw), SUMMARY_MAX)
    title = (
        clean_text(raw.get("title"), TITLE_MAX)
        or truncate(summary, 120)
        or neutralise(link)[:TITLE_MAX]
    )
    if not title:
        return None
    guid = strip_controls(str(raw.get("id") or "").strip())
    guid = guid or link or "hash:" + sha256(f"{title}\n{summary}")
    return Entry(
        guid=guid,
        link=link,
        title=title,
        summary=summary,
        author=clean_text(raw.get("author"), AUTHOR_MAX),
        published_at=_entry_datetime(raw, now),
    )


def _entry_link(raw, base_url):
    link = strip_controls(str(raw.get("link") or "").strip())
    if link.startswith("/") and base_url:
        link = urljoin(base_url, link)
    if not link.lower().startswith(("http://", "https://")) or len(link) > LINK_MAX:
        return ""
    return link


def _raw_summary(raw):
    if raw.get("summary"):
        return raw["summary"]
    for content in raw.get("content") or []:
        if content.get("value"):
            return content["value"]
    return ""


def _entry_datetime(raw, now):
    for key in ("published", "updated", "created"):
        moment = _local_zone_datetime(raw.get(key)) or _struct_datetime(raw.get(f"{key}_parsed"))
        if moment is not None and moment >= EARLIEST:
            return min(moment, now)  # future dates are clock or zone errors
    return now


def _struct_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(value), tz=dt_timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _local_zone_datetime(value):
    """RFC 822 dates ending in an AU/NZ zone name, e.g. '... 09:00:00 AEDT'."""
    if not isinstance(value, str):
        return None
    match = _LOCAL_ZONE_SUFFIX.search(value)
    if not match:
        return None
    try:
        moment = parsedate_to_datetime(f"{value[:match.start()]} {_LOCAL_ZONES[match.group(1)]}")
    except (IndexError, TypeError, ValueError):
        return None
    return moment.astimezone(dt_timezone.utc) if moment.tzinfo else None


# --- storing ----------------------------------------------------------------


def store_entries(feed, entries, *, now):
    """Insert entries not yet on the wire and refresh last_seen_at on the rest.

    Returns the number inserted.
    """
    cutoff = now - timedelta(days=retention_days())
    links_per_guid = defaultdict(set)
    for entry in entries:
        links_per_guid[entry.guid].add(entry.link)
    key_counts = Counter(link_key(e.link) for e in entries if e.link)

    batch = {}
    for entry in entries:
        if entry.published_at < cutoff:  # never let expired entries in
            continue
        # A GUID reused for different links identifies none of them alone.
        identity = entry.guid
        if len(links_per_guid[entry.guid]) > 1:
            identity = f"{entry.guid}\n{entry.link}"
        batch.setdefault(sha256(identity), entry)
    if not batch:
        return 0

    known = set(
        FeedItem.objects.filter(feed=feed, guid_hash__in=list(batch)).values_list(
            "guid_hash", flat=True
        )
    )
    if known:
        FeedItem.objects.filter(feed=feed, guid_hash__in=known).update(last_seen_at=now)
    candidates = {h: e for h, e in batch.items() if h not in known}
    if not candidates:
        return 0

    keys = {}
    for guid_hash, entry in candidates.items():
        key = link_key(entry.link)
        # A link several entries share (e.g. a section front page) identifies none.
        keys[guid_hash] = key if key and key_counts[key] == 1 else ""
    on_wire = {}
    wanted = {k for k in keys.values() if k}
    if wanted:
        for item in FeedItem.objects.filter(link_hash__in=wanted).only(
            "pk", "link_hash", "section_id"
        ):
            on_wire.setdefault(item.link_hash, item)

    new_items, seen_elsewhere, needs_section = [], [], []
    for guid_hash, entry in candidates.items():
        key = keys[guid_hash]
        existing = on_wire.get(key) if key else None
        if existing is not None:
            # Same article already on the wire via another feed: keep one copy.
            if existing.pk:
                seen_elsewhere.append(existing.pk)
                if existing.section_id is None and feed.section_id:
                    needs_section.append(existing.pk)
                    existing.section_id = feed.section_id
            continue
        item = FeedItem(
            feed=feed,
            section_id=feed.section_id,
            guid=entry.guid[:GUID_MAX],
            guid_hash=guid_hash,
            link=entry.link,
            link_hash=key,
            title=entry.title,
            summary=entry.summary,
            author=entry.author,
            published_at=entry.published_at,
            fetched_at=now,
            last_seen_at=now,
        )
        new_items.append(item)
        if key:
            on_wire[key] = item
    if seen_elsewhere:
        FeedItem.objects.filter(pk__in=seen_elsewhere).update(last_seen_at=now)
    if needs_section:
        FeedItem.objects.filter(pk__in=needs_section, section__isnull=True).update(
            section_id=feed.section_id
        )
    FeedItem.objects.bulk_create(new_items, ignore_conflicts=True)
    return len(new_items)


def _record_success(feed, result, *, now, status):
    values = {
        "last_fetched_at": now,
        "last_success_at": now,
        "last_status": status,
        "last_error": "",
        "consecutive_failures": 0,
    }
    if status == Feed.STATUS_OK:
        values.update(etag=result.etag, last_modified=result.last_modified)
    else:  # a 304 may carry refreshed validators
        if result.etag:
            values["etag"] = result.etag
        if result.last_modified:
            values["last_modified"] = result.last_modified
    Feed.objects.filter(pk=feed.pk).update(**values)


def _record_failure(feed, error, *, now):
    try:
        Feed.objects.filter(pk=feed.pk).update(
            last_fetched_at=now,
            last_status=Feed.STATUS_ERROR,
            last_error=error[:1000],
            consecutive_failures=F("consecutive_failures") + 1,
        )
    except Exception:
        log.exception("Couldn't record the failure of feed %s", feed.pk)


@dataclass
class FeedReport:
    SKIPPED = "skipped"

    feed: Feed
    status: str
    new: int = 0
    error: str = ""

    @property
    def failed(self):
        return self.status == Feed.STATUS_ERROR


class _FeedFailed(Exception):
    pass


def process(feed, result, *, now):
    """Apply one FetchResult to the database and record the feed's health.

    `now` is the run's start time, so feeds stay due on a fixed cadence.
    """
    try:
        if result.error:
            raise _FeedFailed(result.error)
        if result.status == 304:
            with transaction.atomic():
                if feed.last_success_at:  # everything listed last time is still listed
                    FeedItem.objects.filter(
                        feed=feed, last_seen_at__gte=feed.last_success_at
                    ).update(last_seen_at=now)
                _record_success(feed, result, now=now, status=Feed.STATUS_NOT_MODIFIED)
            return FeedReport(feed, Feed.STATUS_NOT_MODIFIED)
        try:
            parsed = parse(
                result.content,
                base_url=result.final_url,
                content_type=result.content_type,
                now=now,
            )
        except ParseError as exc:
            raise _FeedFailed(f"Parse error: {exc}") from exc
        if not parsed.well_formed:
            # Possibly truncated: keep what parsed, but force a full download next time.
            result.etag = result.last_modified = ""
        with transaction.atomic():
            new = store_entries(feed, parsed.entries, now=now)
            _record_success(feed, result, now=now, status=Feed.STATUS_OK)
        return FeedReport(feed, Feed.STATUS_OK, new=new)
    except _FeedFailed as exc:
        error = str(exc)
    except Exception as exc:
        log.exception("Failed to store feed %s", feed.url)
        error = _describe(exc)
    error = strip_controls(error)
    log.warning("Feed %r failed: %s", feed.name, error)
    _record_failure(feed, error, now=now)
    return FeedReport(feed, Feed.STATUS_ERROR, error=error)


# --- runs -------------------------------------------------------------------

_SESSION_SETTINGS = [
    # A blocked statement fails instead of hanging the cron run.
    ("lock_timeout", "10s"),
    ("statement_timeout", "60s"),
    # If this process dies holding the run lock, Postgres notices in ~90s.
    ("tcp_keepalives_idle", "60"),
    ("tcp_keepalives_interval", "10"),
    ("tcp_keepalives_count", "3"),
]


@contextmanager
def _run_session():
    """Take the one-run-at-a-time lock; yield whether it was acquired."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
        acquired = cursor.fetchone()[0]
        if acquired:
            for name, value in _SESSION_SETTINGS:
                cursor.execute("SELECT set_config(%s, %s, false)", [name, value])
    try:
        yield acquired
    finally:
        if acquired:
            try:
                with connection.cursor() as cursor:
                    for name, _ in _SESSION_SETTINGS:
                        cursor.execute(f"RESET {name}")
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
            except Exception:  # a dead connection releases the lock by itself
                log.exception("Couldn't release the ingest lock cleanly")


def prune_items(now):
    """Delete undrafted items no feed has listed for the retention period."""
    cutoff = now - timedelta(days=retention_days())
    deleted, _ = FeedItem.objects.filter(story__isnull=True, last_seen_at__lt=cutoff).delete()
    return deleted


@dataclass
class RunReport:
    feeds: list = field(default_factory=list)
    pruned: int = 0
    locked_out: bool = False

    @property
    def new(self):
        return sum(r.new for r in self.feeds)

    @property
    def failures(self):
        return [r for r in self.feeds if r.failed]

    @property
    def skipped(self):
        return [r for r in self.feeds if r.status == FeedReport.SKIPPED]


def run(feeds=None, *, force=False, workers=8, deadline=240, prune=True):
    """Fetch `feeds` (default: active feeds that are due) and store new items."""
    with _run_session() as acquired:
        if not acquired:
            return RunReport(locked_out=True)
        now = timezone.now()
        if feeds is None:
            feeds = [
                f
                for f in Feed.objects.filter(active=True).select_related("section")
                if force or f.is_due(now)
            ]
        report = RunReport()
        pending = {f.pk: f for f in feeds}
        reqs = [FetchRequest(f.pk, f.url, f.etag, f.last_modified) for f in feeds]
        started = set()
        end = time.monotonic() + deadline
        for result in _fetch_all(reqs, workers=workers, end=end, started=started):
            report.feeds.append(process(pending.pop(result.feed_id), result, now=now))
        for feed in pending.values():
            if feed.pk in started:
                late = FetchResult(feed_id=feed.pk, error=f"No response within the {deadline}s run deadline")
                report.feeds.append(process(feed, late, now=now))
            else:  # never reached: stays due for the next run, no penalty
                report.feeds.append(FeedReport(feed, FeedReport.SKIPPED))
        if prune:
            report.pruned = prune_items(now)
        return report
