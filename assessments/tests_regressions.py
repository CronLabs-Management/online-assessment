"""Regression tests for the defects found in the review. Each test names the failure it guards against."""

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import exam_api, services
from .forms import CodingProblemForm, ExamSectionForm
from .models import (
    Application,
    Attempt,
    AttemptCoding,
    AttemptSection,
    CodingProblem,
    CodingTestCase,
    ConnectionEvent,
    ExamSection,
)
from .review import csv_safe
from .runners import Judge0Runner, RunnerError
from .services import AttemptError, perform
from .tests_engine import EngineFixture


class CodingSectionTests(EngineFixture):
    def test_two_coding_sections_draw_distinct_problems(self):
        # Regression: a second coding section could reuse a problem and fail with IntegrityError on start.
        ExamSection.objects.create(
            exam=self.exam, title="Coding 2", kind="coding", order=3, duration_minutes=5, coding_selection_count=1
        )
        extra = CodingProblem.objects.create(
            exam=self.exam, title="Extra", statement="x", languages="python", marks=10, order=99
        )
        CodingTestCase.objects.create(problem=extra, input_data="1 1", expected_output="2", order=1)
        attempt = self.start()
        chosen = list(AttemptCoding.objects.filter(attempt=attempt).values_list("problem_id", flat=True))
        self.assertEqual(len(chosen), len(set(chosen)))
        self.assertEqual(len(chosen), 3)

    def test_too_small_pool_refuses_to_start_instead_of_crashing(self):
        # Regression: a coding section asking for more problems than the pool holds must be refused cleanly.
        self.coding_section.coding_selection_count = 50
        self.coding_section.save()
        with self.assertRaises(AttemptError) as ctx:
            self.start()
        self.assertEqual(ctx.exception.code, "misconfigured")


class ExpiryTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.attempt = self.start()

    def test_abandoned_attempt_is_finished_by_cron_sweep(self):
        # Regression: an attempt the candidate walked away from was never finalised.
        Attempt.objects.filter(pk=self.attempt.pk).update(deadline_at=timezone.now() - timedelta(seconds=5))
        processed = services.expire_due_attempts()
        self.attempt.refresh_from_db()
        self.assertEqual(processed, 1)
        self.assertEqual(self.attempt.status, Attempt.Status.EXPIRED)

    def test_sweep_ignores_attempts_with_time_left(self):
        self.assertEqual(services.expire_due_attempts(), 0)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, Attempt.Status.IN_PROGRESS)

    def test_final_section_timeout_is_recorded_as_expired_not_submitted(self):
        # Regression: running out of time on the last section was recorded as a normal submission.
        # The coding section's deadline is derived from when it starts (the MCQ deadline) plus its
        # duration, so pushing the MCQ deadline back past the coding duration expires both in one sweep.
        self.set_section_deadline(self.attempt, "mcq", timezone.now() - timedelta(minutes=25))
        perform(self.candidate, self.attempt.pk, lambda a, now: None)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, Attempt.Status.EXPIRED)
        self.assertEqual(
            AttemptSection.objects.get(attempt=self.attempt, section__kind="coding").status,
            AttemptSection.Status.EXPIRED,
        )

    def test_expiry_survives_a_rejected_action(self):
        # Regression: the expiry was rolled back when the action that triggered it was refused.
        Attempt.objects.filter(pk=self.attempt.pk).update(deadline_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(AttemptError):
            perform(self.candidate, self.attempt.pk, lambda a, now: services.save_answer(a, now, 0, 0))
        self.attempt.refresh_from_db()
        self.assertTrue(self.attempt.is_final)

    def test_pause_credit_never_moves_deadline_past_exam_close(self):
        # Regression: pause credit could push the deadline beyond the exam's closing time.
        Attempt.objects.filter(pk=self.attempt.pk).update(last_seen_at=timezone.now() - timedelta(hours=1))
        perform(self.candidate, self.attempt.pk, lambda a, now: None)
        self.attempt.refresh_from_db()
        self.assertLessEqual(self.attempt.deadline_at, self.exam.closes_at)
        event = ConnectionEvent.objects.get(attempt=self.attempt)
        self.assertLessEqual(event.granted_seconds, event.gap_seconds)


class AccessTests(EngineFixture):
    def test_rejected_candidate_loses_access_mid_attempt(self):
        # Regression: a candidate rejected after starting kept working until the timer ran out.
        attempt = self.start()
        Application.objects.filter(pk=self.application.pk).update(status=Application.Status.REVOKED)
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, attempt.pk, lambda a, now: None)
        self.assertEqual(ctx.exception.code, "not_approved")

    def test_result_page_redirects_while_attempt_is_running(self):
        # Regression: the result page rendered incomplete scores mid-attempt.
        attempt = self.start()
        self.client.force_login(self.candidate)
        response = self.client.get(reverse("attempt_result", args=[attempt.pk]))
        self.assertEqual(response.status_code, 302)

    def test_reviewer_cannot_decide_on_their_own_attempt(self):
        # Regression: a reviewer who also sat the test could approve their own attempt.
        reviewer = self.candidate
        reviewer.groups.add(Group.objects.get_or_create(name="Reviewers")[0])
        attempt = self.start()
        self.client.force_login(reviewer)
        self.client.post(
            reverse("staff_attempt_decision", args=[attempt.pk]),
            {"decision": "pass", "notes": "self"},
        )
        self.assertFalse(attempt.reviews.exists())


