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


class ExamSection(models.Model):
    """A timed part of an exam. Sections run in order and cannot be reopened."""

    class Kind(models.TextChoices):
        MCQ = "mcq", "Multiple choice"
        CODING = "coding", "Coding"

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="sections")
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    order = models.PositiveIntegerField(default=1)
    duration_minutes = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],default=30)
    question_count = models.PositiveIntegerField(
        default=0,
        help_text="MCQ sections: how many questions each candidate receives. 0 means all questions.",
    )
    coding_selection_count = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        default=2, help_text="Coding sections: how many problems each candidate receives from the pool."
    )

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["exam", "order"], name="unique_section_order_per_exam")
        ]

    def __str__(self):
        return f"{self.exam.title}: {self.title}"


class MCQQuestion(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="mcq_questions")
    text = models.TextField()
    marks = models.PositiveIntegerField(default=1)
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.text[:80]


class MCQOption(models.Model):
    question = models.ForeignKey(MCQQuestion, on_delete=models.CASCADE, related_name="options")
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.text


class CodingProblem(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="coding_problems")
    title = models.CharField(max_length=200)
    statement = models.TextField()
    input_spec = models.TextField(blank=True)
    output_spec = models.TextField(blank=True)
    constraints = models.TextField(blank=True)
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
    credentials_sent_at = models.DateTimeField(null=True, blank=True)
    details_confirmed_at = models.DateTimeField(null=True, blank=True)
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
        RECOMMENDED = "recommended", "Recommended"
        NOT_RECOMMENDED = "not_recommended", "Not recommended"
        FURTHER_REVIEW = "further_review", "Further review required"

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="reviews")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="interview_reviews"
    )
    decision = models.CharField(max_length=20, choices=Decision.choices)
    notes = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_decision_display()} by {self.reviewer}"


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

    class Meta:
        verbose_name = "site settings"
        verbose_name_plural = "site settings"

    def __str__(self):
        return "Site settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

