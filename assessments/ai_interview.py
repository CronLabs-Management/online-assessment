import io
import json
import logging
import os
import re

from django.utils import timezone

from .models import Application

logger = logging.getLogger(__name__)

DIFFICULTY_ORDER = {"Easy": 1, "Intermediate": 2, "Hard": 3}


def normalize_difficulty(val: str) -> str:
    s = str(val or "").strip().lower()
    if s in ("easy", "fundamental", "basic", "junior", "beginner"):
        return "Easy"
    if s in ("hard", "advanced", "expert", "senior", "complex"):
        return "Hard"
    return "Intermediate"


def sort_and_enrich_questions(questions):
    """
    Normalizes difficulty strictly to Easy, Intermediate, Hard,
    and sorts questions strictly: Easy -> Intermediate -> Hard.
    Enriches with default answered status and max marks.
    """
    for q in questions:
        diff = normalize_difficulty(q.get("difficulty"))
        q["difficulty"] = diff
        q["max_marks"] = 5 if diff == "Easy" else 10
        if "answered" not in q:
            q["answered"] = False
        if "marks_awarded" not in q:
            q["marks_awarded"] = 0
    questions.sort(key=lambda q: (DIFFICULTY_ORDER.get(q.get("difficulty"), 2), q.get("keyword", "")))
    for idx, q in enumerate(questions, 1):
        q["id"] = idx
    return questions


def extract_resume_text(application: Application) -> str:
    """
    Extract readable text from a candidate's existing resume file if present.
    Never fabricates resumes or candidate skills. Returns empty string if no resume exists.
    """
    if not application.resume or not application.resume.name:
        return ""

    raw_bytes = None
    try:
        if hasattr(application.resume, "path") and os.path.exists(application.resume.path):
            with open(application.resume.path, "rb") as f:
                raw_bytes = f.read()
        else:
            application.resume.open("rb")
            raw_bytes = application.resume.read()
    except Exception as exc:
        logger.warning("Could not read resume file for application %s: %s", application.pk, exc)
        return ""

    if not raw_bytes:
        return ""

    ext = os.path.splitext(application.resume.name)[1].lower() or ".pdf"
    text = ""
    if ext == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw_bytes))
            text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        except Exception as exc:
            logger.warning("pypdf text extraction failed for app %s: %s", application.pk, exc)
        if text:
            return text
        try:
            import pymupdf
            doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
            pages = []
            for page in doc:
                page_text = page.get_text()
                if page_text:
                    pages.append(page_text)
            text = "\n".join(pages).strip()
        except Exception as exc:
            logger.warning("PyMuPDF text extraction failed for app %s: %s", application.pk, exc)
    elif ext in (".docx", ".doc"):
        try:
            import docx
            doc = docx.Document(io.BytesIO(raw_bytes))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    row_text = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                    if row_text:
                        paragraphs.append(row_text)
            text = "\n".join(paragraphs).strip()
        except Exception as exc:
            logger.warning("python-docx extraction failed for app %s: %s", application.pk, exc)

    if not text and ext not in (".pdf", ".docx", ".doc"):
        try:
            text = raw_bytes.decode("utf-8", errors="ignore").strip()
        except Exception:
            text = ""

    return text.strip()


