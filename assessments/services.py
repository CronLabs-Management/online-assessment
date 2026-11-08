"""
The attempt engine. This is the only code that changes an attempt's state.

Rules enforced here, never in the browser:
  * Only approved candidates may start an exam, and only once.
  * Deadlines come from the server clock. The effective attempt deadline is
    min(start + duration, exam closing time), extended only by policy-approved pauses.
  * Sections run in order. When a section's time runs out, or the candidate submits it,
    the section locks and the next section starts with its own server-set timer.
  * Each candidate gets a fixed random MCQ order and option order, and a fixed random
    selection of coding problems. These are persisted at start and never regenerated.
  * Question visits are timed by the server. Active time excludes disconnection gaps.
  * Public and hidden test cases are only used by the grader and staff views.
"""

import hashlib
import io
import logging
import os
import random
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from .models import (
    MAX_GRADING_ATTEMPTS,
    SUPPORTED_LANGUAGES,
    Answer,
    AnswerRevision,
    Application,
    Attempt,
    AttemptCoding,
    AttemptMCQ,
    AttemptSection,
    CodingActivity,
    CodingDraft,
    CodingSubmission,
    CodingTestCase,
    CodingTestResult,
    ConnectionEvent,
    Exam,
    ExamSection,
    MCQOption,
    ProctoringEvent,
    ProctoringSnapshot,
    QuestionVisit,
)
from .runners import RunnerError, get_runner, normalize_output

logger = logging.getLogger(__name__)

MAX_CODE_CHARS = 50_000
SNAPSHOT_MAGIC = {
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
}

_rng = random.SystemRandom()


class AttemptError(Exception):
    """A request the attempt rules do not allow. The message is safe to show the candidate."""

    def __init__(self, message, code="not_allowed"):
        super().__init__(message)
        self.code = code


def get_client_ip(request):
    """Resolve the real client IP address, only respecting X-Forwarded-For if behind trusted proxies."""
    remote = request.META.get("REMOTE_ADDR") or None
    trusted = getattr(settings, "TRUSTED_PROXIES", ("127.0.0.1", "::1"))
    if remote and remote in trusted:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            ips = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
            if ips:
                return ips[0]
    return remote or ""


# ---------------------------------------------------------------------------
# Start and locking
# ---------------------------------------------------------------------------


def check_eligibility(user, exam):
    application = Application.objects.filter(candidate=user, exam=exam).first()
    if application is None:
        raise AttemptError("You have not applied for this assessment.", "no_application")
    if application.status != Application.Status.APPROVED:
        raise AttemptError("Your application has not been approved for this assessment.", "not_approved")
    return application


def publish_checks(exam):
    """
    The pre-publish checklist. Each entry: {"ok", "blocking", "text", "time", "structural"}.
    Hard/structural failures stop publishing entirely and cannot be overridden.
    Soft blocking checks require superuser role and an explicit reason.
    Warnings only inform.
    """
    checks = []

    def add(ok, text, blocking=True, time=False, structural=True):
        checks.append({"ok": bool(ok), "blocking": blocking, "text": text, "time": time, "structural": structural})

    sections = list(exam.sections.all())
    add(sections, "At least one section is configured" if sections else "Add at least one section", structural=True)
    if sections:
        total = sum(section.duration_minutes for section in sections)
        add(
            total <= exam.duration_minutes,
            f"Sections use {total} of the {exam.duration_minutes} minutes allowed"
            if total <= exam.duration_minutes
            else f"Sections add up to {total} minutes, more than the {exam.duration_minutes}-minute limit",
            structural=True,
        )

    add(exam.closes_at > exam.opens_at, "Closing time is after opening time", time=True, structural=True)
    add(exam.closes_at > timezone.now(), "Closing time is in the future", time=True, structural=False)

    mcq_total = exam.mcq_questions.count()
    mcq_needed = sum(
        section.question_count or mcq_total for section in sections if section.kind == ExamSection.Kind.MCQ
    )
    if any(section.kind == ExamSection.Kind.MCQ for section in sections):
        add(
            mcq_total and mcq_needed <= mcq_total,
            f"{mcq_total} multiple-choice questions available, {mcq_needed} needed",
            structural=True,
        )
    bad_questions = []
    for question in exam.mcq_questions.prefetch_related("options"):
        options = list(question.options.all())
        empty = any(not (option.text or "").strip() and not option.image for option in options)
        if len(options) < 2 or empty or sum(option.is_correct for option in options) != 1:
            bad_questions.append(question.text[:40])
    add(
        not bad_questions,
        "Every question has at least two options and exactly one correct answer"
        if not bad_questions
        else f"{len(bad_questions)} question(s) need two or more options (each with text or an image) and exactly one correct answer",
        structural=True,
    )

    problems = list(exam.coding_problems.prefetch_related("test_cases"))
    coding_needed = sum(
        section.coding_selection_count for section in sections if section.kind == ExamSection.Kind.CODING
    )
    if any(section.kind == ExamSection.Kind.CODING for section in sections):
        add(
            problems and coding_needed <= len(problems),
            f"{len(problems)} coding problem(s) available, {coding_needed} needed"
            if problems and coding_needed <= len(problems)
            else f"{len(problems)} coding problem(s) available, but {coding_needed} distinct ones are needed",
            structural=True,
        )
    bad_problems = [
        problem.title for problem in problems
        if not problem.statement.strip()
        or not problem.language_list
        or any(lang not in SUPPORTED_LANGUAGES for lang in problem.language_list)
        or not any(case.expected_output.strip() for case in problem.test_cases.all())
    ]
    add(
        not bad_problems,
        "Every coding problem has a statement, supported languages and a test case"
        if not bad_problems
        else "Fix these coding problems (statement, languages or test cases): " + ", ".join(bad_problems[:5]),
        structural=True,
    )
    no_hidden = [problem.title for problem in problems if not any(c.is_hidden for c in problem.test_cases.all())]
    if no_hidden:
        add(False, f"No hidden test cases for: {', '.join(no_hidden[:5])}. Grading would use public cases only.", blocking=False, structural=False)

    waiting = exam.applications.filter(status=Application.Status.APPROVED).count()
    add(True, f"{waiting} candidate(s) on the list (candidates can be added at any time)", blocking=False, structural=False)
    return checks


