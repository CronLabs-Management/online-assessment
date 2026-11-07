import csv
import json
import logging
import mimetypes
import os

logger = logging.getLogger(__name__)

from django.conf import settings
from django.contrib import messages
from django.core.mail import get_connection
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from . import notifications
from .ai_interview import generate_interview_questions_with_gemini, get_gemini_config
from .forms import CandidateEditForm, CandidateImportForm, InterviewDecisionForm, RevokeForm
from .models import Application, Attempt, AuditLog, Exam, InterviewReview, ProctoringSnapshot
from .permissions import any_permission_required, has_perm, is_team_member, permission_required
from .review import attempt_csv_rows, build_attempt_review, csv_safe
from .services import AttemptError, allow_resume, expire_due_attempts, snapshot_file_path
from .views import User, audit


@permission_required("tests.view")
def staff_exam_overview(request, pk):
    expire_due_attempts()
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


def _candidate_rows(exam, q="", status="", flagged=False, sort="newest", score_view="all"):
    applications = exam.applications.select_related("candidate")
    if q:
        applications = applications.filter(
            Q(full_name__icontains=q) | Q(candidate__email__icontains=q) | Q(phone__icontains=q)
        )
    attempts = {
        a.candidate_id: a
        for a in exam.attempts.select_related("candidate", "exam").prefetch_related(
            "reviews", "mcq_items__question", "coding_items__problem"
        )
    }
    rows = []
    for application in applications:
        attempt = attempts.get(application.candidate_id)
        if status == "not_started" and attempt is not None:
            continue
        if status == "revoked" and application.status != Application.Status.REVOKED:
            continue
        if status == "completed" and (attempt is None or attempt.status not in (Attempt.Status.SUBMITTED, Attempt.Status.EXPIRED)):
            continue
        if status and status not in ("not_started", "revoked", "completed") and (attempt is None or attempt.status != status):
            continue
        if flagged and not (attempt and attempt.flagged_for_review):
            continue

        mcq_val = float(attempt.mcq_score) if (attempt and attempt.mcq_score is not None) else None
        coding_val = float(attempt.coding_score) if (attempt and attempt.coding_score is not None) else None
        if mcq_val is not None or coding_val is not None:
            total_val = round((mcq_val or 0.0) + (coding_val or 0.0), 2)
        else:
            total_val = None

        rounds_status = attempt.interview_rounds_status if attempt else None

        round_1_data = None
        round_2_data = None
        interview_score_sum = 0.0
        interview_max_sum = 0.0
        has_any_interview_score = False

        if attempt:
            reviews_by_round = {}
            for rev in attempt.reviews.all():
                if rev.round_number not in reviews_by_round:
                    reviews_by_round[rev.round_number] = rev

            r1 = reviews_by_round.get(1)
            if r1:
                score_num = float(r1.interview_score) if r1.interview_score is not None else None
                max_num = float(r1.max_interview_score) if r1.max_interview_score is not None else 10.0
                round_1_data = {
                    "score": score_num,
                    "max": max_num,
                    "decision": r1.decision,
                    "selected": r1.selected_for_next_round,
                    "interviewer": r1.interviewer_name or (r1.reviewer.username if r1.reviewer else ""),
                }
                if score_num is not None:
                    interview_score_sum += score_num
                    interview_max_sum += max_num
                    has_any_interview_score = True

            r2 = reviews_by_round.get(2)
            if r2:
                score_num = float(r2.interview_score) if r2.interview_score is not None else None
                max_num = float(r2.max_interview_score) if r2.max_interview_score is not None else 10.0
                round_2_data = {
                    "score": score_num,
                    "max": max_num,
                    "decision": r2.decision,
                    "selected": r2.selected_for_next_round,
                    "interviewer": r2.interviewer_name or (r2.reviewer.username if r2.reviewer else ""),
                }
                if score_num is not None:
                    interview_score_sum += score_num
                    interview_max_sum += max_num
                    has_any_interview_score = True

            for rnd_num, rev in reviews_by_round.items():
                if rnd_num > 2 and rev.interview_score is not None:
                    interview_score_sum += float(rev.interview_score)
                    interview_max_sum += float(rev.max_interview_score or 10.0)
                    has_any_interview_score = True

        if total_val is not None or has_any_interview_score:
            final_val = round((total_val or 0.0) + interview_score_sum, 2)
            if attempt:
                mcq_max = sum(item.question.marks for item in attempt.mcq_items.all())
                coding_max = sum(item.problem.marks for item in attempt.coding_items.all())
                exam_max = float(mcq_max + coding_max)
            else:
                exam_mcq_max = sum(q.marks for q in exam.mcq_questions.all())
                exam_coding_max = sum(p.marks for p in exam.coding_problems.all())
                exam_max = float(exam_mcq_max + exam_coding_max)
            final_max = round(exam_max + interview_max_sum, 2) if (exam_max or interview_max_sum) else None
        else:
            final_val = None
            final_max = None

        rows.append({
            "application": application,
            "attempt": attempt,
            "mcq_score": mcq_val,
            "coding_score": coding_val,
            "total_score": total_val,
            "round_1": round_1_data,
            "round_2": round_2_data,
            "final_score": final_val,
            "final_max": final_max,
            "rounds_status": rounds_status,
        })

    # Dynamic sorting
    if sort == "total_desc":
        rows.sort(key=lambda r: (r["total_score"] is not None, r["total_score"] or 0, (r["application"].full_name or "").lower()), reverse=True)
    elif sort == "total_asc":
        rows.sort(key=lambda r: (r["total_score"] is None, r["total_score"] if r["total_score"] is not None else 0, (r["application"].full_name or "").lower()))
    elif sort == "final_desc":
        rows.sort(key=lambda r: (r["final_score"] is not None, r["final_score"] or 0, (r["application"].full_name or "").lower()), reverse=True)
    elif sort == "final_asc":
        rows.sort(key=lambda r: (r["final_score"] is None, r["final_score"] if r["final_score"] is not None else 0, (r["application"].full_name or "").lower()))
    elif sort == "mcq_desc":
        rows.sort(key=lambda r: (r["mcq_score"] is not None, r["mcq_score"] or 0, (r["application"].full_name or "").lower()), reverse=True)
    elif sort == "mcq_asc":
        rows.sort(key=lambda r: (r["mcq_score"] is None, r["mcq_score"] if r["mcq_score"] is not None else 0, (r["application"].full_name or "").lower()))
    elif sort == "coding_desc":
        rows.sort(key=lambda r: (r["coding_score"] is not None, r["coding_score"] or 0, (r["application"].full_name or "").lower()), reverse=True)
    elif sort == "coding_asc":
        rows.sort(key=lambda r: (r["coding_score"] is None, r["coding_score"] if r["coding_score"] is not None else 0, (r["application"].full_name or "").lower()))
    elif sort == "r1_desc":
        rows.sort(key=lambda r: (r["round_1"] is not None and r["round_1"]["score"] is not None, r["round_1"]["score"] if (r["round_1"] and r["round_1"]["score"] is not None) else 0, (r["application"].full_name or "").lower()), reverse=True)
    elif sort == "r1_asc":
        rows.sort(key=lambda r: (r["round_1"] is None or r["round_1"]["score"] is None, r["round_1"]["score"] if (r["round_1"] and r["round_1"]["score"] is not None) else 999999, (r["application"].full_name or "").lower()))
    elif sort == "r2_desc":
        rows.sort(key=lambda r: (r["round_2"] is not None and r["round_2"]["score"] is not None, r["round_2"]["score"] if (r["round_2"] and r["round_2"]["score"] is not None) else 0, (r["application"].full_name or "").lower()), reverse=True)
    elif sort == "r2_asc":
        rows.sort(key=lambda r: (r["round_2"] is None or r["round_2"]["score"] is None, r["round_2"]["score"] if (r["round_2"] and r["round_2"]["score"] is not None) else 999999, (r["application"].full_name or "").lower()))
    elif sort == "name_asc":
        rows.sort(key=lambda r: (r["application"].full_name or "").lower())
    elif sort == "name_desc":
        rows.sort(key=lambda r: (r["application"].full_name or "").lower(), reverse=True)
    elif sort == "status":
        rows.sort(key=lambda r: (r["attempt"].status if r["attempt"] else "zz_not_started", (r["application"].full_name or "").lower()))
    elif sort == "oldest":
        rows.sort(key=lambda r: r["application"].created_at)
    else:  # newest / default
        rows.sort(key=lambda r: r["application"].created_at, reverse=True)

    return rows


