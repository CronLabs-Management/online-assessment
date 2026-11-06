import io
import json
import logging
import os
import re

import requests
from django.utils import timezone

from .models import Application, SiteSettings

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

    if not text:
        try:
            text = raw_bytes.decode("utf-8", errors="ignore").strip()
        except Exception:
            text = ""

    return text.strip()


def get_gemini_config():
    """
    Retrieves the configured Gemini API key and model name.
    Strictly prioritized from the website Platform Settings:
    1. SiteSettings.gemini_api_key (Platform Settings)
    2. Environment variable: GEMINI_API_KEY or GOOGLE_API_KEY (fallback)
    """
    site = SiteSettings.load()
    api_key = (site.gemini_api_key or "").strip()
    if not api_key:
        api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    model = (site.gemini_model or "gemini-1.5-flash").strip()
    return api_key, model


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


def generate_interview_questions_with_gemini(
    application: Application,
    api_key_override: str = None,
    model_override: str = None,
    force: bool = False,
    candidate_text_override: str = None,
):
    """
    Extracts candidate resume text and calls the Google Gemini REST API using the key
    configured in Platform Settings.
    Produces questions sorted strictly by Easy, Intermediate, and Hard with explicit keywords.
    """
    resume_text = candidate_text_override or extract_resume_text(application)
    if not resume_text:
        application.ai_interview_questions = []
        application.ai_questions_generated_at = timezone.now()
        application.ai_questions_error = "No verified candidate resume found. Resume-derived AI questions are disabled."
        application.save(update_fields=["ai_interview_questions", "ai_questions_generated_at", "ai_questions_error"])
        return {
            "success": False,
            "error": "No verified candidate resume found. Resume-derived AI questions are disabled.",
            "count": 0,
            "questions": [],
        }

    configured_key, configured_model = get_gemini_config()
    api_key = (api_key_override or configured_key or "").strip()
    model = (model_override or configured_model or "gemini-1.5-flash").strip()

    candidate_name = application.full_name or application.candidate.username or f"Candidate {application.pk}"

    # If no API key is available in Platform Settings, use keyword extraction generator
    if not api_key:
        fallback_questions = generate_fallback_questions(resume_text, candidate_name)
        application.ai_interview_questions = fallback_questions
        application.ai_questions_generated_at = timezone.now()
        application.ai_questions_error = "Gemini API key is not configured in Platform Settings. Questions generated from resume keyword analysis (Easy, Intermediate, Hard)."
        application.save(update_fields=["ai_interview_questions", "ai_questions_generated_at", "ai_questions_error"])
        return {
            "success": True,
            "engine": "fallback_keyword_engine",
            "count": len(fallback_questions),
            "questions": fallback_questions,
            "notice": "Generated using resume keyword extractor. Configure Gemini API key in Platform Settings for live LLM generation.",
        }

    prompt = f"""You are a principal technical interviewer conducting a live face-to-face interview with candidate {candidate_name} for the position assessed under '{application.exam.title}'.

Candidate Resume Text:
\"\"\"
{resume_text}
\"\"\"

CRITICAL REQUIREMENTS:
1. Thoroughly analyze the candidate's resume above.
2. Extract specific technical keywords, claims, frameworks, tools, or projects explicitly cited in the resume.
3. For EVERY question, you MUST identify the exact KEYWORD or CLAIM from the resume the question is testing, along with a verbatim context quote from the resume.
4. You MUST sort and classify questions across three difficulty tiers: 'Easy', 'Intermediate', and 'Hard'.
   - Generate 2 'Easy' questions (fundamental concepts, clean coding, basic framework workflows).
   - Generate 2-3 'Intermediate' questions (hands-on architecture, containerization, caching, practical problem solving).
   - Generate 2 'Hard' questions (deep database internals, distributed consistency, concurrency, high-scale resiliency).
5. For EVERY question, write a concise EXPECTED ANSWER cheat-sheet outlining what the interviewer should listen for to verify whether the candidate has authentic experience.

OUTPUT FORMAT:
Return a strict JSON array of objects conforming to:
[
  {{
    "keyword": "string (the exact skill, tool, keyword, or claim from the resume, e.g. 'PostgreSQL Query Optimization')",
    "context_snippet": "string (verbatim quote or excerpt from the resume mentioning this keyword)",
    "category": "string (e.g. 'Backend Engineering', 'Database Design', 'Cloud Architecture', 'Distributed Systems')",
    "difficulty": "string ('Easy' | 'Intermediate' | 'Hard')",
    "question": "string (clear, direct, probing face-to-face question to ask the candidate)",
    "expected_answer": "string (cheat-sheet of key concepts, correct trade-offs, and indicators the interviewer should listen for)",
    "follow_up_hint": "string (optional quick follow-up probe)"
  }}
]
"""

    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.3,
        },
    }

    try:
        response = requests.post(gemini_url, headers=headers, json=payload, timeout=30)
        if response.status_code != 200:
            error_data = response.text
            try:
                err_json = response.json()
                error_data = err_json.get("error", {}).get("message", response.text)
            except Exception:
                pass
            raise RuntimeError(f"Gemini API returned HTTP {response.status_code}: {error_data}")

        res_json = response.json()
        candidates = res_json.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini API returned an empty candidate list.")

        text_content = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        if not text_content:
            raise RuntimeError("Gemini API returned empty text part.")

        cleaned_text = text_content.strip()
        if cleaned_text.startswith("```json"):
            cleaned_text = cleaned_text[7:]
        elif cleaned_text.startswith("```"):
            cleaned_text = cleaned_text[3:]
        if cleaned_text.endswith("```"):
            cleaned_text = cleaned_text[:-3]
        cleaned_text = cleaned_text.strip()

        questions_data = json.loads(cleaned_text)
        if not isinstance(questions_data, list):
            if isinstance(questions_data, dict) and "questions" in questions_data:
                questions_data = questions_data["questions"]
            else:
                raise ValueError("Parsed Gemini response is not a JSON list of questions.")

        raw_questions = []
        for q in questions_data:
            if not isinstance(q, dict):
                continue
            raw_questions.append(
                {
                    "keyword": str(q.get("keyword") or "General Technical Experience").strip(),
                    "context_snippet": str(q.get("context_snippet") or "Resume technical background").strip(),
                    "category": str(q.get("category") or "Technical Evaluation").strip(),
                    "difficulty": str(q.get("difficulty") or "Intermediate").strip(),
                    "question": str(q.get("question") or "").strip(),
                    "expected_answer": str(q.get("expected_answer") or "").strip(),
                    "follow_up_hint": str(q.get("follow_up_hint") or "").strip(),
                }
            )

        if not raw_questions:
            raise ValueError("No valid questions could be extracted from Gemini response.")

        # Sort strictly by Easy, Intermediate, Hard
        sorted_questions = sort_and_enrich_questions(raw_questions)

        # Persist to database
        application.ai_interview_questions = sorted_questions
        application.ai_questions_generated_at = timezone.now()
        application.ai_questions_error = ""
        application.save(update_fields=["ai_interview_questions", "ai_questions_generated_at", "ai_questions_error"])

        return {
            "success": True,
            "engine": f"gemini ({model})",
            "count": len(sorted_questions),
            "questions": sorted_questions,
        }

    except Exception as exc:
        logger.exception("Failed to generate interview questions with Gemini: %s", exc)
        err_msg = str(exc)
        fallback_questions = generate_fallback_questions(resume_text, candidate_name)
        application.ai_interview_questions = fallback_questions
        application.ai_questions_generated_at = timezone.now()
        application.ai_questions_error = f"Gemini API request failed ({err_msg}). Displaying resume keyword questions."
        application.save(update_fields=["ai_interview_questions", "ai_questions_generated_at", "ai_questions_error"])
        return {
            "success": False,
            "error": err_msg,
            "engine": "fallback_keyword_engine",
            "count": len(fallback_questions),
            "questions": fallback_questions,
        }
