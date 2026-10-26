"""Exam security controls: one browser session per test, login throttling, clipboard and full-screen monitoring."""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import services
from .models import Application, Attempt, ProctoringEvent
from .services import AttemptError, perform
from .tests_engine import TMP_MEDIA, EngineFixture

User = get_user_model()
FAST_HASHER = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class SessionBindingTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.attempt = self.start()

    def _act(self, session_key):
        return perform(
            self.candidate,
            self.attempt.pk,
            lambda a, now: services.candidate_state(a, now),
            session_key=session_key,
        )

    def test_first_action_binds_the_test_to_that_session(self):
        self._act("session-a")
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.session_fingerprint, services.session_fingerprint("session-a"))

    def test_another_session_is_refused_and_the_attempt_is_flagged(self):
        self._act("session-a")
        with self.assertRaises(AttemptError) as ctx:
            self._act("session-b")
        self.assertEqual(ctx.exception.code, "session_mismatch")
        self.attempt.refresh_from_db()
        self.assertTrue(self.attempt.flagged_for_review)
        self.assertEqual(self.attempt.proctoring_events.filter(kind=ProctoringEvent.Kind.SESSION_CHANGED).count(), 1)

    def test_repeated_refusals_are_recorded_once_not_on_every_poll(self):
        self._act("session-a")
        for _ in range(5):
            with self.assertRaises(AttemptError):
                self._act("session-b")
        self.assertEqual(self.attempt.proctoring_events.filter(kind=ProctoringEvent.Kind.SESSION_CHANGED).count(), 1)

    def test_original_session_keeps_working_after_a_refusal(self):
        self._act("session-a")
        with self.assertRaises(AttemptError):
            self._act("session-b")
        self._act("session-a")  # No error.

    def test_staff_allow_resume_moves_the_test_to_the_new_session(self):
        self._act("session-a")
        with self.assertRaises(AttemptError):
            self._act("session-b")
        services.allow_resume(self.attempt, self.staff)
        self._act("session-b")
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.session_fingerprint, services.session_fingerprint("session-b"))
        self.assertTrue(self.attempt.flagged_for_review)  # The conflict stays on record.

    def test_finished_attempts_are_not_blocked_by_a_session_check(self):
        self._act("session-a")
        Attempt.objects.filter(pk=self.attempt.pk).update(status=Attempt.Status.SUBMITTED)
        self.attempt.refresh_from_db()
        self.assertTrue(services._bind_or_match_session(self.attempt, "session-b", timezone.now()))


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class SessionOverHttpTests(EngineFixture):
    def test_second_browser_gets_a_clear_refusal_and_staff_can_allow_it(self):
        attempt = self.start()
        first = Client()
        first.force_login(self.candidate)
        second = Client()
        second.force_login(self.candidate)
        url = reverse("api_state", args=[attempt.pk])

        self.assertEqual(first.get(url).status_code, 200)
        refused = second.get(url)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["code"], "session_mismatch")
        self.assertNotIn("score", refused.content.decode().lower())

        staff = Client()
        staff.force_login(self.staff)
        staff.post(reverse("staff_attempt_allow_resume", args=[attempt.pk]))
        self.assertEqual(second.get(url).status_code, 200)
        self.assertEqual(first.get(url).status_code, 409)

    def test_code_run_is_subject_to_the_same_session_rule(self):
        from .models import AttemptCoding

        attempt = self.start()
        problem_id = AttemptCoding.objects.filter(attempt=attempt).order_by("pk").first().problem_id
        first = Client()
        first.force_login(self.candidate)
        second = Client()
        second.force_login(self.candidate)
        first.get(reverse("api_state", args=[attempt.pk]))
        response = second.post(
            reverse("api_code_run", args=[attempt.pk]),
            data={"problem_id": problem_id, "language": "python", "code": "print(1)"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "session_mismatch")

    def test_staff_cannot_use_the_candidate_resume_route_as_a_candidate(self):
        attempt = self.start()
        response = Client().post(reverse("staff_attempt_allow_resume", args=[attempt.pk]))
        self.assertEqual(response.status_code, 302)  # Redirected to sign in, nothing changed.
        attempt.refresh_from_db()
        self.assertEqual(attempt.session_fingerprint, "")


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class ClipboardAndFullscreenTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.attempt = self.start()

    def _event(self, kind, context=None, duration=0):
        return perform(
            self.candidate,
            self.attempt.pk,
            lambda a, now: services.record_event(a, now, kind, None, duration, context or {}),
        )

    def test_a_large_paste_is_flagged(self):
        event = self._event("clipboard", {"action": "paste", "chars": 900})
        self.assertTrue(event.over_threshold)
        self.attempt.refresh_from_db()
        self.assertTrue(self.attempt.flagged_for_review)

    def test_a_small_paste_and_copies_are_recorded_without_flagging(self):
        self._event("clipboard", {"action": "paste", "chars": 12})
        self._event("clipboard", {"action": "copy", "chars": 900})
        self.attempt.refresh_from_db()
        self.assertFalse(self.attempt.flagged_for_review)
        self.assertEqual(self.attempt.proctoring_events.filter(kind="clipboard").count(), 2)

    def test_leaving_full_screen_counts_toward_the_switch_threshold(self):
        self.exam.tab_switch_threshold = 2
        self.exam.save()
        self.attempt.policy_snapshot = self.exam.policy_snapshot()
        self.attempt.save()
        flags = [self._event("fullscreen_exit").over_threshold for _ in range(3)]
        self.assertEqual(flags, [False, False, True])

    def test_policy_carries_the_full_screen_setting(self):
        self.assertTrue(self.exam.policy_snapshot()["fullscreen_required"])


class LoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("asha@example.com", "asha@example.com", "abcd-efgh-2345")
        staff = User.objects.create_user("admin", "admin@example.com", "pw-12345-x", is_staff=True)
        now = timezone.now()
        from .models import Exam

        exam = Exam.objects.create(
            title="Throttle test", opens_at=now, closes_at=now + timezone.timedelta(hours=1),
            duration_minutes=10, is_published=True, created_by=staff,
        )
        Application.objects.create(candidate=self.user, exam=exam, full_name="Asha", status=Application.Status.APPROVED)

    def tearDown(self):
        cache.clear()

    def _login(self, password):
        return self.client.post(reverse("login"), {"username": "asha@example.com", "password": password})

    def test_repeated_wrong_passwords_block_even_the_right_one(self):
        for _ in range(5):
            self._login("wrong")
        response = self._login("abcd-efgh-2345")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Too many failed sign-in attempts")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_successful_sign_in_clears_the_count(self):
        for _ in range(4):
            self._login("wrong")
        self.assertRedirects(self._login("abcd-efgh-2345"), reverse("home"), fetch_redirect_response=False)
        self.client.logout()
        for _ in range(4):
            self._login("wrong")
        self.assertRedirects(self._login("abcd-efgh-2345"), reverse("home"), fetch_redirect_response=False)

    def test_block_lifts_after_the_window(self):
        for _ in range(5):
            self._login("wrong")
        cache.clear()  # Stands in for the fifteen-minute window passing.
        self.assertRedirects(self._login("abcd-efgh-2345"), reverse("home"), fetch_redirect_response=False)


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA)
class TestPageControlsTests(EngineFixture):
    def test_full_screen_gate_is_rendered_when_required(self):
        attempt = self.start()
        self.client.force_login(self.candidate)
        response = self.client.get(reverse("attempt_page", args=[attempt.pk]))
        self.assertContains(response, 'id="fullscreen-gate"')
        self.assertContains(response, 'data-fullscreen-required="1"')

    def test_full_screen_gate_is_absent_when_not_required(self):
        self.exam.fullscreen_required = False
        self.exam.save()
        attempt = self.start()
        self.client.force_login(self.candidate)
        response = self.client.get(reverse("attempt_page", args=[attempt.pk]))
        self.assertNotContains(response, 'id="fullscreen-gate"')
