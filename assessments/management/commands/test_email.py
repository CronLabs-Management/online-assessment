"""
Send a test email to verify EMAIL_* settings are configured correctly.

Usage:
    python manage.py test_email someone@example.com
    python manage.py test_email someone@example.com --subject "Custom subject"
    python manage.py test_email someone@example.com --direct

This is the quickest way to confirm your cPanel mail account, SMTP host,
port, TLS / SSL, and credentials are all working after deployment.
"""
import smtplib
import ssl

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError

from assessments.notifications import deliver_email


class Command(BaseCommand):
    help = "Send a test email to verify EMAIL_* settings are configured correctly."

    def add_arguments(self, parser):
        parser.add_argument(
            "recipient",
            type=str,
            help="Email address to send the test message to.",
        )
        parser.add_argument(
            "--subject",
            type=str,
            default="Scrambler Assessments - Test Email",
            help="Custom subject line for the test email.",
        )
        parser.add_argument(
            "--direct",
            action="store_true",
            help="Bypass fallback and send directly via standard Django send_mail.",
        )

    def handle(self, *args, **options):
        recipient = options["recipient"]
        subject = options["subject"]
        direct = options["direct"]

        self.stdout.write("=" * 65)
        self.stdout.write("EMAIL CONFIGURATION DIAGNOSTICS")
        self.stdout.write("=" * 65)
        self.stdout.write(f"  Backend:    {settings.EMAIL_BACKEND}")
        self.stdout.write(f"  Host:       {settings.EMAIL_HOST}")
        self.stdout.write(f"  Port:       {settings.EMAIL_PORT}")
        self.stdout.write(f"  TLS:        {settings.EMAIL_USE_TLS}")
        self.stdout.write(f"  SSL:        {settings.EMAIL_USE_SSL}")
        self.stdout.write(f"  User:       {settings.EMAIL_HOST_USER or '(not set)'}")
        self.stdout.write(f"  Password:   {'********' if settings.EMAIL_HOST_PASSWORD else '(not set)'}")
        self.stdout.write(f"  From:       {settings.DEFAULT_FROM_EMAIL}")
        self.stdout.write(f"  Recipient:  {recipient}")
        self.stdout.write("-" * 65)

        # Pre-flight warnings for common configuration pitfalls
        warnings = []
        if not settings.EMAIL_HOST_PASSWORD or settings.EMAIL_HOST_PASSWORD == "change-me":
            warnings.append("EMAIL_HOST_PASSWORD is empty or set to 'change-me' in .env.")
        if settings.EMAIL_PORT == 465 and not settings.EMAIL_USE_SSL:
            warnings.append("Port 465 usually requires EMAIL_USE_SSL=True.")
        if settings.EMAIL_PORT == 587 and not settings.EMAIL_USE_TLS:
            warnings.append("Port 587 usually requires EMAIL_USE_TLS=True.")
        if settings.EMAIL_USE_SSL and settings.EMAIL_USE_TLS:
            warnings.append("EMAIL_USE_SSL and EMAIL_USE_TLS are both True! Django only supports one.")

        # Check if EMAIL_HOST resolves to Cloudflare proxy (which blocks SMTP)
        try:
            import socket
            resolved_ip = socket.gethostbyname(settings.EMAIL_HOST)
            if resolved_ip.startswith("104.") or resolved_ip.startswith("172.67."):
                warnings.append(
                    f"EMAIL_HOST '{settings.EMAIL_HOST}' resolves to Cloudflare ({resolved_ip})! "
                    "Cloudflare proxy does NOT support SMTP. Make sure the DNS record is set to 'DNS only' (grey cloud) in Cloudflare, "
                    "or change EMAIL_HOST to your direct server mail host."
                )
        except Exception:
            pass

        if warnings:
            for w in warnings:
                self.stdout.write(self.style.WARNING(f"  [WARN] {w}"))
            self.stdout.write("-" * 65)

        self.stdout.write(f"=> Attempting to send test email to {recipient}...")

        body = (
            "This is a test email from your Scrambler Assessments platform.\n\n"
            "If you received this message, your email subsystem is operating correctly!\n\n"
            f"Active Settings:\n"
            f"- Backend: {settings.EMAIL_BACKEND}\n"
            f"- Host: {settings.EMAIL_HOST}:{settings.EMAIL_PORT}\n"
            f"- SSL: {settings.EMAIL_USE_SSL} | TLS: {settings.EMAIL_USE_TLS}\n"
            f"- Sender: {settings.DEFAULT_FROM_EMAIL}\n"
        )
        html_body = (
            "<div style='font-family:sans-serif; max-width:550px; padding:20px; border:1px solid #e2e8f0; border-radius:8px;'>"
            "<h2 style='color:#0f172a; margin-top:0;'>Scrambler Assessments &mdash; Test Email</h2>"
            "<p style='color:#334155;'>If you received this message, your email subsystem is operating correctly!</p>"
            "<div style='background:#f8fafc; padding:12px; border-radius:6px; font-size:13px; font-family:monospace;'>"
            f"<b>Host:</b> {settings.EMAIL_HOST}:{settings.EMAIL_PORT}<br>"
            f"<b>SSL:</b> {settings.EMAIL_USE_SSL} &nbsp;|&nbsp; <b>TLS:</b> {settings.EMAIL_USE_TLS}<br>"
            f"<b>From:</b> {settings.DEFAULT_FROM_EMAIL}<br>"
            "</div>"
            "</div>"
        )

        try:
            if direct:
                send_mail(
                    subject=subject,
                    message=body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[recipient],
                    fail_silently=False,
                    html_message=html_body,
                )
                delivered = True
            else:
                delivered = deliver_email(subject, body, recipient, html_body=html_body)

            if delivered:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"\n SUCCESS: Test email successfully sent to {recipient}!"
                    )
                )
            else:
                raise CommandError(
                    "Email delivery failed. All delivery attempts (primary and fallback) were unsuccessful."
                )
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPException, OSError, ssl.SSLError, Exception) as exc:
            self.stdout.write(self.style.ERROR(f"\n DELIVERY FAILED: {exc}"))
            self.stdout.write("\nTroubleshooting tips for cPanel:")
            self.stdout.write(" 1. For port 465: Ensure EMAIL_USE_SSL=True and EMAIL_USE_TLS=False.")
            self.stdout.write(" 2. For port 587: Ensure EMAIL_USE_TLS=True and EMAIL_USE_SSL=False.")
            self.stdout.write(" 3. Check that EMAIL_HOST_USER matches the full email created in cPanel (e.g. noreply@domain.com).")
            self.stdout.write(" 4. Verify EMAIL_HOST_PASSWORD is exact with no extraneous trailing spaces.")
            self.stdout.write(" 5. If using cPanel, test with EMAIL_HOST=mail.yourdomain.com or 127.0.0.1.")
            self.stdout.write(" 6. If EMAIL_HOST is behind Cloudflare, switch Cloudflare DNS to 'DNS only' (grey cloud) or use your direct mail server host in EMAIL_HOST.")
            raise CommandError(f"Email check failed: {exc}") from exc
