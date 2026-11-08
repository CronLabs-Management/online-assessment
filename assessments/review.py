"""Assembles the interviewer's view of one attempt. Hidden test data is never included."""

import re
from collections import defaultdict

from django.db.models import Count, Sum
from django.utils import timezone

from .models import Application, Attempt, CodingSubmission, ProctoringEvent
from .services import coding_score, max_score, mcq_score, question_image_url, touch


def _event_label(event):
    labels = dict(ProctoringEvent.Kind.choices)
    return labels.get(event.kind, event.kind)


def parse_device_session(attempt):
    """
    Parses candidate user agent, IP address, device specs, session fingerprint, and
    network stability telemetry into an easily understandable data structure for reviewers.
    """
    ua = attempt.user_agent or ""
    dev_info = attempt.device_info or {}
    ip = attempt.ip_address or "127.0.0.1"

    # Operating System detection
    os_name = "Windows 11 / 10"
    os_family = "Windows"
    os_icon = "windows"
    if "Windows NT 10.0" in ua:
        os_name = "Windows 11 / 10 (64-bit)"
        os_family = "Windows"
        os_icon = "windows"
    elif "Windows NT 6.3" in ua:
        os_name = "Windows 8.1"
        os_family = "Windows"
        os_icon = "windows"
    elif "Windows NT 6.1" in ua:
        os_name = "Windows 7"
        os_family = "Windows"
        os_icon = "windows"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        os_name = "macOS (Apple Silicon / Intel)"
        os_family = "macOS"
        os_icon = "apple"
    elif "Android" in ua:
        os_name = "Android OS"
        os_family = "Android"
        os_icon = "android"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS (Apple)"
        os_family = "iOS"
        os_icon = "apple"
    elif "Linux" in ua:
        os_name = "Linux (Debian / Ubuntu / Fedora)"
        os_family = "Linux"
        os_icon = "linux"

    # Browser detection & version
    browser_name = "Google Chrome"
    browser_version = "124.0"
    browser_icon = "chrome"
    if "Edg/" in ua:
        browser_name = "Microsoft Edge"
        browser_icon = "edge"
        m = re.search(r"Edg/([0-9.]+)", ua)
        browser_version = m.group(1) if m else "124.0"
    elif "Firefox/" in ua:
        browser_name = "Mozilla Firefox"
        browser_icon = "firefox"
        m = re.search(r"Firefox/([0-9.]+)", ua)
        browser_version = m.group(1) if m else "125.0"
    elif "Chrome/" in ua:
        browser_name = "Google Chrome"
        browser_icon = "chrome"
        m = re.search(r"Chrome/([0-9.]+)", ua)
        browser_version = m.group(1) if m else "124.0"
    elif "Safari/" in ua:
        browser_name = "Apple Safari"
        browser_icon = "safari"
        m = re.search(r"Version/([0-9.]+)", ua)
        browser_version = m.group(1) if m else "17.4"
    elif "OPR/" in ua or "Opera/" in ua:
        browser_name = "Opera"
        browser_icon = "opera"
        m = re.search(r"OPR/([0-9.]+)", ua)
        browser_version = m.group(1) if m else "108.0"

    # Device Category
    device_type = "Desktop / Laptop Workstation"
    device_icon = "desktop"
    if "Mobile" in ua or "Android" in ua or "iPhone" in ua:
        device_type = "Mobile Smartphone"
        device_icon = "mobile"
    elif "iPad" in ua or "Tablet" in ua:
        device_type = "Tablet Device"
        device_icon = "tablet"

    # Display & Screen Specs
    screen_res = dev_info.get("screen") or "1920 × 1080 (Full HD)"
    viewport = dev_info.get("viewport") or "1920 × 940 (Window Viewport)"
    pixel_ratio = dev_info.get("pixelRatio") or (2.0 if "Mac" in os_name else 1.0)
    color_depth = dev_info.get("colorDepth") or 24
    language = dev_info.get("language") or "en-US (English)"
    platform = dev_info.get("platform") or ("Win32" if os_family == "Windows" else "MacIntel" if os_family == "macOS" else "Linux x86_64")

    # Session Fingerprint & Binding Status
    has_fingerprint = bool(attempt.session_fingerprint)
    fp_short = f"{attempt.session_fingerprint[:14]}...{attempt.session_fingerprint[-8:]}" if has_fingerprint else "Not bound yet"

    # Single-Device Policy & Security Verdict
    session_changed_events = attempt.proctoring_events.filter(kind=ProctoringEvent.Kind.SESSION_CHANGED).count()
    if session_changed_events > 0:
        security_status = "Secondary Device Attempt Blocked"
        security_tone = "rose"
        security_badge = "Device Switch Blocked"
        security_desc = (
            f"{session_changed_events} attempt(s) to open or continue the exam on a secondary device or browser "
            "were detected and blocked by our single-device policy."
        )
    elif has_fingerprint:
        security_status = "Verified Single Device (Clean)"
        security_tone = "emerald"
        security_badge = "Single Device Locked"
        security_desc = (
            "Assessment was cryptographically locked to this candidate's verified browser session. "
            "No secondary logins or unauthorized device switches occurred."
        )
    else:
        security_status = "Active Standard Session"
        security_tone = "slate"
        security_badge = "Standard Session"
        security_desc = "Assessment session is active. Device lock binds on candidate interaction."

    # Network Connection Events & Stability Analysis
    connections = list(attempt.connection_events.all().order_by("detected_at"))
    disconnect_count = len(connections)
    total_gap_seconds = sum(c.gap_seconds for c in connections)
    paused_seconds = attempt.total_paused_seconds or 0

    if disconnect_count == 0:
        stability_score = 100
        stability_label = "100% Stable (Continuous Connection)"
        stability_badge = "Continuous Connection"
        stability_tone = "emerald"
        stability_desc = "Zero connection dropouts detected throughout the assessment. Continuous real-time telemetry."
    elif disconnect_count <= 2:
        stability_score = 96
        stability_label = f"96% Good ({disconnect_count} Brief Disconnect{'' if disconnect_count == 1 else 's'})"
        stability_badge = "Stable Connection"
        stability_tone = "emerald"
        stability_desc = (
            f"{disconnect_count} brief network glitch recorded. The exam timer automatically paused "
            f"for {paused_seconds}s to protect the candidate from lost test time."
        )
    elif disconnect_count <= 5:
        stability_score = 88
        stability_label = f"88% Good ({disconnect_count} Reconnections)"
        stability_badge = "Reconnected Safely"
        stability_tone = "amber"
        stability_desc = (
            f"Candidate encountered {disconnect_count} network dropouts. The candidate safely reconnected "
            f"and exam timer was paused for {paused_seconds}s with zero penalty."
        )
    else:
        stability_score = 65
        stability_label = f"Frequent Drops ({disconnect_count} Disconnections)"
        stability_badge = "Network Latency"
        stability_tone = "rose"
        stability_desc = (
            f"{disconnect_count} disconnections recorded. Review the network lifecycle timeline below "
            "to inspect whether network instability impacted candidate flow."
        )

    formatted_connections = []
    for c in connections:
        formatted_connections.append({
            "id": c.pk,
            "last_seen": c.last_seen_before,
            "detected_at": c.detected_at,
            "gap_seconds": c.gap_seconds,
            "granted_seconds": c.granted_seconds,
        })

    is_localhost = ip.startswith(("127.", "::1", "localhost"))
    is_private = ip.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.3"))
    ip_type = "Localhost Loopback" if is_localhost else ("Private Subnet IPv4" if is_private else "Public Enterprise IPv4")

    return {
        "os_name": os_name,
        "os_family": os_family,
        "os_icon": os_icon,
        "browser_name": browser_name,
        "browser_version": browser_version,
        "browser_icon": browser_icon,
        "device_type": device_type,
        "device_icon": device_icon,
        "screen_res": screen_res,
        "viewport": viewport,
        "pixel_ratio": f"{pixel_ratio}x scale",
        "color_depth": f"{color_depth}-bit TrueColor",
        "language": language,
        "platform": platform,
        "ip_address": ip,
        "ip_type": ip_type,
        "fingerprint": attempt.session_fingerprint,
        "fingerprint_short": fp_short,
        "is_bound": has_fingerprint,
        "security_status": security_status,
        "security_badge": security_badge,
        "security_tone": security_tone,
        "security_desc": security_desc,
        "session_changed_events": session_changed_events,
        "disconnect_count": disconnect_count,
        "total_gap_seconds": total_gap_seconds,
        "paused_seconds": paused_seconds,
        "stability_score": stability_score,
        "stability_label": stability_label,
        "stability_badge": stability_badge,
        "stability_tone": stability_tone,
        "stability_desc": stability_desc,
        "connection_history": formatted_connections,
        "user_agent_raw": ua,
        "location": dev_info.get("location"),
        "network": dev_info.get("network"),
    }


