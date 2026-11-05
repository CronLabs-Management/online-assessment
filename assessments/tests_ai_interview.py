from unittest.mock import MagicMock, patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assessments.ai_interview import (
    extract_resume_text,
    generate_fallback_questions,
    generate_interview_questions_with_gemini,
    get_gemini_config,
)
from assessments.models import Application, Attempt, Exam, SiteSettings

User = get_user_model()


class AIInterviewFeatureTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staff_reviewer",
            email="reviewer@example.test",
            password="Password123!",
            is_staff=True,
        )
        self.candidate_user = User.objects.create_user(
            username="candidate_jane",
            email="jane@example.test",
            password="Password123!",
        )
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="Senior Backend Engineer Assessment",
            created_by=self.staff_user,
            duration_minutes=60,
            opens_at=now - timezone.timedelta(hours=1),
            closes_at=now + timezone.timedelta(hours=4),
            is_published=True,
        )
        self.application = Application.objects.create(
            candidate=self.candidate_user,
            exam=self.exam,
            full_name="Jane Doe",
            phone="+1234567890",
            status=Application.Status.APPROVED,
        )
        self.attempt = Attempt.objects.create(
            candidate=self.candidate_user,
            exam=self.exam,
            application=self.application,
            status=Attempt.Status.SUBMITTED,
            started_at=now,
            deadline_at=now + timezone.timedelta(minutes=60),
            last_seen_at=now,
            ended_at=now + timezone.timedelta(minutes=45),
        )

    def test_extract_resume_text_generates_and_reads_resume(self):
        text = extract_resume_text(self.application)
        self.assertTrue(len(text) > 30)
        self.assertIn("Jane Doe", text)

    def test_fallback_keyword_questions_generation(self):
        sample_resume = (
            "Jane Doe\n"
            "Languages: Python, JavaScript, TypeScript\n"
            "Frameworks: Django, FastAPI, React\n"
            "Databases: PostgreSQL, Redis\n"
            "DevOps: Docker, Kubernetes, AWS\n"
        )
        questions = generate_fallback_questions(sample_resume, "Jane Doe")
        self.assertTrue(len(questions) >= 3)
        for q in questions:
            self.assertIn("keyword", q)
            self.assertIn("question", q)
            self.assertIn("expected_answer", q)
            self.assertIn("category", q)
            self.assertIn("difficulty", q)
            self.assertTrue(len(q["keyword"]) > 0)
            self.assertTrue(len(q["question"]) > 0)
            self.assertTrue(len(q["expected_answer"]) > 0)

    def test_generate_interview_questions_without_api_key_uses_fallback(self):
        SiteSettings.objects.all().delete()
        res = generate_interview_questions_with_gemini(self.application)
        self.assertTrue(res["success"])
        self.assertEqual(res["engine"], "fallback_keyword_engine")
        self.application.refresh_from_db()
        self.assertTrue(len(self.application.ai_interview_questions) > 0)
        self.assertIsNotNone(self.application.ai_questions_generated_at)

    @patch("requests.post")
    def test_generate_interview_questions_with_gemini_api_mock(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": """[
                                    {
                                        "keyword": "PostgreSQL Query Optimization",
                                        "context_snippet": "Optimized slow SQL queries reducing p99 latency by 40%",
                                        "category": "Database Engineering",
                                        "difficulty": "Advanced",
                                        "question": "How do you diagnose index bloat and dead tuples in PostgreSQL?",
                                        "expected_answer": "Look for vacuum status, pg_stat_user_tables n_dead_tup, and REINDEX commands.",
                                        "follow_up_hint": "Ask about autovacuum tuning."
                                    }
                                ]"""
                            }
                        ]
                    }
                }
            ]
        }
        mock_post.return_value = mock_response

        res = generate_interview_questions_with_gemini(
            self.application,
            api_key_override="test-api-key-12345",
            model_override="gemini-1.5-flash",
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["count"], 1)
        self.application.refresh_from_db()
        self.assertEqual(len(self.application.ai_interview_questions), 1)
        first_q = self.application.ai_interview_questions[0]
        self.assertEqual(first_q["keyword"], "PostgreSQL Query Optimization")
        self.assertEqual(first_q["difficulty"], "Hard")
        self.assertIn("diagnose index bloat", first_q["question"])

    def test_staff_generate_ai_questions_endpoint(self):
        self.client.force_login(self.staff_user)
        url = reverse("staff_generate_ai_questions", args=[self.attempt.pk])
        response = self.client.post(url, {"force": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("staff_attempt_review", args=[self.attempt.pk]), response.url)
        self.application.refresh_from_db()
        self.assertTrue(len(self.application.ai_interview_questions) > 0)

    def test_review_page_displays_questions_and_keywords(self):
        # Generate questions first
        generate_interview_questions_with_gemini(self.application)
        self.client.force_login(self.staff_user)
        url = reverse("staff_attempt_review", args=[self.attempt.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Face-to-Face Interview Questions")
        self.assertContains(response, "Resume Keyword:")
        self.assertContains(response, "Expected Answer")
        self.assertContains(response, "Interview Marks &amp; Progress Showcase")
        self.assertContains(response, "Interviewer Name (Signature)")
        self.assertContains(response, "Candidate Short Info &amp; Summary")

    def test_difficulty_sorting_order(self):
        from assessments.ai_interview import sort_and_enrich_questions
        raw = [
            {"difficulty": "Hard", "question": "H1", "keyword": "K1"},
            {"difficulty": "Easy", "question": "E1", "keyword": "K2"},
            {"difficulty": "Intermediate", "question": "M1", "keyword": "K3"},
            {"difficulty": "Easy", "question": "E2", "keyword": "K4"},
        ]
        sorted_qs = sort_and_enrich_questions(raw)
        self.assertEqual(sorted_qs[0]["difficulty"], "Easy")
        self.assertEqual(sorted_qs[1]["difficulty"], "Easy")
        self.assertEqual(sorted_qs[2]["difficulty"], "Intermediate")
        self.assertEqual(sorted_qs[3]["difficulty"], "Hard")

    def test_staff_record_round_evaluation_with_signature_and_short_info(self):
        self.exam.interview_rounds_count = 2
        self.exam.save()

        self.client.force_login(self.staff_user)
        url = reverse("staff_attempt_decision", args=[self.attempt.pk])
        post_data = {
            "round_number": 1,
            "interviewer_name": "Sarah Connor - Lead Architect",
            "candidate_summary": "Strong systems programming knowledge, clear communication, solved all concurrency questions.",
            "decision": "recommended",
            "selected_for_next_round": "true",
            "interview_score": "25.0",
            "max_interview_score": "30.0",
            "notes": "Clear pass for Round 1 technical evaluation.",
            "questions_data": '[{"id":1,"answered":true,"marks_awarded":10,"max_marks":10}]',
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)
        self.assertIn("#interview-section", response.url)

        # Verify InterviewReview record
        from assessments.models import InterviewReview
        review = InterviewReview.objects.get(attempt=self.attempt, round_number=1)
        self.assertEqual(review.interviewer_name, "Sarah Connor - Lead Architect")
        self.assertEqual(review.candidate_summary, post_data["candidate_summary"])
        self.assertTrue(review.selected_for_next_round)
        self.assertEqual(float(review.interview_score), 25.0)

        # Check rounds status
        rounds_status = self.attempt.interview_rounds_status
        self.assertEqual(rounds_status["total_rounds"], 2)
        self.assertEqual(rounds_status["current_round"], 2)
        self.assertEqual(rounds_status["rounds"][0]["status"], "selected")

    def test_candidate_list_shows_round_status_badges(self):
        self.exam.interview_rounds_count = 2
        self.exam.save()

        from assessments.models import InterviewReview
        InterviewReview.objects.create(
            attempt=self.attempt,
            reviewer=self.staff_user,
            round_number=1,
            interviewer_name="Dave Reviewer",
            candidate_summary="Good round 1",
            decision=InterviewReview.Decision.RECOMMENDED,
            selected_for_next_round=True,
            interview_score=20,
            max_interview_score=25,
        )

        self.client.force_login(self.staff_user)
        url = reverse("staff_exam_candidates", args=[self.exam.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Interview Rounds (2)")
        self.assertContains(response, "R1: Done ✓")
        self.assertContains(response, "Round 2 Pending")

    def test_candidate_cannot_access_staff_review_or_questions_endpoint(self):
        self.client.force_login(self.candidate_user)
        url = reverse("staff_generate_ai_questions", args=[self.attempt.pk])
        response = self.client.post(url, {"force": "1"})
        # Non-staff candidate is redirected to login by @reviewer_required
        self.assertEqual(response.status_code, 302)

