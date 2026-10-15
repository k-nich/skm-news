import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from unittest import mock, skipUnless

from django.contrib.admin import helpers
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from news.models import Section, Story

from . import ingest
from .admin import MAX_DRAFTS_PER_ACTION
from .drafts import PLACEHOLDER, split_bullets
from .ingest import LOCK_KEY, ParseError, clean_text, link_key, parse, run
from .models import Feed, FeedItem

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=dt_timezone.utc)
REAL_ADDRESS_POLICY = ingest._address_allowed


def rss(items, title="Test Feed", channel_extra=""):
    """Build an RSS 2.0 document. Item values are inserted verbatim."""
    out = [
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{title}</title><link>https://example.com/</link><description>d</description>"
        f"{channel_extra}"
    ]
    for item in items:
        out.append("<item>")
        for tag in ("title", "link", "guid", "description", "pubDate", "author"):
            if tag in item:
                out.append(f"<{tag}>{item[tag]}</{tag}>")
        out.append("</item>")
    out.append("</channel></rss>")
    return "".join(out).encode("utf-8")


def ago(**kwargs):
    return format_datetime(timezone.now() - timedelta(**kwargs))


def entries(doc, **kwargs):
    return parse(doc, now=kwargs.pop("now", NOW), **kwargs).entries


ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Test</title><id>urn:uuid:feed</id><updated>2026-10-15T01:00:00Z</updated>
  <entry>
    <title type="html">Atom &lt;em&gt;entry&lt;/em&gt;</title>
    <id>urn:uuid:1</id>
    <link rel="alternate" href="https://example.org/a1"/>
    <updated>2026-10-15T01:00:00Z</updated>
    <content type="html">&lt;p&gt;Body text here.&lt;/p&gt;</content>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://example.net/">
    <title>RDF Test</title><link>https://example.net/</link><description>d</description>
    <items><rdf:Seq><rdf:li rdf:resource="https://example.net/r1"/></rdf:Seq></items>
  </channel>
  <item rdf:about="https://example.net/r1">
    <title>RDF item</title><link>https://example.net/r1</link>
    <description>RDF summary.</description><dc:date>2026-10-15T02:00:00+02:00</dc:date>
  </item>
