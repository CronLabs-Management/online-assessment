"""Regression tests for the fixes from the enhancements-branch audit (2026-11-15)."""

import io
import zipfile
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import ai_client, interview_rounds, services
from .forms import validate_resume_file
from .models import (
    Application,
    Attempt,
    InterviewReview,
    ProctoringEvent,
    SiteSettings,
)
from .review import attempt_csv_rows
from .tests_results import ResultsFixture
from .validators import validate_full_name


class SignInUsesOnlyThePasswordHashTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.admin = User.objects.create_user("boss", "boss@example.com", "pw-admin-123", is_staff=True)
        now = timezone.now()
        from .models import Exam

        self.exam = Exam.objects.create(
            title="Hiring", opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=2),
            duration_minutes=30, created_by=self.admin, is_published=True,
        )
        self.user = User.objects.create_user("asha@example.com", "asha@example.com", "real-pass-2345")
        self.application = Application.objects.create(
            candidate=self.user, exam=self.exam, full_name="Asha Rao", status=Application.Status.APPROVED,
            plain_password="Other-Pass-9999",
        )

    def test_plain_text_copy_does_not_sign_in(self):
        response = self.client.post(reverse("login"), {"username": "asha@example.com", "password": "Other-Pass-9999"})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("real-pass-2345"))  # The real password was not overwritten.

    def test_case_changed_password_does_not_sign_in(self):
        self.user.set_password("Other-Pass-9999")
        self.user.save()
        response = self.client.post(reverse("login"), {"username": "asha@example.com", "password": "other-pass-9999"})
        self.assertEqual(response.status_code, 200)

    def test_sending_login_details_makes_the_emailed_password_work(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("staff_resend_credentials", args=[self.application.pk]))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Other-Pass-9999"))
        self.client.logout()
        response = self.client.post(reverse("login"), {"username": "asha@example.com", "password": "Other-Pass-9999"})
        self.assertEqual(response.status_code, 302)


