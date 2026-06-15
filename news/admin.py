from django.contrib import admin

from .models import Section, Story


@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    list_display = ("name", "order", "story_count")
    list_editable = ("order",)

    @admin.display(description="published stories")
    def story_count(self, obj):
        return obj.stories.filter(published=True).count()


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    list_display = (
        "headline",
        "placement",
        "section",
        "published",
        "breaking",
        "secs",
        "order",
    )
    list_editable = ("published", "breaking", "order")
    list_filter = ("placement", "section", "published", "breaking")
    search_fields = ("headline", "kicker", "bullets")
    fieldsets = (
        (None, {"fields": ("headline", "kicker", "bullets")}),
        ("Placement", {"fields": ("placement", "section", "order")}),
        ("Flags", {"fields": ("secs", "breaking", "published")}),
    )
