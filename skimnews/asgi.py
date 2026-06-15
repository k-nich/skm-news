"""ASGI config for the SKIM NEWS project."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "skimnews.settings")

application = get_asgi_application()
