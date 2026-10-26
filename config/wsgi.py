"""WSGI entry point. Passenger on cPanel loads passenger_wsgi.py, which imports this."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
