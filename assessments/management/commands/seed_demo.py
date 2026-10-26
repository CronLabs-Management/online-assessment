"""
Create a published demo test with approved candidates. Development and load testing only.

Accounts created here use a known password, so the command refuses to run unless DEBUG is on.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from assessments.models import (
    Application,
    CodingProblem,
    CodingTestCase,
    Exam,
    ExamSection,
    MCQOption,
    MCQQuestion,
)

User = get_user_model()

MCQ_BANK = [
    ("Which keyword defines a function in Python?", ["def", "func", "function", "lambda"]),
    ("What does len([1, 2, 3]) return?", ["3", "2", "4", "1"]),
    ("Which type is immutable?", ["tuple", "list", "dict", "set"]),
    ("What is the result of 7 // 2?", ["3", "3.5", "4", "2.5"]),
    ("Which statement opens a loop over a sequence?", ["for", "while", "loop", "repeat"]),
]

CODING_BANK = [
    {
        "title": "Sum two integers",
        "statement": "Read two integers a and b separated by a space and print their sum.",
        "public": [("1 2", "3"), ("10 20", "30"), ("-5 5", "0")],
        "hidden": [("100 200", "300"), ("7 -7", "0")],
    },
    {
        "title": "Reverse a line",
        "statement": "Read one line of text and print it reversed.",
        "public": [("abc", "cba"), ("a", "a"), ("racecar", "racecar")],
        "hidden": [("Python", "nohtyP"), ("12345", "54321")],
    },
    {
        "title": "Count even numbers",
        "statement": "Read a line of space-separated integers and print how many are even.",
        "public": [("1 2 3 4", "2"), ("7", "0"), ("2 4 6", "3")],
        "hidden": [("0 -2 5", "2"), ("1 3 5 7 9", "0")],
    },
]


class Command(BaseCommand):
    help = "Create a demo test with approved candidates. Refuses to run unless DEBUG is on."

    def add_arguments(self, parser):
        parser.add_argument("--candidates", type=int, default=5, help="Number of approved candidates.")
        parser.add_argument("--password", default="demo-pass-12345")
        parser.add_argument("--title", default="Demo assessment")

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo creates accounts with a known password. Run it only with DJANGO_DEBUG=1.")

        password = options["password"]
        staff, _ = User.objects.get_or_create(username="admin", defaults={"email": "admin@example.test", "is_staff": True})
        staff.is_staff = True
        staff.set_password(password)
        staff.save()

        now = timezone.now()
        exam = Exam.objects.create(
            title=options["title"],
            instructions=(
                "Answer all multiple-choice questions, then complete the coding problems.\n"
                "Each section has its own timer. Do not leave the page during the camera check.\n"
                "Public test cases are shown while you work; hidden cases are used for grading."
            ),
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(days=30),
            duration_minutes=60,
            is_published=True,
            created_by=staff,
            shuffle_questions=True,
            shuffle_options=True,
            camera_required=True,
            snapshots_enabled=True,
            # The headless browser test cannot enter full screen. Real exams keep the default (on).
            fullscreen_required=False,
        )
        mcq_section = ExamSection.objects.create(
            exam=exam, title="Multiple choice", kind=ExamSection.Kind.MCQ, order=1,
            duration_minutes=30, question_count=3,
        )
        coding_section = ExamSection.objects.create(
            exam=exam, title="Coding", kind=ExamSection.Kind.CODING, order=2,
            duration_minutes=30, coding_selection_count=2,
        )

        for index, (text, options_text) in enumerate(MCQ_BANK, start=1):
            question = MCQQuestion.objects.create(exam=exam, text=text, marks=2, order=index)
            for position, label in enumerate(options_text, start=1):
                MCQOption.objects.create(question=question, text=label, is_correct=(position == 1), order=position)

        for index, spec in enumerate(CODING_BANK, start=1):
            problem = CodingProblem.objects.create(
                exam=exam,
                title=spec["title"],
                statement=spec["statement"],
                input_spec="One test case per line on standard input.",
                output_spec="Print the answer followed by a newline.",
                languages="python, java, c, cpp",
                marks=10,
                order=index,
                starter_code="",
            )
            for case_index, (stdin, expected) in enumerate(spec["public"], start=1):
                CodingTestCase.objects.create(problem=problem, input_data=stdin, expected_output=expected, order=case_index)
            for case_index, (stdin, expected) in enumerate(spec["hidden"], start=len(spec["public"]) + 1):
                CodingTestCase.objects.create(
                    problem=problem, input_data=stdin, expected_output=expected, is_hidden=True, order=case_index
                )

        created = []
        for number in range(1, options["candidates"] + 1):
            email = f"candidate{number:04d}@example.test"
            user, _ = User.objects.get_or_create(username=email, defaults={"email": email})
            user.email = email
            user.set_password(password)
            user.save()
            Application.objects.update_or_create(
                candidate=user,
                exam=exam,
                defaults={
                    "full_name": f"Candidate {number:04d}",
                    "status": Application.Status.APPROVED,
                    "credentials_sent_at": now,
                    "details_confirmed_at": now,
                },
            )
            created.append(email)

        self.stdout.write(self.style.SUCCESS(f"Demo test #{exam.pk} '{exam.title}' is published."))
        self.stdout.write(f"Staff login:     admin / {password}")
        self.stdout.write(f"Candidate logins: {created[0]} … {created[-1]} / {password}")
        self.stdout.write(f"Sections: MCQ #{mcq_section.pk}, coding #{coding_section.pk}")
