import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import services
from .models import (
    Application,
    Attempt,
    CodingProblem,
    CodingTestCase,
    Exam,
    ExamSection,
    InterviewReview,
    MCQOption,
    MCQQuestion,
    SiteSettings,
)
from .services import perform
from .tests_engine import TMP_MEDIA, FakeRunner

User = get_user_model()


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class CandidateFlowViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin", "a@example.com", "pw-12345-x", is_staff=True)
        self.candidate = User.objects.create_user("cand", "c@example.com", "pw-12345-x")
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="View test",
            instructions="Be honest.",
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=2),
            duration_minutes=30,
            is_published=True,
            created_by=self.staff,
            camera_required=False,
        )
        ExamSection.objects.create(exam=self.exam, title="MCQ", kind="mcq", order=1, duration_minutes=10)
        ExamSection.objects.create(exam=self.exam, title="Code", kind="coding", order=2, duration_minutes=15, coding_selection_count=1)
        q = MCQQuestion.objects.create(exam=self.exam, text="2+2?", marks=2, order=1)
        self.correct = MCQOption.objects.create(question=q, text="4", is_correct=True, order=1)
        MCQOption.objects.create(question=q, text="5", is_correct=False, order=2)
        self.question = q
        problem = CodingProblem.objects.create(exam=self.exam, title="Add", statement="Add", languages="python", marks=10, order=1)
        CodingTestCase.objects.create(problem=problem, input_data="1 2", expected_output="3", order=1)
        CodingTestCase.objects.create(problem=problem, input_data="9 9", expected_output="18", is_hidden=True, order=2)
        self.problem = problem
        self.application = Application.objects.create(
            candidate=self.candidate,
            exam=self.exam,
            full_name="Cand Name",
            status=Application.Status.APPROVED,
            details_confirmed_at=timezone.now(),
        )
        self.client.force_login(self.candidate)

    def test_start_then_state_then_answer_then_submit(self):
        response = self.client.post(reverse("start_attempt", args=[self.exam.pk]))
        attempt = Attempt.objects.get(candidate=self.candidate)
        self.assertRedirects(response, reverse("attempt_page", args=[attempt.pk]))

        page = self.client.get(reverse("attempt_page", args=[attempt.pk]))
        self.assertContains(page, 'id="attempt-root"')

        state = self.client.get(reverse("api_state", args=[attempt.pk])).json()
        self.assertEqual(state["current_section"]["kind"], "mcq")
        self.assertEqual(len(state["questions"]), 1)
        self.assertNotIn("is_correct", json.dumps(state))

        option = state["questions"][0]["options"][0]["id"]
        response = self.client.put(
            reverse("api_answer", args=[attempt.pk]),
            data=json.dumps({"question_id": self.question.pk, "option_id": option}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        visit = self.client.post(
            reverse("api_visit", args=[attempt.pk]),
            data=json.dumps({"kind": "mcq", "item_id": self.question.pk}),
            content_type="application/json",
        )
        self.assertEqual(visit.status_code, 200)

        submit = self.client.post(
            reverse("api_submit_section", args=[attempt.pk]),
            data=json.dumps({"section_id": state["current_section"]["id"]}),
            content_type="application/json",
        )
        self.assertEqual(submit.status_code, 200)
        state = self.client.get(reverse("api_state", args=[attempt.pk])).json()
        self.assertEqual(state["current_section"]["kind"], "coding")

    def test_code_submission_returns_public_results_only(self):
        self.client.post(reverse("start_attempt", args=[self.exam.pk]))
        attempt = Attempt.objects.get(candidate=self.candidate)
        section_id = attempt.sections.get(section__kind="mcq").section_id
        perform(self.candidate, attempt.pk, lambda a, now: services.submit_section(a, now, section_id))
        from unittest import mock

        real_grade = services.grade_submission
        runner = FakeRunner(outputs={"1 2": "3", "9 9": "18"})
        with mock.patch("assessments.services.grade_submission",
                        side_effect=lambda sub: real_grade(sub, runner=runner)):
            response = self.client.post(
                reverse("api_code_submit", args=[attempt.pk]),
                data=json.dumps({"problem_id": self.problem.pk, "language": "python", "code": "print(3)"}),
                content_type="application/json",
            )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["submission"]["final"])
        # Candidates never see case results, counts or marks after submitting.
        for leaked in ("public_results", "hidden_passed", "public_passed", "marks_awarded", "total_cases"):
            self.assertNotIn(leaked, body["submission"])

    def test_api_rejects_staff_and_anonymous(self):
        self.client.logout()
        response = self.client.get(reverse("api_state", args=[1]))
        self.assertEqual(response.status_code, 401)

    def test_candidate_cannot_open_another_candidates_attempt(self):
        self.client.post(reverse("start_attempt", args=[self.exam.pk]))
        attempt = Attempt.objects.get(candidate=self.candidate)
        other = User.objects.create_user("other", "o@example.com", "pw-12345-x")
        self.client.force_login(other)
        response = self.client.get(reverse("attempt_page", args=[attempt.pk]))
        self.assertEqual(response.status_code, 404)

    def test_bad_json_is_a_400_not_a_crash(self):
        self.client.post(reverse("start_attempt", args=[self.exam.pk]))
        attempt = Attempt.objects.get(candidate=self.candidate)
        response = self.client.put(
            reverse("api_answer", args=[attempt.pk]), data="{not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_event_endpoint_validates_kind(self):
        self.client.post(reverse("start_attempt", args=[self.exam.pk]))
        attempt = Attempt.objects.get(candidate=self.candidate)
        response = self.client.post(
            reverse("api_event", args=[attempt.pk]),
            data=json.dumps({"kind": "teleport"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        ok = self.client.post(
            reverse("api_event", args=[attempt.pk]),
            data=json.dumps({"kind": "tab_hidden", "duration_ms": 1500, "client_at": timezone.now().isoformat()}),
            content_type="application/json",
        )
        self.assertEqual(ok.status_code, 200)
        self.assertIn("event_id", ok.json())


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class StaffViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin", "a@example.com", "pw-12345-x", is_staff=True)
        self.candidate = User.objects.create_user("cand", "c@example.com", "pw-12345-x")
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="Staff view test",
            instructions="Rules",
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=2),
            duration_minutes=30,
            is_published=True,
            created_by=self.staff,
            camera_required=False,
        )
        self.application = Application.objects.create(
            candidate=self.candidate,
            exam=self.exam,
            full_name="Cand Name",
            status=Application.Status.APPROVED,
            details_confirmed_at=timezone.now(),
        )
        self.client.force_login(self.staff)

    def _attempt_with_data(self):
        ExamSection.objects.create(exam=self.exam, title="MCQ", kind="mcq", order=1, duration_minutes=10)
        q = MCQQuestion.objects.create(exam=self.exam, text="Pick", marks=1, order=1)
        MCQOption.objects.create(question=q, text="Right", is_correct=True, order=1)
        MCQOption.objects.create(question=q, text="Wrong", is_correct=False, order=2)
        attempt = services.start_attempt(self.candidate, self.exam)
        option = q.options.get(is_correct=True)
        perform(self.candidate, attempt.pk, lambda a, now: services.save_answer(a, now, q.pk, option.pk))
        perform(self.candidate, attempt.pk, lambda a, now: services.record_event(a, now, "tab_hidden", None, 900, {}))
        return attempt

    def test_overview_candidates_and_export_render(self):
        attempt = self._attempt_with_data()
        self.assertEqual(self.client.get(reverse("staff_exam_overview", args=[self.exam.pk])).status_code, 200)
        response = self.client.get(reverse("staff_exam_candidates", args=[self.exam.pk]), {"q": "cand"})
        self.assertContains(response, "Cand Name")
        self.assertContains(response, reverse("staff_attempt_review", args=[attempt.pk]))
        csv_response = self.client.get(reverse("staff_export_csv", args=[self.exam.pk]))
        self.assertEqual(csv_response["Content-Type"], "text/csv")
        self.assertIn(b"Cand Name", csv_response.content)

    def test_review_page_renders_answers_and_timeline(self):
        attempt = self._attempt_with_data()
        response = self.client.get(reverse("staff_attempt_review", args=[attempt.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Right")
        self.assertContains(response, "Tab/window switches")
        self.assertContains(response, "Tab hidden")

    def test_decision_records_review_and_audit(self):
        from .models import AuditLog

        attempt = self._attempt_with_data()
        response = self.client.post(
            reverse("staff_attempt_decision", args=[attempt.pk]),
            {"decision": InterviewReview.Decision.RECOMMENDED, "notes": "Solid answers."},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InterviewReview.objects.get(attempt=attempt).reviewer, self.staff)
        self.assertTrue(AuditLog.objects.filter(action="interview.decision").exists())

    def test_snapshot_served_only_to_staff_and_logged(self):
        from .models import AuditLog

        attempt = self._attempt_with_data()
        data = b"\xff\xd8\xff" + b"0" * 64
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("s.jpg", data, content_type="image/jpeg")
        Attempt.objects.filter(pk=attempt.pk).update(snapshot_consent_at=timezone.now())
        snapshot = perform(self.candidate, attempt.pk, lambda a, now: services.store_snapshot(a, now, upload))

        response = self.client.get(reverse("staff_snapshot", args=[snapshot.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        response.close()
        self.assertTrue(AuditLog.objects.filter(action="snapshot.viewed").exists())

        self.client.force_login(self.candidate)
        self.assertEqual(self.client.get(reverse("staff_snapshot", args=[snapshot.pk])).status_code, 302)

    def test_candidate_result_page_hides_scores_until_visible(self):
        attempt = self._attempt_with_data()
        self.client.force_login(self.candidate)
        perform(self.candidate, attempt.pk, lambda a, now: services.submit_section(a, now, attempt.sections.first().section_id))
        response = self.client.get(reverse("attempt_result", args=[attempt.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Multiple-choice score")

    def test_audit_log_renders(self):
        self._attempt_with_data()
        response = self.client.get(reverse("staff_audit_log"))
        self.assertEqual(response.status_code, 200)

    def test_staff_section_edit_and_delete(self):
        section = ExamSection.objects.create(
            exam=self.exam, title="Initial Section", kind="mcq", order=1, duration_minutes=20
        )
        edit_url = reverse("staff_section_edit", args=[section.pk])
        # GET edit page
        resp = self.client.get(edit_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Edit Section")
        # POST update
        post_resp = self.client.post(edit_url, {
            "title": "Updated Section Title",
            "kind": section.kind,
            "order": section.order,
            "duration_minutes": 45,
            "question_count": 0,
            "coding_selection_count": 1,
        })
        self.assertEqual(post_resp.status_code, 302)
        section.refresh_from_db()
        self.assertEqual(section.title, "Updated Section Title")
        self.assertEqual(section.duration_minutes, 45)

        # POST delete
        del_url = reverse("staff_section_delete", args=[section.pk])
        del_resp = self.client.post(del_url)
        self.assertEqual(del_resp.status_code, 302)
        self.assertFalse(ExamSection.objects.filter(pk=section.pk).exists())

    def test_staff_mcq_edit_and_delete(self):
        question = MCQQuestion.objects.create(exam=self.exam, text="Initial question?", marks=2, order=1)
        opt1 = MCQOption.objects.create(question=question, text="Option 1", is_correct=True, order=1)
        opt2 = MCQOption.objects.create(question=question, text="Option 2", is_correct=False, order=2)

        edit_url = reverse("staff_mcq_edit", args=[question.pk])
        resp = self.client.get(edit_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Edit question")

        # Update MCQ
        post_data = {
            "text": "What is 3+3?",
            "marks": 5,
            "order": 1,
            "options-TOTAL_FORMS": "2",
            "options-INITIAL_FORMS": "2",
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            "options-0-id": str(opt1.pk),
            "options-0-text": "6",
            "options-0-is_correct": "on",
            "options-1-id": str(opt2.pk),
            "options-1-text": "7",
        }
        post_resp = self.client.post(edit_url, post_data)
        self.assertEqual(post_resp.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.text, "What is 3+3?")
        self.assertEqual(question.marks, 5)

        # Delete MCQ
        del_url = reverse("staff_mcq_delete", args=[question.pk])
        del_resp = self.client.post(del_url)
        self.assertEqual(del_resp.status_code, 302)
        self.assertFalse(MCQQuestion.objects.filter(pk=question.pk).exists())

    def test_staff_coding_edit_and_delete(self):
        problem = CodingProblem.objects.create(
            exam=self.exam, title="Initial Problem", statement="Initial Statement", languages="python", marks=10, order=1
        )
        case1 = CodingTestCase.objects.create(problem=problem, input_data="1", expected_output="1", order=1)
        case2 = CodingTestCase.objects.create(problem=problem, input_data="2", expected_output="2", is_hidden=True, order=2)

        edit_url = reverse("staff_coding_edit", args=[problem.pk])
        resp = self.client.get(edit_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Edit coding problem")

        post_data = {
            "title": "Multiply Numbers",
            "statement": "Multiply two ints",
            "languages": "python, java, c, cpp",
            "marks": 20,
            "order": 1,
            "cases-TOTAL_FORMS": "2",
            "cases-INITIAL_FORMS": "2",
            "cases-MIN_NUM_FORMS": "0",
            "cases-MAX_NUM_FORMS": "1000",
            "cases-0-id": str(case1.pk),
            "cases-0-input_data": "2 3",
            "cases-0-expected_output": "6",
            "cases-1-id": str(case2.pk),
            "cases-1-input_data": "4 5",
            "cases-1-expected_output": "20",
            "cases-1-is_hidden": "on",
        }
        post_resp = self.client.post(edit_url, post_data)
        self.assertEqual(post_resp.status_code, 302)
        problem.refresh_from_db()
        self.assertEqual(problem.title, "Multiply Numbers")
        self.assertIn("java", problem.languages)

        # Delete problem
        del_url = reverse("staff_coding_delete", args=[problem.pk])
        del_resp = self.client.post(del_url)
        self.assertEqual(del_resp.status_code, 302)
        self.assertFalse(CodingProblem.objects.filter(pk=problem.pk).exists())

    def test_staff_exam_publish_override_warnings(self):
        # Create draft exam with incomplete sections / questions
        draft = Exam.objects.create(
            title="Draft Exam with warnings",
            opens_at=timezone.now(),
            closes_at=timezone.now() + timedelta(hours=1),
            duration_minutes=30,
            is_published=False,
            created_by=self.staff,
        )
        # Even with warnings, publish with override sets is_published = True
        pub_url = reverse("staff_exam_publish", args=[draft.pk])
        resp = self.client.post(pub_url, {"override": "true"})
        self.assertEqual(resp.status_code, 302)
        draft.refresh_from_db()
        self.assertTrue(draft.is_published)

    def test_branding_settings_and_serve(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        settings_url = reverse("staff_settings")
        resp = self.client.get(settings_url)
        self.assertEqual(resp.status_code, 200)

        logo_file = SimpleUploadedFile("logo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, content_type="image/png")
        post_resp = self.client.post(settings_url, {
            "site_name": "Scrambler Academy",
            "snapshot_retention_days": 30,
            "site_logo": logo_file,
        })
        self.assertEqual(post_resp.status_code, 302)
        site = SiteSettings.load()
        self.assertEqual(site.site_name, "Scrambler Academy")
        self.assertTrue(bool(site.site_logo))

        # Test serve_branding
        serve_url = reverse("serve_branding", args=["site_logo"])
        serve_resp = self.client.get(serve_url)
        self.assertEqual(serve_resp.status_code, 200)
        self.assertEqual(serve_resp["Content-Type"], "image/png")
        serve_resp.close()

    def test_settings_test_email_action(self):
        settings_url = reverse("staff_settings")
        resp = self.client.post(settings_url, {
            "action": "test_email",
            "test_recipient": "admin@example.com",
        })
        self.assertEqual(resp.status_code, 302)