class SectionFormTests(EngineFixture):
    def test_duplicate_section_order_shows_message_not_server_error(self):
        # Regression: a duplicate order caused an IntegrityError (HTTP 500).
        self.client.force_login(self.staff)
        before = self.exam.sections.count()
        response = self.client.post(
            reverse("staff_section_add", args=[self.exam.pk]),
            {"section-title": "Dup", "section-kind": "mcq", "section-order": 1,
             "section-duration_minutes": 5, "section-question_count": 1, "section-coding_selection_count": 1},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.exam.sections.count(), before)

    def test_zero_duration_is_rejected_by_model_validation(self):
        section = ExamSection(exam=self.exam, title="Zero", kind="mcq", order=9, duration_minutes=0)
        with self.assertRaises(ValidationError):
            section.full_clean()

    def test_zero_duration_is_rejected_by_form(self):
        form = ExamSectionForm(
            {"title": "Zero", "kind": "mcq", "order": 9, "duration_minutes": 0,
             "question_count": 1, "coding_selection_count": 1},
            prefix="section",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("duration_minutes", form.errors)

    def test_unsupported_language_is_rejected_by_form(self):
        form = CodingProblemForm(
            {"title": "T", "statement": "S", "input_spec": "", "output_spec": "", "languages": "cobol",
             "marks": 10, "order": 1, "starter_code": ""}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("languages", form.errors)


class RateLimitTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_fixed_window_allows_up_to_limit_then_blocks(self):
        results = [services.rate_limited("k", 2, 60) for _ in range(3)]
        self.assertEqual(results, [False, False, True])

    def test_steady_traffic_does_not_block_forever(self):
        # Regression: a sliding counter under steady traffic never reset.
        with mock.patch("assessments.services.time.time", return_value=1_000_000.0):
            self.assertFalse(services.rate_limited("k", 1, 60))
            self.assertTrue(services.rate_limited("k", 1, 60))
        with mock.patch("assessments.services.time.time", return_value=1_000_061.0):
            self.assertFalse(services.rate_limited("k", 1, 60))


class CsvSafetyTests(TestCase):
    def test_formula_values_are_neutralised(self):
        for value in ["=1+1", "+cmd", "-2", "@SUM(A1)", "\tcalc", "\rcalc"]:
            self.assertTrue(csv_safe(value).startswith("'"), value)

    def test_ordinary_values_are_untouched(self):
        self.assertEqual(csv_safe("Asha Rao"), "Asha Rao")
        self.assertEqual(csv_safe(42), 42)
        self.assertIsNone(csv_safe(None))


class Judge0StatusTests(TestCase):
    def setUp(self):
        self.runner = Judge0Runner("http://sandbox.invalid")

    def _status(self, status_id):
        payload = {"status": {"id": status_id}, "stdout": "", "time": "0.01", "memory": 100}
        with mock.patch.object(Judge0Runner, "_post", return_value=payload):
            return self.runner.run_case("python", "print(1)", "")

    def test_accepted_and_wrong_answer_are_graded_by_the_grader(self):
        self.assertEqual(self._status(3).status, "ok")
        self.assertEqual(self._status(4).status, "ok")

    def test_time_limit_and_compile_and_runtime_map_to_their_outcomes(self):
        self.assertEqual(self._status(5).status, "timeout")
        self.assertEqual(self._status(6).status, "compile_error")
        self.assertEqual(self._status(11).status, "runtime_error")

    def test_queued_processing_and_internal_errors_are_runner_failures(self):
        # Regression: queued or processing jobs were scored as wrong answers.
        for status_id in (None, 0, 1, 2, 13, 99):
            with self.subTest(status=status_id), self.assertRaises(RunnerError):
                self._status(status_id)


class EventInputTests(TestCase):
    def test_context_keeps_scalars_only_and_bounds_size(self):
        raw = {"a": "x" * 500, "n": float("nan"), "b": True, "obj": {"nested": 1}}
        cleaned = exam_api._clean_context(raw)
        self.assertEqual(len(cleaned["a"]), exam_api.MAX_CONTEXT_STRING)
        self.assertIsNone(cleaned["n"])
        self.assertTrue(cleaned["b"])
        self.assertNotIn("obj", cleaned)

    def test_context_key_count_is_capped(self):
        raw = {f"k{i}": i for i in range(100)}
        self.assertLessEqual(len(exam_api._clean_context(raw)), exam_api.MAX_CONTEXT_KEYS)

    def test_context_must_be_an_object(self):
        self.assertEqual(exam_api._clean_context(["not", "a", "dict"]), {})

    def test_nan_in_request_body_is_refused_as_bad_request(self):
        class FakeRequest:
            body = b'{"duration_ms": NaN}'

        with self.assertRaises(AttemptError) as ctx:
            exam_api._body(FakeRequest())
        self.assertEqual(ctx.exception.code, "bad_request")

    def test_non_object_json_is_refused(self):
        class FakeRequest:
            body = json.dumps([1, 2]).encode()

        with self.assertRaises(AttemptError):
            exam_api._body(FakeRequest())


class ExhaustedGradingTests(EngineFixture):
    def test_submission_that_keeps_failing_flags_the_attempt(self):
        # Regression: a sandbox that failed three times silently scored zero for the candidate.
        from .models import MAX_GRADING_ATTEMPTS, CodingSubmission
        from .tests_engine import FakeRunner

        attempt = self.start()
        submission = CodingSubmission.objects.create(
            attempt=attempt,
            problem=self.problems[0],
            language="python",
            source_code="print(1)",
            version=1,
            status=CodingSubmission.Status.QUEUED,
        )
        for _ in range(MAX_GRADING_ATTEMPTS):
            services.grade_submission(submission, runner=FakeRunner(fail=True))
        attempt.refresh_from_db()
        self.assertTrue(attempt.flagged_for_review)