FALLBACK_KEYWORD_CATALOG = [
    {
        "patterns": [r"\bpython\b", r"\bdjango\b", r"\bfastapi\b", r"\bflask\b"],
        "keyword": "Python & Backend Frameworks",
        "category": "Backend Engineering",
        "difficulty": "Easy",
        "question": "Can you explain how Django's ORM handles select_related vs prefetch_related, and when you should use each to prevent N+1 query bottlenecks?",
        "expected_answer": (
            "Listen for: select_related performs a SQL JOIN (best for foreign keys/one-to-one), while prefetch_related "
            "executes a separate SQL query with an 'IN' clause and merges Python-side (best for many-to-many/reverse relations). "
            "Strong candidates mention logging SQL queries, django-debug-toolbar, or .only()/.defer() to avoid loading unneeded columns."
        ),
        "follow_up_hint": "Ask how they handle database transactions with atomic blocks when updating multiple tables.",
    },
    {
        "patterns": [r"\btesting\b", r"\bpytest\b", r"\bunit test\b", r"\bci/cd\b", r"\btdd\b"],
        "keyword": "Automated Testing & CI/CD",
        "category": "Software Quality",
        "difficulty": "Easy",
        "question": "What is your philosophy on automated testing? How do you strike the right balance between unit tests, integration tests, and end-to-end regression tests?",
        "expected_answer": (
            "Listen for: The testing pyramid concept (abundant fast unit tests, selective integration tests with realistic DBs/fixtures, "
            "minimal brittle E2E tests). Mentions of mocking external I/O boundaries, coverage thresholds, and automated CI test gates."
        ),
        "follow_up_hint": "Ask how they handle flaky tests in automated CI pipelines.",
    },
    {
        "patterns": [r"\bdocker\b", r"\bkubernetes\b", r"\bk8s\b", r"\bcontainer\b"],
        "keyword": "Docker & Container Architecture",
        "category": "Cloud & DevOps",
        "difficulty": "Intermediate",
        "question": "In your production Docker containers, how do you minimize image size and ensure containers run securely with least privilege?",
        "expected_answer": (
            "Listen for: Multi-stage builds, minimal base images (like Alpine or distroless), avoiding storing secrets in image layers, "
            "running as a non-root USER, and properly handling SIGTERM signals for graceful process shutdowns."
        ),
        "follow_up_hint": "Ask how they configure healthchecks and readiness vs liveness probes.",
    },
    {
        "patterns": [r"\bredis\b", r"\bcache\b", r"\bcaching\b", r"\bmemcached\b"],
        "keyword": "Redis & Caching Strategies",
        "category": "Performance & Scalability",
        "difficulty": "Intermediate",
        "question": "How do you design a robust caching layer with Redis to protect against Cache Stampede (dog-piling) and Cache Penetration?",
        "expected_answer": (
            "Listen for: Cache stampede mitigation via distributed locks (Redlock / SET NX EX), probabilistic early recomputation (XFetch), "
            "or background cache warming. For cache penetration, storing empty/dummy results with short TTLs or using Bloom filters."
        ),
        "follow_up_hint": "Ask which Redis data structure they prefer for leaderboards or rate-limiting (Sorted Sets).",
    },
    {
        "patterns": [r"\brest\b", r"\bapi\b", r"\bmicroservices\b", r"\bgrpc\b"],
        "keyword": "REST API Architecture & Microservices",
        "category": "API Architecture",
        "difficulty": "Intermediate",
        "question": "How do you approach API versioning, backward compatibility, and graceful error responses across microservices without breaking active clients?",
        "expected_answer": (
            "Listen for: Semantic versioning, URI path or header versioning, additive schema changes (never deleting/renaming existing fields), "
            "standardized RFC 7807 problem details error format, and API gateways for contract validation."
        ),
        "follow_up_hint": "Ask about circuit breaking and rate limiting mechanisms.",
    },
    {
        "patterns": [r"\bpostgres\b", r"\bpostgresql\b", r"\bmysql\b", r"\bsql\b", r"\bdatabase\b"],
        "keyword": "PostgreSQL & Database Optimization",
        "category": "Database Engineering",
        "difficulty": "Hard",
        "question": "When a mission-critical database query suddenly slows down in production, walk me through your step-by-step diagnostic workflow using EXPLAIN ANALYZE.",
        "expected_answer": (
            "Listen for: Inspecting Seq Scan vs Index Scan, checking actual vs estimated row counts (outdated stats), "
            "identifying buffer cache misses/disk reads, analyzing slow joins (Hash Join vs Nested Loop), and verifying "
            "if composite indexes, partial indexes, or VACUUM ANALYZE are required."
        ),
        "follow_up_hint": "Ask when a B-Tree index might be ignored by the query planner (e.g. low cardinality or functions on columns).",
    },
    {
        "patterns": [r"\bcelery\b", r"\brabbitmq\b", r"\bkafka\b", r"\bqueue\b", r"\basync\b"],
        "keyword": "Asynchronous Message Queues",
        "category": "Distributed Systems",
        "difficulty": "Hard",
        "question": "When dispatching asynchronous tasks (e.g., via Celery, Kafka, or RabbitMQ), how do you guarantee idempotency and avoid duplicate processing on worker retries?",
        "expected_answer": (
            "Listen for: Utilizing unique transaction IDs or idempotency keys, database unique constraints or atomic lock checks, "
            "handling at-least-once message delivery guarantees, and configuring dead-letter queues (DLQ) for poisoned messages."
        ),
        "follow_up_hint": "Ask how task failures are alerted and whether tasks are transactional with the database commit.",
    },
    {
        "patterns": [r"\baws\b", r"\bcloud\b", r"\bazure\b", r"\bgcp\b", r"\bs3\b", r"\bec2\b"],
        "keyword": "Cloud Infrastructure & Resiliency",
        "category": "Cloud Architecture",
        "difficulty": "Hard",
        "question": "Describe your experience architecting high-availability infrastructure on the cloud. How do you design for multi-zone redundancy and disaster recovery?",
        "expected_answer": (
            "Listen for: Multi-AZ deployments behind Application Load Balancers, automated health-checking and auto-scaling groups, "
            "managed database failover (e.g. RDS Multi-AZ), Infrastructure as Code (Terraform), and regular backup recovery drills."
        ),
        "follow_up_hint": "Ask about RPO (Recovery Point Objective) and RTO (Recovery Time Objective).",
    },
]


