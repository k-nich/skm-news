from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html

from .drafts import create_draft_story
from .models import Feed, FeedItem

MAX_DRAFTS_PER_ACTION = 25


@admin.register(Feed)
class FeedAdmin(admin.ModelAdmin):
    list_display = (
        "name", "section", "active", "interval_minutes", "health",
        "last_success_at", "item_count",
    )
    list_filter = ("active", "last_status", "section")
    search_fields = ("name", "url")
    readonly_fields = (
        "last_status", "last_fetched_at", "last_success_at", "next_due",
        "consecutive_failures", "last_error", "etag", "last_modified",
    )
    fieldsets = (
        (None, {"fields": ("name", "url", "section", "active", "interval_minutes")}),
        ("Health", {"fields": (
            "last_status", "last_fetched_at", "last_success_at", "next_due",
            "consecutive_failures", "last_error",
        )}),
        ("HTTP cache", {"classes": ("collapse",), "fields": ("etag", "last_modified")}),
    )
    actions = ("fetch_next_run",)

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("section")
            .annotate(item_total=Count("items"))
        )

    def save_model(self, request, obj, form, change):
        if change and "url" in form.changed_data:
            obj.reset_fetch_state()  # old validators/backoff belong to the old URL
        super().save_model(request, obj, form, change)

    @admin.display(description="Items", ordering="item_total")
    def item_count(self, obj):
        return obj.item_total

    @admin.display(description="Health", ordering="consecutive_failures")
    def health(self, obj):
        if not obj.last_status:
            return "Not fetched yet"
        if obj.last_status == Feed.STATUS_ERROR:
            return f"Failing ({obj.consecutive_failures}x)"
        return obj.get_last_status_display()

    @admin.display(description="Next fetch due")
    def next_due(self, obj):
        return obj.next_due_at() or "Next run"

    # Fetching happens only in the cron service: an inline fetch would tie up
    # the web worker that also serves the public site.
    @admin.action(description="Fetch on the next scheduled run", permissions=["change"])
    def fetch_next_run(self, request, queryset):
        count = queryset.update(last_fetched_at=None, consecutive_failures=0)
        self.message_user(
            request, f"{count} feed(s) will be fetched on the next scheduled run.", messages.SUCCESS
        )


class DraftedFilter(admin.SimpleListFilter):
    title = "status"
    parameter_name = "drafted"

    def lookups(self, request, model_admin):
        return (("no", "Not drafted"), ("yes", "Drafted"))

    def queryset(self, request, queryset):
        if self.value() == "no":
            return queryset.filter(story__isnull=True)
        if self.value() == "yes":
            return queryset.filter(story__isnull=False)
        return queryset


@admin.register(FeedItem)
class FeedItemAdmin(admin.ModelAdmin):
    """Read-only wire. Journalists pick items and turn them into draft stories."""

    list_display = ("title", "feed", "section", "published_at", "original", "draft")
    list_filter = (DraftedFilter, "section", "feed")
    list_select_related = ("feed", "section", "story__author")
    search_fields = ("title", "summary")
    date_hierarchy = "published_at"
    list_per_page = 50
    fields = (
        "title", "summary", "original", "author", "feed", "section",
        "published_at", "fetched_at", "last_seen_at", "draft",
    )
    readonly_fields = fields
    actions = ("create_draft_stories",)

    # The ingester owns these rows: an edit would be overwritten and a
    # deleted item would come back on the next fetch.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_draft_permission(self, request):
        return request.user.has_perm("news.add_story")

    @admin.display(description="Original")
    def original(self, obj):
        if not obj.link:
            return "-"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">Open ↗</a>', obj.link
        )

    @admin.display(description="Draft", ordering="story")
    def draft(self, obj):
        if obj.story_id is None:
            return "-"
        url = reverse("admin:news_story_change", args=[obj.story_id])
        label = "Published" if obj.story.published else "Draft"
        return format_html('<a href="{}">{} (by {})</a>', url, label, obj.story.author or "unknown")

    @admin.action(
        description="Create draft stories from selected items", permissions=["draft"]
    )
    def create_draft_stories(self, request, queryset):
        selected = queryset.count()
        if selected > MAX_DRAFTS_PER_ACTION:
            self.message_user(
                request,
                f"Select at most {MAX_DRAFTS_PER_ACTION} items at a time ({selected} selected).",
                messages.ERROR,
            )
            return None
        created = []
        with transaction.atomic():
            items = (
                queryset.filter(story__isnull=True)
                .select_related("section")
                .select_for_update(of=("self",))
            )
            for item in items:
                created.append(create_draft_story(item, request.user))
        if selected > len(created):
            self.message_user(
                request,
                f"Skipped {selected - len(created)} item(s) that already have a story.",
                messages.WARNING,
            )
        if not created:
            return None
        self.message_user(
            request,
            f"Created {len(created)} unpublished draft(s). Rewrite the bullets "
            "in SKIM style before publishing.",
            messages.SUCCESS,
        )
        no_section = sum(1 for story in created if story.section_id is None)
        if no_section:
            self.message_user(
                request,
                f"{no_section} draft(s) have no section. Set one (or make it a Lead) "
                "or it won't appear on the front page.",
                messages.WARNING,
            )
        if len(created) == 1:
            return HttpResponseRedirect(reverse("admin:news_story_change", args=[created[0].pk]))
        return HttpResponseRedirect(
            reverse("admin:news_story_changelist") + "?published__exact=0"
        )
