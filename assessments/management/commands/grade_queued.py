import time
from django.core.management.base import BaseCommand

from assessments.services import grade_queued


class Command(BaseCommand):
    help = "Grade queued coding submissions. Can run as a continuous worker daemon or via cron."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--daemon", action="store_true", help="Run continuously in a loop as a worker process.")
        parser.add_argument("--sleep", type=float, default=2.0, help="Seconds to sleep between polls in daemon mode.")

    def handle(self, *args, **options):
        limit = options["limit"]
        daemon = options["daemon"]
        sleep_interval = options["sleep"]

        if not daemon:
            graded = grade_queued(limit=limit)
            self.stdout.write(f"Graded {graded} submission(s).")
            return

        self.stdout.write(self.style.SUCCESS(f"Starting grading worker daemon (poll interval: {sleep_interval}s)..."))
        try:
            while True:
                graded = grade_queued(limit=limit)
                if graded:
                    self.stdout.write(f"Graded {graded} submission(s).")
                time.sleep(sleep_interval)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Grading worker daemon stopped gracefully."))
