import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from . import services
from .models import (
    Answer,
    Application,
    Attempt,
    AttemptMCQ,
    AttemptSection,
    CodingSubmission,
    CodingTestCase,
    CodingTestResult,
    ConnectionEvent,
    Exam,
    ExamSection,
    MCQOption,
    MCQQuestion,
    ProctoringEvent,
    QuestionVisit,
)
from .runners import CaseOutcome, RunnerError
from .services import AttemptError, perform

User = get_user_model()
import tempfile

TMP_MEDIA = tempfile.mkdtemp(prefix="assessment-test-media-")


class FakeRunner:
    """Stands in for the sandbox. `behaviour` maps source text to the stdout it returns."""

    def __init__(self, outputs=None, compile_error=False, fail=False):
        self.outputs = outputs or {}
        self.compile_error = compile_error
        self.fail = fail
        self.calls = 0

    def run_case(self, language, source_code, stdin):
        self.calls += 1
        if self.fail:
            raise RunnerError("sandbox down")
        if self.compile_error:
            return CaseOutcome("compile_error", compile_output="SyntaxError")
        return CaseOutcome("ok", stdout=self.outputs.get(stdin, ""), time_ms=5, memory_kb=900)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class EngineFixture(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin", "a@example.com", "pw-12345-x", is_staff=True)
        self.candidate = User.objects.create_user("cand", "c@example.com", "pw-12345-x")
        self.other = User.objects.create_user("other", "o@example.com", "pw-12345-x")
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="Full test",
            instructions="Read the rules.",
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=3),
            duration_minutes=60,
            is_published=True,
            created_by=self.staff,
            camera_required=False,
            tab_switch_threshold=2,
            network_grace_seconds=20,
            max_pause_minutes=2,
        )
        self.mcq_section = ExamSection.objects.create(
            exam=self.exam, title="MCQ", kind="mcq", order=1, duration_minutes=10
        )
        self.coding_section = ExamSection.objects.create(
            exam=self.exam, title="Coding", kind="coding", order=2, duration_minutes=20, coding_selection_count=2
        )
        self.questions = []
        for i in range(3):
            q = MCQQuestion.objects.create(exam=self.exam, text=f"Question {i + 1}", marks=2, order=i + 1)
            options = [
                MCQOption.objects.create(question=q, text=f"Q{i + 1} option {j}", is_correct=(j == 0), order=j + 1)
                for j in range(4)
            ]
            self.questions.append((q, options))

        from .models import CodingProblem

        self.problems = []
        for i in range(3):
            problem = CodingProblem.objects.create(
                exam=self.exam,
                title=f"Problem {i + 1}",
                statement="Add numbers.",
                languages="python",
                marks=10,
                order=i + 1,
                starter_code="# write here",
            )
            CodingTestCase.objects.create(problem=problem, input_data="1 2", expected_output="3", order=1)
            CodingTestCase.objects.create(problem=problem, input_data="5 5", expected_output="10", order=2)
            CodingTestCase.objects.create(problem=problem, input_data="9 9", expected_output="18", is_hidden=True, order=3)
            self.problems.append(problem)

        self.application = Application.objects.create(
            candidate=self.candidate,
            exam=self.exam,
            full_name="Cand",
            status=Application.Status.APPROVED,
            details_confirmed_at=now,
        )

    def start(self, user=None):
        return services.start_attempt(user or self.candidate, self.exam)

    def grant_consent(self):
        Attempt.objects.filter(pk=self.attempt.pk).update(snapshot_consent_at=timezone.now())

    def set_section_deadline(self, attempt, kind, deadline):
        AttemptSection.objects.filter(attempt=attempt, section__kind=kind).update(deadline_at=deadline)


