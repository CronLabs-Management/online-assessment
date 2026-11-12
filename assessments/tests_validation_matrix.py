from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from assessments.forms import (
    CandidateImportForm,
    ConfirmDetailsForm,
    ExamForm,
)
from assessments.models import Application, Exam
from assessments.validators import validate_full_name, validate_phone_number

User = get_user_model()


class PhoneValidationTests(TestCase):
    def test_phone_validator_accepts_valid_formats(self):
        valid = [
            "+91 9876543210",
            "9876543210",
            "+1 (555) 019-2834",
            "+44 7911 123456",
            "1234567",
            "+33 1 42 68 53 00",
            "020 7946 0991",
        ]
        for phone in valid:
            self.assertTrue(bool(validate_phone_number(phone)))

    def test_phone_validator_rejects_text_and_letters(self):
        invalid = [
            "text",
            "hello",
            "not a phone",
            "phone123456",
            "98765abc",
            "+91 phone",
        ]
        for phone in invalid:
            with self.assertRaises(ValidationError) as ctx:
                validate_phone_number(phone)
            self.assertIn("cannot contain letters", str(ctx.exception).lower())

    def test_phone_validator_rejects_too_short(self):
        for short in ["123", "1234", "123456"]:
            with self.assertRaises(ValidationError) as ctx:
                validate_phone_number(short)
            self.assertIn("too short", str(ctx.exception).lower())

    def test_phone_validator_rejects_too_long(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_phone_number("123456789012345678")
        self.assertIn("too long", str(ctx.exception).lower())

    def test_phone_validator_rejects_repeating_single_digits(self):
        for repeating in ["0000000000", "1111111111", "9999999999"]:
            with self.assertRaises(ValidationError) as ctx:
                validate_phone_number(repeating)
            self.assertIn("repeating", str(ctx.exception).lower())

    def test_phone_validator_rejects_symbols(self):
        for sym in ["9876543210!@#", "<script>", "9876543210; drop"]:
            with self.assertRaises(ValidationError):
                validate_phone_number(sym)


class FullNameValidationTests(TestCase):
    def test_name_validator_accepts_valid_names(self):
        valid = [
            "Rahul Sharma",
            "J. Doe",
            "O'Connor",
            "Mary-Jane Watson",
            "Álvaro González",
            "Dr. Martin Luther King Jr.",
        ]
        for name in valid:
            self.assertEqual(validate_full_name(name), name)

    def test_name_validator_rejects_numbers_only(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_full_name("12345")
        self.assertIn("contain letters", str(ctx.exception).lower())

    def test_name_validator_rejects_too_short(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_full_name("A")
        self.assertIn("at least 2 characters", str(ctx.exception).lower())

    def test_name_validator_rejects_html_or_script_tags(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_full_name("<script>alert(1)</script>")
        self.assertIn("special symbols or html", str(ctx.exception).lower())

    def test_name_validator_rejects_symbols_only(self):
        for s in ["---", "???", "***"]:
            with self.assertRaises(ValidationError):
                validate_full_name(s)


class ConfirmDetailsFormValidationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="admin@example.com", email="admin@example.com", password="password123")
        self.user = User.objects.create_user(username="cand@example.com", email="cand@example.com", password="password123")
        self.exam = Exam.objects.create(
            title="Test Exam",
            instructions="Test",
            created_by=self.admin,
            opens_at=timezone.now() - timezone.timedelta(hours=1),
            closes_at=timezone.now() + timezone.timedelta(hours=24),
            duration_minutes=60,
        )
        self.app = Application.objects.create(
            candidate=self.user,
            exam=self.exam,
            full_name="Candidate One",
            phone="9876543210",
        )

    def test_confirm_details_rejects_text_in_phone(self):
        data = {
            "full_name": "Candidate One",
            "phone": "invalid text",
            "monitoring_consent": True,
        }
        form = ConfirmDetailsForm(data=data, instance=self.app)
        self.assertFalse(form.is_valid())
        self.assertIn("phone", form.errors)
        self.assertIn("cannot contain letters", form.errors["phone"][0].lower())

    def test_confirm_details_rejects_invalid_phone_digits(self):
        data = {
            "full_name": "Candidate One",
            "phone": "123",
            "monitoring_consent": True,
        }
        form = ConfirmDetailsForm(data=data, instance=self.app)
        self.assertFalse(form.is_valid())
        self.assertIn("phone", form.errors)

    def test_confirm_details_accepts_valid_phone(self):
        data = {
            "full_name": "Candidate One",
            "phone": "+91 9876543210",
            "monitoring_consent": True,
        }
        form = ConfirmDetailsForm(data=data, instance=self.app)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["phone"], "+91 9876543210")

    def test_confirm_details_rejects_invalid_full_name(self):
        data = {
            "full_name": "12345",
            "phone": "+91 9876543210",
            "monitoring_consent": True,
        }
        form = ConfirmDetailsForm(data=data, instance=self.app)
        self.assertFalse(form.is_valid())
        self.assertIn("full_name", form.errors)


class CandidateImportFormValidationTests(TestCase):
    def test_single_candidate_rejects_text_phone(self):
        data = {
            "single_email": "new@example.com",
            "single_full_name": "New Candidate",
            "single_phone": "invalid_phone",
        }
        form = CandidateImportForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("single_phone", form.errors)

    def test_single_candidate_accepts_valid_phone(self):
        data = {
            "single_email": "new@example.com",
            "single_full_name": "New Candidate",
            "single_phone": "+91 9876543210",
        }
        form = CandidateImportForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        rows, errors = form.parse()
        self.assertEqual(len(errors), 0)
        self.assertEqual(rows[0]["phone"], "+91 9876543210")

    def test_bulk_candidate_rejects_text_phone_in_csv(self):
        data = {
            "candidates": "user1@example.com, Valid Candidate, not_a_phone",
        }
        form = CandidateImportForm(data=data)
        self.assertTrue(form.is_valid())
        rows, errors = form.parse()
        self.assertEqual(len(errors), 1)
        self.assertIn("not a valid phone number", errors[0].lower())

    def test_bulk_candidate_rejects_invalid_name_in_csv(self):
        data = {
            "candidates": "user1@example.com, 12345, 9876543210",
        }
        form = CandidateImportForm(data=data)
        self.assertTrue(form.is_valid())
        rows, errors = form.parse()
        self.assertEqual(len(errors), 1)
        self.assertIn("not a valid candidate name", errors[0].lower())


class ExamFormValidationTests(TestCase):
    def test_closes_at_before_opens_at_rejected(self):
        now = timezone.now()
        data = {
            "title": "Valid Exam",
            "instructions": "Instructions",
            "opens_at": (now + timezone.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "closes_at": (now + timezone.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "duration_minutes": 60,
            "tab_switch_threshold": 3,
            "missing_face_seconds": 10,
            "network_grace_seconds": 30,
            "max_pause_minutes": 15,
            "interview_rounds_count": 1,
        }
        form = ExamForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("closes_at", form.errors)

    def test_zero_duration_rejected(self):
        now = timezone.now()
        data = {
            "title": "Valid Exam",
            "instructions": "Instructions",
            "opens_at": now.strftime("%Y-%m-%dT%H:%M"),
            "closes_at": (now + timezone.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "duration_minutes": 0,
            "tab_switch_threshold": 3,
            "missing_face_seconds": 10,
            "network_grace_seconds": 30,
            "max_pause_minutes": 15,
            "interview_rounds_count": 1,
        }
        form = ExamForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("duration_minutes", form.errors)
