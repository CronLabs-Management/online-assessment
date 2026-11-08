"""Code history, language switches, new proctoring signals and the integrity summary."""

import json
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from . import services, tests_views
from .models import Attempt, CodingActivity, ProctoringEvent
from .review import build_attempt_review
from .services import perform
from .tests_engine import TMP_MEDIA, FakeRunner


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class TrackingTests(TestCase):
    """Reuses the candidate flow fixture: one MCQ section, then one coding section."""

    setUp = tests_views.CandidateFlowViewTests.setUp

    def _coding_attempt(self):
        self.client.post(reverse("start_attempt", args=[self.exam.pk]))
        attempt = Attempt.objects.get(candidate=self.candidate)
        section_id = attempt.sections.get(section__kind="mcq").section_id
        perform(self.candidate, attempt.pk, lambda a, now: services.submit_section(a, now, section_id))
        return attempt

    def _post(self, attempt, path, payload):
        send = self.client.put if path == "api_draft" else self.client.post
        return send(reverse(path, args=[attempt.pk]), data=json.dumps(payload), content_type="application/json")

    def test_each_run_is_kept_with_its_result(self):
        attempt = self._coding_attempt()
        with mock.patch("assessments.services.get_runner", return_value=FakeRunner(outputs={"1 2": "3"})):
            self._post(attempt, "api_code_run", {"problem_id": self.problem.pk, "language": "python", "code": "print(3)"})
            self._post(attempt, "api_code_run", {"problem_id": self.problem.pk, "language": "python", "code": "print(4)"})
        runs = list(CodingActivity.objects.filter(attempt=attempt, kind="run"))
        self.assertEqual([r.source_code for r in runs], ["print(3)", "print(4)"])
        self.assertEqual(runs[0].public_total, 1)

    def test_language_switch_is_logged(self):
        self.problem.languages = "python, c"
        self.problem.save()
        attempt = self._coding_attempt()
        self._post(attempt, "api_draft", {"problem_id": self.problem.pk, "language": "python", "code": "print(1)"})
        self._post(attempt, "api_draft", {"problem_id": self.problem.pk, "language": "c", "code": "int main(){}"})
        switch = CodingActivity.objects.get(attempt=attempt, kind="language_switch")
        self.assertEqual((switch.previous_language, switch.language), ("python", "c"))
        self.assertEqual(switch.source_code, "print(1)")

    def test_large_code_jump_without_paste_is_recorded(self):
        attempt = self._coding_attempt()
        self._post(attempt, "api_draft", {"problem_id": self.problem.pk, "language": "python", "code": "x = 1"})
        self._post(attempt, "api_draft", {"problem_id": self.problem.pk, "language": "python", "code": "x = 1\n" + "y = 2\n" * 80})
        self.assertTrue(ProctoringEvent.objects.filter(attempt=attempt, kind="large_code_change").exists())

    def test_large_code_jump_after_paste_is_not_double_counted(self):
        attempt = self._coding_attempt()
        self._post(attempt, "api_draft", {"problem_id": self.problem.pk, "language": "python", "code": "x = 1"})
        self._post(attempt, "api_event", {"kind": "clipboard", "duration_ms": 0, "context": {"action": "paste", "chars": 480}})
        self._post(attempt, "api_draft", {"problem_id": self.problem.pk, "language": "python", "code": "x = 1\n" + "y = 2\n" * 80})
        self.assertFalse(ProctoringEvent.objects.filter(attempt=attempt, kind="large_code_change").exists())

    def test_new_ip_address_is_recorded_once(self):
        attempt = self._coding_attempt()
        Attempt.objects.filter(pk=attempt.pk).update(ip_address="10.0.0.1")
        for _ in range(2):
            self.client.get(reverse("api_state", args=[attempt.pk]), REMOTE_ADDR="10.0.0.2")
        self.assertEqual(ProctoringEvent.objects.filter(attempt=attempt, kind="network_changed").count(), 1)

    def test_client_signals_are_accepted_and_flag(self):
        attempt = self._coding_attempt()
        response = self._post(attempt, "api_event", {"kind": "multiple_screens", "duration_ms": 0, "context": {}})
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        self.assertTrue(attempt.flagged_for_review)
        self._post(attempt, "api_event", {"kind": "voice_detected", "duration_ms": 4000, "context": {}})
        short = ProctoringEvent.objects.get(attempt=attempt, kind="voice_detected")
        self.assertFalse(short.over_threshold)

    def test_integrity_summary_levels_and_reasons(self):
        attempt = self._coding_attempt()
        review = build_attempt_review(Attempt.objects.get(pk=attempt.pk))
        self.assertEqual(review["summary"]["integrity"]["level"], "No concerns")
        self._post(attempt, "api_event", {"kind": "virtual_camera", "duration_ms": 0, "context": {"label": "OBS Virtual Camera"}})
        review = build_attempt_review(Attempt.objects.get(pk=attempt.pk))
        integrity = review["summary"]["integrity"]
        self.assertEqual(integrity["level"], "High concern")
        self.assertIn("virtual", integrity["reasons"][0]["text"])

    def test_review_page_shows_code_history(self):
        attempt = self._coding_attempt()
        with mock.patch("assessments.services.get_runner", return_value=FakeRunner(outputs={"1 2": "3"})):
            self._post(attempt, "api_code_run", {"problem_id": self.problem.pk, "language": "python", "code": "print('history')"})
        self.client.force_login(self.staff)
        page = self.client.get(reverse("staff_attempt_review", args=[attempt.pk]))
        self.assertContains(page, "Code history")
        self.assertContains(page, "print(&#x27;history&#x27;)")
        self.assertContains(page, "Integrity summary")