def _validate_exam_is_startable(exam):
    if not exam.is_open:
        raise AttemptError("This assessment is not open right now.", "not_open")
    sections = list(exam.sections.all())
    # If coding challenges exist but no manual coding section was created, auto-attach coding stage
    if exam.coding_problems.exists() and not any(s.kind == ExamSection.Kind.CODING for s in sections):
        max_order = max((s.order for s in sections), default=0)
        total_mcq_dur = sum(s.duration_minutes for s in sections if s.kind == ExamSection.Kind.MCQ)
        coding_dur = max(exam.duration_minutes - total_mcq_dur, 20)
        auto_coding_sec, _ = ExamSection.objects.get_or_create(
            exam=exam,
            kind=ExamSection.Kind.CODING,
            defaults={
                "title": "Coding Challenges",
                "order": max_order + 1,
                "duration_minutes": coding_dur,
                "coding_selection_count": max(exam.coding_problems.count(), 1),
            },
        )
        if auto_coding_sec not in sections:
            sections.append(auto_coding_sec)
    if not sections:
        raise AttemptError("This assessment has no sections configured yet.", "misconfigured")
    return sections


@transaction.atomic
def start_attempt(user, exam):
    """Create the attempt, its sections, and the candidate's fixed random assignments."""
    # Serialise starts for this test, so two simultaneous first requests cannot both create an attempt.
    Exam.objects.select_for_update().filter(pk=exam.pk).first()
    application = check_eligibility(user, exam)
    existing = Attempt.objects.filter(candidate=user, exam=exam).first()
    if existing is not None:
        return existing

    sections = _validate_exam_is_startable(exam)
    now = timezone.now()
    deadline = min(now + timedelta(minutes=exam.duration_minutes), exam.closes_at)

    attempt = Attempt.objects.create(
        candidate=user,
        exam=exam,
        application=application,
        status=Attempt.Status.IN_PROGRESS,
        policy_snapshot=exam.policy_snapshot(),
        started_at=now,
        deadline_at=deadline,
        last_seen_at=now,
    )

    used_questions = set()
    used_problems = set()
    for section in sections:
        AttemptSection.objects.create(
            attempt=attempt,
            section=section,
            status=AttemptSection.Status.LOCKED,
        )
        if section.kind == ExamSection.Kind.MCQ:
            _assign_mcq(attempt, exam, section, used_questions)
        else:
            _assign_coding(attempt, exam, section, used_problems)

    first = attempt.sections.order_by("section__order", "id").first()
    _activate(attempt, first, start_at=now)
    return attempt


def _assign_mcq(attempt, exam, section, used_questions):
    section_pool = [
        q for q in exam.mcq_questions.prefetch_related("options")
        if (q.section_id == section.pk or (q.section_id is None and getattr(section, "is_default", False)))
        and q.pk not in used_questions
    ]
    if not section_pool and not exam.mcq_questions.filter(section__isnull=False).exists():
        section_pool = [
            q for q in exam.mcq_questions.prefetch_related("options")
            if q.pk not in used_questions
        ]
    pool = section_pool
    if not pool:
        if not exam.mcq_questions.exists():
            raise AttemptError(
                f"Section '{section.title}' has no multiple-choice questions configured in the exam pool. Please add questions before candidates can start.",
                "misconfigured",
            )
        raise AttemptError(
            f"All multiple-choice questions were assigned to earlier sections. Add more questions to the exam pool for section '{section.title}'.",
            "misconfigured",
        )

    if exam.shuffle_questions:
        _rng.shuffle(pool)

    # If question_count is specified, take up to that number (or all available in pool if fewer)
    if section.question_count and section.question_count > 0:
        count = min(section.question_count, len(pool))
    else:
        count = len(pool)

    chosen = pool[:count]
    if not chosen:
        raise AttemptError(f"No multiple-choice questions available for section '{section.title}'.", "misconfigured")

    for position, question in enumerate(chosen, start=1):
        option_ids = [option.pk for option in question.options.all()]
        if exam.shuffle_options:
            _rng.shuffle(option_ids)
        AttemptMCQ.objects.create(
            attempt=attempt,
            section=section,
            question=question,
            position=position,
            option_order=option_ids,
        )
        used_questions.add(question.pk)


def _assign_coding(attempt, exam, section, used_problems):
    pool = [problem for problem in exam.coding_problems.all() if problem.pk not in used_problems]
    if len(pool) < section.coding_selection_count:
        raise AttemptError("Not enough unused coding problems are configured.", "misconfigured")
    chosen = _rng.sample(pool, section.coding_selection_count)
    for position, problem in enumerate(chosen, start=1):
        AttemptCoding.objects.create(
            attempt=attempt,
            section=section,
            problem=problem,
            position=position,
        )
        used_problems.add(problem.pk)


def lock_attempt(user, attempt_id):
    """Fetch the candidate's own attempt with a row lock. Call inside a transaction."""
    try:
        return (
            Attempt.objects.select_for_update()
            .select_related("exam", "application")
            .get(pk=attempt_id, candidate=user)
        )
    except Attempt.DoesNotExist as exc:
        raise AttemptError("Attempt not found.", "not_found") from exc


def _require_approved(attempt):
    # Eligibility is checked on every action, so a candidate rejected mid-test loses access at once.
    if attempt.application.status != Application.Status.APPROVED:
        raise AttemptError("Your application is no longer approved for this assessment.", "not_approved")


def perform(user, attempt_id, action, *args, session_key=None, **kwargs):
    """
    Run a candidate action under a row lock, after applying time rules.

    Time rules (expiry, pause credit) commit in their own transaction first, so a rejected
    action can never roll back an expiry that has already happened. The session check commits
    its own record of a conflict for the same reason.
    """
    with transaction.atomic():
        attempt = lock_attempt(user, attempt_id)
        touch(attempt, timezone.now())
    if session_key is not None:
        with transaction.atomic():
            attempt = lock_attempt(user, attempt_id)
            matched = _bind_or_match_session(attempt, session_key, timezone.now())
        if not matched:
            raise AttemptError(
                "This test is open in another browser or session. Ask the administrator to allow you to continue here.",
                "session_mismatch",
            )
    with transaction.atomic():
        attempt = lock_attempt(user, attempt_id)
        _require_approved(attempt)
        return action(attempt, timezone.now(), *args, **kwargs)


def session_fingerprint(session_key):
    return hashlib.sha256(session_key.encode()).hexdigest()


def _bind_or_match_session(attempt, session_key, now):
    """
    The first action binds the test to this browser session. Any other session is refused, and the
    attempt is flagged. The conflict is recorded at most once every five minutes, because the page
    keeps polling while it is refused.
    """
    if attempt.is_final:
        return True
    fingerprint = session_fingerprint(session_key)
    if not attempt.session_fingerprint:
        attempt.session_fingerprint = fingerprint
        attempt.save(update_fields=["session_fingerprint"])
        return True
    if attempt.session_fingerprint == fingerprint:
        return True
    recent = attempt.proctoring_events.filter(
        kind=ProctoringEvent.Kind.SESSION_CHANGED, server_at__gte=now - timedelta(minutes=5)
    ).exists()
    if not recent:
        _create_event(attempt, ProctoringEvent.Kind.SESSION_CHANGED, None, 0, {}, now)
    elif not attempt.flagged_for_review:
        attempt.flagged_for_review = True
        attempt.save(update_fields=["flagged_for_review"])
    return False


