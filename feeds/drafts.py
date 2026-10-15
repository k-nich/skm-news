"""Turn wire items into unpublished draft stories."""
import re

from news.models import Story

from .ingest import neutralise, truncate

# Split after . ! or ? (optionally followed by a closing quote/bracket) when the
# next sentence starts with a capital or digit.
_SENTENCE_BREAK = re.compile(
    r"(?:(?<=[.!?])|(?<=[.!?][\"'”’)]))\s+(?=[\"'“‘(]?[A-Z0-9])"
)
MAX_BULLETS = 3
BULLET_MAX = 160
PLACEHOLDER = "Rewrite as 2-3 skim bullets."


def split_bullets(text):
    """First few sentences of a summary, one per bullet."""
    sentences = [s.strip() for s in _SENTENCE_BREAK.split(text or "") if s.strip()]
    return [truncate(s, BULLET_MAX) for s in sentences[:MAX_BULLETS]]


def create_draft_story(item, author):
    """Create an unpublished Story from `item` and link the two."""
    # Item text is already neutralised at ingest; again here because the
    # front page renders story text as HTML.
    headline_max = Story._meta.get_field("headline").max_length
    story = Story.objects.create(
        placement=Story.PLACEMENT_SECTION,
        section=item.section,
        author=author,
        headline=truncate(neutralise(item.title), headline_max),
        bullets="\n".join(split_bullets(neutralise(item.summary)) or [PLACEHOLDER]),
        published=False,
    )
    item.story = story
    item.save(update_fields=["story"])
    return story
