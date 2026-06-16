from django.shortcuts import render

from .models import Section, Story

# Branding shown on the masthead. Change here (or promote to a model later).
MASTHEAD = {"name": "SKIM NEWS", "tagline": "the five-minute paper"}


def _serialize(story):
    """Shape a Story to match the JS renderer's expected fields."""
    return {
        "kicker": story.kicker,
        "headline": story.headline,
        "bullets": story.bullet_list(),
        "secs": story.secs,
        "breaking": story.breaking,
    }


def front_page(request):
    """Build the edition from the database and hand it to the page as JSON.

    The template's JavaScript renders the same EDITION shape it always has;
    we've only swapped the data source from a hardcoded array to the DB.
    """
    lead = [
        _serialize(s)
        for s in Story.objects.filter(placement=Story.PLACEMENT_LEAD, published=True)
    ]

    sections = []
    for section in Section.objects.prefetch_related("stories"):
        stories = [
            _serialize(s)
            for s in section.stories.filter(
                placement=Story.PLACEMENT_SECTION, published=True
            )
        ]
        if stories:
            sections.append({"name": section.name, "stories": stories})

    edition = {"masthead": MASTHEAD, "lead": lead, "sections": sections}
    return render(request, "news/index.html", {"edition": edition})