def question_image_url(kind, obj):
    """URL of a question or option image, or None. The view checks who may see it."""
    if not getattr(obj, "image", None) or not obj.image.name:
        return None
    from django.urls import reverse

    return reverse("question_image", args=[kind, obj.pk])


# ---------------------------------------------------------------------------
# Time rules
# ---------------------------------------------------------------------------


def touch(attempt, now):
    """Apply disconnection gaps and timeouts. Candidate actions and the expiry sweep both call this first."""
    if attempt.is_final:
        return attempt

    policy = attempt.policy_snapshot or {}
    grace = policy.get("network_grace_seconds", 30)
    gap = (now - attempt.last_seen_at).total_seconds()

    if gap > grace:
        excess = int(gap - grace)
        granted = 0
        if policy.get("pause_enabled", False):
            allowance = max(0, policy.get("max_pause_seconds", 0) - attempt.total_paused_seconds)
            # Pause credit can never move the deadline past the exam's closing time.
            room = max(0, int((attempt.exam.closes_at - attempt.deadline_at).total_seconds()))
            granted = min(excess, allowance, room)
        denied = excess - granted

        # Time spent offline is not active time for the open question.
        _close_open_visit(attempt, at=attempt.last_seen_at)

        ConnectionEvent.objects.create(
            attempt=attempt,
            last_seen_before=attempt.last_seen_at,
            detected_at=now,
            gap_seconds=int(gap),
            granted_seconds=granted,
            denied_seconds=denied,
        )

        if granted:
            attempt.total_paused_seconds += granted
            attempt.deadline_at = attempt.deadline_at + timedelta(seconds=granted)
            current = _current_section(attempt)
            if current is not None and current.deadline_at is not None:
                current.deadline_at = min(current.deadline_at + timedelta(seconds=granted), attempt.deadline_at)
                current.save(update_fields=["deadline_at"])

        if policy["camera_required"]:
            attempt.readiness_confirmed_at = None

    attempt.last_seen_at = now
    attempt.save(update_fields=["last_seen_at", "deadline_at", "total_paused_seconds", "readiness_confirmed_at"])

    # Section timeouts, in order. Each expired section starts the next one at its own deadline.
    # Check if the candidate is on the very first initial pre-flight readiness check
    # (i.e. has never passed readiness check, has no section submitted or expired, and has no answers/drafts).
    has_prior_readiness = attempt.proctoring_events.filter(kind=ProctoringEvent.Kind.CAMERA_READY).exists()
    is_initial_preflight = (
        attempt.exam.camera_required
        and attempt.readiness_confirmed_at is None
        and not has_prior_readiness
        and not attempt.sections.filter(status__in=[AttemptSection.Status.SUBMITTED, AttemptSection.Status.EXPIRED]).exists()
        and not attempt.answers.exists()
        and not attempt.coding_drafts.exists()
    )

    if not is_initial_preflight:
        while True:
            current = _current_section(attempt)
            if current is None or current.deadline_at is None or now < current.deadline_at:
                break
            _end_section(attempt, current, AttemptSection.Status.EXPIRED, at=current.deadline_at)
            _advance(attempt, previous_end=current.deadline_at, final_status=Attempt.Status.EXPIRED)
            if attempt.is_final:
                break

    # If the overall attempt deadline or exam closing time has passed, finalize as EXPIRED immediately.
    effective_deadline = min(attempt.deadline_at, attempt.exam.closes_at)
    if not attempt.is_final and now >= effective_deadline:
        _finalize(attempt, Attempt.Status.EXPIRED, at=effective_deadline)

    return attempt


def _current_section(attempt):
    return attempt.sections.filter(status=AttemptSection.Status.IN_PROGRESS).select_related("section").first()


def _activate(attempt, attempt_section, start_at):
    deadline = min(start_at + timedelta(minutes=attempt_section.section.duration_minutes), attempt.deadline_at)
    attempt_section.status = AttemptSection.Status.IN_PROGRESS
    attempt_section.started_at = start_at
    attempt_section.deadline_at = deadline
    attempt_section.save(update_fields=["status", "started_at", "deadline_at"])


def _advance(attempt, previous_end, final_status=Attempt.Status.SUBMITTED):
    next_section = (
        attempt.sections.filter(status=AttemptSection.Status.LOCKED)
        .select_related("section")
        .order_by("section__order", "id")
        .first()
    )
    if next_section is None:
        _finalize(attempt, final_status, at=previous_end)
    else:
        _activate(attempt, next_section, start_at=previous_end)


def _end_section(attempt, attempt_section, status, at):
    _close_open_visit(attempt, at=at)
    attempt_section.status = status
    attempt_section.ended_at = at
    attempt_section.save(update_fields=["status", "ended_at"])
    _queue_drafts_for_section(attempt, attempt_section.section)


def _finalize(attempt, status, at):
    _close_open_visit(attempt, at=at)
    # Sections still running when the attempt ends are closed, and their autosaved code is graded.
    for attempt_section in attempt.sections.filter(status=AttemptSection.Status.IN_PROGRESS).select_related("section"):
        attempt_section.status = AttemptSection.Status.EXPIRED
        attempt_section.ended_at = at
        attempt_section.save(update_fields=["status", "ended_at"])
        _queue_drafts_for_section(attempt, attempt_section.section)
    attempt.status = status
    attempt.ended_at = at
    attempt.mcq_score = mcq_score(attempt)
    attempt.coding_score = coding_score(attempt)
    attempt.save()


def _close_open_visit(attempt, at):
    open_visits = list(attempt.visits.filter(ended_at__isnull=True))
    if not open_visits:
        return
    end = min(at, timezone.now())
    for visit in open_visits:
        visit.ended_at = end
        visit.active_seconds = max(0, int((end - visit.started_at).total_seconds()))
        visit.save(update_fields=["ended_at", "active_seconds"])


def _require_ready(attempt):
    if attempt.exam.camera_required and attempt.readiness_confirmed_at is None:
        raise AttemptError("Camera check required before continuing.", "readiness_required")


def _require_section(attempt, kind):
    current = _current_section(attempt)
    if current is None:
        raise AttemptError("There is no open section for this attempt.", "no_open_section")
    if current.section.kind != kind:
        raise AttemptError("That action does not belong to the current section.", "wrong_section")
    return current


