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
5. Deploy. On boot the Procfile runs migrations, collects static files,
   then starts Gunicorn.
6. Create your admin login from the Railway shell (or `railway run`):
   `python manage.py createsuperuser`

The front page is at `/`, the Django admin at `/admin/`.

## What each production file does

- `Procfile`         - start command: migrate, collectstatic, then Gunicorn.
- `requirements.txt` - adds gunicorn, psycopg (Postgres), whitenoise, dj-database-url.
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
