from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Application,
    Attempt,
    CodingProblem,
    CodingSubmission,
    CodingTestCase,
    Exam,
    ExamSection,
    MCQOption,
    MCQQuestion,
)
from .services import claim_submission, record_readiness
from .tests_engine import TMP_MEDIA

User = get_user_model()


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class CandidateCredentialsAndLifecycleTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin_cred", "admin_cred@example.com", "pw-admin", is_staff=True)
        self.exam = Exam.objects.create(
            title="Cred Exam",
            opens_at=timezone.now() - timedelta(hours=1),
            closes_at=timezone.now() + timedelta(hours=2),
            duration_minutes=60,
            is_published=True,
            created_by=self.staff,
        )
        self.client.force_login(self.staff)

    def test_import_with_name_and_no_phone_succeeds(self):
        resp = self.client.post(
            reverse("staff_candidates_import", args=[self.exam.pk]),
            {"candidates": "raj@example.com, Raj Kumar"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        app = Application.objects.filter(exam=self.exam, candidate__email="raj@example.com").first()
        self.assertIsNotNone(app)
        self.assertEqual(app.full_name, "Raj Kumar")
        self.assertFalse(bool(app.phone))

    def test_import_with_name_and_phone_succeeds(self):
        resp = self.client.post(
            reverse("staff_candidates_import", args=[self.exam.pk]),
            {"candidates": "priya@example.com, Priya Sharma, 9876543210"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        app = Application.objects.filter(exam=self.exam, candidate__email="priya@example.com").first()
        self.assertIsNotNone(app)
        self.assertEqual(app.full_name, "Priya Sharma")
        self.assertEqual(app.phone, "9876543210")

    def test_import_missing_name_rejected_with_row_error(self):
        resp = self.client.post(
            reverse("staff_candidates_import", args=[self.exam.pk]),
            {"candidates": "noname@example.com\ngood@example.com, Good Name"},
            follow=True,
        )
        self.assertContains(resp, "Line 1: Candidate full name is required.")
        self.assertFalse(Application.objects.filter(candidate__email="noname@example.com").exists())

    def test_candidate_password_authenticates_matches_admin_and_email(self):
        self.client.post(
            reverse("staff_candidates_import", args=[self.exam.pk]),
            {"candidates": "authcheck@example.com, Auth Check"},
        )
        app = Application.objects.get(exam=self.exam, candidate__email="authcheck@example.com")
        self.assertTrue(bool(app.plain_password))

        # Admin dashboard GET does not rotate password
        dash_resp = self.client.get(reverse("staff_exam_candidates", args=[self.exam.pk]))
        self.assertEqual(dash_resp.status_code, 200)
        app.refresh_from_db()
        current_pw = app.plain_password

        # Candidate can authenticate directly with that password
        self.client.logout()
        login_resp = self.client.post(
            reverse("login"),
            {"username": "authcheck@example.com", "password": current_pw},
        )
        self.assertRedirects(login_resp, reverse("home"), fetch_redirect_response=False)

        # Admin resend sends the exact same working password
        self.client.force_login(self.staff)
        mail.outbox.clear()
        self.client.post(reverse("staff_send_credentials", args=[self.exam.pk]), {"mode": "all"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(current_pw, mail.outbox[0].body)
        app.refresh_from_db()
        self.assertEqual(app.plain_password, current_pw)

    def test_explicit_regenerate_rotates_password_consistently(self):
        self.client.post(
            reverse("staff_candidates_import", args=[self.exam.pk]),
            {"candidates": "regen@example.com, Regen Candidate"},
        )
        app = Application.objects.get(exam=self.exam, candidate__email="regen@example.com")
        old_pw = app.plain_password

        resp = self.client.post(reverse("staff_regenerate_credentials", args=[app.pk]))
        self.assertEqual(resp.status_code, 302)
        app.refresh_from_db()
        new_pw = app.plain_password
        self.assertNotEqual(old_pw, new_pw)
        self.assertTrue(app.candidate.check_password(new_pw))

        # Candidate logs in with the new password
        self.client.logout()
        login_resp = self.client.post(
            reverse("login"),
            {"username": "regen@example.com", "password": new_pw},
        )
        self.assertRedirects(login_resp, reverse("home"), fetch_redirect_response=False)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class AssessmentReadinessAndTimerTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin_timer", "admin_t@example.com", "pw", is_staff=True)
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="Timer Exam",
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=2),
            duration_minutes=45,
            is_published=True,
            snapshots_enabled=True,
            created_by=self.staff,
        )
        self.sec = ExamSection.objects.create(exam=self.exam, title="MCQ", kind="mcq", order=1, duration_minutes=45)
        self.candidate = User.objects.create_user("cand_timer", "cand_t@example.com", "pw")
        self.app = Application.objects.create(
            candidate=self.candidate, exam=self.exam, full_name="Timer Candidate", status=Application.Status.APPROVED
        )

    def test_attempt_starts_unconfirmed_until_candidate_submits_readiness(self):
        now = timezone.now()
        initial_deadline = min(now + timedelta(minutes=45), self.exam.closes_at)
        attempt = Attempt.objects.create(
            candidate=self.candidate,
            exam=self.exam,
            application=self.app,
            status=Attempt.Status.IN_PROGRESS,
            started_at=now - timedelta(minutes=5),
            deadline_at=initial_deadline,
            last_seen_at=now - timedelta(minutes=5),
            policy_snapshot=self.exam.policy_snapshot(),
        )
        self.assertIsNone(attempt.readiness_confirmed_at)
        self.assertIsNone(attempt.snapshot_consent_at)

        # Confirm readiness
        record_readiness(attempt, now, snapshot_consent=True)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.readiness_confirmed_at)
        self.assertEqual(attempt.started_at, now)
        self.assertEqual(attempt.deadline_at, min(now + timedelta(minutes=45), self.exam.closes_at))
        first_started = attempt.started_at
        first_deadline = attempt.deadline_at

        # Repeated readiness calls are idempotent and do not reset timer
        record_readiness(attempt, now + timedelta(minutes=10), snapshot_consent=True)
        attempt.refresh_from_db()
        self.assertEqual(attempt.started_at, first_started)
        self.assertEqual(attempt.deadline_at, first_deadline)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class AssessmentContentImmutabilityTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin_imm", "admin_imm@example.com", "pw", is_staff=True)
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="Immutable Exam",
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=2),
            duration_minutes=60,
            is_published=True,
            created_by=self.staff,
        )
        self.sec = ExamSection.objects.create(exam=self.exam, title="Section 1", kind="mcq", order=1, duration_minutes=30)
        self.q = MCQQuestion.objects.create(exam=self.exam, section=self.sec, text="2+2=?", marks=2, order=1)
        self.opt1 = MCQOption.objects.create(question=self.q, text="4", is_correct=True, order=1)
        self.opt2 = MCQOption.objects.create(question=self.q, text="5", is_correct=False, order=2)
        self.problem = CodingProblem.objects.create(exam=self.exam, title="Add Problem", statement="Add 2 nums", languages="python", marks=10, order=1)
        self.case = CodingTestCase.objects.create(problem=self.problem, input_data="1 2", expected_output="3", order=1)

        self.candidate = User.objects.create_user("cand_imm", "cand_imm@example.com", "pw")
        self.app = Application.objects.create(candidate=self.candidate, exam=self.exam, full_name="Candidate Imm", status=Application.Status.APPROVED)
        self.attempt = Attempt.objects.create(
            candidate=self.candidate,
            exam=self.exam,
            application=self.app,
            status=Attempt.Status.IN_PROGRESS,
            started_at=now,
            deadline_at=now + timedelta(minutes=60),
            last_seen_at=now,
            policy_snapshot=self.exam.policy_snapshot(),
        )
        self.client.force_login(self.staff)

    def test_section_modification_blocked_when_attempts_exist(self):
        resp_add = self.client.post(reverse("staff_section_add", args=[self.exam.pk]), {"section-title": "New Sec", "section-order": 2, "section-duration_minutes": 10, "section-kind": "mcq"})
        self.assertEqual(resp_add.status_code, 302)
        self.assertEqual(self.exam.sections.count(), 1)

        resp_edit = self.client.post(reverse("staff_section_edit", args=[self.sec.pk]), {"title": "Edited", "order": 1, "duration_minutes": 45, "kind": "mcq"})
        self.assertEqual(resp_edit.status_code, 302)
        self.sec.refresh_from_db()
        self.assertEqual(self.sec.title, "Section 1")

        resp_del = self.client.post(reverse("staff_section_delete", args=[self.sec.pk]))
        self.assertEqual(resp_del.status_code, 302)
        self.assertTrue(ExamSection.objects.filter(pk=self.sec.pk).exists())

    def test_mcq_modification_blocked_when_attempts_exist(self):
        resp_edit = self.client.post(reverse("staff_mcq_edit", args=[self.q.pk]), {"text": "Modified Question?"})
        self.assertEqual(resp_edit.status_code, 302)
        self.q.refresh_from_db()
        self.assertEqual(self.q.text, "2+2=?")

        resp_del = self.client.post(reverse("staff_mcq_delete", args=[self.q.pk]))
        self.assertEqual(resp_del.status_code, 302)
        self.assertTrue(MCQQuestion.objects.filter(pk=self.q.pk).exists())

    def test_coding_modification_blocked_when_attempts_exist(self):
        resp_edit = self.client.post(reverse("staff_coding_edit", args=[self.problem.pk]), {"title": "Modified Title"})
        self.assertEqual(resp_edit.status_code, 302)
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.title, "Add Problem")

        resp_del = self.client.post(reverse("staff_coding_delete", args=[self.problem.pk]))
        self.assertEqual(resp_del.status_code, 302)
        self.assertTrue(CodingProblem.objects.filter(pk=self.problem.pk).exists())


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class CodingGradingConcurrencyTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin_grad", "admin_grad@example.com", "pw", is_staff=True)
        now = timezone.now()
        self.exam = Exam.objects.create(title="Grading Exam", opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=2), duration_minutes=60, is_published=True, created_by=self.staff)
        self.sec = ExamSection.objects.create(exam=self.exam, title="Coding", kind="coding", order=1, duration_minutes=60, coding_selection_count=1)
        self.problem = CodingProblem.objects.create(exam=self.exam, title="Sum", statement="Sum", languages="python", marks=10, order=1)
        CodingTestCase.objects.create(problem=self.problem, input_data="1 2", expected_output="3", order=1)
        CodingTestCase.objects.create(problem=self.problem, input_data="4 5", expected_output="9", is_hidden=True, order=2)
        self.candidate = User.objects.create_user("cand_grad", "cand_grad@example.com", "pw")
        self.app = Application.objects.create(candidate=self.candidate, exam=self.exam, full_name="Candidate Grad", status=Application.Status.APPROVED)
        self.attempt = Attempt.objects.create(
            candidate=self.candidate,
            exam=self.exam,
            application=self.app,
            status=Attempt.Status.IN_PROGRESS,
            started_at=now,
            deadline_at=now + timedelta(minutes=60),
            last_seen_at=now,
            policy_snapshot=self.exam.policy_snapshot(),
        )

    def test_claim_submission_atomic_and_concurrency_safe(self):
        sub = CodingSubmission.objects.create(
            attempt=self.attempt,
            problem=self.problem,
            version=1,
            language="python",
            source_code="print(sum(map(int, input().split())))",
            status=CodingSubmission.Status.QUEUED,
        )
        # Worker 1 claims
        claimed_w1 = claim_submission(sub.pk, worker_id="worker-1")
        self.assertTrue(claimed_w1)
        sub.refresh_from_db()
        self.assertEqual(sub.status, CodingSubmission.Status.RUNNING)
        self.assertEqual(sub.claimed_by, "worker-1")

        # Worker 2 tries to claim same submission while RUNNING -> rejected
        claimed_w2 = claim_submission(sub.pk, worker_id="worker-2")
        self.assertFalse(claimed_w2)

    def test_stale_running_job_recovered_after_crash(self):
        sub = CodingSubmission.objects.create(
            attempt=self.attempt,
            problem=self.problem,
            version=1,
            language="python",
            source_code="print(3)",
            status=CodingSubmission.Status.RUNNING,
            claimed_by="crashed-worker",
            claimed_at=timezone.now() - timedelta(minutes=5),  # Stale > 3 mins
        )
        # New worker claims stale job
        claimed = claim_submission(sub.pk, worker_id="recovered-worker")
        self.assertTrue(claimed)
        sub.refresh_from_db()
        self.assertEqual(sub.claimed_by, "recovered-worker")


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class SecurityAndOperationsHardeningTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin_sec", "admin_sec@example.com", "pw", is_staff=True)
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="Sec Exam", opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=2),
            duration_minutes=60, is_published=True, created_by=self.staff, interview_rounds_count=1
        )
        self.client.force_login(self.staff)

    def test_interview_rounds_update_rejects_external_redirect(self):
        # Open redirect target
        resp = self.client.post(
            reverse("staff_exam_update_rounds", args=[self.exam.pk]),
            {"interview_rounds_count": "2", "next": "https://attacker.example/phish"},
        )
        self.assertEqual(resp.status_code, 302)
        # Redirected to safe internal candidate list, not external attacker URL
        self.assertEqual(resp.url, reverse("staff_exam_candidates", args=[self.exam.pk]))
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.interview_rounds_count, 2)

    def test_client_ip_does_not_trust_untrusted_forwarded_for(self):
        from django.test import RequestFactory

        from assessments.exam_api import _client_ip
        rf = RequestFactory()

        # Direct client with spoofed X-Forwarded-For
        req = rf.get("/api/state/", HTTP_X_FORWARDED_FOR="1.2.3.4", REMOTE_ADDR="198.51.100.5")
        ip = _client_ip(req)
        # Since 198.51.100.5 is not in TRUSTED_PROXIES, REMOTE_ADDR is returned
        self.assertEqual(ip, "198.51.100.5")

        # Request from trusted proxy
        req_trusted = rf.get("/api/state/", HTTP_X_FORWARDED_FOR="203.0.113.19", REMOTE_ADDR="127.0.0.1")
        ip_trusted = _client_ip(req_trusted)
        self.assertEqual(ip_trusted, "203.0.113.19")

    def test_readiness_probe_cleans_file_and_caches_result(self):
        resp = self.client.get(reverse("readiness_check"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("status"), "ready")
        self.assertEqual(data.get("dependencies", {}).get("storage"), "ok")
        self.assertEqual(data.get("dependencies", {}).get("database"), "ok")