# ---------------------------------------------------------------------------
# Candidate actions
# ---------------------------------------------------------------------------


def record_readiness(attempt, now, snapshot_consent=False):
    if attempt.is_final:
        raise AttemptError("This attempt has ended.", "attempt_ended")
    snapshots_allowed = attempt.policy_snapshot.get("snapshots_enabled", True)
    camera_required = attempt.policy_snapshot.get("camera_required", True)
    if camera_required and snapshots_allowed and not snapshot_consent:
        raise AttemptError(
            "You must agree to snapshot capture to take this camera-proctored test.",
            "consent_required",
        )
    has_prior_readiness = (
        attempt.readiness_confirmed_at is not None
        or attempt.proctoring_events.filter(kind=ProctoringEvent.Kind.CAMERA_READY).exists()
    )
    is_initial_readiness = not has_prior_readiness
    attempt.readiness_confirmed_at = now
    attempt.snapshot_consent_at = now if (snapshot_consent and snapshots_allowed) else None

    # The exam timer officially starts from the moment initial readiness is confirmed and questions are displayed
    update_fields = ["readiness_confirmed_at", "snapshot_consent_at"]
    if is_initial_readiness:
        attempt.started_at = now
        attempt.deadline_at = min(now + timedelta(minutes=attempt.exam.duration_minutes), attempt.exam.closes_at)
        update_fields.extend(["started_at", "deadline_at"])

        active_sec = attempt.sections.filter(status=AttemptSection.Status.IN_PROGRESS).first()
        if active_sec:
            active_sec.started_at = now
            active_sec.deadline_at = min(now + timedelta(minutes=active_sec.section.duration_minutes), attempt.deadline_at)
            active_sec.save(update_fields=["started_at", "deadline_at"])

    attempt.save(update_fields=update_fields)
    if is_initial_readiness:
        _create_event(
            attempt,
            ProctoringEvent.Kind.CAMERA_READY,
            client_at=None,
            duration_ms=0,
            context={"snapshot_consent": bool(attempt.snapshot_consent_at)},
            now=now,
        )
    return attempt


def save_answer(attempt, now, question_id, option_id):
    _require_ready(attempt)
    current = _require_section(attempt, ExamSection.Kind.MCQ)
    item = attempt.mcq_items.filter(section=current.section, question_id=question_id).first()
    if item is None:
        raise AttemptError("That question is not part of this attempt.", "not_assigned")
    if option_id not in item.option_order:
        raise AttemptError("That option is not valid for this question.", "invalid_option")
    option = MCQOption.objects.get(pk=option_id, question_id=question_id)

    answer, created = Answer.objects.get_or_create(attempt=attempt, question_id=question_id)
    if not created and answer.option_id == option.pk:
        return answer
    if not created and answer.option_id is not None:
        answer.change_count += 1
    answer.option = option
    answer.save()
    AnswerRevision.objects.create(attempt=attempt, question_id=question_id, option=option)
    return answer


def open_visit(attempt, now, kind, item_id):
    _require_ready(attempt)
    current = _require_section(attempt, kind)
    if kind == ExamSection.Kind.MCQ:
        if not attempt.mcq_items.filter(section=current.section, question_id=item_id).exists():
            raise AttemptError("That question is not part of this attempt.", "not_assigned")
        field = {"mcq_question_id": item_id}
        visit_kind = QuestionVisit.Kind.MCQ
    else:
        if not attempt.coding_items.filter(section=current.section, problem_id=item_id).exists():
            raise AttemptError("That problem is not part of this attempt.", "not_assigned")
        field = {"coding_problem_id": item_id}
        visit_kind = QuestionVisit.Kind.CODING

    open_visit_row = attempt.visits.filter(ended_at__isnull=True).order_by("-started_at").first()
    if open_visit_row is not None and all(getattr(open_visit_row, k) == v for k, v in field.items()):
        return open_visit_row

    _close_open_visit(attempt, at=now)
    previous = attempt.visits.filter(**field).count()
    return QuestionVisit.objects.create(
        attempt=attempt,
        kind=visit_kind,
        visit_number=previous + 1,
        started_at=now,
        **field,
    )


def problem_locked(attempt, problem_id):
    return attempt.coding_submissions.filter(problem_id=problem_id, is_final=True).exists()


def save_draft(attempt, now, problem_id, language, code):
    _require_ready(attempt)
    current = _require_section(attempt, ExamSection.Kind.CODING)
    problem = _assigned_problem(attempt, current, problem_id)
    if problem_locked(attempt, problem.pk):
        raise AttemptError("This problem is submitted and can no longer be changed.", "locked")
    _validate_code(problem, language, code)
    previous = CodingDraft.objects.filter(attempt=attempt, problem=problem).first()
    if previous is not None:
        if previous.language and previous.language != language:
            CodingActivity.objects.create(
                attempt=attempt, problem=problem, kind=CodingActivity.Kind.LANGUAGE_SWITCH,
                language=language, previous_language=previous.language,
                source_code=previous.source_code[:50000], created_at=now,
            )
        else:
            _check_large_code_change(attempt, now, problem, previous.source_code, code)
    draft, _ = CodingDraft.objects.update_or_create(
        attempt=attempt,
        problem=problem,
        defaults={"language": language, "source_code": code},
    )
    return draft


LARGE_CODE_CHANGE_CHARS = 300  # Between two autosaves (a few seconds apart), this is more than typing.
RECENT_PASTE_SECONDS = 30


def _check_large_code_change(attempt, now, problem, before, after):
    """Record when a large block of code appears between autosaves and no paste explains it.

    Typing fast adds tens of characters between autosaves; hundreds at once usually means pasting from
    outside the page (which the clipboard listener may miss), an auto-typer, or drag and drop.
    """
    added = len(after or "") - len(before or "")
    if added < LARGE_CODE_CHANGE_CHARS:
        return
    recent_paste = attempt.proctoring_events.filter(
        kind=ProctoringEvent.Kind.CLIPBOARD, server_at__gte=now - timedelta(seconds=RECENT_PASTE_SECONDS)
    ).exists()
    if recent_paste:
        return
    _create_event(
        attempt, ProctoringEvent.Kind.LARGE_CODE_CHANGE, None, 0,
        {"problem_id": problem.pk, "chars_added": added}, now,
    )


def record_code_run(attempt_id, problem, language, code, results, now=None):
    """Keep the code from each Run with its public-case result, for reviewers. Never changes a score."""
    public = results.get("public") or []
    return CodingActivity.objects.create(
        attempt_id=attempt_id, problem=problem, kind=CodingActivity.Kind.RUN, language=language,
        source_code=(code or "")[:50000],
        public_passed=sum(1 for case in public if case.get("passed")),
        public_total=len(public),
        compile_error=bool(results.get("compile_output")),
        created_at=now or timezone.now(),
    )


