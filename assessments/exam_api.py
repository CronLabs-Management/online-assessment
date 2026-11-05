"""Candidate-facing views and JSON endpoints. All logic lives in services.py."""

import json
import math
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .models import Application, Attempt, CodingSubmission, Exam, ProctoringEvent
from .services import AttemptError


def _is_reviewer(user):
    return user.is_authenticated and (
        user.is_staff or user.groups.filter(name=getattr(settings, "REVIEWER_GROUP", "Reviewers")).exists()
    )

ERROR_STATUS = {
    "not_found": 404,
    "not_allowed": 403,
    "not_approved": 403,
    "no_application": 403,
    "not_open": 409,
    "readiness_required": 409,
    "no_open_section": 409,
    "wrong_section": 409,
    "attempt_ended": 409,
    "rate_limited": 429,
    "consent_required": 409,
    "snapshots_disabled": 409,
    "too_large": 413,
    "bad_image": 415,
    "session_mismatch": 409,
}


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def _perform(request, pk, action):
    """Runs a candidate action bound to the browser session that made the request."""
    if not request.session.session_key:
        request.session.save()
    result = services.perform(
        request.user, pk, action, session_key=request.session.session_key
    )
    try:
        att = Attempt.objects.filter(pk=pk).only("ip_address", "user_agent").first()
        if att and (not att.ip_address or not att.user_agent):
            update_fields = []
            if not att.ip_address:
                att.ip_address = _client_ip(request)
                if att.ip_address:
                    update_fields.append("ip_address")
            if not att.user_agent:
                att.user_agent = request.META.get("HTTP_USER_AGENT", "")[:1000]
                if att.user_agent:
                    update_fields.append("user_agent")
            if update_fields:
                att.save(update_fields=update_fields)
    except Exception:
        pass
    return result


def api_view(view):
    """Wrap a JSON endpoint: login required, rule errors become JSON responses."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Please log in again.", "code": "unauthenticated"}, status=401)
        if request.user.is_staff:
            return JsonResponse({"error": "Staff accounts cannot take assessments.", "code": "not_allowed"}, status=403)
        try:
            return view(request, *args, **kwargs)
        except AttemptError as exc:
            return JsonResponse(
                {"error": str(exc), "code": exc.code},
                status=ERROR_STATUS.get(exc.code, 400),
            )

    return wrapper


MAX_CONTEXT_KEYS = 20
MAX_CONTEXT_STRING = 200
MAX_EVENT_DURATION_MS = 60 * 60 * 1000


def _reject_constant(name):
    # JSON NaN and Infinity are not valid input for any field.
    raise ValueError(f"'{name}' is not allowed")


def _clean_context(raw):
    """Keep a small, bounded set of scalar values from the browser's event context."""
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key, value in list(raw.items())[:MAX_CONTEXT_KEYS]:
        key = str(key)[:50]
        if isinstance(value, str):
            cleaned[key] = value[:MAX_CONTEXT_STRING]
        elif value is None or isinstance(value, bool):
            cleaned[key] = value
        elif isinstance(value, (int, float)):
            cleaned[key] = value if not isinstance(value, float) or math.isfinite(value) else None
    return cleaned


