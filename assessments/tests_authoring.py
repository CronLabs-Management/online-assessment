"""Question images, section marks, coding examples and CSV question import."""

import io
import zipfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from . import services
from .models import Application, Attempt, CodingProblem, Exam, ExamSection, MCQOption, MCQQuestion
from .runners import judge0_language_ids
from .tests_engine import TMP_MEDIA

User = get_user_model()


def png_bytes(color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


def png(name="pic.png", color="red"):
    return SimpleUploadedFile(name, png_bytes(color), content_type="image/png")


@override_settings(PRIVATE_MEDIA_ROOT=TMP_MEDIA, MEDIA_ROOT=TMP_MEDIA)
class AuthoringTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", "admin@example.com", "pw-12345-x", is_staff=True)
        now = timezone.now()
        self.exam = Exam.objects.create(
            title="Authoring", instructions="", opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=3),
            duration_minutes=60, created_by=self.admin, camera_required=False,
        )
        self.section = self.exam.get_default_section()
        self.client.force_login(self.admin)


class McqImageTests(AuthoringTestCase):
    def _post_question(self, **extra):
        data = {
            "section": self.section.pk, "text": "Which colour?", "marks": 2, "order": 1,
            "options-TOTAL_FORMS": 3, "options-INITIAL_FORMS": 0, "options-MIN_NUM_FORMS": 0, "options-MAX_NUM_FORMS": 1000,
            "options-0-text": "", "options-0-image": png("a.png"), "options-0-is_correct": "on",
            "options-1-text": "Blue",
            "options-2-text": "Green",
            "image": png("q.png"),
        }
        data.update(extra)
        return self.client.post(reverse("staff_mcq_add", args=[self.exam.pk]), data)

    def test_question_and_image_only_option_are_saved(self):
        self._post_question()
        question = MCQQuestion.objects.get()
        self.assertTrue(question.image.name.startswith("question_images/"))
        first = question.options.order_by("order").first()
        self.assertEqual(first.text, "")
        self.assertTrue(first.image)
        self.assertTrue(first.is_correct)

    def test_display_order_from_the_page_is_saved(self):
        self._post_question(**{"options-order": "options-2,options-0,options-1"})
        texts = list(MCQQuestion.objects.get().options.order_by("order").values_list("text", flat=True))
        self.assertEqual(texts, ["Green", "", "Blue"])

    def test_fake_image_is_rejected(self):
        bad = SimpleUploadedFile("evil.png", b"<script>alert(1)</script>", content_type="image/png")
        self._post_question(image=bad)
        self.assertFalse(MCQQuestion.objects.exists())

    def test_candidate_sees_image_only_during_their_attempt(self):
        self._post_question()
        question = MCQQuestion.objects.get()
        self.exam.is_published = True
        self.exam.save()
        cand = User.objects.create_user("c@example.com", "c@example.com", "pw-12345-x")
        other = User.objects.create_user("o@example.com", "o@example.com", "pw-12345-x")
        url = reverse("question_image", args=["question", question.pk])

        self.client.force_login(other)
        self.assertEqual(self.client.get(url).status_code, 404)

        Application.objects.create(candidate=cand, exam=self.exam, full_name="C",
                                   details_confirmed_at=timezone.now(), monitoring_consent_at=timezone.now())
        attempt = services.start_attempt(cand, self.exam)
        self.client.force_login(cand)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        response.close()
        state = self.client.get(reverse("api_state", args=[attempt.pk])).json()
        self.assertEqual(state["questions"][0]["image_url"], url)
        self.assertNotIn("is_correct", str(state))

        Attempt.objects.filter(pk=attempt.pk).update(status=Attempt.Status.SUBMITTED)
        self.assertEqual(self.client.get(url).status_code, 404)


class SectionMarksTests(AuthoringTestCase):
    def test_new_question_form_uses_section_marks(self):
        self.section.marks_per_question = 5
        self.section.save()
        page = self.client.get(reverse("staff_mcq_add", args=[self.exam.pk]) + f"?section={self.section.pk}")
        self.assertEqual(page.context["question_form"].initial["marks"], 5)

    def test_apply_marks_to_existing_questions(self):
        MCQQuestion.objects.create(exam=self.exam, section=self.section, text="Q", marks=1)
        self.client.post(reverse("staff_section_edit", args=[self.section.pk]), {
            "title": self.section.title, "order": self.section.order, "duration_minutes": 20,
            "marks_per_question": 10, "question_count": 0, "coding_selection_count": 1,
            "apply_marks_to_existing": "on",
        })
        self.assertEqual(MCQQuestion.objects.get().marks, 10)

    def test_only_one_coding_section(self):
        ExamSection.objects.create(exam=self.exam, title="Code", kind="coding", order=5, duration_minutes=30)
        self.client.post(reverse("staff_section_add", args=[self.exam.pk]), {
            "section-title": "More code", "section-kind": "coding", "section-order": 6, "section-duration_minutes": 30,
            "section-marks_per_question": 1, "section-question_count": 0, "section-coding_selection_count": 1,
        })
        self.assertEqual(self.exam.sections.filter(kind="coding").count(), 1)


