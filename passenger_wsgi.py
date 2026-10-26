"""Entry point for cPanel's Passenger (Setup Python App). Keep this file at the app root."""

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Dynamically ensure cPanel virtualenv site-packages are on sys.path
home_dir = os.path.expanduser("~")
venv_base = os.path.join(home_dir, "virtualenv", os.path.basename(APP_DIR))
if os.path.isdir(venv_base):
    for ver in sorted(os.listdir(venv_base), reverse=True):
        site_packages = os.path.join(venv_base, ver, f"lib/python{ver}/site-packages")
        if os.path.isdir(site_packages) and site_packages not in sys.path:
            sys.path.insert(0, site_packages)
            break

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from config.wsgi import application  # noqa: E402
