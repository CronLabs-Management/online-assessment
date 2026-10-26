"""Assembles the interviewer's view of one attempt. Hidden test data is never included."""

from django.db.models import Count, Sum

from .models import Application, Attempt, CodingSubmission, ProctoringEvent
from .services import coding_score, max_score, mcq_score


def _event_label(event):
    labels = dict(ProctoringEvent.Kind.choices)
    return labels.get(event.kind, event.kind)


def build_attempt_review(attempt):
    answers = {a.question_id: a for a in attempt.answers.select_related("option")}

    mcq_rows = []
    for item in attempt.mcq_items.select_related("question").prefetch_related("question__options"):
        options = {option.pk: option for option in item.question.options.all()}
        shown = [options[oid] for oid in item.option_order if oid in options]
        answer = answers.get(item.question_id)
        selected = answer.option if answer and answer.option_id else None
        correct = next((option for option in options.values() if option.is_correct), None)
        is_correct = bool(selected and selected.is_correct)
        visits = list(attempt.visits.filter(mcq_question_id=item.question_id).order_by("started_at"))
        mcq_rows.append({
            "position": item.position,
            "question_id": item.question_id,
            "text": item.question.text,
            "shown_options": [option.text for option in shown],
            "selected": selected.text if selected else None,
            "correct": correct.text if correct else None,
            "is_correct": is_correct,
            "marks": item.question.marks,
            "marks_awarded": item.question.marks if is_correct else 0,
            "total_seconds": sum(v.active_seconds for v in visits),
            "visit_seconds": [v.active_seconds for v in visits],
            "visit_count": len(visits),
            "answer_changes": answer.change_count if answer else 0,
            "revision_count": attempt.answer_revisions.filter(question_id=item.question_id).count(),
        })

    coding_rows = []
    for item in attempt.coding_items.select_related("problem").prefetch_related("problem__test_cases"):
        problem = item.problem
        submissions = list(
            attempt.coding_submissions.filter(problem=problem).order_by("-version").prefetch_related("results__test_case")
        )
        visits = list(attempt.visits.filter(coding_problem=problem).order_by("started_at"))
        versions = []
        for submission in submissions:
            versions.append({
                "version": submission.version,
                "language": submission.language,
                "status": submission.get_status_display(),
                "submitted_at": submission.submitted_at,
                "marks": float(submission.marks_awarded),
                "public_passed": submission.public_passed,
                "hidden_passed": submission.hidden_passed,
                "total_cases": submission.total_cases,
                "runner_error": submission.runner_error,
                "compile_output": submission.compile_output,
                "source_code": submission.source_code,
            })
        latest = submissions[0] if submissions else None
        results = []
        if latest is not None and latest.status == CodingSubmission.Status.COMPLETED:
            for result in latest.results.all():
                case = result.test_case
                row = {
                    "label": ("Hidden" if case.is_hidden else "Public") + f" {case.order}",
                    "is_hidden": case.is_hidden,
                    "status": result.get_status_display(),
                    "passed": result.status == "passed",
                    "time_ms": result.time_ms,
                    "memory_kb": result.memory_kb,
                    "input": None if case.is_hidden else case.input_data,
                    "expected": None if case.is_hidden else case.expected_output,
                    "stdout": None if case.is_hidden else result.stdout_preview,
                }
                results.append(row)
        coding_rows.append({
            "position": item.position,
            "problem_id": problem.pk,
            "title": problem.title,
            "statement": problem.statement,
            "languages": problem.language_list,
            "marks": problem.marks,
            "total_seconds": sum(v.active_seconds for v in visits),
            "visit_seconds": [v.active_seconds for v in visits],
            "visit_count": len(visits),
            "versions": versions,
            "latest": versions[0] if versions else None,
            "results": results,
        })

    timeline = []
    for event in attempt.proctoring_events.prefetch_related("snapshots"):
        snapshot = event.snapshots.first()
        timeline.append({
            "id": event.pk,
            "kind": event.kind,
            "label": _event_label(event),
            "server_at": event.server_at,
            "client_at": event.client_at,
            "duration_seconds": round(event.duration_ms / 1000, 1),
            "over_threshold": event.over_threshold,
            "snapshot_id": snapshot.pk if snapshot else None,
        })

    event_counts = dict(
        attempt.proctoring_events.values("kind").annotate(n=Count("id")).values_list("kind", "n")
    )
    missing_ms = attempt.proctoring_events.filter(kind=ProctoringEvent.Kind.FACE_MISSING).aggregate(
        total=Sum("duration_ms")
    )["total"] or 0
    tab_switches = event_counts.get(ProctoringEvent.Kind.TAB_HIDDEN, 0) + event_counts.get(
        ProctoringEvent.Kind.WINDOW_BLUR, 0
    )

    total_active = sum(
        v.active_seconds for v in attempt.visits.all()
    )

    summary = {
        "candidate": attempt.candidate.get_full_name() or attempt.candidate.username,
        "email": attempt.candidate.email,
        "application_status": attempt.application.get_status_display(),
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "status": attempt.get_status_display(),
        "active_seconds": total_active,
        "paused_seconds": attempt.total_paused_seconds,
        "mcq_score": float(attempt.mcq_score) if attempt.mcq_score is not None else float(mcq_score(attempt)),
        "coding_score": float(attempt.coding_score) if attempt.coding_score is not None else coding_score(attempt),
        "max_score": max_score(attempt),
        "tab_switches": tab_switches,
        "missing_face_count": event_counts.get(ProctoringEvent.Kind.FACE_MISSING, 0),
        "missing_face_seconds": round(missing_ms / 1000, 1),
        "multiple_faces_count": event_counts.get(ProctoringEvent.Kind.MULTIPLE_FACES, 0),
        "camera_interruptions": event_counts.get(ProctoringEvent.Kind.CAMERA_LOST, 0)
        + event_counts.get(ProctoringEvent.Kind.CAMERA_REVOKED, 0),
        "connection_interruptions": attempt.connection_events.count(),
        "snapshot_count": attempt.snapshots.count(),
        "flagged_for_review": attempt.flagged_for_review,
    }

    return {
        "attempt": attempt,
        "summary": summary,
        "mcq_rows": mcq_rows,
        "coding_rows": coding_rows,
        "timeline": timeline,
        "connections": attempt.connection_events.all(),
        "reviews": attempt.reviews.select_related("reviewer"),
    }


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Stop spreadsheet apps running candidate text as a formula (CSV injection)."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def attempt_csv_rows(exam):
    """One row per applicant, including those who never started."""
    attempts = {a.candidate_id: a for a in Attempt.objects.filter(exam=exam)}
    for application in Application.objects.filter(exam=exam).select_related("candidate").order_by("full_name"):
        attempt = attempts.get(application.candidate_id)
        row = {
            "full_name": application.full_name,
            "username": application.candidate.username,
            "email": application.candidate.email,
            "application_status": application.get_status_display(),
            "attempt_status": attempt.get_status_display() if attempt else "Not started",
            "started_at": attempt.started_at.isoformat() if attempt else "",
            "ended_at": attempt.ended_at.isoformat() if attempt and attempt.ended_at else "",
            "mcq_score": float(attempt.mcq_score) if attempt and attempt.mcq_score is not None else "",
            "coding_score": float(attempt.coding_score) if attempt and attempt.coding_score is not None else "",
            "flagged_for_review": "yes" if attempt and attempt.flagged_for_review else "no",
        }
        yield row
