# Generated manually for resume and expanded coding languages
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assessments", "0002_exam_security"),
    ]

    operations = [
        migrations.AddField(
            model_name="application",
            name="resume",
            field=models.FileField(blank=True, max_length=255, null=True, upload_to="resumes/"),
        ),
        migrations.AlterField(
            model_name="codingproblem",
            name="languages",
            field=models.CharField(
                default="python, java, c, cpp",
                help_text="Comma-separated languages candidates may use. Supported: python, java, c, cpp, javascript",
                max_length=200,
            ),
        ),
    ]
