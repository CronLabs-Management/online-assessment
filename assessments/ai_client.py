"""Calls to the AI provider through a pool of API keys, with automatic failover.

How a request is routed:

1. Candidate keys are the active keys that are not resting, lowest ``priority`` first. Within a
   priority, the key used least recently goes first, so traffic spreads evenly (round robin).
2. If a key is rate limited (HTTP 429, or a quota message), it rests: for the provider's Retry-After
   if given, otherwise a minute; a daily quota rests it for an hour before it is tried again. The same
   request then moves straight on to the next key.
3. A rejected key (HTTP 400 "API key not valid", 401, 403) rests for a day and shows its error in
   Settings, so an admin can fix or remove it.
4. Server errors and timeouts try the next key without resting the current one for long.

If every key fails, AIUnavailable is raised with a summary; callers fall back to keyword questions.
Keys from the GEMINI_API_KEY / GOOGLE_API_KEY environment variables are used only when the pool is empty.
"""

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import requests
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import AIKey, SiteSettings

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
REQUEST_TIMEOUT = 45
RATE_LIMIT_REST = timedelta(minutes=1)
QUOTA_REST = timedelta(hours=1)
BAD_KEY_REST = timedelta(days=1)
SERVER_ERROR_REST = timedelta(seconds=20)
MAX_KEYS_PER_REQUEST = 10


class AIUnavailable(Exception):
    """No key could complete the request."""


@dataclass
class _EnvKey:
    """A key from the environment, used when no keys are saved. Behaves like an AIKey for one request."""

    api_key: str
    model: str = ""
    pk: None = None
    label: str = "Environment key"


def default_model():
    try:
        model = (SiteSettings.load().gemini_model or "").strip()
    except Exception:
        model = ""
    return model or DEFAULT_MODEL


def keys_in_order(now=None):
    """Keys to try, in order. Resting keys are skipped; if all are resting, the soonest to recover is tried."""
    now = now or timezone.now()
    active = list(AIKey.objects.filter(is_active=True))
    if not active:
        env = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
        return [_EnvKey(api_key=env)] if env else []
    ready = [k for k in active if not k.cooling_until or k.cooling_until <= now]
    epoch = datetime(2000, 1, 1, tzinfo=UTC)  # Never-used keys go first.
    ready.sort(key=lambda k: (k.priority, k.last_used_at or epoch, k.pk))
    if ready:
        return ready[:MAX_KEYS_PER_REQUEST]
    # Everything is resting: try the one that recovers first rather than failing outright.
    return sorted(active, key=lambda k: k.cooling_until)[:1]


def has_any_key():
    return bool(keys_in_order())


def _retry_after(response):
    value = response.headers.get("Retry-After", "")
    if value.isdigit():
        return timedelta(seconds=min(int(value), 24 * 3600))
    match = re.search(r'"retryDelay":\s*"(\d+)s"', response.text or "")
    if match:
        return timedelta(seconds=min(int(match.group(1)), 24 * 3600))
    return None


def _error_message(response):
    try:
        return str(response.json().get("error", {}).get("message", ""))[:280] or f"HTTP {response.status_code}"
    except ValueError:
        return f"HTTP {response.status_code}"


def _mark(key, *, ok, rest=None, error=""):
    if key.pk is None:
        return
    now = timezone.now()
    fields = {"last_used_at": now}
    if ok:
        fields.update(success_count=F("success_count") + 1, cooling_until=None, last_error="")
    else:
        fields.update(failure_count=F("failure_count") + 1, last_error=error[:300], last_error_at=now)
        if rest:
            fields["cooling_until"] = now + rest
    AIKey.objects.filter(pk=key.pk).update(**fields)


def classify(response):
    """(rest period or None, short reason) for a failed response."""
    status = response.status_code
    message = _error_message(response)
    lowered = message.lower()
    if status == 429 or "resource_exhausted" in (response.text or "").lower() or "quota" in lowered:
        if "per day" in lowered or "daily" in lowered or "perday" in lowered.replace(" ", ""):
            return QUOTA_REST, f"Daily quota reached: {message}"
        return _retry_after(response) or RATE_LIMIT_REST, f"Rate limited: {message}"
    if status in (401, 403) or (status == 400 and "api key" in lowered):
        return BAD_KEY_REST, f"Key rejected: {message}"
    if status >= 500:
        return SERVER_ERROR_REST, f"Provider error: {message}"
    return None, message


def generate_text(prompt, *, json_output=True, temperature=0.4, session=None, keys=None):
    """Send ``prompt`` and return (text, key label). Tries each key in turn; raises AIUnavailable."""
    http = session or requests
    keys = keys if keys is not None else keys_in_order()
    if not keys:
        raise AIUnavailable("No AI API key is configured. Add one under Settings → AI keys.")
    failures = []
    for key in keys:
        model = re.sub(r"[^a-zA-Z0-9._-]", "", (key.model or default_model())) or DEFAULT_MODEL
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        if json_output:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        if key.pk is not None:
            # Claim the key now so a request starting at the same moment picks the next one.
            AIKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
        try:
            response = http.post(
                url, headers={"Content-Type": "application/json", "x-goog-api-key": key.api_key},
                json=payload, timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            _mark(key, ok=False, rest=SERVER_ERROR_REST, error=f"Network error: {exc.__class__.__name__}")
            failures.append(f"{key.label}: network error")
            continue
        if response.status_code != 200:
            rest, reason = classify(response)
            _mark(key, ok=False, rest=rest, error=reason)
            failures.append(f"{key.label}: {reason}")
            logger.warning("AI key %s failed (%s); trying the next key.", key.label, reason)
            if rest is None and response.status_code == 400:
                # A bad request (not a key problem) fails the same way on every key.
                break
            continue
        try:
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError):
            _mark(key, ok=False, error="Empty or blocked response")
            failures.append(f"{key.label}: empty response")
            continue
        _mark(key, ok=True)
        return text, key.label
    raise AIUnavailable("All AI keys failed. " + "; ".join(failures[:5]))


def test_key(key):
    """Send a tiny request with one key only. Returns (ok, message) and updates the key's health."""
    model = re.sub(r"[^a-zA-Z0-9._-]", "", (key.model or default_model())) or DEFAULT_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        response = requests.post(
            url, headers={"Content-Type": "application/json", "x-goog-api-key": key.api_key},
            json={"contents": [{"parts": [{"text": "Reply with the word OK."}]}]}, timeout=20,
        )
    except requests.RequestException as exc:
        return False, f"Could not reach the provider ({exc.__class__.__name__})."
    if response.status_code == 200:
        _mark(key, ok=True)
        return True, f"Working with model {model}."
    rest, reason = classify(response)
    _mark(key, ok=False, rest=rest, error=reason)
    return False, reason


@transaction.atomic
def reset_key(key_id):
    AIKey.objects.filter(pk=key_id).update(cooling_until=None, last_error="", last_error_at=None)
