import hashlib
import time
from functools import wraps

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import JsonResponse

from reaper.models import RateBucket


class ServiceError(Exception):
    """Only fixed, user-safe messages should be supplied."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def cipher():
    keys = [key for key in settings.TOKEN_ENCRYPTION_KEYS if key]
    if not keys:
        raise ImproperlyConfigured("TOKEN_ENCRYPTION_KEYS must be configured.")
    return MultiFernet([Fernet(key.encode()) for key in keys])


def encrypt(value):
    return cipher().encrypt(value.encode()).decode() if value else ""


def decrypt(value):
    return cipher().decrypt(value.encode()).decode() if value else ""


def rate_limit(namespace, limit, seconds=60):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            identity = (
                f"user:{request.user.pk}"
                if request.user.is_authenticated
                else f"ip:{request.META.get('REMOTE_ADDR', '')}"
            )
            key = hashlib.sha256(f"{namespace}:{identity}".encode()).hexdigest()
            window = int(time.time()) // seconds
            with transaction.atomic():
                RateBucket.objects.get_or_create(key=key, defaults={"window": window})
                bucket = RateBucket.objects.select_for_update().get(pk=key)
                bucket.count = bucket.count + 1 if bucket.window == window else 1
                bucket.window = window
                bucket.save()
                allowed = bucket.count <= limit
            if not allowed:
                response = JsonResponse(
                    {"error": "Too many requests. Please try again shortly."}, status=429
                )
                response["Retry-After"] = str(seconds)
                return response
            return view(request, *args, **kwargs)

        return wrapped

    return decorate
