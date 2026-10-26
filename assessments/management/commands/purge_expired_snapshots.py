from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from assessments import services
from assessments.models import ProctoringSnapshot, SiteSettings


class Command(BaseCommand):
    help = "Delete evidence images older than the retention period set in Settings. Run daily from cron."

    def handle(self, *args, **options):
        days = SiteSettings.load().snapshot_retention_days
        cutoff = timezone.now() - timedelta(days=days)
        expired = ProctoringSnapshot.objects.filter(received_at__lt=cutoff)
        removed = failed = 0
        for snapshot in expired:
            try:
                path = services.snapshot_file_path(snapshot)
                if path.exists():
                    path.unlink()
            except (OSError, services.AttemptError) as exc:
                # The row is kept until its file is confirmed gone, so the next run retries it.
                failed += 1
                self.stderr.write(f"Could not delete snapshot {snapshot.pk}: {exc}")
                continue
            snapshot.delete()
            removed += 1
        self.stdout.write(f"Removed {removed} image(s) older than {days} days; {failed} failed.")
