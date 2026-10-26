"""Tests for camera consent, the local development runner, grading and reviewer access."""

import json
import shutil
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import services
from .models import Attempt, AttemptCoding, CodingSubmission, ProctoringSnapshot
from .runners import Judge0Runner, LocalRunner, RunnerError, UnconfiguredRunner, get_runner
from .services import AttemptError, perform
from .tests_engine import TMP_MEDIA, EngineFixture

User = get_user_model()
JPEG = b"\xff\xd8\xff" + b"0" * 200

def unittest_skip_unless(condition):
    return skipUnless(condition, "interpreter not installed on this machine")


HAS_PYTHON = shutil.which("python3") is not None
HAS_NODE = shutil.which("node") is not None


class ConsentTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.exam.camera_required = True
        self.exam.save()

    def test_readiness_is_refused_without_snapshot_consent_when_camera_is_required(self):
        attempt = self.start()
        with self.assertRaises(AttemptError) as ctx:
            services.record_readiness(attempt, timezone.now(), snapshot_consent=False)
        self.assertEqual(ctx.exception.code, "consent_required")
        attempt.refresh_from_db()
        self.assertIsNone(attempt.readiness_confirmed_at)

    def test_consent_is_recorded_and_allows_snapshots(self):
        attempt = self.start()
        services.record_readiness(attempt, timezone.now(), snapshot_consent=True)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.snapshot_consent_at)
        upload = SimpleUploadedFile("face.jpg", JPEG, content_type="image/jpeg")
        snapshot = perform(
            self.candidate, attempt.pk, lambda a, now: services.store_snapshot(a, now, upload)
        )
        self.assertTrue(ProctoringSnapshot.objects.filter(pk=snapshot.pk).exists())

    def test_snapshot_refused_without_consent(self):
        attempt = self.start()
        services.record_readiness(attempt, timezone.now(), snapshot_consent=True)
        Attempt.objects.filter(pk=attempt.pk).update(snapshot_consent_at=None)
        upload = SimpleUploadedFile("face.jpg", JPEG, content_type="image/jpeg")
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, attempt.pk, lambda a, now: services.store_snapshot(a, now, upload))
        self.assertEqual(ctx.exception.code, "consent_required")

    def test_snapshots_refused_when_disabled_for_the_test(self):
        self.exam.snapshots_enabled = False
        self.exam.save()
        attempt = self.start()
        services.record_readiness(attempt, timezone.now(), snapshot_consent=True)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.snapshot_consent_at)
        upload = SimpleUploadedFile("face.jpg", JPEG, content_type="image/jpeg")
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, attempt.pk, lambda a, now: services.store_snapshot(a, now, upload))
        self.assertEqual(ctx.exception.code, "snapshots_disabled")

    def test_policy_snapshot_freezes_snapshot_setting_for_running_attempt(self):
        attempt = self.start()
        self.exam.snapshots_enabled = False
        self.exam.save()
        attempt.refresh_from_db()
        self.assertTrue(attempt.policy_snapshot["snapshots_enabled"])

    def test_consent_not_required_when_camera_is_off(self):
        self.exam.camera_required = False
        self.exam.save()
        attempt = self.start()
        services.record_readiness(attempt, timezone.now(), snapshot_consent=False)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.readiness_confirmed_at)
        self.assertIsNone(attempt.snapshot_consent_at)


@override_settings(CODE_CPU_TIME_LIMIT_S=1, CODE_MEMORY_LIMIT_KB=256 * 1024, CODE_OUTPUT_LIMIT_KB=1)
class LocalRunnerTests(TestCase):
    @unittest_skip_unless(HAS_PYTHON)
    def test_python_program_runs_and_returns_output(self):
        outcome = LocalRunner().run_case("python", "a, b = map(int, input().split())\nprint(a + b)\n", "2 3\n")
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.stdout.strip(), "5")

    @unittest_skip_unless(HAS_PYTHON)
    def test_exception_is_a_runtime_error(self):
        outcome = LocalRunner().run_case("python", "raise ValueError('boom')\n", "")
        self.assertEqual(outcome.status, "runtime_error")
        self.assertIn("ValueError", outcome.stderr)

    @unittest_skip_unless(HAS_PYTHON)
    def test_infinite_loop_is_stopped_by_the_time_limit(self):
        outcome = LocalRunner().run_case("python", "while True:\n    pass\n", "")
        self.assertEqual(outcome.status, "timeout")

    @unittest_skip_unless(HAS_PYTHON)
    def test_output_is_capped(self):
        outcome = LocalRunner().run_case("python", "print('x' * 5000)\n", "")
        self.assertLessEqual(len(outcome.stdout), 1024)

    @unittest_skip_unless(HAS_PYTHON)
    def test_code_runs_in_isolated_mode_without_site_packages_or_cwd_imports(self):
        outcome = LocalRunner().run_case(
            "python", "import sys\nprint(sys.flags.isolated)\n", ""
        )
        self.assertEqual(outcome.stdout.strip(), "1")

    def test_unsupported_language_is_rejected(self):
        with self.assertRaises(RunnerError):
            LocalRunner().run_case("cobol", "DISPLAY 'hi'.", "")

    @unittest_skip_unless(HAS_NODE)
    def test_javascript_program_runs(self):
        outcome = LocalRunner().run_case("javascript", "console.log(String(7 * 6));\n", "")
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.stdout.strip(), "42")


