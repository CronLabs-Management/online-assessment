"""One interview review per attempt and round.

Bulk and single decisions both write the review for a round; without a constraint, two requests at
once could create two rows and the round would be counted twice. Older duplicates are merged first:
the newest review is kept, which is the one the review page already showed.
"""

from django.db import migrations, models


def drop_duplicates(apps, schema_editor):
    InterviewReview = apps.get_model("assessments", "InterviewReview")
    seen = set()
    stale = []
    for review in InterviewReview.objects.order_by("attempt_id", "round_number", "-created_at", "-pk").only(
        "pk", "attempt_id", "round_number"
    ).iterator():
        key = (review.attempt_id, review.round_number)
        if key in seen:
            stale.append(review.pk)
        else:
            seen.add(key)
    for start in range(0, len(stale), 500):
        InterviewReview.objects.filter(pk__in=stale[start:start + 500]).delete()


class Migration(migrations.Migration):
    dependencies = [("assessments", "0018_restore_monitoring_disclosure")]

    operations = [
        migrations.RunPython(drop_duplicates, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="interviewreview",
            constraint=models.UniqueConstraint(fields=("attempt", "round_number"), name="one_review_per_round"),
        ),
    ]
