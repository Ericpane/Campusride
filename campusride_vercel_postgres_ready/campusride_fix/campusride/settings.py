"""Production-ready Django settings for CampusRide.

The app uses SQLite automatically for local development and PostgreSQL when
DATABASE_URL is present (as it will be on Vercel after adding a Postgres
integration).
"""

import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Security / environment
# ---------------------------------------------------------------------------
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() in {"1", "true", "yes", "on"}

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        # Local development only. Never use this fallback in production.
        SECRET_KEY = "django-insecure-local-development-key-change-me"
    else:
        raise RuntimeError("DJANGO_SECRET_KEY must be set when DEBUG=False")


def _split_env(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    ".vercel.app",
    *(_split_env("ALLOWED_HOSTS")),
]

# Vercel exposes these automatically. Custom domains can be supplied through
# APP_URL / CSRF_TRUSTED_ORIGINS if you add one later.
csrf_origins = set(_split_env("CSRF_TRUSTED_ORIGINS"))
for env_name in ("APP_URL", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL", "VERCEL_BRANCH_URL"):
    value = os.getenv(env_name, "").strip()
    if not value:
        continue
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    csrf_origins.add(value.rstrip("/"))

# Local development origins.
csrf_origins.update({"http://localhost", "http://127.0.0.1", "http://0.0.0.0"})
CSRF_TRUSTED_ORIGINS = sorted(csrf_origins)

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # JavaScript in the existing UI reads csrftoken.
CSRF_USE_SESSIONS = False

# ---------------------------------------------------------------------------
# Django application
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rides",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "campusride.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "campusride.wsgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Local: SQLite remains convenient for development.
# Vercel: set DATABASE_URL from a managed PostgreSQL provider (for example,
# Vercel Marketplace/Neon). The same code then uses PostgreSQL automatically.
database_url = os.getenv("DATABASE_URL", "").strip()

if database_url:
    # Prefer psycopg 3 (installed from psycopg[binary]).
    parsed = urlparse(database_url)
    if parsed.scheme in {"postgres", "postgresql"}:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": parsed.path.lstrip("/"),
                "USER": unquote(parsed.username or ""),
                "PASSWORD": unquote(parsed.password or ""),
                "HOST": parsed.hostname or "",
                "PORT": str(parsed.port or 5432),
                "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
                "CONN_HEALTH_CHECKS": True,
                "DISABLE_SERVER_SIDE_CURSORS": True,
            }
        }
        # Preserve any query-string options supplied by the provider.
        if parsed.query:
            query = parse_qs(parsed.query)
            options = {}
            for key, values in query.items():
                if values:
                    options[key] = values[-1]
            if options:
                DATABASES["default"]["OPTIONS"] = options
    else:
        raise RuntimeError("DATABASE_URL must be a PostgreSQL URL (postgres:// or postgresql://)")
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# ---------------------------------------------------------------------------
# Password validation / internationalization
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_USE_FINDERS = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Custom auth URLs
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "rider_map"

# ---------------------------------------------------------------------------
# Vercel / serverless-friendly behavior
# ---------------------------------------------------------------------------
# Vercel can invoke multiple function instances. PostgreSQL provides the row
# locking that the trip optimizer expects; SQLite is only the local fallback.
# Do not rely on a writable local filesystem for persistent application data.
PYTHONUNBUFFERED = "1"