class StartAndAssignmentTests(EngineFixture):
    def test_unapproved_candidate_cannot_start(self):
        self.application.status = Application.Status.REVOKED
        self.application.save()
        with self.assertRaises(AttemptError) as ctx:
            self.start()
        self.assertEqual(ctx.exception.code, "not_approved")

    def test_start_creates_sections_with_first_active(self):
        attempt = self.start()
        statuses = list(attempt.sections.order_by("section__order").values_list("status", flat=True))
        self.assertEqual(statuses, ["in_progress", "locked"])

    def test_start_is_idempotent(self):
        first = self.start()
        second = self.start()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_deadline_is_clamped_to_exam_close(self):
        self.exam.closes_at = timezone.now() + timedelta(minutes=5)
        self.exam.save()
        attempt = self.start()
        self.assertLessEqual(attempt.deadline_at, self.exam.closes_at)

    def test_mcq_assignment_is_fixed_and_contains_all_options(self):
        attempt = self.start()
        items = list(AttemptMCQ.objects.filter(attempt=attempt))
        self.assertEqual(len(items), 3)
        for item in items:
            expected = set(item.question.options.values_list("pk", flat=True))
            self.assertEqual(set(item.option_order), expected)
        stored = list(AttemptMCQ.objects.filter(attempt=attempt).values_list("pk", "option_order"))
        services.touch(attempt, timezone.now())
        self.assertEqual(stored, list(AttemptMCQ.objects.filter(attempt=attempt).values_list("pk", "option_order")))

    def test_coding_selection_is_two_distinct_problems(self):
        attempt = self.start()
        self.set_section_deadline(attempt, "mcq", timezone.now() - timedelta(seconds=1))
        perform(self.candidate, attempt.pk, lambda a, now: services.touch(a, now))
        chosen = list(attempt.coding_items.values_list("problem_id", flat=True))
        self.assertEqual(len(chosen), 2)
        self.assertEqual(len(set(chosen)), 2)

    def test_misconfigured_exam_refuses_to_start(self):
        self.coding_section.coding_selection_count = 5
        self.coding_section.save()
        with self.assertRaises(AttemptError) as ctx:
            self.start()
        self.assertEqual(ctx.exception.code, "misconfigured")

    def test_policy_snapshot_is_frozen_at_start(self):
        attempt = self.start()
        self.exam.tab_switch_threshold = 99
        self.exam.save()
        attempt.refresh_from_db()
        self.assertEqual(attempt.policy_snapshot["tab_switch_threshold"], 2)


class AnswerAndVisitTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.attempt = self.start()

    def _mcq_item(self, index=0):
        return AttemptMCQ.objects.filter(attempt=self.attempt).order_by("position")[index]

    def test_answer_saved_and_changes_counted(self):
        item = self._mcq_item()
        first_option = item.question.options.first()
        other = item.question.options.exclude(pk=first_option.pk).first()
        perform(self.candidate, self.attempt.pk, lambda a, now: services.save_answer(a, now, item.question_id, first_option.pk))
        perform(self.candidate, self.attempt.pk, lambda a, now: services.save_answer(a, now, item.question_id, other.pk))
        answer = Answer.objects.get(attempt=self.attempt, question_id=item.question_id)
        self.assertEqual(answer.option_id, other.pk)
        self.assertEqual(answer.change_count, 1)

    def test_answer_for_unassigned_question_is_rejected(self):
        outside = MCQQuestion.objects.create(exam=self.exam, text="Not assigned", marks=1, order=99)
        option = MCQOption.objects.create(question=outside, text="x", is_correct=True)
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, self.attempt.pk, lambda a, now: services.save_answer(a, now, outside.pk, option.pk))
        self.assertEqual(ctx.exception.code, "not_assigned")

    def test_option_from_another_question_is_rejected(self):
        item = self._mcq_item()
        foreign = MCQOption.objects.filter(question__exam=self.exam).exclude(question_id=item.question_id).first()
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, self.attempt.pk, lambda a, now: services.save_answer(a, now, item.question_id, foreign.pk))
        self.assertIn(ctx.exception.code, ("invalid_option", "not_found"))

    def test_coding_actions_rejected_while_mcq_section_is_open(self):
        problem = self.problems[0]
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, self.attempt.pk, lambda a, now: services.save_draft(a, now, problem.pk, "python", "x"))
        self.assertEqual(ctx.exception.code, "wrong_section")

    def test_visit_time_excludes_other_questions(self):
        first = self._mcq_item(0).question_id
        second = self._mcq_item(1).question_id
        t0 = timezone.now()
        with mock.patch("assessments.services.timezone.now", return_value=t0):
            perform(self.candidate, self.attempt.pk, lambda a, now: services.open_visit(a, now, "mcq", first))
        with mock.patch("assessments.services.timezone.now", return_value=t0 + timedelta(seconds=20)):
            perform(self.candidate, self.attempt.pk, lambda a, now: services.open_visit(a, now, "mcq", second))
        with mock.patch("assessments.services.timezone.now", return_value=t0 + timedelta(minutes=6)):
            perform(self.candidate, self.attempt.pk, lambda a, now: services.open_visit(a, now, "mcq", first))
        with mock.patch("assessments.services.timezone.now", return_value=t0 + timedelta(minutes=6, seconds=10)):
            perform(self.candidate, self.attempt.pk, lambda a, now: services.open_visit(a, now, "mcq", second))
        visits = list(QuestionVisit.objects.filter(attempt=self.attempt, mcq_question_id=first))
        # First visit: 20 s; second visit (opened at 6 min, closed at 6 min 10 s): 10 s.
        self.assertEqual([v.active_seconds for v in visits], [20, 10])
        self.assertEqual(visits[1].visit_number, 2)
        self.assertEqual(QuestionVisit.objects.filter(attempt=self.attempt, mcq_question_id=first).count(), 2)


class TimeRuleTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.attempt = self.start()

    def test_section_expiry_advances_and_locks_previous(self):
        self.set_section_deadline(self.attempt, "mcq", timezone.now() - timedelta(seconds=1))
        perform(self.candidate, self.attempt.pk, lambda a, now: None)
        self.attempt.refresh_from_db()
        statuses = dict(self.attempt.sections.values_list("section__kind", "status"))
        self.assertEqual(statuses["mcq"], "expired")
        self.assertEqual(statuses["coding"], "in_progress")

    def test_answers_after_expiry_are_refused(self):
        item = AttemptMCQ.objects.filter(attempt=self.attempt).first()
        option = item.question.options.first()
        self.set_section_deadline(self.attempt, "mcq", timezone.now() - timedelta(seconds=1))
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, self.attempt.pk, lambda a, now: services.save_answer(a, now, item.question_id, option.pk))
        self.assertIn(ctx.exception.code, ("wrong_section", "no_open_section"))

    def test_submit_section_locks_and_scores_on_finish(self):
        item = AttemptMCQ.objects.filter(attempt=self.attempt).order_by("position").first()
        correct = item.question.options.get(is_correct=True)
        perform(self.candidate, self.attempt.pk, lambda a, now: services.save_answer(a, now, item.question_id, correct.pk))
        section_id = self.mcq_section.pk
        perform(self.candidate, self.attempt.pk, lambda a, now: services.submit_section(a, now, section_id))
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.sections.get(section__kind="mcq").status, "submitted")
        self.assertEqual(self.attempt.sections.get(section__kind="coding").status, "in_progress")

    def test_full_submission_finalizes_with_scores(self):
        section_id = self.mcq_section.pk
        perform(self.candidate, self.attempt.pk, lambda a, now: services.submit_section(a, now, section_id))
        coding_id = self.coding_section.pk
        perform(self.candidate, self.attempt.pk, lambda a, now: services.submit_section(a, now, coding_id))
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, Attempt.Status.SUBMITTED)
        self.assertIsNotNone(self.attempt.ended_at)
        self.assertEqual(float(self.attempt.mcq_score), 0)

    def test_overall_deadline_expires_attempt(self):
        self.attempt.deadline_at = timezone.now() - timedelta(seconds=1)
        self.attempt.save()
        perform(self.candidate, self.attempt.pk, lambda a, now: None)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, Attempt.Status.EXPIRED)

    def test_disconnection_extends_deadline_up_to_allowance(self):
        original = self.attempt.deadline_at
        self.attempt.last_seen_at = timezone.now() - timedelta(seconds=100)
        self.attempt.save()
        perform(self.candidate, self.attempt.pk, lambda a, now: None)
        self.attempt.refresh_from_db()
        event = ConnectionEvent.objects.get(attempt=self.attempt)
        self.assertGreaterEqual(event.gap_seconds, 99)
        self.assertGreater(event.granted_seconds, 0)
        self.assertEqual(event.denied_seconds, 0)
        self.assertGreater(self.attempt.deadline_at, original)

    def test_pause_allowance_is_capped(self):
        self.attempt.last_seen_at = timezone.now() - timedelta(hours=1)
        self.attempt.save()
        perform(self.candidate, self.attempt.pk, lambda a, now: None)
        self.attempt.refresh_from_db()
        event = ConnectionEvent.objects.get(attempt=self.attempt)
        self.assertEqual(event.granted_seconds, 120)  # max_pause_minutes=2
        self.assertGreater(event.denied_seconds, 0)
        self.assertEqual(self.attempt.total_paused_seconds, 120)

    def test_pause_disabled_grants_no_time(self):
        self.exam.pause_enabled = False
        self.exam.save()
        self.attempt.policy_snapshot = {**self.attempt.policy_snapshot, "pause_enabled": False}
        self.attempt.last_seen_at = timezone.now() - timedelta(seconds=90)
        self.attempt.save()
        original = self.attempt.deadline_at
        perform(self.candidate, self.attempt.pk, lambda a, now: None)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.deadline_at, original)

    def test_reconnect_requires_camera_check_when_required(self):
        self.attempt.policy_snapshot = {**self.attempt.policy_snapshot, "camera_required": True}
        self.attempt.readiness_confirmed_at = timezone.now()
        self.attempt.last_seen_at = timezone.now() - timedelta(seconds=90)
        self.attempt.save()
        perform(self.candidate, self.attempt.pk, lambda a, now: None)
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.readiness_confirmed_at)


class ProctoringTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.attempt = self.start()

    def test_tab_switches_over_threshold_flag_attempt_not_eligibility(self):
        for _ in range(3):
            perform(
                self.candidate, self.attempt.pk,
                lambda a, now: services.record_event(a, now, "tab_hidden", None, 1200, {}),
            )
        self.attempt.refresh_from_db()
        self.assertTrue(self.attempt.flagged_for_review)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.Status.APPROVED)

    def test_events_have_sequential_numbers(self):
        for kind in ["tab_hidden", "window_blur", "head_turn"]:
            perform(self.candidate, self.attempt.pk, lambda a, now, k=kind: services.record_event(a, now, k, None, 0, {}))
        sequences = list(ProctoringEvent.objects.filter(attempt=self.attempt).values_list("sequence", flat=True))
        self.assertEqual(sequences, [1, 2, 3])

    def test_snapshot_rejects_non_image_bytes(self):
        self.grant_consent()
        upload = SimpleUploadedFile("x.jpg", b"not an image at all", content_type="image/jpeg")
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, self.attempt.pk, lambda a, now: services.store_snapshot(a, now, upload))
        self.assertEqual(ctx.exception.code, "bad_image")

    def test_snapshot_accepts_jpeg_and_stores_private_hash(self):
        self.grant_consent()
        data = b"\xff\xd8\xff" + b"0" * 200
        upload = SimpleUploadedFile("x.jpg", data, content_type="image/jpeg")
        snapshot = perform(self.candidate, self.attempt.pk, lambda a, now: services.store_snapshot(a, now, upload))
        self.assertEqual(snapshot.content_type, "image/jpeg")
        self.assertEqual(len(snapshot.sha256), 64)
        path = services.snapshot_file_path(snapshot)
        self.assertTrue(path.exists())
        self.assertNotIn("..", snapshot.relative_path)

    @override_settings(SNAPSHOT_MAX_BYTES=50)
    def test_snapshot_size_limit(self):
        self.grant_consent()
        upload = SimpleUploadedFile("x.jpg", b"\xff\xd8\xff" + b"0" * 200, content_type="image/jpeg")
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, self.attempt.pk, lambda a, now: services.store_snapshot(a, now, upload))
        self.assertEqual(ctx.exception.code, "too_large")


class CodingTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.attempt = self.start()
        section_id = self.mcq_section.pk
        perform(self.candidate, self.attempt.pk, lambda a, now: services.submit_section(a, now, section_id))
        self.assigned = list(self.attempt.coding_items.order_by("position").values_list("problem_id", flat=True))

    def test_candidate_state_hides_hidden_cases(self):
        state = perform(self.candidate, self.attempt.pk, lambda a, now: services.candidate_state(a, now))
        for problem in state["problems"]:
            inputs = [case["input"] for case in problem["public_cases"]]
            self.assertNotIn("9 9", inputs)
            self.assertEqual(len(problem["public_cases"]), 2)
        # Check the hidden expected output as a JSON value. A bare substring search also matches
        # timestamps and ids, which made this test fail at random.
        self.assertNotIn('"18"', json.dumps(state))
        self.assertNotIn('"expected": "18"', json.dumps(state))

    def test_candidate_state_excludes_locked_section_content(self):
        state = perform(self.candidate, self.attempt.pk, lambda a, now: services.candidate_state(a, now))
        self.assertEqual(state["current_section"]["kind"], "coding")
        self.assertEqual(state["questions"], [])

    def test_submit_code_then_grade_with_fake_runner(self):
        problem_id = self.assigned[0]
        submission = perform(
            self.candidate, self.attempt.pk,
            lambda a, now: services.submit_code(a, now, problem_id, "python", "print(3)"),
        )
        runner = FakeRunner(outputs={"1 2": "3", "5 5": "10", "9 9": "18"})
        submission = services.grade_submission(submission, runner=runner)
        self.assertEqual(submission.status, CodingSubmission.Status.COMPLETED)
        self.assertEqual(submission.total_cases, 3)
        self.assertEqual(submission.public_passed, 2)
        self.assertEqual(submission.hidden_passed, 1)
        self.assertEqual(float(submission.marks_awarded), 10.0)

    def test_wrong_answers_score_partially(self):
        problem_id = self.assigned[0]
        submission = perform(
            self.candidate, self.attempt.pk,
            lambda a, now: services.submit_code(a, now, problem_id, "python", "print(0)"),
        )
        runner = FakeRunner(outputs={"1 2": "3"})
        submission = services.grade_submission(submission, runner=runner)
        self.assertEqual(submission.public_passed, 1)
        self.assertEqual(float(submission.marks_awarded), round(10 * 1 / 3, 2))

    def test_compile_error_marks_zero_and_skips_rest(self):
        problem_id = self.assigned[0]
        submission = perform(
            self.candidate, self.attempt.pk,
            lambda a, now: services.submit_code(a, now, problem_id, "python", "def("),
        )
        submission = services.grade_submission(submission, runner=FakeRunner(compile_error=True))
        statuses = list(submission.results.values_list("status", flat=True))
        self.assertEqual(statuses[0], CodingTestResult.Status.COMPILE_ERROR)
        self.assertTrue(all(s in ("compile_error", "skipped") for s in statuses))
        self.assertEqual(float(submission.marks_awarded), 0)

    def test_runner_outage_is_recorded_not_scored(self):
        problem_id = self.assigned[0]
        submission = perform(
            self.candidate, self.attempt.pk,
            lambda a, now: services.submit_code(a, now, problem_id, "python", "print(3)"),
        )
        submission = services.grade_submission(submission, runner=FakeRunner(fail=True))
        self.assertEqual(submission.status, CodingSubmission.Status.RUNNER_ERROR)
        self.assertEqual(submission.results.count(), 0)

    def test_submission_is_final_and_locks_the_problem(self):
        problem_id = self.assigned[0]
        first = perform(self.candidate, self.attempt.pk, lambda a, now: services.submit_code(a, now, problem_id, "python", "a"))
        self.assertTrue(first.is_final)
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, self.attempt.pk, lambda a, now: services.submit_code(a, now, problem_id, "python", "b"))
        self.assertEqual(ctx.exception.code, "locked")
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, self.attempt.pk, lambda a, now: services.save_draft(a, now, problem_id, "python", "c"))
        self.assertEqual(ctx.exception.code, "locked")

    def test_unsupported_language_is_refused(self):
        problem_id = self.assigned[0]
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, self.attempt.pk, lambda a, now: services.submit_code(a, now, problem_id, "cobol", "x"))
        self.assertEqual(ctx.exception.code, "bad_language")

    def test_unassigned_problem_is_refused(self):
        assigned = set(self.assigned)
        outside = next(p for p in self.problems if p.pk not in assigned)
        with self.assertRaises(AttemptError) as ctx:
            perform(self.candidate, self.attempt.pk, lambda a, now: services.submit_code(a, now, outside.pk, "python", "x"))
        self.assertEqual(ctx.exception.code, "not_assigned")

    def test_autosaved_draft_is_queued_when_section_ends(self):
        problem_id = self.assigned[0]
        perform(self.candidate, self.attempt.pk, lambda a, now: services.save_draft(a, now, problem_id, "python", "print(3)"))
        section_id = self.coding_section.pk
        perform(self.candidate, self.attempt.pk, lambda a, now: services.submit_section(a, now, section_id))
        queued = CodingSubmission.objects.filter(attempt=self.attempt, status=CodingSubmission.Status.QUEUED)
        self.assertEqual(queued.count(), 1)
        self.assertEqual(queued.first().source_code, "print(3)")

    def test_grade_queued_processes_backlog(self):
        problem_id = self.assigned[0]
        perform(self.candidate, self.attempt.pk, lambda a, now: services.submit_code(a, now, problem_id, "python", "print(3)"))
        with mock.patch("assessments.services.get_runner", return_value=FakeRunner(outputs={"1 2": "3", "5 5": "10", "9 9": "18"})):
            graded = services.grade_queued()
        self.assertEqual(graded, 1)
        self.assertFalse(CodingSubmission.objects.filter(status=CodingSubmission.Status.QUEUED).exists())


class IsolationTests(EngineFixture):
    def test_other_candidate_cannot_touch_attempt(self):
        attempt = self.start()
        with self.assertRaises(AttemptError) as ctx:
            perform(self.other, attempt.pk, lambda a, now: None)
        self.assertEqual(ctx.exception.code, "not_found")
