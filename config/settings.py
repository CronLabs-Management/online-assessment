"""
Django settings for the proctored assessment platform.

Configuration comes from environment variables so the same code runs locally
(SQLite, DEBUG on) and on cPanel (MySQL, DEBUG off). Set these in the cPanel
Python app environment variables panel or in a local .env loader of your choice.
"""

import os
from pathlib import Path

try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path):
    if not path.is_file():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and (
                (val.startswith('"') and val.endswith('"'))
                or (val.startswith("'") and val.endswith("'"))
            ):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val


_load_env_file(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


def env_number(name, default, cast=int):
    raw = os.environ.get(name, "").strip()
    return cast(raw) if raw else cast(default)


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
DEBUG = env_bool("DJANGO_DEBUG", default=False)

if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"
    else:
        raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = [
    origin for origin in env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "assessments.apps.AssessmentsConfig",
]

import importlib.util

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *(["whitenoise.middleware.WhiteNoiseMiddleware"] if importlib.util.find_spec("whitenoise") else []),
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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
                "assessments.context_processors.site_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# MySQL on cPanel when DB_NAME is set; SQLite for local development otherwise.
if os.environ.get("DB_NAME"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ["DB_NAME"],
            "USER": os.environ.get("DB_USER", ""),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "localhost"),
            "PORT": os.environ.get("DB_PORT", "3306"),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            # Candidate pages send several requests at once, and SQLite allows one writer at a time.
            # Wait for the lock instead of failing, and take it at the start of each transaction.
            "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "home"

SESSION_COOKIE_SECURE = not DEBUG
# Sessions end after 12 hours, which covers a full working day without a long-lived login on shared machines.
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
# Redirect plain HTTP to HTTPS and send HSTS. Behind cPanel's TLS termination the
# X-Forwarded-Proto header (set above) tells Django the request was secure.
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=not DEBUG)
SECURE_HSTS_SECONDS = env_number("DJANGO_HSTS_SECONDS", "0" if DEBUG else "3600")
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", default=False)

# Silence non-critical deployment advisories for HSTS subdomains and preload list
SILENCED_SYSTEM_CHECKS = [
    "security.W005",  # HSTS subdomains advisory (kept False to protect non-SSL subdomains)
    "security.W021",  # HSTS browser preload list advisory (kept False to avoid irreversible locking)
]

CSRF_FAILURE_VIEW = "assessments.views.csrf_failure_view"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Assessment platform settings
# ---------------------------------------------------------------------------

# Evidence images and candidate resumes are stored outside the web root and served only through
# authorized staff views. Set PRIVATE_MEDIA_ROOT to a folder above public_html on cPanel.
PRIVATE_MEDIA_ROOT = Path(os.environ.get("PRIVATE_MEDIA_ROOT", BASE_DIR / "private_media"))
MEDIA_ROOT = PRIVATE_MEDIA_ROOT
SNAPSHOT_MAX_BYTES = env_number("SNAPSHOT_MAX_BYTES", 300 * 1024)
SNAPSHOT_RATE_LIMIT_PER_10_MIN = env_number("SNAPSHOT_RATE_LIMIT_PER_10_MIN", 30)
EVENT_RATE_LIMIT_PER_MIN = env_number("EVENT_RATE_LIMIT_PER_MIN", 120)

# Code execution runs on a separate sandbox service (Judge0 or compatible).
# Leave JUDGE0_URL empty to disable scored coding until the service is available.
JUDGE0_URL = os.environ.get("JUDGE0_URL", "").rstrip("/")
JUDGE0_API_KEY = os.environ.get("JUDGE0_API_KEY", "")
JUDGE0_AUTH_HEADER = os.environ.get("JUDGE0_AUTH_HEADER", "X-Auth-Token")
CODE_CPU_TIME_LIMIT_S = env_number("CODE_CPU_TIME_LIMIT_S", 2, float)
CODE_MEMORY_LIMIT_KB = env_number("CODE_MEMORY_LIMIT_KB", 256 * 1024)
CODE_OUTPUT_LIMIT_KB = env_number("CODE_OUTPUT_LIMIT_KB", 64)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "assessments",
    }
}

DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000


# Browser-side face model. Self-host this file under static/ for production if the server
# cannot reach Google storage from the candidate's network.
FACE_MODEL_URL = os.environ.get(
    "FACE_MODEL_URL",
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
)

# Public address of the site, used to build links in emails (no trailing slash).
SITE_URL = os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/")
if not DEBUG and not os.environ.get("SITE_URL"):
    raise RuntimeError("SITE_URL must be set when DJANGO_DEBUG is off, so emailed links point at your site.")

# Email. Development prints messages to the console; production sends through SMTP
# (on cPanel, use the mail account created in cPanel and its SMTP host).
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if DEBUG else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = env_number("EMAIL_PORT", 465 if env_bool("EMAIL_USE_SSL", default=False) else 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")

# Smart SSL/TLS negotiation:
# - Port 465 is SMTPS (Implicit SSL): requires EMAIL_USE_SSL=True, EMAIL_USE_TLS=False
# - Port 587 is STARTTLS: requires EMAIL_USE_TLS=True, EMAIL_USE_SSL=False
# Respect explicit environment variables, otherwise deduce intelligently from EMAIL_PORT.
if "EMAIL_USE_SSL" in os.environ:
    EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", default=False)
else:
    EMAIL_USE_SSL = (EMAIL_PORT == 465)

if "EMAIL_USE_TLS" in os.environ:
    EMAIL_USE_TLS = False if EMAIL_USE_SSL else env_bool("EMAIL_USE_TLS", default=False)
else:
    EMAIL_USE_TLS = (EMAIL_PORT == 587 and not EMAIL_USE_SSL)

EMAIL_TIMEOUT = env_number("EMAIL_TIMEOUT", 5)
DEFAULT_FROM_EMAIL = (
    os.environ.get("DEFAULT_FROM_EMAIL")
    or os.environ.get("EMAIL_FROM")
    or os.environ.get("EMAIL_HOST_USER")
    or "Assessments <no-reply@localhost>"
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Code runner: "judge0" (isolated sandbox service), "local" (development only), or "" (auto).
CODE_RUNNER = os.environ.get("CODE_RUNNER", "").strip().lower()

# Users in this group may review attempts and record interview decisions, read-only otherwise.
REVIEWER_GROUP = "Reviewers"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "DEBUG" if DEBUG else "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "assessments": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
    },
}