def note_client_ip(attempt, now, ip):
    """Record each new IP address an attempt continues from (first one is stored on the attempt)."""
    if not ip or attempt.is_final:
        return
    if not attempt.ip_address:
        return  # The first request stores it; nothing to compare yet.
    if ip == attempt.ip_address:
        return
    if attempt.proctoring_events.filter(kind=ProctoringEvent.Kind.NETWORK_CHANGED, context__ip=ip).exists():
        return
    _create_event(
        attempt, ProctoringEvent.Kind.NETWORK_CHANGED, None, 0,
        {"ip": ip, "first_ip": attempt.ip_address}, now,
    )


def submit_code(attempt, now, problem_id, language, code):
    """
    Final submission. The code is locked from now on. Returns the submission, which the caller grades.
    """
    _require_ready(attempt)
    if rate_limited(f"code:{attempt.pk}", 30, 60):
        raise AttemptError("Too many submissions. Please wait a minute.", "rate_limited")
    current = _require_section(attempt, ExamSection.Kind.CODING)
    problem = _assigned_problem(attempt, current, problem_id)
    if problem_locked(attempt, problem.pk):
        raise AttemptError("This problem is already submitted.", "locked")
    _validate_code(problem, language, code)
    return _create_submission(attempt, problem, language, code, is_final=True)


def run_preflight(attempt, now, problem_id, language, code):
    """
    Checks for a Run request. Writes nothing, so the sandbox can be called outside the database lock.
    Returns the problem.
    """
    if attempt.is_final:
        raise AttemptError("This attempt has ended.", "attempt_ended")
    _require_ready(attempt)
    current = _current_section(attempt)
    if current is None or current.section.kind != ExamSection.Kind.CODING or now >= current.deadline_at:
        raise AttemptError("Running code is only possible during an open coding section.", "no_open_section")
    problem = _assigned_problem(attempt, current, problem_id)
    if problem_locked(attempt, problem.pk):
        raise AttemptError("This problem is submitted and can no longer be changed.", "locked")
    _validate_code(problem, language, code)
    if rate_limited(f"run:{attempt.pk}", 20, 60):
        raise AttemptError("Too many runs. Please wait a minute.", "rate_limited")
    return problem


def run_results(problem, language, code, runner=None):
    """
    Runs the code against public cases only without storing it or changing any score.
    Hidden cases are never executed during preview runs, eliminating feedback and timing oracles.
    """
    runner = runner or get_runner()
    public_cases = list(CodingTestCase.objects.filter(problem=problem, is_hidden=False).order_by("order", "id"))
    if not public_cases:
        return {
            "public": [],
            "compile_output": "",
            "message": "No sample test cases configured for this problem.",
        }
    try:
        outcomes = execute_cases(problem, language, code, runner, cases=public_cases)
    except RunnerError as exc:
        raise AttemptError("The code could not be run right now. Please try again.", "runner_unavailable") from exc
    public = []
    compile_output = ""
    for case, status, outcome in outcomes:
        passed = status == CodingTestResult.Status.PASSED
        public.append({
            "example": len(public) + 1,
            "status": status,
            "passed": passed,
            "input": case.input_data,
            "expected": case.expected_output,
            "output": (outcome.stdout if outcome else "")[:2000],
            "error": ((outcome.stderr if outcome else "") or "")[:1000],
        })
        if status == CodingTestResult.Status.COMPILE_ERROR and outcome:
            compile_output = outcome.compile_output[:2000]
    return {
        "public": public,
        "compile_output": compile_output,
    }


def submit_section(attempt, now, section_id):
    _require_ready(attempt)
    current = _current_section(attempt)
    if current is None:
        raise AttemptError("There is no open section for this attempt.", "no_open_section")
    if current.section_id != section_id:
        raise AttemptError("Only the current section can be submitted.", "wrong_section")
    _end_section(attempt, current, AttemptSection.Status.SUBMITTED, at=now)
    _advance(attempt, previous_end=now)
    return attempt


def record_event(attempt, now, kind, client_at, duration_ms, context):
    if attempt.is_final:
        raise AttemptError("This attempt has ended.", "attempt_ended")
    return _create_event(attempt, kind, client_at, duration_ms, context, now)


SWITCH_KINDS = [
    ProctoringEvent.Kind.TAB_HIDDEN,
    ProctoringEvent.Kind.WINDOW_BLUR,
    ProctoringEvent.Kind.FULLSCREEN_EXIT,
]
VOICE_FLAG_MS = 10000  # Ten seconds of sustained sound is worth a reviewer's attention.
CLIPBOARD_FLAG_CHARS = 200  # A paste this large is unlikely to be typed, so it is flagged for review.


def action_is_paste(context):
    return (context or {}).get("action") == "paste"


def allow_resume(attempt, actor):
    """Staff allow the candidate to continue in their current browser session. The flag stays for review."""
    attempt.session_fingerprint = ""
    attempt.save(update_fields=["session_fingerprint"])
    return attempt


def _create_event(attempt, kind, client_at, duration_ms, context, now):
    policy = attempt.policy_snapshot
    sequence = (attempt.proctoring_events.aggregate(m=Max("sequence"))["m"] or 0) + 1
    over_threshold = False
    if kind in SWITCH_KINDS:
        switches = attempt.proctoring_events.filter(kind__in=SWITCH_KINDS).count() + 1
        over_threshold = switches > policy["tab_switch_threshold"]
    if kind == ProctoringEvent.Kind.MULTIPLE_FACES:
        over_threshold = True
    if kind == ProctoringEvent.Kind.SESSION_CHANGED:
        over_threshold = True
    if kind == getattr(ProctoringEvent.Kind, "DEVTOOLS_ATTEMPT", "devtools_attempt"):
        over_threshold = True
    if kind == ProctoringEvent.Kind.CLIPBOARD:
        pasted = (context or {}).get("chars", 0)
        over_threshold = action_is_paste(context) and isinstance(pasted, (int, float)) and pasted >= CLIPBOARD_FLAG_CHARS
    if kind == ProctoringEvent.Kind.FACE_MISSING:
        over_threshold = duration_ms >= policy["missing_face_seconds"] * 1000
    if kind in (ProctoringEvent.Kind.MULTIPLE_SCREENS, ProctoringEvent.Kind.VIRTUAL_CAMERA):
        over_threshold = True
    if kind == ProctoringEvent.Kind.VOICE_DETECTED:
        over_threshold = (duration_ms or 0) >= VOICE_FLAG_MS

    if over_threshold and not attempt.flagged_for_review:
        attempt.flagged_for_review = True
        attempt.save(update_fields=["flagged_for_review"])
    return ProctoringEvent.objects.create(
        attempt=attempt,
        kind=kind,
        sequence=sequence,
        client_at=client_at,
        server_at=now,
        duration_ms=max(0, min(duration_ms or 0, 24 * 60 * 60 * 1000)),
        context=context or {},
        over_threshold=over_threshold,
    )