class RunnerSelectionTests(TestCase):
    @override_settings(CODE_RUNNER="local")
    def test_local_mode_selects_local_runner(self):
        self.assertIsInstance(get_runner(), LocalRunner)

    @override_settings(CODE_RUNNER="", JUDGE0_URL="")
    def test_default_without_judge0_is_unconfigured(self):
        self.assertIsInstance(get_runner(), UnconfiguredRunner)

    @override_settings(CODE_RUNNER="", JUDGE0_URL="https://judge.example.test")
    def test_judge0_url_selects_judge0(self):
        self.assertIsInstance(get_runner(), Judge0Runner)

    @override_settings(CODE_RUNNER="judge0", JUDGE0_URL="")
    def test_judge0_mode_without_url_is_unconfigured(self):
        self.assertIsInstance(get_runner(), UnconfiguredRunner)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA, CODE_CPU_TIME_LIMIT_S=2, CODE_OUTPUT_LIMIT_KB=64)
class LocalGradingTests(EngineFixture):
    """Real execution through LocalRunner: public and hidden cases graded, hidden data never returned."""

    def setUp(self):
        super().setUp()
        # Make coding the first section so the candidate can submit code straight away.
        # Two steps, because section order is unique per exam.
        self.mcq_section.order = 3
        self.mcq_section.save()
        self.coding_section.order = 1
        self.coding_section.save()
        self.mcq_section.order = 2
        self.mcq_section.save()

    def _submit(self, code, problem=None):
        attempt = self.start()
        problem = problem or AttemptCoding.objects.filter(attempt=attempt).first().problem
        submission = perform(
            self.candidate, attempt.pk, lambda a, now: services.submit_code(a, now, problem.pk, "python", code)
        )
        return attempt, problem, submission

    @unittest_skip_unless(HAS_PYTHON)
    def test_correct_program_passes_public_and_hidden_cases(self):
        code = "a, b = map(int, input().split())\nprint(a + b)\n"
        _, problem, submission = self._submit(code)
        services.grade_submission(submission, runner=LocalRunner())
        submission.refresh_from_db()
        self.assertEqual(submission.status, CodingSubmission.Status.COMPLETED)
        self.assertEqual(submission.total_cases, 3)
        self.assertEqual(submission.public_passed, 2)
        self.assertEqual(submission.hidden_passed, 1)
        self.assertEqual(float(submission.marks_awarded), float(problem.marks))

    @unittest_skip_unless(HAS_PYTHON)
    def test_wrong_program_earns_no_marks_and_candidate_never_sees_hidden_data(self):
        code = "print(0)\n"
        attempt, _, submission = self._submit(code)
        services.grade_submission(submission, runner=LocalRunner())
        submission.refresh_from_db()
        self.assertEqual(float(submission.marks_awarded), 0)

        state = services.candidate_state(attempt, timezone.now())
        blob = json.dumps(state)
        self.assertNotIn("9 9", blob)
        self.assertNotIn('"18"', blob)

    @unittest_skip_unless(HAS_PYTHON)
    def test_runner_outage_is_recorded_not_graded_as_wrong(self):
        _, _, submission = self._submit("print(1)\n")
        with mock.patch.object(LocalRunner, "run_case", side_effect=RunnerError("down")):
            services.grade_submission(submission, runner=LocalRunner())
        submission.refresh_from_db()
        self.assertEqual(submission.status, CodingSubmission.Status.RUNNER_ERROR)


class ReviewerAccessTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.reviewer = User.objects.create_user("reviewer", "r@example.com", "pw-12345-x")
        self.reviewer.groups.add(Group.objects.get_or_create(name="Reviewers")[0])

    def test_reviewer_can_open_attempt_review_but_not_edit_the_test(self):
        attempt = self.start()
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse("staff_attempt_review", args=[attempt.pk]))
        self.assertEqual(response.status_code, 200)

        title = self.exam.title
        self.client.post(
            reverse("staff_exam_edit", args=[self.exam.pk]),
            {"title": "Hacked", "instructions": "x", "opens_at": "2020-01-01T00:00",
             "closes_at": "2030-01-01T00:00", "duration_minutes": 60},
        )
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.title, title)

    def test_reviewer_can_record_interview_decision(self):
        attempt = self.start()
        self.client.force_login(self.reviewer)
        self.client.post(
            reverse("staff_attempt_decision", args=[attempt.pk]),
            {"decision": "recommended", "notes": "Solid MCQ work."},
        )
        self.assertTrue(attempt.reviews.filter(reviewer=self.reviewer).exists())

    def test_candidate_cannot_open_review_pages(self):
        attempt = self.start()
        self.client.force_login(self.candidate)
        response = self.client.get(reverse("staff_attempt_review", args=[attempt.pk]))
        self.assertNotEqual(response.status_code, 200)