def generate_fallback_questions(resume_text: str, candidate_name: str):
    """
    Generates keyword-grounded interview questions sorted strictly by
    Easy, Intermediate, and Hard by scanning the candidate's extracted resume text.
    Returns empty list if no resume text is provided.
    """
    if not resume_text or not resume_text.strip():
        return []

    matched = []
    text_lower = resume_text.lower()

    for item in FALLBACK_KEYWORD_CATALOG:
        found_pattern = None
        for pattern in item["patterns"]:
            match = re.search(pattern, text_lower)
            if match:
                found_pattern = match.group(0)
                break

        if found_pattern:
            snippet = ""
            for line in resume_text.splitlines():
                if found_pattern.lower() in line.lower() and len(line.strip()) > 15:
                    snippet = line.strip()
                    if len(snippet) > 160:
                        snippet = snippet[:157] + "..."
                    break
            if not snippet:
                snippet = f"Referenced '{found_pattern}' in candidate profile skills and experience overview."

            matched.append(
                {
                    "keyword": item["keyword"],
                    "context_snippet": snippet,
                    "category": item["category"],
                    "difficulty": item["difficulty"],
                    "question": item["question"],
                    "expected_answer": item["expected_answer"],
                    "follow_up_hint": item["follow_up_hint"],
                }
            )

    # Ensure representation across Easy, Intermediate, and Hard
    easy_count = sum(1 for q in matched if q["difficulty"] == "Easy")
    inter_count = sum(1 for q in matched if q["difficulty"] == "Intermediate")
    hard_count = sum(1 for q in matched if q["difficulty"] == "Hard")

    if easy_count == 0:
        matched.append(
            {
                "keyword": "Code Maintainability & Clean Code",
                "context_snippet": "Candidate engineering standards and teamwork experience.",
                "category": "Engineering Practices",
                "difficulty": "Easy",
                "question": "What code review practices or style guidelines do you follow to ensure readability and maintainability for other developers?",
                "expected_answer": "Listen for: PEP 8 or team style guides, small pull requests, meaningful naming, writing clear comments for 'why' rather than 'what', and automated linters.",
                "follow_up_hint": "Ask how they handle disagreements during code reviews.",
            }
        )
    if inter_count == 0:
        matched.append(
            {
                "keyword": "System Architecture & API Design",
                "context_snippet": "Candidate software engineering experience.",
                "category": "API Architecture",
                "difficulty": "Intermediate",
                "question": "When designing an API endpoint that handles high traffic, how do you handle rate-limiting, error handling, and payload validation?",
                "expected_answer": "Listen for: Token bucket / Leaky bucket algorithms, standardized HTTP status codes, validation serializers, and idempotent operations.",
                "follow_up_hint": "Ask what HTTP status code they use for validation errors vs auth errors.",
            }
        )
    if hard_count == 0:
        matched.append(
            {
                "keyword": "Distributed Systems & Scalability",
                "context_snippet": "Candidate technical background and assessment readiness.",
                "category": "System Design",
                "difficulty": "Hard",
                "question": "How do you design a distributed system to prevent race conditions when multiple concurrent requests update the same account balance or resource?",
                "expected_answer": "Listen for: Pessimistic locking (SELECT FOR UPDATE), optimistic locking with version columns, distributed locks with Redis/Redlock, and database ACID transaction isolation levels.",
                "follow_up_hint": "Ask about deadlocks and how to mitigate them.",
            }
        )

    return sort_and_enrich_questions(matched[:8])


