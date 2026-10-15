"""Fetch due RSS/Atom feeds into the wire.

Runs as a Railway cron service:  python manage.py ingest_feeds
By hand, every active feed now:  python manage.py ingest_feeds --force
"""
import faulthandler
import sys
import time

from django.core.management.base import BaseCommand, CommandError

from feeds.ingest import run
from feeds.models import Feed

# Seconds past --deadline before the process dumps its stack and exits(1).
# Railway never kills a hung run and skips later runs while it's alive.
HARD_STOP_MARGIN = 120


class Command(BaseCommand):
    help = "Fetch due RSS/Atom feeds and store new items on the wire."

    def add_arguments(self, parser):
        parser.add_argument(
            "--feed", type=int, action="append", dest="feed_ids", metavar="ID",
            help="Fetch only this feed id, ignoring its schedule (repeatable).",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Fetch every active feed now, ignoring schedules and backoff.",
        )
        parser.add_argument(
            "--no-prune", action="store_true",
            help="Don't delete expired wire items that were never drafted.",
        )
        parser.add_argument("--workers", type=int, default=8, help="Parallel downloads (default 8).")
        parser.add_argument(
            "--deadline", type=int, default=240,
            help="Seconds before unfinished downloads are abandoned (default 240).",
        )

    def handle(self, *args, **opts):
        feeds = None
        if opts["feed_ids"]:
            wanted = set(opts["feed_ids"])
            feeds = list(Feed.objects.filter(pk__in=wanted).select_related("section"))
            missing = wanted - {f.pk for f in feeds}
            if missing:
                raise CommandError(f"No feed with id {', '.join(map(str, sorted(missing)))}.")

        deadline = max(1, opts["deadline"])
        hard_stop = _arm_hard_stop(deadline + HARD_STOP_MARGIN)
        started = time.monotonic()
        try:
            report = run(
                feeds,
                force=opts["force"],
                workers=max(1, opts["workers"]),
                deadline=deadline,
                prune=not opts["no_prune"],
            )
        finally:
            if hard_stop:
                faulthandler.cancel_dump_traceback_later()
        if report.locked_out:
            self.stdout.write(self.style.WARNING("Another ingest run holds the lock; exiting."))
            return

        for r in sorted(report.feeds, key=lambda r: r.feed.name):
            if r.failed:
                self.stdout.write(self.style.ERROR(f"FAIL {r.feed.name}: {r.error}"))
            elif r.status == Feed.STATUS_NOT_MODIFIED:
                self.stdout.write(f"304  {r.feed.name}")
            elif r.status == r.SKIPPED:
                self.stdout.write(f"SKIP {r.feed.name}: not reached before the deadline")
            else:
                self.stdout.write(f"OK   {r.feed.name}: {r.new} new")

        summary = (
            f"{len(report.feeds)} feed(s), {report.new} new item(s), "
            f"{len(report.failures)} failed, {len(report.skipped)} skipped, "
            f"{report.pruned} pruned in {time.monotonic() - started:.1f}s."
        )
        attempted = len(report.feeds) - len(report.skipped)
        if attempted and len(report.failures) == attempted:
            raise CommandError(f"All feeds failed. {summary}")
        self.stdout.write(self.style.SUCCESS(summary))


def _arm_hard_stop(seconds):
    try:
        faulthandler.dump_traceback_later(seconds, exit=True, file=sys.stderr)
    except (AttributeError, OSError, ValueError):  # stderr without a file descriptor
        return False
    return True
