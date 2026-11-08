import os
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

# ---------------------------------------------------------------------------
# Authoring: exams, sections, questions
# ---------------------------------------------------------------------------


# A coding submission that fails with a sandbox error is retried this many times, then left for staff.
MAX_GRADING_ATTEMPTS = 3

# Languages candidates may use. Each must be supported by the runner (see runners.py).
SUPPORTED_LANGUAGES = ("python", "java", "c", "cpp", "javascript")


class Exam(models.Model):
    """An assessment configured by staff. Candidates see it only once published."""

    title = models.CharField(max_length=200)
    instructions = models.TextField(
        help_text="Shown to candidates before they start. Plain text; line breaks are kept."
    )
    email_instructions = models.TextField(
        blank=True,
        default="",
        help_text="Extra instructions added to this test's login email, one per line. Leave blank to use only the site-wide ones.",
    )
    opens_at = models.DateTimeField()
    closes_at = models.DateTimeField()
    duration_minutes = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        default=60, help_text="Total permitted time for one attempt."
    )
    shuffle_questions = models.BooleanField(default=True)
    shuffle_options = models.BooleanField(default=True)
    camera_required = models.BooleanField(default=True)
    fullscreen_required = models.BooleanField(
        default=True,
        help_text="Candidates must keep the test in full-screen mode. Each time they leave it, the event is recorded.",
    )
    snapshots_enabled = models.BooleanField(
        default=True,
        help_text="Capture a low-resolution image when a proctoring event occurs, with candidate consent.",
    )

    # Integrity and recovery policy. Copied onto each attempt when it starts.
    tab_switch_threshold = models.PositiveIntegerField(
        default=3, help_text="Tab/window switches before the attempt is flagged for review."
    )
    missing_face_seconds = models.PositiveIntegerField(
        default=5, help_text="Continuous seconds without a detected face before an event is recorded."
    )
    network_grace_seconds = models.PositiveIntegerField(
        default=20, help_text="Silence from the candidate's browser tolerated before it counts as an interruption."
    )
    pause_enabled = models.BooleanField(
        default=True, help_text="Whether interruptions extend the candidate's time."
    )
    max_pause_minutes = models.PositiveIntegerField(
        default=5, help_text="Maximum cumulative time interruptions may add to one attempt."
    )
    interview_rounds_count = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Total number of interview rounds configured for this assessment (staff-only).",
    )

    is_published = models.BooleanField(
        default=False,
        help_text="Set by the pre-publish check. Candidates can see the test only while it is published.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="exams_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.opens_at and self.closes_at and self.closes_at <= self.opens_at:
            raise ValidationError({"closes_at": "The closing time must be after the opening time."})

    @property
    def is_open(self):
        now = timezone.now()
        return self.is_published and self.opens_at <= now < self.closes_at

    def policy_snapshot(self):
        """The rules an attempt runs under. Stored on the attempt so later edits cannot change it."""
        return {
            "shuffle_questions": self.shuffle_questions,
            "shuffle_options": self.shuffle_options,
            "camera_required": self.camera_required,
            "fullscreen_required": self.fullscreen_required,
            "snapshots_enabled": self.snapshots_enabled,
            "tab_switch_threshold": self.tab_switch_threshold,
            "missing_face_seconds": self.missing_face_seconds,
            "network_grace_seconds": self.network_grace_seconds,
            "pause_enabled": self.pause_enabled,
            "max_pause_seconds": self.max_pause_minutes * 60,
            "duration_seconds": self.duration_minutes * 60,
            "closes_at": self.closes_at.isoformat(),
        }

    def get_default_section(self):
        """Returns or creates the default MCQ section for this exam."""
        default_sec = self.sections.filter(is_default=True).first()
        if default_sec:
            return default_sec
        first_mcq = self.sections.filter(kind=ExamSection.Kind.MCQ).order_by("order", "id").first()
        if first_mcq:
            first_mcq.is_default = True
            first_mcq.save(update_fields=["is_default"])
            return first_mcq
        max_order = self.sections.aggregate(m=models.Max("order"))["m"] or 0
        return self.sections.create(
            title="Default Section",
            kind=ExamSection.Kind.MCQ,
            order=max_order + 1 if max_order else 1,
            duration_minutes=self.duration_minutes,
            question_count=0,
            is_default=True,
        )


class ExamSection(models.Model):
    """A timed part of an exam. Sections run in order and cannot be reopened."""

    class Kind(models.TextChoices):
        MCQ = "mcq", "Multiple choice"
        CODING = "coding", "Coding"

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="sections")
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.MCQ)
    order = models.PositiveIntegerField(default=1)
    duration_minutes = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],default=30)
    marks_per_question = models.PositiveIntegerField(
        default=1,
        help_text="MCQ sections: default marks for each new question. Each question can still be changed.",
    )
    question_count = models.PositiveIntegerField(
        default=0,
        help_text="MCQ sections: how many questions each candidate receives. 0 means all questions.",
    )
    coding_selection_count = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        default=2, help_text="Coding sections: how many problems each candidate receives from the pool."
    )
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["exam", "order"], name="unique_section_order_per_exam")
        ]

    def __str__(self):
        return f"{self.exam.title}: {self.title}"