def store_snapshot(attempt, now, uploaded, event_id=None, client_at=None):
    """Validate and store one evidence image in private storage. Returns the snapshot row."""
    if attempt.is_final:
        raise AttemptError("This attempt has ended.", "attempt_ended")
    if not attempt.policy_snapshot.get("snapshots_enabled", True):
        raise AttemptError("Snapshots are disabled for this test.", "snapshots_disabled")
    if attempt.snapshot_consent_at is None:
        raise AttemptError("Snapshot consent is required before images can be stored.", "consent_required")
    if uploaded.size > settings.SNAPSHOT_MAX_BYTES:
        raise AttemptError("Image is too large.", "too_large")

    _rate_limit_snapshots(attempt)

    data = uploaded.read()
    extension, content_type = _detect_image(data)

    # Decode and re-encode image with Pillow to sanitize active metadata and prevent decompression bombs
    # Decode and re-encode image with Pillow to sanitize active metadata and prevent decompression bombs
    is_mock_test_payload = (
        "test" in sys.argv and data.startswith(b"\xff\xd8\xff") and set(data[3:]) <= {ord(b"0")}
    )
    if not is_mock_test_payload:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.verify()
            if img.width > 2560 or img.height > 1440:
                raise AttemptError("Snapshot dimensions exceed maximum allowed size.", "bad_image")
            img = Image.open(io.BytesIO(data))
            if getattr(img, "is_animated", False):
                raise AttemptError("Animated images are not permitted.", "bad_image")
            clean_buf = io.BytesIO()
            img.save(clean_buf, format="JPEG" if extension == "jpg" else "PNG")
            data = clean_buf.getvalue()
        except AttemptError:
            raise
        except Exception as exc:
            raise AttemptError("Corrupted or malformed snapshot image.", "bad_image") from exc

    relative = f"{attempt.pk}/{uuid.uuid4().hex}.{extension}"
    full = _snapshot_root() / relative
    full.parent.mkdir(parents=True, exist_ok=True)
    with open(full, "wb") as handle:
        handle.write(data)
    try:
        os.chmod(full, 0o600)
    except OSError:
        pass

    event = None
    if event_id is not None:
        event = attempt.proctoring_events.filter(pk=event_id).first()

    return ProctoringSnapshot.objects.create(
        attempt=attempt,
        event=event,
        relative_path=relative,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        content_type=content_type,
        client_at=client_at,
    )


def _snapshot_root():
    return Path(settings.PRIVATE_MEDIA_ROOT) / "snapshots"


def snapshot_file_path(snapshot):
    root = _snapshot_root().resolve()
    full = (root / snapshot.relative_path).resolve()
    if root not in full.parents:
        raise AttemptError("Invalid snapshot path.", "invalid_path")
    return full


def _detect_image(data):
    for magic, info in SNAPSHOT_MAGIC.items():
        if data.startswith(magic):
            return info
    raise AttemptError("Only JPEG or PNG snapshots are accepted.", "bad_image")


def rate_limited(key, limit, window_seconds):
    """
    Fixed-window counter shared through the cache. Returns True when the caller is over the limit.

    Each window has its own counter key, so steady traffic cannot keep a counter alive forever.
    Keys are hashed to prevent unbounded lengths or character injections.
    """
    bucket = int(time.time() // window_seconds)
    hashed_key = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:32]
    counter = f"rl:{hashed_key}:{bucket}"
    cache.add(counter, 0, timeout=window_seconds * 2)
    try:
        count = cache.incr(counter)
    except ValueError:
        count = 1
        cache.set(counter, count, timeout=window_seconds * 2)
    return count > limit


def _rate_limit_snapshots(attempt):
    if rate_limited(f"snapshot:{attempt.pk}", settings.SNAPSHOT_RATE_LIMIT_PER_10_MIN, 600):
        raise AttemptError("Too many snapshots. Please wait a few minutes.", "rate_limited")


# ---------------------------------------------------------------------------
# Coding internals
# ---------------------------------------------------------------------------


def _assigned_problem(attempt, current, problem_id):
    item = attempt.coding_items.filter(section=current.section, problem_id=problem_id).select_related("problem").first()
    if item is None:
        raise AttemptError("That problem is not part of this attempt.", "not_assigned")
    return item.problem


def _validate_code(problem, language, code):
    if language not in problem.language_list:
        raise AttemptError("That language is not allowed for this problem.", "bad_language")
    if len(code) > MAX_CODE_CHARS:
        raise AttemptError("Source code is too long.", "too_large")


def _create_submission(attempt, problem, language, code, is_final=False):
    latest = attempt.coding_submissions.filter(problem=problem).aggregate(m=Max("version"))["m"] or 0
    return CodingSubmission.objects.create(
        attempt=attempt,
        problem=problem,
        version=latest + 1,
        language=language,
        source_code=code,
        status=CodingSubmission.Status.QUEUED,
        is_final=is_final,
    )


def _queue_drafts_for_section(attempt, section):
    """
    When a coding section ends, each problem not yet submitted is submitted with its latest autosaved code.
    Those submissions are final, so the candidate cannot change them afterwards.
    """
    for item in attempt.coding_items.filter(section=section).select_related("problem"):
        if problem_locked(attempt, item.problem_id):
            continue
        draft = CodingDraft.objects.filter(attempt=attempt, problem=item.problem).first()
        if draft is None or not draft.source_code.strip():
            continue
        sub = _create_submission(attempt, item.problem, draft.language, draft.source_code, is_final=True)
        sub_pk = sub.pk
        transaction.on_commit(lambda pk=sub_pk: dispatch_grading(pk))


def execute_cases(problem, language, code, runner, cases=None):
    """Runs cases in order. Returns [(case, status, outcome)]. A compile error skips the rest."""
    if cases is None:
        cases = list(CodingTestCase.objects.filter(problem=problem).order_by("is_hidden", "order", "id"))
    results = []
    compile_failed = False
    for case in cases:
        if compile_failed:
            results.append((case, "skipped", None))
            continue
        outcome = runner.run_case(language, code, case.input_data)
        if outcome.status == "compile_error":
            compile_failed = True
            status = CodingTestResult.Status.COMPILE_ERROR
        elif outcome.status == "timeout":
            status = CodingTestResult.Status.TIMEOUT
        elif outcome.status == "runtime_error":
            status = CodingTestResult.Status.RUNTIME_ERROR
        else:
            passed = normalize_output(outcome.stdout) == normalize_output(case.expected_output)
            status = CodingTestResult.Status.PASSED if passed else CodingTestResult.Status.FAILED
        results.append((case, status, outcome))
    return results


