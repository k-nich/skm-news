"""WSGI config for the SKIM NEWS project."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "skimnews.settings")

application = get_wsgi_application()
