"""Seed BBC News and BBC Sport feeds, mapped to sections by name.

BBC servers weren't reachable from the environment this was written in, so
these URLs were confirmed against feed directories (feeder.co, Feedspot) on
15 Oct 2026 rather than fetched directly. The first ingest run reports each
one as OK or FAIL.
"""
from django.db import migrations

SOURCES = [
    # (name, url, section name or None)
    ("BBC News - Top Stories", "https://feeds.bbci.co.uk/news/rss.xml", None),
    ("BBC News - World", "https://feeds.bbci.co.uk/news/world/rss.xml", "World"),
    ("BBC News - Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "Business"),
    ("BBC News - Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", "Tech"),
    ("BBC News - Science & Environment", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "Science"),
    ("BBC News - Entertainment & Arts", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", "Culture"),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml", "Sport"),
]


def seed(apps, schema_editor):
    Feed = apps.get_model("feeds", "Feed")
    Section = apps.get_model("news", "Section")
    for name, url, section_name in SOURCES:
        section = (
            Section.objects.filter(name__iexact=section_name).first() if section_name else None
        )
        Feed.objects.get_or_create(url=url, defaults={"name": name, "section": section})


class Migration(migrations.Migration):
    dependencies = [
        ("feeds", "0002_seed_feeds"),
        ("news", "0003_alter_story_options_story_author_story_locked"),
    ]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
