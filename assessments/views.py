import hashlib
import mimetypes
import os

from django.conf import settings as dj_settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    CodingProblemForm,
    CodingTestCaseFormSet,
    ConfirmDetailsForm,
    EmailLoginForm,
    ExamForm,
    ExamSectionForm,
    MCQOptionEditFormSet,
    MCQOptionFormSet,
    MCQQuestionForm,
    SiteSettingsForm,
)
from .models import (
    Application,
    Attempt,
    AuditLog,
    CodingProblem,
    Exam,
    ExamSection,
    MCQQuestion,
    SiteSettings,
)
from .services import publish_checks

User = get_user_model()


def staff_required(view):
    """Administrators only: exam configuration, candidate access and settings."""
    return login_required(user_passes_test(lambda u: u.is_staff)(view))


def is_reviewer(user):
    return user.is_authenticated and (
        user.is_staff or user.groups.filter(name=dj_settings.REVIEWER_GROUP).exists()
    )


def reviewer_required(view):
    """Administrators and members of the Reviewers group: read attempts, record interview notes."""
    return login_required(user_passes_test(is_reviewer)(view))


def audit(actor, action, target, detail=""):
    AuditLog.objects.create(actor=actor, action=action, target=target, detail=detail)


LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES_PER_EMAIL = 5  # Stops guessing a password for one address.
LOGIN_MAX_FAILURES_PER_IP = 30  # Stops one machine trying many addresses.


def _login_key(kind, value):
    return f"login-failures:{kind}:{hashlib.sha256(value.encode()).hexdigest()}"


def _login_blocked(email, ip):
    return (
        cache.get(_login_key("email", email), 0) >= LOGIN_MAX_FAILURES_PER_EMAIL
        or cache.get(_login_key("ip", ip), 0) >= LOGIN_MAX_FAILURES_PER_IP
    )


def _count_login_failure(email, ip):
    for kind, value in (("email", email), ("ip", ip)):
        if value:
            key = _login_key(kind, value)
            cache.set(key, cache.get(key, 0) + 1, LOGIN_WINDOW_SECONDS)


class CandidateLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = EmailLoginForm
    redirect_authenticated_user = True

    def _submitted_email(self):
        return (self.request.POST.get("username") or "").strip().lower()

    def _client_ip(self):
        return self.request.META.get("REMOTE_ADDR", "")

    def post(self, request, *args, **kwargs):
        # A blocked address is refused before the password is checked, so a correct password
        # cannot be used to probe the block either.
        if _login_blocked(self._submitted_email(), self._client_ip()):
            form = self.get_form()
            form.add_error(
                None,
                "Too many failed sign-in attempts. Wait 15 minutes and try again, or ask the administrator to send your login details again.",
            )
            return self.render_to_response(self.get_context_data(form=form))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        _count_login_failure(self._submitted_email(), self._client_ip())
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(_login_key("email", self._submitted_email()))
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Candidate-facing views
# ---------------------------------------------------------------------------


def home(request):
    if not request.user.is_authenticated:
        return render(request, "assessments/welcome.html")
    if request.user.is_staff:
        return redirect("staff_exam_list")
    applications = (
        Application.objects.filter(candidate=request.user)
        .select_related("exam")
        .order_by("exam__opens_at")
    )
    attempts = {a.exam_id: a for a in Attempt.objects.filter(candidate=request.user)}
    rows = [
        {"application": application, "exam": application.exam, "attempt": attempts.get(application.exam_id),
         "state": candidate_state_for(application, attempts.get(application.exam_id))}
        for application in applications
        if application.exam.is_published
    ]
    return render(request, "assessments/home.html", {"rows": rows})


