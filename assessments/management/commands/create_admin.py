"""
One-time bootstrap for the first Super Admin account.

Never ship a hard-coded 'admin'/'admin' account — that is the single most
common way an internal tool gets compromised the week it's deployed.

This command requires --username and --email arguments. It either uses a
provided --password or generates a strong random password that is printed
exactly once and never stored or logged anywhere else.

Usage:
    python manage.py create_admin --username admin --email admin@example.com
    python manage.py create_admin --username admin --email admin@example.com --password MySecr3t
"""
import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Create an initial superuser account for the assessment platform."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            required=True,
            help="Username for the admin account.",
        )
        parser.add_argument(
            "--email",
            required=True,
            help="Email address for the admin account.",
        )
        parser.add_argument(
            "--password",
            default=None,
            help="Password (if omitted, a strong random one is generated and shown once).",
        )

    def handle(self, *args, **options):
        username = options["username"]
        email = options["email"]
        password = options["password"]

        if User.objects.filter(username=username).exists():
            self.stdout.write(
                self.style.WARNING(
                    f"User '{username}' already exists — not touching their password."
                )
            )
            return

        generated = password is None
        if generated:
            password = secrets.token_urlsafe(18)

        User.objects.create_superuser(username=username, email=email, password=password)

        self.stdout.write(
            self.style.SUCCESS(f"Super Admin '{username}' created with email '{email}'.")
        )
        if generated:
            self.stdout.write(f"Generated password (shown once, not stored): {password}")
            self.stdout.write("Log in and change it immediately.")
