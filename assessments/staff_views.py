import csv
import os

from django.conf import settings
from django.contrib import messages
from django.core.mail import get_connection
from django.db import transaction
from django.db.models import Avg, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import notifications
from .forms import CandidateImportForm, InterviewDecisionForm, RevokeForm
from .models import Application, Attempt, AuditLog, Exam, InterviewReview, ProctoringSnapshot
from .review import attempt_csv_rows, build_attempt_review, csv_safe
from .services import AttemptError, allow_resume, snapshot_file_path
from .views import User, audit, reviewer_required, staff_required


@reviewer_required
def staff_exam_overview(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    applications = exam.applications.all()
    attempts = exam.attempts.all()
    approved = applications.filter(status=Application.Status.APPROVED).count()
    started_ids = attempts.values_list("candidate_id", flat=True)
    not_started = applications.filter(status=Application.Status.APPROVED).exclude(candidate_id__in=started_ids).count()
    finished = attempts.filter(status__in=[Attempt.Status.SUBMITTED, Attempt.Status.EXPIRED])
    completed_scores = finished.aggregate(
        mcq=Avg("mcq_score"), coding=Avg("coding_score")
    )
    context = {
        "exam": exam,
        "on_list": applications.count(),
        "approved": approved,
        "revoked": applications.filter(status=Application.Status.REVOKED).count(),
        "emailed": applications.filter(credentials_sent_at__isnull=False).count(),
        "confirmed": applications.filter(details_confirmed_at__isnull=False).count(),
        "not_started": not_started,
        "in_progress": attempts.filter(status=Attempt.Status.IN_PROGRESS).count(),
        "submitted": attempts.filter(status=Attempt.Status.SUBMITTED).count(),
        "expired": attempts.filter(status=Attempt.Status.EXPIRED).count(),
        "flagged": attempts.filter(flagged_for_review=True).count(),
        "completion_rate": round(100 * finished.count() / approved, 1) if approved else 0,
        "avg_mcq": round(float(completed_scores["mcq"] or 0), 2),
        "avg_coding": round(float(completed_scores["coding"] or 0), 2),
    }
    return render(request, "assessments/staff/overview.html", context)


SEND_BATCH = 50  # Password hashing is deliberately slow, so bulk sends are capped per request.


def _candidate_rows(exam, q="", status="", flagged=False):
    applications = exam.applications.select_related("candidate")
    if q:
        applications = applications.filter(
            Q(full_name__icontains=q) | Q(candidate__email__icontains=q)
        )
    attempts = {a.candidate_id: a for a in exam.attempts.all()}
    rows = []
    for application in applications:
        attempt = attempts.get(application.candidate_id)
        if status == "not_started" and attempt is not None:
            continue
        if status == "revoked" and application.status != Application.Status.REVOKED:
            continue
        if status and status not in ("not_started", "revoked") and (attempt is None or attempt.status != status):
            continue
        if flagged and not (attempt and attempt.flagged_for_review):
            continue
        rows.append({"application": application, "attempt": attempt})
    return rows


@staff_required
def staff_exam_candidates(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    flagged = request.GET.get("flagged") == "1"
    rows = _candidate_rows(exam, q, status, flagged)
    counts = {
        "total": exam.applications.count(),
        "unsent": exam.applications.filter(status=Application.Status.APPROVED, credentials_sent_at__isnull=True).count(),
        "not_started": exam.applications.filter(status=Application.Status.APPROVED)
        .exclude(candidate_id__in=exam.attempts.values_list("candidate_id", flat=True)).count(),
    }
    return render(
        request,
        "assessments/staff/candidates.html",
        {
            "exam": exam,
            "rows": rows,
            "q": q,
            "status": status,
            "flagged": flagged,
            "statuses": [("not_started", "Not started")] + list(Attempt.Status.choices) + [("revoked", "Access removed")],
            "counts": counts,
            "batch": SEND_BATCH,
        },
    )


@staff_required
def staff_candidates_import(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    if request.method == "POST":
        form = CandidateImportForm(request.POST)
        if form.is_valid():
            rows, errors = form.parse()
            created = existing = 0
            problems = list(errors)
            with transaction.atomic():
                for row in rows:
                    user = User.objects.filter(username=row["email"]).first()
                    if user is not None and user.is_staff:
                        problems.append(f"{row['email']} belongs to a staff account and was skipped.")
                        continue
                    if user is None:
                        user = User(username=row["email"], email=row["email"], is_active=True)
                        user.set_unusable_password()
                        user.save()
                    application, was_created = Application.objects.get_or_create(
                        candidate=user,
                        exam=exam,
                        defaults={
                            "full_name": row["full_name"] or row["email"].split("@")[0],
                            "phone": row["phone"],
                            "status": Application.Status.APPROVED,
                        },
                    )
                    if was_created:
                        created += 1
                    else:
                        existing += 1
            if created:
                audit(request.user, "candidates.imported", f"exam:{exam.pk}", f"added={created} existing={existing}")
            summary = f"{created} candidate(s) added"
            if existing:
                summary += f", {existing} already on this test"
            messages.success(request, summary + ". Use “Send login details” to email them.")
            for problem in problems:
                messages.warning(request, problem)
            return redirect("staff_exam_candidates", pk=exam.pk)
    else:
        form = CandidateImportForm()
    return render(request, "assessments/staff/candidate_import.html", {"exam": exam, "form": form})


def _send_to(application, actor, connection=None):
    """Creates a new password and emails it. The candidate's password is only updated if email delivery succeeds."""
    password = notifications.new_password()
    if not notifications.send_login_details(application, password, actor=actor, connection=connection):
        return False
    user = application.candidate
    user.set_password(password)
    user.save(update_fields=["password"])
    application.credentials_sent_at = timezone.now()
    application.save(update_fields=["credentials_sent_at"])
    return True


@staff_required
@require_POST
def staff_resend_credentials(request, pk):
    application = get_object_or_404(Application.objects.select_related("candidate", "exam"), pk=pk)
    if application.status != Application.Status.APPROVED:
        messages.error(request, "Restore this candidate's access before sending login details.")
    elif _send_to(application, request.user):
        audit(request.user, "credentials.sent", f"application:{application.pk}", application.candidate.email)
        messages.success(
            request,
            f"Login details sent to {application.candidate.email}.",
        )
    else:
        messages.error(
            request,
            f"The email to {application.candidate.email} could not be sent. Check the mail settings and try again.",
        )
    return redirect("staff_exam_candidates", pk=application.exam_id)


@staff_required
@require_POST
def staff_send_credentials(request, pk):
    """Sends login details to a batch of candidates."""
    exam = get_object_or_404(Exam, pk=pk)
    mode = request.POST.get("mode", "unsent")
    queryset = exam.applications.select_related("candidate").filter(status=Application.Status.APPROVED)
    if mode == "unsent":
        queryset = queryset.filter(credentials_sent_at__isnull=True)
    queryset = queryset.order_by("id")
    total = queryset.count()

    batch_apps = list(queryset[:SEND_BATCH])
    if not batch_apps:
        messages.info(request, "Nobody is waiting for login details.")
        return redirect("staff_exam_candidates", pk=exam.pk)

    connection = None
    try:
        connection = get_connection(timeout=settings.EMAIL_TIMEOUT)
        connection.open()
    except Exception:
        connection = None

    sent = failed = 0
    for application in batch_apps:
        if _send_to(application, request.user, connection=connection):
            sent += 1
        else:
            failed += 1

    if connection:
        try:
            connection.close()
        except Exception:
            pass

    audit(request.user, "credentials.batch", f"exam:{exam.pk}", f"mode={mode} sent={sent} failed={failed}")
    if sent:
        messages.success(request, f"Login details sent to {sent} candidate(s).")
    if failed:
        messages.warning(request, f"{failed} email(s) could not be sent. Check the mail settings, then send again.")
    remaining = total - sent - failed
    if remaining > 0:
        messages.info(request, f"{remaining} candidate(s) still waiting. Press the button again to send the next batch.")
    return redirect("staff_exam_candidates", pk=exam.pk)


@staff_required
@require_POST
def staff_access(request, pk):
    application = get_object_or_404(Application.objects.select_related("candidate"), pk=pk)
    action = request.POST.get("action")
    form = RevokeForm(request.POST)
    form.is_valid()
    if action == "revoke":
        application.status = Application.Status.REVOKED
        application.decision_reason = form.cleaned_data.get("reason", "")
        verb = "revoked"
    elif action == "restore":
        application.status = Application.Status.APPROVED
        application.decision_reason = ""
        verb = "restored"
    else:
        messages.error(request, "Choose whether to remove or restore access.")
        return redirect("staff_exam_candidates", pk=application.exam_id)
    application.changed_by = request.user
    application.changed_at = timezone.now()
    application.save()
    audit(request.user, f"access.{verb}", f"application:{application.pk}", application.candidate.email)
    messages.success(request, f"Access {verb} for {application.candidate.email}.")
    return redirect("staff_exam_candidates", pk=application.exam_id)


@staff_required
@require_POST
def staff_attempt_allow_resume(request, pk):
    """Lets a candidate continue in their current browser after a session conflict. The flag stays for review."""
    attempt = get_object_or_404(Attempt.objects.select_related("candidate"), pk=pk)
    if attempt.is_final:
        messages.error(request, "This attempt has already ended.")
    else:
        allow_resume(attempt, request.user)
        audit(request.user, "session.resume_allowed", f"attempt:{attempt.pk}", attempt.candidate.email)
        messages.success(
            request,
            "The candidate can continue in their current browser. The attempt stays flagged for review.",
        )
    return redirect("staff_attempt_review", pk=attempt.pk)


@reviewer_required
def staff_attempt_review(request, pk):
    attempt = get_object_or_404(Attempt.objects.select_related("exam", "candidate", "application"), pk=pk)
    context = build_attempt_review(attempt)
    context["decision_form"] = InterviewDecisionForm()
    return render(request, "assessments/staff/attempt_review.html", context)


@reviewer_required
@require_POST
def staff_attempt_decision(request, pk):
    attempt = get_object_or_404(Attempt, pk=pk)
    if attempt.candidate_id == request.user.pk:
        messages.error(request, "You cannot record a decision on your own attempt.")
        return redirect("staff_attempt_review", pk=attempt.pk)
    form = InterviewDecisionForm(request.POST)
    if form.is_valid():
        review = InterviewReview.objects.create(
            attempt=attempt,
            reviewer=request.user,
            decision=form.cleaned_data["decision"],
            notes=form.cleaned_data["notes"],
        )
        audit(request.user, "interview.decision", f"attempt:{attempt.pk}", f"{review.decision} – {review.notes[:300]}")
        messages.success(request, "Interview decision recorded.")
    else:
        messages.error(request, "Please choose a decision and add notes.")
    return redirect("staff_attempt_review", pk=attempt.pk)


@reviewer_required
def staff_snapshot(request, pk):
    snapshot = get_object_or_404(ProctoringSnapshot.objects.select_related("attempt"), pk=pk)
    try:
        path = snapshot_file_path(snapshot)
    except AttemptError as exc:
        raise Http404("Snapshot unavailable.") from exc
    if not path.exists():
        raise Http404("Snapshot unavailable.")
    audit(request.user, "snapshot.viewed", f"snapshot:{snapshot.pk}", f"attempt={snapshot.attempt_id}")
    # FileResponse takes ownership of the open file and closes it after streaming.
    response = FileResponse(open(path, "rb"), content_type=snapshot.content_type)  # noqa: SIM115
    response["Cache-Control"] = "private, no-store"
    response["Content-Disposition"] = f'inline; filename="snapshot-{snapshot.pk}"'
    return response


@reviewer_required
@require_POST
def staff_snapshot_review(request, pk):
    snapshot = get_object_or_404(ProctoringSnapshot, pk=pk)
    status = request.POST.get("review_status")
    if status in ProctoringSnapshot.ReviewStatus.values:
        snapshot.review_status = status
        snapshot.save(update_fields=["review_status"])
        audit(request.user, "snapshot.reviewed", f"snapshot:{snapshot.pk}", status)
    return redirect("staff_attempt_review", pk=snapshot.attempt_id)


@staff_required
def staff_export_csv(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="exam-{exam.pk}-results.csv"'
    writer = csv.DictWriter(
        response,
        fieldnames=[
            "full_name", "username", "email", "application_status", "attempt_status",
            "started_at", "ended_at", "mcq_score", "coding_score", "flagged_for_review",
        ],
    )
    writer.writeheader()
    for row in attempt_csv_rows(exam):
        writer.writerow({key: csv_safe(value) for key, value in row.items()})
    audit(request.user, "export.results", f"exam:{exam.pk}", "csv")
    return response


@staff_required
def staff_audit_log(request):
    entries = AuditLog.objects.select_related("actor")[:500]
    return render(request, "assessments/staff/audit_log.html", {"entries": entries})


@reviewer_required
def staff_application_resume(request, pk):
    application = get_object_or_404(Application, pk=pk)
    if not application.resume or not application.resume.name:
        raise Http404("No resume uploaded for this candidate.")
    try:
        file_path = application.resume.path
        if os.path.exists(file_path):
            return FileResponse(open(file_path, "rb"), as_attachment=False, filename=os.path.basename(file_path))
    except (NotImplementedError, AttributeError):
        pass
    try:
        return FileResponse(application.resume.open("rb"), as_attachment=False)
    except (OSError, FileNotFoundError) as err:
        raise Http404("Resume file not found on disk.") from err

