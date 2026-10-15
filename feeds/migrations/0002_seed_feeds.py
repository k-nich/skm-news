"""Seed a starter set of public-broadcaster feeds (each URL checked live on
15 Oct 2026). Items take the section whose name matches; feeds with no match
start without a section. Edit or add feeds in the admin afterwards."""
from django.db import migrations

SOURCES = [
    # (name, url, section name or None)
    ("ABC News - Top Stories", "https://www.abc.net.au/news/feed/10719986/rss.xml", None),
    ("ABC News - World", "https://www.abc.net.au/news/feed/104217382/rss.xml", "World"),
    ("ABC News - Business", "https://www.abc.net.au/news/feed/104217374/rss.xml", "Business"),
    ("ABC News - Entertainment & Culture", "https://www.abc.net.au/news/feed/103728568/rss.xml", "Culture"),
    ("ABC News - Sport", "https://www.abc.net.au/news/feed/103728570/rss.xml", "Sport"),
    ("SBS News - Top Stories", "https://www.sbs.com.au/news/feed", None),
    ("SBS News - World", "https://www.sbs.com.au/news/topic/world/feed", "World"),
    ("RNZ - World", "https://www.rnz.co.nz/rss/world.xml", "World"),
    ("RNZ - Business", "https://www.rnz.co.nz/rss/business.xml", "Business"),
    ("RNZ - Sport", "https://www.rnz.co.nz/rss/sport.xml", "Sport"),
    ("NPR - World", "https://feeds.npr.org/1004/rss.xml", "World"),
    ("NPR - Technology", "https://feeds.npr.org/1019/rss.xml", "Tech"),
    ("NPR - Science", "https://feeds.npr.org/1007/rss.xml", "Science"),
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
        ("feeds", "0001_initial"),
        ("news", "0003_alter_story_options_story_author_story_locked"),
    ]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