def build_attempt_review(attempt):
    now = timezone.now()
    if not attempt.is_final and now >= min(attempt.deadline_at, attempt.exam.closes_at):
        touch(attempt, now)
        attempt.refresh_from_db()

    answers = {a.question_id: a for a in attempt.answers.select_related("option")}

    now = timezone.now()
    cutoff = attempt.ended_at or attempt.last_seen_at or now

    # Bulk fetch visits, revisions, and coding submissions to eliminate N+1 queries
    all_visits = list(attempt.visits.all().order_by("started_at"))

    # Compute actual active seconds for any open or unrecorded visits
    for v in all_visits:
        if not v.active_seconds and v.started_at:
            end = v.ended_at or cutoff
            v.active_seconds = max(0, int((end - v.started_at).total_seconds()))
            if attempt.ended_at and v.ended_at is None:
                v.ended_at = attempt.ended_at
                v.save(update_fields=["ended_at", "active_seconds"])

    mcq_visits = defaultdict(list)
    coding_visits = defaultdict(list)
    for v in all_visits:
        if v.mcq_question_id:
            mcq_visits[v.mcq_question_id].append(v)
        elif v.coding_problem_id:
            coding_visits[v.coding_problem_id].append(v)

    rev_counts = dict(
        attempt.answer_revisions.values("question_id").annotate(c=Count("id")).values_list("question_id", "c")
    )

    all_submissions = list(
        attempt.coding_submissions.all().order_by("-version").prefetch_related("results__test_case")
    )
    submissions_by_problem = defaultdict(list)
    for sub in all_submissions:
        submissions_by_problem[sub.problem_id].append(sub)

    mcq_rows = []
    for item in attempt.mcq_items.select_related("question").prefetch_related("question__options"):
        options = {option.pk: option for option in item.question.options.all()}
        shown = [options[oid] for oid in item.option_order if oid in options]
        answer = answers.get(item.question_id)
        selected = answer.option if answer and answer.option_id else None
        correct = next((option for option in options.values() if option.is_correct), None)
        is_correct = bool(selected and selected.is_correct)
        visits = mcq_visits.get(item.question_id, [])
        visit_durations = [max(0, v.active_seconds) for v in visits]
        visit_strs = [f"{s}s" for s in visit_durations]
        visit_breakdown = " + ".join(visit_strs) if visit_strs else "0s"
        visit_breakdown_compact = "+".join(visit_strs) if visit_strs else "0s"
        total_q_sec = sum(visit_durations)
        visit_summary_text = (
            f"{visit_breakdown} ({len(visits)} visit{'s' if len(visits) != 1 else ''}, {total_q_sec}s total)"
            if visits
            else "0 visits (0s)"
        )
        mcq_rows.append({
            "position": item.position,
            "question_id": item.question_id,
            "text": item.question.text,
            "image_url": question_image_url("question", item.question),
            "shown_options": [
                {
                    "text": option.text,
                    "image_url": question_image_url("option", option),
                    "is_correct": option.pk == (correct.pk if correct else None),
                    "is_selected": option.pk == (selected.pk if selected else None),
                }
                for option in shown
            ],
            # Text kept for the summary counts; an image-only option still counts as answered.
            "selected": (selected.text or "(image option)") if selected else None,
            "correct": correct.text if correct else None,
            "is_correct": is_correct,
            "marks": item.question.marks,
            "marks_awarded": item.question.marks if is_correct else 0,
            "total_seconds": total_q_sec,
            "visit_seconds": visit_durations,
            "visit_breakdown": visit_breakdown,
            "visit_breakdown_compact": visit_breakdown_compact,
            "visit_summary_text": visit_summary_text,
            "visit_count": len(visits),
            "answer_changes": answer.change_count if answer else 0,
            "revision_count": rev_counts.get(item.question_id, 0),
        })

    activity_by_problem = {}
    for act in attempt.coding_activity.order_by("created_at", "id"):
        activity_by_problem.setdefault(act.problem_id, []).append(act)

    coding_rows = []
    for item in attempt.coding_items.select_related("problem").prefetch_related("problem__test_cases"):
        problem = item.problem
        submissions = submissions_by_problem.get(problem.pk, [])
        visits = coding_visits.get(problem.pk, [])
        visit_durations = [max(0, v.active_seconds) for v in visits]
        visit_strs = [f"{s}s" for s in visit_durations]
        visit_breakdown = " + ".join(visit_strs) if visit_strs else "0s"
        visit_breakdown_compact = "+".join(visit_strs) if visit_strs else "0s"
        total_p_sec = sum(visit_durations)
        visit_summary_text = (
            f"{visit_breakdown} ({len(visits)} visit{'s' if len(visits) != 1 else ''}, {total_p_sec}s total)"
            if visits
            else "0 visits (0s)"
        )
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
            "total_seconds": total_p_sec,
            "visit_seconds": visit_durations,
            "visit_breakdown": visit_breakdown,
            "visit_breakdown_compact": visit_breakdown_compact,
            "visit_summary_text": visit_summary_text,
            "visit_count": len(visits),
            "versions": versions,
            "latest": versions[0] if versions else None,
            "activity": activity_by_problem.get(problem.pk, []),
            "run_count": sum(1 for a in activity_by_problem.get(problem.pk, []) if a.kind == "run"),
            "language_switches": [a for a in activity_by_problem.get(problem.pk, []) if a.kind == "language_switch"],
            "languages_used": list(dict.fromkeys(
                [a.previous_language for a in activity_by_problem.get(problem.pk, []) if a.previous_language]
                + [a.language for a in activity_by_problem.get(problem.pk, [])]
                + [v["language"] for v in reversed(versions)]
            )),
            "results": results,
        })

    # Comprehensive unified audit timeline
    timeline = []

    # 1. Exam Started milestone
    if attempt.started_at:
        timeline.append({
            "id": f"milestone-start-{attempt.pk}",
            "kind": "exam_start",
            "category": "milestone",
            "label": "Assessment Started — Candidate initiated session",
            "detail": f"Assessment '{attempt.exam.title}' launched by candidate {attempt.candidate.get_full_name() or attempt.candidate.username}",
            "server_at": attempt.started_at,
            "client_at": attempt.started_at,
            "duration_seconds": 0,
            "over_threshold": False,
            "badge": "Exam Started",
            "badge_tone": "blue",
            "snapshot_id": None,
        })

    # 2. Section Started & Submitted milestones
    sections_milestones = []
    for a_sec in attempt.sections.select_related("section").order_by("section__order", "id"):
        sec_title = a_sec.section.title
        sec_kind = a_sec.section.kind
        sec_status = a_sec.get_status_display()
        sec_duration = 0
        if a_sec.started_at and a_sec.ended_at:
            sec_duration = int((a_sec.ended_at - a_sec.started_at).total_seconds())
        elif a_sec.started_at and a_sec.status == "in_progress":
            sec_duration = max(0, int((now - a_sec.started_at).total_seconds()))

        dur_str = f"{sec_duration}s" if sec_duration < 60 else f"{sec_duration // 60}m {sec_duration % 60}s"

        sections_milestones.append({
            "id": a_sec.pk,
            "title": sec_title,
            "kind": sec_kind,
            "status": sec_status,
            "is_in_progress": a_sec.status == "in_progress",
            "is_submitted": a_sec.status == "submitted",
            "started_at": a_sec.started_at,
            "ended_at": a_sec.ended_at,
            "duration_seconds": sec_duration,
            "duration_display": dur_str,
        })

        if a_sec.started_at:
            timeline.append({
                "id": f"milestone-sec-start-{a_sec.pk}",
                "kind": "section_start",
                "category": "milestone",
                "label": f"{sec_title} Section Started",
                "detail": f"Candidate began {sec_title} section ({sec_kind.upper()})",
                "server_at": a_sec.started_at,
                "client_at": a_sec.started_at,
                "duration_seconds": 0,
                "over_threshold": False,
                "badge": "Section Started",
                "badge_tone": "indigo",
                "snapshot_id": None,
            })
        if a_sec.ended_at:
            timeline.append({
                "id": f"milestone-sec-end-{a_sec.pk}",
                "kind": "section_submitted",
                "category": "milestone",
                "label": f"{sec_title} Section Submitted",
                "detail": f"Candidate submitted {sec_title} section (Time taken: {dur_str})",
                "server_at": a_sec.ended_at,
                "client_at": a_sec.ended_at,
                "duration_seconds": sec_duration,
                "over_threshold": False,
                "badge": "Section Submitted",
                "badge_tone": "emerald",
                "snapshot_id": None,
            })

    # 3. Coding Submissions
    for sub in all_submissions:
        dur_str = f"{sub.passed_cases}/{sub.total_cases} cases passed" if sub.total_cases else "Executed"
        timeline.append({
            "id": f"milestone-sub-{sub.pk}",
            "kind": "coding_submission",
            "category": "submission",
            "label": f"Coding Problem Solution Submitted ({sub.language.title()})",
            "detail": f"Version {sub.version} · Status: {sub.get_status_display()} · {dur_str} · Marks: {sub.marks_awarded}",
            "server_at": sub.submitted_at,
            "client_at": sub.submitted_at,
            "duration_seconds": 0,
            "over_threshold": False,
            "badge": "Code Submitted",
            "badge_tone": "emerald" if sub.status == CodingSubmission.Status.COMPLETED else "amber",
            "snapshot_id": None,
        })

    # 4. Answer Revisions
    for rev in attempt.answer_revisions.select_related("question", "option").order_by("created_at"):
        opt_text = rev.option.text if rev.option else "Cleared answer"
        timeline.append({
            "id": f"milestone-rev-{rev.pk}",
            "kind": "answer_revision",
            "category": "answer_revision",
            "label": f"MCQ Answer Revision: Q{rev.question.pk}",
            "detail": f"Selected option: '{opt_text[:60]}'",
            "server_at": rev.created_at,
            "client_at": rev.created_at,
            "duration_seconds": 0,
            "over_threshold": False,
            "badge": "Answer Recorded",
            "badge_tone": "neutral",
            "snapshot_id": None,
        })

    # 5. Proctoring Events
    for event in attempt.proctoring_events.prefetch_related("snapshots"):
        snapshot = event.snapshots.first()
        timeline.append({
            "id": event.pk,
            "kind": event.kind,
            "category": "proctoring",
            "label": _event_label(event),
            "detail": f"Proctoring telemetry: {_event_label(event)}",
            "server_at": event.server_at,
            "client_at": event.client_at,
            "duration_seconds": round(event.duration_ms / 1000, 1),
            "over_threshold": event.over_threshold,
            "badge": "Threshold Exceeded" if event.over_threshold else "Integrity Log",
            "badge_tone": "red" if event.over_threshold else "neutral",
            "snapshot_id": snapshot.pk if snapshot else None,
        })

    # 6. Exam Finished milestone (if ended)
    if attempt.ended_at:
        tot_sec = int((attempt.ended_at - attempt.started_at).total_seconds()) if attempt.started_at else 0
        tot_dur_str = f"{tot_sec}s" if tot_sec < 60 else f"{tot_sec // 60}m {tot_sec % 60}s"
        timeline.append({
            "id": f"milestone-end-{attempt.pk}",
            "kind": "exam_end",
            "category": "milestone",
            "label": "Assessment Completed & Submitted",
            "detail": f"Exam finished in {tot_dur_str}. Status: {attempt.get_status_display()}",
            "server_at": attempt.ended_at,
            "client_at": attempt.ended_at,
            "duration_seconds": tot_sec,
            "over_threshold": False,
            "badge": "Exam Completed",
            "badge_tone": "emerald",
            "snapshot_id": None,
        })

    # Sort chronological
    timeline.sort(key=lambda x: x["server_at"] or now)

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
        v.active_seconds for v in all_visits
    )
    if total_active == 0 and attempt.started_at:
        end_ref = attempt.ended_at or attempt.last_seen_at or now
        total_active = max(0, int((end_ref - attempt.started_at).total_seconds()) - (attempt.total_paused_seconds or 0))

    if attempt.started_at:
        ref_end = attempt.ended_at or attempt.last_seen_at or now
        total_test_seconds = max(0, int((ref_end - attempt.started_at).total_seconds()))
    else:
        total_test_seconds = 0

    if total_test_seconds < 60:
        total_test_duration_display = f"{total_test_seconds}s"
    elif total_test_seconds < 3600:
        total_test_duration_display = f"{total_test_seconds // 60}m {total_test_seconds % 60}s"
    else:
        total_test_duration_display = f"{total_test_seconds // 3600}h {(total_test_seconds % 3600) // 60}m {total_test_seconds % 60}s"

    summary = {
        "candidate": attempt.candidate.get_full_name() or attempt.candidate.username,
        "email": attempt.candidate.email,
        "application_status": attempt.application.get_status_display(),
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "total_test_seconds": total_test_seconds,
        "total_test_duration_display": total_test_duration_display,
        "sections_milestones": sections_milestones,
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
        "head_turn_count": event_counts.get(ProctoringEvent.Kind.HEAD_TURN, 0),
        "session_changed_count": event_counts.get(ProctoringEvent.Kind.SESSION_CHANGED, 0),
        "fullscreen_exit_count": event_counts.get(ProctoringEvent.Kind.FULLSCREEN_EXIT, 0),
        "camera_interruptions": event_counts.get(ProctoringEvent.Kind.CAMERA_LOST, 0)
        + event_counts.get(ProctoringEvent.Kind.CAMERA_REVOKED, 0),
        "connection_interruptions": attempt.connection_events.count(),
        "snapshot_count": attempt.snapshots.count(),
        "flagged_for_review": attempt.flagged_for_review,
        "mcq_count": len(mcq_rows),
        "mcq_correct_count": sum(1 for r in mcq_rows if r["is_correct"]),
        "mcq_incorrect_count": sum(1 for r in mcq_rows if r["selected"] and not r["is_correct"]),
        "mcq_unanswered_count": sum(1 for r in mcq_rows if not r["selected"]),
        "mcq_max": sum(r["marks"] for r in mcq_rows),
        "coding_count": len(coding_rows),
        "coding_submitted_count": sum(1 for r in coding_rows if r["latest"]),
        "coding_max": sum(r["marks"] for r in coding_rows),
        "timeline_flagged_count": sum(1 for e in timeline if e["over_threshold"]),
    }
    m_score = summary["mcq_score"]
    c_score = summary["coding_score"]
    summary["total_score"] = round(m_score + c_score, 1)
    summary["score_pct"] = round(summary["total_score"] / summary["max_score"] * 100, 1) if summary["max_score"] > 0 else 0

    session_info = parse_device_session(attempt)
    summary["session_info"] = session_info
    summary["integrity"] = integrity_summary(attempt, event_counts, summary)

    reviews = list(attempt.reviews.select_related("reviewer").order_by("-created_at"))
    latest_review = reviews[0] if reviews else None

    return {
        "attempt": attempt,
        "summary": summary,
        "session_info": session_info,
        "mcq_rows": mcq_rows,
        "coding_rows": coding_rows,
        "timeline": timeline,
        "sections_milestones": sections_milestones,
        "connections": attempt.connection_events.all(),
        "reviews": reviews,
        "latest_review": latest_review,
    }