@permission_required("candidates.view")
def staff_exam_candidates(request, pk):
    expire_due_attempts()
    exam = get_object_or_404(Exam, pk=pk)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    flagged = request.GET.get("flagged") == "1"
    sort = request.GET.get("sort", "newest").strip()

    rows = _candidate_rows(exam, q=q, status=status, flagged=flagged, sort=sort)
    paginator = Paginator(rows, 50)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)
    finished_count = exam.attempts.filter(status__in=[Attempt.Status.SUBMITTED, Attempt.Status.EXPIRED]).count()
    counts = {
        "total": exam.applications.count(),
        "unsent": exam.applications.filter(status=Application.Status.APPROVED, credentials_sent_at__isnull=True).count(),
        "not_started": exam.applications.filter(status=Application.Status.APPROVED)
        .exclude(candidate_id__in=exam.attempts.values_list("candidate_id", flat=True)).count(),
        "completed": finished_count,
        "submitted": exam.attempts.filter(status=Attempt.Status.SUBMITTED).count(),
        "expired": exam.attempts.filter(status=Attempt.Status.EXPIRED).count(),
        "in_progress": exam.attempts.filter(status=Attempt.Status.IN_PROGRESS).count(),
        "flagged": exam.attempts.filter(flagged_for_review=True).count(),
    }
    has_coding = exam.coding_problems.exists() or exam.sections.filter(kind="coding").exists() or any(r["coding_score"] is not None for r in rows)
    has_round_2 = (getattr(exam, "interview_rounds_count", 1) or 1) >= 2 or any(r["round_2"] is not None for r in rows)

    return render(
        request,
        "assessments/staff/candidates.html",
        {
            "exam": exam,
            "rows": page_obj.object_list,
            "page_obj": page_obj,
            "q": q,
            "status": status,
            "flagged": flagged,
            "sort": sort,
            "has_coding": has_coding,
            "has_round_2": has_round_2,
            "sort_options": [
                ("total_desc", "Assessment Total (High → Low)"),
                ("total_asc", "Assessment Total (Low → High)"),
                ("final_desc", "Final Score (High → Low)"),
                ("final_asc", "Final Score (Low → High)"),
                ("mcq_desc", "MCQ Marks (High → Low)"),
                ("mcq_asc", "MCQ Marks (Low → High)"),
                ("coding_desc", "Coding Marks (High → Low)"),
                ("coding_asc", "Coding Marks (Low → High)"),
                ("r1_desc", "Round 1 Score (High → Low)"),
                ("r2_desc", "Round 2 Score (High → Low)"),
                ("name_asc", "Candidate Name (A → Z)"),
                ("name_desc", "Candidate Name (Z → A)"),
                ("status", "Assessment Status"),
                ("newest", "Date Enrolled (Newest First)"),
                ("oldest", "Date Enrolled (Oldest First)"),
            ],
            "statuses": [
                ("completed", "Completed (All)"),
                ("submitted", "Submitted (On Time)"),
                ("expired", "Timed Out / Expired"),
                ("in_progress", "In Progress"),
                ("not_started", "Not started"),
                ("revoked", "Access removed"),
            ],
            "counts": counts,
            "batch": SEND_BATCH,
        },
    )


