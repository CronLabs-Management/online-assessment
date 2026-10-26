from django.core.management.base import BaseCommand

from assessments.services import expire_due_attempts


class Command(BaseCommand):
    help = (
        "Finish attempts whose time has run out, including attempts the candidate abandoned. "
        "Run every few minutes from cron, before grade_queued."
    )

    def handle(self, *args, **options):
        processed = expire_due_attempts()
        self.stdout.write(f"Processed {processed} overdue attempt(s).")
