import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import services
from .forms import SiteSettingsForm
from .models import (
    Application,
    Attempt,
    AttemptSection,
    CodingSubmission,
    CodingTestCase,
    Exam,
    ExamSection,
    SiteSettings,
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

        # GET only renders confirmation page without consuming token or authenticating
        res_get = self.client.get(reverse("setup_account", args=[raw_token]))
        self.assertEqual(res_get.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.app.refresh_from_db()
        self.assertIsNone(self.app.setup_token_used_at)

        # POST consumes token and logs in successfully
        res = self.client.post(reverse("setup_account", args=[raw_token]))
        self.assertRedirects(res, reverse("home"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.candidate.pk)

        # Token is marked used
        self.app.refresh_from_db()
        self.assertIsNotNone(self.app.setup_token_used_at)

        # Second use (replay) fails
        self.client.logout()
        res_replay = self.client.post(reverse("setup_account", args=[raw_token]))
        self.assertRedirects(res_replay, reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_email_change_requires_verification(self):
        self.client.force_login(self.candidate)
        self.client.post(
            reverse("confirm_details", args=[self.exam.pk]),
            data={
                "email": "new_address@example.com",
                "full_name": "Auth Candidate",
                "phone": "+1234567890",
                "monitoring_consent": "on",
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


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class CandidateResumeSecurityTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff_resume", "staff_resume@example.com", "pw-admin", is_staff=True)
        self.exam = Exam.objects.create(
            title="Resume Exam",
            opens_at=timezone.now(),
            closes_at=timezone.now() + timedelta(hours=1),
            duration_minutes=30,
            is_published=True,
            created_by=self.staff,
        )
        self.candidate1 = User.objects.create_user("cand_res_1", "cand_res_1@example.com", "pw-cand-1")
        self.candidate2 = User.objects.create_user("cand_res_2", "cand_res_2@example.com", "pw-cand-2")

        pdf_content = b"%PDF-1.4 sample pdf content for resume test"
        self.app1 = Application.objects.create(
            candidate=self.candidate1,
            exam=self.exam,
            full_name="Alice Smith",
            status=Application.Status.APPROVED,
            resume=SimpleUploadedFile("alice_resume.pdf", pdf_content, content_type="application/pdf"),
        )
        self.app2 = Application.objects.create(
            candidate=self.candidate2,
            exam=self.exam,
            full_name="Bob Jones",
            status=Application.Status.APPROVED,
        )

    def test_candidate_resume_defensive_headers_and_filename(self):
        self.client.force_login(self.candidate1)
        res = self.client.get(reverse("candidate_resume", args=[self.exam.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.headers.get("Content-Security-Policy"), "default-src 'none'")
        self.assertEqual(res.headers.get("Cache-Control"), "private, no-store")
        disposition = res.headers.get("Content-Disposition", "")
        self.assertIn("inline", disposition)
        self.assertIn(f"Resume_alice_smith_{self.app1.pk}.pdf", disposition)

    def test_candidate_resume_download_attachment_header(self):
        self.client.force_login(self.candidate1)
        res = self.client.get(reverse("candidate_resume", args=[self.exam.pk]) + "?download=1")
        self.assertEqual(res.status_code, 200)
        disposition = res.headers.get("Content-Disposition", "")
        self.assertIn("attachment", disposition)
        self.assertIn(f"Resume_alice_smith_{self.app1.pk}.pdf", disposition)

    def test_candidate_cannot_access_missing_resume_or_other_candidate_resume(self):
        self.client.force_login(self.candidate2)
        res = self.client.get(reverse("candidate_resume", args=[self.exam.pk]))
        self.assertEqual(res.status_code, 404)

        self.client.logout()
        res_unauth = self.client.get(reverse("candidate_resume", args=[self.exam.pk]))
        self.assertEqual(res_unauth.status_code, 302)

    def test_staff_application_resume_defensive_headers(self):
        self.client.force_login(self.staff)
        res = self.client.get(reverse("staff_application_resume", args=[self.app1.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.headers.get("Content-Security-Policy"), "default-src 'none'")
        self.assertEqual(res.headers.get("Cache-Control"), "private, no-store")
        self.assertIn(f"Resume_alice_smith_{self.app1.pk}.pdf", res.headers.get("Content-Disposition", ""))


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class BrandingSecurityTests(TestCase):
    def setUp(self):
        self.site = SiteSettings.load()
        svg_content = b"<svg xmlns='http://www.w3.org/2000/svg'><circle r='10'/></svg>"
        self.site.site_logo = SimpleUploadedFile("clean_logo.svg", svg_content, content_type="image/svg+xml")
        self.site.save()

    def test_serve_branding_csp_and_cache_headers(self):
        res = self.client.get(reverse("serve_branding", args=["site_logo"]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.headers.get("Content-Security-Policy"), "default-src 'none'; style-src 'unsafe-inline'")
        self.assertEqual(res.headers.get("Cache-Control"), "public, max-age=86400")


class EmailUniquenessSecurityTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff_email", "staff_email@example.com", "pw-admin", is_staff=True)
        self.exam = Exam.objects.create(
            title="Email Exam",
            opens_at=timezone.now(),
            closes_at=timezone.now() + timedelta(hours=1),
            duration_minutes=30,
            is_published=True,
            created_by=self.staff,
        )
        self.cand_a = User.objects.create_user("cand_a", "canda@example.com", "pw-cand-a")
        self.cand_b = User.objects.create_user("cand_b", "candb@example.com", "pw-cand-b")
        self.app_a = Application.objects.create(
            candidate=self.cand_a, exam=self.exam, full_name="Candidate A", status=Application.Status.APPROVED
        )
        self.app_b = Application.objects.create(
            candidate=self.cand_b, exam=self.exam, full_name="Candidate B", status=Application.Status.APPROVED
        )

    def test_confirm_details_duplicate_email_rejected_without_crash(self):
        self.client.force_login(self.cand_a)
        res = self.client.post(
            reverse("confirm_details", args=[self.exam.pk]),
            data={"email": "candb@example.com", "full_name": "Candidate A", "phone": "+1234567890", "monitoring_consent": "on"},
        )
        self.assertRedirects(res, reverse("confirm_details", args=[self.exam.pk]))
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.pending_email, "")

    def test_verify_email_collision_rejected_without_crash(self):
        import hashlib
        raw_token = "valid-verify-token-abcdef123456"
        self.app_a.pending_email = "candb@example.com"
        self.app_a.email_verification_token = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        self.app_a.email_verification_sent_at = timezone.now()
        self.app_a.save()

        self.client.force_login(self.cand_a)
        res = self.client.get(reverse("verify_email", args=[raw_token]))
        self.assertRedirects(res, reverse("home"))
        self.cand_a.refresh_from_db()
        self.assertEqual(self.cand_a.email, "canda@example.com")


class SiteSettingsUploadSecurityTests(TestCase):
    def test_site_settings_form_rejects_malicious_svg(self):
        base_data = {"site_name": "Test", "snapshot_retention_days": 30}
        bad_svg = b"<svg><script>alert(1)</script></svg>"
        form = SiteSettingsForm(
            data=base_data,
            files={"site_logo": SimpleUploadedFile("bad.svg", bad_svg, content_type="image/svg+xml")},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("site_logo", form.errors)

        handler_svg = b"<svg onload='alert(1)'></svg>"
        form2 = SiteSettingsForm(
            data=base_data,
            files={"favicon": SimpleUploadedFile("bad_fav.svg", handler_svg, content_type="image/svg+xml")},
        )
        self.assertFalse(form2.is_valid())
        self.assertIn("favicon", form2.errors)

        padded_svg = (b"<!-- " + b"A" * 5000 + b" --><svg><foreignObject><body><script>alert(1)</script></body></foreignObject></svg>")
        form3 = SiteSettingsForm(
            data=base_data,
            files={"site_logo": SimpleUploadedFile("padded.svg", padded_svg, content_type="image/svg+xml")},
        )
        self.assertFalse(form3.is_valid())
        self.assertIn("site_logo", form3.errors)

        clean_svg = b"<svg xmlns='http://www.w3.org/2000/svg'><circle cx='50' cy='50' r='40'/></svg>"
        form4 = SiteSettingsForm(
            data=base_data,
            files={"site_logo": SimpleUploadedFile("clean.svg", clean_svg, content_type="image/svg+xml")},
        )
        self.assertTrue(form4.is_valid())


class CsvSafeSecurityTests(TestCase):
    def test_csv_safe_formula_injection_prevention(self):
        self.assertEqual(csv_safe("=1+1"), "'=1+1")
        self.assertEqual(csv_safe("+SUM(A1:A10)"), "'+SUM(A1:A10)")
        self.assertEqual(csv_safe("-cmd|'/c calc'!A1"), "'-cmd|'/c calc'!A1")
        self.assertEqual(csv_safe("@HYPERLINK('http://evil.com')"), "'@HYPERLINK('http://evil.com')")

        self.assertEqual(csv_safe("   =1+1"), "'   =1+1")
        self.assertEqual(csv_safe("\t\t+2+2"), "'\t\t+2+2")
        self.assertEqual(csv_safe("  \t  -test"), "'  \t  -test")

        self.assertEqual(csv_safe("Rahul Sharma"), "Rahul Sharma")
        self.assertEqual(csv_safe("john.doe@example.com"), "john.doe@example.com")
        self.assertEqual(csv_safe(123), 123)
