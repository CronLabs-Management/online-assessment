import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assessments import ai_client, interview_rounds
from assessments.ai_interview import generate_round_questions, round_profile
from assessments.models import AIKey, Application, Attempt, Exam, InterviewReview

User = get_user_model()


def _response(status, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = json.dumps(body or {})
    resp.json.return_value = body or {}
    return resp


def _ok(text):
    return _response(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


class KeyPoolTests(TestCase):
    def setUp(self):
        self.a = AIKey.objects.create(label="A", api_key="key-a-123456", priority=1)
        self.b = AIKey.objects.create(label="B", api_key="key-b-123456", priority=1)

    def _sent_keys(self, mock_post):
        return [call.kwargs["headers"]["x-goog-api-key"] for call in mock_post.call_args_list]

    @patch("assessments.ai_client.requests.post")
    def test_rate_limited_key_fails_over_to_next_and_rests(self, mock_post):
        mock_post.side_effect = [
            _response(429, {"error": {"message": "Resource has been exhausted"}}, {"Retry-After": "30"}),
            _ok("hello"),
        ]
        text, label = ai_client.generate_text("hi")
        self.assertEqual(text, "hello")
        self.assertEqual(label, "B")
        self.assertEqual(self._sent_keys(mock_post), ["key-a-123456", "key-b-123456"])
        self.a.refresh_from_db()
        self.assertTrue(self.a.is_cooling)
        self.assertAlmostEqual((self.a.cooling_until - timezone.now()).total_seconds(), 30, delta=5)
        self.assertEqual(self.a.failure_count, 1)
        self.b.refresh_from_db()
        self.assertEqual(self.b.success_count, 1)

    @patch("assessments.ai_client.requests.post")
    def test_resting_key_is_skipped_until_it_recovers(self, mock_post):
        self.a.cooling_until = timezone.now() + timedelta(minutes=5)
        self.a.save()
        mock_post.return_value = _ok("x")
        ai_client.generate_text("hi")
        self.assertEqual(self._sent_keys(mock_post), ["key-b-123456"])

    @patch("assessments.ai_client.requests.post")
    def test_requests_rotate_between_keys(self, mock_post):
        mock_post.return_value = _ok("x")
        labels = [ai_client.generate_text("hi")[1] for _ in range(4)]
        self.assertEqual(sorted(labels), ["A", "A", "B", "B"])
        self.assertNotEqual(labels[0], labels[1])

    @patch("assessments.ai_client.requests.post")
    def test_priority_wins_over_rotation(self, mock_post):
        self.b.priority = 0
        self.b.save()
        mock_post.return_value = _ok("x")
        labels = [ai_client.generate_text("hi")[1] for _ in range(3)]
        self.assertEqual(labels, ["B", "B", "B"])

    @patch("assessments.ai_client.requests.post")
    def test_rejected_key_rests_for_a_day(self, mock_post):
        mock_post.side_effect = [_response(400, {"error": {"message": "API key not valid."}}), _ok("x")]
        ai_client.generate_text("hi")
        self.a.refresh_from_db()
        self.assertGreater(self.a.cooling_until, timezone.now() + timedelta(hours=23))
        self.assertIn("Key rejected", self.a.last_error)

    @patch("assessments.ai_client.requests.post")
    def test_daily_quota_rests_for_an_hour(self, mock_post):
        mock_post.side_effect = [
            _response(429, {"error": {"message": "Quota exceeded for requests per day"}}),
            _ok("x"),
        ]
        ai_client.generate_text("hi")
        self.a.refresh_from_db()
        self.assertGreater(self.a.cooling_until, timezone.now() + timedelta(minutes=55))

    @patch("assessments.ai_client.requests.post")
    def test_plain_bad_request_does_not_burn_every_key(self, mock_post):
        mock_post.return_value = _response(400, {"error": {"message": "Invalid JSON payload"}})
        with self.assertRaises(ai_client.AIUnavailable):
            ai_client.generate_text("hi")
        self.assertEqual(mock_post.call_count, 1)

    @patch("assessments.ai_client.requests.post")
    def test_all_keys_failing_raises(self, mock_post):
        mock_post.return_value = _response(429, {"error": {"message": "quota"}})
        with self.assertRaises(ai_client.AIUnavailable) as ctx:
            ai_client.generate_text("hi")
        self.assertIn("A:", str(ctx.exception))
        self.assertIn("B:", str(ctx.exception))

    def test_all_resting_tries_the_soonest_to_recover(self):
        now = timezone.now()
        AIKey.objects.filter(pk=self.a.pk).update(cooling_until=now + timedelta(minutes=10))
        AIKey.objects.filter(pk=self.b.pk).update(cooling_until=now + timedelta(minutes=2))
        self.assertEqual([k.label for k in ai_client.keys_in_order()], ["B"])

    def test_inactive_keys_are_never_used(self):
        AIKey.objects.update(is_active=False)
        with patch.dict("os.environ", {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
            self.assertEqual(ai_client.keys_in_order(), [])

    def test_keys_page_requires_settings_permission_and_never_shows_full_key(self):
        admin = User.objects.create_user("boss", "boss@example.test", "pw", is_staff=True)
        self.client.force_login(admin)
        response = self.client.get(reverse("staff_ai_keys"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "key-a-123456")
        response = self.client.post(
            reverse("staff_ai_keys"),
            {"action": "save", "label": "C", "api_key": "key-c-999999", "model": "", "priority": 2, "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AIKey.objects.filter(label="C", api_key="key-c-999999").exists())


class RoundQuestionTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("lead", "lead@example.test", "pw", is_staff=True)
        cand = User.objects.create_user("cand", "cand@example.test", "pw")
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="Backend", created_by=self.staff, duration_minutes=30,
            opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=1),
            is_published=True, interview_rounds_count=3,
        )
        self.app = Application.objects.create(
            candidate=cand, exam=self.exam, full_name="Ravi", status=Application.Status.APPROVED
        )
        self.attempt = Attempt.objects.create(
            candidate=cand, exam=self.exam, application=self.app, status=Attempt.Status.SUBMITTED,
            started_at=now, deadline_at=now + timedelta(minutes=30), last_seen_at=now, ended_at=now,
        )
        AIKey.objects.create(label="A", api_key="key-a-123456")

    def test_later_rounds_are_harder(self):
        first, middle, last = (round_profile(n, 3)["mix"] for n in (1, 2, 3))
        self.assertGreater(first["Easy"], middle["Easy"])
        self.assertGreater(last["Hard"], first["Hard"])

    @patch("assessments.ai_client.requests.post")
    def test_round_two_prompt_includes_round_one_questions_and_notes(self, mock_post):
        self.app.ai_round_questions = {"1": [{"question": "Explain the cache you built.", "difficulty": "Easy"}]}
        self.app.save()
        InterviewReview.objects.create(
            attempt=self.attempt, round_number=1, reviewer=self.staff, candidate_summary="Weak on databases"
        )
        payload = {"resume_summary": "Mid-level backend dev.", "questions": [
            {"question": "How would you shard it?", "difficulty": "Hard", "keyword": "Redis"}]}
        mock_post.return_value = _ok(json.dumps(payload))
        result = generate_round_questions(self.app, 2, candidate_text_override="Built a Redis cache")
        self.assertTrue(result["success"])
        prompt = mock_post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Explain the cache you built.", prompt)
        self.assertIn("Weak on databases", prompt)
        self.assertIn("round 2 of 3", prompt)
        self.app.refresh_from_db()
        self.assertEqual(self.app.ai_round_questions["2"][0]["question"], "How would you shard it?")
        self.assertEqual(self.app.ai_round_questions["1"][0]["question"], "Explain the cache you built.")
        self.assertEqual(self.app.ai_resume_summary, "Mid-level backend dev.")

    def test_saving_scores_keeps_question_text_and_other_rounds(self):
        self.app.ai_round_questions = {
            "1": [{"question": "Q one", "difficulty": "Easy"}, {"question": "Q two", "difficulty": "Hard"}],
            "2": [{"question": "Round two Q", "difficulty": "Hard"}],
        }
        self.app.save()
        self.client.force_login(self.staff)
        self.client.post(reverse("staff_attempt_decision", args=[self.attempt.pk]), {
            "round_number": 1, "decision": "recommended", "interview_score": "7", "max_interview_score": "20",
            "questions_data": json.dumps([{"id": 1, "marks_awarded": 7, "answered": True}, {"id": 2, "answered": False}]),
        })
        review = InterviewReview.objects.get(attempt=self.attempt, round_number=1)
        self.assertEqual([q["question"] for q in review.questions_data], ["Q one", "Q two"])
        self.assertEqual(review.questions_data[0]["marks_awarded"], 7)
        self.assertIsNone(review.questions_data[1]["marks_awarded"])
        self.app.refresh_from_db()
        self.assertEqual(self.app.ai_round_questions["2"][0]["question"], "Round two Q")

        rows = interview_rounds.round_questions(self.app, 1, 3, review)
        self.assertTrue(rows[0]["is_reviewed"])
        response = self.client.get(reverse("staff_attempt_review", args=[self.attempt.pk]))
        self.assertContains(response, "Round two Q")
        self.assertContains(response, "Q one")

    def test_marks_are_clamped_to_the_question_maximum(self):
        self.app.ai_round_questions = {"1": [{"question": "Q", "max_marks": 10}]}
        rows = interview_rounds.merge_scores(self.app, 1, 3, [{"id": 1, "marks_awarded": 99, "answered": True}])
        self.assertEqual(rows[0]["marks_awarded"], 10)

    def test_legacy_single_list_is_split_across_rounds(self):
        self.app.ai_interview_questions = [{"question": f"Q{i}"} for i in range(6)]
        self.app.save()
        sizes = [len(interview_rounds.base_questions(self.app, r, 3)) for r in (1, 2, 3)]
        self.assertEqual(sizes, [2, 2, 2])

    @patch("assessments.ai_client.requests.post")
    def test_generate_button_targets_one_round(self, mock_post):
        self.app.ai_round_questions = {"1": [{"question": "Keep me"}]}
        self.app.save()
        mock_post.return_value = _ok(json.dumps({"questions": [{"question": "Fresh round 3", "difficulty": "Hard"}]}))
        self.client.force_login(self.staff)
        with patch("assessments.ai_interview.extract_resume_text", return_value="Python dev"):
            self.client.post(reverse("staff_generate_ai_questions", args=[self.attempt.pk]), {"round": 3})
        self.app.refresh_from_db()
        self.assertEqual(self.app.ai_round_questions["1"][0]["question"], "Keep me")
        self.assertEqual(self.app.ai_round_questions["3"][0]["question"], "Fresh round 3")