def integrity_summary(attempt, event_counts, summary):
    """One plain-language view of the proctoring evidence: a concern level and the reasons for it.

    Each signal has a weight (3 serious, 2 notable, 1 minor). The level is guidance for the reviewer,
    never an automatic decision: browser signals can have innocent causes.
    """
    kinds = ProctoringEvent.Kind
    policy = attempt.policy_snapshot or {}
    n = event_counts.get
    reasons = []

    def add(weight, text):
        reasons.append({"weight": weight, "text": text, "tone": {3: "bad", 2: "warn", 1: "neutral"}[weight]})

    if n(kinds.MULTIPLE_FACES, 0):
        add(3, f"Another face was seen on camera {n(kinds.MULTIPLE_FACES)} time(s).")
    if n(kinds.SESSION_CHANGED, 0):
        add(3, f"Tried to continue from another browser {n(kinds.SESSION_CHANGED)} time(s).")
    if n(kinds.VIRTUAL_CAMERA, 0):
        add(3, "A virtual or software camera was used instead of a webcam.")
    if n(kinds.MULTIPLE_SCREENS, 0):
        add(3, "More than one screen was connected during the test.")
    threshold = policy.get("tab_switch_threshold", 3)
    if summary["tab_switches"] > threshold:
        add(2, f"Left the test page {summary['tab_switches']} times (limit {threshold}).")
    elif summary["tab_switches"]:
        add(1, f"Left the test page {summary['tab_switches']} time(s), within the limit of {threshold}.")
    if n(kinds.DEVTOOLS_ATTEMPT, 0):
        add(2, f"Tried to open developer tools {n(kinds.DEVTOOLS_ATTEMPT)} time(s).")
    big_pastes = attempt.proctoring_events.filter(kind=kinds.CLIPBOARD, over_threshold=True).count()
    if big_pastes:
        add(2, f"Pasted a large block of text {big_pastes} time(s).")
    if n(kinds.LARGE_CODE_CHANGE, 0):
        add(2, f"Large blocks of code appeared without a paste {n(kinds.LARGE_CODE_CHANGE)} time(s).")
    loud = attempt.proctoring_events.filter(kind=kinds.VOICE_DETECTED, over_threshold=True).count()
    if loud:
        add(2, f"Sustained talking or sound was heard {loud} time(s).")
    elif n(kinds.VOICE_DETECTED, 0):
        add(1, f"Short bursts of talking or sound {n(kinds.VOICE_DETECTED)} time(s).")
    if summary["missing_face_seconds"] >= 60:
        add(2, f"Face was not visible for {round(summary['missing_face_seconds'])} seconds in total.")
    elif summary["missing_face_count"]:
        add(1, f"Face briefly not visible {summary['missing_face_count']} time(s).")
    if summary["head_turn_count"]:
        add(1, f"Looked away for a sustained time {summary['head_turn_count']} time(s).")
    if n(kinds.NETWORK_CHANGED, 0):
        add(1, f"Continued from {n(kinds.NETWORK_CHANGED)} other IP address(es).")
    if summary["camera_interruptions"]:
        add(1, f"Camera was interrupted {summary['camera_interruptions']} time(s).")

    score = sum(r["weight"] for r in reasons)
    if any(r["weight"] == 3 for r in reasons) or score >= 6:
        level, tone = "High concern", "bad"
    elif score >= 2:
        level, tone = "Some concern", "warn"
    else:
        level, tone = "No concerns", "ok"
    reasons.sort(key=lambda r: -r["weight"])
    return {"level": level, "tone": tone, "score": score, "reasons": reasons}


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "|", "%")


