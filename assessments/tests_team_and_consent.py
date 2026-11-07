"""Staff roles and permissions, the monitoring notice, editable email text, and the test window close."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import notifications, services
from .models import (
    Application,
    Attempt,
    Exam,
    ExamSection,
    MCQOption,
    MCQQuestion,
    SiteSettings,
    StaffProfile,
    StaffRole,
)
from .permissions import INTERVIEWER_PERMISSIONS, has_perm, is_team_member, role_label
from .tests_engine import TMP_MEDIA

User = get_user_model()


def make_exam(staff, **overrides):
    now = timezone.now()
    values = dict(
        title="Backend hiring test",
        instructions="Be honest.",
        opens_at=now - timedelta(hours=1),
        closes_at=now + timedelta(hours=2),
        duration_minutes=60,
        is_published=True,
        created_by=staff,
        camera_required=False,
    )
    values.update(overrides)
    exam = Exam.objects.create(**values)
    ExamSection.objects.create(exam=exam, title="MCQ", kind="mcq", order=1, duration_minutes=60)
    q = MCQQuestion.objects.create(exam=exam, text="2+2?", marks=2, order=1)
    MCQOption.objects.create(question=q, text="4", is_correct=True, order=1)
    MCQOption.objects.create(question=q, text="5", is_correct=False, order=2)
    return exam


class RoleTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", "admin@example.com", "pw-12345-x", is_staff=True)
        self.exam = make_exam(self.admin)
        self.interviewer_role = StaffRole.objects.get(name="Interviewer")  # Created by the migration.
        self.interviewer = User.objects.create_user("int@example.com", "int@example.com", "pw-12345-x")
        StaffProfile.objects.create(user=self.interviewer, role=self.interviewer_role)
        self.candidate = User.objects.create_user("cand@example.com", "cand@example.com", "pw-12345-x")

    def test_migration_creates_interviewer_role(self):
        self.assertEqual(self.interviewer_role.permissions, INTERVIEWER_PERMISSIONS)

    def test_admin_has_every_permission(self):
        self.assertTrue(has_perm(self.admin, "team.manage"))
        self.assertEqual(role_label(self.admin), "Admin")

    def test_interviewer_can_review_but_not_edit_tests(self):
        self.client.force_login(self.interviewer)
        self.assertEqual(self.client.get(reverse("staff_exam_overview", args=[self.exam.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("staff_exam_edit", args=[self.exam.pk])).status_code, 302)
        self.assertEqual(self.client.get(reverse("team_overview")).status_code, 302)
        self.assertEqual(self.client.get(reverse("staff_settings")).status_code, 302)

    def test_changing_a_role_changes_access_without_code(self):
        self.interviewer_role.permissions = INTERVIEWER_PERMISSIONS + ["tests.manage"]
        self.interviewer_role.save()
        self.client.force_login(self.interviewer)
        self.assertEqual(self.client.get(reverse("staff_exam_edit", args=[self.exam.pk])).status_code, 200)

    def test_inactive_staff_lose_permissions_but_are_not_candidates(self):
        StaffProfile.objects.filter(user=self.interviewer).update(is_active=False)
        user = User.objects.get(pk=self.interviewer.pk)
        self.assertFalse(has_perm(user, "results.view"))
        self.assertTrue(is_team_member(user))

    def test_candidate_has_no_team_access(self):
        self.client.force_login(self.candidate)
        self.assertEqual(self.client.get(reverse("staff_exam_list")).status_code, 302)
        self.assertFalse(is_team_member(self.candidate))

    def test_old_reviewers_group_still_works(self):
        legacy = User.objects.create_user("old@example.com", "old@example.com", "pw-12345-x")
        legacy.groups.add(Group.objects.get_or_create(name="Reviewers")[0])
        self.assertTrue(has_perm(legacy, "results.view"))
        self.assertFalse(has_perm(legacy, "tests.manage"))

    def test_staff_cannot_start_a_test(self):
        Application.objects.create(candidate=self.interviewer, exam=self.exam, full_name="Int",
                                   details_confirmed_at=timezone.now(), monitoring_consent_at=timezone.now())
        self.client.force_login(self.interviewer)
        self.client.post(reverse("start_attempt", args=[self.exam.pk]))
        self.assertFalse(Attempt.objects.filter(candidate=self.interviewer).exists())

    def test_password_hidden_without_credentials_view(self):
        app = Application.objects.create(candidate=self.candidate, exam=self.exam, full_name="Cand", plain_password="Secret-pass-99")
        self.interviewer_role.permissions = INTERVIEWER_PERMISSIONS + ["candidates.manage"]
        self.interviewer_role.save()
        self.client.force_login(self.interviewer)
        page = self.client.get(reverse("staff_exam_candidates", args=[self.exam.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Secret-pass-99")
        edit = self.client.get(reverse("staff_candidate_edit", args=[app.pk]))
        self.assertNotContains(edit, "Secret-pass-99")

        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("staff_exam_candidates", args=[self.exam.pk])), "Secret-pass-99")


class TeamPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", "admin@example.com", "pw-12345-x", is_staff=True)
        self.client.force_login(self.admin)

    def test_create_role_and_add_member(self):
        response = self.client.post(reverse("team_role_add"), {
            "name": "Test author", "description": "Writes questions",
            "permissions": ["tests.view", "questions.manage", "not.a.permission"],
        })
        self.assertEqual(response.status_code, 200)  # Invalid choice is rejected.
        self.client.post(reverse("team_role_add"), {
            "name": "Test author", "description": "Writes questions",
            "permissions": ["questions.manage", "tests.view"],
        })
        role = StaffRole.objects.get(name="Test author")
        self.assertEqual(role.permissions, ["tests.view", "questions.manage"])

        self.client.post(reverse("team_member_add"), {
            "email": "Writer@Example.com", "full_name": "Wren Writer", "title": "Engineer",
            "role": role.pk, "is_active": "on",
        })
        profile = StaffProfile.objects.get(user__email="writer@example.com")
        self.assertEqual(profile.role, role)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(has_perm(User.objects.get(pk=profile.user_id), "questions.manage"))

    def test_candidate_email_cannot_become_staff(self):
        cand = User.objects.create_user("c@example.com", "c@example.com", "pw-12345-x")
        exam = make_exam(self.admin)
        Application.objects.create(candidate=cand, exam=exam, full_name="Cand")
        role = StaffRole.objects.get(name="Interviewer")
        response = self.client.post(reverse("team_member_add"), {
            "email": "c@example.com", "full_name": "Cand", "role": role.pk, "is_active": "on",
        })
        self.assertContains(response, "used by a candidate")
        self.assertFalse(StaffProfile.objects.filter(user=cand).exists())

    def test_role_with_members_cannot_be_deleted(self):
        role = StaffRole.objects.get(name="Interviewer")
        member = User.objects.create_user("m@example.com", "m@example.com", "pw-12345-x")
        StaffProfile.objects.create(user=member, role=role)
        self.client.post(reverse("team_role_delete", args=[role.pk]))
        self.assertTrue(StaffRole.objects.filter(pk=role.pk).exists())


class MonitoringConsentTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", "admin@example.com", "pw-12345-x", is_staff=True)
        self.exam = make_exam(self.admin)
        self.candidate = User.objects.create_user("cand@example.com", "cand@example.com", "pw-12345-x")
        self.application = Application.objects.create(candidate=self.candidate, exam=self.exam, full_name="Cand")
        self.client.force_login(self.candidate)

    def test_notice_is_shown_and_editable(self):
        site = SiteSettings.load()
        site.monitoring_notice = "We record your webcam.\nWe record your location."
        site.save()
        page = self.client.get(reverse("confirm_details", args=[self.exam.pk]))
        self.assertContains(page, "We record your location.")

    def test_details_cannot_be_saved_without_consent(self):
        self.client.post(reverse("confirm_details", args=[self.exam.pk]), {"full_name": "Cand", "phone": "9876543210"})
        self.application.refresh_from_db()
        self.assertIsNone(self.application.details_confirmed_at)
        self.assertIsNone(self.application.monitoring_consent_at)

    def test_consent_is_recorded_with_the_text_agreed_to(self):
        self.client.post(reverse("confirm_details", args=[self.exam.pk]),
                         {"full_name": "Cand", "phone": "9876543210", "monitoring_consent": "on"})
        self.application.refresh_from_db()
        self.assertIsNotNone(self.application.monitoring_consent_at)
        self.assertIn("webcam", self.application.monitoring_consent_text)

    def test_start_requires_consent(self):
        Application.objects.filter(pk=self.application.pk).update(details_confirmed_at=timezone.now())
        response = self.client.post(reverse("start_attempt", args=[self.exam.pk]))
        self.assertRedirects(response, reverse("confirm_details", args=[self.exam.pk]), fetch_redirect_response=False)
        self.assertFalse(Attempt.objects.exists())


class LoginEmailTextTests(TestCase):
    def test_email_uses_editable_subject_and_instructions(self):
        admin = User.objects.create_user("admin", "admin@example.com", "pw-12345-x", is_staff=True)
        exam = make_exam(admin, email_instructions="Bring a photo ID.")
        site = SiteSettings.load()
        site.site_name = "Acme Hiring"
        site.login_email_subject = "{site}: your {test} login"
        site.login_email_instructions = "Use Chrome."
        site.save()
        cand = User.objects.create_user("c@example.com", "c@example.com", "pw-12345-x")
        app = Application.objects.create(candidate=cand, exam=exam, full_name="Cand")
        self.assertTrue(notifications.send_login_details(app, "Pass-1234"))
        message = mail.outbox[-1]
        self.assertEqual(message.subject, "Acme Hiring: your Backend hiring test login")
        self.assertIn("Use Chrome.", message.body)
        self.assertIn("Bring a photo ID.", message.body)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class WindowCloseTests(TestCase):
    """A 1-hour test in a 2-hour window: a candidate who drops out and returns late still ends at the close."""

    def test_reconnecting_near_the_close_ends_at_the_close(self):
        admin = User.objects.create_user("admin", "admin@example.com", "pw-12345-x", is_staff=True)
        start = timezone.now()
        exam = make_exam(admin, opens_at=start - timedelta(minutes=1), closes_at=start + timedelta(minutes=119),
                         duration_minutes=60, pause_enabled=True, max_pause_minutes=120, network_grace_seconds=30)
        cand = User.objects.create_user("c@example.com", "c@example.com", "pw-12345-x")
        Application.objects.create(candidate=cand, exam=exam, full_name="Cand",
                                   details_confirmed_at=start, monitoring_consent_at=start)
        attempt = services.start_attempt(cand, exam)
        # Ten minutes in, the connection drops and the candidate comes back 15 minutes before the close.
        Attempt.objects.filter(pk=attempt.pk).update(last_seen_at=start + timedelta(minutes=10))
        attempt.refresh_from_db()
        back = start + timedelta(minutes=104)
        services.touch(attempt, back)
        attempt.refresh_from_db()
        self.assertLessEqual(attempt.deadline_at, exam.closes_at)
        self.assertFalse(attempt.is_final)

        services.touch(attempt, exam.closes_at + timedelta(seconds=1))
        attempt.refresh_from_db()
        self.assertTrue(attempt.is_final)
        self.assertLessEqual(attempt.ended_at, exam.closes_at)
