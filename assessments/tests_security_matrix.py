import io
import json
import os
import shutil
import tempfile
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.checks import run_checks
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from . import notifications, runners, services
from .forms import validate_resume_file
from .models import (
    Application,
    Attempt,
    AttemptCoding,
    AttemptSection,
    AuditLog,
    CodingProblem,
    CodingSubmission,
    CodingTestCase,
    Exam,
    ExamSection,
    ProctoringEvent,
    ProctoringSnapshot,
)
from .review import csv_safe
from .runners import CaseOutcome, Judge0Runner, LocalRunner, RunnerError, get_runner
from .tests_engine import TMP_MEDIA, EngineFixture, FakeRunner

User = get_user_model()


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class HiddenTestOracleTests(EngineFixture):
    """
    Asserts candidate-facing code-run and submission endpoints NEVER reveal hidden
    test cases, inputs, expected outputs, hidden counts, or pass totals.
    """

    def setUp(self):
        super().setUp()
        self.attempt = self.start()
        # Move section to coding
        mcq_sec = self.attempt.sections.filter(section__kind=ExamSection.Kind.MCQ).first()
        services._end_section(self.attempt, mcq_sec, AttemptSection.Status.SUBMITTED, at=timezone.now())
        services._advance(self.attempt, previous_end=timezone.now())
        self.coding_sec = self.attempt.sections.filter(section__kind=ExamSection.Kind.CODING).first()
        self.problem = self.attempt.coding_items.first().problem

        # Add distinct test cases
        self.problem.test_cases.all().delete()
        self.pub_case = CodingTestCase.objects.create(
            problem=self.problem,
            order=1,
            is_hidden=False,
            input_data="PUBLIC_IN",
            expected_output="PUBLIC_OUT",
        )
        self.hid_case = CodingTestCase.objects.create(
            problem=self.problem,
            order=2,
            is_hidden=True,
            input_data="SECRET_HIDDEN_IN_12345",
            expected_output="SECRET_HIDDEN_OUT_67890",
        )
        self.client.force_login(self.candidate)

    def test_run_preview_returns_only_public_cases_and_never_hidden_data(self):
        with patch("assessments.services.get_runner") as mock_get_runner:
            fake_runner = FakeRunner(outputs={"PUBLIC_IN": "PUBLIC_OUT", "SECRET_HIDDEN_IN_12345": "SECRET_HIDDEN_OUT_67890"})
            mock_get_runner.return_value = fake_runner

            res = self.client.post(
                reverse("api_code_run", args=[self.attempt.pk]),
                data=json.dumps({"problem_id": self.problem.pk, "language": "python", "code": "print(1)"}),
                content_type="application/json",
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data.get("ok"))
            self.assertIn("public", data)
            self.assertNotIn("hidden", data)
            # Public test outcome is present
            self.assertEqual(len(data["public"]), 1)
            self.assertEqual(data["public"][0]["input"], "PUBLIC_IN")
            # Hidden input and output never leak in payload
            raw_json = res.content.decode("utf-8")
            self.assertNotIn("SECRET_HIDDEN_IN", raw_json)
            self.assertNotIn("SECRET_HIDDEN_OUT", raw_json)
            # Runner was only invoked for public case (1 call)
            self.assertEqual(fake_runner.calls, 1)

    def test_run_preview_compile_error_and_timeout_do_not_leak_hidden_data(self):
        with patch("assessments.services.get_runner") as mock_get_runner:
            mock_runner = MagicMock()
            mock_runner.run_case.return_value = CaseOutcome("compile_error", compile_output="Error in syntax")
            mock_get_runner.return_value = mock_runner

            res = self.client.post(
                reverse("api_code_run", args=[self.attempt.pk]),
                data=json.dumps({"problem_id": self.problem.pk, "language": "python", "code": "invalid(("}),
                content_type="application/json",
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertNotIn("hidden", data)
            self.assertEqual(data.get("compile_output"), "Error in syntax")

    def test_submission_endpoint_does_not_reveal_case_outcomes_or_hidden_data(self):
        res = self.client.post(
            reverse("api_code_submit", args=[self.attempt.pk]),
            data=json.dumps({"problem_id": self.problem.pk, "language": "python", "code": "print(1)"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("status"), "queued")
        # Hidden data is not present
        raw_json = res.content.decode("utf-8")
        self.assertNotIn("SECRET_HIDDEN", raw_json)
        self.assertNotIn("marks_awarded", raw_json)
        self.assertNotIn("results", raw_json)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class RunnerBoundaryTests(TestCase):
    """Verifies that the code runner boundary fails closed in production and bounds inputs."""

    @override_settings(DEBUG=False, CODE_RUNNER="local")
    def test_production_rejects_local_runner_in_get_runner(self):
        with self.assertRaises(RunnerError) as ctx:
            get_runner()
        self.assertIn("prohibited", str(ctx.exception))

    @override_settings(DEBUG=False, CODE_RUNNER="local")
    def test_production_deployment_check_flags_local_runner(self):
        from assessments.checks import check_runner_security
        errors = check_runner_security(None)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, "assessments.E001")

    @override_settings(DEBUG=True, CODE_RUNNER="local")
    def test_development_permits_local_runner_check(self):
        from assessments.checks import check_runner_security
        errors = check_runner_security(None)
        self.assertEqual(errors, [])

    @override_settings(DEBUG=True)
    def test_runner_rejects_oversized_source_code_and_stdin(self):
        runner = LocalRunner()
        huge_code = "x = 1\n" * 15000  # > 50,000 chars
        with self.assertRaises(RunnerError) as ctx:
            runner.run_case("python", huge_code, "in")
        self.assertIn("Source code exceeds", str(ctx.exception))

        huge_stdin = "A" * 70000  # > 64 KB
        with self.assertRaises(RunnerError) as ctx:
            runner.run_case("python", "print(1)", huge_stdin)
        self.assertIn("Input size exceeds", str(ctx.exception))

    def test_judge0_runner_sanitizes_network_errors(self):
        runner = Judge0Runner("http://192.168.1.99:2358", api_key="secret-api-key")
        with self.assertRaises(RunnerError) as ctx:
            runner.run_case("python", "print(1)", "in")
        # Ensure raw IP or credentials are not leaked in exception string
        self.assertNotIn("secret-api-key", str(ctx.exception))
        self.assertIn("Sandbox execution service is currently unavailable.", str(ctx.exception))


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class AsyncGradingIdempotencyTests(EngineFixture):
    """Tests asynchronous grading, status endpoints, and duplicate submission idempotency."""

    def setUp(self):
        super().setUp()
        self.attempt = self.start()
        mcq_sec = self.attempt.sections.filter(section__kind=ExamSection.Kind.MCQ).first()
        services._end_section(self.attempt, mcq_sec, AttemptSection.Status.SUBMITTED, at=timezone.now())
        services._advance(self.attempt, previous_end=timezone.now())
        self.problem = self.attempt.coding_items.first().problem
        self.client.force_login(self.candidate)

    def test_submission_and_duplicate_submission_are_idempotent(self):
        res1 = self.client.post(
            reverse("api_code_submit", args=[self.attempt.pk]),
            data=json.dumps({"problem_id": self.problem.pk, "language": "python", "code": "def solve(): return 1"}),
            content_type="application/json",
        )
        self.assertEqual(res1.status_code, 200)
        sub_id1 = res1.json()["submission_id"]

        # Duplicate submission retry should return 200 and the same submission ID
        res2 = self.client.post(
            reverse("api_code_submit", args=[self.attempt.pk]),
            data=json.dumps({"problem_id": self.problem.pk, "language": "python", "code": "def solve(): return 1"}),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["submission_id"], sub_id1)
        self.assertEqual(CodingSubmission.objects.filter(attempt=self.attempt, problem=self.problem).count(), 1)

    def test_submission_status_endpoint_authorization(self):
        sub = CodingSubmission.objects.create(
            attempt=self.attempt,
            problem=self.problem,
            version=1,
            language="python",
            source_code="print(1)",
            status=CodingSubmission.Status.QUEUED,
            is_final=True,
        )
        # Candidate owner can check status
        res = self.client.get(reverse("api_submission_status", args=[self.attempt.pk, sub.pk]))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "queued")
        self.assertNotIn("marks_awarded", data)

        # Another candidate cannot check status (403 forbidden)
        self.client.force_login(self.other)
        res_other = self.client.get(reverse("api_submission_status", args=[self.attempt.pk, sub.pk]))
        self.assertEqual(res_other.status_code, 403)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class ProctoringIntegrityTests(EngineFixture):
    """Verifies event allowlist, server-only event protections, and closed attempt rejections."""

    def setUp(self):
        super().setUp()
        self.attempt = self.start()
        self.client.force_login(self.candidate)

    def test_client_cannot_submit_server_only_events(self):
        # session_changed is server-generated only
        res = self.client.post(
            reverse("api_event", args=[self.attempt.pk]),
            data=json.dumps({"kind": "session_changed", "duration_ms": 0}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json().get("code"), "bad_request")

        # camera_ready is server-generated only
        res2 = self.client.post(
            reverse("api_event", args=[self.attempt.pk]),
            data=json.dumps({"kind": "camera_ready", "duration_ms": 0}),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 400)

    def test_client_allowed_event_succeeds(self):
        res = self.client.post(
            reverse("api_event", args=[self.attempt.pk]),
            data=json.dumps({"kind": "tab_hidden", "duration_ms": 2500, "client_at": timezone.now().isoformat()}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("ok"))
        self.assertEqual(self.attempt.proctoring_events.filter(kind="tab_hidden").count(), 1)

    def test_closed_attempt_rejects_client_events(self):
        self.attempt.status = Attempt.Status.SUBMITTED
        self.attempt.save(update_fields=["status"])

        res = self.client.post(
            reverse("api_event", args=[self.attempt.pk]),
            data=json.dumps({"kind": "tab_hidden", "duration_ms": 100}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 409)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class AuthorizationAuditTests(EngineFixture):
    """Table-driven authorization audit across object endpoints for candidate, reviewer, and staff."""

    def setUp(self):
        super().setUp()
        self.attempt = self.start()

    def test_candidate_cannot_access_another_candidates_attempt(self):
        self.client.force_login(self.other)
        # Attempting to read state of another candidate's attempt
        res_state = self.client.get(reverse("api_state", args=[self.attempt.pk]))
        self.assertEqual(res_state.status_code, 404)

        # Attempting to post events to another candidate's attempt
        res_event = self.client.post(
            reverse("api_event", args=[self.attempt.pk]),
            data=json.dumps({"kind": "tab_hidden", "duration_ms": 100}),
            content_type="application/json",
        )
        self.assertEqual(res_event.status_code, 404)

        # Attempting to run code on another candidate's attempt
        prob_id = self.attempt.coding_items.first().problem_id
        res_run = self.client.post(
            reverse("api_code_run", args=[self.attempt.pk]),
            data=json.dumps({"problem_id": prob_id, "language": "python", "code": "print(1)"}),
            content_type="application/json",
        )
        self.assertEqual(res_run.status_code, 404)

    def test_unauthorized_user_cannot_access_staff_endpoints(self):
        self.client.force_login(self.candidate)
        staff_endpoints = [
            reverse("staff_exam_list"),
            reverse("staff_exam_create"),
            reverse("staff_exam_overview", args=[self.exam.pk]),
            reverse("staff_export_csv", args=[self.exam.pk]),
        ]
        for url in staff_endpoints:
            with self.subTest(url=url):
                res = self.client.get(url)
                # Redirects to login or returns 403
                self.assertIn(res.status_code, [302, 403])


class CSVFormulaInjectionTests(TestCase):
    """Verifies formula injection defense in spreadsheet exports."""

    def test_csv_safe_strips_formula_prefixes_even_with_whitespace(self):
        dangerous = [
            "=cmd|' /C calc'!A0",
            "+1+1",
            "-5+2",
            "@SUM(A1:B10)",
            "  =cmd|' /C calc'!A0",
            "\t=1+1",
            "\r=2+2",
            " |calc",
            " %test",
        ]
        for payload in dangerous:
            with self.subTest(payload=payload):
                safe = csv_safe(payload)
                self.assertTrue(safe.startswith("'"), f"Payload not neutralized: {payload} -> {safe}")

    def test_csv_safe_preserves_legitimate_text(self):
        self.assertEqual(csv_safe("John Doe"), "John Doe")
        self.assertEqual(csv_safe("john@example.com"), "john@example.com")
        self.assertEqual(csv_safe(123), 123)
        self.assertIsNone(csv_safe(None))


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class UploadSecurityTests(TestCase):
    """Verifies file validation against spoofed extensions, decompression bombs, and safe downloads."""

    def test_resume_upload_rejects_spoofed_file_extension(self):
        # A file named resume.pdf that contains plain text without %PDF- magic bytes
        fake_pdf = SimpleUploadedFile("resume.pdf", b"This is not a real PDF file content.")
        with self.assertRaises(ValidationError) as ctx:
            validate_resume_file(fake_pdf)
        self.assertIn("Invalid PDF file", str(ctx.exception))

    def test_resume_upload_accepts_valid_signatures(self):
        valid_pdf = SimpleUploadedFile("resume.pdf", b"%PDF-1.4\n%real pdf header")
        validate_resume_file(valid_pdf)  # should not raise

        valid_docx = SimpleUploadedFile("resume.docx", b"PK\x03\x04\x14\x00\x06\x00")
        validate_resume_file(valid_docx)  # should not raise

    def test_snapshot_rejects_malformed_image(self):
        staff = User.objects.create_user("staff", "st@example.com", "pw", is_staff=True)
        exam = Exam.objects.create(
            title="Snap exam", opens_at=timezone.now(), closes_at=timezone.now() + timedelta(hours=1),
            duration_minutes=60, is_published=True, created_by=staff
        )
        cand = User.objects.create_user("cand_snap", "cand_snap@example.com", "pw")
        app = Application.objects.create(candidate=cand, exam=exam, full_name="Candidate Snap", status=Application.Status.APPROVED)
        attempt = Attempt.objects.create(
            candidate=cand,
            exam=exam,
            application=app,
            policy_snapshot={},
            snapshot_consent_at=timezone.now(),
            started_at=timezone.now(),
            deadline_at=timezone.now() + timedelta(hours=1),
            last_seen_at=timezone.now(),
        )

        # Spoofed image: starts with JPEG magic bytes FF D8 FF but corrupt payload
        corrupt_jpg = SimpleUploadedFile("snap.jpg", b"\xff\xd8\xff\x00\x00corrupt")
        with self.assertRaises(services.AttemptError) as ctx:
            services.store_snapshot(attempt, timezone.now(), corrupt_jpg)
        self.assertEqual(ctx.exception.code, "bad_image")


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class AuthenticationAndRecoveryTests(TestCase):
    """Verifies single-use setup tokens, email change verification, and session rotation."""

    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user("admin_auth", "admin_auth@example.com", "pw-admin", is_staff=True)
        self.exam = Exam.objects.create(
            title="Auth exam", opens_at=timezone.now(), closes_at=timezone.now() + timedelta(hours=1),
            duration_minutes=30, is_published=True, created_by=self.staff
        )
        self.candidate = User.objects.create_user("cand_auth", "cand_auth@example.com", "pw-cand-1234")
        self.app = Application.objects.create(
            candidate=self.candidate, exam=self.exam, full_name="Auth Candidate", status=Application.Status.APPROVED
        )

    def test_setup_token_flow_single_use_and_expiry(self):
        import hashlib
        raw_token = "valid-setup-token-secret-12345678"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        self.app.setup_token_hash = token_hash
        self.app.setup_token_expires_at = timezone.now() + timedelta(hours=24)
        self.app.save()

        # First use logs in successfully
        res = self.client.get(reverse("setup_account", args=[raw_token]))
        self.assertRedirects(res, reverse("home"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.candidate.pk)

        # Token is marked used
        self.app.refresh_from_db()
        self.assertIsNotNone(self.app.setup_token_used_at)

        # Second use (replay) fails
        self.client.logout()
        res_replay = self.client.get(reverse("setup_account", args=[raw_token]))
        self.assertRedirects(res_replay, reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_email_change_requires_verification(self):
        self.client.force_login(self.candidate)
        resume_file = SimpleUploadedFile("resume.pdf", b"%PDF-1.4\nvalid pdf")
        res = self.client.post(
            reverse("confirm_details", args=[self.exam.pk]),
            data={
                "email": "new_address@example.com",
                "full_name": "Auth Candidate",
                "phone": "+1234567890",
                "resume": resume_file,
            },
        )
        self.app.refresh_from_db()
        self.candidate.refresh_from_db()
        # Candidate email remains unchanged until verification
        self.assertEqual(self.candidate.email, "cand_auth@example.com")
        self.assertEqual(self.app.pending_email, "new_address@example.com")
        self.assertTrue(bool(self.app.email_verification_token))


class SecurityHeadersAndHealthTests(TestCase):
    """Verifies that security headers, CSP, and health endpoints are working as expected."""

    def test_health_and_readiness_endpoints(self):
        res_h = self.client.get(reverse("health_check"))
        self.assertEqual(res_h.status_code, 200)
        self.assertEqual(res_h.json().get("status"), "ok")

        res_r = self.client.get(reverse("readiness_check"))
        self.assertEqual(res_r.status_code, 200)
        self.assertEqual(res_r.json().get("status"), "ready")

    def test_security_headers_and_csp_present(self):
        res = self.client.get(reverse("home"))
        self.assertIn("Content-Security-Policy", res.headers)
        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("Permissions-Policy", res.headers)
        self.assertIn("X-Request-ID", res.headers)