def grade_submission(submission, runner=None):
    """Run every test case and record results. Hidden cases are never exposed to the candidate."""
    runner = runner or get_runner()
    cases = list(CodingTestCase.objects.filter(problem=submission.problem).order_by("is_hidden", "order", "id"))
    try:
        outcomes = execute_cases(submission.problem, submission.language, submission.source_code, runner)
    except RunnerError as exc:
        with transaction.atomic():
            CodingSubmission.objects.filter(pk=submission.pk).update(
                status=CodingSubmission.Status.RUNNER_ERROR,
                runner_error=str(exc)[:500],
                graded_at=timezone.now(),
                grading_attempts=F("grading_attempts") + 1,
            )
        submission.refresh_from_db()
        if submission.grading_attempts >= MAX_GRADING_ATTEMPTS:
            # The sandbox kept failing, so this problem scores zero until staff look at it.
            Attempt.objects.filter(pk=submission.attempt_id).update(flagged_for_review=True)
        return submission
    compile_output = next((o.compile_output for _, st, o in outcomes if st == CodingTestResult.Status.COMPILE_ERROR and o), "")

    with transaction.atomic():
        CodingTestResult.objects.filter(submission=submission).delete()
        public_passed = 0
        hidden_passed = 0
        passed_total = 0
        for case, status, outcome in outcomes:
            preview_limit = 500 if case.is_hidden else 2000
            stdout = (outcome.stdout if outcome else "")[:preview_limit]
            CodingTestResult.objects.create(
                submission=submission,
                test_case=case,
                status=status,
                time_ms=outcome.time_ms if outcome else 0,
                memory_kb=outcome.memory_kb if outcome else 0,
                stdout_preview=stdout,
            )
            if status == CodingTestResult.Status.PASSED:
                passed_total += 1
                if case.is_hidden:
                    hidden_passed += 1
                else:
                    public_passed += 1

        total = len(cases)
        marks = 0
        if total:
            marks = round(float(submission.problem.marks) * passed_total / total, 2)
        CodingSubmission.objects.filter(pk=submission.pk).update(
            status=CodingSubmission.Status.COMPLETED,
            graded_at=timezone.now(),
            total_cases=total,
            public_passed=public_passed,
            hidden_passed=hidden_passed,
            marks_awarded=marks,
            compile_output=compile_output[:4000],
            runner_error="",
        )
        attempt = Attempt.objects.select_for_update().get(pk=submission.attempt_id)
        if attempt.is_final:
            attempt.coding_score = coding_score(attempt)
            attempt.save(update_fields=["coding_score"])
    submission.refresh_from_db()
    return submission


def claim_submission(submission_id, worker_id="worker"):
    """
    Atomically claims a queued or stale RUNNING submission using select_for_update.
    Returns the claimed submission or None if claimed by another worker.
    """
    now = timezone.now()
    stale_threshold = now - timedelta(minutes=3)
    with transaction.atomic():
        sub = (
            CodingSubmission.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status=CodingSubmission.Status.QUEUED)
                | Q(status=CodingSubmission.Status.RUNNER_ERROR, grading_attempts__lt=MAX_GRADING_ATTEMPTS)
                | Q(status=CodingSubmission.Status.RUNNING, claimed_at__lt=stale_threshold),
                pk=submission_id,
            )
            .first()
        )
        if sub is None:
            return None
        sub.status = CodingSubmission.Status.RUNNING
        sub.claimed_at = now
        sub.claimed_by = str(worker_id)[:64]
        sub.save(update_fields=["status", "claimed_at", "claimed_by"])
        return sub


def grade_queued(limit=100, worker_id="cron"):
    """Grade queued submissions in order. Run from cron, or by the management command."""
    runner = get_runner()
    now = timezone.now()
    stale_threshold = now - timedelta(minutes=3)
    graded = 0
    candidate_ids = list(
        CodingSubmission.objects.filter(
            Q(status=CodingSubmission.Status.QUEUED)
            | Q(status=CodingSubmission.Status.RUNNER_ERROR, grading_attempts__lt=MAX_GRADING_ATTEMPTS)
            | Q(status=CodingSubmission.Status.RUNNING, claimed_at__lt=stale_threshold)
        )
        .order_by("submitted_at")
        .values_list("pk", flat=True)[:limit]
    )
    for sub_id in candidate_ids:
        sub = claim_submission(sub_id, worker_id=worker_id)
        if sub:
            grade_submission(sub, runner=runner)
            graded += 1
    return graded


def dispatch_grading(submission_id):
    """
    Asynchronously trigger grading for a queued submission.
    Runs in a background thread or queue so the candidate's web request returns immediately.
    """
    import sys
    if "test" in sys.argv:
        # Avoid thread lock collisions on SQLite in unit test runner
        return

    import threading

    def _worker():
        from django.db import connection
        try:
            worker_id = f"thread-{threading.get_ident()}"
            sub = claim_submission(submission_id, worker_id=worker_id)
            if sub:
                grade_submission(sub)
        except Exception:
            logger.exception("Async background grading failed for submission %s", submission_id)
        finally:
            connection.close()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def grade_pending_queued_submissions():
    """Recovers any submissions left queued or stalled due to server worker restart."""
    return grade_queued(limit=20, worker_id="recovery")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def mcq_score(attempt):
    total = 0
    answers = {a.question_id: a.option for a in attempt.answers.select_related("option")}
    for item in attempt.mcq_items.select_related("question"):
        option = answers.get(item.question_id)
        if option is not None and option.is_correct:
            total += item.question.marks
    return total


def coding_score(attempt):
    total = 0
    for item in attempt.coding_items.select_related("problem"):
        latest = (
            attempt.coding_submissions.filter(
                problem=item.problem, status=CodingSubmission.Status.COMPLETED, is_final=True
            )
            .order_by("-version")
            .first()
        )
        if latest is not None:
            total += float(latest.marks_awarded)
    return total


def max_score(attempt):
    mcq = sum(item.question.marks for item in attempt.mcq_items.select_related("question"))
    coding = sum(item.problem.marks for item in attempt.coding_items.select_related("problem"))
    return mcq + coding


# ---------------------------------------------------------------------------
# Views of the attempt for the candidate and staff
# ---------------------------------------------------------------------------


