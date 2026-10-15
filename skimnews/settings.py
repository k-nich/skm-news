"""
Django settings for the SKIM NEWS project.

Configuration is driven by environment variables so the same code runs
locally and on Railway. For local dev the defaults work out of the box;
on Railway you set the variables in the service's Variables tab.

Environment variables:
  DJANGO_SECRET_KEY     required in production (DEBUG=0)
  DJANGO_DEBUG          "1" for dev (default), "0" for production
  DJANGO_ALLOWED_HOSTS  comma-separated; Railway's domain is added automatically
  DATABASE_URL          provided by Railway's Postgres plugin; falls back to SQLite locally
"""
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

# SECRET_KEY: a dev-only fallback is used when DEBUG is on. In production
# (DEBUG=0) the app refuses to start without a real key from the environment.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"
    else:
        raise RuntimeError("DJANGO_SECRET_KEY must be set when DEBUG is off.")

# Hosts: start from any explicitly listed, then add Railway's public domain.
# Hosts come from DJANGO_ALLOWED_HOSTS (comma-separated). For the live site set
# it to your custom domain(s), e.g. "skm.news,www.skm.news".
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

# Railway injects its own public domain; include it automatically so the
# *.up.railway.app URL keeps working alongside your custom domain.
RAILWAY_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
if RAILWAY_DOMAIN and RAILWAY_DOMAIN not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RAILWAY_DOMAIN)

# CSRF needs the full https origin for each real (non-local) host. Django
# requires this for admin logins and any POST over HTTPS, so we derive it
# from ALLOWED_HOSTS rather than maintaining a second list by hand.
CSRF_TRUSTED_ORIGINS = [
    f"https://{h}" for h in ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1")
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "news",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files in production; keep it right after security.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "skimnews.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "skimnews.wsgi.application"

# Database: Railway's Postgres sets DATABASE_URL; locally we fall back to SQLite.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        ssl_require=not DEBUG,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-au"
TIME_ZONE = "Australia/Sydney"
USE_I18N = True
USE_TZ = True

# Static files (WhiteNoise with compressed, hashed filenames for caching).
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Security hardening that only switches on in production.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
