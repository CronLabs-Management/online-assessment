"""
Tests for the candidate access and test-taking flow as the administrator and candidates see it:
import, login email and resend, pre-publish checks, Run and Submit, retention, and page rendering.
"""

import json
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import services
from .models import (
    Application,
    Attempt,
    AttemptCoding,
    CodingProblem,
    CodingSubmission,
    CodingTestCase,
    Exam,
    ExamSection,
    MCQOption,
    MCQQuestion,
    ProctoringSnapshot,
    SiteSettings,
)
from .services import perform
from .staff_views import SEND_BATCH
from .tests_engine import TMP_MEDIA, EngineFixture, FakeRunner

User = get_user_model()
FAST_HASHER = ["django.contrib.auth.hashers.MD5PasswordHasher"]
# The generated password format: four-character groups from the same alphabet notifications.py uses.
PASSWORD_RE = re.compile(r"\b[a-hjkm-np-z2-9]{4}-[a-hjkm-np-z2-9]{4}-[a-hjkm-np-z2-9]{4}\b")


def password_in(message):
    match = PASSWORD_RE.search(message.body)
    assert match, "no password found in the login email"
    return match.group(0)


def make_exam(staff, *, published=False, title="Hiring test"):
    now = timezone.now()
    return Exam.objects.create(
        title=title,
        opens_at=now - timedelta(hours=1),
        closes_at=now + timedelta(hours=2),
        duration_minutes=30,
        is_published=published,
        created_by=staff,
        camera_required=False,
    )


@override_settings(PASSWORD_HASHERS=FAST_HASHER)
class CredentialFlowTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin", "admin@example.com", "pw-12345-x", is_staff=True)
        self.exam = make_exam(self.staff, published=True)
        self.client.force_login(self.staff)

    def _import(self, text):
        return self.client.post(reverse("staff_candidates_import", args=[self.exam.pk]), {"candidates": text})

    def _application(self, email):
        return Application.objects.get(candidate__email=email, exam=self.exam)

    def _send(self, application):
        return self.client.post(reverse("staff_resend_credentials", args=[application.pk]))

    def test_import_adds_candidates_without_emailing_them(self):
        self._import("asha@example.com, Asha Rao, 9876543210\nravi@example.com")
        application = self._application("asha@example.com")
        self.assertEqual(application.status, Application.Status.APPROVED)
        self.assertEqual(application.full_name, "Asha Rao")
        self.assertIsNone(application.credentials_sent_at)
        self.assertFalse(application.candidate.has_usable_password())
        self.assertEqual(len(mail.outbox), 0)

    def test_importing_the_same_person_twice_does_not_duplicate(self):
        self._import("asha@example.com, Asha Rao")
        self._import("asha@example.com, Asha Rao")
        self.assertEqual(Application.objects.filter(exam=self.exam).count(), 1)

    def test_login_email_contains_a_password_that_works(self):
        self._import("asha@example.com, Asha Rao")
        application = self._application("asha@example.com")
        self._send(application)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["asha@example.com"])
        application.refresh_from_db()
        application.candidate.refresh_from_db()
        self.assertIsNotNone(application.credentials_sent_at)
        self.assertTrue(application.candidate.check_password(password_in(mail.outbox[0])))

    def test_resend_replaces_the_old_password(self):
        self._import("asha@example.com, Asha Rao")
        application = self._application("asha@example.com")
        self._send(application)
        first = password_in(mail.outbox[0])
        self._send(application)
        second = password_in(mail.outbox[1])
        application.candidate.refresh_from_db()
        self.assertNotEqual(first, second)
        self.assertFalse(application.candidate.check_password(first))
        self.assertTrue(application.candidate.check_password(second))

    def test_failed_send_keeps_the_previous_password(self):
        self._import("asha@example.com, Asha Rao")
        application = self._application("asha@example.com")
        self._send(application)
        first = password_in(mail.outbox[0])
        with mock.patch("assessments.notifications.send_mail", side_effect=OSError("smtp down")):
            self._send(application)
        application.candidate.refresh_from_db()
        self.assertTrue(application.candidate.check_password(first))
        self.assertEqual(len(mail.outbox), 1)

    def test_bulk_send_is_capped_per_click(self):
        self._import("\n".join(f"cand{i}@example.com" for i in range(SEND_BATCH + 5)))
        self.client.post(reverse("staff_send_credentials", args=[self.exam.pk]), {"mode": "unsent"})
        self.assertEqual(len(mail.outbox), SEND_BATCH)
        self.assertEqual(Application.objects.filter(exam=self.exam, credentials_sent_at__isnull=True).count(), 5)

    def test_revoke_and_restore_access(self):
        self._import("asha@example.com, Asha Rao")
        application = self._application("asha@example.com")
        self.client.post(reverse("staff_access", args=[application.pk]), {"action": "revoke", "reason": "Left the list"})
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.REVOKED)
        self.assertEqual(application.decision_reason, "Left the list")
        self.assertEqual(application.changed_by, self.staff)

        self.client.post(reverse("staff_access", args=[application.pk]), {"action": "restore"})
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.APPROVED)

    def test_revoked_candidate_cannot_receive_a_new_password(self):
        self._import("asha@example.com, Asha Rao")
        application = self._application("asha@example.com")
        self.client.post(reverse("staff_access", args=[application.pk]), {"action": "revoke"})
        self._send(application)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(PASSWORD_HASHERS=FAST_HASHER)
class CandidateSignInTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin", "admin@example.com", "pw-12345-x", is_staff=True)
        self.exam = make_exam(self.staff, published=True)
        self.candidate = User.objects.create_user("asha@example.com", "asha@example.com", "abcd-efgh-2345")
        self.application = Application.objects.create(
            candidate=self.candidate, exam=self.exam, full_name="Asha Rao", status=Application.Status.APPROVED
        )

    def test_signs_in_with_email_and_password(self):
        response = self.client.post(reverse("login"), {"username": "asha@example.com", "password": "abcd-efgh-2345"})
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)

    def test_email_is_matched_regardless_of_case(self):
        response = self.client.post(reverse("login"), {"username": "ASHA@Example.com", "password": "abcd-efgh-2345"})
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)

    def test_wrong_password_explains_what_to_check(self):
        response = self.client.post(reverse("login"), {"username": "asha@example.com", "password": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Those details don")

    def test_start_is_blocked_until_details_are_confirmed(self):
        self.client.force_login(self.candidate)
        response = self.client.post(reverse("start_attempt", args=[self.exam.pk]))
        self.assertRedirects(response, reverse("confirm_details", args=[self.exam.pk]), fetch_redirect_response=False)
        self.assertFalse(Attempt.objects.filter(candidate=self.candidate).exists())

    def test_confirming_details_saves_them(self):
        self.client.force_login(self.candidate)
        resume = SimpleUploadedFile("resume.pdf", b"%PDF-1.4 dummy", content_type="application/pdf")
        self.client.post(
            reverse("confirm_details", args=[self.exam.pk]),
            {
                "full_name": "Asha R. Rao",
                "email": "asha@example.com",
                "phone": "9876543210",
                "resume": resume,
            },
        )
        self.application.refresh_from_db()
        self.assertEqual(self.application.full_name, "Asha R. Rao")
        self.assertEqual(self.application.phone, "9876543210")
        self.assertTrue(bool(self.application.resume))
        self.assertIsNotNone(self.application.details_confirmed_at)


class PublishGateTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin", "admin@example.com", "pw-12345-x", is_staff=True)
        self.client.force_login(self.staff)

    def _complete_exam(self):
        exam = make_exam(self.staff)
        ExamSection.objects.create(exam=exam, title="MCQ", kind="mcq", order=1, duration_minutes=10, question_count=1)
        ExamSection.objects.create(
            exam=exam, title="Code", kind="coding", order=2, duration_minutes=15, coding_selection_count=1
        )
        question = MCQQuestion.objects.create(exam=exam, text="2+2?", marks=2, order=1)
        MCQOption.objects.create(question=question, text="4", is_correct=True, order=1)
        MCQOption.objects.create(question=question, text="5", is_correct=False, order=2)
        problem = CodingProblem.objects.create(
            exam=exam, title="Add", statement="Add two numbers.", languages="python", marks=10, order=1
        )
        CodingTestCase.objects.create(problem=problem, input_data="1 2", expected_output="3", order=1)
        return exam

    @staticmethod
    def _blocking_failures(exam):
        return [c["text"] for c in services.publish_checks(exam) if c["blocking"] and not c["ok"]]

    def test_complete_test_passes_every_blocking_check(self):
        self.assertEqual(self._blocking_failures(self._complete_exam()), [])

    def test_empty_test_cannot_be_published(self):
        exam = make_exam(self.staff)
        self.assertTrue(self._blocking_failures(exam))
        self.client.post(reverse("staff_exam_publish", args=[exam.pk]))
        exam.refresh_from_db()
        self.assertFalse(exam.is_published)

    def test_question_with_two_correct_answers_blocks_publishing(self):
        exam = self._complete_exam()
        question = exam.mcq_questions.get()
        MCQOption.objects.filter(question=question, text="5").update(is_correct=True)
        self.assertTrue(self._blocking_failures(exam))
        self.client.post(reverse("staff_exam_publish", args=[exam.pk]))
        exam.refresh_from_db()
        self.assertFalse(exam.is_published)

    def test_publish_then_unpublish(self):
        exam = self._complete_exam()
        self.client.post(reverse("staff_exam_publish", args=[exam.pk]))
        exam.refresh_from_db()
        self.assertTrue(exam.is_published)
        self.client.post(reverse("staff_exam_unpublish", args=[exam.pk]))
        exam.refresh_from_db()
        self.assertFalse(exam.is_published)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class RunAndSubmitTests(EngineFixture):
    def _begin_coding(self):
        """Starts the attempt, confirms readiness and finishes the MCQ section so the coding section is open."""
        attempt = self.start()
        now = timezone.now()
        perform(self.candidate, attempt.pk, lambda a, t: services.record_readiness(a, t, snapshot_consent=False))
        perform(self.candidate, attempt.pk, lambda a, t: services.submit_section(a, t, self.mcq_section.pk))
        return attempt, now

    def _assigned_problem_id(self, attempt):
        return AttemptCoding.objects.filter(attempt=attempt).order_by("pk").first().problem_id

    def test_run_reports_hidden_counts_and_stores_nothing(self):
        attempt, _ = self._begin_coding()
        problem_id = self._assigned_problem_id(attempt)
        runner = FakeRunner(outputs={"1 2": "3", "5 5": "10", "9 9": "0"})
        before = CodingSubmission.objects.count()

        def run(a, now):
            problem = services.run_preflight(a, now, problem_id, "python", "print(1)")
            return services.run_results(problem, "python", "print(1)", runner=runner)

        result = perform(self.candidate, attempt.pk, run)
        self.assertEqual(CodingSubmission.objects.count(), before)
        self.assertEqual([row["passed"] for row in result["public"]], [True, True])
        self.assertEqual(set(result["hidden"]), {"passed", "total"})
        self.assertEqual(result["hidden"]["total"], 1)
        self.assertEqual(result["hidden"]["passed"], 0)
        # Hidden case inputs and outputs never leave the server.
        self.assertNotIn("9 9", json.dumps(result))

    def test_submit_locks_the_problem_and_hides_results(self):
        attempt, _ = self._begin_coding()
        problem_id = self._assigned_problem_id(attempt)
        submission = perform(
            self.candidate, attempt.pk, lambda a, now: services.submit_code(a, now, problem_id, "python", "print(1)")
        )
        self.assertTrue(submission.is_final)
        self.assertTrue(services.problem_locked(attempt, problem_id))
        with self.assertRaises(services.AttemptError) as ctx:
            perform(
                self.candidate, attempt.pk, lambda a, now: services.save_draft(a, now, problem_id, "python", "x")
            )
        self.assertEqual(ctx.exception.code, "locked")
        summary = services._public_submission_summary(submission)
        self.assertTrue(summary["final"])
        self.assertEqual(set(summary), {"version", "language", "submitted_at", "final"})


@override_settings(PASSWORD_HASHERS=FAST_HASHER, PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class RetentionTests(EngineFixture):
    def _snapshot(self, name, age_days):
        snapshot = ProctoringSnapshot.objects.create(
            attempt=self.start(), relative_path=name, sha256="0" * 64, size_bytes=1, content_type="image/jpeg"
        )
        ProctoringSnapshot.objects.filter(pk=snapshot.pk).update(received_at=timezone.now() - timedelta(days=age_days))
        return snapshot

    def test_default_retention_is_sixty_days(self):
        self.assertEqual(SiteSettings.load().snapshot_retention_days, 60)

    def test_purge_removes_only_evidence_older_than_the_setting(self):
        old = self._snapshot("a/old.jpg", 61)
        recent = self._snapshot("a/recent.jpg", 59)
        call_command("purge_expired_snapshots", stdout=mock.Mock())
        self.assertFalse(ProctoringSnapshot.objects.filter(pk=old.pk).exists())
        self.assertTrue(ProctoringSnapshot.objects.filter(pk=recent.pk).exists())

    def test_changing_the_setting_changes_what_is_purged(self):
        self.client.force_login(self.staff)
        self.client.post(reverse("staff_settings"), {"snapshot_retention_days": 90})
        snapshot = self._snapshot("a/kept.jpg", 75)
        call_command("purge_expired_snapshots", stdout=mock.Mock())
        self.assertTrue(ProctoringSnapshot.objects.filter(pk=snapshot.pk).exists())

    def test_settings_reject_out_of_range_values(self):
        self.client.force_login(self.staff)
        self.client.post(reverse("staff_settings"), {"snapshot_retention_days": 0})
        self.assertEqual(SiteSettings.load().snapshot_retention_days, 60)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class PageRenderTests(EngineFixture):
    def test_anonymous_visitor_sees_sign_in_guidance(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Sign in with the email address you were given")

    def test_login_page_asks_for_email(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Use the email address you were given")

    def test_candidate_pages_render(self):
        self.client.force_login(self.candidate)
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        self.assertEqual(self.client.get(reverse("exam_detail", args=[self.exam.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("confirm_details", args=[self.exam.pk])).status_code, 200)

    def test_staff_pages_render(self):
        self.client.force_login(self.staff)
        pk = self.exam.pk
        for name, args in [
            ("staff_exam_list", []),
            ("staff_exam_create", []),
            ("staff_exam_edit", [pk]),
            ("staff_exam_detail", [pk]),
            ("staff_exam_overview", [pk]),
            ("staff_exam_candidates", [pk]),
            ("staff_candidates_import", [pk]),
            ("staff_settings", []),
            ("staff_audit_log", []),
        ]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)

    def test_staff_test_page_shows_the_checklist(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("staff_exam_detail", args=[self.exam.pk]))
        self.assertContains(response, "Before you publish")
        self.assertContains(response, "Unpublish test")
