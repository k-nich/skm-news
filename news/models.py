from django.conf import settings
from django.db import models


class Section(models.Model):
    """A newspaper section that becomes a column on the page (World, Tech...)."""

    name = models.CharField(max_length=60)
    order = models.PositiveIntegerField(
        default=0, help_text="Lower numbers appear first (left to right)."
    )

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Story(models.Model):
    """A single bite-sized story: a headline plus a few skim bullets."""

    PLACEMENT_LEAD = "lead"
    PLACEMENT_SECTION = "section"
    PLACEMENT_CHOICES = [
        (PLACEMENT_LEAD, "Lead — big top story"),
        (PLACEMENT_SECTION, "Section column"),
    ]

    placement = models.CharField(
        max_length=10, choices=PLACEMENT_CHOICES, default=PLACEMENT_SECTION
    )
    section = models.ForeignKey(
        Section,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stories",
        help_text="Required for section stories; leave blank for lead stories.",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stories",
        help_text="Who wrote this. Set automatically to the creator if left blank.",
    )
    kicker = models.CharField(
        max_length=60, blank=True, help_text="Small label above the headline, e.g. MARKETS."
    )
    headline = models.CharField(max_length=200)
    bullets = models.TextField(
        help_text="One bullet per line. Two or three short lines skim best."
    )
    secs = models.PositiveIntegerField(
        default=20, verbose_name="Scan time (seconds)", help_text="Feeds the read meter."
    )
    breaking = models.BooleanField(
        default=False, help_text="Shows the pulsing 'Breaking' marker."
    )
    published = models.BooleanField(
        default=True, help_text="Uncheck to hide from the page without deleting."
    )
    locked = models.BooleanField(
        default=False,
        help_text="When locked, the author can no longer edit. Only Editors and "
        "Admins can lock or unlock.",
    )
    order = models.PositiveIntegerField(
        default=0, help_text="Lower numbers appear first within the placement/section."
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "-created"]
        verbose_name_plural = "stories"
        permissions = [
            ("edit_any_story", "Can edit stories authored by others"),
            ("lock_story", "Can lock or unlock a story for editing"),
        ]

    def __str__(self):
        return self.headline

    def bullet_list(self):
        """Bullets as a clean list, splitting the textarea on line breaks."""
        return [line.strip() for line in self.bullets.splitlines() if line.strip()]