class OneReviewPerRoundTests(ResultsFixture):
    def test_second_review_for_the_same_round_is_refused(self):
        InterviewReview.objects.create(attempt=self.attempts[0], round_number=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            InterviewReview.objects.create(attempt=self.attempts[0], round_number=1)

    def test_round_outside_configured_rounds_is_refused(self):
        response = self.client.post(
            reverse("staff_attempt_decision", args=[self.attempts[0].pk]),
            {"round_number": 3, "decision": "recommended"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(InterviewReview.objects.filter(attempt=self.attempts[0], round_number=3).exists())


class ExportTests(ResultsFixture):
    def test_bad_ids_do_not_crash(self):
        response = self.client.get(reverse("staff_export_csv", args=[self.exam.pk]) + "?ids=abc,../1")
        self.assertEqual(response.status_code, 302)

    def test_export_query_count_does_not_grow_with_candidates(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                list(attempt_csv_rows(self.exam))
            return len(ctx.captured_queries)

        before = count()
        from django.contrib.auth import get_user_model

        User = get_user_model()
        now = timezone.now()
        for n in range(2, 8):
            cand = User.objects.create_user(f"x{n}", f"x{n}@example.com", "pw")
            app = Application.objects.create(candidate=cand, exam=self.exam, full_name=f"X {n}",
                                             status=Application.Status.APPROVED)
            Attempt.objects.create(
                candidate=cand, exam=self.exam, application=app, status=Attempt.Status.SUBMITTED,
                started_at=now, deadline_at=now + timedelta(minutes=30), last_seen_at=now, ended_at=now,
            )
        self.assertEqual(count(), before)

    def test_prefetched_section_scores_match_the_direct_ones(self):
        rows = list(attempt_csv_rows(self.exam))
        cand0 = next(r for r in rows if r["full_name"] == "Cand 0")
        direct = {s["section_id"]: s["score"] for s in services.section_scores(self.attempts[0])}
        for section_id, score in direct.items():
            self.assertEqual(cand0[f"section:{section_id}"], score)


class ResumeUploadTests(TestCase):
    def test_legacy_doc_is_refused(self):
        upload = SimpleUploadedFile("cv.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 100)
        with self.assertRaises(ValidationError):
            validate_resume_file(upload)

    def test_docx_that_expands_too_far_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", "<w:document/>")
            archive.writestr("word/media/big.bin", b"\0" * (61 * 1024 * 1024))
        upload = SimpleUploadedFile("cv.docx", buf.getvalue())
        with self.assertRaises(ValidationError):
            validate_resume_file(upload)

    def test_normal_docx_is_accepted(self):
        import docx

        buf = io.BytesIO()
        document = docx.Document()
        document.add_paragraph("Python developer")
        document.save(buf)
        validate_resume_file(SimpleUploadedFile("cv.docx", buf.getvalue()))


class AITimeBudgetTests(TestCase):
    def test_used_up_budget_stops_before_calling_the_provider(self):
        class Session:
            calls = 0

            def post(self, *args, **kwargs):
                Session.calls += 1
                raise AssertionError("should not be called")

        keys = [ai_client._EnvKey(api_key="k1"), ai_client._EnvKey(api_key="k2")]
        with self.assertRaises(ai_client.AIUnavailable) as ctx:
            ai_client.generate_text("hi", keys=keys, session=Session(), budget_seconds=0)
        self.assertIn("time budget", str(ctx.exception))
        self.assertEqual(Session.calls, 0)

    def test_each_call_gets_at_most_the_remaining_budget(self):
        seen = []

        class Response:
            status_code = 500
            headers = {}
            text = ""

            def json(self):
                return {}

        class Session:
            def post(self, url, timeout=None, **kwargs):
                seen.append(timeout)
                return Response()

        with self.assertRaises(ai_client.AIUnavailable):
            ai_client.generate_text("hi", keys=[ai_client._EnvKey(api_key="k")], session=Session(), budget_seconds=10)
        self.assertLessEqual(seen[0], 10)


class SettingsCacheTests(TestCase):
    @override_settings(SITE_SETTINGS_CACHE_SECONDS=600, SITE_SETTINGS_LOCAL_SECONDS=10)
    def test_cache_is_invalidated_on_save_and_copies_are_independent(self):
        SiteSettings.clear_cache()
        first = SiteSettings.load()
        first.site_name = "Changed in memory only"  # e.g. an invalid form bound to the instance
        self.assertNotEqual(SiteSettings.load().site_name, "Changed in memory only")
        first.site_name = "Saved name"
        first.save()
        self.assertEqual(SiteSettings.load().site_name, "Saved name")
        SiteSettings.clear_cache()

    @override_settings(SITE_SETTINGS_CACHE_SECONDS=600, SITE_SETTINGS_LOCAL_SECONDS=10)
    def test_repeat_loads_in_one_worker_cost_no_queries(self):
        SiteSettings.clear_cache()
        SiteSettings.load()
        with self.assertNumQueries(0):
            SiteSettings.load()
            SiteSettings.load()
        SiteSettings.clear_cache()


class ExpirySweepThrottleTests(TestCase):
    @override_settings(EXPIRE_SWEEP_THROTTLE_SECONDS=30)
    def test_second_sweep_inside_the_window_is_skipped_unless_forced(self):
        from django.core.cache import cache

        cache.delete("expire_due_attempts_lock")
        with patch.object(services.AttemptSection.objects, "filter", wraps=services.AttemptSection.objects.filter) as spy:
            services.expire_due_attempts()
            first = spy.call_count
            services.expire_due_attempts()
            self.assertEqual(spy.call_count, first)  # Throttled: nothing queried.
            services.expire_due_attempts(force=True)
            self.assertGreater(spy.call_count, first)
        cache.delete("expire_due_attempts_lock")


class NameValidatorTests(TestCase):
    def test_names_in_other_scripts_are_accepted(self):
        for name in ["தமிழ் செல்வன்", "రామ కృష్ణ", "অমিত রায়", "ಕನ್ನಡ ಹೆಸರು", "محمد علي", "李小龙", "Seán O'Brien"]:
            self.assertEqual(validate_full_name(name), name)

    def test_symbols_and_digits_only_are_refused(self):
        for name in ["12345", "<b>x</b>", "a_b", "x=y"]:
            with self.assertRaises(ValidationError):
                validate_full_name(name)


class PasteFlagTests(ResultsFixture):
    def setUp(self):
        super().setUp()
        self.attempt = self.attempts[0]
        Attempt.objects.filter(pk=self.attempt.pk).update(status=Attempt.Status.IN_PROGRESS, ended_at=None)
        self.attempt.refresh_from_db()

    def _paste(self, **extra):
        context = {"action": "paste", "chars": 500, **extra}
        return services._create_event(self.attempt, ProctoringEvent.Kind.CLIPBOARD, None, 0, context, timezone.now())

    def test_large_paste_is_flagged(self):
        self.assertTrue(self._paste().over_threshold)

    def test_pasting_the_public_sample_is_recorded_but_not_flagged(self):
        event = self._paste(matches_sample=True)
        self.assertFalse(event.over_threshold)
        self.assertTrue(event.context["matches_sample"])


class InterviewScoreInputTests(TestCase):
    def test_non_finite_ids_and_marks_are_ignored(self):
        class App:
            ai_round_questions = {"1": [{"id": 1, "question": "Q", "max_marks": 10}]}
            ai_interview_questions = []

        rows = interview_rounds.merge_scores(
            App(), 1, 1, [{"id": "inf", "marks_awarded": 5, "answered": True},
                          {"id": 1, "marks_awarded": "nan", "answered": True}]
        )
        self.assertEqual(rows[0]["marks_awarded"], 0.0)


class GradingThreadCapTests(TestCase):
    @override_settings(TESTING=False)
    def test_no_thread_when_all_slots_are_busy(self):
        with patch.object(services._grading_slots, "acquire", return_value=False), \
                patch.object(services.threading, "Thread") as thread:
            services.dispatch_grading(123)
        thread.assert_not_called()