</rdf:RDF>"""


class ParseTests(SimpleTestCase):
    BASE = "https://www.example.com/news/feed.xml"

    def test_rss_html_is_flattened_and_dates_are_utc(self):
        doc = rss([{
            "title": "Rates &amp; markets",
            "link": "https://example.com/a",
            "guid": "abc-1",
            "description": "<![CDATA[<p>First <b>bold</b> line.</p><p>Second&nbsp;line.</p>]]>",
            "pubDate": "Thu, 15 Oct 2026 10:03:07 +1300",
        }])
        [entry] = entries(doc, base_url=self.BASE)
        self.assertEqual(entry.title, "Rates & markets")
        self.assertEqual(entry.summary, "First bold line. Second line.")
        self.assertEqual(entry.guid, "abc-1")
        self.assertEqual(entry.published_at, datetime(2026, 10, 14, 21, 3, 7, tzinfo=dt_timezone.utc))

    def test_markup_never_survives_as_html(self):
        attack = "Rates &lt; 5% &amp;lt;img src=x onerror=alert(1)&amp;gt;"
        self.assertEqual(clean_text(attack, 500), "Rates ‹ 5%")
        doc = rss([{"title": "&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;Hi",
                    "link": "https://e.com/x",
                    "description": "&amp;amp;lt;b onmouseover=x&amp;amp;gt;Escaped thrice"}])
        [entry] = entries(doc)
        self.assertNotIn("<", entry.title + entry.summary)
        self.assertTrue(entry.title.endswith("Hi"))

    def test_pathological_markup_is_handled(self):
        for value in ("<" * 60 + "b>" * 60 + "x", "<p" * 10000):
            with self.subTest(length=len(value)):
                started = time.monotonic()
                self.assertNotIn("<", clean_text(value, 2000))
                self.assertLess(time.monotonic() - started, 0.5)

    def test_control_characters_and_bad_char_refs_are_dropped(self):
        doc = rss([
            {"title": "Nul\x00 byte", "link": "https://e.com/1"},
            {"title": "Emoji &#55357;&#56832; pair", "link": "https://e.com/2"},
            {"title": "Out of range &#1114112; and &#x0; and &#99999999999;", "link": "https://e.com/3"},
            {"title": "Fine", "link": "https://e.com/4"},
        ])
        titles = [e.title for e in entries(doc)]
        self.assertEqual(len(titles), 4)
        self.assertEqual(titles[0], "Nul byte")
        self.assertEqual(titles[3], "Fine")
        for title in titles:
            self.assertNotRegex(title, "[\x00-\x08\ud800-\udfff]")

    def test_entity_declarations_are_refused(self):
        doc = rss([{"title": "&x;", "link": "https://e.com/1"}]).replace(
            b"<rss", b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss', 1
        )
        with self.assertRaisesMessage(ParseError, "declares XML entities"):
            parse(doc)
        utf16 = doc.decode("utf-8").replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
        with self.assertRaisesMessage(ParseError, "declares XML entities"):
            parse(utf16)

    def test_links_resolve_against_the_feed_but_guids_stay_bare(self):
        doc = rss([
            {"title": "Relative", "link": "/news/1", "guid": "1"},
            {"title": "Bare guid", "guid": "12345"},
        ])
        relative, bare = entries(doc, base_url=self.BASE)
        self.assertEqual(relative.link, "https://www.example.com/news/1")
        self.assertEqual((bare.link, bare.guid), ("", "12345"))

    def test_fallbacks_for_missing_guid_title_and_date(self):
        doc = rss([
            {"title": "No guid", "link": "https://example.com/b"},
            {"description": "Only a summary. Nothing else."},
            {"title": "Future", "link": "https://example.com/c", "pubDate": "Mon, 01 Jan 2035 00:00:00 +0000"},
            {"title": "Placeholder date", "link": "https://example.com/d", "pubDate": "Mon, 01 Jan 0001 00:00:00 +0000"},
        ])
        no_guid, summary_only, future, placeholder = entries(doc, base_url=self.BASE)
        self.assertEqual(no_guid.guid, "https://example.com/b")
        self.assertEqual(no_guid.published_at, NOW)  # undated -> fetch time
        self.assertEqual(summary_only.title, "Only a summary. Nothing else.")
        self.assertTrue(summary_only.guid.startswith("hash:"))
        self.assertEqual(future.published_at, NOW)  # clamped
        self.assertEqual(placeholder.published_at, NOW)

    def test_australian_zone_names(self):
        doc = rss([{"title": "x", "link": "https://e.com/x", "pubDate": "Thu, 15 Oct 2026 09:00:00 AEDT"}])
        [entry] = entries(doc)
        self.assertEqual(entry.published_at, datetime(2026, 10, 14, 22, 0, tzinfo=dt_timezone.utc))

    def test_non_http_links_are_dropped(self):
        doc = rss([{"title": "x", "link": "javascript:alert(1)"}])
        self.assertEqual(entries(doc)[0].link, "")

    def test_atom(self):
        [entry] = entries(ATOM, base_url=self.BASE)
        self.assertEqual(
            (entry.title, entry.link, entry.guid, entry.summary, entry.author),
            ("Atom entry", "https://example.org/a1", "urn:uuid:1", "Body text here.", "Jane Doe"),
        )
        self.assertEqual(entry.published_at, datetime(2026, 10, 15, 1, 0, tzinfo=dt_timezone.utc))

    def test_rdf(self):
        [entry] = entries(RDF, base_url=self.BASE)
        self.assertEqual(entry.title, "RDF item")
        self.assertEqual(entry.published_at, datetime(2026, 10, 15, 0, 0, tzinfo=dt_timezone.utc))

    def test_truncated_feed_keeps_complete_items_but_is_flagged(self):
        doc = rss([{"title": f"Item {i}", "link": f"https://e.com/{i}", "guid": str(i)} for i in range(3)])
        parsed = parse(doc[: doc.index(b"<item><title>Item 2") + 20], now=NOW)
        self.assertEqual([e.title for e in parsed.entries][:2], ["Item 0", "Item 1"])
        self.assertFalse(parsed.well_formed)
        self.assertTrue(parse(doc, content_type="text/html", now=NOW).well_formed)

    def test_rejects_html_and_empty_bodies(self):
        page = b"<!doctype html><html><head><title>Hi</title></head><body><p>x</p></body></html>"
        for body in (page, b"", b"  \n"):
            with self.subTest(body=body[:10]), self.assertRaises(ParseError):
                parse(body, content_type="text/html", now=NOW)

    def test_empty_but_valid_feed_returns_nothing(self):
        self.assertEqual(entries(rss([])), [])

    def test_entry_count_is_capped(self):
        doc = rss([{"title": f"T{i}", "link": f"https://e.com/{i}"} for i in range(ingest.MAX_ENTRIES + 50)])
        self.assertEqual(len(entries(doc)), ingest.MAX_ENTRIES)

    def test_content_that_looks_like_a_path_is_not_opened(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as fh:
            fh.write(rss([{"title": "from disk", "link": "https://e.com/disk"}]))
        try:
            with self.assertRaises(ParseError):
                parse(fh.name.encode(), now=NOW)
        finally:
            os.unlink(fh.name)

    def test_link_key(self):
        self.assertEqual(
            link_key("https://www.Example.com/news/a/?utm_source=rss&id=7"),
            link_key("http://example.com/news/a?id=7&fbclid=zz"),
        )
        self.assertNotEqual(link_key("https://e.com/a?id=1"), link_key("https://e.com/a?id=2"))
        self.assertNotEqual(link_key("https://e.com/live#post-1"), link_key("https://e.com/live#post-2"))

    def test_split_bullets(self):
        text = 'He said "it is done." Markets rose 2%. U.S. officials agreed. A fourth one.'
        self.assertEqual(
            split_bullets(text),
            ['He said "it is done."', "Markets rose 2%.", "U.S. officials agreed."],
        )
        self.assertEqual(split_bullets(""), [])


class FeedServer:
    """Tiny local HTTP server so fetches are real but offline."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append((self.path, dict(self.headers)))
                route = server.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                time.sleep(route.get("delay", 0))
                etag = route.get("etag")
                if etag and self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                body = route.get("body", b"")
                self.send_response(route.get("status", 200))
                self.send_header("Content-Type", route.get("type", "application/rss+xml; charset=utf-8"))
                if etag:
                    self.send_header("ETag", etag)
                for name, value in route.get("headers", {}).items():
                    self.send_header(name, value)
                if not route.get("no_length"):
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path, host="127.0.0.1"):
        return f"http://{host}:{self.httpd.server_address[1]}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ServerTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = FeedServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        super().tearDownClass()

    def setUp(self):
        Feed.objects.all().delete()  # drop the seeded real-world feeds
        self.server.routes.clear()
        self.server.requests.clear()
        self.world = Section.objects.get(name="World")
        # The test server is on loopback, which the fetcher normally refuses.
        patcher = mock.patch.object(ingest, "_address_allowed", lambda address: True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_feed(self, path, body=b"", section=None, **route):
        self.server.routes[path] = {"body": body, **route}
        return Feed.objects.create(name=path.strip("/"), url=self.server.url(path), section=section)

    def items(self, n, prefix="https://example.com/", **extra):
        return [
            {"title": f"Story {i}", "link": f"{prefix}{i}", "guid": f"{prefix}{i}",
             "description": f"Summary {i}.", "pubDate": ago(hours=i + 1), **extra}
            for i in range(n)
        ]


class IngestTests(ServerTestCase):
    def test_insert_then_conditional_get(self):
        feed = self.make_feed("/a", rss(self.items(3)), section=self.world, etag='"v1"')
        report = run(force=True)
        self.assertEqual((report.new, report.failures), (3, []))
        feed.refresh_from_db()
        self.assertEqual((feed.last_status, feed.etag, feed.consecutive_failures), ("ok", '"v1"', 0))
        self.assertEqual(set(FeedItem.objects.values_list("section", flat=True)), {self.world.pk})

        report = run(force=True)
        self.assertEqual((report.new, report.feeds[0].status), (0, Feed.STATUS_NOT_MODIFIED))
        headers = self.server.requests[-1][1]
        self.assertEqual(headers["If-None-Match"], '"v1"')
        self.assertTrue(headers["User-Agent"].startswith("SkimNewsBot/"))
        self.assertEqual(FeedItem.objects.count(), 3)

    def test_only_new_entries_are_added(self):
        self.make_feed("/a", rss(self.items(2)))
        run(force=True)
        self.server.routes["/a"]["body"] = rss(self.items(4))
        self.assertEqual(run(force=True).new, 2)
        self.assertEqual(FeedItem.objects.count(), 4)

    def test_one_bad_item_does_not_sink_the_feed(self):
        doc = rss([
            {"title": "Bad\x00 one", "link": "https://e.com/1", "guid": "1"},
            {"title": "Emoji &#55357;&#56832;", "link": "https://e.com/2", "guid": "2\x00"},
            {"title": "Good", "link": "https://e.com/3", "guid": "3"},
        ])
        feed = self.make_feed("/a", doc)
        report = run(force=True)
        self.assertEqual((report.new, report.failures), (3, []))
        feed.refresh_from_db()
        self.assertEqual(feed.last_status, "ok")
        self.assertNotIn("\x00", "".join(FeedItem.objects.values_list("title", flat=True)))

    def test_control_characters_in_headers_are_dropped(self):
        feed = self.make_feed("/a", rss(self.items(1)), headers={"ETag": '"v\x001"'})
        self.assertEqual(run(force=True).failures, [])
        feed.refresh_from_db()
        self.assertEqual(feed.etag, '"v1"')

    def test_same_article_in_two_feeds_is_stored_once_with_a_section(self):
        shared = self.items(1, prefix="https://www.example.com/shared/")
        self.make_feed("/top", rss(shared))
        run(force=True)
        self.assertIsNone(FeedItem.objects.get().section)

        moved = [dict(shared[0], link="http://example.com/shared/0?utm_source=rss", guid="other-guid")]
        self.make_feed("/world", rss(moved), section=self.world)
        self.assertEqual(run(force=True).new, 0)
        self.assertEqual(FeedItem.objects.get().section, self.world)

    def test_distinct_items_sharing_a_link_or_guid_are_all_kept(self):
        front_page = [dict(item, link="https://e.com/section") for item in self.items(3)]
        same_guid = [dict(item, guid="dup") for item in self.items(3, prefix="https://f.com/")]
        live_blog = [dict(item, link=f"https://g.com/live#post-{i}", guid=f"post-{i}")
                     for i, item in enumerate(self.items(2))]
        self.make_feed("/a", rss(front_page + same_guid + live_blog))
        self.assertEqual(run(force=True).new, 8)
        self.assertEqual(run(force=True).new, 0)

    def test_errors_back_off_then_recover(self):
        feed = self.make_feed("/bad", status=500)
        report = run(force=True)
        self.assertEqual(report.failures[0].error, "HTTP 500")
        feed.refresh_from_db()
        self.assertEqual((feed.last_status, feed.consecutive_failures), ("error", 1))
        self.assertFalse(feed.is_due(feed.last_fetched_at + timedelta(minutes=27)))
        self.assertTrue(feed.is_due(feed.last_fetched_at + timedelta(minutes=29)))

        self.server.routes["/bad"] = {"body": rss(self.items(1))}
        run(force=True)
        feed.refresh_from_db()
        self.assertEqual((feed.last_status, feed.consecutive_failures, feed.last_error), ("ok", 0, ""))

    def test_unparseable_empty_and_oversize_responses_fail(self):
        self.make_feed("/html", b"<html><body>nope</body></html>", type="text/html")
        self.make_feed("/empty", b"")
        self.make_feed("/big", rss(self.items(50)))
        with mock.patch.object(ingest, "MAX_BYTES", 1000):
            errors = {r.feed.name: r.error for r in run(force=True).failures}
        self.assertTrue(errors["html"].startswith("Parse error"))
        self.assertEqual(errors["empty"], "Parse error: Empty response")
        self.assertTrue(errors["big"].startswith("Response over"))
        self.assertEqual(FeedItem.objects.count(), 0)

    def test_truncated_download_does_not_save_validators(self):
        doc = rss(self.items(5))
        feed = self.make_feed("/a", doc[: doc.index(b"<item><title>Story 3")], etag='"v1"', no_length=True)
        self.assertEqual(run(force=True).new, 3)
        feed.refresh_from_db()
        self.assertEqual((feed.last_status, feed.etag), ("ok", ""))

    def test_unreachable_feed_fails_cleanly(self):
        Feed.objects.create(name="dead", url="http://127.0.0.1:9/feed")
        self.assertIn("ConnectionError", run(force=True).failures[0].error)

    def test_private_addresses_are_refused_on_every_hop(self):
        self.make_feed("/secret", rss(self.items(1)))
        self.make_feed("/hop", status=302, headers={"Location": self.server.url("/secret", host="127.0.0.2")})
        with mock.patch.object(ingest, "_address_allowed", REAL_ADDRESS_POLICY):
            errors = [r.error for r in run(force=True).failures]
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(e.startswith("Blocked:") for e in errors))

        only_first_host = lambda address: str(address) == "127.0.0.1"  # noqa: E731
        with mock.patch.object(ingest, "_address_allowed", only_first_host):
            report = run([Feed.objects.get(name="hop")])
        self.assertIn("127.0.0.2", report.failures[0].error)
        self.assertNotIn("/secret", [path for path, _ in self.server.requests[-1:]])

    def test_run_deadline_abandons_slow_feeds(self):
        self.make_feed("/slow", rss(self.items(1)), delay=3)
        self.make_feed("/fast", rss(self.items(1, prefix="https://fast.example/")))
        started = time.monotonic()
        report = run(force=True, deadline=1)
        self.assertLess(time.monotonic() - started, 2.5)
        by_name = {r.feed.name: r for r in report.feeds}
        self.assertEqual(by_name["fast"].new, 1)
        self.assertIn("deadline", by_name["slow"].error)

    def test_feeds_never_reached_are_not_penalised(self):
        # Feeds are queued by name, so the slow one is fetched first.
        self.make_feed("/a-slow", rss(self.items(1)), delay=3)
        queued = self.make_feed("/b-queued", rss(self.items(1, prefix="https://q.example/")))
        report = run(force=True, deadline=1, workers=1)
        by_name = {r.feed.name: r for r in report.feeds}
        self.assertIn("deadline", by_name["a-slow"].error)
        self.assertEqual(by_name["b-queued"].status, "skipped")
        queued.refresh_from_db()
        self.assertEqual((queued.last_fetched_at, queued.consecutive_failures), (None, 0))

    def test_only_due_active_feeds_are_fetched(self):
        due = self.make_feed("/due", rss(self.items(1)))
        recent = self.make_feed("/recent", rss(self.items(1)))
        Feed.objects.filter(pk=recent.pk).update(last_fetched_at=timezone.now() - timedelta(minutes=5))
        self.make_feed("/off", rss(self.items(1)))
        Feed.objects.filter(url__endswith="/off").update(active=False)
        report = run()
        self.assertEqual([r.feed.pk for r in report.feeds], [due.pk])

    def test_retention_follows_what_feeds_still_list(self):
        old = self.items(1, prefix="https://old.example/", pubDate=ago(days=20))
        undated = [{"title": "Undated", "link": "https://e.com/undated", "guid": "u"}]
        self.make_feed("/a", rss(old + undated))
        self.assertEqual(run(force=True).new, 1)  # the 20-day-old entry is ignored
        item = FeedItem.objects.get()
        long_ago = timezone.now() - timedelta(days=30)

        # Still listed: never pruned, never re-added as new.
        FeedItem.objects.update(published_at=long_ago, fetched_at=long_ago, last_seen_at=long_ago)
        report = run(force=True)
        self.assertEqual((report.new, report.pruned), (0, 0))
        self.assertEqual(FeedItem.objects.get().pk, item.pk)

        # Gone from the feed for longer than retention: pruned for good,
        # unless it was drafted into a story.
        drafted = FeedItem.objects.create(feed=item.feed, guid="d", guid_hash="d", title="drafted",
                                          published_at=long_ago, last_seen_at=long_ago,
                                          story=Story.objects.create(headline="h", bullets="b"))
        self.server.routes["/a"]["body"] = rss([])
        FeedItem.objects.update(last_seen_at=long_ago)
        self.assertEqual(run(force=True).pruned, 1)
        self.assertEqual(list(FeedItem.objects.values_list("pk", flat=True)), [drafted.pk])

    def test_not_modified_keeps_listed_items_alive(self):
        feed = self.make_feed("/a", rss(self.items(1)), etag='"v1"')
        run(force=True)
        long_ago = timezone.now() - timedelta(days=30)
        FeedItem.objects.update(last_seen_at=long_ago)
        Feed.objects.filter(pk=feed.pk).update(last_success_at=long_ago)
        report = run(force=True)
        self.assertEqual((report.feeds[0].status, report.pruned), (Feed.STATUS_NOT_MODIFIED, 0))
        self.assertGreater(FeedItem.objects.get().last_seen_at, long_ago)

    @skipUnless(connection.vendor == "postgresql", "advisory locks are Postgres-only")
    def test_overlapping_run_is_skipped_and_session_is_restored(self):
        self.make_feed("/a", rss(self.items(1)))
        other = connection.copy()
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [LOCK_KEY])
            self.assertTrue(run(force=True).locked_out)
            self.assertEqual(self.server.requests, [])
        finally:
            other.close()
        self.assertEqual(run(force=True).new, 1)
        with connection.cursor() as cursor:
            cursor.execute("SHOW lock_timeout")
            self.assertEqual(cursor.fetchone()[0], "0")

    def test_deleting_a_feed_keeps_drafted_items(self):
        feed = self.make_feed("/a", rss(self.items(2)))
        run(force=True)
        drafted, other = FeedItem.objects.order_by("pk")
        drafted.story = Story.objects.create(headline="h", bullets="b")
        drafted.save()
        feed.delete()
        drafted.refresh_from_db()
        self.assertIsNone(drafted.feed)
        self.assertTrue(FeedItem.objects.filter(pk=other.pk, feed__isnull=True).exists())

    def test_command(self):
        self.make_feed("/a", rss(self.items(2)))
        out = StringIO()
        call_command("ingest_feeds", "--force", stdout=out)
        self.assertIn("OK   a: 2 new", out.getvalue())
        self.assertIn("1 feed(s), 2 new item(s), 0 failed, 0 skipped", out.getvalue())

        Feed.objects.update(url=self.server.url("/missing"))
        with self.assertRaisesMessage(CommandError, "All feeds failed"):
            call_command("ingest_feeds", "--force", stdout=StringIO())
        with self.assertRaisesMessage(CommandError, "No feed with id 999"):
            call_command("ingest_feeds", "--feed", "999", stdout=StringIO())