def round_profile(round_number, total_rounds):
    """How hard a round's questions are. Later rounds go deeper; the last round is the hardest."""
    total_rounds = max(1, total_rounds or 1)
    if total_rounds == 1 or round_number <= 1:
        if total_rounds == 1:
            return {"mix": {"Easy": 2, "Intermediate": 3, "Hard": 2}, "focus": (
                "Cover the whole resume: confirm fundamentals, then probe one or two claims in depth.")}
        return {"mix": {"Easy": 2, "Intermediate": 3, "Hard": 1}, "focus": (
            "Screening round: confirm the fundamentals behind the main skills and check that the "
            "candidate really did the work their resume describes.")}
    if round_number >= total_rounds:
        return {"mix": {"Easy": 0, "Intermediate": 1, "Hard": 5}, "focus": (
            "Final round: the hardest questions. Go beyond the resume into design trade-offs, failure "
            "handling at scale, ownership of decisions, and how they would lead the work. Expect "
            "senior-level depth.")}
    return {"mix": {"Easy": 0, "Intermediate": 2, "Hard": 4}, "focus": (
        "Deep-dive round: pick the strongest projects from the resume and dig into how they were built, "
        "what broke, the numbers behind the claims, and the alternatives they rejected.")}


def _previous_round_context(application, round_number):
    """Questions already asked and interviewer notes from earlier rounds, so later rounds build on them."""
    asked, notes = [], []
    rounds = application.ai_round_questions or {}
    for rnd in range(1, round_number):
        for q in rounds.get(str(rnd), []):
            if q.get("question"):
                asked.append(f"Round {rnd}: {q['question']}")
    from .models import InterviewReview

    reviews = InterviewReview.objects.filter(
        attempt__application=application, round_number__lt=round_number
    ).order_by("round_number", "created_at")
    for review in reviews:
        text = " ".join(x for x in (review.candidate_summary, review.notes) if x).strip()
        if text:
            notes.append(f"Round {review.round_number} interviewer notes: {text[:800]}")
    return asked, notes


def _parse_ai_json(text):
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"```$", "", cleaned).strip()
    data = json.loads(cleaned)
    summary = ""
    if isinstance(data, dict):
        summary = str(data.get("resume_summary") or data.get("summary") or "")[:3000]
        data = data.get("questions", [])
    if not isinstance(data, list):
        raise ValueError("The AI response did not contain a list of questions.")
    questions = []
    for q in data:
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            continue
        questions.append({
            "keyword": str(q.get("keyword") or "General")[:120],
            "context_snippet": str(q.get("context_snippet") or "")[:400],
            "category": str(q.get("category") or "Technical")[:80],
            "difficulty": q.get("difficulty") or "Intermediate",
            "question": str(q["question"])[:1000],
            "expected_answer": str(q.get("expected_answer") or "")[:1500],
            "follow_up_hint": str(q.get("follow_up_hint") or "")[:500],
        })
    if not questions:
        raise ValueError("No usable questions in the AI response.")
    return summary, questions


