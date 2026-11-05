from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("assessments", "0004_sitesettings_branding"),
    ]

    operations = [
        migrations.AddField(
            model_name="application",
            name="plain_password",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Last generated password for candidate login.",
                max_length=128,
            ),
        ),
        migrations.AddField(
            model_name="examsection",
            name="is_default",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="mcqquestion",
            name="section",
            field=models.ForeignKey(
                blank=True,
                help_text="The section this MCQ question belongs to. If not set, belongs to default section.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="questions",
                to="assessments.examsection",
            ),
        ),
    ]