def question_image_path(instance, filename):
    """Random names, so an image URL never reveals which question or option it belongs to."""
    ext = os.path.splitext(filename)[1].lower()[:8] or ".png"
    return f"question_images/{uuid.uuid4().hex}{ext}"


class MCQQuestion(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="mcq_questions")
    section = models.ForeignKey(
        ExamSection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="questions",
        help_text="The section this MCQ question belongs to. If not set, belongs to default section.",
    )
    text = models.TextField()
    image = models.FileField(
        upload_to=question_image_path, max_length=255, null=True, blank=True,
        help_text="Optional picture shown with the question, such as a diagram or code screenshot.",
    )
    marks = models.PositiveIntegerField(default=1)
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.text[:80]

    @property
    def effective_section(self):
        return self.section or self.exam.get_default_section()

    def save(self, *args, **kwargs):
        if not self.section_id and self.exam_id:
            self.section = self.exam.get_default_section()
        super().save(*args, **kwargs)


class MCQOption(models.Model):
    question = models.ForeignKey(MCQQuestion, on_delete=models.CASCADE, related_name="options")
    text = models.CharField(max_length=500, blank=True, help_text="Can be left blank when the option is an image.")
    image = models.FileField(upload_to=question_image_path, max_length=255, null=True, blank=True)
    is_correct = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.text or "(image option)"


class CodingProblem(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="coding_problems")
    title = models.CharField(max_length=200)
    statement = models.TextField()
    input_spec = models.TextField(blank=True)
    output_spec = models.TextField(blank=True)
    constraints = models.TextField(blank=True)
    example_input = models.TextField(blank=True, help_text="One worked example: the input.")
    example_output = models.TextField(blank=True, help_text="The expected output for the example input.")
    example_explanation = models.TextField(blank=True, help_text="Why that input gives that output.")
    starter_code = models.TextField(blank=True)
    languages = models.CharField(
        max_length=200,
        default="python, java, c, cpp",
        help_text="Comma-separated languages candidates may use. Supported: python, java, c, cpp, javascript",
    )
    marks = models.PositiveIntegerField(default=10)
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.title

    @property
    def language_list(self):
        langs = []
        for raw in self.languages.split(","):
            cleaned = raw.strip().lower()
            if cleaned == "c++":
                cleaned = "cpp"
            if cleaned and cleaned not in langs:
                langs.append(cleaned)
        return langs


class CodingTestCase(models.Model):
    """Public cases are shown to candidates. Hidden cases are used only for grading."""

    problem = models.ForeignKey(CodingProblem, on_delete=models.CASCADE, related_name="test_cases")
    input_data = models.TextField(blank=True)
    expected_output = models.TextField()
    is_hidden = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["is_hidden", "order", "id"]

    def __str__(self):
        return f"{self.problem.title} case {self.order}"


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