def build_round_prompt(application, resume_text, round_number, total_rounds):
    profile = round_profile(round_number, total_rounds)
    mix = ", ".join(f"{n} {level}" for level, n in profile["mix"].items() if n)
    asked, notes = _previous_round_context(application, round_number)
    name = application.full_name or "the candidate"
    earlier = ""
    if asked:
        earlier += "\nQuestions already asked in earlier rounds (do not repeat them; go deeper instead):\n" + "\n".join(f"- {a}" for a in asked[:40])
    if notes:
        earlier += "\n\nWhat earlier interviewers noted (use it to target weak or unclear areas):\n" + "\n".join(notes)
    summary = application.ai_resume_summary.strip()
    summary_block = f"\nResume summary from an earlier round:\n{summary}\n" if summary else ""
    return f"""You are a principal engineer preparing interview round {round_number} of {total_rounds} with {name} for "{application.exam.title}".

Resume:
\"\"\"
{resume_text[:20000]}
\"\"\"
{summary_block}{earlier}

Round goal: {profile["focus"]}

Write exactly these questions: {mix}. Every question must come from something specific in this resume
(a project, tool, metric or claim), quoted in context_snippet. Prefer "how did you…" and "what would
happen if…" over definitions. For each, give the answer an interviewer should listen for, and one
follow-up that tests depth.

Return JSON only:
{{
  "resume_summary": "4-6 sentences: seniority, strongest areas, main projects, and claims worth verifying",
  "questions": [
    {{"keyword": "...", "context_snippet": "...", "category": "...", "difficulty": "Easy|Intermediate|Hard",
      "question": "...", "expected_answer": "...", "follow_up_hint": "..."}}
  ]
}}"""


def generate_round_questions(application, round_number=1, total_rounds=None, *, api_key_override=None,
                             model_override=None, candidate_text_override=None):
    """Create the AI questions for one interview round and save them on the application.

    Uses the key pool (see ai_client); falls back to resume keyword questions when no key works.
    Returns {"success", "count", "engine", "error"?}.
    """
    from . import ai_client

    total_rounds = total_rounds or getattr(application.exam, "interview_rounds_count", 1) or 1
    round_number = max(1, min(int(round_number or 1), total_rounds))
    resume_text = candidate_text_override or extract_resume_text(application)
    now = timezone.now()
    rounds = dict(application.ai_round_questions or {})

    def save(questions, error=""):
        rounds[str(round_number)] = questions
        application.ai_round_questions = rounds
        application.ai_questions_generated_at = now
        application.ai_questions_error = error
        fields = ["ai_round_questions", "ai_questions_generated_at", "ai_questions_error", "ai_resume_summary"]
        if round_number == 1:
            application.ai_interview_questions = questions
            fields.append("ai_interview_questions")
        application.save(update_fields=fields)

    if not resume_text:
        save([], "No verified candidate resume found, so there is nothing to base questions on. Ask the candidate to upload one.")
        return {"success": False, "count": 0, "engine": "none", "error": application.ai_questions_error}

    keys = None
    if api_key_override:
        keys = [ai_client._EnvKey(api_key=api_key_override, model=model_override or "")]
    prompt = build_round_prompt(application, resume_text, round_number, total_rounds)
    try:
        text, used = ai_client.generate_text(prompt, keys=keys)
        summary, questions = _parse_ai_json(text)
    except (ai_client.AIUnavailable, ValueError, json.JSONDecodeError) as exc:
        logger.warning("AI questions failed for application %s round %s: %s", application.pk, round_number, exc)
        fallback = generate_fallback_questions(resume_text, application.full_name)
        for q in fallback:
            q["round_number"] = round_number
        save(fallback, f"AI unavailable ({str(exc)[:300]}). Showing questions from resume keywords instead.")
        return {"success": bool(fallback), "count": len(fallback), "engine": "keywords", "error": application.ai_questions_error}

    if summary and not application.ai_resume_summary:
        application.ai_resume_summary = summary
    questions = sort_and_enrich_questions(questions)
    for q in questions:
        q["round_number"] = round_number
    save(questions)
    return {"success": True, "count": len(questions), "engine": f"Gemini ({used})"}


def generate_interview_questions_with_gemini(application, api_key_override=None, model_override=None,
                                             force=False, candidate_text_override=None, round_number=1):
    """Older name, kept for callers: generates round 1 (or the given round)."""
    return generate_round_questions(
        application, round_number, api_key_override=api_key_override, model_override=model_override,
        candidate_text_override=candidate_text_override,
    )


def get_gemini_config():
    """(has a usable key, default model). Kept for templates that show whether AI is set up."""
    from . import ai_client

    return ("pool" if ai_client.has_any_key() else ""), ai_client.default_model()