# Admin pages reference static files; the manifest only exists after collectstatic.
@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class AdminTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        call_command("setup_roles", stdout=StringIO())
        self.feed = self.make_feed("/a", section=self.world)
        self.item = self.make_item("g1")

    def make_item(self, guid, **fields):
        values = dict(
            feed=self.feed, section=self.world, guid=guid, guid_hash=guid,
            link=f"https://example.com/{guid}", title="Wire headline",
            summary="First sentence here. Second one follows. Third. Fourth.",
            published_at=timezone.now(),
        )
        values.update(fields)
        return FeedItem.objects.create(**values)

    def user(self, name, group, staff=True):
        user = User.objects.create_user(name, password="x", is_staff=staff)
        user.groups.add(Group.objects.get(name=group))
        self.client.force_login(user)
        return user

    def action(self, model, name, pks):
        return self.client.post(
            reverse(f"admin:feeds_{model}_changelist"),
            {"action": name, helpers.ACTION_CHECKBOX_NAME: pks, "index": 0},
        )

    def messages(self, response):
        return [str(m) for m in response.wsgi_request._messages]

    def test_journalist_drafts_a_story_from_the_wire(self):
        journalist = self.user("jo", "Journalist")
        self.assertEqual(self.client.get(reverse("admin:feeds_feeditem_changelist")).status_code, 200)

        response = self.action("feeditem", "create_draft_stories", [self.item.pk])
        self.item.refresh_from_db()
        story = self.item.story
        self.assertRedirects(response, reverse("admin:news_story_change", args=[story.pk]))
        self.assertEqual(
            (story.author, story.section, story.published, story.placement, story.headline),
            (journalist, self.world, False, "section", "Wire headline"),
        )
        self.assertEqual(story.bullet_list(), ["First sentence here.", "Second one follows.", "Third."])

        page = self.client.get(reverse("admin:news_story_change", args=[story.pk]))
        self.assertContains(page, "https://example.com/g1")

        self.action("feeditem", "create_draft_stories", [self.item.pk])
        self.assertEqual(Story.objects.filter(feed_item__isnull=False).count(), 1)

    def test_drafting_warns_about_missing_sections_and_placeholder_bullets(self):
        item = self.make_item("g2", section=None, summary="")
        self.user("jo", "Journalist")
        response = self.action("feeditem", "create_draft_stories", [item.pk])
        item.refresh_from_db()
        self.assertEqual(item.story.bullets, PLACEHOLDER)
        self.assertTrue(any("no section" in m for m in self.messages(response)))

    def test_drafting_is_capped_per_action(self):
        pks = [self.make_item(f"bulk{i}").pk for i in range(MAX_DRAFTS_PER_ACTION)] + [self.item.pk]
        self.user("jo", "Journalist")
        response = self.action("feeditem", "create_draft_stories", pks)
        self.assertTrue(any("at most" in m for m in self.messages(response)))
        self.assertFalse(Story.objects.filter(feed_item__isnull=False).exists())

    def test_wire_is_read_only(self):
        self.user("boss", "Admin")
        url = reverse("admin:feeds_feeditem_change", args=[self.item.pk])
        self.assertNotContains(self.client.get(url), 'name="_save"')
        self.assertEqual(self.client.post(url, {"title": "x"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:feeds_feeditem_add")).status_code, 403)
        delete = reverse("admin:feeds_feeditem_delete", args=[self.item.pk])
        self.assertEqual(self.client.get(delete).status_code, 403)

    def test_feed_management_is_admin_only(self):
        self.user("jo", "Journalist")
        self.assertEqual(self.client.get(reverse("admin:feeds_feed_changelist")).status_code, 403)

        self.user("ed", "Editor")
        page = self.client.get(reverse("admin:feeds_feed_changelist"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'value="fetch_next_run"')
        self.assertEqual(self.client.get(reverse("admin:feeds_feed_add")).status_code, 403)

        self.user("reader", "Reader", staff=False)
        self.assertEqual(self.client.get(reverse("admin:feeds_feeditem_changelist")).status_code, 302)

    def test_fetch_on_next_run(self):
        Feed.objects.filter(pk=self.feed.pk).update(
            last_fetched_at=timezone.now(), consecutive_failures=4, last_status="error"
        )
        self.user("boss", "Admin")
        response = self.action("feed", "fetch_next_run", [self.feed.pk])
        self.assertEqual(self.messages(response), ["1 feed(s) will be fetched on the next scheduled run."])
        self.feed.refresh_from_db()
        self.assertTrue(self.feed.is_due())
        self.assertEqual(self.server.requests, [])  # nothing fetched in the web request

    def form(self, **overrides):
        data = {"name": "Feed", "url": "https://example.org/rss.xml", "section": self.world.pk,
                "active": "on", "interval_minutes": 15}
        data.update(overrides)
        return data

    def test_feed_form_rejects_private_addresses(self):
        self.user("boss", "Admin")
        for url in ("http://169.254.169.254/latest", "http://localhost/feed",
                    "http://postgres.railway.internal/", "http://[::1]/feed"):
            with self.subTest(url=url):
                response = self.client.post(reverse("admin:feeds_feed_add"), self.form(url=url))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Use a public feed address")
        self.assertFalse(Feed.objects.filter(name="Feed").exists())

    def test_changing_a_feed_url_resets_its_fetch_state(self):
        Feed.objects.filter(pk=self.feed.pk).update(
            etag='"old"', last_modified="x", consecutive_failures=5,
            last_fetched_at=timezone.now(), last_status="error", last_error="boom",
        )
        self.user("boss", "Admin")
        response = self.client.post(
            reverse("admin:feeds_feed_change", args=[self.feed.pk]),
            self.form(name="a", url="https://example.org/new.xml"),
        )
        self.assertEqual(response.status_code, 302)
        self.feed.refresh_from_db()
        self.assertEqual(
            (self.feed.etag, self.feed.consecutive_failures, self.feed.last_error, self.feed.is_due()),
            ("", 0, "", True),
        )

    def test_drafted_story_keeps_its_source_after_the_feed_is_deleted(self):
        self.user("jo", "Journalist")
        self.action("feeditem", "create_draft_stories", [self.item.pk])
        self.feed.delete()
        story = Story.objects.get(feed_item__isnull=False)
        page = self.client.get(reverse("admin:news_story_change", args=[story.pk]))
        self.assertContains(page, "example.com ↗")

    def test_admin_pages_render(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))
        self.server.routes["/a"]["status"] = 500
        run(force=True)  # record a failure so the health fields have content
        for url in (
            reverse("admin:feeds_feed_changelist"),
            reverse("admin:feeds_feed_change", args=[self.feed.pk]),
            reverse("admin:feeds_feed_add"),
            reverse("admin:feeds_feeditem_changelist") + "?drafted=no",
            reverse("admin:feeds_feeditem_change", args=[self.item.pk]),
            reverse("admin:news_story_change", args=[Story.objects.first().pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