def candidate_state_for(application, attempt):
    """One plain-language state per candidate and test. Used by the home page and the test page."""
    exam = application.exam
    if application.status == Application.Status.REVOKED:
        return {"code": "revoked", "label": "Access removed", "tone": "red"}
    if attempt is not None:
        if attempt.is_final:
            return {"code": "ended", "label": "Submitted", "tone": "slate"}
        return {"code": "in_progress", "label": "In progress", "tone": "amber"}
    if application.details_confirmed_at is None:
        return {"code": "confirm", "label": "Confirm your details", "tone": "blue"}
    if timezone.now() >= exam.closes_at:
        return {"code": "closed", "label": "Closed", "tone": "slate"}
    if timezone.now() < exam.opens_at:
        return {"code": "waiting", "label": "Opens soon", "tone": "slate"}
    return {"code": "open", "label": "Open", "tone": "green"}


@login_required
def exam_detail(request, pk):
    if request.user.is_staff:
        return redirect("staff_exam_detail", pk=pk)
    exam = get_object_or_404(Exam, pk=pk, is_published=True)
    application = Application.objects.filter(candidate=request.user, exam=exam).first()
    if application is None:
        raise Http404("You are not on the list for this assessment.")
    attempt = Attempt.objects.filter(candidate=request.user, exam=exam).first()
    return render(
        request,
        "assessments/exam_detail.html",
        {
            "exam": exam,
            "application": application,
            "attempt": attempt,
            "state": candidate_state_for(application, attempt),
            "server_now": timezone.now(),
        },
    )


def serve_branding(request, kind):
    """Serve uploaded site logo or favicon publicly."""
    if kind not in ("site_logo", "favicon"):
        raise Http404("Unknown branding asset.")
    site = SiteSettings.load()
    field = getattr(site, kind, None)
    if not field or not field.name or not os.path.exists(field.path):
        raise Http404("Branding asset not uploaded.")
    content_type, _ = mimetypes.guess_type(field.name)
    return FileResponse(open(field.path, "rb"), content_type=content_type or "image/png")


@login_required
def confirm_details(request, pk):
    if request.user.is_staff:
        return redirect("staff_exam_detail", pk=pk)
    exam = get_object_or_404(Exam, pk=pk, is_published=True)
    application = get_object_or_404(Application, candidate=request.user, exam=exam)
    if application.status == Application.Status.REVOKED:
        messages.error(request, "Your access to this assessment has been removed.")
        return redirect("home")
    if request.method == "POST":
        form = ConfirmDetailsForm(request.POST, request.FILES, instance=application)
        if form.is_valid():
            application = form.save(commit=False)
            application.details_confirmed_at = timezone.now()
            application.save()
            new_email = form.cleaned_data.get("email", "").strip().lower()
            if new_email and application.candidate.email != new_email:
                application.candidate.email = new_email
                application.candidate.save(update_fields=["email"])
            messages.success(request, "Thank you. Your details and resume have been confirmed.")
            return redirect("exam_detail", pk=exam.pk)
    else:
        form = ConfirmDetailsForm(instance=application)
    return render(request, "assessments/confirm_details.html", {"exam": exam, "application": application, "form": form})


# ---------------------------------------------------------------------------
# Staff: tests
# ---------------------------------------------------------------------------


@reviewer_required
def staff_exam_list(request):
    exams = Exam.objects.all()
    return render(request, "assessments/staff/exam_list.html", {"exams": exams, "now": timezone.now()})


@staff_required
def staff_exam_create(request):
    if request.method == "POST":
        form = ExamForm(request.POST)
        if form.is_valid():
            exam = form.save(commit=False)
            exam.created_by = request.user
            exam.is_published = False
            exam.save()
            audit(request.user, "exam.created", f"exam:{exam.pk}", exam.title)
            messages.success(request, "Test created. Add sections, questions and coding problems, then publish.")
            return redirect("staff_exam_detail", pk=exam.pk)
    else:
        form = ExamForm()
    return render(request, "assessments/staff/exam_form.html", {"form": form, "exam": None})


