import hashlib
import mimetypes
import os
import secrets
from pathlib import Path

from django.conf import settings as dj_settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
            cache.add(key, 0, LOGIN_WINDOW_SECONDS)
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, LOGIN_WINDOW_SECONDS)


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

    def get_success_url(self):
        redirect_to = self.get_redirect_url()
        if redirect_to:
            return redirect_to
        if self.request.user.is_staff or is_reviewer(self.request.user):
            return reverse("staff_exam_list")
        return reverse("home")

    def form_valid(self, form):
        cache.delete(_login_key("email", self._submitted_email()))
        if self._client_ip():
            cache.delete(_login_key("ip", self._client_ip()))
        response = super().form_valid(form)
        self.request.session.cycle_key()
        return response


# ---------------------------------------------------------------------------
# Candidate-facing views
# ---------------------------------------------------------------------------


def logout_view(request):
    from django.contrib.auth import logout
    logout(request)
    return redirect("login")


def home(request):
    if not request.user.is_authenticated:
        return redirect("login")
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
    completed_attempts = (
        Attempt.objects.filter(candidate=request.user, status__in=Attempt.FINAL_STATUSES)
        .select_related("exam")
        .order_by("-ended_at")
    )
    return render(request, "assessments/home.html", {"rows": rows, "completed_attempts": completed_attempts})


def candidate_state_for(application, attempt):
    """One plain-language state per candidate and test. Used by the home page and the test page."""
    exam = application.exam
    if application.status == Application.Status.REVOKED:
        return {"code": "revoked", "label": "Access removed", "tone": "red"}
    if attempt is not None:
        if attempt.is_final:
            return {"code": "ended", "label": "Submitted", "tone": "slate"}
        return {"code": "in_progress", "label": "In progress", "tone": "blue"}
    if timezone.now() >= exam.closes_at:
        return {"code": "closed", "label": "Closed", "tone": "slate"}
    if application.details_confirmed_at is None or not application.resume:
        return {"code": "confirm", "label": "Verify details & resume", "tone": "blue"}
    if timezone.now() < exam.opens_at:
        return {"code": "waiting", "label": "Opens soon", "tone": "slate"}
    return {"code": "open", "label": "Ready to start", "tone": "green"}


@login_required
def exam_detail(request, pk):
    if request.user.is_staff:
        return redirect("staff_exam_detail", pk=pk)
    exam = get_object_or_404(Exam, pk=pk, is_published=True)
    application = Application.objects.filter(candidate=request.user, exam=exam).first()
    if application is None:
        raise Http404("You are not on the list for this assessment.")
    if application.status == Application.Status.REVOKED:
        messages.error(request, "Your access to this assessment has been removed.")
        return redirect("home")
    attempt = Attempt.objects.filter(candidate=request.user, exam=exam).first()

    # Guard: Candidate MUST verify details and upload resume before proceeding to system check & exam
    if attempt is None and application.details_confirmed_at is None:
        messages.info(request, "Please verify your candidate details and upload your resume before proceeding to the system check.")
        return redirect("confirm_details", pk=exam.pk)

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


