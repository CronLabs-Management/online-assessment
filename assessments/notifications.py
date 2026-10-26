"""
Email notifications for the assessment platform.

Architecture notes (replicated from other working cPanel projects):
- Every send is wrapped in a safe try/except so a failed email never crashes
  the request or rolls back any database transaction.
- Candidate credentials include both plain-text and rich responsive HTML
  versions, guaranteeing reliable readability across all email clients.
- On cPanel, external SMTP hosts sometimes fail due to hairpin NAT. When the
  primary send fails and we are using the SMTP backend, we try localhost
  loopback connections before giving up.
- Every send (or failure) is recorded in the AuditLog for staff visibility.
"""

import logging
import os
import secrets
import ssl
import threading

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives, get_connection, send_mail
from django.template.loader import render_to_string
from django.urls import reverse

from .models import Application, AuditLog, SiteSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _absolute(path):
    """Build a full URL from a relative path using the configured SITE_URL."""
    return f"{settings.SITE_URL}{path}"


def new_password():
    """A random password that is easy to type: letters and digits, grouped with dashes."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


# ---------------------------------------------------------------------------
# Low-level email delivery
# ---------------------------------------------------------------------------


# Fallback configs for cPanel hairpin NAT issues and custom fallback hosts.
def _get_prioritized_fallbacks():
    """Order fallbacks so that configured fallback hosts and loopbacks are attempted."""
    hosts = []
    custom_fallbacks = os.environ.get("EMAIL_FALLBACK_HOSTS") or os.environ.get("EMAIL_FALLBACK_HOST") or ""
    for host in custom_fallbacks.split(","):
        host = host.strip()
        if host and host not in hosts:
            hosts.append(host)

    hosts.extend(["127.0.0.1", "localhost"])

    base = []
    for h in hosts:
        base.append({"host": h, "port": 465, "use_tls": False, "use_ssl": True})
        base.append({"host": h, "port": 587, "use_tls": True, "use_ssl": False})
    base.append({"host": "127.0.0.1", "port": 25, "use_tls": False, "use_ssl": False})

    if getattr(settings, "EMAIL_USE_SSL", False) or getattr(settings, "EMAIL_PORT", 587) == 465:
        return [c for c in base if c["use_ssl"]] + [c for c in base if not c["use_ssl"]]
    elif getattr(settings, "EMAIL_USE_TLS", False) or getattr(settings, "EMAIL_PORT", 587) == 587:
        return [c for c in base if c["use_tls"]] + [c for c in base if not c["use_tls"]]
    return base


def _send_via_primary(subject, body, recipient, html_body=None):
    """Try to send using the globally configured Django email settings."""
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient],
        fail_silently=False,
        html_message=html_body,
    )


def _send_via_fallback(subject, body, recipient, html_body=None):
    """
    Try alternative hosts and localhost / loopback SMTP connections (cPanel hairpin NAT workaround).
    Uses a fast 3-second timeout and SSL context that bypasses IP hostname checks.
    """
    if settings.EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend":
        return False

    loopback_ssl_context = None
    try:
        loopback_ssl_context = ssl.create_default_context()
        loopback_ssl_context.check_hostname = False
        loopback_ssl_context.verify_mode = ssl.CERT_NONE
    except Exception:
        pass

    for cfg in _get_prioritized_fallbacks():
        if cfg["host"] == settings.EMAIL_HOST and cfg["port"] == settings.EMAIL_PORT:
            continue
        try:
            conn_kwargs = {
                "host": cfg["host"],
                "port": cfg["port"],
                "username": settings.EMAIL_HOST_USER,
                "password": settings.EMAIL_HOST_PASSWORD,
                "use_tls": cfg["use_tls"],
                "use_ssl": cfg["use_ssl"],
                "timeout": 3,
            }

            conn = get_connection(**conn_kwargs)
            # Explicitly set ssl_context on the connection instance to override Django's cached property
            if (cfg["use_ssl"] or cfg["use_tls"]) and loopback_ssl_context:
                conn.ssl_context = loopback_ssl_context

            msg = EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[recipient],
                connection=conn,
            )
            if html_body:
                msg.attach_alternative(html_body, "text/html")
            msg.send(fail_silently=False)
            logger.info(
                "Email delivered to %s via fallback %s:%s",
                recipient,
                cfg["host"],
                cfg["port"],
            )
            return True
        except Exception as fb_exc:
            logger.debug(
                "Fallback %s:%s failed: %s",
                cfg["host"],
                cfg["port"],
                fb_exc,
            )

    return False


def deliver_email(subject, body, recipient, html_body=None, connection=None):
    """
    Deliver an email with automatic fallback.

    Returns True when the message was accepted by any mail server,
    False when all attempts failed.
    """
    # 0. Shared connection attempt if provided (for batch efficiency)
    if connection:
        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[recipient],
                connection=connection,
            )
            if html_body:
                msg.attach_alternative(html_body, "text/html")
            msg.send(fail_silently=False)
            return True
        except Exception as exc:
            logger.warning("Shared connection delivery to %s failed: %s", recipient, exc)

    # Primary attempt: configured settings
    try:
        _send_via_primary(subject, body, recipient, html_body=html_body)
        return True
    except Exception as exc:
        logger.warning(
            "Primary email send to %s via %s failed: %s.",
            recipient,
            settings.EMAIL_HOST,
            exc,
        )

    # Fallback attempts: direct cPanel host, domain aliases, localhost loopback
    if _send_via_fallback(subject, body, recipient, html_body=html_body):
        return True

    logger.error("All email delivery attempts to %s failed.", recipient)
    return False


# ---------------------------------------------------------------------------
# Application-level notifications
# ---------------------------------------------------------------------------


def send_login_details(application, password, actor=None, connection=None):
    """
    Email the candidate their sign-in details.

    Returns True when the message was accepted by the mail server.
    The caller stores the new password only after this returns True,
    so a failed send never locks the candidate out.
    """
    user = application.candidate
    try:
        site_name = SiteSettings.load().site_name or "Scrambler Assessments"
    except Exception:
        site_name = "Scrambler Assessments"

    context = {
        "application": application,
        "exam": application.exam,
        "email": user.email,
        "password": password,
        "login_link": _absolute(reverse("login")),
        "exam_link": _absolute(reverse("exam_detail", args=[application.exam_id])),
        "site_name": site_name,
    }
    body = render_to_string("emails/login_details.txt", context)
    try:
        html_body = render_to_string("emails/login_details.html", context)
    except Exception as exc:
        logger.debug("Could not render HTML email template: %s", exc)
        html_body = None

    subject = f"Your login for {application.exam.title}"

    success = deliver_email(subject, body, user.email, html_body=html_body, connection=connection)

    # Audit regardless of outcome.
    if success:
        AuditLog.objects.create(
            actor=actor,
            action="email.login_details",
            target=f"application:{application.pk}",
            detail=f"sent to {user.email}",
        )
    else:
        AuditLog.objects.create(
            actor=actor,
            action="email.login_details.failed",
            target=f"application:{application.pk}",
            detail=f"all attempts to {user.email} failed",
        )

    return success


def send_login_details_async(application_id, password, actor_id=None):
    """
    Dispatch send_login_details in a background daemon thread so the
    request never blocks and the website never freezes.
    """
    def _worker():
        try:
            application = Application.objects.select_related("candidate", "exam").get(pk=application_id)
            actor = None
            if actor_id:
                actor = get_user_model().objects.filter(pk=actor_id).first()
            send_login_details(application, password, actor=actor)
        except Exception as exc:
            logger.error("Background email delivery failed for application %s: %s", application_id, exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def send_batch_async(items, actor_id=None):
    """
    Dispatch a batch of [(application_id, password), ...] in a background daemon thread.
    """
    def _worker():
        actor = None
        if actor_id:
            actor = get_user_model().objects.filter(pk=actor_id).first()
        for app_id, password in items:
            try:
                application = Application.objects.select_related("candidate", "exam").get(pk=app_id)
                send_login_details(application, password, actor=actor)
            except Exception as exc:
                logger.error("Background batch email failed for application %s: %s", app_id, exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread
