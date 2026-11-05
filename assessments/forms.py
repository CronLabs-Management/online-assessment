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
            "interview_rounds_count",
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
            "interview_rounds_count": "Total Interview Rounds (Staff Only)",
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
        fields = ["title", "order", "duration_minutes", "question_count"]
        labels = {
            "title": "MCQ Section Title",
            "order": "Section Order",
            "duration_minutes": "Duration (minutes)",
            "question_count": "Questions to assign (0 = all in pool)",
        }
        help_texts = {
            "question_count": "How many questions from this section each candidate receives. Set 0 for all questions.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "w-full rounded border px-3 py-2")

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.kind = ExamSection.Kind.MCQ
        if commit:
            instance.save()
        return instance


class MCQQuestionForm(forms.ModelForm):
    section = forms.ModelChoiceField(
        queryset=ExamSection.objects.none(),
        required=False,
        label="MCQ Section",
        help_text="Choose which section this question belongs to. Defaults to 'Default Section'.",
    )
    new_section_title = forms.CharField(
        required=False,
        label="Or create a new section",
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Verbal Reasoning"}),
        help_text="Leave blank to use the section selected above.",
    )

    class Meta:
        model = MCQQuestion
        fields = ["section", "new_section_title", "text", "marks", "order"]
        widgets = {"text": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, exam=None, initial_section_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.exam = exam
        if exam is None and self.instance and self.instance.exam_id:
            self.exam = self.instance.exam
        if self.exam:
            default_sec = self.exam.get_default_section()
            self.fields["section"].queryset = self.exam.sections.filter(kind=ExamSection.Kind.MCQ).order_by("order", "id")
            if initial_section_id:
                try:
                    matched = self.exam.sections.filter(pk=int(initial_section_id), kind=ExamSection.Kind.MCQ).first()
                    if matched:
                        self.initial["section"] = matched
                except (ValueError, TypeError):
                    pass
            if not self.initial.get("section") and not (self.instance and self.instance.section_id):
                self.initial["section"] = default_sec
        for name, field in self.fields.items():
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
        widget=forms.TextInput(attrs={
            "autofocus": True,
            "autocomplete": "username",
            "class": "login-field-input",
            "placeholder": "name@example.com",
        }),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={
            "autocomplete": "current-password",
            "class": "login-field-input-pwd",
            "placeholder": "Enter your password",
        }),
    )

    error_messages = {
        "invalid_login": "Those details don't match. Check the email address, and copy the password exactly as it was sent.",
        "inactive": "This account is not active. Contact the administrator.",
    }

    def clean_username(self):
        value = self.cleaned_data["username"].strip()
        user = User.objects.filter(email__iexact=value).first() or User.objects.filter(username__iexact=value).first()
        if user:
            return user.get_username()
        return value.lower() if "@" in value else value


class CandidateImportForm(forms.Form):
    """Support adding candidates via structured input columns or bulk spreadsheet pasting."""

    single_full_name = forms.CharField(
        label="Full Name",
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Rahul Sharma", "class": "w-full rounded border px-3 py-2"}),
    )
    single_email = forms.EmailField(
        label="Email Address",
        max_length=254,
        required=False,
        widget=forms.EmailInput(attrs={"placeholder": "candidate@example.com", "class": "w-full rounded border px-3 py-2"}),
    )
    single_phone = forms.CharField(
        label="Mobile Number",
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "+91 9876543210", "class": "w-full rounded border px-3 py-2"}),
    )
    candidates = forms.CharField(
        label="Or Paste Bulk Mailing List",
        widget=forms.Textarea(attrs={"rows": 8, "class": "w-full rounded border px-3 py-2 font-mono text-sm",
                                     "placeholder": "asha@example.com, Asha Rao, 9876543210\nrahul@example.com, Rahul K, 9812345678"}),
        max_length=200_000,
        required=False,
        help_text="One candidate per line: email, full name, phone. Commas or tabs work.",
    )

    def clean(self):
        cleaned = super().clean()
        single_email = (cleaned.get("single_email") or "").strip()
        bulk = (cleaned.get("candidates") or "").strip()
        if not single_email and not bulk:
            raise ValidationError("Please provide candidate details above or paste a candidate list.")
        return cleaned

    def parse(self):
        """Returns (rows, errors). Each row is a dict with email, full_name and phone."""
        rows, errors = [], []
        seen = set()
        single_email = (self.cleaned_data.get("single_email") or "").strip().lower()
        if single_email:
            seen.add(single_email)
            rows.append({
                "email": single_email,
                "full_name": (self.cleaned_data.get("single_full_name") or "").strip(),
                "phone": (self.cleaned_data.get("single_phone") or "").strip(),
            })

        for number, raw in enumerate((self.cleaned_data.get("candidates") or "").splitlines(), start=1):
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
            if email in seen:
                errors.append(f"Line {number}: “{email}” was duplicated in the submitted list and only processed once.")
                continue
            seen.add(email)
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

    pos = uploaded.tell() if hasattr(uploaded, "tell") else 0
    header = uploaded.read(8)
    if hasattr(uploaded, "seek"):
        uploaded.seek(pos)

    if ext == ".pdf":
        if not header.startswith(b"%PDF-"):
            raise ValidationError("Invalid PDF file: file signature does not match PDF format.")
    elif ext == ".docx":
        if not header.startswith(b"PK\x03\x04"):
            raise ValidationError("Invalid Word document: file signature does not match DOCX format.")
    elif ext == ".doc":
        if not header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise ValidationError("Invalid Word document: file signature does not match legacy DOC format.")