@permission_required("candidates.manage")
def staff_candidates_import(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    if request.method == "POST":
        form = CandidateImportForm(request.POST)
        if form.is_valid():
            rows, errors = form.parse()
            created = existing = 0
            problems = list(errors)
            duplicates = []
            with transaction.atomic():
                for row in rows:
                    user = User.objects.filter(Q(username__iexact=row["email"]) | Q(email__iexact=row["email"])).first()
                    if user is not None and is_team_member(user):
                        problems.append(f"{row['email']} belongs to a staff or admin account and was skipped.")
                        continue
                    if user is None:
                        user = User(username=row["email"], email=row["email"], is_active=True)
                        user.set_unusable_password()
                        user.save()
                    application, was_created = Application.objects.get_or_create(
                        candidate=user,
                        exam=exam,
                        defaults={
                            "full_name": row["full_name"],
                            "phone": row["phone"],
                            "status": Application.Status.APPROVED,
                        },
                    )
                    application.ensure_password()
                    if was_created:
                        created += 1
                    else:
                        existing += 1
                        display_name = application.full_name or user.email
                        duplicates.append(f"{display_name} ({user.email})")
            if created:
                audit(request.user, "candidates.imported", f"exam:{exam.pk}", f"added={created} existing={existing}")

            if created > 0 and duplicates:
                messages.success(request, f"Successfully added {created} candidate(s). Use “Batch Credentials” to email their passwords.")
                messages.warning(
                    request,
                    f"Duplicate Candidates Detected ({len(duplicates)}): The following candidate(s) are already registered for {exam.title} and were skipped: {', '.join(duplicates)}."
                )
            elif created > 0:
                messages.success(request, f"Successfully added {created} candidate(s). Use “Batch Credentials” to email their passwords.")
            elif duplicates:
                messages.warning(
                    request,
                    f"Duplicate Candidates Detected ({len(duplicates)}): No new candidates added. The following candidate(s) are already registered for {exam.title}: {', '.join(duplicates)}."
                )
            elif not problems:
                messages.info(request, "No candidate records were processed.")

            for problem in problems:
                messages.warning(request, problem)
            return redirect("staff_exam_candidates", pk=exam.pk)
    else:
        form = CandidateImportForm()
    enrolled_candidates = [
        {"email": app.candidate.email, "name": app.full_name or app.candidate.email}
        for app in exam.applications.select_related("candidate")
    ]
    return render(
        request,
        "assessments/staff/candidate_import.html",
        {
            "exam": exam,
            "form": form,
            "enrolled_candidates": enrolled_candidates,
        },
    )


def _send_to(application, actor, connection=None):
    """Sends current active password to candidate without rotating it."""
    password = application.plain_password
    if not password:
        password = application.ensure_password()
    if not notifications.send_login_details(application, password, actor=actor, connection=connection):
        return False
    application.credentials_sent_at = timezone.now()
    application.save(update_fields=["credentials_sent_at"])
    return True


@permission_required("credentials.send")
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


@permission_required("credentials.send")
@require_POST
def staff_regenerate_credentials(request, pk):
    application = get_object_or_404(Application.objects.select_related("candidate", "exam"), pk=pk)
    if application.status != Application.Status.APPROVED:
        messages.error(request, "Restore this candidate's access before regenerating credentials.")
        return redirect("staff_exam_candidates", pk=application.exam_id)

    new_password = notifications.new_password()
    application.plain_password = new_password
    user = application.candidate
    user.set_password(new_password)
    user.save(update_fields=["password"])
    application.save(update_fields=["plain_password"])

    send_now = request.POST.get("send_email") == "1"
    if send_now:
        if notifications.send_login_details(application, new_password, actor=request.user):
            application.credentials_sent_at = timezone.now()
            application.save(update_fields=["credentials_sent_at"])
            messages.success(request, f"New credentials generated and emailed to {application.candidate.email}.")
        else:
            messages.warning(request, f"New credentials generated, but email to {application.candidate.email} could not be delivered.")
    else:
        messages.success(request, f"New password generated for {application.candidate.email}. You can copy or send it anytime.")

    audit(request.user, "credentials.regenerated", f"application:{application.pk}", f"email_sent={send_now}")
    return redirect("staff_exam_candidates", pk=application.exam_id)


@permission_required("credentials.view")
@require_POST
def staff_audit_credential_access(request, pk):
    application = get_object_or_404(Application, pk=pk)
    action = request.POST.get("action", "revealed")
    if action not in ("revealed", "copied"):
        action = "revealed"
    audit(request.user, f"credentials.{action}", f"application:{application.pk}", application.candidate.email)
    return JsonResponse({"ok": True})


@permission_required("credentials.send")
@require_POST
def staff_send_credentials(request, pk):
    """Sends login details to a batch of candidates."""
    exam = get_object_or_404(Exam, pk=pk)
    mode = request.POST.get("mode", "unsent")
    selected_ids = request.POST.getlist("selected_ids")
    if not selected_ids and request.POST.get("ids"):
        selected_ids = [i.strip() for i in request.POST.get("ids").split(",") if i.strip()]

    queryset = exam.applications.select_related("candidate").filter(status=Application.Status.APPROVED)
    if selected_ids:
        queryset = queryset.filter(pk__in=selected_ids)
    elif mode == "unsent":
        queryset = queryset.filter(credentials_sent_at__isnull=True)
    queryset = queryset.order_by("id")
    total = queryset.count()

    batch_limit = len(selected_ids) if selected_ids else SEND_BATCH
    batch_apps = list(queryset[:batch_limit])
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

    audit(request.user, "credentials.batch", f"exam:{exam.pk}", f"mode={mode} sent={sent} failed={failed} selected={len(selected_ids) if selected_ids else 0}")
    if sent:
        if selected_ids:
            messages.success(request, f"Login details sent to {sent} selected candidate(s).")
        else:
            messages.success(request, f"Login details sent to {sent} candidate(s).")
    if failed:
        messages.warning(request, f"{failed} email(s) could not be sent. Check the mail settings, then send again.")
    remaining = total - sent - failed
    if remaining > 0 and not selected_ids:
        messages.info(request, f"{remaining} candidate(s) still waiting. Press the button again to send the next batch.")
    return redirect("staff_exam_candidates", pk=exam.pk)


@permission_required("candidates.manage")
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


@permission_required("candidates.manage")
def staff_candidate_edit(request, pk):
    application = get_object_or_404(Application.objects.select_related("candidate", "exam"), pk=pk)
    can_see_password = has_perm(request.user, "credentials.view")
    previous_password = application.plain_password
    if request.method == "POST":
        form = CandidateEditForm(request.POST, request.FILES, instance=application)
        if not can_see_password:
            form.fields.pop("plain_password", None)
        if form.is_valid():
            with transaction.atomic():
                app = form.save()
                new_email = form.cleaned_data["email"].strip().lower()
                user = app.candidate
                if user.email != new_email or user.username != new_email:
                    if User.objects.exclude(pk=user.pk).filter(Q(username__iexact=new_email) | Q(email__iexact=new_email)).exists():
                        messages.error(request, f"Email {new_email} is already in use by another account.")
                        return render(request, "assessments/staff/candidate_edit.html", {"form": form, "application": application})
                    user.username = new_email
                    user.email = new_email
                    user.save(update_fields=["username", "email"])
                new_pw = (form.cleaned_data.get("plain_password") or "").strip()
                # Compare with the password before the form was saved: form.save() has already copied
                # the new value onto the application, so comparing with app.plain_password never matched.
                if new_pw and new_pw != previous_password:
                    user.set_password(new_pw)
                    user.save(update_fields=["password"])
                    app.plain_password = new_pw
                    app.save(update_fields=["plain_password"])
            audit(request.user, "candidate.updated", f"application:{application.pk}", f"{application.full_name} ({application.candidate.email})")
            messages.success(request, f"Updated details for {application.full_name}.")
            return redirect("staff_exam_candidates", pk=application.exam_id)
    else:
        form = CandidateEditForm(instance=application)
        if not can_see_password:
            form.fields.pop("plain_password", None)
    return render(request, "assessments/staff/candidate_edit.html", {"form": form, "application": application})


@permission_required("candidates.manage")
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


@permission_required("results.view")
def staff_attempt_review(request, pk):
    expire_due_attempts()
    attempt = get_object_or_404(
        Attempt.objects.select_related("exam", "candidate", "application").prefetch_related("reviews"), pk=pk
    )

    context = build_attempt_review(attempt)
    configured_api_key, configured_model = get_gemini_config()
    context["has_gemini_key"] = bool(configured_api_key)
    context["gemini_model"] = configured_model

    # Multi-round interview workflow status
    rounds_status = attempt.interview_rounds_status
    current_round = rounds_status["current_round"]
    context["current_round"] = current_round

    ORDINALS = {
        1: "First", 2: "Second", 3: "Third", 4: "Fourth", 5: "Fifth",
        6: "Sixth", 7: "Seventh", 8: "Eighth", 9: "Ninth", 10: "Tenth"
    }
    context["round_ordinal"] = ORDINALS.get(current_round, f"Round {current_round}")
    next_rnd = current_round + 1
    context["next_round_number"] = next_rnd
    context["next_round_ordinal"] = ORDINALS.get(next_rnd, f"Round {next_rnd}")
    context["is_final_round"] = (current_round >= rounds_status["total_rounds"])
    context["allowed_rounds_range"] = list(range(1, 11))

    # Categorize questions by difficulty: Easy, Intermediate, Hard
    raw_questions = attempt.application.ai_interview_questions or []
    if not raw_questions and attempt.application:
        try:
            gen_res = generate_interview_questions_with_gemini(attempt.application)
            if gen_res.get("success"):
                attempt.application.refresh_from_db()
                raw_questions = attempt.application.ai_interview_questions or []
        except Exception as exc:
            logger.warning("Auto generation of questions failed: %s", exc)

    enriched_questions = []
    for idx, q in enumerate(raw_questions, 1):
        q_copy = dict(q)
        q_copy["id"] = q_copy.get("id") or idx
        q_copy["max_marks"] = int(q_copy.get("max_marks", 10))
        # Keep scored state separate from 0
        has_score = bool(q_copy.get("answered") or q_copy.get("is_reviewed"))
        q_copy["is_reviewed"] = has_score
        q_copy["answered"] = has_score
        if has_score:
            q_copy["marks_awarded"] = float(q_copy.get("marks_awarded", 0))
        else:
            q_copy["marks_awarded"] = None
        enriched_questions.append(q_copy)

    easy_questions = [q for q in enriched_questions if q.get("difficulty") == "Easy"]
    intermediate_questions = [q for q in enriched_questions if q.get("difficulty") == "Intermediate"]
    hard_questions = [q for q in enriched_questions if q.get("difficulty") == "Hard"]

    # Calculate marks totals
    total_max_marks = sum(int(q.get("max_marks", 10)) for q in enriched_questions)
    total_awarded_marks = sum(float(q.get("marks_awarded", 0)) for q in enriched_questions if q.get("marks_awarded") is not None)
    answered_count = sum(1 for q in enriched_questions if q.get("is_reviewed") or q.get("answered"))

    context["interview_questions"] = enriched_questions
    context["easy_questions"] = easy_questions
    context["intermediate_questions"] = intermediate_questions
    context["hard_questions"] = hard_questions
    context["has_questions"] = bool(enriched_questions)
    context["total_max_marks"] = total_max_marks
    context["total_awarded_marks"] = total_awarded_marks
    context["answered_count"] = answered_count
    context["unanswered_count"] = len(enriched_questions) - answered_count

    # Review history for all previous rounds with ordinals
    round_reviews = list(attempt.reviews.all().order_by("round_number", "-created_at"))
    reviews_by_round = {}
    for rev in round_reviews:
        rev.round_ordinal = ORDINALS.get(rev.round_number, f"Round {rev.round_number}")
        if rev.round_number not in reviews_by_round:
            reviews_by_round[rev.round_number] = rev
    context["round_reviews"] = round_reviews

    # Enrich rounds with round-specific details
    total_rounds = len(rounds_status["rounds"])
    total_q = len(enriched_questions)
    for r_info in rounds_status["rounds"]:
        rnd_num = r_info["round_number"]
        rev = reviews_by_round.get(rnd_num)
        r_info["review"] = rev
        r_info["round_ordinal"] = ORDINALS.get(rnd_num, f"Round {rnd_num}")
        if rev and rev.questions_data:
            r_qs = rev.questions_data
        else:
            if total_rounds == 2 and total_q >= 6:
                r_qs = enriched_questions[:4] if rnd_num == 1 else enriched_questions[4:]
            elif total_rounds > 1 and total_q > 0:
                per_round = max(1, total_q // total_rounds)
                start_idx = (rnd_num - 1) * per_round
                end_idx = start_idx + per_round if rnd_num < total_rounds else total_q
                r_qs = enriched_questions[start_idx:end_idx]
            else:
                r_qs = enriched_questions
        r_scored = sum(1 for q in r_qs if q.get("is_reviewed") or q.get("answered"))
        r_info["questions"] = r_qs
        r_info["scored_count"] = r_scored
        r_info["total_count"] = len(r_qs)
        r_info["round_score"] = float(rev.interview_score) if rev and rev.interview_score is not None else sum(float(q.get("marks_awarded", 0)) for q in r_qs if q.get("marks_awarded") is not None)
        r_info["round_max_score"] = float(rev.max_interview_score) if rev and rev.max_interview_score is not None else sum(int(q.get("max_marks", 10)) for q in r_qs)

    context["rounds_status"] = rounds_status

    # Needs attention list for Overview tab
    needs_attention = []
    if rounds_status.get("overall_status") not in ("selected", "rejected") and current_round:
        cur_rnd_info = next((r for r in rounds_status["rounds"] if r["round_number"] == current_round), None)
        if cur_rnd_info and not cur_rnd_info["is_done"]:
            needs_attention.append({
                "type": "round",
                "title": f"Interview {ORDINALS.get(current_round, f'Round {current_round}')} Round Pending",
                "detail": f"Interviewer score and outcome decision pending for Round {current_round}.",
                "badge": "Action Required",
                "badge_tone": "amber",
                "action_tab": "round-reviews",
                "action_round": current_round,
                "action_label": f"Evaluate Round {current_round}",
            })

    if context["summary"].get("flagged_for_review"):
        sec_events = context["summary"].get("session_changed_count", 0)
        tab_switches = context["summary"].get("tab_switches", 0)
        missing_face = context["summary"].get("missing_face_count", 0)

        detail_items = []
        if sec_events > 0:
            detail_items.append(f"{sec_events} secondary browser attempt(s)")
        if tab_switches > 3:
            detail_items.append(f"{tab_switches} tab switches")
        if missing_face > 0:
            detail_items.append(f"{missing_face} face absence alerts")

        needs_attention.append({
            "type": "integrity",
            "title": "Integrity Signals Exceeded Threshold",
            "detail": " &bull; ".join(detail_items) if detail_items else "Review security events before finalizing evaluation.",
            "badge": "Integrity Review",
            "badge_tone": "rose",
            "action_tab": "integrity-session",
            "action_label": "View Integrity Telemetry",
        })

    unans_mcq = context["summary"].get("mcq_unanswered_count", 0)
    if unans_mcq > 0:
        needs_attention.append({
            "type": "mcq",
            "title": f"{unans_mcq} Question(s) Left Unanswered",
            "detail": "Candidate left one or more MCQ questions unanswered during the test.",
            "badge": "Assessment Info",
            "badge_tone": "slate",
            "action_tab": "assessment",
            "action_subtab": "mcq",
            "action_label": "Inspect MCQs",
        })
    context["needs_attention"] = needs_attention

    # Current round initial form
    context["decision_form"] = InterviewDecisionForm(
        initial={
            "round_number": current_round,
            "selected_for_next_round": True,
            "decision": InterviewReview.Decision.RECOMMENDED,
            "interviewer_name": request.user.get_full_name() or request.user.username,
        }
    )
    return render(request, "assessments/staff/attempt_review.html", context)


@permission_required("tests.manage")
@require_POST
def staff_exam_update_rounds(request, pk):
    exam = get_object_or_404(Exam, pk=pk)
    try:
        rounds_count = int(request.POST.get("interview_rounds_count", 1))
        if 1 <= rounds_count <= 10:
            exam.interview_rounds_count = rounds_count
            exam.save(update_fields=["interview_rounds_count"])
            audit(request.user, "exam.update_rounds", f"exam:{exam.pk}", f"rounds={rounds_count}")
            messages.success(request, f"Total interview rounds updated to {rounds_count} for '{exam.title}'.")
        else:
            messages.error(request, "Interview rounds count must be between 1 and 10.")
    except (ValueError, TypeError):
        messages.error(request, "Invalid rounds count specified.")

    default_url = reverse("staff_exam_candidates", args=[exam.pk])
    target_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if target_url and url_has_allowed_host_and_scheme(
        url=target_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = target_url
    else:
        next_url = default_url
    return redirect(next_url)


@permission_required("interviews.conduct")
@require_POST
def staff_attempt_decision(request, pk):
    attempt = get_object_or_404(Attempt.objects.select_related("exam", "application"), pk=pk)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("accept", "")

    if attempt.candidate_id == request.user.pk:
        if is_ajax:
            return JsonResponse({"success": False, "error": "You cannot record an interview evaluation on your own attempt."}, status=403)
        messages.error(request, "You cannot record an interview evaluation on your own attempt.")
        return redirect("staff_attempt_review", pk=attempt.pk)

    post_data = request.POST.copy()
    if not post_data and request.body:
        try:
            body_data = json.loads(request.body.decode("utf-8"))
            if isinstance(body_data, dict):
                post_data.update(body_data)
        except Exception:
            pass

    form = InterviewDecisionForm(post_data)
    if form.is_valid():
        round_number = form.cleaned_data.get("round_number") or 1
        max_configured_rounds = max(attempt.exam.interview_rounds_count, 1)
        if round_number < 1 or round_number > max_configured_rounds + 2:
            err_msg = f"Round number {round_number} is out of allowed range (1 to {max_configured_rounds + 2})."
            if is_ajax:
                return JsonResponse({"success": False, "error": err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect("staff_attempt_review", pk=attempt.pk)

        notes = form.cleaned_data.get("notes", "").strip()
        interviewer_name = form.cleaned_data.get("interviewer_name", "").strip() or (
            request.user.get_full_name() or request.user.username
        )
        candidate_summary = form.cleaned_data.get("candidate_summary", "").strip() or notes or "Evaluation recorded"
        decision = form.cleaned_data["decision"]

        # When decision is recommended, candidate is selected for next round / final offer
        if "selected_for_next_round" in post_data:
            raw_sel = post_data.get("selected_for_next_round")
            selected_for_next_round = (raw_sel in [True, "true", "True", "1", "on"] and decision != InterviewReview.Decision.NOT_RECOMMENDED)
        else:
            selected_for_next_round = (decision == InterviewReview.Decision.RECOMMENDED)

        interview_score = form.cleaned_data.get("interview_score")
        max_interview_score = form.cleaned_data.get("max_interview_score")

        if interview_score is not None:
            if interview_score < 0:
                err_msg = "Interview score cannot be negative."
                if is_ajax:
                    return JsonResponse({"success": False, "error": err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect("staff_attempt_review", pk=attempt.pk)
            if max_interview_score is not None and interview_score > max_interview_score:
                err_msg = f"Interview score ({interview_score}) cannot exceed maximum ({max_interview_score})."
                if is_ajax:
                    return JsonResponse({"success": False, "error": err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect("staff_attempt_review", pk=attempt.pk)

        # Update questions answered state and marks in application with size and schema validation
        questions_raw = post_data.get("questions_data", "")
        questions_list = []
        if questions_raw:
            raw_str = json.dumps(questions_raw) if isinstance(questions_raw, list) else str(questions_raw)
            if len(raw_str) > 65536:
                err_msg = "Question data payload exceeds maximum allowed size (64KB)."
                if is_ajax:
                    return JsonResponse({"success": False, "error": err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect("staff_attempt_review", pk=attempt.pk)
            try:
                parsed = questions_raw if isinstance(questions_raw, list) else json.loads(questions_raw)
                if isinstance(parsed, list):
                    # Schema sanitize each question dict
                    for item in parsed:
                        if isinstance(item, dict):
                            questions_list.append({
                                "question": str(item.get("question", ""))[:500],
                                "marks": float(item.get("marks", 0)) if item.get("marks") is not None else 0,
                                "max_marks": float(item.get("max_marks", 10)) if item.get("max_marks") is not None else 10,
                                "status": str(item.get("status", "answered"))[:50],
                                "notes": str(item.get("notes", ""))[:1000],
                            })
            except Exception as exc:
                logger.warning("Could not parse questions_data JSON: %s", exc)

        with transaction.atomic():
            if questions_list:
                attempt.application.ai_interview_questions = questions_list
                attempt.application.save(update_fields=["ai_interview_questions"])

            review, _ = InterviewReview.objects.update_or_create(
                attempt=attempt,
                round_number=round_number,
                defaults={
                    "reviewer": request.user,
                    "interviewer_name": interviewer_name,
                    "candidate_summary": candidate_summary,
                    "decision": decision,
                    "selected_for_next_round": selected_for_next_round,
                    "interview_score": interview_score,
                    "max_interview_score": max_interview_score,
                    "notes": notes,
                    "questions_data": questions_list,
                },
            )

            decision_label = review.get_decision_display()
            audit(
                request.user,
                "interview.decision",
                f"attempt:{attempt.pk}",
                f"round={round_number}, signed_by={interviewer_name}, decision={decision_label}, score={interview_score}",
            )
        messages.success(
            request,
            f"Round {round_number} evaluation recorded by {interviewer_name}. Status: {decision_label}.",
        )
        if is_ajax:
            return JsonResponse({
                "success": True,
                "round_number": round_number,
                "decision": decision,
                "decision_display": decision_label,
                "interviewer_name": interviewer_name,
                "interview_score": float(interview_score) if interview_score is not None else None,
                "max_interview_score": float(max_interview_score) if max_interview_score is not None else None,
                "message": f"Round {round_number} evaluation recorded by {interviewer_name}. Status: {decision_label}.",
            })
    else:
        errs = "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items())
        if is_ajax:
            return JsonResponse({"success": False, "error": errs, "errors": form.errors}, status=400)
        messages.error(request, f"Please complete the interview form fields: {errs}")

    redirect_url = reverse("staff_attempt_review", args=[attempt.pk]) + "#interview-section"
    return redirect(redirect_url)


@permission_required("results.view")
def staff_snapshot(request, pk):
    snapshot = get_object_or_404(ProctoringSnapshot.objects.select_related("attempt"), pk=pk)
    try:
        path = snapshot_file_path(snapshot)
    except AttemptError as exc:
        raise Http404("Snapshot unavailable.") from exc
    if not path.exists():
        raise Http404("Snapshot unavailable.")
    audit(request.user, "snapshot.viewed", f"snapshot:{snapshot.pk}", f"attempt={snapshot.attempt_id}")
    ext = mimetypes.guess_extension(snapshot.content_type) or ".webp"
    filename = f"snapshot-{snapshot.pk}{ext}"
    as_attachment = request.GET.get("download") == "1"
    response = FileResponse(open(path, "rb"), content_type=snapshot.content_type, as_attachment=as_attachment, filename=filename)  # noqa: SIM115
    response["Cache-Control"] = "private, no-store"
    disposition = "attachment" if as_attachment else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'"
    return response


@permission_required("results.view")
@require_POST
def staff_snapshot_review(request, pk):
    snapshot = get_object_or_404(ProctoringSnapshot, pk=pk)
    status = request.POST.get("review_status")
    if status in ProctoringSnapshot.ReviewStatus.values:
        snapshot.review_status = status
        snapshot.save(update_fields=["review_status"])
        audit(request.user, "snapshot.reviewed", f"snapshot:{snapshot.pk}", status)
    return redirect("staff_attempt_review", pk=snapshot.attempt_id)


@permission_required("results.export")
def staff_export_csv(request, pk):
    expire_due_attempts()
    exam = get_object_or_404(Exam, pk=pk)
    selected_ids = request.GET.getlist("selected_ids") or request.POST.getlist("selected_ids")
    if not selected_ids and request.GET.get("ids"):
        selected_ids = [i.strip() for i in request.GET.get("ids").split(",") if i.strip()]

    response = HttpResponse(content_type="text/csv")
    exam_slug = slugify(exam.title) or f"exam-{exam.pk}"
    filename = f"exam-{exam.pk}-{exam_slug}-selected-results.csv" if selected_ids else f"exam-{exam.pk}-{exam_slug}-results.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    fieldnames = [
        "full_name", "username", "email", "phone", "application_status", "attempt_status",
        "started_at", "ended_at", "mcq_score", "coding_score", "total_score", "flagged_for_review",
    ]
    writer = csv.DictWriter(response, fieldnames=fieldnames)
    writer.writeheader()
    for row in attempt_csv_rows(exam, application_ids=selected_ids if selected_ids else None):
        writer.writerow({key: csv_safe(row.get(key, "")) for key in fieldnames})
    audit(request.user, "export.results", f"exam:{exam.pk}", f"csv selected={len(selected_ids) if selected_ids else 'all'}")
    return response


@permission_required("candidates.manage")
def staff_candidates_sample_csv(request, pk):
    get_object_or_404(Exam, pk=pk)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="candidates-import-template.csv"'
    writer = csv.writer(response)
    writer.writerow(["email", "full_name", "phone"])
    writer.writerow(["alex.morgan@example.com", "Alex Morgan", "+1 555-019-2831"])
    writer.writerow(["priya.sharma@example.com", "Priya Sharma", "+91 98765 43210"])
    writer.writerow(["jordan.lee@example.com", "Jordan Lee", "+44 7700 900123"])
    return response


@permission_required("audit.view")
def staff_audit_log(request):
    entries = AuditLog.objects.select_related("actor")[:500]
    return render(request, "assessments/staff/audit_log.html", {"entries": entries})


@any_permission_required("candidates.view", "results.view")
def staff_application_resume(request, pk):
    application = get_object_or_404(Application.objects.select_related("candidate"), pk=pk)

    if not application.resume or not application.resume.name:
        raise Http404("No resume on file.")

    try:
        if not os.path.exists(application.resume.path):
            raise Http404("Resume file not found.")
    except (NotImplementedError, AttributeError, OSError) as exc:
        raise Http404("Resume file not accessible.") from exc

    slug_name = slugify(application.full_name or application.candidate.username or f"candidate_{application.pk}").replace("-", "_")
    ext = os.path.splitext(application.resume.name)[1].lower() or ".pdf"
    download_filename = f"Resume_{slug_name}_{application.pk}{ext}"

    as_attachment = request.GET.get("download") == "1"
    content_type, _ = mimetypes.guess_type(download_filename)
    content_type = content_type or "application/pdf"

    try:
        file_path = application.resume.path
        if os.path.exists(file_path):
            file_obj = open(file_path, "rb")  # noqa: SIM115
        else:
            file_obj = application.resume.open("rb")
    except (OSError, FileNotFoundError, NotImplementedError, AttributeError) as exc:
        raise Http404("Unable to read resume.") from exc

    response = FileResponse(file_obj, as_attachment=as_attachment, filename=download_filename, content_type=content_type)
    disposition = "attachment" if as_attachment else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{download_filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'"
    response["Cache-Control"] = "private, no-store"
    audit(request.user, "resume.downloaded" if as_attachment else "resume.viewed", f"application:{application.pk}", download_filename)
    return response


@permission_required("interviews.conduct")
def staff_generate_ai_questions(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    attempt = get_object_or_404(Attempt.objects.select_related("application", "candidate", "exam"), pk=pk)
    application = attempt.application

    res = generate_interview_questions_with_gemini(
        application=application,
        force=True,
    )

    if res.get("success"):
        engine = res.get("engine", "Gemini")
        count = res.get("count", 0)
        messages.success(request, f"Generated {count} interview questions sorted by Easy, Intermediate, and Hard based on resume keywords.")
        audit(request.user, "ai_interview.generated", f"attempt:{attempt.pk}", f"count={count}, engine={engine}")
    else:
        err = res.get("error", "Unknown error")
        messages.warning(request, f"Notice: {err}")
        audit(request.user, "ai_interview.disabled", f"attempt:{attempt.pk}", f"error={err}")

    redirect_url = reverse("staff_attempt_review", args=[attempt.pk]) + "#interview-section"
    return redirect(redirect_url)


