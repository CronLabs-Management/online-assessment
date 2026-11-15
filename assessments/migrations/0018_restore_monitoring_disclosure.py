"""Restore the full monitoring disclosure, and drop the legacy plain copy of the Gemini key.

Migration 0017 replaced the default notice with text that no longer said the webcam, location and
network are recorded. Sites that still show that vague text (unchanged by an admin) get the full
notice back, so candidates' consent covers what is actually collected.
"""

from django.db import migrations, models

VAGUE_NOTICE = (
    "Browser and screen checks verify that your system is ready for the assessment.\n"
    "Network connection check ensures uninterrupted saving of your answers and code.\n"
    "Fullscreen view is maintained throughout the assessment to ensure focus.\n"
    "Your answers, code submissions, and time spent are securely submitted upon completion.\n"
    "All assessment data is confidential and evaluated solely by the hiring team."
)
VAGUE_TITLE = "Assessment Guidelines & Conduct"
FULL_NOTICE = (
    "Your webcam video is analysed during the test, and still images are saved when something unusual happens.\n"
    "Your approximate location is recorded when you start.\n"
    "Your network details are recorded: IP address, connection type and speed.\n"
    "Your browser and device details are recorded, such as screen size and operating system.\n"
    "If the test uses your microphone, only sound levels are checked for talking; audio is never recorded.\n"
    "Leaving full screen, switching tabs or windows, extra screens, and copy and paste are recorded.\n"
    "Your answers, code, and the time you spend on each question are recorded.\n"
    "This information is used only to review this assessment, is visible only to the hiring team, "
    "and evidence images are deleted after the retention period."
)

TITLE = "Before you begin: how this test is monitored"


def restore(apps, schema_editor):
    SiteSettings = apps.get_model("assessments", "SiteSettings")
    for site in SiteSettings.objects.all():
        fields = []
        if (site.monitoring_notice or "").strip() == VAGUE_NOTICE:
            site.monitoring_notice = FULL_NOTICE
            fields.append("monitoring_notice")
        if (site.monitoring_notice_title or "").strip() == VAGUE_TITLE:
            site.monitoring_notice_title = TITLE
            fields.append("monitoring_notice_title")
        if fields:
            site.save(update_fields=fields)


def clear_legacy_key(apps, schema_editor):
    """The old single Gemini key was copied into the key pool by 0016; don't keep a second plain copy."""
    SiteSettings = apps.get_model("assessments", "SiteSettings")
    AIKey = apps.get_model("assessments", "AIKey")
    for site in SiteSettings.objects.exclude(gemini_api_key=""):
        if AIKey.objects.filter(api_key=site.gemini_api_key.strip()).exists():
            site.gemini_api_key = ""
            site.save(update_fields=["gemini_api_key"])


class Migration(migrations.Migration):
    dependencies = [("assessments", "0017_alter_application_phone_alter_attempt_deadline_at_and_more")]

    operations = [
        migrations.AlterField(
            model_name="sitesettings",
            name="monitoring_notice",
            field=models.TextField(
                blank=True,
                default=FULL_NOTICE,
                help_text="What we collect during a test, one item per line. Candidates must agree before they can start.",
            ),
        ),
        migrations.AlterField(
            model_name="sitesettings",
            name="monitoring_notice_title",
            field=models.CharField(blank=True, default=TITLE, max_length=200),
        ),
        migrations.RunPython(restore, migrations.RunPython.noop),
        migrations.RunPython(clear_legacy_key, migrations.RunPython.noop),
    ]