class Application(models.Model):
    """A candidate's request to take an exam. Only approved candidates may start it."""

    class Status(models.TextChoices):
        APPROVED = "approved", "Approved"
        REVOKED = "revoked", "Access revoked"

    candidate = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="applications"
    )
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="applications")
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, blank=True)
    resume = models.FileField(upload_to="resumes/", null=True, blank=True, max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.APPROVED)
    decision_reason = models.TextField(blank=True, help_text="Why access was revoked, if it was.")
    plain_password = models.CharField(max_length=128, blank=True, default="", help_text="Last generated password for candidate login.")
    credentials_sent_at = models.DateTimeField(null=True, blank=True)
    details_confirmed_at = models.DateTimeField(null=True, blank=True)
    monitoring_consent_at = models.DateTimeField(
        null=True, blank=True, help_text="When the candidate agreed to the monitoring notice.",
    )
    monitoring_consent_text = models.TextField(
        blank=True, default="", help_text="The exact monitoring notice the candidate agreed to.",
    )
    pending_email = models.EmailField(blank=True, default="", help_text="Unverified new email address requested by candidate.")
    email_verification_token = models.CharField(max_length=64, blank=True, default="", help_text="SHA-256 hash of token for verifying email change.")
    email_verification_sent_at = models.DateTimeField(null=True, blank=True)
    setup_token_hash = models.CharField(max_length=64, blank=True, default="", help_text="SHA-256 hash of single-use setup/login token.")
    setup_token_expires_at = models.DateTimeField(null=True, blank=True)
    setup_token_used_at = models.DateTimeField(null=True, blank=True)
    ai_interview_questions = models.JSONField(
        default=list,
        blank=True,
        help_text="AI-generated face-to-face interview questions and expected answers based on candidate resume keywords.",
    )
    ai_questions_generated_at = models.DateTimeField(null=True, blank=True)
    ai_questions_error = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications_changed",
    )
    changed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["candidate", "exam"], name="one_application_per_exam")
        ]

    def __str__(self):
        return f"{self.full_name} – {self.exam.title} ({self.get_status_display()})"

    @property
    def has_resume_file(self):
        """Returns True only if a resume is attached and the file exists on storage."""
        if not self.resume or not getattr(self.resume, "name", None):
            return False
        try:
            return bool(self.resume.storage.exists(self.resume.name))
        except Exception:
            return False

    def ensure_password(self):
        """Ensures this application has a visible plain password for admin, synchronized with User password hash."""
        if not self.plain_password:
            from . import notifications
            pw = notifications.new_password()
            self.plain_password = pw
            if self.pk:
                Application.objects.filter(pk=self.pk).update(plain_password=pw)
            if self.candidate_id:
                try:
                    cand = self.candidate
                    cand.set_password(pw)
                    cand.save(update_fields=["password"])
                except Exception:
                    pass
        return self.plain_password

    def save(self, *args, **kwargs):
        is_new_pw = False
        if not self.plain_password:
            from . import notifications
            self.plain_password = notifications.new_password()
            is_new_pw = True
        super().save(*args, **kwargs)
        if is_new_pw and self.candidate_id:
            try:
                cand = self.candidate
                if not cand.has_usable_password():
                    cand.set_password(self.plain_password)
                    cand.save(update_fields=["password"])
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Attempts: the single authoritative record of a candidate's exam
# ---------------------------------------------------------------------------


class Attempt(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        SUBMITTED = "submitted", "Submitted"
        EXPIRED = "expired", "Time expired"
        CANCELLED = "cancelled", "Cancelled"

    FINAL_STATUSES = (Status.SUBMITTED, Status.EXPIRED, Status.CANCELLED)

    candidate = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="attempts"
    )
    exam = models.ForeignKey(Exam, on_delete=models.PROTECT, related_name="attempts")
    application = models.ForeignKey(Application, on_delete=models.PROTECT, related_name="attempts")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IN_PROGRESS)
    policy_snapshot = models.JSONField(default=dict)
    # SHA-256 of the browser session the test is bound to. Empty until the first action.
    session_fingerprint = models.CharField(max_length=64, blank=True, default="")
    started_at = models.DateTimeField()
    deadline_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    readiness_confirmed_at = models.DateTimeField(null=True, blank=True)
    snapshot_consent_at = models.DateTimeField(null=True, blank=True)
    total_paused_seconds = models.PositiveIntegerField(default=0)
    flagged_for_review = models.BooleanField(default=False)
    mcq_score = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    coding_score = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    device_info = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(fields=["candidate", "exam"], name="one_attempt_per_candidate_exam")
        ]

    def __str__(self):
        return f"{self.candidate.username} – {self.exam.title} ({self.get_status_display()})"

    @property
    def is_final(self):
        return self.status in self.FINAL_STATUSES

    @property
    def total_score(self):
        if self.mcq_score is not None or self.coding_score is not None:
            return round(float(self.mcq_score or 0) + float(self.coding_score or 0), 2)
        return None

    @property
    def interview_rounds_status(self):
        """
        Calculates completion status for each interview round configured on the exam.
        Returns detailed summary and per-round list.
        """
        total_rounds = getattr(self.exam, "interview_rounds_count", 1) or 1
        reviews = list(self.reviews.all().order_by("round_number", "-created_at"))
        reviews_by_round = {}
        for r in reviews:
            if r.round_number not in reviews_by_round:
                reviews_by_round[r.round_number] = r

        rounds_list = []
        has_rejection = False
        all_passed = True
        current_active_round = 1

        for rnd in range(1, total_rounds + 1):
            rev = reviews_by_round.get(rnd)
            if rev:
                is_done = True
                if rev.decision == "recommended" or rev.selected_for_next_round:
                    rnd_status = "selected"
                    label = f"Round {rnd}: Done ✓ (Selected)"
                elif rev.decision == "not_recommended":
                    rnd_status = "rejected"
                    label = f"Round {rnd}: Done ✕ (Rejected)"
                    has_rejection = True
                    all_passed = False
                else:
                    rnd_status = "on_hold"
                    label = f"Round {rnd}: Done ⏳ (On Hold)"
                    all_passed = False
            else:
                is_done = False
                all_passed = False
                if has_rejection:
                    rnd_status = "skipped"
                    label = f"Round {rnd}: Skipped"
                elif rnd == 1 or (rnd > 1 and reviews_by_round.get(rnd - 1) and (reviews_by_round[rnd - 1].decision == "recommended" or reviews_by_round[rnd - 1].selected_for_next_round)):
                    rnd_status = "pending"
                    label = f"Round {rnd}: Pending ⏳"
                    if current_active_round == 1 and reviews_by_round.get(1):
                        current_active_round = rnd
                else:
                    rnd_status = "locked"
                    label = f"Round {rnd}: Locked"

            rounds_list.append({
                "round_number": rnd,
                "is_done": is_done,
                "status": rnd_status,
                "label": label,
                "review": rev,
                "interviewer_name": rev.interviewer_name if rev else "",
            })

        # Calculate next pending round
        for r_info in rounds_list:
            if not r_info["is_done"] and r_info["status"] == "pending":
                current_active_round = r_info["round_number"]
                break
        else:
            current_active_round = total_rounds

        if has_rejection:
            overall = "rejected"
            overall_label = "Rejected ✕"
        elif all_passed and len(reviews_by_round) >= total_rounds:
            overall = "selected"
            overall_label = "Selected (Hire) ★"
        elif len(reviews_by_round) > 0:
            overall = "in_progress"
            overall_label = f"Round {current_active_round} Pending ⏳"
        else:
            overall = "not_started"
            overall_label = "Round 1 Pending ⏳"

        return {
            "total_rounds": total_rounds,
            "rounds": rounds_list,
            "overall_status": overall,
            "overall_label": overall_label,
            "current_round": current_active_round,
            "completed_rounds_count": len(reviews_by_round),
            "latest_review": reviews[0] if reviews else None,
        }



class AttemptSection(models.Model):
    class Status(models.TextChoices):
        LOCKED = "locked", "Locked"
        IN_PROGRESS = "in_progress", "In progress"
        SUBMITTED = "submitted", "Submitted"
        EXPIRED = "expired", "Expired"

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="sections")
    section = models.ForeignKey(ExamSection, on_delete=models.PROTECT, related_name="attempt_sections")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.LOCKED)
    started_at = models.DateTimeField(null=True, blank=True)
    deadline_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["section__order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["attempt", "section"], name="one_attempt_section")
        ]


class AttemptMCQ(models.Model):
    """A question as assigned to one candidate: its position and the option order they see."""

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="mcq_items")
    section = models.ForeignKey(ExamSection, on_delete=models.PROTECT, related_name="+")
    question = models.ForeignKey(MCQQuestion, on_delete=models.PROTECT, related_name="+")
    position = models.PositiveIntegerField()
    option_order = models.JSONField(default=list, help_text="MCQOption IDs in the order shown.")

    class Meta:
        ordering = ["section__order", "position"]
        constraints = [
            models.UniqueConstraint(fields=["attempt", "question"], name="one_mcq_per_attempt"),
        ]


class AttemptCoding(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="coding_items")
    section = models.ForeignKey(ExamSection, on_delete=models.PROTECT, related_name="+")
    problem = models.ForeignKey(CodingProblem, on_delete=models.PROTECT, related_name="+")
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["section__order", "position"]
        constraints = [
            models.UniqueConstraint(fields=["attempt", "problem"], name="one_problem_per_attempt"),
        ]