@login_required
def candidate_resume(request, pk):
    """Allow an authenticated candidate to view or download their uploaded resume."""
    exam = get_object_or_404(Exam, pk=pk, is_published=True)
    application = get_object_or_404(Application, candidate=request.user, exam=exam)
    if not application.resume or not application.resume.name:
        raise Http404("No resume uploaded.")
    try:
        file_path = application.resume.path
        if not os.path.exists(file_path):
            raise Http404("Resume file not found.")
        content_type, _ = mimetypes.guess_type(file_path)
        response = FileResponse(open(file_path, "rb"), content_type=content_type or "application/pdf")
        if request.GET.get("download") == "1":
            response["Content-Disposition"] = f'attachment; filename="{os.path.basename(file_path)}"'
        else:
            response["Content-Disposition"] = f'inline; filename="{os.path.basename(file_path)}"'
        return response
    except Exception:
        raise Http404("Unable to read resume.")


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
    if timezone.now() >= exam.closes_at:
        messages.error(request, "This assessment window has closed.")
        return redirect("home")
    attempt = Attempt.objects.filter(candidate=request.user, exam=exam).first()
    if attempt and attempt.is_final:
        messages.info(request, "You have already completed and submitted this assessment.")
        return redirect("attempt_result", pk=attempt.pk)

    if request.method == "POST":
        form = ConfirmDetailsForm(request.POST, request.FILES, instance=application)
        if form.is_valid():
            if not application.resume and not request.FILES.get("resume"):
                form.add_error("resume", "Please upload your resume before proceeding.")
                return render(request, "assessments/confirm_details.html", {"exam": exam, "application": application, "form": form})
            application = form.save(commit=False)
            application.details_confirmed_at = timezone.now()
            new_email = form.cleaned_data.get("email", "").strip().lower()
            if new_email and application.candidate.email != new_email:
                raw_token = secrets.token_urlsafe(32)
                application.pending_email = new_email
                application.email_verification_token = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
                application.email_verification_sent_at = timezone.now()
                verify_url = request.build_absolute_uri(reverse("verify_email", args=[raw_token]))
                try:
                    send_mail(
                        subject=f"Verify your email address for {exam.title}",
                        message=f"Please confirm your new email address by visiting:\n{verify_url}\n\nThis verification link expires in 24 hours.",
                        from_email=dj_settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[new_email],
                        fail_silently=True,
                    )
                except Exception:
                    pass
                messages.info(request, f"A verification email was sent to {new_email}. Your login email will update once verified.")
            application.save()
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
    default_sec = exam.get_default_section()
    mcq_sections = list(exam.sections.filter(kind=ExamSection.Kind.MCQ).order_by("order", "id"))
    questions = list(exam.mcq_questions.select_related("section").prefetch_related("options").order_by("order", "id"))
    for sec in mcq_sections:
        sec.assigned_questions = [q for q in questions if q.section_id == sec.pk or (not q.section_id and sec.is_default)]

    context = {
        "exam": exam,
        "default_section": default_sec,
        "mcq_sections": mcq_sections,
        "sections": exam.sections.all(),
        "mcq_questions": questions,
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
    exam = section.exam
    if section.is_default:
        messages.error(request, "The default section cannot be deleted.")
        return redirect("staff_exam_detail", pk=exam_pk)
    title = section.title
    default_sec = exam.get_default_section()
    reassigned = exam.mcq_questions.filter(section=section).update(section=default_sec)
    section.delete()
    audit(request.user, "section.deleted", f"exam:{exam_pk}", f"{title} (reassigned {reassigned} to default)")
    messages.success(request, f"Section '{title}' deleted. {reassigned} question(s) moved to '{default_sec.title}'.")
    return redirect("staff_exam_detail", pk=exam_pk)


@staff_required
def staff_mcq_add(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    initial_sec = request.GET.get("section")
    if request.method == "POST":
        question_form = MCQQuestionForm(request.POST, exam=exam, initial_section_id=initial_sec)
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
                    new_sec_title = (question_form.cleaned_data.get("new_section_title") or "").strip()
                    if new_sec_title:
                        from django.db.models import Max
                        max_order = exam.sections.aggregate(m=Max("order"))["m"] or 0
                        sec = ExamSection.objects.create(
                            exam=exam,
                            title=new_sec_title,
                            kind=ExamSection.Kind.MCQ,
                            order=max_order + 1,
                            duration_minutes=30,
                            question_count=0,
                        )
                        question.section = sec
                    elif not question.section_id:
                        question.section = exam.get_default_section()
                    question.save()
                    for index, form in enumerate(filled, start=1):
                        option = form.save(commit=False)
                        option.question = question
                        option.order = index
                        option.save()
                audit(request.user, "mcq.created", f"exam:{exam.pk}", question.text[:100])
                messages.success(request, f"Question added to section '{question.section.title}'.")
                return redirect(f"{reverse('staff_exam_detail', kwargs={'pk': exam.pk})}#section-{question.section_id}")
    else:
        question_form = MCQQuestionForm(initial={"order": exam.mcq_questions.count() + 1}, exam=exam, initial_section_id=initial_sec)
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
        question_form = MCQQuestionForm(request.POST, instance=question, exam=exam)
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
                    question = question_form.save(commit=False)
                    new_sec_title = (question_form.cleaned_data.get("new_section_title") or "").strip()
                    if new_sec_title:
                        from django.db.models import Max
                        max_order = exam.sections.aggregate(m=Max("order"))["m"] or 0
                        sec = ExamSection.objects.create(
                            exam=exam,
                            title=new_sec_title,
                            kind=ExamSection.Kind.MCQ,
                            order=max_order + 1,
                            duration_minutes=30,
                            question_count=0,
                        )
                        question.section = sec
                    elif not question.section_id:
                        question.section = exam.get_default_section()
                    question.save()
                    for form in option_formset.deleted_forms:
                        if form.instance.pk:
                            form.instance.delete()
                    for index, form in enumerate(filled, start=1):
                        option = form.save(commit=False)
                        option.question = question
                        option.order = index
                        option.save()
                audit(request.user, "mcq.updated", f"exam:{exam.pk}", question.text[:100])
                messages.success(request, f"Multiple choice question updated in '{question.section.title}'.")
                return redirect(f"{reverse('staff_exam_detail', kwargs={'pk': exam.pk})}#section-{question.section_id}")
    else:
        question_form = MCQQuestionForm(instance=question, exam=exam)
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


@login_required
def verify_email(request, token):
    """Confirm a candidate email change after verifying token possession."""
    if not token or len(token) < 16:
        messages.error(request, "Invalid email verification link.")
        return redirect("home")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    cutoff = timezone.now() - timezone.timedelta(hours=24)
    application = Application.objects.filter(
        email_verification_token=token_hash,
        email_verification_sent_at__gte=cutoff,
    ).select_related("candidate").first()
    if application is None:
        messages.error(request, "This verification link is invalid or has expired.")
        return redirect("home")

    if request.user != application.candidate and not request.user.is_staff:
        messages.error(request, "You are not authorized to verify this email address.")
        return redirect("home")

    with transaction.atomic():
        old_email = application.candidate.email
        new_email = application.pending_email
        if new_email:
            application.candidate.email = new_email
            # Sync username if candidate username was old email
            if application.candidate.username == old_email or not application.candidate.username:
                application.candidate.username = new_email
            application.candidate.save(update_fields=["email", "username"])
            audit(request.user if request.user.is_authenticated else None, "candidate.email_verified", f"candidate:{application.candidate_id}", f"from={old_email} to={new_email}")
        application.pending_email = ""
        application.email_verification_token = ""
        application.save(update_fields=["pending_email", "email_verification_token"])

    messages.success(request, f"Your email address has been verified as {new_email}.")
    return redirect("home")


def setup_account(request, token):
    """Single-use setup/login link for candidate onboarding and passwordless access."""
    if not token or len(token) < 16:
        messages.error(request, "Invalid setup link.")
        return redirect("login")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    application = Application.objects.filter(
        setup_token_hash=token_hash,
        setup_token_used_at__isnull=True,
        setup_token_expires_at__gte=timezone.now(),
    ).select_related("candidate").first()

    if application is None:
        messages.error(request, "This setup link is invalid, expired, or has already been used.")
        return redirect("login")

    candidate = application.candidate
    if not candidate or not candidate.is_active:
        messages.error(request, "This candidate account is inactive. Please contact the coordinator.")
        return redirect("login")

    with transaction.atomic():
        updated = Application.objects.filter(
            pk=application.pk,
            setup_token_used_at__isnull=True,
        ).update(setup_token_used_at=timezone.now())
        if not updated:
            messages.error(request, "This setup link is invalid, expired, or has already been used.")
            return redirect("login")

    from django.contrib.auth import login
    login(request, candidate)
    request.session.cycle_key()
    audit(candidate, "auth.setup_token_login", f"candidate:{application.candidate_id}")
    messages.success(request, f"Welcome, {application.full_name}. You have been signed in successfully.")
    return redirect("home")


def health_check(request):
    """Liveness probe: verifies web server process is responsive."""
    return JsonResponse({"status": "ok", "timestamp": timezone.now().isoformat()})


def readiness_check(request):
    """Readiness probe: verifies critical dependencies without exposing secrets."""
    status = {"database": "ok", "cache": "ok", "storage": "ok"}
    is_ready = True

    try:
        from django.db import connection
        connection.ensure_connection()
    except Exception:
        status["database"] = "unavailable"
        is_ready = False

    try:
        cache.set("_readiness_probe", 1, 10)
        if cache.get("_readiness_probe") != 1:
            status["cache"] = "unresponsive"
            is_ready = False
    except Exception:
        status["cache"] = "unavailable"
        is_ready = False

    try:
        storage_path = Path(dj_settings.PRIVATE_MEDIA_ROOT)
        storage_path.mkdir(parents=True, exist_ok=True)
        test_file = storage_path / ".readiness_probe"
        test_file.write_text("probe", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except Exception:
        status["storage"] = "unavailable"
        is_ready = False

    http_status = 200 if is_ready else 503
    return JsonResponse({"status": "ready" if is_ready else "degraded", "dependencies": status}, status=http_status)

