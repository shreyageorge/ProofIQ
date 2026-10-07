"""WSGI entry point for hosts that expect an app.py module."""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dataguard.settings")

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
