from django.core.management.base import BaseCommand

from assessments.services import grade_queued


class Command(BaseCommand):
    help = "Grade queued coding submissions. Run every minute from cron on cPanel."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        graded = grade_queued(limit=options["limit"])
        self.stdout.write(f"Graded {graded} submission(s).")
