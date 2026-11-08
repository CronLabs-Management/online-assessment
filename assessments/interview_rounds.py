"""Which AI questions belong to each interview round, and how interviewer scores are saved on them.

Questions are stored per round on ``Application.ai_round_questions`` ({"1": [...], "2": [...]}).
Older applications only have ``ai_interview_questions`` (one list for every round); those are split
across the rounds the way the review page always did. Scores live on ``InterviewReview.questions_data``
as the full question list for that round, so a saved round keeps exactly what the interviewer saw.
"""

DEFAULT_MAX_MARKS = 10


def _legacy_split(questions, round_number, total_rounds):
    total = len(questions)
    if total_rounds <= 1 or not total:
        return questions
    if total_rounds == 2 and total >= 6:
        return questions[:4] if round_number == 1 else questions[4:]
    per_round = max(1, total // total_rounds)
    start = (round_number - 1) * per_round
    end = start + per_round if round_number < total_rounds else total
    return questions[start:end]


def uses_round_storage(application):
    return bool(application.ai_round_questions) or not application.ai_interview_questions


def base_questions(application, round_number, total_rounds):
    """The generated questions for one round, without scores."""
    if application.ai_round_questions:
        return list(application.ai_round_questions.get(str(round_number)) or [])
    return list(_legacy_split(application.ai_interview_questions or [], round_number, total_rounds))


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _scored(item):
    return bool(item.get("answered") or item.get("is_reviewed"))


def _enrich(questions):
    rows = []
    for idx, q in enumerate(questions, 1):
        row = dict(q)
        row["id"] = int(_number(row.get("id"), idx) or idx)
        row["max_marks"] = int(_number(row.get("max_marks"), DEFAULT_MAX_MARKS) or DEFAULT_MAX_MARKS)
        scored = _scored(row)
        row["answered"] = row["is_reviewed"] = scored
        row["marks_awarded"] = _number(row.get("marks_awarded", row.get("marks")), 0) if scored else None
        rows.append(row)
    return rows


def round_questions(application, round_number, total_rounds, review=None):
    """Questions for a round with any saved scores applied, ready for the review page."""
    saved = list(review.questions_data or []) if review else []
    if saved and all(isinstance(q, dict) and q.get("question") for q in saved):
        return _enrich(saved)
    rows = _enrich(base_questions(application, round_number, total_rounds))
    if saved:
        by_id = {int(_number(q.get("id"), 0) or 0): q for q in saved if isinstance(q, dict)}
        for row in rows:
            score = by_id.get(row["id"])
            if score and _scored(score):
                row["answered"] = row["is_reviewed"] = True
                row["marks_awarded"] = _number(score.get("marks_awarded", score.get("marks")), 0)
    return rows


def merge_scores(application, round_number, total_rounds, submitted, review=None):
    """Apply scores posted from the review page ({id, marks_awarded, answered}) to the round's questions.

    Returns the full list to store on the review. Marks are clamped to 0..max_marks.
    """
    rows = round_questions(application, round_number, total_rounds, review)
    by_id = {}
    for item in submitted or []:
        if isinstance(item, dict) and _number(item.get("id")) is not None:
            by_id[int(_number(item.get("id")))] = item
    for row in rows:
        item = by_id.get(row["id"])
        if item is None:
            continue
        if _scored(item):
            marks = _number(item.get("marks_awarded", item.get("marks")), 0) or 0
            row["marks_awarded"] = max(0.0, min(marks, float(row["max_marks"])))
            row["answered"] = row["is_reviewed"] = True
        else:
            row["marks_awarded"] = None
            row["answered"] = row["is_reviewed"] = False
        if item.get("notes"):
            row["notes"] = str(item["notes"])[:1000]
    return rows
