from datetime import timedelta

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from .validators import normalise_feed_url, validate_public_url

# A failing feed waits interval x 2^failures before the next attempt, capped here.
MAX_BACKOFF = timedelta(hours=24)
# Cron runs drift by a few seconds; without slack a 15-minute feed on a
# 15-minute schedule would only be due every second run.
DUE_TOLERANCE = timedelta(seconds=90)


class Feed(models.Model):
    """A public RSS/Atom source polled by the `ingest_feeds` command."""

    STATUS_OK = "ok"
    STATUS_NOT_MODIFIED = "not_modified"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [
        (STATUS_OK, "OK"),
        (STATUS_NOT_MODIFIED, "Not modified"),
        (STATUS_ERROR, "Error"),
    ]

    name = models.CharField(max_length=100)
    url = models.URLField(
        "feed URL", max_length=500, unique=True, validators=[validate_public_url]
    )
    section = models.ForeignKey(
        "news.Section",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feeds",
        help_text="Section given to this feed's items. Blank for general/top-stories feeds.",
    )
    active = models.BooleanField(default=True)
    interval_minutes = models.PositiveIntegerField(
        "fetch every (minutes)",
        default=15,
        validators=[MinValueValidator(5)],
        help_text="Minimum time between fetches. The cron job decides when runs happen.",
    )

    # Maintained by the ingester.
    etag = models.CharField(max_length=255, blank=True, editable=False)
    last_modified = models.CharField(max_length=64, blank=True, editable=False)
    last_fetched_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_success_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, blank=True, editable=False
    )
    last_error = models.TextField(blank=True, editable=False)
    consecutive_failures = models.PositiveIntegerField(default=0, editable=False)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.url:  # so trivial variants can't add the same feed twice
            self.url = normalise_feed_url(self.url)

    def next_due_at(self):
        """When this feed may next be fetched (None = never fetched, due now)."""
        if self.last_fetched_at is None:
            return None
        base = timedelta(minutes=self.interval_minutes)
        wait = base * (2 ** min(self.consecutive_failures, 6))
        wait = min(wait, max(base, MAX_BACKOFF))
        return self.last_fetched_at + wait - DUE_TOLERANCE

    def is_due(self, now=None):
        due = self.next_due_at()
        return due is None or (now or timezone.now()) >= due

    def reset_fetch_state(self):
        """Forget cache validators and backoff, e.g. after the URL changes."""
        self.etag = ""
        self.last_modified = ""
        self.last_fetched_at = None
        self.last_status = ""
        self.last_error = ""
        self.consecutive_failures = 0


class FeedItem(models.Model):
    """One entry from a feed: the raw wire copy journalists draft stories from."""

    # SET_NULL so deleting a feed keeps items already drafted into stories;
    # undrafted orphans age out through retention.
    feed = models.ForeignKey(
        Feed, on_delete=models.SET_NULL, null=True, blank=True, related_name="items"
    )
    section = models.ForeignKey(
        "news.Section",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feed_items",
    )
    guid = models.TextField()
    guid_hash = models.CharField(max_length=64)
    link = models.URLField(max_length=2000, blank=True)
    # Normalised link, so the same article arriving via two feeds is stored once.
    # Blank when the link can't identify the entry.
    link_hash = models.CharField(max_length=64, blank=True)
    title = models.CharField(max_length=500)
    summary = models.TextField(blank=True)
    author = models.CharField(max_length=200, blank=True)
    published_at = models.DateTimeField(
        db_index=True, help_text="From the feed; fetch time when the feed gives no usable date."
    )
    fetched_at = models.DateTimeField("first fetched", default=timezone.now)
    last_seen_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="Last time a feed still listed this item; retention counts from here.",
    )
    story = models.OneToOneField(
        "news.Story",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feed_item",
        help_text="Draft story created from this item.",
    )

    class Meta:
        ordering = ["-published_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["feed", "guid_hash"], name="feeds_feeditem_unique_guid_per_feed"
            )
        ]
        indexes = [models.Index(fields=["link_hash"], name="feeds_item_link_hash_idx")]

    def __str__(self):
        return self.title
