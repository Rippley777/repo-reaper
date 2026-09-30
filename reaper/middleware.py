import json
import logging

from django.urls import reverse


class OAuthDiagnosticsMiddleware:
    """Log only routing metadata, including requests rejected by CSRF middleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in (reverse("oauth_start"), reverse("oauth_callback")):
            from reaper.views import oauth_callback_uri

            metadata = {
                "path": request.path,
                "method": request.method,
                "host": request.get_host(),
                "is_secure": request.is_secure(),
                "scheme": request.scheme,
                "callback_uri": oauth_callback_uri(),
            }
            for header in ("HTTP_HOST", "HTTP_X_FORWARDED_HOST", "HTTP_X_FORWARDED_PROTO"):
                metadata[header] = request.META.get(header, "")[:255]
            # JSON escapes control characters; never include URLs with queries or cookie headers.
            logging.getLogger("reaper.oauth").info("oauth_request %s", json.dumps(metadata))
        return self.get_response(request)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https://avatars.githubusercontent.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response["Referrer-Policy"] = "same-origin"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response["X-Frame-Options"] = "DENY"
        if request.user.is_authenticated or (
            request.path not in ("/", "/health") and not request.path.startswith("/static/")
        ):
            response["Cache-Control"] = "no-store, private"
        return response
