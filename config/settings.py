import os
from pathlib import Path
from urllib.parse import urlsplit

import dj_database_url
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import URLValidator
from django.http.request import split_domain_port
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_list(name, default):
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


def origin(value, name):
    value = value.strip()
    try:
        URLValidator(schemes=["http", "https"])(value)
        parts = urlsplit(value)
        if (
            parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or parts.username is not None
            or parts.password is not None
            or "?" in value
            or "#" in value
        ):
            raise ValueError
    except (ValidationError, ValueError):
        raise ImproperlyConfigured(
            f"{name} must contain HTTP(S) origins without paths or credentials."
        ) from None
    return f"{parts.scheme}://{parts.netloc.lower()}"


DEBUG = os.getenv("REAPER_DEBUG", "false").strip().lower() == "true"
SECRET_KEY = os.getenv("SECRET_KEY", "development-only-not-for-production")
APP_URL = origin(os.getenv("APP_URL", "http://localhost:8000"), "APP_URL")
APP_HOST = urlsplit(APP_URL).hostname
ALLOWED_HOSTS = env_list(
    "ALLOWED_HOSTS", f"{APP_HOST},localhost,127.0.0.1,testserver" if DEBUG else APP_HOST
)
for host in ALLOWED_HOSTS:
    domain, port = split_domain_port(host.lstrip(".").lower())
    if not domain or port:
        raise ImproperlyConfigured(
            "ALLOWED_HOSTS must contain hostnames without schemes, paths or ports."
        )
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must not be empty.")
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=60
    )
}
TOKEN_ENCRYPTION_KEYS = [key.strip() for key in os.getenv("TOKEN_ENCRYPTION_KEYS", "").split(",")]
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_APP_SLUG = os.getenv("GITHUB_APP_SLUG", "")
AI_MODELS = [m.strip() for m in os.getenv("AI_MODELS", "gpt-4.1-mini").split(",") if m.strip()]
ANALYSIS_VERSION = "1.0"
DAILY_SCAN_LIMIT = int(os.getenv("DAILY_SCAN_LIMIT", "50"))
MAX_BATCH_SIZE = 20
MAX_EVIDENCE_CHARS = 48000
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "reaper",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "reaper.middleware.OAuthDiagnosticsMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "reaper.middleware.SecurityHeadersMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "reaper.context.site",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
AUTH_USER_MODEL = "reaper.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"}
]
LOGIN_URL = "/login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_DOMAIN = None
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_DOMAIN = None
CSRF_TRUSTED_ORIGINS = [
    origin(value, "CSRF_TRUSTED_ORIGINS") for value in env_list("CSRF_TRUSTED_ORIGINS", APP_URL)
]
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
# Only enable when the reverse proxy strips client-supplied forwarded headers.
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https")
    if os.getenv("TRUST_PROXY", "false").strip().lower() == "true"
    else None
)
# Preserve the public Host at the proxy; never trust arbitrary X-Forwarded-Host.
USE_X_FORWARDED_HOST = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
DATA_UPLOAD_MAX_MEMORY_SIZE = 65536
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
        "reaper.oauth": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": [], "propagate": False},
    },
}
if not DEBUG:
    if SECRET_KEY == "development-only-not-for-production" or len(SECRET_KEY) < 50:
        raise ImproperlyConfigured("Set a random SECRET_KEY of at least 50 characters.")
    if not TOKEN_ENCRYPTION_KEYS[0] or not APP_URL.startswith("https://"):
        raise ImproperlyConfigured("Production requires encryption keys and HTTPS APP_URL.")
    if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
        raise ImproperlyConfigured("Production requires PostgreSQL.")

if not AI_MODELS:
    raise ImproperlyConfigured("Configure at least one model in AI_MODELS.")
