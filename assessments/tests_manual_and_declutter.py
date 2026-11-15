from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Application, Exam, ExamSection, StaffRole

User = get_user_model()


class PlatformManualAndUIDeclutterTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="admin_user",
            email="admin@example.com",
            password="test-password-123",
            is_staff=True,
        )
        self.candidate_user = User.objects.create_user(
            username="cand_user",
            email="candidate@example.com",
            password="test-password-123",
        )
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="Fullstack Evaluation 2026",
            instructions="Maintain integrity at all times.",
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=3),
            duration_minutes=60,
            is_published=True,
            created_by=self.staff,
            camera_required=True,
        )
        self.section = ExamSection.objects.create(
            exam=self.exam,
            title="General Knowledge",
            kind="mcq",
            order=1,
            duration_minutes=20,
        )
        self.application = Application.objects.create(
            exam=self.exam,
            candidate=self.candidate_user,
            full_name="Candidate Full Name",
            phone="+1234567890",
            status="approved",
            details_confirmed_at=now,
            monitoring_consent_at=now,
        )

    def test_manual_page_accessible_anonymously(self):
        """Anonymous guests only view the Candidate Manual, not staff chapters."""
        response = self.client.get(reverse("platform_manual"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        
        # Only Chapter 1 is present
        self.assertIn('id="chapter-1"', content)
        self.assertIn("Candidate Testing &amp; Preparation Guide", content)
        self.assertIn("Candidate Edition", content)
        
        # Staff chapters are protected and omitted
        for i in range(2, 7):
            self.assertNotIn(f'id="chapter-{i}"', content)
            self.assertNotIn(f'href="#chapter-{i}"', content)
            
        self.assertNotIn("Test Authoring &amp; Question Pool", content)
        self.assertNotIn("Role-Based Access Control", content)
        self.assertNotIn("role-filter-btn", content)

    def test_candidate_only_sees_candidate_manual(self):
        """Logged-in candidates strictly see Chapter 1 (Candidate Guide), not internal staff/admin documentation."""
        self.client.force_login(self.candidate_user)
        response = self.client.get(reverse("platform_manual"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        
        # Candidate guide and subsections present
        self.assertIn('id="chapter-1"', content)
        self.assertIn('id="chapter-1-rules"', content)
        self.assertIn('id="chapter-1-troubleshooting"', content)
        self.assertIn("Candidate Assessment Manual", content)
        self.assertIn("Candidate Edition", content)
        
        # Staff chapters strictly excluded
        for i in range(2, 7):
            self.assertNotIn(f'id="chapter-{i}"', content)
            self.assertNotIn(f'href="#chapter-{i}"', content)
            
        self.assertNotIn("Test Authoring &amp; Question Pool", content)
        self.assertNotIn("Role-Based Access Control", content)
        self.assertNotIn("role-filter-btn", content)

    def test_manual_page_accessible_as_staff(self):
        """Authenticated staff members can view all 6 chapters with staff role filters."""
        self.client.force_login(self.staff)
        response = self.client.get(reverse("platform_manual"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        
        self.assertIn("Platform User Manual", content)
        self.assertIn("role-filter-btn", content)
        
        # All 6 chapters present for staff
        for i in range(1, 7):
            self.assertIn(f'id="chapter-{i}"', content)
            self.assertIn(f'href="#chapter-{i}"', content)
            
        self.assertIn("Test Authoring &amp; Question Pool", content)
        self.assertIn("Candidate Enrollment &amp; Credential Delivery", content)
        self.assertIn("Live Proctoring, Telemetry &amp; Audit Logs", content)
        self.assertIn("Multi-Round Interviewing &amp; AI Copilot", content)
        self.assertIn("Staff Administration &amp; System Settings", content)

    def test_candidate_home_page_decluttered(self):
        """Home page has candidate resources link to manual without redundant walls of text."""
        self.client.force_login(self.candidate_user)
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn(reverse("platform_manual"), content)
        self.assertIn("Candidate Resources", content)

    def test_exam_detail_briefing_screen_clean_layout(self):
        """Exam detail briefing screen displays 4-chip conduct rules and system readiness checklist."""
        self.client.force_login(self.candidate_user)
        response = self.client.get(reverse("exam_detail", args=[self.exam.pk]), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        
        # Verify 4-chip code of conduct
        self.assertIn("Continuous Fullscreen", content)
        self.assertIn("Keep Face Centered", content)
        self.assertIn("Work Independently", content)
        self.assertIn("Continuous Auto-Save", content)
        
        # Verify pre-flight checklist
        self.assertIn("Pre-Flight Checklist", content)
        self.assertIn("Webcam Feed", content)
        self.assertIn("Fullscreen Window", content)
        
        # Verify manual guide link
        self.assertIn(f'{reverse("platform_manual")}#chapter-1', content)

    def test_confirm_details_clean_layout(self):
        """Confirm details page has streamlined trust reassurance and clean form layout."""
        self.client.force_login(self.candidate_user)
        response = self.client.get(reverse("confirm_details", args=[self.exam.pk]), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Step 1 of 3: Identity &amp; Profile Verification", content)
        self.assertIn("Candidate Full Name", content)

    def test_staff_team_role_form_clean_layout(self):
        """Team role form renders 12-column responsive grid without legend overlap."""
        self.client.force_login(self.staff)
        role = StaffRole.objects.create(name="Reviewer Analyst", description="Reviews exams")
        response = self.client.get(reverse("team_role_edit", args=[role.pk]), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Edit Role: Reviewer Analyst", content)
        self.assertIn("active-count", content)

    def test_staff_settings_links_to_manual(self):
        """Staff settings page includes direct link to User Manual Chapter 6."""
        self.client.force_login(self.staff)
        response = self.client.get(reverse("staff_settings"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn(f'{reverse("platform_manual")}#chapter-6', content)

    def test_redesigned_header_candidate_view(self):
        """Candidate header renders brand logo, dashboard link, candidate pill, and mobile drawer."""
        self.client.force_login(self.candidate_user)
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Candidate", content)
        self.assertIn("mobile-nav-toggle", content)
        self.assertIn("mobile-nav-drawer", content)
        self.assertIn("nav-item-pill", content)

    def test_redesigned_header_staff_view(self):
        """Staff header renders staff console badge, exams navigation, and docs badge."""
        self.client.force_login(self.staff)
        response = self.client.get(reverse("staff_exam_list"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Staff", content)
        self.assertIn("Exams", content)
        self.assertIn("mobile-nav-toggle", content)
        self.assertIn("mobile-nav-drawer", content)

    def test_signout_confirmation_modal_present_for_authenticated_users(self):
        """Authenticated users have sign-out trigger and confirmation dialog with cancel/confirm buttons."""
        self.client.force_login(self.candidate_user)
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("header-signout-trigger", content)
        self.assertIn("mobile-signout-trigger", content)
        self.assertIn("signout-confirm-modal", content)
        self.assertIn("signout-modal-cancel", content)
        self.assertIn("signout-modal-confirm", content)
        self.assertIn("global-logout-form", content)

    def test_universal_in_app_modal_present_and_declarative_confirms(self):
        """Universal in-app modal shell is rendered and destructive actions use data-confirm instead of native confirm()."""
        self.client.force_login(self.staff)
        
        # Check base universal modal
        response = self.client.get(reverse("team_overview"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn('id="oa-universal-modal"', content)
        self.assertIn('id="oa-modal-confirm"', content)
        self.assertIn('id="oa-modal-cancel"', content)
        self.assertIn('window.oaConfirm', content)
        self.assertIn('window.oaAlert', content)
        
        # Verify team delete role uses data-confirm without native confirm()
        StaffRole.objects.create(name="Temporary Role", description="Temporary")
        response = self.client.get(reverse("team_overview"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        role_content = response.content.decode("utf-8")
        self.assertIn('data-confirm="Delete the role Temporary Role?', role_content)
        self.assertNotIn("onsubmit=\"return confirm(", role_content)

    def test_exam_detail_actions_use_in_app_confirmation(self):
        """Exam detail section delete and unpublish actions use declarative in-app confirmations."""
        self.client.force_login(self.staff)
        # Create a non-default MCQ section so the delete button is rendered
        ExamSection.objects.create(
            exam=self.exam,
            title="Advanced Algorithms",
            kind="mcq",
            order=2,
            is_default=False,
        )
        response = self.client.get(reverse("staff_exam_detail", args=[self.exam.pk]), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn('data-confirm="Unpublish this assessment?', content)
        self.assertIn('data-confirm="Delete section', content)
        self.assertIn('Advanced Algorithms', content)
        self.assertIn('data-confirm-tone="danger"', content)
        self.assertNotIn("return confirm(", content)