@staff_required
def staff_exam_edit(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    if request.method == "POST":
        form = ExamForm(request.POST, instance=exam)
        if form.is_valid():
            form.save()
            audit(request.user, "exam.updated", f"exam:{exam.pk}", exam.title)
            messages.success(request, "Test settings saved.")
            return redirect("staff_exam_detail", pk=exam.pk)
    else:
        form = ExamForm(instance=exam)
    return render(request, "assessments/staff/exam_form.html", {"form": form, "exam": exam})


@staff_required
def staff_exam_detail(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    checks = publish_checks(exam)
    blocking_failures = [c for c in checks if c["blocking"] and not c["ok"]]
    context = {
        "exam": exam,
        "sections": exam.sections.all(),
        "mcq_questions": exam.mcq_questions.prefetch_related("options"),
        "coding_problems": exam.coding_problems.prefetch_related("test_cases"),
        "section_form": ExamSectionForm(prefix="section"),
        "checks": checks,
        "can_publish": True,  # User override enabled: warn instead of blocking creation/publish
        "blocking_count": len(blocking_failures),
        "candidate_count": exam.applications.count(),
    }
    return render(request, "assessments/staff/exam_detail.html", context)


@staff_required
@require_POST
def staff_exam_publish(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    if exam.is_published:
        messages.info(request, "This test is already published.")
        return redirect("staff_exam_detail", pk=exam.pk)
    failures = [c["text"] for c in publish_checks(exam) if c["blocking"] and not c["ok"]]
    is_override = request.POST.get("override") in ("true", "1")
    if failures and not is_override:
        messages.error(request, "The test cannot be published yet: " + " · ".join(failures))
        return redirect("staff_exam_detail", pk=exam.pk)
    exam.is_published = True
    exam.save(update_fields=["is_published"])
    audit(request.user, "exam.published", f"exam:{exam.pk}", exam.title)
    if failures:
        messages.warning(
            request,
            "Test published with warnings overridden: " + " · ".join(failures),
        )
    else:
        messages.success(request, "Test published. Candidates on the list can now see it.")
    return redirect("staff_exam_detail", pk=exam.pk)


@staff_required
@require_POST
def staff_exam_unpublish(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    exam.is_published = False
    exam.save(update_fields=["is_published"])
    audit(request.user, "exam.unpublished", f"exam:{exam.pk}", exam.title)
    messages.success(request, "Test unpublished. Candidates cannot see it until you publish it again.")
    return redirect("staff_exam_detail", pk=exam.pk)


@staff_required
@require_POST
def staff_section_add(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    form = ExamSectionForm(request.POST, prefix="section")
    if form.is_valid():
        section = form.save(commit=False)
        section.exam = exam
        if exam.sections.filter(order=section.order).exists():
            messages.error(request, f"Section order {section.order} is already used. Choose another order.")
        else:
            section.save()
            audit(request.user, "section.created", f"exam:{exam.pk}", section.title)
            messages.success(request, "Section added.")
    else:
        messages.error(request, "Section not saved: " + "; ".join(
            f"{field}: {', '.join(errors)}" for field, errors in form.errors.items()
        ))
    return redirect("staff_exam_detail", pk=exam.pk)


@staff_required
def staff_section_edit(request, pk):
    section = get_object_or_404(ExamSection, pk=pk)
    exam = section.exam
    if request.method == "POST":
        form = ExamSectionForm(request.POST, instance=section)
        if form.is_valid():
            target_order = form.cleaned_data["order"]
            if exam.sections.exclude(pk=section.pk).filter(order=target_order).exists():
                form.add_error("order", f"Section order {target_order} is already used. Choose another order.")
            else:
                form.save()
                audit(request.user, "section.updated", f"exam:{exam.pk}", section.title)
                messages.success(request, f"Section '{section.title}' updated.")
                return redirect("staff_exam_detail", pk=exam.pk)
    else:
        form = ExamSectionForm(instance=section)
    return render(
        request,
        "assessments/staff/section_form.html",
        {"exam": exam, "section": section, "form": form},
    )


@staff_required
@require_POST
def staff_section_delete(request, pk):
    section = get_object_or_404(ExamSection, pk=pk)
    exam_pk = section.exam.pk
    title = section.title
    section.delete()
    audit(request.user, "section.deleted", f"exam:{exam_pk}", title)
    messages.success(request, f"Section '{title}' deleted.")
    return redirect("staff_exam_detail", pk=exam_pk)


@staff_required
def staff_mcq_add(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    if request.method == "POST":
        question_form = MCQQuestionForm(request.POST)
        option_formset = MCQOptionFormSet(request.POST, prefix="options")
        if question_form.is_valid() and option_formset.is_valid():
            filled = [f for f in option_formset.forms if f.cleaned_data and f.cleaned_data.get("text") and not f.cleaned_data.get("DELETE")]
            correct = [f for f in filled if f.cleaned_data.get("is_correct")]
            if len(filled) < 2:
                option_formset._non_form_errors = option_formset.error_class(["Add at least two answer options."])
            elif len(correct) != 1:
                option_formset._non_form_errors = option_formset.error_class(["Mark exactly one option as correct."])
            else:
                with transaction.atomic():
                    question = question_form.save(commit=False)
                    question.exam = exam
                    question.save()
                    for index, form in enumerate(filled, start=1):
                        option = form.save(commit=False)
                        option.question = question
                        option.order = index
                        option.save()
                audit(request.user, "mcq.created", f"exam:{exam.pk}", question.text[:100])
                messages.success(request, "Question added.")
                return redirect("staff_exam_detail", pk=exam.pk)
    else:
        question_form = MCQQuestionForm(initial={"order": exam.mcq_questions.count() + 1})
        option_formset = MCQOptionFormSet(prefix="options")
    return render(
        request,
        "assessments/staff/mcq_form.html",
        {"exam": exam, "question": None, "question_form": question_form, "option_formset": option_formset},
    )


@staff_required
def staff_mcq_edit(request, pk):
    question = get_object_or_404(MCQQuestion, pk=pk)
    exam = question.exam
    if request.method == "POST":
        question_form = MCQQuestionForm(request.POST, instance=question)
        option_formset = MCQOptionEditFormSet(request.POST, instance=question, prefix="options")
        if question_form.is_valid() and option_formset.is_valid():
            filled = [f for f in option_formset.forms if f.cleaned_data and f.cleaned_data.get("text") and not f.cleaned_data.get("DELETE")]
            correct = [f for f in filled if f.cleaned_data.get("is_correct")]
            if len(filled) < 2:
                option_formset._non_form_errors = option_formset.error_class(["Add at least two answer options."])
            elif len(correct) != 1:
                option_formset._non_form_errors = option_formset.error_class(["Mark exactly one option as correct."])
            else:
                with transaction.atomic():
                    question = question_form.save()
                    for form in option_formset.deleted_forms:
                        if form.instance.pk:
                            form.instance.delete()
                    for index, form in enumerate(filled, start=1):
                        option = form.save(commit=False)
                        option.question = question
                        option.order = index
                        option.save()
                audit(request.user, "mcq.updated", f"exam:{exam.pk}", question.text[:100])
                messages.success(request, "Multiple choice question updated.")
                return redirect("staff_exam_detail", pk=exam.pk)
    else:
        question_form = MCQQuestionForm(instance=question)
        option_formset = MCQOptionEditFormSet(instance=question, prefix="options")
    return render(
        request,
        "assessments/staff/mcq_form.html",
        {"exam": exam, "question": question, "question_form": question_form, "option_formset": option_formset},
    )


@staff_required
@require_POST
def staff_mcq_delete(request, pk):
    question = get_object_or_404(MCQQuestion, pk=pk)
    exam_pk = question.exam.pk
    question.delete()
    audit(request.user, "mcq.deleted", f"exam:{exam_pk}", f"Question {pk}")
    messages.success(request, "Multiple choice question deleted.")
    return redirect("staff_exam_detail", pk=exam_pk)


@staff_required
def staff_coding_add(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    if request.method == "POST":
        problem_form = CodingProblemForm(request.POST)
        case_formset = CodingTestCaseFormSet(request.POST, prefix="cases")
        if problem_form.is_valid() and case_formset.is_valid():
            cases = [f for f in case_formset.forms if f.cleaned_data and not f.cleaned_data.get("DELETE")
                     and f.cleaned_data.get("expected_output")]
            if not cases:
                case_formset._non_form_errors = case_formset.error_class(
                    ["Add at least one test case with an expected output."]
                )
            else:
                with transaction.atomic():
                    problem = problem_form.save(commit=False)
                    problem.exam = exam
                    problem.save()
                    for index, form in enumerate(cases, start=1):
                        case = form.save(commit=False)
                        case.problem = problem
                        case.order = index
                        case.save()
                audit(request.user, "coding.created", f"exam:{exam.pk}", problem.title)
                messages.success(request, "Coding problem added.")
                return redirect("staff_exam_detail", pk=exam.pk)
    else:
        problem_form = CodingProblemForm(initial={"order": exam.coding_problems.count() + 1})
        case_formset = CodingTestCaseFormSet(prefix="cases")
    return render(
        request,
        "assessments/staff/coding_form.html",
        {"exam": exam, "problem": None, "problem_form": problem_form, "case_formset": case_formset},
    )


@staff_required
def staff_coding_edit(request, pk):
    problem = get_object_or_404(CodingProblem, pk=pk)
    exam = problem.exam
    if request.method == "POST":
        problem_form = CodingProblemForm(request.POST, instance=problem)
        case_formset = CodingTestCaseFormSet(request.POST, instance=problem, prefix="cases")
        if problem_form.is_valid() and case_formset.is_valid():
            cases = [
                f for f in case_formset.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE") and f.cleaned_data.get("expected_output")
            ]
            if not cases:
                case_formset._non_form_errors = case_formset.error_class(
                    ["Add at least one test case with an expected output."]
                )
            else:
                with transaction.atomic():
                    problem = problem_form.save()
                    for form in case_formset.deleted_forms:
                        if form.instance.pk:
                            form.instance.delete()
                    for index, form in enumerate(cases, start=1):
                        case = form.save(commit=False)
                        case.problem = problem
                        case.order = index
                        case.save()
                audit(request.user, "coding.updated", f"exam:{exam.pk}", problem.title)
                messages.success(request, f"Coding problem '{problem.title}' updated.")
                return redirect("staff_exam_detail", pk=exam.pk)
    else:
        problem_form = CodingProblemForm(instance=problem)
        case_formset = CodingTestCaseFormSet(instance=problem, prefix="cases")
    return render(
        request,
        "assessments/staff/coding_form.html",
        {"exam": exam, "problem": problem, "problem_form": problem_form, "case_formset": case_formset},
    )


@staff_required
@require_POST
def staff_coding_delete(request, pk):
    problem = get_object_or_404(CodingProblem, pk=pk)
    exam_pk = problem.exam.pk
    title = problem.title
    problem.delete()
    audit(request.user, "coding.deleted", f"exam:{exam_pk}", title)
    messages.success(request, f"Coding problem '{title}' deleted.")
    return redirect("staff_exam_detail", pk=exam_pk)


# ---------------------------------------------------------------------------
# Staff: settings
# ---------------------------------------------------------------------------


@staff_required
def staff_settings(request):
    site = SiteSettings.load()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "test_email":
            recipient = (request.POST.get("test_recipient") or request.user.email or "").strip()
            if not recipient:
                messages.error(request, "Please enter a valid recipient email address.")
            else:
                from .notifications import deliver_email
                subject = f"[{site.site_name or 'Assessments'}] Test Email"
                body = (
                    f"Hello,\n\n"
                    f"This is a test email sent from the {site.site_name or 'Assessments'} settings panel by {request.user.username}.\n\n"
                    f"Active Server Settings:\n"
                    f"- Backend: {dj_settings.EMAIL_BACKEND}\n"
                    f"- Host: {dj_settings.EMAIL_HOST}:{dj_settings.EMAIL_PORT}\n"
                    f"- SSL: {dj_settings.EMAIL_USE_SSL} | TLS: {dj_settings.EMAIL_USE_TLS}\n"
                    f"- Sender: {dj_settings.DEFAULT_FROM_EMAIL}\n"
                )
                html_body = (
                    f"<div style='font-family:sans-serif; max-width:550px; padding:20px; border:1px solid #e2e8f0; border-radius:8px;'>"
                    f"<h2 style='color:#0f172a; margin-top:0;'>{site.site_name or 'Assessments'} &mdash; Test Email</h2>"
                    f"<p style='color:#334155;'>Test email triggered from the Settings panel by <strong>{request.user.username}</strong>.</p>"
                    f"<div style='background:#f8fafc; padding:12px; border-radius:6px; font-size:13px; font-family:monospace;'>"
                    f"<b>Host:</b> {dj_settings.EMAIL_HOST}:{dj_settings.EMAIL_PORT}<br>"
                    f"<b>SSL:</b> {dj_settings.EMAIL_USE_SSL} &nbsp;|&nbsp; <b>TLS:</b> {dj_settings.EMAIL_USE_TLS}<br>"
                    f"<b>From:</b> {dj_settings.DEFAULT_FROM_EMAIL}<br>"
                    f"</div>"
                    f"</div>"
                )
                delivered = deliver_email(subject, body, recipient, html_body=html_body)
                if delivered:
                    audit(request.user, "email.test.success", "email", f"to={recipient}")
                    messages.success(request, f"Test email successfully delivered to {recipient} via {dj_settings.EMAIL_HOST}:{dj_settings.EMAIL_PORT}!")
                else:
                    audit(request.user, "email.test.failed", "email", f"to={recipient}")
                    messages.error(request, f"Failed to deliver test email to {recipient}. Check server logs and mail configuration in .env.")
            return redirect("staff_settings")

        form = SiteSettingsForm(request.POST, request.FILES, instance=site)
        if form.is_valid():
            form.save()
            audit(request.user, "settings.updated", "site", f"site_name={site.site_name}, retention={site.snapshot_retention_days} days")
            messages.success(request, "Settings saved.")
            return redirect("staff_settings")
    else:
        form = SiteSettingsForm(instance=site)

    email_status = {
        "backend": dj_settings.EMAIL_BACKEND,
        "host": dj_settings.EMAIL_HOST,
        "port": dj_settings.EMAIL_PORT,
        "use_ssl": dj_settings.EMAIL_USE_SSL,
        "use_tls": dj_settings.EMAIL_USE_TLS,
        "user": dj_settings.EMAIL_HOST_USER,
        "from_email": dj_settings.DEFAULT_FROM_EMAIL,
        "is_configured": bool(dj_settings.EMAIL_HOST_PASSWORD and dj_settings.EMAIL_HOST_PASSWORD != "change-me"),
    }
    return render(request, "assessments/staff/settings.html", {"form": form, "email_status": email_status})


def csrf_failure_view(request, reason=""):
    """User-friendly response when a candidate's session or form security token expires."""
    return render(request, "403_csrf.html", {"reason": reason}, status=403)


def favicon_view(request):
    """Serve the configured branding favicon or a 204 No Content response to stop 404 warnings."""
    try:
        site = SiteSettings.load()
        if site.favicon and site.favicon.name and os.path.exists(site.favicon.path):
            content_type, _ = mimetypes.guess_type(site.favicon.name)
            return FileResponse(open(site.favicon.path, "rb"), content_type=content_type or "image/x-icon")
    except Exception:
        pass
    return HttpResponse(status=204)


def robots_txt_view(request):
    """Serve a basic robots.txt to prevent crawler 404 warnings."""
    content = "User-agent: *\nDisallow: /admin/\nDisallow: /staff/\nDisallow: /api/\n"
    return HttpResponse(content, content_type="text/plain")

