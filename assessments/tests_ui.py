"""Checks for the shared UI shell and the screens reworked in the UI pass."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import Application, Exam, ExamSection, SiteSettings

User = get_user_model()


class UIShellTests(TestCase):
    def setUp(self):
        cache.clear()
        SiteSettings._local = None
        self.staff = User.objects.create_user(
            username="ui_admin", email="ui-admin@example.com", password="test-password-123",
            is_staff=True, is_superuser=True,
        )
        self.candidate = User.objects.create_user(
            username="ui_cand", email="ui-cand@example.com", password="test-password-123",
        )
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="UI exam", instructions="Be honest.", opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=3), duration_minutes=60, is_published=True,
            created_by=self.staff, camera_required=True, fullscreen_required=False,
        )
        ExamSection.objects.create(exam=self.exam, title="MCQ", kind="mcq", order=1, duration_minutes=20)
        ExamSection.objects.create(exam=self.exam, title="Code", kind="coding", order=2, duration_minutes=30)
        self.application = Application.objects.create(
            exam=self.exam, candidate=self.candidate, full_name="UI Candidate", phone="+1234567890",
            status="approved", details_confirmed_at=now, monitoring_consent_at=now,
        )

    def test_login_accepts_username_not_only_email(self):
        html = self.client.get(reverse("login")).content.decode()
        self.assertIn('autocomplete="username"', html)
        self.assertNotIn('type="email" name="username"', html)
        self.assertTrue(self.client.login(username="ui_admin", password="test-password-123"))

    def test_shell_loads_design_system_assets(self):
        self.client.force_login(self.candidate)
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn("css/ui.css", html)
        self.assertIn("js/ui.js", html)
        self.assertIn('id="mobile-nav-toggle"', html)
        self.assertIn('id="global-logout-form"', html)

    def test_fullscreen_shown_as_recommended_when_not_required(self):
        self.client.force_login(self.candidate)
        home = self.client.get(reverse("home")).content.decode()
        self.assertIn("Full screen recommended", " ".join(home.split()))
        self.assertIn("Camera required", " ".join(home.split()))
        briefing = self.client.get(reverse("exam_detail", args=[self.exam.pk])).content.decode()
        self.assertIn("Recommended", briefing)
        self.exam.fullscreen_required = True
        self.exam.save()
        home = self.client.get(reverse("home")).content.decode()
        self.assertIn("Full screen required", " ".join(home.split()))

    def test_home_does_not_query_per_assessment(self):
        self.client.force_login(self.candidate)
        self.client.get(reverse("home"))  # warm caches
        with CaptureQueriesContext(connection) as one:
            self.client.get(reverse("home"))
        for i in range(3):
            exam = Exam.objects.create(
                title=f"Extra {i}", opens_at=self.exam.opens_at, closes_at=self.exam.closes_at,
                duration_minutes=30, is_published=True, created_by=self.staff,
            )
            ExamSection.objects.create(exam=exam, title="S", kind="mcq", order=1, duration_minutes=10)
            Application.objects.create(exam=exam, candidate=self.candidate, full_name="UI Candidate", status="approved")
        with CaptureQueriesContext(connection) as four:
            response = self.client.get(reverse("home"))
        self.assertContains(response, "2 sections")
        self.assertLessEqual(len(four), len(one) + 1)

    def test_footer_uses_site_settings(self):
        site = SiteSettings.load()
        site.footer_copyright = "Footer owner 2026"
        site.footer_text = "Custom footer line"
        site.save()
        cache.clear()
        SiteSettings._local = None
        self.client.force_login(self.candidate)
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn("Footer owner 2026", html)
        self.assertIn("Custom footer line", html)

    def test_staff_test_list_summary_and_counts(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("staff_exam_list"))
        self.assertEqual(response.context["summary"]["total"], 1)
        self.assertEqual(response.context["summary"]["open_now"], 1)
        self.assertContains(response, "2 sections")
        self.assertContains(response, "Open now")

    def test_settings_shows_real_environment(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("staff_settings"))
        env = response.context["environment"]
        from django.conf import settings

        self.assertEqual(env["time_zone"], settings.TIME_ZONE)
        self.assertContains(response, settings.TIME_ZONE)
        self.assertNotContains(response, "SQLite3 / PostgreSQL")
        self.assertContains(response, "ui-savebar")

    def test_review_page_uses_app_navigation(self):
        from .models import Attempt

        attempt = Attempt.objects.create(
            application=self.application, exam=self.exam, candidate=self.candidate, started_at=timezone.now(),
            deadline_at=timezone.now() + timedelta(hours=1), last_seen_at=timezone.now(),
        )
        self.client.force_login(self.staff)
        response = self.client.get(reverse("staff_attempt_review", args=[attempt.pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('class="ui-header', html)
        self.assertIn('id="user-menu-button"', html)
        self.assertNotIn("Prototype", html)