class CsvImportTests(AuthoringTestCase):
    def _upload(self, kind, name, content):
        f = SimpleUploadedFile(name, content)
        return self.client.post(reverse("staff_question_import", args=[self.exam.pk, kind]), {"file": f})

    def test_templates_download(self):
        for kind in ("mcq", "coding"):
            response = self.client.get(reverse("staff_question_template", args=[kind]))
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"," , response.content)

    def test_mcq_csv_import_with_letter_answer_and_new_section(self):
        csv = (
            b"section,question,option_1,option_2,option_3,correct_option,marks\n"
            b"Aptitude,2+2?,3,4,,B,\n"
            b"Aptitude,Capital of France?,Paris,Rome,Berlin,1,4\n"
        )
        self._upload("mcq", "q.csv", csv)
        section = ExamSection.objects.get(exam=self.exam, title="Aptitude")
        q1 = MCQQuestion.objects.get(text="2+2?")
        self.assertEqual(q1.section, section)
        self.assertEqual(q1.options.count(), 2)
        self.assertEqual(q1.options.get(is_correct=True).text, "4")
        self.assertEqual(q1.marks, section.marks_per_question)
        self.assertEqual(MCQQuestion.objects.get(text="Capital of France?").marks, 4)

    def test_correct_option_counts_columns_not_filled_options(self):
        csv = b"question,option_1,option_2,option_3,correct_option\nPick C,A,,C,3\n"
        self._upload("mcq", "q.csv", csv)
        self.assertEqual(MCQOption.objects.get(is_correct=True).text, "C")

    def test_pipe_separated_options(self):
        self._upload("mcq", "q.csv", b"question,options,correct_option\nPick,a|b|c|d|e,5\n")
        question = MCQQuestion.objects.get()
        self.assertEqual(question.options.count(), 5)
        self.assertEqual(question.options.get(is_correct=True).text, "e")

    def test_bad_row_imports_nothing(self):
        csv = b"question,option_1,option_2,correct_option\nGood,a,b,1\nBad,a,b,7\n"
        response = self._upload("mcq", "q.csv", csv)
        self.assertContains(response, "Row 3")
        self.assertFalse(MCQQuestion.objects.exists())

    def test_zip_with_images(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("questions.csv", "question,option_images,correct_option,question_image\nShape?,a.png|b.png,2,q.png\n")
            for name in ("a.png", "b.png", "q.png"):
                archive.writestr(f"images/{name}", png_bytes())
        self._upload("mcq", "bundle.zip", buffer.getvalue())
        question = MCQQuestion.objects.get()
        self.assertTrue(question.image)
        self.assertEqual(question.options.exclude(image="").count(), 2)

    def test_coding_csv_import(self):
        csv = (
            b"title,statement,example_input,example_output,example_explanation,languages,marks,"
            b"test_1_input,test_1_output,test_1_hidden,test_2_input,test_2_output,test_2_hidden\n"
            b'Sum,"Add two numbers.\nPrint the sum.",2 3,5,2+3=5,"python, c++",25,1 2,3,no,5 5,10,yes\n'
        )
        self._upload("coding", "c.csv", csv)
        problem = CodingProblem.objects.get()
        self.assertEqual(problem.marks, 25)
        self.assertEqual(problem.language_list, ["python", "cpp"])
        self.assertEqual(problem.example_explanation, "2+3=5")
        self.assertEqual(list(problem.test_cases.order_by("order").values_list("is_hidden", flat=True)), [False, True])

    def test_import_is_locked_after_attempts_begin(self):
        cand = User.objects.create_user("c@example.com", "c@example.com", "pw-12345-x")
        app = Application.objects.create(candidate=cand, exam=self.exam, full_name="C")
        now = timezone.now()
        Attempt.objects.create(candidate=cand, exam=self.exam, application=app, started_at=now,
                               deadline_at=now + timedelta(hours=1), last_seen_at=now)
        self._upload("mcq", "q.csv", b"question,option_1,option_2,correct_option\nQ,a,b,1\n")
        self.assertFalse(MCQQuestion.objects.exists())


class Judge0LanguageTests(TestCase):
    def test_language_ids_can_be_overridden(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"JUDGE0_LANGUAGE_IDS": '{"python": 100}'}):
            ids = judge0_language_ids()
        self.assertEqual(ids["python"], 100)
        self.assertEqual(ids["java"], 62)