def _body(request):
    try:
        data = json.loads(request.body or b"{}", parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise AttemptError("Request body must be valid JSON.", "bad_request") from exc
    if not isinstance(data, dict):
        raise AttemptError("Request body must be a JSON object.", "bad_request")
    return data


def _int(data, key):
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AttemptError(f"'{key}' must be a whole number.", "bad_request")
    return value


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@login_required
@require_POST
def start_attempt_view(request, pk):
    if request.user.is_staff:
        messages.error(request, "Staff accounts cannot take assessments.")
        return redirect("home")
    exam = get_object_or_404(Exam, pk=pk, is_published=True)
    application = Application.objects.filter(candidate=request.user, exam=exam).first()
    if application is None:
        messages.error(request, "You are not registered or invited for this assessment.")
        return redirect("home")
    if application.status != Application.Status.APPROVED:
        messages.error(request, "Your application has not been approved for this assessment.")
        return redirect("home")
    if application.details_confirmed_at is None:
        messages.info(request, "Please verify your candidate details and upload your resume before starting the assessment.")
        return redirect("confirm_details", pk=exam.pk)
    try:
        attempt = services.start_attempt(request.user, exam)
        if attempt.readiness_confirmed_at is None:
            services.record_readiness(attempt, timezone.now(), snapshot_consent=exam.snapshots_enabled)
        loc_raw = request.POST.get("location")
        net_raw = request.POST.get("network")
        if loc_raw or net_raw:
            dev = dict(attempt.device_info or {})
            if loc_raw:
                try:
                    dev["location"] = json.loads(loc_raw)
                except Exception:
                    pass
            if net_raw:
                try:
                    dev["network"] = json.loads(net_raw)
                except Exception:
                    pass
            update_fields = ["device_info"]
            attempt.device_info = dev
            if not attempt.ip_address:
                attempt.ip_address = _client_ip(request)
                if attempt.ip_address:
                    update_fields.append("ip_address")
            attempt.save(update_fields=update_fields)
    except AttemptError as exc:
        messages.error(request, str(exc))
        return redirect("exam_detail", pk=exam.pk)
    return redirect("attempt_page", pk=attempt.pk)


@login_required
@require_GET
def attempt_page(request, pk):
    if request.user.is_staff:
        return redirect("staff_exam_list")
    attempt = get_object_or_404(Attempt.objects.select_related("exam"), pk=pk, candidate=request.user)
    if attempt.is_final:
        return redirect("attempt_result", pk=attempt.pk)
    return render(request, "assessments/attempt.html", {"attempt": attempt, "exam": attempt.exam})


@login_required
@require_GET
def attempt_result(request, pk):
    if request.user.is_staff:
        return redirect("staff_exam_list")
    attempt = get_object_or_404(Attempt.objects.select_related("exam"), pk=pk, candidate=request.user)
    if not attempt.is_final:
        return redirect("attempt_page", pk=attempt.pk)
    summary = services._result_summary(attempt)
    return render(
        request,
        "assessments/attempt_result.html",
        {"attempt": attempt, "exam": attempt.exam, "summary": summary, "user": request.user},
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@api_view
@require_GET
def api_state(request, pk):
    state = _perform(request, pk, lambda attempt, now: services.candidate_state(attempt, now))
    return JsonResponse(state)


@api_view
@require_POST
def api_readiness(request, pk):
    data = _body(request)
    consent = data.get("snapshot_consent") is True
    device = data.get("device")
    location = data.get("location")
    network = data.get("network")
    attempt = _perform(
        request, pk, lambda a, now: services.record_readiness(a, now, snapshot_consent=consent)
    )
    clean_dev = {}
    if isinstance(device, dict):
        clean_dev.update({
            k: device[k]
            for k in ("screen", "viewport", "platform", "language", "pixelRatio", "colorDepth")
            if k in device
        })
    if isinstance(location, dict):
        clean_dev["location"] = {
            k: location[k]
            for k in ("latitude", "longitude", "accuracy", "altitude", "timestamp")
            if k in location and isinstance(location[k], (int, float, str))
        }
    if isinstance(network, dict):
        clean_dev["network"] = {
            k: network[k]
            for k in ("type", "effectiveType", "downlink", "rtt", "saveData", "connectionType")
            if k in network and isinstance(network[k], (int, float, str, bool))
        }
    if clean_dev:
        att = Attempt.objects.filter(pk=attempt.pk).first()
        if att:
            merged = dict(att.device_info or {})
            merged.update(clean_dev)
            att.device_info = merged
            update_fields = ["device_info"]
            if not att.ip_address:
                att.ip_address = _client_ip(request)
                if att.ip_address:
                    update_fields.append("ip_address")
            att.save(update_fields=update_fields)
    return JsonResponse({"ok": True, "readiness_confirmed_at": attempt.readiness_confirmed_at.isoformat()})


@api_view
@require_POST
def api_visit(request, pk):
    data = _body(request)
    kind = data.get("kind")
    if kind not in ("mcq", "coding"):
        raise AttemptError("'kind' must be 'mcq' or 'coding'.", "bad_request")
    item_id = _int(data, "item_id")
    visit = _perform(request, pk, lambda a, now: services.open_visit(a, now, kind, item_id))
    return JsonResponse({"ok": True, "visit_number": visit.visit_number})


@api_view
@require_http_methods(["PUT"])
def api_answer(request, pk):
    data = _body(request)
    question_id = _int(data, "question_id")
    option_id = _int(data, "option_id")
    answer = _perform(request, pk, lambda a, now: services.save_answer(a, now, question_id, option_id))
    return JsonResponse({"ok": True, "option_id": answer.option_id})


@api_view
@require_http_methods(["PUT"])
def api_draft(request, pk):
    data = _body(request)
    problem_id = _int(data, "problem_id")
    language = str(data.get("language", ""))[:20]
    code = str(data.get("code", ""))
    _perform(request, pk, lambda a, now: services.save_draft(a, now, problem_id, language, code))
    return JsonResponse({"ok": True})


@api_view
@require_POST
def api_code_submit(request, pk):
    data = _body(request)
    problem_id = _int(data, "problem_id")
    language = str(data.get("language", ""))[:20]
    code = str(data.get("code", ""))
    try:
        submission = _perform(request, pk, lambda a, now: services.submit_code(a, now, problem_id, language, code))
        services.dispatch_grading(submission.pk)
    except services.AttemptError as err:
        if err.code == "locked":
            attempt = get_object_or_404(Attempt, pk=pk, candidate=request.user)
            submission = attempt.coding_submissions.filter(problem_id=problem_id, is_final=True).order_by("-version").first()
            if submission:
                if submission.status == CodingSubmission.Status.QUEUED:
                    services.dispatch_grading(submission.pk)
                return JsonResponse({
                    "ok": True,
                    "submission_id": submission.pk,
                    "status": submission.status,
                    "submission": services._public_submission_summary(submission),
                })
        raise
    return JsonResponse({
        "ok": True,
        "submission_id": submission.pk,
        "status": submission.status,
        "submission": services._public_submission_summary(submission),
    })


@api_view
@require_GET
def api_submission_status(request, pk, submission_id):
    """
    Authenticated, ownership-checked endpoint to check background grading status.
    Returns status: queued, completed, runner_error.
    Never exposes hidden test details, outputs, or marks to the candidate.
    """
    attempt = get_object_or_404(Attempt, pk=pk)
    if attempt.candidate_id != request.user.pk and not (request.user.is_staff or _is_reviewer(request.user)):
        return JsonResponse({"ok": False, "error": "Permission denied.", "code": "forbidden"}, status=403)
    submission = get_object_or_404(CodingSubmission, pk=submission_id, attempt=attempt)
    return JsonResponse({
        "ok": True,
        "submission_id": submission.pk,
        "status": submission.status,
        "submitted_at": submission.submitted_at.isoformat(),
        "final": submission.is_final,
    })


@api_view
@require_POST
def api_code_run(request, pk):
    """Runs code against every case without saving it. Nothing is stored and no score changes."""
    data = _body(request)
    problem_id = _int(data, "problem_id")
    language = str(data.get("language", ""))[:20]
    code = str(data.get("code", ""))
    # Same checks as every candidate action (time rules, approval, session). The sandbox call itself
    # runs after the lock is released, so a slow run never holds up the attempt.
    problem = _perform(request, pk, lambda a, now: services.run_preflight(a, now, problem_id, language, code))
    return JsonResponse({"ok": True, **services.run_results(problem, language, code)})


@api_view
@require_POST
def api_submit_section(request, pk):
    data = _body(request)
    section_id = _int(data, "section_id")
    _perform(request, pk, lambda a, now: services.submit_section(a, now, section_id))
    return JsonResponse({"ok": True})


CLIENT_ALLOWED_EVENT_KINDS = {
    ProctoringEvent.Kind.TAB_HIDDEN,
    ProctoringEvent.Kind.WINDOW_BLUR,
    ProctoringEvent.Kind.FACE_MISSING,
    ProctoringEvent.Kind.MULTIPLE_FACES,
    ProctoringEvent.Kind.HEAD_TURN,
    ProctoringEvent.Kind.CAMERA_LOST,
    ProctoringEvent.Kind.CAMERA_REVOKED,
    ProctoringEvent.Kind.AI_UNAVAILABLE,
    ProctoringEvent.Kind.FULLSCREEN_EXIT,
    ProctoringEvent.Kind.CLIPBOARD,
    ProctoringEvent.Kind.DEVTOOLS_ATTEMPT,
}


@api_view
@require_POST
def api_event(request, pk):
    limit = _rate_limit_key(request, pk, "event", services_limit=120)
    if limit:
        raise AttemptError("Too many events. Slow down.", "rate_limited")
    data = _body(request)
    kind = data.get("kind")
    if kind not in CLIENT_ALLOWED_EVENT_KINDS:
        raise AttemptError("Unknown or forbidden client event kind.", "bad_request")
    duration_ms = data.get("duration_ms", 0)
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
        raise AttemptError("'duration_ms' must be a number.", "bad_request")
    if isinstance(duration_ms, float) and not math.isfinite(duration_ms):
        raise AttemptError("'duration_ms' must be a finite number.", "bad_request")
    duration_ms = min(max(int(duration_ms), 0), MAX_EVENT_DURATION_MS)
    context = _clean_context(data.get("context"))
    client_at = _parse_client_time(data.get("client_at"))
    event = _perform(request, pk,
        lambda a, now: services.record_event(a, now, kind, client_at, duration_ms, context),
    )
    attempt = Attempt.objects.get(pk=pk)
    switches = attempt.proctoring_events.filter(kind__in=services.SWITCH_KINDS).count()
    missing_count = attempt.proctoring_events.filter(kind=ProctoringEvent.Kind.FACE_MISSING).count()
    return JsonResponse({
        "ok": True,
        "event_id": event.pk,
        "over_threshold": event.over_threshold,
        "kind": kind,
        "switch_count": switches,
        "switch_limit": attempt.policy_snapshot.get("tab_switch_threshold", 3),
        "missing_count": missing_count,
        "missing_limit_seconds": attempt.policy_snapshot.get("missing_face_seconds", 5),
        "flagged": attempt.flagged_for_review,
    })


@api_view
@require_POST
def api_snapshot(request, pk):
    uploaded = request.FILES.get("image")
    if uploaded is None:
        raise AttemptError("No image was uploaded.", "bad_request")
    event_id = request.POST.get("event_id") or None
    if event_id is not None:
        try:
            event_id = int(event_id)
        except ValueError as exc:
            raise AttemptError("'event_id' must be a number.", "bad_request") from exc
    client_at = _parse_client_time(request.POST.get("client_at"))
    snapshot = _perform(request, pk,
        lambda a, now: services.store_snapshot(a, now, uploaded, event_id=event_id, client_at=client_at),
    )
    return JsonResponse({"ok": True, "snapshot_id": snapshot.pk})


def _parse_client_time(value):
    from datetime import datetime
    from datetime import timezone as dt_timezone

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _rate_limit_key(request, pk, name, services_limit):
    """True when the caller is over the per-minute limit for this action."""
    from django.conf import settings as dj_settings

    from . import services as rate_services

    limit = getattr(dj_settings, "EVENT_RATE_LIMIT_PER_MIN", services_limit)
    return rate_services.rate_limited(f"{name}:{pk}:{request.user.pk}", limit, 60)