class Answer(models.Model):
    """The candidate's current answer. Revisions keep the history."""

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(MCQQuestion, on_delete=models.PROTECT, related_name="+")
    option = models.ForeignKey(MCQOption, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    change_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["attempt", "question"], name="one_answer_per_question"),
        ]


class AnswerRevision(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="answer_revisions")
    question = models.ForeignKey(MCQQuestion, on_delete=models.CASCADE, related_name="+")
    option = models.ForeignKey(MCQOption, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["attempt", "created_at"])]


class QuestionVisit(models.Model):
    """One continuous look at a question. Times are server-recorded; active time excludes pauses."""

    class Kind(models.TextChoices):
        MCQ = "mcq", "Multiple choice"
        CODING = "coding", "Coding"

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="visits")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    mcq_question = models.ForeignKey(MCQQuestion, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    coding_problem = models.ForeignKey(CodingProblem, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    visit_number = models.PositiveIntegerField()
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    active_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["started_at", "id"]
        indexes = [models.Index(fields=["attempt", "started_at"])]


class ConnectionEvent(models.Model):
    """A period where the candidate's browser was silent longer than the grace period."""

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="connection_events")
    last_seen_before = models.DateTimeField()
    detected_at = models.DateTimeField()
    gap_seconds = models.PositiveIntegerField()
    granted_seconds = models.PositiveIntegerField(default=0)
    denied_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["detected_at"]


class ProctoringEvent(models.Model):
    class Kind(models.TextChoices):
        TAB_HIDDEN = "tab_hidden", "Tab hidden"
        WINDOW_BLUR = "window_blur", "Window lost focus"
        FACE_MISSING = "face_missing", "Face not detected"
        MULTIPLE_FACES = "multiple_faces", "Multiple faces"
        HEAD_TURN = "head_turn", "Sustained head turn"
        CAMERA_LOST = "camera_lost", "Camera stream interrupted"
        CAMERA_REVOKED = "camera_revoked", "Camera permission revoked"
        CAMERA_READY = "camera_ready", "Camera ready check passed"
        AI_UNAVAILABLE = "ai_unavailable", "Face check unavailable on this device"
        SESSION_CHANGED = "session_changed", "Tried to continue from another browser session"
        FULLSCREEN_EXIT = "fullscreen_exit", "Left full-screen mode"
        CLIPBOARD = "clipboard", "Copy, cut or paste used in the test"
        DEVTOOLS_ATTEMPT = "devtools_attempt", "Developer tools shortcut attempted"

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="proctoring_events")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    sequence = models.PositiveIntegerField()
    client_at = models.DateTimeField(null=True, blank=True, help_text="Reported by the browser; not trusted.")
    server_at = models.DateTimeField()
    duration_ms = models.PositiveIntegerField(default=0)
    context = models.JSONField(default=dict, blank=True)
    over_threshold = models.BooleanField(default=False)

    class Meta:
        ordering = ["server_at", "sequence"]
        indexes = [models.Index(fields=["attempt", "server_at"])]
        constraints = [
            models.UniqueConstraint(fields=["attempt", "sequence"], name="unique_event_sequence"),
        ]


class ProctoringSnapshot(models.Model):
    class ReviewStatus(models.TextChoices):
        PENDING = "pending", "Pending review"
        REVIEWED = "reviewed", "Reviewed"
        FLAGGED = "flagged", "Flagged"

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="snapshots")
    event = models.ForeignKey(ProctoringEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="snapshots")
    relative_path = models.CharField(max_length=255, unique=True)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    content_type = models.CharField(max_length=30)
    client_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    review_status = models.CharField(max_length=10, choices=ReviewStatus.choices, default=ReviewStatus.PENDING)

    class Meta:
        ordering = ["received_at"]


# ---------------------------------------------------------------------------
# Coding
# ---------------------------------------------------------------------------


class CodingDraft(models.Model):
    """Autosaved source code. Kept separately from graded submissions."""

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="coding_drafts")
    problem = models.ForeignKey(CodingProblem, on_delete=models.CASCADE, related_name="+")
    language = models.CharField(max_length=20)
    source_code = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["attempt", "problem"], name="one_draft_per_problem"),
        ]