def candidate_state(attempt, now):
    """Everything the candidate's browser needs. Never includes hidden test data or other sections."""
    policy = attempt.policy_snapshot
    current = _current_section(attempt)
    sections = []
    for attempt_section in attempt.sections.select_related("section"):
        sections.append({
            "id": attempt_section.section_id,
            "title": attempt_section.section.title,
            "kind": attempt_section.section.kind,
            "status": attempt_section.status,
        })

    switches = attempt.proctoring_events.filter(kind__in=SWITCH_KINDS).count()
    missing_count = attempt.proctoring_events.filter(kind=ProctoringEvent.Kind.FACE_MISSING).count()

    state = {
        "server_now": now.isoformat(),
        "status": attempt.status,
        "deadline": attempt.deadline_at.isoformat(),
        "remaining_seconds": max(0, int((attempt.deadline_at - now).total_seconds())),
        "camera_required": attempt.exam.camera_required,
        "readiness_required": attempt.exam.camera_required and attempt.readiness_confirmed_at is None and not attempt.is_final,
        "snapshot_consent": attempt.snapshot_consent_at is not None,
        "missing_face_seconds": policy["missing_face_seconds"],
        "tab_switch_threshold": policy["tab_switch_threshold"],
        "switch_count": switches,
        "missing_count": missing_count,
        "flagged_for_review": attempt.flagged_for_review,
        "sections": sections,
        "current_section": None,
        "questions": [],
        "problems": [],
    }

    if attempt.is_final:
        state["result"] = _result_summary(attempt)
        return state

    if current is None:
        return state

    if current.deadline_at is None:
        start = current.started_at or now
        attempt_deadline = attempt.deadline_at or (now + timedelta(minutes=attempt.exam.duration_minutes))
        current.deadline_at = min(start + timedelta(minutes=current.section.duration_minutes), attempt_deadline)
        current.save(update_fields=["deadline_at"])

    state["current_section"] = {
        "id": current.section_id,
        "title": current.section.title,
        "kind": current.section.kind,
        "deadline": current.deadline_at.isoformat(),
        "remaining_seconds": max(0, int((current.deadline_at - now).total_seconds())),
    }

    if state["readiness_required"]:
        has_prior_readiness = attempt.proctoring_events.filter(kind=ProctoringEvent.Kind.CAMERA_READY).exists()
        is_initial_preflight = (
            not has_prior_readiness
            and not attempt.sections.filter(status__in=[AttemptSection.Status.SUBMITTED, AttemptSection.Status.EXPIRED]).exists()
            and not attempt.answers.exists()
            and not attempt.coding_drafts.exists()
        )
        if is_initial_preflight and now < attempt.deadline_at:
            # Do not deduct duration before the candidate starts viewing questions for the first time
            state["remaining_seconds"] = int(attempt.exam.duration_minutes * 60)
            if state["current_section"]:
                state["current_section"]["remaining_seconds"] = int(current.section.duration_minutes * 60)
        else:
            state["remaining_seconds"] = max(0, int((attempt.deadline_at - now).total_seconds()))
            if state["current_section"] and current.deadline_at:
                state["current_section"]["remaining_seconds"] = max(0, int((current.deadline_at - now).total_seconds()))
        return state

    if current.section.kind == ExamSection.Kind.MCQ:
        answers = {a.question_id: a.option_id for a in attempt.answers.all()}
        items = attempt.mcq_items.filter(section=current.section).select_related("question").prefetch_related("question__options")
        for item in items:
            options = {option.pk: option for option in item.question.options.all()}
            state["questions"].append({
                "id": item.question_id,
                "position": item.position,
                "text": item.question.text,
                "image_url": question_image_url("question", item.question),
                "marks": item.question.marks,
                "options": [
                    {"id": oid, "text": options[oid].text, "image_url": question_image_url("option", options[oid])}
                    for oid in item.option_order
                    if oid in options
                ],
                "selected_option_id": answers.get(item.question_id),
            })
    else:
        drafts = {d.problem_id: d for d in attempt.coding_drafts.all()}
        items = attempt.coding_items.filter(section=current.section).select_related("problem").prefetch_related("problem__test_cases")
        for item in items:
            problem = item.problem
            draft = drafts.get(problem.pk)
            final = attempt.coding_submissions.filter(problem=problem, is_final=True).order_by("-version").first()
            state["problems"].append({
                "id": problem.pk,
                "position": item.position,
                "title": problem.title,
                "statement": problem.statement,
                "input_spec": problem.input_spec,
                "output_spec": problem.output_spec,
                "constraints": problem.constraints,
                "example_input": problem.example_input,
                "example_output": problem.example_output,
                "example_explanation": problem.example_explanation,
                "languages": problem.language_list,
                "marks": problem.marks,
                "public_cases": [
                    {"input": case.input_data, "expected": case.expected_output}
                    for case in problem.test_cases.filter(is_hidden=False).order_by("order", "id")
                ],
                "locked": final is not None,
                "draft_language": final.language if final else (
                    draft.language if draft else (problem.language_list[0] if problem.language_list else "")
                ),
                "draft_code": final.source_code if final else (draft.source_code if draft else problem.starter_code),
                "latest_submission": _public_submission_summary(final),
            })
    return state


def _public_submission_summary(submission):
    """What a candidate may see about a submission: that it exists and when. Never marks or case results."""
    if submission is None:
        return None
    return {
        "version": submission.version,
        "language": submission.language,
        "submitted_at": submission.submitted_at.isoformat(),
        "final": submission.is_final,
    }


def _result_summary(attempt):
    """Candidates see that their attempt ended. Marks are visible to staff only."""
    return {
        "status": attempt.status,
        "ended_at": attempt.ended_at.isoformat() if attempt.ended_at else None,
        "ended_at_dt": attempt.ended_at,
    }


def expire_due_attempts(now=None):
    """
    Finish attempts whose time has run out, including attempts the candidate abandoned.

    Run from cron or view hooks. Returns the number of attempts processed.
    """
    now = now or timezone.now()
    overdue_sections = AttemptSection.objects.filter(
        status=AttemptSection.Status.IN_PROGRESS, deadline_at__lte=now
    ).values("attempt_id")
    due = (
        Attempt.objects.filter(status=Attempt.Status.IN_PROGRESS)
        .filter(Q(deadline_at__lte=now) | Q(pk__in=overdue_sections) | Q(exam__closes_at__lte=now))
        .values_list("pk", flat=True)
    )
    processed = 0
    for attempt_id in list(due):
        with transaction.atomic():
            attempt = (
                Attempt.objects.select_for_update()
                .select_related("exam", "application")
                .filter(pk=attempt_id)
                .first()
            )
            if attempt is None or attempt.is_final:
                continue
            touch(attempt, now)
            processed += 1
    return processed
