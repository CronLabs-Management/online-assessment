import os
import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from .models import (
    SUPPORTED_LANGUAGES,
    Application,
    CodingProblem,
    CodingTestCase,
    Exam,
    ExamSection,
    InterviewReview,
    MCQOption,
    MCQQuestion,
    SiteSettings,
)

User = get_user_model()

DATETIME_INPUT = forms.DateTimeInput(
    attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
)


class ExamForm(forms.ModelForm):
    class Meta:
        model = Exam
        fields = [
            "title",
            "instructions",
            "opens_at",
            "closes_at",
            "duration_minutes",
            "fullscreen_required",
            "tab_switch_threshold",
            "camera_required",
            "snapshots_enabled",
            "missing_face_seconds",
            "network_grace_seconds",
            "pause_enabled",
            "max_pause_minutes",
            "shuffle_questions",
            "shuffle_options",
        ]
        labels = {
            "fullscreen_required": "Enforce full-screen mode (candidates cannot leave full screen)",
            "tab_switch_threshold": "Screen switch warning threshold (exits / tab switches before attempt is flagged)",
            "camera_required": "Require webcam readiness check before starting test",
            "snapshots_enabled": "Capture webcam snapshots when proctoring events occur",
            "missing_face_seconds": "Missing face threshold (seconds face is missing before event logged)",
            "network_grace_seconds": "Disconnection grace period (seconds tolerated without pause penalty)",
            "pause_enabled": "Grant pause credit for disconnections longer than grace period",
            "max_pause_minutes": "Maximum cumulative pause credit allowed (minutes)",
            "shuffle_questions": "Randomize question ordering per candidate",
            "shuffle_options": "Randomize multiple choice options order per candidate",
        }
        widgets = {
            "instructions": forms.Textarea(attrs={"rows": 8}),
            "opens_at": DATETIME_INPUT,
            "closes_at": DATETIME_INPUT,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "h-4 w-4")
            else:
                field.widget.attrs.setdefault("class", "w-full rounded border px-3 py-2")
        self.fields["opens_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["closes_at"].input_formats = ["%Y-%m-%dT%H:%M"]


class ExamSectionForm(forms.ModelForm):
    class Meta:
        model = ExamSection
        fields = ["title", "kind", "order", "duration_minutes", "question_count", "coding_selection_count"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "w-full rounded border px-3 py-2")


class MCQQuestionForm(forms.ModelForm):
    class Meta:
        model = MCQQuestion
        fields = ["text", "marks", "order"]
        widgets = {"text": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "w-full rounded border px-3 py-2")


class MCQOptionForm(forms.ModelForm):
    class Meta:
        model = MCQOption
        fields = ["text", "is_correct"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["text"].widget.attrs.update({"class": "w-full rounded border px-3 py-2"})
        self.fields["is_correct"].widget.attrs.update({"class": "h-4 w-4"})


MCQOptionFormSet = inlineformset_factory(
    MCQQuestion,
    MCQOption,
    form=MCQOptionForm,
    extra=4,
    can_delete=True,
)

MCQOptionEditFormSet = inlineformset_factory(
    MCQQuestion,
    MCQOption,
    form=MCQOptionForm,
    extra=2,
    can_delete=True,
)


class CodingProblemForm(forms.ModelForm):
    class Meta:
        model = CodingProblem
        fields = [
            "title",
            "statement",
            "input_spec",
            "output_spec",
            "constraints",
            "starter_code",
            "languages",
            "marks",
            "order",
        ]
        widgets = {
            "statement": forms.Textarea(attrs={"rows": 6}),
            "input_spec": forms.Textarea(attrs={"rows": 3}),
            "output_spec": forms.Textarea(attrs={"rows": 3}),
            "constraints": forms.Textarea(attrs={"rows": 3}),
            "starter_code": forms.Textarea(attrs={"rows": 6, "class": "font-mono"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "w-full rounded border px-3 py-2")

    def clean_languages(self):
        raw_items = [item.strip().lower() for item in self.cleaned_data["languages"].split(",") if item.strip()]
        items = ["cpp" if item == "c++" else item for item in raw_items]
        if not items:
            raise ValidationError("Choose at least one language.")
        unsupported = [item for item in items if item not in SUPPORTED_LANGUAGES]
        if unsupported:
            raise ValidationError(
                f"Unsupported language(s): {', '.join(unsupported)}. "
                f"Supported: {', '.join(SUPPORTED_LANGUAGES)}."
            )
        return ", ".join(dict.fromkeys(items))


class CodingTestCaseForm(forms.ModelForm):
    class Meta:
        model = CodingTestCase
        fields = ["input_data", "expected_output", "is_hidden"]
        widgets = {
            "input_data": forms.Textarea(attrs={"rows": 2, "class": "font-mono"}),
            "expected_output": forms.Textarea(attrs={"rows": 2, "class": "font-mono"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_hidden"].widget.attrs.update({"class": "h-4 w-4"})


CodingTestCaseFormSet = inlineformset_factory(
    CodingProblem,
    CodingTestCase,
    form=CodingTestCaseForm,
    extra=5,
    can_delete=True,
)


class EmailLoginForm(AuthenticationForm):
    """
    Candidates sign in with the email address their login details were sent to. Staff may use their username.
    Addresses are case-insensitive, so they are lowercased before the lookup.
    """

    username = forms.CharField(
        label="Email",
        max_length=254,
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username", "class": "w-full rounded border px-3 py-2"}),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "class": "w-full rounded border px-3 py-2 pr-10"}),
    )

    error_messages = {
        "invalid_login": "Those details don't match. Check the email address, and copy the password exactly as it was sent.",
        "inactive": "This account is not active. Contact the administrator.",
    }

    def clean_username(self):
        value = self.cleaned_data["username"].strip()
        return value.lower() if "@" in value else value


class CandidateImportForm(forms.Form):
    """One candidate per line: email, optional full name, optional phone. Commas or tabs separate the parts."""

    candidates = forms.CharField(
        label="Candidates",
        widget=forms.Textarea(attrs={"rows": 12, "class": "w-full rounded border px-3 py-2 font-mono text-sm",
                                     "placeholder": "asha@example.com, Asha Rao, 9876543210"}),
        max_length=200_000,
        help_text="One candidate per line. You can paste from a spreadsheet.",
    )

    def parse(self):
        """Returns (rows, errors). Each row is a dict with email, full_name and phone."""
        rows, errors = [], []
        for number, raw in enumerate(self.cleaned_data["candidates"].splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            parts = [part.strip() for part in re.split(r"[,\t]", line)]
            email = parts[0].lower()
            try:
                forms.EmailField().clean(email)
            except forms.ValidationError:
                errors.append(f"Line {number}: “{parts[0]}” is not a valid email address.")
                continue
            rows.append({
                "email": email,
                "full_name": parts[1][:200] if len(parts) > 1 and parts[1] else "",
                "phone": parts[2][:20] if len(parts) > 2 else "",
            })
        return rows, errors


def validate_resume_file(uploaded):
    if not uploaded:
        return
    ext = os.path.splitext(uploaded.name)[1].lower()
    allowed = [".pdf", ".doc", ".docx"]
    if ext not in allowed:
        raise ValidationError(f"Resume must be a PDF or Word document ({', '.join(allowed)}).")
    if uploaded.size > 10 * 1024 * 1024:
        raise ValidationError("Resume file size must not exceed 10 MB.")


class ConfirmDetailsForm(forms.ModelForm):
    email = forms.EmailField(
        label="Email address",
        required=True,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "candidate@example.com"}),
    )
    resume = forms.FileField(
        label="Upload resume",
        required=False,
        validators=[validate_resume_file],
        widget=forms.FileInput(attrs={"accept": ".pdf,.doc,.docx"}),
        help_text="PDF or Word document (Max 10 MB).",
    )

    class Meta:
        model = Application
        fields = ["full_name", "phone", "resume"]
        labels = {
            "full_name": "Full legal name",
            "phone": "Mobile number",
            "resume": "Upload resume",
        }
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder": "Full legal name"}),
            "phone": forms.TextInput(attrs={"placeholder": "Mobile number with country code"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name != "resume":
                field.widget.attrs.setdefault("class", "w-full rounded border px-3 py-2")
            else:
                field.widget.attrs.setdefault(
                    "class",
                    "w-full text-sm text-slate-600 file:mr-4 file:py-2 file:px-4 file:rounded-md file:border-0 file:text-sm file:font-semibold file:bg-slate-100 file:text-slate-700 hover:file:bg-slate-200",
                )
        self.fields["full_name"].required = True
        self.fields["phone"].required = True
        if self.instance and self.instance.candidate_id:
            self.fields["email"].initial = self.instance.candidate.email or self.instance.candidate.username
        if self.instance and not self.instance.resume:
            self.fields["resume"].required = True


class RevokeForm(forms.Form):
    reason = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={"class": "w-full rounded border px-2 py-1 text-sm", "placeholder": "Reason (optional)"}),
    )


class SiteSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ["site_name", "site_logo", "favicon", "snapshot_retention_days"]
        labels = {
            "site_name": "Platform name",
            "site_logo": "Site logo",
            "favicon": "Favicon (.ico, .png, .svg)",
            "snapshot_retention_days": "Keep evidence images for (days)",
        }
        widgets = {
            "site_name": forms.TextInput(attrs={"placeholder": "e.g. Scrambler Assessments"}),
            "site_logo": forms.FileInput(attrs={"accept": "image/*"}),
            "favicon": forms.FileInput(attrs={"accept": ".ico,.png,.svg,.jpg,.jpeg"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site_name"].required = False
        for name, field in self.fields.items():
            if name in ("site_logo", "favicon"):
                field.widget.attrs.setdefault(
                    "class",
                    "w-full text-sm text-slate-600 file:mr-4 file:py-2 file:px-4 file:rounded-md file:border-0 file:text-sm file:font-semibold file:bg-slate-100 file:text-slate-700 hover:file:bg-slate-200",
                )
            elif name == "snapshot_retention_days":
                field.widget.attrs.setdefault("class", "w-32 rounded border px-3 py-2")
            else:
                field.widget.attrs.setdefault("class", "w-full rounded border px-3 py-2")


class DecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=[("approved", "Approve"), ("rejected", "Reject")])
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "class": "w-full rounded border px-3 py-2"}),
        help_text="Shown to the candidate when rejected.",
    )


class InterviewDecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=InterviewReview.Decision.choices)
    notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4, "class": "w-full rounded border px-3 py-2"}),
        max_length=5000,
    )
