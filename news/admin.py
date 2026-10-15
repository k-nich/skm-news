from urllib.parse import urlsplit

from django.contrib import admin
from django.utils.html import format_html

from .models import Section, Story


@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    list_display = ("name", "order", "story_count")

    @admin.display(description="published stories")
    def story_count(self, obj):
        return obj.stories.filter(published=True).count()


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    list_display = (
        "headline", "section", "author", "placement",
        "published", "locked", "breaking",
    )
    list_filter = ("placement", "section", "published", "locked", "breaking", "author")
    search_fields = ("headline", "kicker", "bullets")
    fieldsets = (
        (None, {"fields": ("headline", "kicker", "bullets")}),
        ("Placement", {"fields": ("placement", "section", "order")}),
        ("Status", {"fields": ("author", "secs", "breaking", "published", "locked")}),
    )
    actions = ("lock_stories", "unlock_stories")

    # --- role helpers -------------------------------------------------
    @staticmethod
    def can_edit_any(user):
        """Editors and Admins can edit stories written by anyone."""
        return user.is_superuser or user.has_perm("news.edit_any_story")

    @staticmethod
    def can_lock(user):
        """Editors and Admins can lock/unlock stories."""
        return user.is_superuser or user.has_perm("news.lock_story")

    # --- object-level access -----------------------------------------
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if self.can_edit_any(request.user):
            return qs
        # Journalists only ever see their own stories.
        return qs.filter(author=request.user)

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        if obj is None or self.can_edit_any(request.user):
            return True
        # Journalist: only their own, and only while unlocked.
        return obj.author_id == request.user.id and not obj.locked

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        if obj is None or self.can_edit_any(request.user):
            return True
        return obj.author_id == request.user.id and not obj.locked

    # --- wire source (stories drafted from an RSS item) -----------------
    def get_fieldsets(self, request, obj=None):
        fieldsets = list(super().get_fieldsets(request, obj))
        if obj is not None and getattr(obj, "feed_item", None) is not None:
            fieldsets.append(("Source", {"fields": ("source_link",)}))
        return fieldsets

    @admin.display(description="Original article")
    def source_link(self, obj):
        item = getattr(obj, "feed_item", None)
        if item is None or not item.link:
            return "-"
        source = item.feed.name if item.feed_id else urlsplit(item.link).hostname
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">{} ↗</a>',
            item.link, source,
        )

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        ro.append("source_link")
        # Journalists can't reassign authorship...
        if not self.can_edit_any(request.user) and "author" not in ro:
            ro.append("author")
        # ...and can't toggle the lock.
        if not self.can_lock(request.user) and "locked" not in ro:
            ro.append("locked")
        return ro

    def save_model(self, request, obj, form, change):
        # First save with no author set -> the creator becomes the author.
        if not obj.author_id:
            obj.author = request.user
        super().save_model(request, obj, form, change)

    # --- bulk lock/unlock (Editors + Admins only) --------------------
    @admin.action(description="Lock selected stories (block author edits)")
    def lock_stories(self, request, queryset):
        n = queryset.update(locked=True)
        self.message_user(request, f"{n} story(ies) locked.")

    @admin.action(description="Unlock selected stories")
    def unlock_stories(self, request, queryset):
        n = queryset.update(locked=False)
        self.message_user(request, f"{n} story(ies) unlocked.")

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not self.can_lock(request.user):
            actions.pop("lock_stories", None)
            actions.pop("unlock_stories", None)
        return actions
