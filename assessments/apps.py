from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _sqlite_wal_mode(sender, connection, **kwargs):
    """Readers no longer block the writer on SQLite (development and CI only; MySQL ignores this)."""
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")


class AssessmentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "assessments"
    verbose_name = "Assessments"

    def ready(self):
        connection_created.connect(_sqlite_wal_mode, dispatch_uid="assessments_sqlite_wal")
