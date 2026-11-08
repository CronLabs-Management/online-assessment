"""Bulk import of MCQ and coding questions from a CSV file (or a ZIP with the CSV and its images).

Both importers check every row first and import nothing if any row has a problem, so a file is never
half imported. Each error names its row number as it appears in a spreadsheet (header is row 1).
"""

import csv
import io
import os
import re
import zipfile

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max

from .forms import QUESTION_IMAGE_MAX_BYTES, QUESTION_IMAGE_TYPES, validate_question_image
from .models import SUPPORTED_LANGUAGES, CodingProblem, CodingTestCase, ExamSection, MCQOption, MCQQuestion

MAX_ROWS = 2000
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
OPTION_SEPARATOR = "|"

MCQ_TEMPLATE_HEADER = [
    "section", "question", "option_1", "option_2", "option_3", "option_4",
    "correct_option", "marks", "question_image", "option_images",
]
MCQ_TEMPLATE_ROWS = [
    ["Aptitude", "What is 15% of 200?", "20", "30", "35", "40", "2", "", "", ""],
    ["Aptitude", "Which shape is shown?", "", "", "", "", "1", "2", "shape.png", "a.png|b.png|c.png|d.png"],
    ["Programming", "Which keyword defines a function in Python?", "func", "def", "lambda", "", "B", "5", "", ""],
]

CODING_TEMPLATE_HEADER = [
    "title", "statement", "input_format", "output_format", "constraints",
    "example_input", "example_output", "example_explanation",
    "languages", "marks", "starter_code",
    "test_1_input", "test_1_output", "test_1_hidden",
    "test_2_input", "test_2_output", "test_2_hidden",
    "test_3_input", "test_3_output", "test_3_hidden",
]
CODING_TEMPLATE_ROWS = [
    [
        "Sum of two numbers", "Read two integers and print their sum.",
        "One line with two integers a and b.", "One integer: a + b.", "-10^9 <= a, b <= 10^9",
        "2 3", "5", "2 + 3 = 5.",
        "python, java, c, cpp", "10", "",
        "1 2", "3", "no",
        "-5 5", "0", "yes",
        "1000000000 1000000000", "2000000000", "yes",
    ],
]

TRUE_WORDS = {"1", "y", "yes", "true", "hidden", "h"}
FALSE_WORDS = {"", "0", "n", "no", "false", "public", "p"}


class ImportProblem(Exception):
    """The whole file was refused. ``errors`` lists what to fix."""

    def __init__(self, errors):
        super().__init__("; ".join(errors[:3]))
        self.errors = errors