class CodingSubmission(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Graded"
        RUNNER_ERROR = "runner_error", "Runner error"

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="coding_submissions")
    problem = models.ForeignKey(CodingProblem, on_delete=models.CASCADE, related_name="submissions")
    version = models.PositiveIntegerField()
    language = models.CharField(max_length=20)
    source_code = models.TextField()
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.QUEUED)
    submitted_at = models.DateTimeField(auto_now_add=True)
    graded_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_by = models.CharField(max_length=64, blank=True, default="")
    total_cases = models.PositiveIntegerField(default=0)
    public_passed = models.PositiveIntegerField(default=0)
    hidden_passed = models.PositiveIntegerField(default=0)
    marks_awarded = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    compile_output = models.TextField(blank=True)
    runner_error = models.TextField(blank=True)
    grading_attempts = models.PositiveSmallIntegerField(default=0)
    # Final submissions are the candidate's answer. They lock the problem; nothing about them is shown to candidates.
    is_final = models.BooleanField(default=False)

    class Meta:
        ordering = ["-version"]
        indexes = [
            models.Index(fields=["status", "submitted_at"]),
            models.Index(fields=["attempt", "problem", "is_final"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["attempt", "problem", "version"], name="unique_submission_version"),
        ]


class CodingTestResult(models.Model):
    class Status(models.TextChoices):
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        TIMEOUT = "timeout", "Time limit exceeded"
        RUNTIME_ERROR = "runtime_error", "Runtime error"
        COMPILE_ERROR = "compile_error", "Compilation error"
        SKIPPED = "skipped", "Not run"

    submission = models.ForeignKey(CodingSubmission, on_delete=models.CASCADE, related_name="results")
    test_case = models.ForeignKey(CodingTestCase, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=14, choices=Status.choices)
    time_ms = models.PositiveIntegerField(default=0)
    memory_kb = models.PositiveIntegerField(default=0)
    stdout_preview = models.TextField(blank=True, help_text="Truncated. Staff review only for hidden cases.")

    class Meta:
        ordering = ["test_case__is_hidden", "test_case__order"]
        constraints = [
            models.UniqueConstraint(fields=["submission", "test_case"], name="one_result_per_case"),
        ]


# ---------------------------------------------------------------------------
# Review and audit
# ---------------------------------------------------------------------------


class InterviewReview(models.Model):
    class Decision(models.TextChoices):
        RECOMMENDED = "recommended", "Selected / Advance to Next Round"
        NOT_RECOMMENDED = "not_recommended", "Rejected"
        FURTHER_REVIEW = "further_review", "Further Review / On Hold"

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="reviews")
    round_number = models.PositiveIntegerField(default=1, help_text="Interview round number (1, 2, ...)")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="interview_reviews"
    )
    interviewer_name = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Signature name of the interviewer who took this round.",
    )
    candidate_summary = models.TextField(
        blank=True,
        default="",
        help_text="Short info about candidate background, communication, and demeanor.",
    )
    decision = models.CharField(max_length=20, choices=Decision.choices, default=Decision.RECOMMENDED)
    selected_for_next_round = models.BooleanField(
        default=False,
        help_text="Whether candidate is selected for next round or final hire.",
    )
    interview_score = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total marks scored in this round.",
    )
    max_interview_score = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Maximum possible marks for this round.",
    )
    notes = models.TextField(blank=True, default="", help_text="Detailed feedback and notes from interviewer.")
    questions_data = models.JSONField(
        default=list,
        blank=True,
        help_text="Saved state of interview questions (answered/unanswered, marks).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["round_number", "-created_at"]

    def __str__(self):
        name = self.interviewer_name or (self.reviewer.username if self.reviewer else "Staff")
        return f"Round {self.round_number} ({self.get_decision_display()}) by {name}"


class AuditLog(models.Model):
    """Records administrative decisions and important system actions."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="audit_entries"
    )
    action = models.CharField(max_length=100)
    target = models.CharField(max_length=200)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor} {self.action} {self.target}"


# ---------------------------------------------------------------------------
# Site settings
# ---------------------------------------------------------------------------


DEFAULT_LOGIN_EMAIL_SUBJECT = "Your login for {test}"
DEFAULT_LOGIN_EMAIL_INTRO = "Your login for the assessment \"{test}\" is ready."
DEFAULT_LOGIN_EMAIL_INSTRUCTIONS = (
    "Keep this email private. Anyone with the password can sign in as you.\n"
    "Use a laptop or desktop with a working webcam, and a stable internet connection.\n"
    "Once you start, the timer runs. If you are disconnected, sign in again quickly: "
    "the test still ends when its window closes.\n"
    "If you lose this email, ask the test administrator to resend your login details."
)
DEFAULT_MONITORING_NOTICE = (
    "Your webcam video is analysed during the test, and still images are saved when something unusual happens.\n"
    "Your approximate location is recorded when you start.\n"
    "Your network details are recorded: IP address, connection type and speed.\n"
    "Your browser and device details are recorded, such as screen size and operating system.\n"
    "Leaving full screen, switching tabs or windows, and copy and paste are recorded.\n"
    "Your answers, code, and the time you spend on each question are recorded.\n"
    "This information is used only to review this assessment, is visible only to the hiring team, "
    "and evidence images are deleted after the retention period."
)


class SiteSettings(models.Model):
    """Single row of settings that staff can change in the interface."""

    site_name = models.CharField(max_length=150, default="Assessments", blank=True)
    site_logo = models.FileField(upload_to="branding/", null=True, blank=True, help_text="Brand logo image displayed in the header")
    favicon = models.FileField(upload_to="branding/", null=True, blank=True, help_text="Website favicon (.ico, .png, .svg)")
    snapshot_retention_days = models.PositiveIntegerField(
        default=60,
        validators=[MinValueValidator(1), MaxValueValidator(365)],
        help_text="Evidence images older than this many days are deleted by the daily purge.",
    )
    footer_copyright = models.CharField(
        max_length=255,
        default="© 2026 Assessments. All rights reserved.",
        blank=True,
        help_text="Copyright notice displayed on the left of the footer.",
    )
    footer_text = models.CharField(
        max_length=255,
        default="Secure Online Assessment Platform",
        blank=True,
        help_text="Custom platform text or tagline displayed in the footer.",
    )
    login_headline = models.CharField(
        max_length=120,
        default="Technical Assessments.",
        blank=True,
        help_text="Large heading on the sign-in page.",
    )
    login_email_subject = models.CharField(
        max_length=200,
        default=DEFAULT_LOGIN_EMAIL_SUBJECT,
        blank=True,
        help_text="Subject of the login email. {test} is replaced by the test name and {site} by the site name.",
    )
    login_email_intro = models.TextField(
        default=DEFAULT_LOGIN_EMAIL_INTRO,
        blank=True,
        help_text="Opening paragraph of the login email. {test} and {site} are replaced as in the subject.",
    )
    login_email_instructions = models.TextField(
        default=DEFAULT_LOGIN_EMAIL_INSTRUCTIONS,
        blank=True,
        help_text="Instructions listed in every login email, one per line.",
    )
    monitoring_notice_title = models.CharField(
        max_length=200,
        default="Before you begin: how this test is monitored",
        blank=True,
    )
    monitoring_notice = models.TextField(
        default=DEFAULT_MONITORING_NOTICE,
        blank=True,
        help_text="What we collect during a test, one item per line. Candidates must agree before they can start.",
    )
    gemini_api_key = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Google Gemini API Key for generating face-to-face interview questions from candidate resumes.",
    )
    gemini_model = models.CharField(
        max_length=64,
        default="gemini-1.5-flash",
        blank=True,
        help_text="Gemini model identifier (e.g., gemini-1.5-flash, gemini-1.5-pro).",
    )

    class Meta:
        verbose_name = "site settings"
        verbose_name_plural = "site settings"

    def __str__(self):
        return "Site settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def monitoring_notice_lines(self):
        return [line.strip() for line in (self.monitoring_notice or "").splitlines() if line.strip()]



# ---------------------------------------------------------------------------
# Team: staff roles and members
# ---------------------------------------------------------------------------


class StaffRole(models.Model):
    """A named set of permissions an admin gives to staff members, such as "Interviewer"."""

    name = models.CharField(max_length=80, unique=True)
    description = models.CharField(max_length=255, blank=True, default="")
    permissions = models.JSONField(default=list, blank=True, help_text="Permission codes from assessments.permissions.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class StaffProfile(models.Model):
    """Marks a user account as a staff member (not a candidate) and holds their role."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_profile")
    role = models.ForeignKey(StaffRole, on_delete=models.SET_NULL, null=True, blank=True, related_name="members")
    title = models.CharField(max_length=120, blank=True, default="", help_text="Job title shown to colleagues, e.g. Senior Engineer.")
    is_active = models.BooleanField(default=True, help_text="Inactive staff keep their history but lose all permissions.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user__email"]

    def __str__(self):
        return f"{self.user} ({self.role or 'no role'})"
