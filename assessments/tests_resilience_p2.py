from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.utils import timezone

from assessments import services
from assessments.models import (
    Application,
    Attempt,
    CodingProblem,
    CodingSubmission,
    CodingTestCase,
    CodingTestResult,
    Exam,
    ExamSection,
    ProctoringEvent,
    ProctoringSnapshot,
    StaffProfile,
    StaffRole,
)
from assessments.team_views import team_member_edit

User = get_user_model()


class ResiliencePhase2Tests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.author = User.objects.create_user(
            username="author@example.com",
            email="author@example.com",
            password="testpassword123",
        )
        self.exam = Exam.objects.create(
            title="Resilience Test Exam",
            created_by=self.author,
            opens_at=timezone.now() - timezone.timedelta(hours=1),
            closes_at=timezone.now() + timezone.timedelta(hours=2),
            duration_minutes=60,
            is_published=True,
        )
        self.section = ExamSection.objects.create(
            exam=self.exam,
            title="Coding Section",
            kind=ExamSection.Kind.CODING,
            duration_minutes=30,
            order=1,
        )
        self.problem = CodingProblem.objects.create(
            exam=self.exam,
            title="Sample Sum",
            statement="Sum two numbers",
            languages="python",
            marks=10,
        )
        self.case1 = CodingTestCase.objects.create(
            problem=self.problem,
            order=1,
            input_data="1 2",
            expected_output="3",
            is_hidden=False,
        )
        self.case2 = CodingTestCase.objects.create(
            problem=self.problem,
            order=2,
            input_data="5 5",
            expected_output="10",
            is_hidden=True,
        )
        self.candidate = User.objects.create_user(
            username="candidate@example.com",
            email="candidate@example.com",
            password="testpassword123",
        )
        self.application = Application.objects.create(
            candidate=self.candidate,
            exam=self.exam,
            full_name="Candidate One",
            status=Application.Status.APPROVED,
            details_confirmed_at=timezone.now(),
            monitoring_consent_at=timezone.now(),
        )

    def test_null_byte_sanitization(self):
        """Sanitizer strips null bytes from code outputs and draft strings to prevent Postgres UTF-8 crashes."""
        raw_output = "Hello\x00World\x00!"
        cleaned = services.sanitize_string(raw_output)
        self.assertEqual(cleaned, "HelloWorld!")
        self.assertNotIn("\x00", cleaned)

        # Test byte strings
        byte_output = b"Data\x00Stream"
        self.assertEqual(services.sanitize_string(byte_output), "DataStream")

    def test_grade_submission_bulk_create_and_sanitization(self):
        """grade_submission uses bulk_create and strips null bytes from runner outputs."""
        submission = CodingSubmission.objects.create(
            attempt=Attempt.objects.create(
                candidate=self.candidate,
                exam=self.exam,
                application=self.application,
                started_at=timezone.now(),
                deadline_at=timezone.now() + timezone.timedelta(minutes=30),
                last_seen_at=timezone.now(),
            ),
            problem=self.problem,
            version=1,
            language="python",
            source_code="print(3)",
            status=CodingSubmission.Status.QUEUED,
        )

        # Mock runner outcome with null bytes in stdout
        mock_outcome1 = MagicMock(status="passed", stdout="3\x00", compile_output="", time_ms=15, memory_kb=2048)
        mock_outcome2 = MagicMock(status="passed", stdout="10\x00", compile_output="", time_ms=18, memory_kb=2048)

        with patch("assessments.services.execute_cases") as mock_exec:
            mock_exec.return_value = [
                (self.case1, CodingTestResult.Status.PASSED, mock_outcome1),
                (self.case2, CodingTestResult.Status.PASSED, mock_outcome2),
            ]
            graded = services.grade_submission(submission, runner=MagicMock())

        self.assertEqual(graded.status, CodingSubmission.Status.COMPLETED)
        self.assertEqual(graded.public_passed, 1)
        self.assertEqual(graded.hidden_passed, 1)

        results = list(CodingTestResult.objects.filter(submission=submission).order_by("test_case__order"))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].stdout_preview, "3")
        self.assertEqual(results[1].stdout_preview, "10")
        self.assertNotIn("\x00", results[0].stdout_preview)
        self.assertNotIn("\x00", results[1].stdout_preview)

    def test_proctoring_event_sequence_conflict_retry(self):
        """Simultaneous events with sequence race condition retry cleanly instead of throwing 500 IntegrityError."""
        attempt = Attempt.objects.create(
            candidate=self.candidate,
            exam=self.exam,
            application=self.application,
            started_at=timezone.now(),
            deadline_at=timezone.now() + timezone.timedelta(minutes=30),
            last_seen_at=timezone.now(),
            policy_snapshot={"tab_switch_threshold": 3, "missing_face_seconds": 5},
        )

        now = timezone.now()
        # Seed an existing event
        event1 = services._create_event(attempt, ProctoringEvent.Kind.TAB_HIDDEN, now, 100, {}, now)
        self.assertEqual(event1.sequence, 1)

        # External concurrent transaction inserted sequence 2
        ProctoringEvent.objects.create(
            attempt=attempt,
            kind=ProctoringEvent.Kind.WINDOW_BLUR,
            sequence=2,
            client_at=now,
            server_at=now,
            duration_ms=50,
            context={},
            over_threshold=False,
        )

        # Stale read on first attempt causes duplicate sequence 2 collision, retry picks up sequence 3
        orig_aggregate = attempt.proctoring_events.aggregate
        call_count = [0]

        def stale_then_fresh_max(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"m": 1}  # Stale read
            return orig_aggregate(*args, **kwargs)

        with patch.object(attempt.proctoring_events, "aggregate", side_effect=stale_then_fresh_max):
            event3 = services._create_event(attempt, ProctoringEvent.Kind.VOICE_DETECTED, now, 50, {}, now)

        self.assertEqual(event3.sequence, 3)
        self.assertEqual(ProctoringEvent.objects.filter(attempt=attempt).count(), 3)

    def test_purge_expired_snapshots_removes_empty_directories(self):
        """purge_expired_snapshots unlinks expired image and cleans up the empty attempt subdirectory."""
        attempt = Attempt.objects.create(
            candidate=self.candidate,
            exam=self.exam,
            application=self.application,
            started_at=timezone.now(),
            deadline_at=timezone.now() + timezone.timedelta(minutes=30),
            last_seen_at=timezone.now(),
            snapshot_consent_at=timezone.now(),
        )
        root = services._snapshot_root()
        attempt_dir = root / str(attempt.pk)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        img_file = attempt_dir / "test_snapshot.jpg"
        img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

        snapshot = ProctoringSnapshot.objects.create(
            attempt=attempt,
            relative_path=f"{attempt.pk}/test_snapshot.jpg",
            sha256="dummy_hash",
            size_bytes=24,
            content_type="image/jpeg",
        )
        # Backdate received_at to older than retention
        ProctoringSnapshot.objects.filter(pk=snapshot.pk).update(
            received_at=timezone.now() - timezone.timedelta(days=120)
        )

        call_command("purge_expired_snapshots")

        self.assertFalse(img_file.exists())
        self.assertFalse(attempt_dir.exists())

    def test_admin_self_deactivation_prevented(self):
        """An administrator cannot deactivate their own account or strip their own team.manage permission."""
        role_admin = StaffRole.objects.create(name="Super Admin", permissions=["team.manage", "tests.manage"])
        role_limited = StaffRole.objects.create(name="Reviewer Only", permissions=["results.view"])

        admin_user = User.objects.create_user(
            username="admin@company.test",
            email="admin@company.test",
            password="adminpassword123",
            first_name="Admin",
            last_name="User",
        )
        admin_profile = StaffProfile.objects.create(user=admin_user, role=role_admin, is_active=True)

        # 1. Attempt self-deactivation
        req_deactivate = self.factory.post(
            f"/staff/team/members/{admin_profile.pk}/",
            {"full_name": "Admin User", "title": "Lead", "role": role_admin.pk, "is_active": ""},
        )
        req_deactivate.user = admin_user
        resp1 = team_member_edit(req_deactivate, admin_profile.pk)
        self.assertEqual(resp1.status_code, 200)  # Form shown again with the error.
        admin_profile.refresh_from_db()
        self.assertTrue(admin_profile.is_active)  # Must remain active!

        # 2. Attempt demotion (removing team.manage)
        req_demote = self.factory.post(
            f"/staff/team/members/{admin_profile.pk}/",
            {"full_name": "Admin User", "title": "Lead", "role": role_limited.pk, "is_active": "on"},
        )
        req_demote.user = admin_user
        resp2 = team_member_edit(req_demote, admin_profile.pk)
        self.assertEqual(resp2.status_code, 200)
        admin_profile.refresh_from_db()
        self.assertEqual(admin_profile.role, role_admin)  # Must remain admin!

    def test_candidate_import_username_truncation_for_rfc5321(self):
        """Candidate import clamps usernames to 150 characters for long RFC 5321 emails."""
        # 160-character email
        local_part = "a" * 145
        long_email = f"{local_part}@verylongdomain.com"
        self.assertGreater(len(long_email), 150)

        rows = [{"email": long_email, "full_name": "Long Name", "phone": "9876543210"}]
        with patch("assessments.staff_views.CandidateImportForm.parse", return_value=(rows, [])):
            from assessments.staff_views import staff_candidates_import
            req = self.factory.post(f"/staff/exams/{self.exam.pk}/candidates/import/", {"candidates": "dummy"})
            admin = User.objects.create_superuser("root", "root@test.com", "pass")
            req.user = admin
            req._messages = MagicMock()
            staff_candidates_import(req, self.exam.pk)

        created_user = User.objects.filter(email=long_email.lower()).first()
        self.assertIsNotNone(created_user)
        self.assertLessEqual(len(created_user.username), 150)
        self.assertEqual(created_user.username, long_email[:150].lower())
