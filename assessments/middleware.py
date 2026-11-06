import uuid

from django.conf import settings


class SecurityHeadersMiddleware:
    """Enforces restrictive Content-Security-Policy and modern security headers."""

    DEFAULT_CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data: https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "worker-src 'self' blob: data: https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "font-src 'self' data: https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' https://storage.googleapis.com https://cdn.jsdelivr.net; "
        "media-src 'self' blob:; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "base-uri 'self';"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Content-Security-Policy
        csp_header = "Content-Security-Policy-Report-Only" if getattr(settings, "CSP_REPORT_ONLY", False) else "Content-Security-Policy"
        csp_value = getattr(settings, "CONTENT_SECURITY_POLICY", self.DEFAULT_CSP)
        if csp_header not in response:
            response[csp_header] = csp_value

        # Additional security headers
        if "Permissions-Policy" not in response:
            response["Permissions-Policy"] = getattr(settings, "PERMISSIONS_POLICY", "camera=(self), microphone=(self), geolocation=(self)")
        if "Referrer-Policy" not in response:
            response["Referrer-Policy"] = getattr(settings, "SECURE_REFERRER_POLICY", "strict-origin-when-cross-origin")
        if "Cross-Origin-Opener-Policy" not in response:
            response["Cross-Origin-Opener-Policy"] = getattr(settings, "SECURE_CROSS_ORIGIN_OPENER_POLICY", "same-origin")
        if "X-Content-Type-Options" not in response:
            response["X-Content-Type-Options"] = "nosniff"

        return response


class CorrelationIdMiddleware:
    """Propagates or generates a correlation ID for request tracing and structured logging."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.request_id = req_id
        response = self.get_response(request)
        response["X-Request-ID"] = req_id
        return response
