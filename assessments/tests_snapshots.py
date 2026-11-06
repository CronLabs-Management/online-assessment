import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .models import Application, Attempt, Exam, ExamSection, ProctoringSnapshot
from .tests_engine import TMP_MEDIA

User = get_user_model()


def make_test_jpeg():
    buf = io.BytesIO()
    img = Image.new("RGB", (320, 240), color=(100, 150, 200))
    img.save(buf, format="JPEG")
    return buf.getvalue()


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class SnapshotEndpointTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin_snap", "admin_snap@example.com", "pw-admin", is_staff=True)
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="Snapshot Exam",
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=2),
            duration_minutes=60,
            is_published=True,
            snapshots_enabled=True,
            created_by=self.staff,
        )
        ExamSection.objects.create(exam=self.exam, title="MCQ", kind="mcq", order=1, duration_minutes=60)
        self.candidate = User.objects.create_user("cand_snap", "cand_snap@example.com", "pw-cand-1234")
        self.application = Application.objects.create(
            candidate=self.candidate, exam=self.exam, full_name="Candidate Snapshot", status=Application.Status.APPROVED
        )
        self.attempt = Attempt.objects.create(
            candidate=self.candidate,
            exam=self.exam,
            application=self.application,
            status=Attempt.Status.IN_PROGRESS,
            started_at=now,
            deadline_at=now + timedelta(minutes=60),
            last_seen_at=now,
            readiness_confirmed_at=now,
            snapshot_consent_at=now,
            policy_snapshot=self.exam.policy_snapshot(),
        )
        self.other_candidate = User.objects.create_user("other_cand", "other@example.com", "pw-other")

    def test_snapshot_upload_route_exists_and_succeeds(self):
        self.client.force_login(self.candidate)
        jpeg_data = make_test_jpeg()
        upload_file = SimpleUploadedFile("frame.jpg", jpeg_data, content_type="image/jpeg")
        url = reverse("api_snapshot", args=[self.attempt.pk])
        resp = self.client.post(url, {"image": upload_file, "face_count": "1", "trigger": "periodic"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("snapshot_id", data)

        snapshot = ProctoringSnapshot.objects.filter(attempt=self.attempt).first()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.attempt, self.attempt)
        self.assertTrue(snapshot.sha256)
        self.assertEqual(snapshot.content_type, "image/jpeg")

    def test_snapshot_upload_fails_without_candidate_consent(self):
        self.attempt.snapshot_consent_at = None
        self.attempt.save(update_fields=["snapshot_consent_at"])
        self.client.force_login(self.candidate)
        upload_file = SimpleUploadedFile("frame.jpg", make_test_jpeg(), content_type="image/jpeg")
        url = reverse("api_snapshot", args=[self.attempt.pk])
        resp = self.client.post(url, {"image": upload_file})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json().get("code"), "consent_required")

    def test_snapshot_upload_rejected_for_unauthenticated_or_wrong_user(self):
        url = reverse("api_snapshot", args=[self.attempt.pk])
        upload_file = SimpleUploadedFile("frame.jpg", make_test_jpeg(), content_type="image/jpeg")
        # Unauthenticated
        resp_unauth = self.client.post(url, {"image": upload_file})
        self.assertEqual(resp_unauth.status_code, 401)

        # Wrong candidate
        self.client.force_login(self.other_candidate)
        upload_file.seek(0)
        resp_other = self.client.post(url, {"image": upload_file})
        self.assertEqual(resp_other.status_code, 404)

    def test_snapshot_upload_fails_after_attempt_ended(self):
        self.attempt.status = Attempt.Status.SUBMITTED
        self.attempt.ended_at = timezone.now()
        self.attempt.save(update_fields=["status", "ended_at"])
        self.client.force_login(self.candidate)
        upload_file = SimpleUploadedFile("frame.jpg", make_test_jpeg(), content_type="image/jpeg")
        url = reverse("api_snapshot", args=[self.attempt.pk])
        resp = self.client.post(url, {"image": upload_file})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json().get("code"), "attempt_ended")

    def test_snapshot_upload_rejects_malformed_image(self):
        self.client.force_login(self.candidate)
        bad_file = SimpleUploadedFile("bad.jpg", b"NOT_AN_IMAGE_PAYLOAD", content_type="image/jpeg")
        url = reverse("api_snapshot", args=[self.attempt.pk])
        resp = self.client.post(url, {"image": bad_file})
        self.assertEqual(resp.status_code, 415)

    def test_snapshot_upload_rejects_oversized_image(self):
        self.client.force_login(self.candidate)
        big_data = b"\xff\xd8\xff" + b"0" * (2 * 1024 * 1024 + 1024)
        big_file = SimpleUploadedFile("big.jpg", big_data, content_type="image/jpeg")
        url = reverse("api_snapshot", args=[self.attempt.pk])
        resp = self.client.post(url, {"image": big_file})
        self.assertEqual(resp.status_code, 413)

    def test_reviewer_can_view_snapshot_and_candidate_cannot(self):
        self.client.force_login(self.candidate)
        upload_file = SimpleUploadedFile("frame.jpg", make_test_jpeg(), content_type="image/jpeg")
        self.client.post(reverse("api_snapshot", args=[self.attempt.pk]), {"image": upload_file})
        snapshot = ProctoringSnapshot.objects.filter(attempt=self.attempt).first()
        self.assertIsNotNone(snapshot)

        # Reviewer can view
        self.client.force_login(self.staff)
        view_url = reverse("staff_snapshot", args=[snapshot.pk])
        resp_staff = self.client.get(view_url)
        self.assertEqual(resp_staff.status_code, 200)
        self.assertEqual(resp_staff["Cache-Control"], "private, no-store")

        # Candidate cannot view reviewer snapshot URL
        self.client.force_login(self.candidate)
        resp_cand = self.client.get(view_url)
        self.assertEqual(resp_cand.status_code, 302)