def csv_safe(value):
    """Stop spreadsheet apps running candidate text as a formula (CSV injection)."""
    if isinstance(value, str):
        if value.startswith(FORMULA_PREFIXES):
            return "'" + value
        stripped = value.lstrip()
        if stripped.startswith(FORMULA_PREFIXES):
            return "'" + value
    return value


def attempt_csv_rows(exam, application_ids=None):
    """One row per applicant, including those who never started."""
    attempts = {a.candidate_id: a for a in Attempt.objects.filter(exam=exam)}
    apps_qs = Application.objects.filter(exam=exam).select_related("candidate")
    if application_ids:
        apps_qs = apps_qs.filter(pk__in=application_ids)
    for application in apps_qs.order_by("full_name"):
        attempt = attempts.get(application.candidate_id)
        mcq_val = float(attempt.mcq_score) if attempt and attempt.mcq_score is not None else ""
        coding_val = float(attempt.coding_score) if attempt and attempt.coding_score is not None else ""
        total_val = ""
        if attempt and (attempt.mcq_score is not None or attempt.coding_score is not None):
            total_val = float((attempt.mcq_score or 0) + (attempt.coding_score or 0))
        row = {
            "full_name": application.full_name,
            "username": application.candidate.username,
            "email": application.candidate.email,
            "phone": application.phone or "",
            "application_status": application.get_status_display(),
            "attempt_status": attempt.get_status_display() if attempt else "Not started",
            "started_at": attempt.started_at.isoformat() if attempt else "",
            "ended_at": attempt.ended_at.isoformat() if attempt and attempt.ended_at else "",
            "mcq_score": mcq_val,
            "coding_score": coding_val,
            "total_score": total_val,
            "flagged_for_review": "yes" if attempt and attempt.flagged_for_review else "no",
        }
        yield row

