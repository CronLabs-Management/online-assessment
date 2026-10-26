from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assessments", "0003_application_resume_and_coding_languages"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="site_name",
            field=models.CharField(blank=True, default="Assessments", max_length=150),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="site_logo",
            field=models.FileField(blank=True, help_text="Brand logo image displayed in the header", null=True, upload_to="branding/"),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="favicon",
            field=models.FileField(blank=True, help_text="Website favicon (.ico, .png, .svg)", null=True, upload_to="branding/"),
        ),
    ]
