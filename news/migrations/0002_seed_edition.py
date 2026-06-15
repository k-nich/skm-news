"""Seed the initial sample edition so a fresh database isn't empty."""
from django.db import migrations


def seed(apps, schema_editor):
    Section = apps.get_model("news", "Section")
    Story = apps.get_model("news", "Story")

    # Don't double-seed if content already exists.
    if Story.objects.exists():
        return

    lead = [
        {
            "kicker": "TOP STORY",
            "headline": "Climate summit ends with a deal — and a long list of asterisks",
            "bullets": [
                "190 nations sign a phase-down pledge; no firm deadline attached.",
                "Funding for developing economies doubles, still short of asks.",
                "Critics call it progress on paper, slow in practice.",
            ],
            "secs": 30,
            "breaking": True,
        },
        {
            "kicker": "MARKETS",
            "headline": "Rate cut lands, and traders exhale",
            "bullets": [
                "Central bank trims a quarter point, signals a pause.",
                "Stocks pop, bond yields slip, dollar softens.",
                "Watch next week's jobs data for the next move.",
            ],
            "secs": 25,
            "breaking": False,
        },
    ]
    for i, s in enumerate(lead):
        Story.objects.create(
            placement="lead", section=None, order=i,
            kicker=s["kicker"], headline=s["headline"],
            bullets="\n".join(s["bullets"]), secs=s["secs"],
            breaking=s["breaking"], published=True,
        )

    sections = [
        ("World", [
            ("EUROPE", "Coalition talks stall over budget",
             ["Two parties walk; third holds the swing vote.",
              "Caretaker government likely into next month."], 20, False),
            ("ASIA", "Record monsoon displaces thousands",
             ["Relief camps near capacity in three states.",
              "Forecasters warn of a second front midweek."], 20, False),
            ("AMERICAS", "Border trade pact clears final vote",
             ["Tariffs drop on 40+ goods categories.",
              "Takes effect at the start of next quarter."], 18, False),
        ]),
        ("Business", [
            ("TECH · EARNINGS", "Chipmaker beats, guides higher",
             ["Revenue up 22% on AI-hardware demand.",
              "Shares jump 9% in after-hours trade."], 20, False),
            ("RETAIL", "Big-box chain to shut 60 stores",
             ["Pivot to smaller urban formats.",
              "Roughly 4,000 roles affected, union says."], 18, False),
            ("ENERGY", "Oil dips below the line analysts watch",
             ["Supply glut meets softening demand.",
              "Pump prices expected to ease by month-end."], 15, False),
        ]),
        ("Tech", [
            ("AI", "New open model tops the leaderboard",
             ["Matches larger rivals at a fraction of the cost.",
              "Released under a permissive license."], 22, False),
            ("PRIVACY", "Regulator fines app over data sharing",
             ["Penalty is the largest under the new rules.",
              "Company says it will appeal."], 16, False),
            ("GADGETS", "Foldable phones get a thinner hinge",
             ["Lighter, cheaper, fewer creases — finally.",
              "On sale ahead of the holiday rush."], 14, False),
        ]),
        ("Science", [
            ("SPACE", "Telescope spots a surprisingly old galaxy",
             ["Light from when the universe was young.",
              "Challenges a few formation timelines."], 18, False),
            ("HEALTH", "Trial shows promise for a common allergy",
             ["Symptoms cut in two-thirds of patients.",
              "Larger study to follow next year."], 16, False),
        ]),
        ("Culture", [
            ("FILM", "Indie debut sweeps the festival circuit",
             ["Wins top prize plus the audience award.",
              "Wide release set for the spring."], 14, False),
            ("BOOKS", "Debut novelist lands a seven-figure deal",
             ["Literary thriller sparks a bidding war.",
              "Adaptation rights already optioned."], 12, False),
        ]),
        ("Sport", [
            ("FOOTBALL", "Underdogs reach the final on a late goal",
             ["Stoppage-time winner ends a 20-year wait.",
              "Final is set for the weekend."], 14, False),
            ("TENNIS", "Top seed crashes out in round two",
             ["Qualifier pulls off the upset of the week.",
              "Draw blows wide open."], 12, False),
        ]),
    ]
    for sec_order, (name, stories) in enumerate(sections):
        section = Section.objects.create(name=name, order=sec_order)
        for i, (kicker, headline, bullets, secs, breaking) in enumerate(stories):
            Story.objects.create(
                placement="section", section=section, order=i,
                kicker=kicker, headline=headline,
                bullets="\n".join(bullets), secs=secs,
                breaking=breaking, published=True,
            )


def unseed(apps, schema_editor):
    apps.get_model("news", "Story").objects.all().delete()
    apps.get_model("news", "Section").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("news", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