def template_csv(header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def _read_upload(uploaded):
    """Return (rows as dicts, {image name: bytes}). Accepts a .csv, or a .zip holding one .csv and images."""
    if uploaded.size > MAX_UPLOAD_BYTES:
        raise ImportProblem(["The file is larger than 50 MB."])
    name = (uploaded.name or "").lower()
    images = {}
    if name.endswith(".zip"):
        try:
            archive = zipfile.ZipFile(uploaded)
        except zipfile.BadZipFile as exc:
            raise ImportProblem(["This ZIP file could not be opened."]) from exc
        with archive:
            members = [m for m in archive.infolist() if not m.is_dir() and not m.filename.startswith("__MACOSX")]
            csvs = [m for m in members if m.filename.lower().endswith(".csv")]
            if len(csvs) != 1:
                raise ImportProblem(["The ZIP must contain exactly one .csv file."])
            if sum(m.file_size for m in members) > MAX_UPLOAD_BYTES * 2:
                raise ImportProblem(["The ZIP unpacks to more than 100 MB."])
            raw = archive.read(csvs[0])
            for member in members:
                ext = os.path.splitext(member.filename)[1].lower()
                if ext in QUESTION_IMAGE_TYPES:
                    if member.file_size > QUESTION_IMAGE_MAX_BYTES:
                        continue  # Reported when a row uses it.
                    images[os.path.basename(member.filename).lower()] = archive.read(member)
    elif name.endswith(".csv"):
        raw = uploaded.read()
    else:
        raise ImportProblem(["Upload a .csv file, or a .zip with the .csv and its images."])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ImportProblem(["The CSV file is empty."])
    reader.fieldnames = [(h or "").strip().lower() for h in reader.fieldnames]
    rows = []
    for row in reader:
        cleaned = {k: (v or "").strip() if isinstance(v, str) else "" for k, v in row.items() if k}
        if any(cleaned.values()):
            rows.append(cleaned)
    if not rows:
        raise ImportProblem(["The CSV has a header but no questions."])
    if len(rows) > MAX_ROWS:
        raise ImportProblem([f"Import at most {MAX_ROWS} rows at a time."])
    return rows, images


def _image_file(images, filename, row_no, errors):
    if not filename:
        return None
    key = os.path.basename(filename).lower()
    data = images.get(key)
    if data is None:
        errors.append(f"Row {row_no}: image '{filename}' is not in the ZIP (or is larger than 5 MB).")
        return None
    upload = ContentFile(data, name=key)
    upload.size = len(data)
    try:
        validate_question_image(upload)
    except ValidationError as exc:
        errors.append(f"Row {row_no}: image '{filename}': {exc.messages[0]}")
        return None
    upload.seek(0)
    return upload


def _option_columns(row):
    numbered = sorted(
        (int(m.group(1)), key) for key in row if (m := re.fullmatch(r"option_(\d+)", key))
    )
    return [row[key] for _, key in numbered]


def _correct_index(value, count):
    """'2' or 'B' -> 1 (zero-based). None when it does not point at an option."""
    value = value.strip().upper()
    if value.isdigit():
        index = int(value) - 1
    elif len(value) == 1 and value.isalpha():
        index = ord(value) - ord("A")
    else:
        return None
    return index if 0 <= index < count else None


def import_mcq(exam, uploaded):
    """Import MCQ questions. Returns (questions created, list of section titles created)."""
    rows, images = _read_upload(uploaded)
    if "question" not in rows[0]:
        raise ImportProblem(["The CSV needs a 'question' column. Download the template to see the columns."])

    sections = {s.title.strip().lower(): s for s in exam.sections.filter(kind=ExamSection.Kind.MCQ)}
    errors, parsed = [], []
    for row_no, row in enumerate(rows, start=2):
        text = row.get("question", "")
        options = _option_columns(row)
        if row.get("options"):
            options = [o.strip() for o in row["options"].split(OPTION_SEPARATOR)]
        option_images = [o.strip() for o in row.get("option_images", "").split(OPTION_SEPARATOR)] if row.get("option_images") else []
        count = max(len(options), len(option_images))
        options += [""] * (count - len(options))
        option_images += [""] * (count - len(option_images))
        # Keep each option's column number, so correct_option still points at the right one when a
        # column in the middle is left blank.
        numbered = [(i, t, img) for i, (t, img) in enumerate(zip(options, option_images, strict=True)) if t or img]
        choices = [(t, img) for _, t, img in numbered]

        question_image = _image_file(images, row.get("question_image", ""), row_no, errors)
        if not text and not question_image:
            errors.append(f"Row {row_no}: the question has no text or image.")
        if len(choices) < 2:
            errors.append(f"Row {row_no}: give at least two options (text or image).")
        column = _correct_index(row.get("correct_option", ""), count)
        positions = [i for i, _, _ in numbered]
        correct = positions.index(column) if column in positions else None
        if correct is None:
            errors.append(f"Row {row_no}: correct_option must be the number (1, 2, …) or letter (A, B, …) of an option that has text or an image.")
        marks = None
        if row.get("marks"):
            if not row["marks"].isdigit() or int(row["marks"]) < 1:
                errors.append(f"Row {row_no}: marks must be a whole number of 1 or more.")
            else:
                marks = int(row["marks"])
        choice_files = [(t, _image_file(images, img, row_no, errors)) for t, img in choices]
        parsed.append({
            "section": row.get("section", ""), "text": text, "image": question_image,
            "choices": choice_files, "correct": correct, "marks": marks,
        })
    if errors:
        raise ImportProblem(errors)

    created_sections = []
    with transaction.atomic():
        next_order = (exam.sections.aggregate(m=Max("order"))["m"] or 0) + 1
        question_order = (exam.mcq_questions.aggregate(m=Max("order"))["m"] or 0) + 1
        for item in parsed:
            title = item["section"]
            if title:
                section = sections.get(title.lower())
                if section is None:
                    section = ExamSection.objects.create(
                        exam=exam, title=title[:200], kind=ExamSection.Kind.MCQ,
                        order=next_order, duration_minutes=30,
                    )
                    sections[title.lower()] = section
                    created_sections.append(section.title)
                    next_order += 1
            else:
                section = exam.get_default_section()
            question = MCQQuestion(
                exam=exam, section=section, text=item["text"], order=question_order,
                marks=item["marks"] or section.marks_per_question,
            )
            if item["image"]:
                question.image.save(item["image"].name, item["image"], save=False)
            question.save()
            question_order += 1
            for index, (text, image) in enumerate(item["choices"]):
                option = MCQOption(question=question, text=text[:500], is_correct=index == item["correct"], order=index + 1)
                if image:
                    option.image.save(image.name, image, save=False)
                option.save()
    return len(parsed), created_sections


def _parse_bool(value):
    value = (value or "").strip().lower()
    if value in TRUE_WORDS:
        return True
    if value in FALSE_WORDS:
        return False
    return None


def import_coding(exam, uploaded):
    """Import coding problems with their test cases. Returns the number created."""
    rows, _images = _read_upload(uploaded)
    if "title" not in rows[0] or "statement" not in rows[0]:
        raise ImportProblem(["The CSV needs 'title' and 'statement' columns. Download the template to see the columns."])

    errors, parsed = [], []
    for row_no, row in enumerate(rows, start=2):
        if not row.get("title"):
            errors.append(f"Row {row_no}: title is empty.")
        if not row.get("statement"):
            errors.append(f"Row {row_no}: statement is empty.")
        langs = [x.strip().lower() for x in (row.get("languages") or "python, java, c, cpp").split(",") if x.strip()]
        langs = ["cpp" if x == "c++" else x for x in langs]
        bad = [x for x in langs if x not in SUPPORTED_LANGUAGES]
        if bad:
            errors.append(f"Row {row_no}: unsupported language(s) {', '.join(bad)}. Use: {', '.join(SUPPORTED_LANGUAGES)}.")
        marks = row.get("marks") or "10"
        if not marks.isdigit() or int(marks) < 1:
            errors.append(f"Row {row_no}: marks must be a whole number of 1 or more.")
        numbers = sorted({int(m.group(1)) for key in row if (m := re.fullmatch(r"test_(\d+)_(input|output|hidden)", key))})
        cases = []
        for n in numbers:
            case_in, case_out = row.get(f"test_{n}_input", ""), row.get(f"test_{n}_output", "")
            if not case_in and not case_out:
                continue
            if not case_out:
                errors.append(f"Row {row_no}: test {n} has an input but no expected output.")
                continue
            hidden = _parse_bool(row.get(f"test_{n}_hidden", ""))
            if hidden is None:
                errors.append(f"Row {row_no}: test_{n}_hidden must be yes or no.")
                continue
            cases.append((case_in, case_out, hidden))
        if not cases:
            errors.append(f"Row {row_no}: add at least one test case (test_1_input, test_1_output).")
        parsed.append({"row": row, "langs": ", ".join(dict.fromkeys(langs)), "marks": marks, "cases": cases})
    if errors:
        raise ImportProblem(errors)

    with transaction.atomic():
        order = (exam.coding_problems.aggregate(m=Max("order"))["m"] or 0) + 1
        for item in parsed:
            row = item["row"]
            problem = CodingProblem.objects.create(
                exam=exam, title=row["title"][:200], statement=row["statement"],
                input_spec=row.get("input_format", ""), output_spec=row.get("output_format", ""),
                constraints=row.get("constraints", ""), example_input=row.get("example_input", ""),
                example_output=row.get("example_output", ""), example_explanation=row.get("example_explanation", ""),
                starter_code=row.get("starter_code", ""), languages=item["langs"], marks=int(item["marks"]), order=order,
            )
            order += 1
            for index, (case_in, case_out, hidden) in enumerate(item["cases"], start=1):
                CodingTestCase.objects.create(
                    problem=problem, input_data=case_in, expected_output=case_out, is_hidden=hidden, order=index,
                )
    return len(parsed)