class CandidateEditForm(forms.ModelForm):
    email = forms.EmailField(
        label="Login Email ID",
        required=True,
        widget=forms.EmailInput(attrs={"class": "w-full rounded border px-3 py-2"}),
    )
    resume = forms.FileField(
        label="Candidate Resume (.pdf, .doc, .docx)",
        required=False,
        validators=[validate_resume_file],
        widget=forms.FileInput(
            attrs={
                "accept": ".pdf,.doc,.docx",
                "class": "w-full text-sm text-slate-600 file:mr-4 file:py-2 file:px-4 file:rounded-md file:border-0 file:text-sm file:font-semibold file:bg-slate-100 file:text-slate-700 hover:file:bg-slate-200",
            }
        ),
        help_text="Upload a PDF or Word document to update this candidate's resume (Max 10 MB).",
    )

    class Meta:
        model = Application
        fields = ["full_name", "phone", "plain_password", "resume"]
        labels = {
            "full_name": "Full Legal Name",
            "phone": "Mobile Number",
            "plain_password": "Candidate Login Password (Always Visible to Admin)",
            "resume": "Candidate Resume",
        }
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "w-full rounded border px-3 py-2"}),
            "phone": forms.TextInput(attrs={"class": "w-full rounded border px-3 py-2"}),
            "plain_password": forms.TextInput(attrs={"class": "w-full rounded border px-3 py-2 font-mono font-bold"}),
        }
        help_texts = {
            "plain_password": "Password used by the candidate to log in. Plain text and always visible to admin.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.candidate_id:
            self.fields["email"].initial = self.instance.candidate.email or self.instance.candidate.username



class ConfirmDetailsForm(forms.ModelForm):
    email = forms.EmailField(
        label="Email address",
        required=False,
        widget=forms.EmailInput(attrs={
            "autocomplete": "email",
            "readonly": "readonly",
            "class": "w-full rounded-xl border border-slate-300 bg-slate-100 text-slate-500 font-medium px-3 py-2 cursor-not-allowed",
            "style": "width: 100%; border-radius: 12px; border: 1.5px solid #CBD5E1; background-color: #F1F5F9; color: #64748B; font-weight: 600; padding: 10px 14px; font-size: 14px; box-sizing: border-box; cursor: not-allowed;",
        }),
        help_text="Your account email address is locked and cannot be changed.",
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
            "full_name": forms.TextInput(attrs={
                "placeholder": "Full legal name",
                "class": "w-full rounded-xl border border-slate-300 px-3 py-2 text-slate-900 font-medium focus:border-blue-600 focus:ring-1 focus:ring-blue-600",
                "style": "width: 100%; border-radius: 12px; border: 1.5px solid #CBD5E1; color: #0F172A; font-weight: 600; padding: 10px 14px; font-size: 14px; box-sizing: border-box;",
            }),
            "phone": forms.TextInput(attrs={
                "placeholder": "Mobile number with country code",
                "class": "w-full rounded-xl border border-slate-300 px-3 py-2 text-slate-900 font-medium focus:border-blue-600 focus:ring-1 focus:ring-blue-600",
                "style": "width: 100%; border-radius: 12px; border: 1.5px solid #CBD5E1; color: #0F172A; font-weight: 600; padding: 10px 14px; font-size: 14px; box-sizing: border-box;",
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "resume":
                field.widget.attrs.setdefault(
                    "class",
                    "w-full text-sm text-slate-600 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-sm file:font-bold file:bg-blue-50 file:text-[#2447F5] hover:file:bg-blue-100 cursor-pointer",
                )
            elif name != "email":
                field.widget.attrs.setdefault("class", "w-full rounded-xl border border-slate-300 px-3 py-2 text-slate-900 font-medium focus:border-blue-600 focus:ring-1 focus:ring-blue-600")

        self.fields["full_name"].required = True
        self.fields["phone"].required = True

        # Pre-fill initial values if already on file
        if self.instance and self.instance.candidate_id:
            cand = self.instance.candidate
            if not self.initial.get("full_name"):
                self.initial["full_name"] = self.instance.full_name or cand.get_full_name() or cand.username
            if not self.initial.get("phone"):
                self.initial["phone"] = self.instance.phone or ""
            # Locked email display
            account_email = cand.email or cand.username
            self.fields["email"].initial = account_email
            self.initial["email"] = account_email

        # If candidate has no resume on file yet, resume is required
        if self.instance and not self.instance.resume:
            self.fields["resume"].required = True

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email and self.instance and self.instance.candidate_id:
            return self.instance.candidate.email or self.instance.candidate.username
        return email


class RevokeForm(forms.Form):
    reason = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={"class": "w-full rounded border px-2 py-1 text-sm", "placeholder": "Reason (optional)"}),
    )


class SiteSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = [
            "site_name",
            "site_logo",
            "favicon",
            "footer_copyright",
            "footer_text",
            "snapshot_retention_days",
            "gemini_api_key",
            "gemini_model",
        ]
        labels = {
            "site_name": "Platform name",
            "site_logo": "Site logo",
            "favicon": "Favicon (.ico, .png, .svg)",
            "footer_copyright": "Footer Copyright Notice",
            "footer_text": "Footer Description / Tagline",
            "snapshot_retention_days": "Keep evidence images for (days)",
            "gemini_api_key": "Google Gemini API Key",
            "gemini_model": "Gemini Model",
        }
        widgets = {
            "site_name": forms.TextInput(attrs={"placeholder": "e.g. Assessments"}),
            "site_logo": forms.FileInput(attrs={"accept": "image/*"}),
            "favicon": forms.FileInput(attrs={"accept": ".ico,.png,.svg,.jpg,.jpeg"}),
            "footer_copyright": forms.TextInput(attrs={"placeholder": "e.g. © 2026 Assessments. All rights reserved."}),
            "footer_text": forms.TextInput(attrs={"placeholder": "e.g. Secure Online Assessment Platform"}),
            "gemini_api_key": forms.PasswordInput(render_value=True, attrs={"placeholder": "AIzaSy...", "autocomplete": "off"}),
            "gemini_model": forms.Select(choices=[
                ("gemini-1.5-flash", "Gemini 1.5 Flash (Fast & Recommended)"),
                ("gemini-1.5-pro", "Gemini 1.5 Pro (Deep Reasoning)"),
                ("gemini-2.0-flash-exp", "Gemini 2.0 Flash Experimental"),
            ]),
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
    round_number = forms.IntegerField(min_value=1, initial=1, required=False, widget=forms.HiddenInput())
    interviewer_name = forms.CharField(
        max_length=150,
        required=False,
        label="Interviewer Signature Name",
        widget=forms.TextInput(attrs={
            "placeholder": "e.g. Alex Morgan (Technical Interviewer)",
            "class": "w-full rounded-xl border border-slate-300 p-3 text-sm text-slate-900 focus:border-[#0E2378] focus:ring-1 focus:ring-[#0E2378]",
        }),
        help_text="Your name serving as the official signature for this round.",
    )
    candidate_summary = forms.CharField(
        required=False,
        label="Candidate Short Info / Overview",
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "Short overview about the candidate (e.g. 4+ yrs experience in Python/Django, good communication, strong system design concepts, hands-on Docker expertise)...",
            "class": "w-full rounded-xl border border-slate-300 p-3 text-sm text-slate-900 focus:border-[#0E2378] focus:ring-1 focus:ring-[#0E2378]",
        }),
        help_text="Provide a short summary about the candidate's background, communication, and overall demeanor.",
    )
    decision = forms.ChoiceField(
        choices=InterviewReview.Decision.choices,
        initial=InterviewReview.Decision.RECOMMENDED,
        widget=forms.RadioSelect,
    )
    selected_for_next_round = forms.BooleanField(
        required=False,
        initial=True,
        label="Selected for Next Round / Final Selection",
        widget=forms.CheckboxInput(attrs={"class": "h-4 w-4 rounded border-slate-300 text-[#0E2378] focus:ring-[#0E2378]"}),
    )
    interview_score = forms.DecimalField(
        required=False,
        max_digits=6,
        decimal_places=2,
        widget=forms.HiddenInput(),
    )
    max_interview_score = forms.DecimalField(
        required=False,
        max_digits=6,
        decimal_places=2,
        widget=forms.HiddenInput(),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "Detailed evaluation notes, technical strengths, gap areas, and problem-solving quality...",
            "class": "w-full rounded-xl border border-slate-300 p-3 text-sm text-slate-900 focus:border-[#0E2378] focus:ring-1 focus:ring-[#0E2378]",
        }),
        max_length=5000,
    )
    questions_data = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )
