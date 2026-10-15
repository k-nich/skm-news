# SKIM NEWS

A skim-first newspaper front page, served by Django, deployable to Railway.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/ . Locally it uses SQLite and a dev secret key
automatically (DEBUG is on by default).

## Deploy to Railway

1. Push this project to a GitHub repo.
2. In Railway: New Project -> Deploy from GitHub repo -> pick this repo.
3. Add a database: in the project, New -> Database -> Add PostgreSQL.
4. Open your web service -> Variables tab and add:
   - `DJANGO_SECRET_KEY`  = a long random string (generate one with
     `python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"`)
   - `DJANGO_DEBUG`       = `0`
   - `DATABASE_URL`       = reference the Postgres service: `${{Postgres.DATABASE_URL}}`
   (Railway also injects `RAILWAY_PUBLIC_DOMAIN`, which settings.py reads
   automatically for ALLOWED_HOSTS and CSRF.)
5. Deploy. On boot the Procfile runs migrations, syncs the role groups,
   collects static files, then starts Gunicorn. The service's Root Directory
   setting must be blank (the repo root).
6. Create your admin login from the Railway shell (or `railway run`):
   `python manage.py createsuperuser`

The front page is at `/`, the Django admin at `/admin/`.

## What each production file does

- `Procfile`         - start command: migrate, setup_roles, collectstatic, then Gunicorn.
- `requirements.txt` - adds gunicorn, psycopg (Postgres), whitenoise, dj-database-url,
                       feedparser and requests (RSS wire).
- `.python-version`  - pins Python 3.12 for reproducible builds.
- `.env.example`     - the variables to set (never commit a real `.env`).
- `settings.py`      - reads SECRET_KEY / DEBUG / hosts / DATABASE_URL from the
                       environment; WhiteNoise serves static files; security
                       hardening switches on automatically when DEBUG=0.

## Editing content (database-driven)

Content lives in the database and is edited through the Django admin at
`/admin/`:

- **Sections** (World, Tech, ...) — each becomes a column. `order` sets
  left-to-right placement.
- **Stories** — a headline, an optional kicker, and bullets (one per line).
  - `placement` = *Lead* puts it in the big top band; *Section* puts it in
    that section's column.
  - `secs` is the scan time that feeds the read meter.
  - `breaking` shows the pulsing marker; `published` hides without deleting;
    `order` sorts within the placement/section.

The view (`news/views.py`) assembles the published stories and injects them
into the page as JSON; the front-end renderer reads that JSON. The template
no longer holds any content — only layout and styling.

A data migration (`news/migrations/0002_seed_edition.py`) seeds the original
sample edition on first migrate, so a fresh database (including the first
Railway deploy) starts populated rather than blank. It won't double-seed if
stories already exist.

On Railway, edit the live edition by opening `/admin/` on your deployed URL
(create a login with `python manage.py createsuperuser` via the Railway shell).

## RSS wire (feeds app)

Public news feeds are pulled into a read-only **wire** in the admin
(*Wire (RSS feeds)*). Nothing from a feed is published automatically.

- **Feeds** (Admin role): name, URL, default section, active flag and minimum
  fetch interval. Health columns show last status, consecutive failures and
  the next due time. "Fetch on the next scheduled run" clears a feed's wait
  and backoff; changing a feed's URL does the same. Fetching only happens in
  the cron service, never in a web request. Thirteen public-broadcaster feeds
  (ABC, SBS, RNZ, NPR) are seeded by `feeds/migrations/0002_seed_feeds.py`,
  mapped to sections by name.
- **Feed items** (Journalist, Editor, Admin): filter by status, section or
  feed, open the original, then select up to 25 items and run "Create draft
  stories from selected items". Each draft is an unpublished Story owned by
  the user, with the item's headline, section and first 2-3 summary sentences
  as bullets; the story page links back to the original. Feed text is the
  publisher's copy, so rewrite it before publishing. Items can't be edited or
  deleted (the next fetch would bring them back).

`python manage.py ingest_feeds` (`feeds/ingest.py`):

- Fetches active feeds that are due: each feed's interval, doubled after each
  consecutive failure (capped at 24h).
- Downloads 8 feeds in parallel with ETag/Last-Modified conditional GETs.
  Limits: 5s connect and 15s read timeouts, 4 MB and 200 entries per feed,
  and a 240s cap per run. Downloads still going at the cap are abandoned
  (they run in daemon threads); feeds not reached are left due without a
  failure. Postgres statements time out after 60s, and the process kills
  itself 120s after the cap, so a run always ends.
- Only fetches public addresses: private, loopback, link-local and internal
  hosts are refused in the admin form and re-checked on every redirect hop,
  and each request connects to the address that was checked. URLs with
  credentials or backslashes are refused.
- Parses RSS 0.9x/1.0/2.0 and Atom (feedparser) into plain text with angle
  brackets neutralised, because the front page renders story text as HTML.
  Dates are stored in UTC (AU/NZ zone names handled); future dates are
  clamped to now, and missing or placeholder dates get the fetch time. One
  unreadable entry is skipped without failing the feed. Documents declaring
  XML entities, or in encodings where markup isn't plain ASCII (UTF-7,
  EBCDIC), are refused; UTF-16/32 is converted first.
- Dedupes by GUID within a feed and by normalised link across feeds, so a
  story carried by two feeds is stored once (keeping a section if either
  feed has one).
- Deletes undrafted items once no feed has listed them for
  `FEEDS_RETENTION_DAYS` (default 14). Entries already older than that are
  never added.
- Takes a Postgres advisory lock so overlapping runs exit straight away.
- Options: `--force` (ignore schedules), `--feed ID` (one feed), `--no-prune`,
  `--workers N`, `--deadline SECONDS`. Exits 1 only when every attempted feed
  failed.

### Railway cron service

Ingestion runs as a second Railway service built from this same repo:

1. Project -> New -> GitHub Repo -> `k-nich/skm-news`. Name it `feeds-cron`;
   don't give it a public domain.
2. Settings -> Custom Start Command: `python manage.py ingest_feeds`
3. Settings -> Cron Schedule: `*/15 * * * *` (UTC; runs must be at least
   5 minutes apart).
4. Variables:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `DJANGO_SECRET_KEY` = `${{<web service name>.DJANGO_SECRET_KEY}}`
   - `DJANGO_DEBUG` = `0`
   - optional `FEEDS_RETENTION_DAYS`
5. Check the first run's logs: one `OK`, `304`, `SKIP` or `FAIL` line per
   feed, then a summary. Gunicorn output there means the custom start command
   isn't applied.

Railway skips a scheduled run while the previous one is still active and
never stops a hung process; the limits above prevent that. To test a feed
straight away, run `python manage.py ingest_feeds --feed ID` from the
`feeds-cron` service's shell.

## Custom domain: skm.news (registered at GoDaddy)

`skm.news` is an apex (root) domain. Railway points custom domains with a
CNAME, and apex CNAMEs only work if your DNS provider supports CNAME
flattening / ALIAS — which GoDaddy does NOT. Two ways around it:

### Recommended: move DNS to Cloudflare (domain stays registered at GoDaddy)
1. Deploy on Railway and confirm the *.up.railway.app URL works.
2. Railway service -> Settings -> Networking -> Custom Domain -> add `skm.news`
   (and optionally `www.skm.news`). Railway shows a CNAME target plus a TXT
   verification record — both are required.
3. Create a free Cloudflare account, add `skm.news`; Cloudflare imports your
   existing records and gives you two nameservers.
4. In GoDaddy: domain -> Nameservers -> use custom nameservers -> paste
   Cloudflare's two. (Propagation: usually under an hour, up to 72h.)
5. In Cloudflare DNS add the records Railway gave you:
     - CNAME, Name `@`, Target = Railway's CNAME value, set to "DNS only"
       (grey cloud) so Railway issues TLS and you avoid redirect loops.
     - the TXT verification record.
     - optional: CNAME `www` -> same target, plus a redirect rule www -> apex.
6. Railway verifies and provisions SSL automatically.
7. In Railway service Variables set: `DJANGO_ALLOWED_HOSTS=skm.news,www.skm.news`
   (settings.py derives CSRF origins from this automatically).

### Alternative: stay on GoDaddy, use www as the canonical site
- GoDaddy DNS: add CNAME `www` -> Railway's target, plus the TXT record.
- Apex `skm.news`: use GoDaddy Domain Forwarding to redirect to
  `https://www.skm.news` (apex CNAME isn't possible on GoDaddy).
- Set `DJANGO_ALLOWED_HOSTS=www.skm.news,skm.news`.
The tradeoff: your primary URL becomes www.skm.news rather than the bare apex.

## User roles (groups)

Four groups are defined in `news/management/commands/setup_roles.py`:

- **Admin** — full control of content (sections + stories), locking, and
  other user accounts.
- **Editor** — can edit ANY story and lock/unlock stories; cannot manage
  users or delete stories (delete is reserved for Admin).
- **Journalist** — can add and edit only their OWN stories, and only while
  unlocked; sees just their own stories in the admin. Cannot lock, cannot
  edit others' stories, cannot manage users.
- **Reader** — no admin permissions. The public site is readable without an
  account, so this is a forward-looking label for future reader-only
  features (saved articles, comments, newsletter, paywall).

### Authorship and locking
- Every story has an **author**. When someone creates a story, they become
  the author automatically; Journalists can't reassign it.
- A story can be **locked** by Editors/Admins (a checkbox on the story, or
  the "Lock selected stories" bulk action). Once locked, the author sees the
  story read-only and can no longer change it — use this to freeze a piece
  once it's approved. Editors/Admins can still edit and unlock it.

The groups are created/refreshed automatically on every deploy (the Procfile
runs `setup_roles` after migrations). You can also run it by hand:

    python manage.py setup_roles

To adjust who-can-do-what, edit `ROLE_PERMISSIONS` in that command and
re-run it (or redeploy) — it sets each group's permissions to match.

### Assigning a person to a role
In `/admin/` -> Users -> pick the user:
1. Add the group (Admin or Journalist) under "Groups".
2. Tick **Staff status** so they can reach `/admin/` at all.
   (Group membership alone does NOT grant admin access — staff status is
   the switch. Readers should NOT be staff.)
3. Leave **Superuser status** OFF for Journalists; reserve it for owners.

Note: the Journalist "own stories only" restriction and the lock are
enforced in the admin (`news/admin.py`) via object-level permission checks,
on top of Django's model-level groups.
