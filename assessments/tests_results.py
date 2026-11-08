"""Results: marks per section, the results CSV, and bulk actions on the candidate list."""

import csv
import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Answer,
    Application,
    Attempt,
    AttemptMCQ,
    Exam,
    ExamSection,
    InterviewReview,
    MCQOption,
    MCQQuestion,
    StaffProfile,
    StaffRole,
)
from .services import section_scores

User = get_user_model()


class ResultsFixture(TestCase):
    def setUp(self):
        now = timezone.now()
        self.admin = User.objects.create_user("admin", "admin@example.com", "pw", is_staff=True)
        self.exam = Exam.objects.create(
            title="Hiring", opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=1),
            duration_minutes=30, created_by=self.admin, interview_rounds_count=2,
        )
        self.aptitude = ExamSection.objects.create(exam=self.exam, title="Aptitude", order=1)
        self.verbal = ExamSection.objects.create(exam=self.exam, title="Verbal", order=2)
        self.attempts = []
        for n in range(2):
            cand = User.objects.create_user(f"c{n}", f"c{n}@example.com", "pw")
            app = Application.objects.create(candidate=cand, exam=self.exam, full_name=f"Cand {n}",
                                             status=Application.Status.APPROVED)
            self.attempts.append(Attempt.objects.create(
                candidate=cand, exam=self.exam, application=app, status=Attempt.Status.SUBMITTED,
                started_at=now, deadline_at=now + timedelta(minutes=30), last_seen_at=now, ended_at=now,
            ))
        attempt = self.attempts[0]
        for pos, (section, marks, right) in enumerate([
            (self.aptitude, 2, True), (self.aptitude, 3, False), (self.verbal, 4, True),
        ], 1):
            q = MCQQuestion.objects.create(exam=self.exam, section=section, text=f"Q{pos}", marks=marks)
            good = MCQOption.objects.create(question=q, text="yes", is_correct=True, order=1)
            bad = MCQOption.objects.create(question=q, text="no", order=2)
            AttemptMCQ.objects.create(attempt=attempt, section=section, question=q, position=pos,
                                      option_order=[good.pk, bad.pk])
            Answer.objects.create(attempt=attempt, question=q, option=good if right else bad)
        self.client.force_login(self.admin)


class SectionScoreTests(ResultsFixture):
    def test_marks_are_split_by_section(self):
        rows = {r["title"]: (r["score"], r["max"]) for r in section_scores(self.attempts[0])}
        self.assertEqual(rows, {"Aptitude": (2.0, 5.0), "Verbal": (4.0, 4.0)})

    def test_export_has_a_column_per_section_and_round(self):
        InterviewReview.objects.create(attempt=self.attempts[0], round_number=1, interview_score=8,
                                       decision=InterviewReview.Decision.RECOMMENDED)
        response = self.client.get(reverse("staff_export_csv", args=[self.exam.pk]))
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        header = rows[0]
        for col in ("Aptitude marks", "Verbal marks", "round_1_score", "round_1_decision", "round_2_score"):
            self.assertIn(col, header)
        cand0 = next(r for r in rows[1:] if r[0] == "Cand 0")
        self.assertEqual(cand0[header.index("Aptitude marks")], "2.0")
        self.assertEqual(cand0[header.index("round_1_score")], "8.0")

    def test_review_page_shows_section_marks(self):
        from .models import AttemptSection

        AttemptSection.objects.create(attempt=self.attempts[0], section=self.aptitude, status="submitted")
        response = self.client.get(reverse("staff_attempt_review", args=[self.attempts[0].pk]))
        self.assertContains(response, "Marks: <b>2 / 5</b>", html=False)


class BulkActionTests(ResultsFixture):
    def _bulk(self, action, round_number=1, attempts=None):
        ids = [a.application_id for a in (attempts or self.attempts)]
        return self.client.post(reverse("staff_candidates_bulk", args=[self.exam.pk]),
                                {"action": action, "round": round_number, "selected_ids": ids})

    def test_advance_selected_candidates_to_next_round(self):
        response = self._bulk("advance", 1)
        self.assertEqual(response.status_code, 302)
        for attempt in self.attempts:
            review = InterviewReview.objects.get(attempt=attempt, round_number=1)
            self.assertTrue(review.selected_for_next_round)
            self.assertEqual(attempt.interview_rounds_status["current_round"], 2)

    def test_reject_keeps_existing_scores(self):
        InterviewReview.objects.create(attempt=self.attempts[0], round_number=1, interview_score=6,
                                       questions_data=[{"question": "Q", "marks_awarded": 6}])
        self._bulk("reject", 1, [self.attempts[0]])
        review = InterviewReview.objects.get(attempt=self.attempts[0], round_number=1)
        self.assertEqual(review.decision, InterviewReview.Decision.NOT_RECOMMENDED)
        self.assertEqual(float(review.interview_score), 6)
        self.assertEqual(review.questions_data[0]["question"], "Q")

    def test_unfinished_attempts_are_skipped(self):
        Attempt.objects.filter(pk=self.attempts[1].pk).update(status=Attempt.Status.IN_PROGRESS)
        self._bulk("advance", 1)
        self.assertFalse(InterviewReview.objects.filter(attempt=self.attempts[1]).exists())

    def test_flag_and_unflag(self):
        self._bulk("flag")
        self.assertEqual(Attempt.objects.filter(flagged_for_review=True).count(), 2)
        self._bulk("unflag")
        self.assertEqual(Attempt.objects.filter(flagged_for_review=True).count(), 0)

    def test_role_without_permission_cannot_decide(self):
        role = StaffRole.objects.create(name="Coordinator", permissions=["candidates.view", "candidates.manage"])
        user = User.objects.create_user("coord", "coord@example.com", "pw")
        StaffProfile.objects.create(user=user, role=role)
        self.client.force_login(user)
        self._bulk("advance", 1)
        self.assertFalse(InterviewReview.objects.exists())
        self._bulk("flag")
        self.assertEqual(Attempt.objects.filter(flagged_for_review=True).count(), 2)
